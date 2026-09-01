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
python3 -m mtgcoach.webapp                       # local web UI, http://127.0.0.1:8765
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
| `webapp.py` | Local HTTP UI (stdlib `http.server` only) — see "Web UI" below |
| `static/index.html` | The entire frontend: inline CSS + vanilla JS, no build step, no framework |

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

`webapp.py` layers extra keys onto that payload before sending it to the
browser: `warnings` (from `ParsedDeck.warnings`), `missing_cards` (from
`scryfall.fetch_cards`), an `image` field added to each entry in
`cut_candidates` (a Scryfall image URL, looked up from the already-fetched
card dict — not part of `report.to_json()` itself), and `playstyle`
(`{archetype, name, blurb, plan, watch_out, focus, focus_label, aiming_at}`,
computed by `webapp._playstyle` from `classification.best` and, when a
target was requested, the aimed-at match — the same numbers and wording
`report.render`'s "ARCHETYPE DISTANCE" section prints to the terminal). If
you go looking for `image` or `playstyle` in `report.py`/`advice.py` and
don't find them, that's why — they're assembled in `webapp.analyze_deck`,
deliberately, so the CLI's JSON output stays exactly the documented contract
and only the web UI carries the extra weight.

## Caching, and the trap in it

Caches live in `.cache/mtgcoach/` (override with `MTGCOACH_CACHE`): card
lookups, the oracle tag index (~1.7 MB), the card corpus (~1 MB).

**The trap:** if you add a field to `scryfall._face_aware`, you must bump
`CARD_CACHE_SCHEMA`. Otherwise every already-cached card is served without the
new field and it silently reads as `None` everywhere. This cost real debugging
time when `edhrec_rank` was added, and happened again (correctly handled) when
`image` was added for the web UI: schema went 3 → 4. The same applies to
`TAG_CACHE_SCHEMA` and `CORPUS_SCHEMA`.

Other data-layer gotchas, all already handled — don't "fix" them back:

- Scryfall **rejects** the combined `A // B` name for split cards. Requests
  send the front face (`card_identifier`) and match the answer back by
  simplified name. Exports use both `//` and ` / ` as separators.
- Scryfall reports `{X}` as **zero**, making Fireball a one-drop. Each `{X}`
  adds 2 to the mana value used for the curve.
- Negative lookups are deliberately **not** cached, so a name that failed
  because of a bug is retried rather than remembered as missing forever.

## Web UI

`webapp.py` + `static/index.html` is a local desktop-style app on top of the
CLI's analysis path — added after the CLI existed, so it deliberately does not
reimplement any analysis logic. It's stdlib-only, same as the rest of the repo
(no Flask, no npm, no build step).

**Routes** (`webapp.Handler`):
- `GET /` — serves `static/index.html`.
- `GET /api/decks` — deck basenames from `decks/*.txt`.
- `GET /api/archetypes` — `[{key, name, blurb}]` for the target-archetype dropdown.
- `GET /api/roles` — `[{key, label}]` for the role vocabulary, so the frontend
  can label role counts without duplicating `roles.py`'s label strings.
- `POST /api/analyze` — body `{deck_name | deck_text, commander?, target?,
  blend?, cuts?}`, returns the JSON contract described above (plus the three
  webapp-only keys).

**Query strings on GET routes are real.** `self.path` from
`BaseHTTPRequestHandler` includes the query string (`/?deck=world_shaper`), so
every route comparison in `do_GET` splits on `?` first. This was a real bug
during development — `self.path == "/"` silently 404'd whenever a query
string was present. If you add a new `GET` route, split the path the same way.

**`?deck=<name>` on the page itself** preselects and runs that sample deck on
load (`static/index.html`, bottom of the `<script>`). It exists mainly so the
UI can be screenshotted/tested headlessly without scripting a click; it also
works as a shareable link. There's no equivalent for `deck_text` — pasted
decklists aren't in the URL, deliberately (keeps URLs short and doesn't put
someone's decklist in browser history/logs).

**Desktop launch.** `serve()` opens the UI in a browser "app mode" window
(Chrome/Edge/Chromium/Brave, `--app=<url>`, no tabs or address bar) instead of
a normal tab or a packaged native app — see `_open_app_window`. This was a
deliberate choice over `pywebview`/Electron: zero new dependencies, in
exchange for the window being an actual browser process. On macOS it probes
with `open -Ra "<app name>"` and launches with `open -na`; elsewhere it
`shutil.which`s a handful of binary names. Falls back to `webbrowser.open` if
none are found. Don't "simplify" this to always call `webbrowser.open` — that
was tried first and explicitly rejected for looking like a browser tab, not
an app.

**Testing.** There are no automated tests for `webapp.py` or
`static/index.html` — `tests/` only covers the core package. Verification so
far has been manual: `curl` against the routes, and headless-browser
screenshots for the frontend, e.g.:

```bash
python3 -m mtgcoach.webapp --no-browser --port 8765 &
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"deck_name":"world_shaper"}' http://127.0.0.1:8765/api/analyze
"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  --headless=new --disable-gpu --window-size=1200,2600 \
  --screenshot=/tmp/mtgcoach.png --virtual-time-budget=8000 \
  "http://127.0.0.1:8765/?deck=world_shaper"
```

(Any Chromium-family browser works for the screenshot; adjust the path.) A
change to the frontend isn't verified until you've actually looked at a
rendered screenshot — the JS has no build/lint step to catch mistakes early.

**Known gaps, not bugs** — things a UI bug-fix pass will likely notice and
should not "fix" without checking with the user first, since they're
unfinished rather than wrong:
- Partner/co-commanders: the API accepts a `commander` *list*
  (`decklist.parse_decklist(commander_override=...)` takes several names),
  but the UI's commander-override field is a single text input that only ever
  sends a one-element list. Partner decks work fine when the decklist's own
  `*CMDR*` markers or the leading-block heuristic detect both commanders; the
  override field just can't name two.
- `blend` and `cuts` are real `/api/analyze` parameters (control how many
  archetypes blend into the target profile, and how many cut candidates come
  back) but aren't exposed as UI controls — the frontend always uses the
  server-side defaults (`blend=2, cuts=8`).
- Card images come from the already-fetched card's `image` field
  (`scryfall._face_aware`, `normal` size, falling back to `small`). Split and
  adventure cards use the top-level `image_uris`; transform/modal
  double-faced cards use the front face's. This hasn't been checked against
  every DFC layout Scryfall has — a cut candidate that's a back-face-heavy
  card is the likeliest place an image silently comes back `null` (the
  frontend already handles that: `cut-noimg` renders the card name instead of
  a broken `<img>`).

**Deliberate UI decisions** — same spirit as the analysis-side ones below,
don't undo these while "cleaning up":
- The stat-grid's Ramp/Card draw/Removal/Board wipes tiles (`PILLAR_ROLES` in
  `static/index.html`) are a **fixed** set, shown regardless of what the deck
  actually leans on — unlike the "Key roles" panel below them, which is the
  dynamic top 10 non-zero role counts for *this* deck. Don't merge these two;
  they answer different questions ("does this deck have the staples every
  Commander deck needs" vs. "what is this deck actually built on").
  Also intentional: `PILLAR_ROLES` fixed labels ("Removal") intentionally
  simplify `roles.py`'s longer labels ("Spot removal / interaction") — leave
  that if you touch role labels elsewhere.
- The land-count stat tile's good/warn/bad thresholds
  (`landGap <= 1 / <= 3 / else`) are a **severity** scale, not a distance
  scale — the *worse* the deviation from `recommended_lands`, the stronger
  the color, ending in `--bad` past a gap of 3. An earlier version of this
  logic left large deviations uncolored (blank fell through past the `warn`
  check) — worse cases read as calmer than mild ones. If you touch this,
  check the *worst* case renders the loudest, not the quietest.
- The cut-candidates view deliberately does **not** show the raw EDHREC rank
  number (removed on request — a bare number like "9285" isn't meaningful
  without context). The `edhrec_rank` field is still in the JSON payload for
  anything else that wants it; just don't resurface it as a plain number in
  `cutTable()`. The `reason` text and `tier` pill are meant to carry that
  signal in human terms instead.
- Recommendations are sorted client-side by priority (`PRIORITY_ORDER`:
  high/medium/low) before rendering. `advice.all_recommendations` does not
  return them in priority order (it's grouped by analysis pass — focus notes,
  then fundamentals, then direction, then the Game Changer note last
  regardless of its `LOW` priority), so this sort is load-bearing for the UI
  reading as "most important first". Don't remove it thinking the backend
  already sorts.

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
