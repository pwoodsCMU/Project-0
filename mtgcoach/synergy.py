"""Deck-relative synergy, discovered from the tag data rather than declared.

The role vocabulary in :mod:`mtgcoach.roles` is a fixed list of about thirty
things a card can do. That is a good basis for comparing decks to each other,
but it can only ever see themes somebody thought to name in advance. A deck
built around something outside the list is invisible to it: Unbound Flourishing
carries ``x cost matters`` and nothing else the vocabulary knows, so in a deck
that is half {X} spells it reads as a card that does nothing at all.

This module takes the opposite approach and lets the deck describe itself. For
every Scryfall tag it compares how concentrated that tag is *in this deck*
against how common it is across all tagged cards:

    lift(tag) = share of this deck carrying it / share of all cards carrying it

A tag with high lift is, by definition, something this deck is doing on
purpose. ``x cost matters`` appears in roughly one card in a thousand overall
and in eight of the Quandrix deck's sixty spells - a lift near 200 - so the
deck's central theme is discovered without anybody having predicted it.

A card's synergy score is then how much of the deck's characteristic tag mass
it carries, which gives a defensible answer to "is this card doing something
for *this* deck" that does not depend on the archetype list at all.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Set

# A theme has to show up in at least this many cards to be a theme rather than
# a coincidence, and be this much more concentrated here than in Magic at large.
MIN_THEME_CARDS = 3
MIN_THEME_LIFT = 2.0
MAX_THEMES = 12

# Below this share of the deck's strongest theme, a card is not carrying the
# plan in any meaningful way.
SYNERGY_THRESHOLD = 0.20


class Theme(object):
    __slots__ = ("label", "cards", "lift", "weight")

    def __init__(self, label: str, cards: int, lift: float, weight: float = 0.0):
        self.label = label
        self.cards = cards          # copies in the deck carrying this tag
        self.lift = lift            # concentration here vs. across all cards
        self.weight = weight        # 0..1, relative to the deck's top theme

    def as_dict(self) -> Dict[str, object]:
        return {"tag": self.label, "cards": self.cards,
                "lift": round(self.lift, 1), "weight": round(self.weight, 3)}

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return "Theme(%r, %d, %.1fx)" % (self.label, self.cards, self.lift)


def tag_frequencies(tag_index: Dict[str, List[str]]) -> Dict[str, int]:
    """How many distinct cards in all of Magic carry each tag."""
    counts: Dict[str, int] = {}
    for labels in tag_index.values():
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
    return counts


def deck_themes(analysis, tag_index: Dict[str, List[str]],
                cosmetic: Optional[Set[str]] = None,
                frequencies: Optional[Dict[str, int]] = None,
                limit: int = MAX_THEMES) -> List[Theme]:
    """The tags this deck is unusually concentrated in, strongest first."""
    cosmetic = cosmetic or set()
    frequencies = frequencies if frequencies is not None else tag_frequencies(tag_index)
    universe = float(len(tag_index)) or 1.0

    spells = [e for e in analysis.entries if not e.is_land]
    total = float(sum(e.quantity for e in spells)) or 1.0

    counts: Dict[str, int] = {}
    for entry in spells:
        for label in set(entry.tags):
            counts[label] = counts.get(label, 0) + entry.quantity

    themes: List[Theme] = []
    for label, count in counts.items():
        if count < MIN_THEME_CARDS or label in cosmetic:
            continue
        global_share = frequencies.get(label, 0) / universe
        if global_share <= 0:
            continue
        lift = (count / total) / global_share
        if lift < MIN_THEME_LIFT:
            continue
        # Weight trades off how much of the deck carries the tag against how
        # distinctive it is; neither alone is a theme.
        themes.append(Theme(label, count, lift, count * math.sqrt(lift)))

    themes.sort(key=lambda t: -t.weight)
    themes = themes[:limit]
    if themes:
        top = themes[0].weight or 1.0
        for theme in themes:
            theme.weight = theme.weight / top
    return themes


def card_synergy(entry, themes: Sequence[Theme]) -> float:
    """0..1 - how much of the deck's themes this one card carries."""
    if not themes:
        return 0.0
    tags = set(entry.tags)
    score = sum(t.weight for t in themes if t.label in tags)
    return min(1.0, score)


def matched_themes(entry, themes: Sequence[Theme]) -> List[str]:
    tags = set(entry.tags)
    return [t.label for t in themes if t.label in tags]
