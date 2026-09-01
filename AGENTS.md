# AGENTS.md

Operational notes for working on this repo. `README.md` explains what the tool
does and why it is designed the way it is — read it for the reasoning. This
file covers how to work on it without breaking things, and which decisions are
deliberate rather than accidental.

## What this is

`mtgcoach` analyses a Magic: The Gathering Commander decklist and reports
descriptive statistics, a soft classification against 14 archetype profiles,
and prioritised advice about what to change. Card data comes from Scryfall.

It is aimed at the **casual** end of the format. That framing decides real
behaviour — see "Game Changers" below.

## Hard constraints

These are easy to violate and expensive to undo:

- **Python 3.9.** No `match`, no `X | Y` unions at runtime, no `dict[str,int]`
  annotations without `from __future__ import annotations`.
- **Standard library only.** There is no `requirements.txt` and there should
  not be one. No `requests`, no `numpy`. HTTP is `urllib`, maths is `math` and
  `statistics`. Breaking this should be a deliberate, discussed choice.
- **The report must stay within 80 columns.** Check with:
  `for d in decks/*.txt; do python3 -m mtgcoach -q "$d"; done | awk 'length>80'`

## Commands

```bash
python3 -m mtgcoach decks/animated_army.txt      # analyse a deck
python3 -m mtgcoach analyze DECK --json -        # machine-readable output
python3 -m mtgcoach archetypes                   # the reference profiles
python3 -m mtgcoach roles                        # the role vocabulary
python3 -m mtgcoach card "Beast Within"          # how one card is read
python3 -m unittest discover -s tests            # 90 tests, ~0.4s
python3 benchmarks/benchmark_cuts.py             # cut quality vs EDHREC
```

Note: the end-to-end test **skips silently** if the Scryfall cache is empty, so
a fresh clone reports `OK (skipped=1)`. Run any analysis once to populate the
cache, then re-run the tests to get real coverage.

## Layout

| Module | Owns |
| --- | --- |
| `scryfall.py` | HTTP, disk caches, bulk tag download, name resolution |
| `decklist.py` | Parsing decklists, commander and partner detection |
| `roles.py` | The fixed vocabulary of ~27 things a card can do |
| `synergy.py` | Deck-relative theme discovery by tag lift |
| `features.py` | Descriptive stats, the 29-feature vector, legality checks |
| `archetypes.py` | 14 reference profiles (hand-authored priors) |
| `classify.py` | Distance, softmax mixture, theme evidence |
| `advice.py` | Recommendations and cut selection — the largest, subtlest file |
| `corpus.py` | Card corpus for replaceability. **Not wired in — see below** |
| `report.py` | Terminal and JSON rendering |
| `cli.py` | Argument parsing and dispatch |

## The JSON contract

`report.to_json()` is the interface for anything built on top of this — a
front-end should consume it rather than importing package internals. It emits:

```
deck, commanders, commander_notes, color_identity, target_archetype,
stats{total_cards, lands, spells, recommended_lands, average_mana_value,
      effective_average_mana_value, median_mana_value, mana_sources, curve,
      types, pips, color_sources, main_creature_type, creature_count, x_spells},
roles{key: {count, share}}, feature_vector, themes[{tag, cards, lift, weight}],
classification{focus, separation, matches[{archetype, name, distance,
      affinity, fit, cosine, theme_support}]},
recommendations[{priority, kind, title, detail, evidence, cards}],
cut_candidates[{name, mana_value, reason, roles, tier, edhrec_rank}],
swap_budget, legality, unresolved
```

If you change this shape, treat it as a breaking API change.

## Caching, and the trap in it

Caches live in `.cache/mtgcoach/` (override with `MTGCOACH_CACHE`): card
lookups, the oracle tag index (~1.7 MB), the card corpus (~1 MB).

**The trap:** if you add a field to `scryfall._face_aware`, you must bump
`CARD_CACHE_SCHEMA`. Otherwise every already-cached card is served without the
new field and it silently reads as `None` everywhere. This cost real debugging
time when `edhrec_rank` was added. The same applies to `TAG_CACHE_SCHEMA` and
`CORPUS_SCHEMA`.

