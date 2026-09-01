"""Local web UI.

    python3 -m mtgcoach.webapp              # serve on http://127.0.0.1:8765

A thin HTTP layer over the existing analysis pipeline. It calls the same
functions ``cli.cmd_analyze`` does and returns exactly ``report.to_json()``'s
contract (see AGENTS.md) plus ``warnings``/``missing_cards``, so any future
change to the analysis engine reaches the UI without touching this file. No
third-party dependencies: stdlib ``http.server`` + a static HTML/JS page.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Sequence

from . import advice, archetypes as archetypes_mod, classify, decklist, features
from . import report, scryfall
from .roles import ROLES

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECKS_DIR = os.path.join(REPO_ROOT, "decks")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def list_decks() -> list:
    if not os.path.isdir(DECKS_DIR):
        return []
    return sorted(f[:-4] for f in os.listdir(DECKS_DIR) if f.endswith(".txt"))


def list_archetypes() -> list:
    return [{"key": a.key, "name": a.name, "blurb": a.blurb}
            for a in archetypes_mod.ARCHETYPES]


def list_roles() -> list:
    return [{"key": r.key, "label": r.label} for r in ROLES]


def analyze_deck(deck_text: Optional[str] = None, deck_name: Optional[str] = None,
                 commander: Optional[Sequence[str]] = None,
                 target_key: Optional[str] = None,
                 blend: int = 2, cuts: int = 8) -> dict:
    if deck_name:
        path = os.path.join(DECKS_DIR, deck_name + ".txt")
        if not os.path.isfile(path):
            raise ApiError("unknown deck %r" % deck_name, 404)
        parsed = decklist.load_decklist(path, commander_override=commander)
        label = deck_name
    elif deck_text:
        parsed = decklist.parse_decklist(deck_text, source="<pasted>",
                                         commander_override=commander)
        label = "<pasted>"
    else:
        raise ApiError("provide deck_name or deck_text")

    if not parsed.entries:
        raise ApiError("no cards found in decklist")

    target = None
    if target_key:
        target = archetypes_mod.ARCHETYPES_BY_KEY.get(target_key)
        if target is None:
            raise ApiError("unknown archetype %r" % target_key)

    try:
        cards, missing = scryfall.fetch_cards(parsed.unique_names())
        tags = scryfall.oracle_tag_index()
    except scryfall.ScryfallError as exc:
        raise ApiError("could not reach Scryfall: %s" % exc, 502)

    analysis = features.analyze(parsed, cards, tags)
    classification = classify.classify(analysis.vector, themes=analysis.themes)
    recommendations = advice.all_recommendations(
        analysis, classification, blend_top=blend, target_archetype=target)
    needed = advice.swap_budget(recommendations)
    filler = any(r.kind == "quality" for r in recommendations)
    limit = cuts if filler else min(cuts, max(needed, 3))
    cut_list = advice.cut_candidates(analysis, classification, blend_top=blend,
                                     target_archetype=target, limit=limit)

    payload = report.to_json(analysis, classification, recommendations, label,
                             target=target, cuts=cut_list)
    payload["warnings"] = parsed.warnings
    payload["missing_cards"] = missing
    for cut in payload["cut_candidates"]:
        card = cards.get(scryfall.normalize_name(cut["name"]))
        cut["image"] = card.get("image") if card else None
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str) -> None:
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html")
        elif path == "/api/decks":
            self._send_json({"decks": list_decks()})
        elif path == "/api/archetypes":
            self._send_json({"archetypes": list_archetypes()})
        elif path == "/api/roles":
            self._send_json({"roles": list_roles()})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path != "/api/analyze":
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        try:
            payload = analyze_deck(
                deck_text=body.get("deck_text"),
                deck_name=body.get("deck_name"),
                commander=body.get("commander"),
                target_key=body.get("target"),
                blend=int(body.get("blend", 2)),
                cuts=int(body.get("cuts", 8)))
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
            return
        self._send_json(payload)


# Chrome/Edge/Chromium's "app mode" opens a chromeless window (no tabs, no
# address bar) pointed at a URL - the cheapest way to a desktop-app feel with
# no new dependency. Falls back to a normal browser tab if none are found.
_APP_MODE_BROWSERS_MAC = ["Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"]
_APP_MODE_BROWSERS_OTHER = ["google-chrome", "chrome", "chromium", "chromium-browser",
                           "microsoft-edge", "msedge", "brave-browser"]


def _open_app_window(url: str) -> None:
    if platform.system() == "Darwin":
        for name in _APP_MODE_BROWSERS_MAC:
            found = subprocess.run(["open", "-Ra", name], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            if found.returncode == 0:
                subprocess.Popen(["open", "-na", name, "--args", "--app=%s" % url])
                return
    else:
        for exe in _APP_MODE_BROWSERS_OTHER:
            path = shutil.which(exe)
            if path:
                subprocess.Popen([path, "--app=%s" % url])
                return
    webbrowser.open(url)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d" % (host, port)
    print("mtgcoach web UI at %s (Ctrl+C to stop)" % url)
    if open_browser:
        _open_app_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Serve the mtgcoach web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(args.host, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
