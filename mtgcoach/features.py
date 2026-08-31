"""Descriptive statistics and the archetype feature vector.

The feature vector is deliberately made of *shares*, not raw counts, so decks of
slightly different sizes stay comparable.  Role features are shares of the
nonland cards (a 65-card "spell deck" inside the 100), because that is the part
of the deck the player actually tunes.
"""

from __future__ import annotations

import re
import statistics
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .roles import AXIS_ROLES, ROLES, ROLES_BY_KEY, card_roles, is_land
from .scryfall import normalize_name

COLORS = ["W", "U", "B", "R", "G"]
COLOR_NAMES = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green"}
CURVE_BUCKETS = ["0", "1", "2", "3", "4", "5", "6", "7+"]

PIP_RE = re.compile(r"\{([^}]+)\}")

# Shape features that sit alongside the role densities in the feature vector.
SHAPE_FEATURES = [
    "creature_share",
    "instant_sorcery_share",
    "noncreature_permanent_share",
    "avg_mv_norm",
    "low_curve_share",
    "top_end_share",
    "land_share",
]

FEATURE_NAMES: List[str] = list(AXIS_ROLES) + SHAPE_FEATURES

# How much each dimension counts toward archetype distance.  Roles that
# distinguish playstyles sharply (counterspells, stax, tokens) are weighted up;
# roles most decks carry regardless of style (lifegain, protection) are damped.
FEATURE_WEIGHTS: Dict[str, float] = {
    "ramp": 1.0,
    "card_draw": 0.9,
    "tutor": 0.8,
    "removal_spot": 0.9,
    "removal_mass": 1.0,
    "counterspell": 1.3,
    "protection": 0.6,
    "recursion": 0.9,
    "graveyard_matters": 1.1,
    "sacrifice": 1.1,
    "tokens": 1.1,
    "combat_aggro": 1.2,
    "equipment_auras": 1.2,
    "stax": 1.4,
    "lands_matter": 1.0,
    "combo_enabler": 1.1,
    "lifegain": 0.5,
    "group_slug": 0.7,
    "typal": 0.7,
    "creature_share": 1.2,
    "instant_sorcery_share": 1.0,
    "noncreature_permanent_share": 0.6,
    "avg_mv_norm": 0.9,
    "low_curve_share": 0.7,
    "top_end_share": 0.7,
    "land_share": 0.4,
}

# Cards exempt from the singleton rule name themselves in their rules text.
ANY_NUMBER_RE = re.compile(r"a deck can have any number of cards named",
                           re.IGNORECASE)
BASIC_LAND_RE = re.compile(r"^\s*(Basic )?(Snow )?Land\b", re.IGNORECASE)


class CardEntry(object):
    """One decklist line, resolved against Scryfall."""

    __slots__ = ("name", "quantity", "card", "tags", "roles", "is_commander")

    def __init__(self, name: str, quantity: int, card: dict,
                 tags: Sequence[str], roles: Set[str], is_commander: bool):
        self.name = name
        self.quantity = quantity
        self.card = card
        self.tags = list(tags)
        self.roles = roles
        self.is_commander = is_commander

    @property
    def mana_value(self) -> float:
        return float(self.card.get("mana_value", 0.0))

    @property
    def type_line(self) -> str:
        return self.card.get("type_line", "")

    @property
    def is_land(self) -> bool:
        return is_land(self.card)


class DeckAnalysis(object):
    def __init__(self):
        self.entries: List[CardEntry] = []
        self.commanders: List[CardEntry] = []
        self.unresolved: List[str] = []
        self.warnings: List[str] = []
        self.total = 0
        self.land_count = 0
        self.nonland_count = 0
        self.curve: Dict[str, int] = {b: 0 for b in CURVE_BUCKETS}
        self.avg_mv = 0.0
        self.median_mv = 0.0
        self.type_counts: Dict[str, int] = {}
        self.pips: Dict[str, int] = {c: 0 for c in COLORS}
        self.color_sources: Dict[str, int] = {c: 0 for c in COLORS}
        self.color_identity: List[str] = []
        self.commander_identity: List[str] = []
        self.role_counts: Dict[str, int] = {r.key: 0 for r in ROLES}
        self.role_cards: Dict[str, List[str]] = {r.key: [] for r in ROLES}
        self.vector: Dict[str, float] = {}
        self.legality: List[str] = []
        self.mdfc_land_backs = 0
        self.untagged = 0

    # -- convenience ------------------------------------------------------- #
    def role_share(self, key: str) -> float:
        if not self.nonland_count:
            return 0.0
        return self.role_counts.get(key, 0) / float(self.nonland_count)

    def mana_sources(self) -> int:
        """Lands plus ramp - the practical count for "can I cast my stuff"."""
        return self.land_count + self.role_counts.get("ramp", 0)


def _type_bucket(type_line: str) -> str:
    front = type_line.split(" // ")[0].lower()
    for label, needle in [
        ("Land", "land"), ("Creature", "creature"), ("Planeswalker", "planeswalker"),
        ("Battle", "battle"), ("Instant", "instant"), ("Sorcery", "sorcery"),
        ("Artifact", "artifact"), ("Enchantment", "enchantment"),
    ]:
        if needle in front:
            return label
    return "Other"


def _pips(mana_cost: str) -> Dict[str, int]:
    out = {c: 0 for c in COLORS}
    for symbol in PIP_RE.findall(mana_cost or ""):
        for color in COLORS:
            if color in symbol.upper():
                out[color] += 1
    return out


