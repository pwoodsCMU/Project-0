# mtgcoach

A command-line coach for **Magic: The Gathering Commander** decks, aimed at
players who can build a working deck but are not sure *why* it underperforms.

It does three things:

1. **Descriptive statistics** - mana curve, land count against a curve-aware
   target, colour pips versus colour sources, type breakdown, and a
   role-by-role account of what the cards in the deck actually do.
2. **Partial classification** - measures the distance from the deck to thirteen
   archetype reference profiles and reports the result as a *mixture*
   ("58% Aristocrats, 22% Go-Wide Aggro"), plus a focus score for how
   committed the deck is to any plan at all.
3. **Recommendations** - prioritised, role-level advice ("add about four
   pieces of spot removal", "you need more control"), never specific card
   names, from two sources: universal fundamentals, and the gap between the
   deck and the archetypes it is closest to.

Python 3.9+, standard library only. Card data comes from
[Scryfall](https://scryfall.com) and is cached on disk after the first run.

## Quick start

```bash
python3 -m mtgcoach decks/beginner_goodstuff.txt
```

```bash
python3 -m mtgcoach analyze decks/my_deck.txt --commander "Korvold, Fae-Cursed King"
```

Paste a list straight in:

```bash
pbpaste | python3 -m mtgcoach analyze -
```

Building toward a specific plan and want to know what is missing:

```bash
python3 -m mtgcoach analyze decks/my_deck.txt --target voltron
```

Other commands:

| Command | What it does |
| --- | --- |
| `mtgcoach analyze DECK` | the full report (default; `mtgcoach DECK` also works) |
| `mtgcoach archetypes` | the thirteen reference profiles and their game plans |
| `mtgcoach roles` | the functional role vocabulary |
| `mtgcoach card NAME...` | the Scryfall tags and roles of individual cards |
| `mtgcoach fit KEY DECK...` | build a new archetype profile by averaging decks |

Useful flags: `--json out.json`, `--offline`, `--top N`, `--roles N`,
`--blend N`, `--cuts N`, `--profiles FILE`, `--refresh-tags`, `--no-color`.

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

Quantities, `x` suffixes, set codes, collector numbers and `*F*` foil markers
are all optional. Sideboard and maybeboard sections are parsed but excluded
from the analysis. The commander is taken from a `*CMDR*` marker, a
`Commander` section, `--commander "Name"`, or - failing those - a lone card in
the first block of a 100-card list.

## How it works

```
decklist -> Scryfall lookup -> functional roles -> feature vector
                                                        |
                          archetype profiles -> weighted distance -> mixture
                                                        |
                                     fundamentals + gaps -> recommendations
```

### Roles

Every card is labelled with the functional roles it fills - a card can hold
several at once. The primary signal is **Scryfall Tagger's oracle tags**,
which are community-curated functional labels (`ramp`, `spot removal`,
`draw engine`, `sacrifice outlet`) published in Scryfall's `oracle-tags` bulk
file. Tags form a hierarchy, so `mana dork` and `ritual` both roll up to
`ramp`; `mtgcoach` flattens each card's tags up that hierarchy, which means a
role only has to name the parent label to catch every child. Oracle-text and
type-line patterns act as a backstop for cards Tagger has not reached.

Deliberate detail: land tutors (Rampant Growth) count as **ramp** and not as
**tutors**, because for deckbuilding purposes they are not the same thing.

The `typal` role deliberately counts *payoffs* only - lords and cards that name
a creature type. How concentrated the creatures themselves are is a separate
measurement (below), because a deck can have thirty Elementals and no payoffs,
or six payoffs and no Elementals, and those need different advice.

`mtgcoach roles` prints the whole vocabulary; `mtgcoach card "Beast Within"`
shows how a single card was read.

### Feature vector

28 dimensions: 20 role densities plus 8 shape features (creature-type
concentration, creature share, instant/sorcery share, noncreature permanent
share, normalised average mana value, cheap-spell share, top-end share, land
share). Role densities are expressed as a share of the deck's **nonland**
cards, so decks of slightly different shapes stay comparable.

**Creature-type concentration** is the share of creatures sharing the deck's
dominant type. Changelings count as whatever the tribe turns out to be, and
when the commander is a creature - or carries a `typal-<type>` tag - its own
type gets first refusal on near-ties, so an Elemental commander means the deck
is measured on Elementals.

**The commander is weighted up.** It is castable in every single game, so it
says more about the deck than any one of the other 99 cards; role densities
count it three times (`features.COMMANDER_WEIGHT`). The descriptive counts in
the report stay honest at one card. With partners both commanders count, and
each keeps its own identity - notes about what a commander enables are
attributed by name rather than merged into one sentence about "your
commander".

### Distance and partial classification

Distance to each archetype is a weighted Euclidean distance normalised by the
total weight, so it reads as a root-mean-square deviation per feature:

```
d(deck, archetype) = sqrt( sum_i w_i (x_i - c_i)^2 / sum_i w_i )
```

Weights (in `features.FEATURE_WEIGHTS`) raise the features that actually
separate playstyles - counterspells, stax, Equipment - and damp the ones most
decks carry regardless, like incidental lifegain.

Distances become affinities through a softmax with a small temperature, which
is what makes the classification *partial*: the output is a distribution over
archetypes rather than a single label. Two derived numbers matter:

- **fit** - how close the best match is in absolute terms, `1 - d/0.20`.
  A low fit on every archetype means the deck has no recognisable plan.
- **focus** - one minus the normalised entropy of the affinity distribution.
  Low focus means the deck is split across plans that may be competing for
  the same slots.

### Archetype profiles

Thirteen hand-authored profiles: Go-Wide Aggro, Voltron, Midrange Value,
Control, Combo, Stax/Prison, Big Mana, Lands Matter, Aristocrats, Reanimator,
Spellslinger, +1/+1 Counters and Typal.

They are **expert priors, not fitted parameters**. Each one states only the
features it takes a position on; everything else falls back to `BASELINE`, a
typical mid-power deck. That distinction matters downstream: the recommender
only argues about features an archetype explicitly declares, so a Lands Matter
deck is never told to add Equipment just because the baseline has some.

The numbers were anchored against the measured exemplar decks in `decks/`, and
each exemplar classifies as its intended archetype (enforced by the test
suite). To replace them with your own data:

```bash
python3 -m mtgcoach fit my_aristocrats decks/a.txt decks/b.txt --out profiles.json
python3 -m mtgcoach analyze decks/mine.txt --profiles profiles.json
```

### Recommendations

Two independent passes, merged and sorted by priority:

**Fundamentals** use absolute targets and ignore archetype: land count against
a curve-aware target (adjusted for cheap ramp, MDFC land backs, and the
archetype's own appetite for lands), total mana sources, coloured sources
against coloured pips, card draw, interaction, board wipes, curve, and format
legality (100 cards, singleton, colour identity, commander eligibility).

Curve advice reads the commander first. A commander that discounts or caps
casting costs, casts things for free, or puts permanents onto the battlefield
directly lets the deck support a genuinely higher curve, so the land target and
the "lower your curve" threshold both work from an **effective** average mana
value that subtracts that allowance. A deck led by a commander granting
`evoke {4}` to everything is not playing the same curve as its raw average
suggests.

**Direction** compares the deck against a blend of its nearest archetypes - or
against `--target` if you name a goal. The largest weighted gaps become advice,
converted into "roughly N cards worth" so it is actionable.

The blend is gated on absolute quality, not rank. Every deck has a
second-nearest archetype, and for a focused deck that runner-up is usually
junk: a second archetype joins only if it holds at least 15% affinity *and*
40% fit. A deck that is squarely one thing is measured against that one thing;
a genuine hybrid keeps both halves.

Guards that keep this from manufacturing work on decks that are already good:

- the noise floor scales with how well the deck already matches - a deck
  sitting 0.05 from its archetype is not told about 0.03 deviations;
- "you have too much X" needs a larger gap than "you need more X", and is
  suppressed entirely for the signature features of the deck's own top
  matches, and for **anything the commander itself does** - over-investment is
  usually the deck's identity;
- ramp, draw, removal and protection are never trimmed.

### What to cut

A Commander deck is exactly 100 cards, so every "add four of these" is also
"cut four of those". Advice that only ever adds is advice the player cannot
act on. The report totals the slots its recommendations want - the **swap
budget** - and then answers where they come from.

Role-level trims cover part of it. The rest is a ranked list of cards in three
tiers, because "cut this" means three quite different things:

1. **Dead weight** - fills no functional role at all. The vanilla 6/6 with no
   abilities. Always the first cut.
2. **Off plan** - does something, but nothing the deck is trying to do.
3. **Redundant** - on plan, but the deck already has more of that effect than
   the plan calls for. Ranked most expensive first, since the fifth board wipe
   should be the priciest one, not the cheapest.

Within every tier the most expensive card goes first. Three rules keep this
from producing absurd advice:

- **Universally useful cards are protected.** Ramp, draw, removal, tutors and
  protection earn their slot in any deck. Without this a synergy score
  cheerfully recommends cutting Sol Ring for being off-theme. They become
  cuttable only once the deck is past what *any* deck wants - not merely past
  what one archetype prefers, or a five-colour deck gets told to cut Cultivate
  - and then they appear as *redundant*, never as off-plan.
- **Cost is never held against a card that serves the plan.** An expensive
  on-plan card is a payoff, not a cut: Kindred Summons costs seven mana and
  does exactly one thing, and that thing wins a typal game.
- **Shape credit.** A Big Mana deck declares a big top end, but
  `top_end_share` is not a role any card can carry, so on role matching alone
  the finisher the deck exists to cast looks like it contributes nothing.
  Cards get credit for fitting the shape their plan asks for. Being expensive
  or being a creature is not itself a contribution, though - only being the
  right *tribe* can rescue a card that does nothing else.

This is the one place the tool names specific cards, and only ever cards the
player already owns - "what should I cut" has no useful role-level answer. The
list never runs longer than the number of slots the advice actually needs.

## Limitations

- Archetype profiles are priors from one author's judgement. They are anchored
  to four exemplar decks, not fitted to a corpus of tournament data.
- Scryfall Tagger coverage is community-driven and uneven, and the oracle-text
  backstop is regex, so individual cards are occasionally miscategorised. The
  report says how many cards had no tag data at all.
- Role density is a count of cards, not a measure of quality or efficiency:
  ten bad draw spells and ten good ones look identical here. The cut list
  inherits this - it ranks how well a card connects to the plan, not how
  strong the card is.
- The tool sees a decklist, not a play pattern. It cannot see that two cards
  combo, that the mana base's untapped ratio is wrong, or that the deck is
  simply too strong or too weak for its table.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

60 tests: parsing, split-card identifiers, partner pairing, role assignment,
creature-type concentration, commander weighting and curve allowance, feature
maths, legality checks, distance and mixture properties, blend gating,
cut-candidate tiers, advice guards, plus an end-to-end check that each sample
deck classifies as intended (skipped if the Scryfall cache is empty).

## Layout

```
mtgcoach/
  scryfall.py    Scryfall access, disk cache, oracle-tag hierarchy flattening
  decklist.py    decklist parsing and commander detection
  roles.py       the functional role vocabulary and its matching rules
  features.py    descriptive statistics, feature vector, legality checks
  archetypes.py  the thirteen reference profiles
  classify.py    distance, softmax mixture, focus, blended target
  advice.py      fundamentals and direction recommendation passes
  report.py      terminal and JSON rendering
  cli.py         argument parsing and command dispatch
decks/           seven exemplar decks plus one deliberately rough beginner deck
tests/           unit and end-to-end tests
```

Cached Scryfall data lives in `.cache/mtgcoach/` (override with
`MTGCOACH_CACHE`). The oracle tag file is about 6 MB and is re-downloaded
every 14 days.
