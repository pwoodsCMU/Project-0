# mtgcoach

A coaching tool for **Magic: The Gathering Commander** decks, aimed at
beginner players who want to better identify why their deck is underpreforming.

It does four things:

1. **Descriptive statistics** - mana curve, land count compared to curve
   target, colour pips versus colour sources, type breakdown, and a
   role estimate for the decks card makeup.
2. **Partial classification** - measures the distance from the deck to fourteen
   archetype reference profiles and reports the result. Includes a focus score
   indicating how committed the deck is to a plan.
3. **Recommendations** - prioritised advice based on both universal fundamentals
   and the distance from the closest archetype.
4. **Potential Cuts** - up to ten cards from the deck that serve the gameplan the
   least, along with reasoning for the cut. Based on deck synergy, staples, and 
   card quality.

Python 3.9+, standard library only. Card data comes from
[Scryfall](https://scryfall.com) and is cached on disk after the first run.

## Quick start

```bash
python3 -m mtgcoach.webapp
```

## Decklist format

Plain text exports from Moxfield, Archidekt, MTGGoldfish and friends all work:

```
// Commander
1 Adeline, Resplendent Cathar

// Deck
1 Sol Ring (LTC) 236 *F*
3x Lightning Bolt
Skullclamp
SB: 1 Pithing Needle
33 Plains
```

When copying from moxfield, remember to put a line between your commander(s) 
and the rest of your decklist!

Quantities, `x` suffixes, set codes, collector numbers and `*F*` foil markers
are all optional. Sideboard and maybeboard sections are parsed but excluded
from the analysis. The commander is taken from a `*CMDR*` marker, a
`Commander` section, `--commander "Name"`, or - failing those - a lone card in
the first block of a 100-card list.


### Roles

Every card is labelled with the functional roles it fills, based on
**Scryfall Tagger's oracle tags**, which are community-curated functional 
labels (`ramp`, `spot removal`, `draw engine`, `sacrifice outlet`) published
in Scryfall's `oracle-tags` bulk file. Tags form a hierarchy, so  a role only 
has to name the parent label to catch every child. Oracle-text and type-line 
patterns act as a fallback for cards not tagged by the community.

### Discovered themes

The role vocabulary is a fixed list of ~30 things a card can do, so
it is somewhat limited in which decks it can detect. To rectify this, the
deck "describes itself": for every Scryfall tag, `synergy.py` compares how
concentrated it is in this deck vs  how common it is across all tagged cards:

```
lift(tag) = share of this deck carrying it / share of all cards carrying it
```

A tag needs at least 3 cards and 2x lift to count as a theme, so one card
isn't mistaken for a plan. Art tags / non-functional tags are excluded. A card's
**synergy score** is how much of the deck's theme weight it carries.
ie. Sol Ring scores 0.88 in a ramp deck, 0.00 in Voltron.

### Feature vector

28 dimensions: 20 role densities (share of the deck's nonland cards) plus 8
shape features (creature-type concentration, creature/instant-sorcery/
noncreature-permanent share, average mana value, cheap-spell/top-end/land
share).

Commander abilities are weighted higher in the deck's shape, to account for
their heavy role in the decks plan. They can affect the mana curve evaluation
if they have a way to cheat out or reduce card costs, and their role densities
are weighted heaver.

### Distance and partial classification

Distance to each archetype is measured as follows:

```
d(deck, archetype) = sqrt( sum_i w_i (x_i - c_i)^2 / sum_i w_i )
```

Distances become affinities through a softmax, giving a distribution over
archetypes:

- **fit** - how close the best match is, `1 - d/0.20`.
- **focus** - one minus the normalised entropy of the affinity distribution;
  low focus means the deck is split across plans that may compete for slots.

### Archetype profiles

Fourteen ai-authored profiles, based on common EDH deck types - Go-Wide Aggro, 
Voltron, Midrange Value, Control, Combo, Stax/Prison, Big Mana, Lands Matter, 
Aristocrats, Reanimator, Spellslinger, +1/+1 Counters, Typal, Group Hug. 
They are **expert priors**: each declares only the features it takes a position on, 
so a Lands Matter deck is never told to add Equipment just because the baseline
has some. Discovered themes act as supporting evidence via each profile's
`signature_tags`.

### Recommendations

Based on the following in combination:

- **Fundamentals** use absolute targets and ignore archetype: land count
  (curve- and archetype-based), mana sources, coloured sources vs. pips,
  draw, interaction, board wipes, curve, and format legality. 
- **Direction** compares the deck against a blend of its nearest archetypes
  (or `--target`). Hybridizes to have a second archetype above 15% affinity
  and 40% fit.

### What to cut

The report sizes a **swap budget** from its own recommendations, then names
up to ten cards to cut, ranked in three tiers:

1. **Dead weight** - fills no functional role at all.
2. **Off plan** - useful cards, but not helping towards the deck's plan.
3. **Redundant** - on plan, but oversaturated in the deck already.

Within a tier the most expensive card by mana cost is cut first. 
Cards carrying the deck's discovered themes, format staples
(`edhrec_rank <= 600`, `advice.STAPLE_RANK`), and universally useful cards
(ramp, draw, removal, tutors, protection) are all protected.

### Validation against community upgrade guides

`benchmarks/benchmark_cuts.py` scores the cut list against EDHREC's published
per-card cut rates for nine precons:

**36/90 (40.0%)** of community top-10 cuts appear in the tool's top 10, 
**46/90 (51.1%)** in its top 15. 

## Limitations

- Archetype profiles are priors from one author's judgement, anchored to a
  handful of exemplar decks rather than fitted to a corpus of tournament data.
- Scryfall Tagger coverage is community-driven and uneven; the oracle-text
  backstop is regex, so individual cards are occasionally miscategorised.
- Role density is a count of cards, not a measure of quality or efficiency.
  The cut list softens this with EDHREC rank, but rank measures popularity,
  not power.
- Card quality is approximated from EDHREC rank. Comparing within a deck's
  own colours, roles and costs corrects much of that, but nothing here can
  see that two specific cards are redundant with each other.
- Theme discovery finds correlation in tags, not the actual combo.
- The tool sees a decklist, not a play pattern - it can't see mana base
  quality or that a deck is too strong or too weak for its table.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

92 tests: parsing, role assignment, theme discovery, feature maths, legality
checks, distance and classification, cut-candidate scoring and its guards,
corpus replaceability, and sample deck classification.

## Layout

```
mtgcoach/
  scryfall.py    Scryfall access, disk cache, oracle-tag hierarchy flattening
  decklist.py    decklist parsing and commander detection
  roles.py       the functional role vocabulary and its matching rules
  features.py    descriptive statistics, feature vector, legality checks
  synergy.py     deck-relative theme discovery by tag lift
  archetypes.py  the fourteen reference profiles
  classify.py    distance, softmax mixture, focus, blended target
  advice.py      fundamentals and direction recommendation passes, cut list
  corpus.py      card corpus for replaceability
  report.py      terminal and JSON rendering
  cli.py         argument parsing and command dispatch
  webapp.py      local web UI server
  static/        web UI frontend
decks/           exemplar decks, precons, and test decks
tests/           unit and end-to-end tests
benchmarks/      cut-list scoring against EDHREC community cut rates
```

Cached Scryfall data lives in `.cache/mtgcoach/` (override with
`MTGCOACH_CACHE`). The oracle tag file is re-downloaded every 14 days.