def analyze(parsed_deck, cards: Dict[str, dict], tag_index: Dict[str, List[str]]) -> DeckAnalysis:
    """Build a :class:`DeckAnalysis` from a parsed decklist and Scryfall data."""
    an = DeckAnalysis()
    an.warnings.extend(parsed_deck.warnings)

    mana_values: List[float] = []

    for name, quantity in parsed_deck.quantities().items():
        card = cards.get(normalize_name(name))
        if card is None:
            an.unresolved.append(name)
            continue
        tags = tag_index.get(card.get("oracle_id") or "", [])
        if not tags:
            an.untagged += quantity
        roles = card_roles(card, tags)
        is_cmdr = any(e.name == name and e.is_commander for e in parsed_deck.entries)
        entry = CardEntry(name, quantity, card, tags, roles, is_cmdr)
        an.entries.append(entry)
        if is_cmdr:
            an.commanders.append(entry)

        an.total += quantity
        bucket = _type_bucket(entry.type_line)
        an.type_counts[bucket] = an.type_counts.get(bucket, 0) + quantity

        if entry.is_land:
            an.land_count += quantity
            for color in card.get("produced_mana") or []:
                if color in an.color_sources:
                    an.color_sources[color] += quantity
        else:
            an.nonland_count += quantity
            mv = entry.mana_value
            mana_values.extend([mv] * quantity)
            key = "7+" if mv >= 7 else str(int(mv))
            an.curve[key] = an.curve.get(key, 0) + quantity
            for color, count in _pips(card.get("mana_cost", "")).items():
                an.pips[color] += count * quantity
            for color in card.get("produced_mana") or []:
                if color in an.color_sources:
                    an.color_sources[color] += quantity
            if " // " in entry.type_line and "land" in entry.type_line.split(" // ")[1].lower():
                an.mdfc_land_backs += quantity

        for role in roles:
            an.role_counts[role] = an.role_counts.get(role, 0) + quantity
            an.role_cards.setdefault(role, []).append(name)

    if mana_values:
        an.avg_mv = sum(mana_values) / len(mana_values)
        an.median_mv = statistics.median(mana_values)

    identity: Set[str] = set()
    for entry in an.entries:
        identity.update(entry.card.get("color_identity") or [])
    an.color_identity = [c for c in COLORS if c in identity]
    cmdr_identity: Set[str] = set()
    for entry in an.commanders:
        cmdr_identity.update(entry.card.get("color_identity") or [])
    an.commander_identity = [c for c in COLORS if c in cmdr_identity]

    an.vector = build_vector(an)
    an.legality = check_legality(an, parsed_deck)
    return an


def build_vector(an: DeckAnalysis) -> Dict[str, float]:
    nonland = float(an.nonland_count) or 1.0
    total = float(an.total) or 1.0
    vec: Dict[str, float] = {}
    for key in AXIS_ROLES:
        vec[key] = an.role_counts.get(key, 0) / nonland

    vec["creature_share"] = an.type_counts.get("Creature", 0) / nonland
    vec["instant_sorcery_share"] = (
        an.type_counts.get("Instant", 0) + an.type_counts.get("Sorcery", 0)) / nonland
    vec["noncreature_permanent_share"] = (
        an.type_counts.get("Artifact", 0) + an.type_counts.get("Enchantment", 0)
        + an.type_counts.get("Planeswalker", 0)) / nonland
    vec["avg_mv_norm"] = min(an.avg_mv / 6.0, 1.0)
    low = sum(an.curve.get(b, 0) for b in ("0", "1", "2"))
    top = sum(an.curve.get(b, 0) for b in ("6", "7+"))
    vec["low_curve_share"] = low / nonland
    vec["top_end_share"] = top / nonland
    vec["land_share"] = an.land_count / total
    return vec


def check_legality(an: DeckAnalysis, parsed_deck) -> List[str]:
    """Commander-format sanity checks a newer player is most likely to trip."""
    issues: List[str] = []

    if an.total and an.total != 100:
        issues.append("deck has %d cards; Commander decks are exactly 100 "
                      "(commander included)" % an.total)

    if not an.commanders:
        issues.append("no commander identified, so colour-identity checks were skipped")
    else:
        for cmdr in an.commanders:
            tl = cmdr.type_line.lower()
            text = (cmdr.card.get("oracle_text") or "").lower()
            # Creature is the usual requirement; Vehicles/Spacecraft that turn
            # into creatures and "can be your commander" cards are legal too.
            eligible = ("creature" in tl or "vehicle" in tl or "spacecraft" in tl
                        or "background" in tl or "can be your commander" in text)
            if "legendary" not in tl and "can be your commander" not in text:
                issues.append("%s is not legendary and does not say it can be "
                              "your commander" % cmdr.name)
            elif not eligible:
                issues.append("%s is legendary but is not a creature - double "
                              "check that it can be your commander" % cmdr.name)

        allowed = set(an.commander_identity)
        offenders = []
        for entry in an.entries:
            extra = set(entry.card.get("color_identity") or []) - allowed
            if extra:
                offenders.append("%s (%s)" % (entry.name, "".join(sorted(extra))))
        if offenders:
            preview = ", ".join(offenders[:6])
            more = "" if len(offenders) <= 6 else " and %d more" % (len(offenders) - 6)
            issues.append("outside your commander's colour identity: %s%s"
                          % (preview, more))

    for entry in an.entries:
        if entry.quantity <= 1:
            continue
        text = entry.card.get("oracle_text") or ""
        if BASIC_LAND_RE.search(entry.type_line) or "basic" in entry.type_line.lower():
            continue
        if ANY_NUMBER_RE.search(text):
            continue
        issues.append("%d copies of %s breaks the singleton rule"
                      % (entry.quantity, entry.name))

    return issues
