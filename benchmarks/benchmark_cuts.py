#!/usr/bin/env python3
"""Benchmark mtgcoach's cut_candidates() against real EDHREC precon cut rates.

EDHREC publishes, for every preconstructed Commander deck, the percentage of
players who removed each card from it. That is ground truth for whether
mtgcoach's cut suggestions match what experienced players actually do.

This script:
  1. Loads benchmarks/precon_cut_rates.json (community data, fetched from
     EDHREC precon pages - see that file's "_meta" block for provenance).
  2. For each precon listed there, runs the exact same decks/<file> through
     mtgcoach's pipeline (decklist -> scryfall -> features -> classify ->
     advice.cut_candidates) that the CLI uses.
  3. Compares mtgcoach's top-10 and top-15 cut suggestions against EDHREC's
     top-10 community cut list for that precon, using name matching that is
     robust to punctuation, apostrophe style, and split/MDFC card names
     written as "Front / Back" (decklists) vs "Front" or "Front // Back"
     (EDHREC).
  4. Prints a per-deck table and an aggregate score.

Stdlib only. This is a measurement tool, not a test suite: it always exits 0,
even when a deck fails to load or a precon is missing data - failures are
reported in the output, not raised.

Usage:
    python3 benchmarks/benchmark_cuts.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mtgcoach import advice, classify, corpus, decklist, features, scryfall

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(BENCH_DIR, "precon_cut_rates.json")
DECKS_DIR = os.path.join(REPO_ROOT, "decks")

COMMUNITY_TOP_N = 10   # EDHREC "top-10 cut list" we compare against
LIMITS_TO_CHECK = (10, 15)


# --------------------------------------------------------------------------
# Name matching
#
# Card names need to survive three kinds of mismatch between a decklist and
# EDHREC's page text:
#   - punctuation/case/whitespace noise ("Ms. Bumbleflower" vs "ms bumbleflower")
#   - curly vs straight apostrophes ("Yuna’s Whistle" vs "Yuna's Whistle")
#   - split/MDFC cards: decklists here write "Front / Back" (single slash,
#     e.g. "Brightcap Badger / Fungus Frolic"); EDHREC sometimes shows just
#     the front face ("Brightcap Badger") and sometimes "Front // Back".
# --------------------------------------------------------------------------

def normalize(name: str) -> str:
    """Fold a single face name down to a robust comparison key."""
    name = unicodedata.normalize("NFKD", name)
    name = name.replace("’", "'").replace("‘", "'")
    name = name.replace("—", "-").replace("–", "-")
    name = name.replace(".", "")
    name = name.strip().strip(",")
    name = re.sub(r"\s+", " ", name)
    return name.lower()


def face_keys(name: str) -> List[str]:
    """All normalized keys a (possibly multi-faced) card name matches under.

    Returns the whole name as one key, plus each individual face if the name
    contains a "/" or "//" separator. A card matches another under this
    scheme if the two key-sets intersect at all.
    """
    keys = {normalize(name)}
    parts = re.split(r"\s*/{1,2}\s*", name)
    if len(parts) > 1:
        for part in parts:
            part = part.strip()
            if part:
                keys.add(normalize(part))
    return list(keys)


def build_lookup(cut_rates: List[dict]) -> Dict[str, dict]:
    """Map every normalized face-key of the community list to its entry."""
    lookup: Dict[str, dict] = {}
    for entry in cut_rates:
        for key in face_keys(entry["name"]):
            lookup[key] = entry
    return lookup


def find_match(card_name: str, lookup: Dict[str, dict]) -> Optional[dict]:
    for key in face_keys(card_name):
        if key in lookup:
            return lookup[key]
    return None


def _self_test() -> None:
    """Sanity-check the matcher before trusting it for scoring.

    In particular: decks/animated_army.txt lists "Brightcap Badger / Fungus
    Frolic" but EDHREC's Animated Army cut list says just "Brightcap Badger" -
    the task calls this out explicitly as a case the matcher must handle.
    """
    checks = [
        ("Brightcap Badger / Fungus Frolic", "Brightcap Badger", True),
        ("Dusk / Dawn", "Dusk", True),
        ("Dusk / Dawn", "Dawn", True),
        ("Realm-Cloaked Giant / Cast Off", "Realm-Cloaked Giant", True),
        ("Yuna's Whistle", "Yuna’s Whistle", True),
        ("Ms. Bumbleflower", "ms bumbleflower", True),
        ("Sol Ring", "Skullclamp", False),
    ]
    failures = []
    for deck_name, community_name, should_match in checks:
        lookup = build_lookup([{"name": community_name, "pct": 0.0}])
        got = find_match(deck_name, lookup) is not None
        if got != should_match:
            failures.append((deck_name, community_name, should_match, got))
    if failures:
        print("MATCHER SELF-TEST: FAILED")
        for deck_name, community_name, expected, got in failures:
            print("  %r vs %r: expected match=%s, got=%s"
                  % (deck_name, community_name, expected, got))
    else:
        print("Matcher self-test: OK (7/7, including Brightcap Badger // "
              "Fungus Frolic split-card case)")
    print()


# --------------------------------------------------------------------------
# Running mtgcoach
# --------------------------------------------------------------------------

def run_deck(deck_file: str):
    """Return (cut_candidates_list, missing_cards) for one deck, or raise."""
    path = os.path.join(DECKS_DIR, deck_file)
    tags = run_deck._tags
    d = decklist.load_decklist(path)
    cards, missing = scryfall.fetch_cards(d.unique_names(), progress=lambda x: None)
    analysis = features.analyze(d, cards, tags)
    # Mirror the CLI pipeline. Set MTGCOACH_BENCH_NO_CORPUS=1 to measure
    # without replaceability scoring, for A/B comparison.
    if not os.environ.get("MTGCOACH_BENCH_NO_CORPUS"):
        corpus.annotate(analysis, run_deck._corpus)
    result = classify.classify(analysis.vector, themes=analysis.themes)
    candidates = advice.cut_candidates(analysis, result, limit=max(LIMITS_TO_CHECK))
    return candidates, missing


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score_deck(candidates: List, cut_rates: List[dict]) -> Dict[int, dict]:
    lookup = build_lookup(cut_rates)
    top_community = sorted(cut_rates, key=lambda e: -e["pct"])[:COMMUNITY_TOP_N]
    top_community_keys = set()
    for entry in top_community:
        top_community_keys.update(face_keys(entry["name"]))

    out = {}
    for limit in LIMITS_TO_CHECK:
        subset = candidates[:limit]
        hits = []
        for c in subset:
            hit = find_match(c.name, lookup)
            if hit is not None and any(k in top_community_keys for k in face_keys(hit["name"])):
                hits.append((c.name, hit["name"], hit["pct"]))
        out[limit] = {
            "suggested_count": len(subset),
            "overlap": len(hits),
            "hits": hits,
        }
    return out


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def main() -> int:
    _self_test()

    if not os.path.exists(FIXTURE_PATH):
        print("No fixture at %s - nothing to benchmark." % FIXTURE_PATH)
        return 0

    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        fixture = json.load(fh)

    decks = fixture.get("decks", {})
    print("Loading Scryfall oracle tag index (used by every deck)...")
    tags = scryfall.oracle_tag_index()
    run_deck._tags = tags
    run_deck._corpus = (None if os.environ.get("MTGCOACH_BENCH_NO_CORPUS")
                        else corpus.load(progress=lambda m: None))

    print("=" * 78)
    print("mtgcoach cut_candidates() vs EDHREC community cut rates")
    print("Community list = EDHREC's top %d most-cut cards per precon." % COMMUNITY_TOP_N)
    print("Fixture fetched: %s" % fixture.get("_meta", {}).get("date_fetched", "unknown"))
    print("=" * 78)

    per_deck_results = []
    failures = []

    for deck_file, info in decks.items():
        precon_name = info.get("precon_name", deck_file)
        print("\n--- %s (%s) ---" % (precon_name, deck_file))
        deck_path = os.path.join(DECKS_DIR, deck_file)
        if not os.path.exists(deck_path):
            msg = "SKIPPED: decks/%s not found" % deck_file
            print(msg)
            failures.append((deck_file, msg))
            continue
        try:
            candidates, missing = run_deck(deck_file)
        except Exception as exc:  # noqa: BLE001 - this is a measurement tool
            msg = "FAILED to run mtgcoach pipeline: %r" % (exc,)
            print(msg)
            failures.append((deck_file, msg))
            continue

        if missing:
            print("  (%d card(s) not found by Scryfall lookup: %s)"
                  % (len(missing), ", ".join(missing[:5]) + ("..." if len(missing) > 5 else "")))

        cut_rates = info.get("cut_rates", [])
        if not cut_rates:
            msg = "SKIPPED: no cut_rates recorded for this precon"
            print(msg)
            failures.append((deck_file, msg))
            continue

        scores = score_deck(candidates, cut_rates)
        per_deck_results.append((deck_file, precon_name, scores))

        for limit in LIMITS_TO_CHECK:
            s = scores[limit]
            print("  top-%2d suggestions: %d/%d suggested cards are in EDHREC's "
                  "top-%d community cuts  (overlap %d/%d)"
                  % (limit, s["overlap"], s["suggested_count"], COMMUNITY_TOP_N,
                     s["overlap"], COMMUNITY_TOP_N))
            if s["hits"]:
                names = ", ".join("%s (%.1f%%)" % (name, pct) for _, name, pct in s["hits"])
                print("    matched: %s" % names)

    print("\n" + "=" * 78)
    print("AGGREGATE (%d precon(s) scored)" % len(per_deck_results))
    print("=" * 78)
    if per_deck_results:
        for limit in LIMITS_TO_CHECK:
            total_overlap = sum(scores[limit]["overlap"] for _, _, scores in per_deck_results)
            total_possible = COMMUNITY_TOP_N * len(per_deck_results)
            pct = 100.0 * total_overlap / total_possible if total_possible else 0.0
            print("  top-%2d: %d/%d overlap across all decks (%.1f%% of community "
                  "top-%d cuts captured on average)"
                  % (limit, total_overlap, total_possible, pct, COMMUNITY_TOP_N))
    else:
        print("  No decks were successfully scored.")

    if failures:
        print("\nFAILURES / SKIPPED (%d):" % len(failures))
        for deck_file, msg in failures:
            print("  %s: %s" % (deck_file, msg))

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
