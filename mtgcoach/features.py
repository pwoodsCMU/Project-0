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

# A creature that counts as every type (Changeling, "is every creature type")
# belongs to whatever tribe the deck is playing.
CHANGELING_RE = re.compile(r"(changeling|is every creature type)", re.IGNORECASE)

# Scryfall reports {X} as zero, so Fireball is mana value 1 and Walking
# Ballista is 0.  Nobody casts them for X=0, and treating them as one-drops
# makes a deck of mana sinks look like it has a superb early game.  Count them
# at a realistic X instead.
X_COST_RE = re.compile(r"\{X\}")
X_SPELL_ALLOWANCE = 2.0

# The commander is castable in every single game, so it carries more weight in
# the deck's identity than any one of the other 99 cards.  Role densities in
# the feature vector count it this many times; the descriptive counts stay
# honest at one card.
COMMANDER_WEIGHT = 3

# Commander abilities that change what a "fair" curve looks like.  A commander
# that caps or discounts casting costs, or puts things into play directly,
# lets the deck run cards it could never hard-cast on time.
# (pattern, description, curve allowance in mana value)
COMMANDER_COST_PATTERNS = [
    (r"cost(?:s)? \{?\d+\}? less", "makes your spells cheaper", 0.6),
    (r"gains? (?:evoke|affinity|convoke|improvise|delve|emerge)",
     "gives your spells an alternative cost", 0.6),
    (r"without paying (?:its|their) mana cost",
     "casts spells without paying for them", 0.6),
    (r"you may cast .{0,60}(?:from your graveyard|from exile|from the top)",
     "casts cards from outside your hand", 0.3),
    (r"put(?:s)? .{0,60}onto the battlefield",
     "puts permanents onto the battlefield directly", 0.3),
]