Other data-layer gotchas, all already handled — don't "fix" them back:

- Scryfall **rejects** the combined `A // B` name for split cards. Requests
  send the front face (`card_identifier`) and match the answer back by
  simplified name. Exports use both `//` and ` / ` as separators.
- Scryfall reports `{X}` as **zero**, making Fireball a one-drop. Each `{X}`
  adds 2 to the mana value used for the curve.
- Negative lookups are deliberately **not** cached, so a name that failed
  because of a bug is retried rather than remembered as missing forever.

## Decisions that are deliberate

Do not "simplify" these without reading why they exist:

- **Roles vs themes are different mechanisms.** `roles.py` is a fixed
  vocabulary for comparing decks *to each other*. `synergy.py` discovers what
  *this* deck is built on by tag lift, catching themes nobody named in advance
  (a deck half made of `{X}` spells has no role that describes it). Both are
  needed.
- **Only *declared* archetype features generate advice.** Each profile states
  the features it takes a position on; everything else falls back to
  `BASELINE` and is treated as "no opinion". Without this, a Lands Matter deck
  gets told to add Equipment because the baseline has some.
- **Universally useful cards and format staples are protected from cut
  suggestions.** Without these guards a synergy score cheerfully recommends
  cutting Sol Ring for being off-theme.
- **Whatever the commander does is the deck's identity** and is never proposed
  as a trim, including shape implications (an artifact commander means the deck
  wants noncreature permanents).
- **Game Changers are a caution, not a protection.** Scryfall's `game_changer`
  flag marks the 53 cards on Wizards' Commander Brackets list. Those cards move
  a deck *up* a bracket, which a casual table usually does not want, so the flag
  raises a power-level note and explicitly does **not** shield a card from being
  cut. A test pins this. Do not turn it into staple protection.
- **`corpus.py` is intentionally not called from the analysis path.**
  Replaceability was implemented, measured against the benchmark, and made the
  cut suggestions *worse* (35/90 vs 36/90 at top-10, 41/90 vs 46/90 at top-15).
  A parameter sweep found no setting that beat plain EDHREC rank. The module is
  kept because it is tested and works, and because suggesting *additions* —
  which the tool does not do yet — is what it is actually suited to. Do not
  wire it into scoring without re-running the benchmark.

## How to evaluate a change

Cut-selection quality is measurable. **Do not tune it by eye** — that is how a
regression nearly shipped.

```bash
python3 benchmarks/benchmark_cuts.py
MTGCOACH_BENCH_NO_CORPUS=1 python3 benchmarks/benchmark_cuts.py   # A/B variant
```

`benchmarks/precon_cut_rates.json` holds EDHREC community cut rates for 9
precons, fetched 2026-08-31. Current baseline:

| Metric | Score |
| --- | --- |
| top-10 overlap | 36/90 (40.0%) |
| top-15 overlap | 46/90 (51.1%) |

Per-deck it ranges from 7/10 (World Shaper) to 2/10 (Counter Blitz). Treat the
fixture as a dated snapshot, not permanent truth; re-fetch periodically, and
when you do, verify the EDHREC slug **and** the commander name — an earlier
fetch grabbed an unrelated precon with a similar name and returned plausible
but wrong numbers.

Classification has a weaker check: seven decks in `tests/` assert their
expected archetype end-to-end. Adding a feature dimension or changing weights
shifts every distance, so run the tests and re-check `fit` across all decks.

## Conventions

- `decks/*.txt` are fixtures. Several are the user's own precons; do not edit
  them. The user adds new ones — treat unfamiliar deck files as theirs.
- `README_ai.md` is the user's coursework AI-use disclosure. **Do not edit it**;
  it is their attestation.
- `.DS_Store` is tracked from the initial commit and shows as modified
  constantly. Leave it out of commits.
- Commit messages explain *why*, not just what, and end with the co-author
  trailer.
