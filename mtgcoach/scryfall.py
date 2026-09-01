"""Scryfall data access with an on-disk cache.

Two data sources are used:

1. ``POST /cards/collection`` for the printed characteristics of each card in a
   decklist (mana value, type line, colors, oracle text).
2. The ``oracle-tags`` bulk file, which is where Scryfall Tagger's community
   functional tags live ("ramp", "spot removal", "draw engine", ...).  Those
   tags are not exposed on the normal card endpoints.

Tags form a hierarchy (``tutor-creature-dragon`` -> ``tutor-creature`` ->
``tutor``).  We flatten every card's tag set upward through that hierarchy so
downstream code can match a single broad label and catch all of its children.

Everything here is stdlib only, and every network result is cached on disk so
repeated runs are fast and work offline.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import unicodedata
import urllib.error
import urllib.request
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

SPLIT_RE = re.compile(r"\s*//\s*|\s+/\s+")

API = "https://api.scryfall.com"
HEADERS = {
    "User-Agent": "mtgcoach/0.1 (commander deck analysis, educational project)",
    "Accept": "*/*",
}

# Scryfall asks for 50-100ms between requests.
_MIN_INTERVAL = 0.12
_last_request = [0.0]

TAGS_MAX_AGE_DAYS = 14

# Bump whenever _face_aware starts extracting a different set of fields, so
# entries cached by an older build are discarded rather than silently served
# without the new data.
CARD_CACHE_SCHEMA = 3

# Same idea for the tag index, which now carries the cosmetic-label set too.
TAG_CACHE_SCHEMA = 2

# Tag families that describe a card's printing or flavour rather than what it
# does: reprint cycles, alliterative names, vanilla-ness, draft signposts.
# Their descendants are excluded from theme detection, where they would
# otherwise dominate - "cycle-ecc-incarnation" appears in five cards of one
# deck and almost nowhere else, which looks like a very strong theme and means
# nothing.
COSMETIC_TAG_ROOTS = [
    "cycle", "card names", "flavors of vanilla", "draft signpost",
    "staple with set's mechanic", "un-design", "meme", "vanity card",
    "invitational card", "guest designer", "great-designer-search-3",
    "playtest forecast", "helper card", "paper-compatible",
    "digital to paper", "potentially black border", "unique type line",
]


class ScryfallError(RuntimeError):
    pass


class OfflineError(ScryfallError):
    """Raised when data is missing from the cache and the network is disabled."""


# --------------------------------------------------------------------------- #
# cache plumbing
# --------------------------------------------------------------------------- #

def cache_dir() -> str:
    path = os.environ.get("MTGCOACH_CACHE")
    if not path:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, ".cache", "mtgcoach")
    os.makedirs(path, exist_ok=True)
    return path


def _cache_path(name: str) -> str:
    return os.path.join(cache_dir(), name)


def _read_json(path: str):
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, payload) -> None:
    opener = gzip.open if path.endswith(".gz") else open
    tmp = path + ".tmp"
    with opener(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def _age_days(path: str) -> float:
    try:
        return (time.time() - os.path.getmtime(path)) / 86400.0
    except OSError:
        return float("inf")


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

def _throttle() -> None:
    delta = time.time() - _last_request[0]
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _last_request[0] = time.time()


def _http(url: str, data: Optional[bytes] = None, timeout: int = 90,
          attempts: int = 3) -> bytes:
    headers = dict(HEADERS)
    if data is not None:
        headers["Content-Type"] = "application/json"
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        _throttle()
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:  # includes HTTPError
            last_err = exc
            time.sleep(0.5 * (2 ** attempt))
    raise ScryfallError("request to %s failed: %s" % (url, last_err))


# --------------------------------------------------------------------------- #
# card characteristics
# --------------------------------------------------------------------------- #

def normalize_name(name: str) -> str:
    """Cache/lookup key for a card name: case- and whitespace-insensitive."""
    return " ".join(name.strip().lower().split())


def card_identifier(name: str) -> Dict[str, str]:
    """The identifier to send to ``/cards/collection`` for a decklist name.

    Scryfall rejects the combined ``A // B`` name of a split or double-faced
    card as a ``name`` identifier, but accepts either face on its own, so send
    the front face and match the answer back by its simplified name.

    Exports disagree on the separator: Moxfield writes ``Dusk // Dawn`` and
    others write ``Dusk / Dawn``, so both are recognised.  A lone slash only
    counts when it is spaced, which leaves names like ``Who/What/When`` alone.
    """
    faces = SPLIT_RE.split(name, 1)
    if len(faces) > 1 and faces[0].strip():
        return {"name": faces[0].strip()}
    return {"name": name}


def simplify_name(name: str) -> str:
    """Looser key that survives accents and punctuation ("Lim-Dul's Vault")."""
    folded = unicodedata.normalize("NFKD", name.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", folded).split())


def _face_aware(card: dict) -> dict:
    """Flatten a Scryfall card (including multi-faced ones) to the bits we use."""
    faces = card.get("card_faces") or []
    if faces and "mana_cost" in faces[0]:
        mana_cost = " // ".join(f.get("mana_cost", "") for f in faces)
        type_line = card.get("type_line") or " // ".join(
            f.get("type_line", "") for f in faces)
        oracle_text = "\n//\n".join(f.get("oracle_text", "") for f in faces)
        power = faces[0].get("power")
        toughness = faces[0].get("toughness")
    else:
        mana_cost = card.get("mana_cost", "")
        type_line = card.get("type_line", "")
        oracle_text = card.get("oracle_text", "")
        power = card.get("power")
        toughness = card.get("toughness")

    keywords = list(card.get("keywords") or [])
    for face in faces:
        keywords.extend(face.get("keywords") or [])

    return {
        "name": card.get("name", ""),
        "oracle_id": card.get("oracle_id") or (faces[0].get("oracle_id") if faces else None),
        "mana_cost": mana_cost,
        "mana_value": float(card.get("cmc", 0.0) or 0.0),
        "type_line": type_line,
        "oracle_text": oracle_text,
        "colors": card.get("colors") or (faces[0].get("colors") if faces else []) or [],
        "color_identity": card.get("color_identity") or [],
        "power": power,
        "toughness": toughness,
        "keywords": sorted(set(keywords)),
        "produced_mana": card.get("produced_mana") or [],
        # How widely the card is played in Commander. Low is popular; the
        # top few hundred are format staples.
        "edhrec_rank": card.get("edhrec_rank"),
        # On WotC's Commander Brackets "Game Changer" list - a card strong
        # enough to move a deck into a higher bracket.
        "game_changer": bool(card.get("game_changer")),
        "layout": card.get("layout", ""),
        "rarity": card.get("rarity", ""),
    }


def fetch_cards(names: Sequence[str], offline: bool = False,
                progress=None) -> Tuple[Dict[str, dict], List[str]]:
    """Return ``({normalized name: card dict}, [names not found])``.

    Results are cached in ``cards.json.gz`` and reused across runs, so a repeat
    analysis of the same deck needs no network at all.
    """
    cache_file = _cache_path("cards.json.gz")
    stored = _read_json(cache_file) or {}
    if stored.get("schema") == CARD_CACHE_SCHEMA:
        cache: Dict[str, dict] = stored.get("cards") or {}
    else:
        cache = {}      # written by an older field set; refetch on demand

    resolved: Dict[str, dict] = {}
    missing: List[str] = []
    to_fetch: List[str] = []
    for name in names:
        key = normalize_name(name)
        entry = cache.get(key)
        if entry:
            resolved[key] = entry
        else:
            if key in cache:
                del cache[key]      # purge a negative cached by an older run
            to_fetch.append(name)

    if to_fetch and offline:
        raise OfflineError(
            "%d card(s) are not in the local cache and --offline was requested "
            "(first missing: %s)" % (len(to_fetch), to_fetch[0]))

    dirty = False
    for start in range(0, len(to_fetch), 75):
        chunk = to_fetch[start:start + 75]
        if progress:
            progress("fetching card data %d-%d of %d"
                     % (start + 1, start + len(chunk), len(to_fetch)))
        body = json.dumps(
            {"identifiers": [card_identifier(n) for n in chunk]}).encode("utf-8")
        payload = json.loads(_http(API + "/cards/collection", data=body))

        loose: Dict[str, dict] = {}
        for raw in payload.get("data", []):
            card = _face_aware(raw)
            names = {card["name"]}
            if " // " in card["name"]:
                names.add(card["name"].split(" // ")[0])
            for name in names:
                cache[normalize_name(name)] = card
                resolved[normalize_name(name)] = card
                loose[simplify_name(name)] = card
            dirty = True

        for req in chunk:
            key = normalize_name(req)
            if key in resolved:
                continue
            # Scryfall matches loosely (accents, punctuation) and answers split
            # cards under their combined name, so re-attach by simplified name
            # to whatever came back.
            alias = loose.get(simplify_name(req))
            if alias is None:
                front = SPLIT_RE.split(req, 1)[0].strip()
                if front and front != req:
                    alias = loose.get(simplify_name(front))
            if alias is not None:
                cache[key] = alias
                resolved[key] = alias
                dirty = True
            else:
                # Negative results are deliberately not cached: a typo fixed
                # upstream, or a bug in how we build identifiers, would
                # otherwise stay "missing" forever.
                missing.append(req)

    if dirty:
        _write_json(cache_file, {"schema": CARD_CACHE_SCHEMA, "cards": cache})
    return resolved, missing


# --------------------------------------------------------------------------- #
# oracle tags
# --------------------------------------------------------------------------- #

def _download_oracle_tags() -> List[dict]:
    meta = json.loads(_http(API + "/bulk-data/oracle-tags", timeout=60))
    uri = meta.get("jsonl_download_uri") or meta.get("download_uri")
    if not uri:
        raise ScryfallError("Scryfall did not advertise an oracle-tags download URI")
    raw = _http(uri, timeout=180)
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass  # already decoded in transit
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def _ancestor_labels(tags: List[dict]) -> Dict[str, Set[str]]:
    """label -> {label} | {every ancestor label}, cycle-safe."""
    by_id = {t["id"]: t for t in tags}
    memo: Dict[str, Set[str]] = {}

    def walk(tag_id: str, stack: Set[str]) -> Set[str]:
        if tag_id in memo:
            return memo[tag_id]
        if tag_id in stack or tag_id not in by_id:
            return set()
        stack.add(tag_id)
        tag = by_id[tag_id]
        out = {tag["label"]}
        for parent in tag.get("parent_ids") or []:
            out |= walk(parent, stack)
        stack.discard(tag_id)
        memo[tag_id] = out
        return out

    return {by_id[tid]["label"]: walk(tid, set()) for tid in by_id}


_tag_cache_memo: Dict[str, dict] = {}


def _load_tag_cache(path: str):
    if path in _tag_cache_memo:
        return _tag_cache_memo[path]
    cached = _read_json(path)
    if isinstance(cached, dict) and cached.get("schema") == TAG_CACHE_SCHEMA:
        _tag_cache_memo[path] = cached
        return cached
    return None


def oracle_tag_index(offline: bool = False, refresh: bool = False,
                     progress=None) -> Dict[str, List[str]]:
    """``oracle_id -> [tag labels, flattened up the tag hierarchy]``."""
    return _tag_cache(offline, refresh, progress)["index"]


def cosmetic_labels(offline: bool = True, progress=None) -> Set[str]:
    """Tags that describe a printing rather than what a card does."""
    try:
        return set(_tag_cache(offline, False, progress).get("cosmetic") or [])
    except OfflineError:
        return set()


def _tag_cache(offline: bool = False, refresh: bool = False,
               progress=None) -> dict:
    index_file = _cache_path("oracle_tags_index.json.gz")
    if not refresh and _age_days(index_file) < TAGS_MAX_AGE_DAYS:
        cached = _load_tag_cache(index_file)
        if cached:
            return cached
    if offline:
        cached = _load_tag_cache(index_file)
        if cached:
            return cached
        raise OfflineError("no cached oracle tag index and --offline was requested")

    if progress:
        progress("downloading Scryfall oracle tags (once every %d days)"
                 % TAGS_MAX_AGE_DAYS)
    tags = _download_oracle_tags()
    closure = _ancestor_labels(tags)

    index: Dict[str, Set[str]] = {}
    for tag in tags:
        labels = closure.get(tag["label"], {tag["label"]})
        for tagging in tag.get("taggings") or []:
            oid = tagging.get("oracle_id")
            if oid:
                index.setdefault(oid, set()).update(labels)

    roots = set(COSMETIC_TAG_ROOTS)
    cosmetic = sorted(label for label, ancestors in closure.items()
                      if ancestors & roots
                      or label.endswith("-storyline-in-cards"))

    payload = {"schema": TAG_CACHE_SCHEMA,
               "index": {oid: sorted(labels) for oid, labels in index.items()},
               "cosmetic": cosmetic}
    _write_json(index_file, payload)
    return payload


def tag_vocabulary(offline: bool = False) -> Dict[str, dict]:
    """Every tag with its description, for `mtgcoach tags` style introspection."""
    vocab_file = _cache_path("oracle_tags_vocab.json.gz")
    if _age_days(vocab_file) < TAGS_MAX_AGE_DAYS:
        cached = _read_json(vocab_file)
        if cached:
            return cached
    if offline:
        return _read_json(vocab_file) or {}
    tags = _download_oracle_tags()
    vocab = {
        t["label"]: {
            "description": t.get("description") or "",
            "cards": len(t.get("taggings") or []),
        }
        for t in tags
    }
    _write_json(vocab_file, vocab)
    return vocab