# Shape features that sit alongside the role densities in the feature vector.
SHAPE_FEATURES = [
    "typal_concentration",
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
    "removal_spot": 1.0,
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
    "counters_matter": 1.2,
    "typal_concentration": 1.3,
    "creature_share": 1.2,
    "instant_sorcery_share": 1.0,
    "noncreature_permanent_share": 0.6,
    "avg_mv_norm": 0.9,
    "low_curve_share": 0.5,
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
    def is_x_spell(self) -> bool:
        return bool(X_COST_RE.search(self.card.get("mana_cost") or ""))

    @property
    def effective_mana_value(self) -> float:
        """Mana value with {X} counted as something you would actually pay."""
        if self.is_x_spell:
            count = len(X_COST_RE.findall(self.card.get("mana_cost") or ""))
            return self.mana_value + X_SPELL_ALLOWANCE * count
        return self.mana_value

    @property
    def edhrec_rank(self):
        return self.card.get("edhrec_rank")

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
        self.x_spell_count = 0
        self.creature_count = 0
        self.dominant_type = ""
        self.dominant_type_count = 0
        self.subtype_counts: Dict[str, int] = {}
        self.commander_notes: List[str] = []
        self.commander_curve_allowance = 0.0
        self.commander_wants_high_curve = False

    def effective_avg_mv(self) -> float:
        """Average mana value, discounted for what the commander makes cheaper."""
        return max(1.0, self.avg_mv - self.commander_curve_allowance)

    # -- convenience ------------------------------------------------------- #
    def role_share(self, key: str) -> float:
        if not self.nonland_count:
            return 0.0
        return self.role_counts.get(key, 0) / float(self.nonland_count)

    def mana_sources(self) -> int:
        """Lands plus ramp - the practical count for "can I cast my stuff"."""
        return self.land_count + self.role_counts.get("ramp", 0)


def creature_subtypes(type_line: str) -> List[str]:
    """Subtypes of the front face, e.g. 'Legendary Creature - Elemental Shaman'."""
    front = type_line.split(" // ")[0]
    if "creature" not in front.lower():
        return []
    for dash in ("\u2014", "-"):
        if dash in front:
            return front.split(dash, 1)[1].split()
    return []


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


def _plain_partner(text: str) -> bool:
    return bool(re.search(r"(^|\n)partner\b", text, re.IGNORECASE)
                and "partner with" not in text.lower())


def infer_partner(parsed_deck, cards: Dict[str, dict]) -> Optional[str]:
    """Promote a missed partner commander out of the maindeck.

    Exports do not agree on where the second commander goes - one real list
    puts the two partners in separate blank-line-separated blocks, so only the
    first is detected and fifty cards then read as off-colour.  When the lone
    detected commander can legally pair with exactly one legendary card in the
    deck, and promoting it repairs colour-identity violations, promote it.
    """
    commanders = [e for e in parsed_deck.entries if e.is_commander]
    if len(commanders) != 1:
        return None
    lead = cards.get(normalize_name(commanders[0].name))
    if lead is None:
        return None
    lead_text = lead.get("oracle_text") or ""
    named = re.search(r"partner with ([^\n(]+)", lead_text, re.IGNORECASE)
    if not named and not _plain_partner(lead_text):
        return None

    candidates = []
    for entry in parsed_deck.entries:
        if entry.is_commander:
            continue
        card = cards.get(normalize_name(entry.name))
        if card is None or "legendary" not in (card.get("type_line") or "").lower():
            continue
        text = card.get("oracle_text") or ""
        if named:
            wanted = named.group(1).strip().rstrip(".").lower()
            if entry.name.lower().startswith(wanted):
                return _promote(parsed_deck, entry)
        elif _plain_partner(text):
            candidates.append((entry, card))

    if len(candidates) != 1:
        return None
    entry, card = candidates[0]

    # Only promote when it actually explains the deck: the partner's colours
    # have to be carrying cards the lone commander cannot.
    allowed = set(lead.get("color_identity") or [])
    widened = allowed | set(card.get("color_identity") or [])
    def violations(identity):
        return sum(1 for e in parsed_deck.entries
                   if (set((cards.get(normalize_name(e.name)) or {})
                           .get("color_identity") or []) - identity))
    if violations(widened) < violations(allowed):
        return _promote(parsed_deck, entry)
    return None


def _promote(parsed_deck, entry) -> str:
    for other in parsed_deck.entries:
        if other.name == entry.name:
            other.is_commander = True
    return entry.name


def analyze(parsed_deck, cards: Dict[str, dict], tag_index: Dict[str, List[str]]) -> DeckAnalysis:
    """Build a :class:`DeckAnalysis` from a parsed decklist and Scryfall data."""
    an = DeckAnalysis()
    promoted = infer_partner(parsed_deck, cards)
    an.warnings.extend(parsed_deck.warnings)
    if promoted:
        an.warnings.append(
            "%s was read as your second commander - it partners with %s and "
            "your deck's colours need it. Mark it in a Commander section if "
            "that is wrong." % (promoted,
                                next(e.name for e in parsed_deck.entries
                                     if e.is_commander and e.name != promoted)))

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
            mv = entry.effective_mana_value
            if entry.is_x_spell:
                an.x_spell_count += quantity
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

    _dominant_creature_type(an)
    _commander_effects(an)
    an.vector = build_vector(an)
    an.legality = check_legality(an, parsed_deck)
    return an


def _commander_effects(an: DeckAnalysis) -> None:
    """Read the commander's text for things that change how the deck is judged.

    The commander is castable every game, so an ability that discounts or
    cheats on casting costs applies to the whole deck - which means the curve
    it can support is genuinely higher than the raw average suggests.
    """
    allowance = 0.0
    seen = set()
    for cmdr in an.commanders:
        # A commander that pays you for expensive permanents means the high
        # curve is the plan, not a mistake.
        if ("high mana value matters" in cmdr.tags
                or re.search(r"mana value \d+ or (greater|more)",
                             cmdr.card.get("oracle_text") or "", re.IGNORECASE)):
            an.commander_wants_high_curve = True
            an.commander_notes.append("%s rewards expensive permanents"
                                      % cmdr.name)
            seen.add((cmdr.name, "rewards expensive permanents"))
        text = cmdr.card.get("oracle_text") or ""
        found = []
        for pattern, description, weight in COMMANDER_COST_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                found.append(description)
                allowance = max(allowance, weight)
        if "ramp" in cmdr.roles and not found:
            found.append("accelerates your mana")
            allowance = max(allowance, 0.4)
        # Notes carry the commander's name: with partners, two different cards
        # are doing two different things and must not be merged into one
        # sentence about "your commander".
        for description in found:
            key = (cmdr.name, description)
            if key not in seen:
                seen.add(key)
                an.commander_notes.append("%s %s" % (cmdr.name, description))
    an.commander_curve_allowance = min(0.8, allowance)


def _dominant_creature_type(an: DeckAnalysis) -> None:
    """Find the tribe the deck is actually playing, and how concentrated it is.

    Changelings count toward whatever the tribe turns out to be.  When the
    commander is itself a creature, its own types get first refusal on ties -
    an Elemental commander means the deck is asking to be measured on
    Elementals, even if some other type happens to be one card ahead.
    """
    counts: Dict[str, int] = {}
    creatures = 0
    changelings = 0
    for entry in an.entries:
        subtypes = creature_subtypes(entry.type_line)
        if not subtypes:
            continue
        creatures += entry.quantity
        if CHANGELING_RE.search(entry.card.get("oracle_text") or ""):
            changelings += entry.quantity
            continue
        for subtype in subtypes:
            counts[subtype] = counts.get(subtype, 0) + entry.quantity

    an.creature_count = creatures
    an.subtype_counts = counts
    if not creatures or not counts:
        return

    best = max(counts.values())
    commander_types = set()
    for cmdr in an.commanders:
        commander_types.update(creature_subtypes(cmdr.type_line))
        # Scryfall tags a typal payoff with the tribe it cares about, which
        # catches commanders that are not themselves of the type they pump.
        for tag in cmdr.tags:
            if tag.startswith("typal-") and tag not in (
                    "typal-creature", "typal-choose", "typal-share"):
                commander_types.add(tag[len("typal-"):].title())

    dominant = max(counts, key=lambda t: counts[t])
    for subtype in commander_types:
        if counts.get(subtype, 0) >= best * 0.8:
            dominant = subtype
            break

    an.dominant_type = dominant
    an.dominant_type_count = counts[dominant] + changelings


def build_vector(an: DeckAnalysis) -> Dict[str, float]:
    nonland = float(an.nonland_count) or 1.0
    total = float(an.total) or 1.0

    # Role densities weight the commander up: it is available in every game,
    # so it says more about the deck than an average single copy does.
    extra = COMMANDER_WEIGHT - 1
    commander_spells = sum(e.quantity for e in an.commanders if not e.is_land)
    role_denominator = nonland + extra * commander_spells

    vec: Dict[str, float] = {}
    for key in AXIS_ROLES:
        count = an.role_counts.get(key, 0)
        count += extra * sum(e.quantity for e in an.commanders
                             if key in e.roles and not e.is_land)
        vec[key] = count / role_denominator

    vec["typal_concentration"] = (
        an.dominant_type_count / float(an.creature_count)
        if an.creature_count else 0.0)

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


def _partner_issues(an: DeckAnalysis) -> List[str]:
    """Two commanders are only legal if the cards actually say they may pair."""
    if len(an.commanders) < 2:
        return []
    if len(an.commanders) > 2:
        return ["%d commanders listed; Commander allows at most two, and only "
                "when the cards pair (Partner, Friends forever, a Background, "
                "or a Doctor's companion)" % len(an.commanders)]

    first, second = an.commanders
    texts = [(c.card.get("oracle_text") or "").lower() for c in an.commanders]
    types = [c.type_line.lower() for c in an.commanders]
    names = [c.name.lower() for c in an.commanders]

    def has_plain_partner(index: int) -> bool:
        text = texts[index]
        return bool(re.search(r"(^|\n)partner\b", text)
                    and "partner with" not in text)

    def partners_with_other(index: int) -> bool:
        other = names[1 - index]
        match = re.search(r"partner with ([^\n(]+)", texts[index])
        if not match:
            return False
        # "Partner with Kydele" also matches the full "Kydele, Chosen of Kruphix".
        named = match.group(1).strip().rstrip(".").lower()
        return named in other or other.startswith(named)

    legal = (
        (has_plain_partner(0) and has_plain_partner(1))
        or partners_with_other(0) or partners_with_other(1)
        or all("friends forever" in text for text in texts)
        or any("choose a background" in texts[i] and "background" in types[1 - i]
               for i in (0, 1))
        or any("doctor's companion" in texts[i] and "time lord doctor" in types[1 - i]
               for i in (0, 1))
    )
    if legal:
        return []
    return ["%s and %s cannot be paired as commanders - two commanders need "
            "Partner, \"Partner with\", Friends forever, a Background, or a "
            "Doctor's companion" % (first.name, second.name)]


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

        issues.extend(_partner_issues(an))

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
