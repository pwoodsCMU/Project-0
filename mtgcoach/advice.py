"""Turning measurements into advice.

Two independent passes produce recommendations:

**Fundamentals** - archetype-independent things that sink beginner decks
regardless of what they are trying to do: mana, card draw, interaction, curve.
These use absolute targets, adjusted for the deck's own curve.

**Direction** - the deck is compared against a blend of the archetypes it is
already closest to, and the largest weighted gaps become "lean further into
this" / "you are over-invested here" advice.  The point is not to force a deck
into a template but to tell the player what their own deck is asking for.

Advice is deliberately at the level of *roles*, never specific cards: "add
about four pieces of spot removal" rather than "play Swords to Plowshares".
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from . import synergy as synergy_mod
from .archetypes import Archetype
from .classify import Classification, blend_matches, blended_target
from .features import (COLOR_NAMES, DeckAnalysis, FEATURE_NAMES,
                       FEATURE_WEIGHTS)
from .roles import ROLES_BY_KEY

HIGH, MEDIUM, LOW = "high", "medium", "low"
_PRIORITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

# Roles almost no Commander deck regrets having more of.
NEVER_TRIM = frozenset(["card_draw", "removal_spot", "protection"])

# A universally useful role only counts as over-supplied once the deck is past
# the number any Commander deck wants, not merely past what one archetype
# prefers.  Without these floors a five-colour deck gets told to cut Cultivate.
UNIVERSAL_FLOORS = {
    "ramp": 16, "card_draw": 14, "removal_spot": 12, "removal_mass": 6,
    "counterspell": 12, "protection": 12, "tutor": 10,
}

# EDHREC rank is how widely a card is played in Commander. The top few hundred
# are format staples, and "you have one board wipe too many" is a bad reason to
# cut the best board wipe ever printed. Staples can still be trimmed at the
# role level; they are just never the card the tool points at.
STAPLE_RANK = 600

# Contribution score below which a card is worth putting on the table as a
# possible cut. Calibrated so a tuned deck yields a short list and a precon
# yields a long one.
CUT_SCORE_CEILING = 2.0

# A card carrying the deck's top theme stays core down to this rank; past it,
# being on theme no longer excuses being a card almost nobody plays.
THEME_CORE_RANK = 3500

# What a commander caring about something implies about the deck's shape.
# These are not roles a card carries, so without this an artifact commander's
# deck gets told to trim the artifacts it is built on.
# Shape features that pull against each other. If the commander wants one, the
# deck should not be advised toward its opposite.
SHAPE_CONFLICTS = {
    "noncreature_permanent_share": ["instant_sorcery_share"],
    "creature_share": ["instant_sorcery_share"],
    "instant_sorcery_share": ["creature_share", "noncreature_permanent_share"],
    "top_end_share": ["low_curve_share"],
}

COMMANDER_SHAPE_IMPLICATIONS = {
    "artifact_matters": ["noncreature_permanent_share"],
    "enchantment_matters": ["noncreature_permanent_share"],
    "equipment_auras": ["noncreature_permanent_share"],
    "typal": ["typal_concentration", "creature_share"],
    "counters_matter": ["creature_share"],
    "tokens": ["creature_share"],
    "sacrifice": ["creature_share"],
    "combat_aggro": ["creature_share"],
}


def commander_wants(analysis: DeckAnalysis) -> set:
    """Everything the commander itself argues for, roles and shape alike."""
    wants = set()
    for cmdr in analysis.commanders:
        wants.update(cmdr.roles)
        for role in cmdr.roles:
            wants.update(COMMANDER_SHAPE_IMPLICATIONS.get(role, ()))
    return wants

# Roles that earn a card its slot in any deck, whatever the plan is.  Without
# this, a synergy score cheerfully recommends cutting Sol Ring for being
# off-theme.
UNIVERSAL_ROLES = frozenset([
    "ramp", "card_draw", "tutor", "removal_spot", "removal_mass",
    "counterspell", "protection",
])


# "You have too much of X" needs a much bigger gap than "you need more X"
# before it is worth saying - over-investment is usually the deck's identity.
TRIM_GAP = 0.08

# Absolute floor on a gap worth mentioning, in share-of-spells terms.
MIN_GAP = 0.05


def _article(word: str) -> str:
    """"a" or "an", for archetype names dropped into a sentence."""
    return "an" if word[:1].upper() in "AEIOU" else "a"


def _join(items: Sequence[str]) -> str:
    """Join a list into readable prose: 'a', 'a and b', 'a, b and c'."""
    items = list(items)
    if len(items) <= 1:
        return items[0] if items else ""
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


def color_sources_needed(pips: int) -> int:
    """Rule-of-thumb source count for a given number of coloured pips."""
    return int(min(24, round(9 + 0.35 * pips)))


class Recommendation(object):
    __slots__ = ("priority", "kind", "title", "detail", "evidence", "cards")

    def __init__(self, priority: str, kind: str, title: str, detail: str,
                 evidence: str = "", cards: int = 0):
        self.priority = priority
        self.kind = kind              # "mana" | "fundamentals" | "direction" | "focus"
        self.title = title
        self.detail = detail
        self.evidence = evidence
        # Signed slot count: positive means "find room for this many cards",
        # negative means "this many slots come free".
        self.cards = cards

    def as_dict(self) -> Dict[str, object]:
        return {"priority": self.priority, "kind": self.kind, "title": self.title,
                "detail": self.detail, "evidence": self.evidence,
                "cards": self.cards}


class CutCandidate(object):
    """A card the deck can most afford to lose, and why."""

    __slots__ = ("name", "quantity", "mana_value", "reason", "roles", "tier",
                 "rank")

    def __init__(self, name: str, quantity: int, mana_value: float,
                 reason: str, roles: Sequence[str], tier: str,
                 rank: Optional[int] = None):
        self.name = name
        self.quantity = quantity
        self.mana_value = mana_value
        self.reason = reason
        self.roles = list(roles)
        self.tier = tier          # "dead" | "off_plan" | "redundant"
        self.rank = rank          # EDHREC rank; lower is more widely played

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "mana_value": self.mana_value,
                "reason": self.reason, "roles": self.roles, "tier": self.tier,
                "edhrec_rank": self.rank}


# What to say when a deck is short of / heavy on a given feature.
FEATURE_ADVICE: Dict[str, Tuple[str, str]] = {
    "ramp": (
        "Add more ramp",
        "Cut some ramp"),
    "card_draw": (
        "Add more card draw",
        "You can trade some card draw for action"),
    "tutor": (
        "Add a few tutors",
        "Trim tutors"),
    "removal_spot": (
        "Add more spot removal",
        "Trim some spot removal"),
    "removal_mass": (
        "Add a board wipe or two",
        "Cut a board wipe or two"),
    "counterspell": (
        "Add more control - counterspells and stack interaction",
        "Cut some counterspells"),
    "protection": (
        "Add protection for your key pieces",
        "Trim protection spells"),
    "recursion": (
        "Add recursion",
        "Trim recursion"),
    "graveyard_matters": (
        "Lean harder on your graveyard",
        "Trim graveyard filler"),
    "sacrifice": (
        "Add sacrifice outlets and death payoffs",
        "Trim sacrifice pieces"),
    "tokens": (
        "Add more token generation",
        "Trim token makers"),
    "combat_aggro": (
        "Add more combat pressure",
        "Trim combat filler"),
    "equipment_auras": (
        "Add more Equipment and Auras",
        "Trim Equipment and Auras"),
    "stax": (
        "Add tax and lockdown effects",
        "Cut back on stax effects"),
    "lands_matter": (
        "Add more lands-matter payoffs",
        "Trim lands-matter cards"),
    "combo_enabler": (
        "Add combo pieces and engines",
        "Trim narrow combo pieces"),
    "lifegain": (
        "Add lifegain",
        "Trim incidental lifegain"),
    "group_slug": (
        "Add non-combat damage",
        "Trim group-slug effects"),
    "counters_matter": (
        "Add more +1/+1 counter synergy",
        "Trim counter payoffs"),
    "typal_concentration": (
        "Commit harder to one creature type",
        "Your creatures do not need to share a type"),
    "typal": (
        "Add more payoffs for your creature type",
        "Trim typal cards that are only playable for their type"),
    "creature_share": (
        "Play more creatures",
        "Play fewer creatures"),
    "instant_sorcery_share": (
        "Play more instants and sorceries",
        "Play fewer instants and sorceries"),
    "noncreature_permanent_share": (
        "Add more noncreature permanents",
        "Trim noncreature permanents"),
    "avg_mv_norm": (
        "Your curve can afford to go bigger",
        "Lower your curve"),
    "low_curve_share": (
        "Add more early plays",
        "You have room for fewer one- and two-drops"),
    "top_end_share": (
        "Add a real top end",
        "Cut some expensive cards"),
    "land_share": (
        "Run more lands",
        "Run fewer lands"),
}

# Extra sentences that explain *why*, keyed by feature.
FEATURE_WHY: Dict[str, str] = {
    "ramp": "Ramp is how you get ahead of a 40-life format; most decks want "
            "8-12 pieces that cost 3 or less.",
    "card_draw": "Card draw is the single most common gap in newer decks - "
                 "without it you run out of gas around turn six.",
    "tutor": "Tutors turn a pile of cards into a plan by finding the piece you "
             "actually need.",
    "removal_spot": "Every table has one permanent that must die. Spot removal "
                    "is your answer to it.",
    "removal_mass": "Board wipes are your reset button when someone gets ahead "
                    "of you on board.",
    "counterspell": "Counterspells trade a card for the scariest thing anyone "
                    "casts, which is how slower decks survive.",
    "protection": "If your deck relies on one or two permanents, protection is "
                  "the difference between a plan and a wish.",
    "recursion": "Recursion turns your best cards into repeatable ones and "
                 "blunts removal.",
    "graveyard_matters": "Your graveyard is a second hand once you build "
                         "around it.",
    "sacrifice": "Sacrifice outlets convert creatures into value and dodge "
                 "exile and theft.",
    "tokens": "Tokens are the fuel: they make anthems, sacrifice outlets and "
              "go-wide attacks work.",
    "combat_aggro": "Something has to actually end the game - evasion, "
                    "anthems, and attack triggers are how creature decks do it.",
    "equipment_auras": "Voltron needs a critical mass of buffs so any draw "
                       "gets your threat over the line.",
    "stax": "Tax pieces buy you the time a slow plan needs - though they also "
            "make you the table's first target.",
    "lands_matter": "Land drops are free triggers if you build around them.",
    "combo_enabler": "Combo needs enough enablers that you find a line "
                     "reliably, not once in five games.",
    "lifegain": "Life is a resource that buys turns in a three-opponent game.",
    "group_slug": "Damage that ignores blockers closes games your creatures "
                  "cannot.",
    "typal": "Lords and type payoffs are what make a tribal deck stronger than "
             "the same 30 creatures in a pile.",
    "counters_matter": "Counters compound: each one makes every payoff that "
                       "counts them better, so the pieces are worth far more "
                       "together than apart.",
    "typal_concentration": "Lords and type payoffs only pay off if most of "
                           "your creatures actually share the type.",
    "creature_share": "Creatures are the cheapest way to pressure three "
                      "opponents at once.",
    "instant_sorcery_share": "Instants and sorceries let you interact on other "
                             "people's turns.",
    "noncreature_permanent_share": "Noncreature permanents survive creature "
                                   "wipes and provide steady value.",
    "avg_mv_norm": "",
    "low_curve_share": "Cheap plays let you do something on turns one through "
                       "three instead of watching.",
    "top_end_share": "A few high-impact expensive cards give the deck "
                     "something to ramp toward.",
    "land_share": "",
}


# --------------------------------------------------------------------------- #
# fundamentals
# --------------------------------------------------------------------------- #

# How far each archetype sits from the generic land count.  Control and big
# mana want to hit every land drop; aggro and Voltron would rather draw action.
ARCHETYPE_LAND_SHIFT: Dict[str, float] = {
    "control": 2.0,
    "big_mana": 2.0,
    "stax_prison": 1.0,
    "reanimator": 1.0,
    "lands_matter": 1.0,
    "midrange_value": 0.0,
    "aristocrats": 0.0,
    "typal": 0.0,
    "spellslinger": 0.0,
    "go_wide_aggro": -1.0,
    "voltron": -1.0,
    "combo": -1.0,
}


def recommended_lands(analysis: DeckAnalysis,
                      classification: Optional[Classification] = None) -> int:
    """A curve-aware land target.

    Starts from the usual 36-land rule of thumb, moves with the curve, comes
    down a little for decks with a lot of cheap ramp, and goes *up* for decks
    whose whole plan is putting lands onto the battlefield.
    """
    target = 36.0 + 2.4 * (analysis.effective_avg_mv() - 3.0)

    lands_matter = analysis.role_share("lands_matter")
    if lands_matter >= 0.20:
        # The lands are the payoff, not just the fuel.
        target += 3.0 if lands_matter >= 0.30 else 2.0
    else:
        # Only cheap ramp really substitutes for a land.
        cheap_ramp = sum(e.quantity for e in analysis.entries
                         if "ramp" in e.roles and e.mana_value <= 3)
        target -= min(3.0, max(0.0, (cheap_ramp - 8) / 4.0))

    if analysis.mdfc_land_backs:
        target -= min(2.0, analysis.mdfc_land_backs * 0.5)

    if classification is not None:
        best = classification.best
        if best.affinity >= 0.35:
            target += ARCHETYPE_LAND_SHIFT.get(best.archetype.key, 0.0)
    return int(max(33, min(42, round(target))))


def fundamentals(analysis: DeckAnalysis,
                 classification: Optional[Classification] = None) -> List[Recommendation]:
    out: List[Recommendation] = []
    nonland = analysis.nonland_count or 1
    counts = analysis.role_counts
    best_key = classification.best.archetype.key if classification else ""
    aggro_ish = best_key in ("go_wide_aggro", "voltron", "typal")

    # --- mana base ------------------------------------------------------- #
    target = recommended_lands(analysis, classification)
    delta = target - analysis.land_count
    if delta >= 3:
        out.append(Recommendation(
            HIGH, "mana", "Run about %d more lands" % delta,
            "You have %d lands with an effective average mana value of %.2f. For that "
            "curve, %d lands is the usual floor. Missing land drops is the "
            "most common reason a deck feels like it does nothing."
            % (analysis.land_count, analysis.effective_avg_mv(), target),
            "lands %d vs target %d" % (analysis.land_count, target)))
    elif delta <= -4:
        trim = min(4, -delta)
        out.append(Recommendation(
            MEDIUM, "mana", "You are running more lands than this deck needs",
            "You have %d lands for an average mana value of %.2f plus %d ramp "
            "pieces, where about %d lands would do. Turning %d of them into "
            "card draw or interaction reduces how often you flood out."
            % (analysis.land_count, analysis.avg_mv, counts.get("ramp", 0),
               target, trim),
            "lands %d vs target %d" % (analysis.land_count, target)))

    sources = analysis.mana_sources()
    if sources < target + 6:
        out.append(Recommendation(
            HIGH if sources < target + 3 else MEDIUM, "mana",
            "Add more mana sources",
            "Lands plus ramp comes to %d. Aim for roughly %d-%d total mana "
            "sources so you hit your third, fourth and fifth land drops on "
            "time." % (sources, target + 6, target + 10),
            "%d lands + %d ramp" % (analysis.land_count, counts.get("ramp", 0))))

    # --- colour consistency ---------------------------------------------- #
    for color in (analysis.commander_identity or analysis.color_identity):
        pips = analysis.pips.get(color, 0)
        srcs = analysis.color_sources.get(color, 0)
        needed = color_sources_needed(pips)
        if pips >= 8 and srcs < needed:
            out.append(Recommendation(
                MEDIUM, "mana", "Not enough %s sources" % COLOR_NAMES[color],
                "You have %d %s pips in your costs but only %d sources that "
                "produce %s - about %d is the comfortable number. "
                "Under-supported colours are why hands look castable and are "
                "not." % (pips, COLOR_NAMES[color], srcs, COLOR_NAMES[color],
                          needed),
                "%d pips / %d sources" % (pips, srcs)))

    # --- card draw -------------------------------------------------------- #
    draw = counts.get("card_draw", 0)
    if draw < 8:
        out.append(Recommendation(
            HIGH, "fundamentals", "Add more card draw",
            "Only %d cards here refill your hand. Aim for 8-12 pieces of draw, "
            "with at least a few of them repeatable engines rather than "
            "one-shot draw spells." % draw,
            "%d draw pieces (%.0f%% of spells)" % (draw, 100.0 * draw / nonland)))

    # --- interaction ------------------------------------------------------ #
    spot = counts.get("removal_spot", 0)
    mass = counts.get("removal_mass", 0)
    interaction = spot + mass + counts.get("counterspell", 0)
    if interaction < 10:
        out.append(Recommendation(
            HIGH, "fundamentals", "Add more interaction",
            "You have %d cards that answer an opponent (%d spot removal, %d "
            "mass removal). Commander decks want 10-14 across three "
            "opponents; without them you simply lose to whoever plays the "
            "strongest permanent." % (interaction, spot, mass),
            "%d answers" % interaction))
    elif spot < 6:
        out.append(Recommendation(
            MEDIUM, "fundamentals", "Add more spot removal",
            "You have %d pieces of targeted removal. Board wipes and "
            "counterspells do not answer the resolved permanent that is "
            "already killing you - aim for 6-10 that do." % spot,
            "%d spot removal" % spot))
    if mass < 2 and not aggro_ish:
        out.append(Recommendation(
            MEDIUM, "fundamentals", "Add a board wipe or two",
            "You have %d mass removal effects. Two or three give you an out "
            "when a creature deck goes wide on you." % mass,
            "%d board wipes" % mass))

    # --- curve ------------------------------------------------------------ #
    effective_mv = analysis.effective_avg_mv()
    if analysis.commander_wants_high_curve:
        pass          # the expensive cards are the point; see the note below
    elif effective_mv > 3.6:
        heavy = analysis.curve.get("6", 0) + analysis.curve.get("7+", 0)
        caveat = ""
        if analysis.commander_curve_allowance:
            caveat = (" This already allows for the fact that %s."
                      % _join(analysis.commander_notes))
        out.append(Recommendation(
            HIGH if effective_mv > 4.0 else MEDIUM, "fundamentals",
            "Lower your curve",
            "Average mana value is %.2f with %d cards at 6+. Trading a few of "
            "the expensive cards for cheaper effects that do most of the same "
            "job makes the deck far more consistent.%s"
            % (analysis.avg_mv, heavy, caveat),
            "avg MV %.2f (effective %.2f)" % (analysis.avg_mv, effective_mv)))

    # A deck with a lot of cheap ramp is doing something on turns one to three
    # even when few of its spells are cheap, so the bar comes down.
    cheap = sum(analysis.curve.get(b, 0) for b in ("0", "1", "2"))
    cheap_ramp = sum(e.quantity for e in analysis.entries
                     if "ramp" in e.roles and e.effective_mana_value <= 3)
    threshold = 0.25 - min(0.08, max(0, cheap_ramp - 8) * 0.01)
    if analysis.commander_wants_high_curve:
        threshold = 0.0
    if cheap / float(nonland) < threshold:
        out.append(Recommendation(
            MEDIUM, "fundamentals", "Add more early plays",
            "Only %d of your %d spells cost 2 or less (%.0f%%). Aim for about "
            "a quarter to a third, so your first three turns are not blank."
            % (cheap, nonland, 100.0 * cheap / nonland),
            "%d cheap spells, %d cheap ramp" % (cheap, cheap_ramp)))

    # --- data quality / legality ------------------------------------------ #
    for issue in analysis.legality:
        out.append(Recommendation(HIGH, "legality", "Deck legality", issue))
    if analysis.unresolved:
        preview = ", ".join(analysis.unresolved[:5])
        out.append(Recommendation(
            MEDIUM, "data", "Some cards could not be identified",
            "Scryfall did not recognise: %s%s. Check the spelling - these were "
            "left out of every number in this report."
            % (preview, "" if len(analysis.unresolved) <= 5 else ", ...")))

    out.sort(key=lambda r: _PRIORITY_ORDER[r.priority])
    return out


# --------------------------------------------------------------------------- #
# archetype direction
# --------------------------------------------------------------------------- #

def _cards(gap: float, nonland: int) -> int:
    return int(round(abs(gap) * nonland))


def direction(analysis: DeckAnalysis, classification: Classification,
              blend_top: int = 2, max_items: int = 6,
              target_archetype: Optional[Archetype] = None) -> List[Recommendation]:
    """Advice from the gap between the deck and where it is being steered.

    By default the destination is a blend of the archetypes the deck is
    already nearest, so the advice follows the deck's own grain.  Pass
    ``target_archetype`` to aim at a stated goal instead - that is the "I am
    trying to build Voltron, what am I missing?" mode.
    """
    nonland = analysis.nonland_count or 1
    identity: set = set()
    declared: set = set()

    # Noise floor, scaled to how well the deck already matches. A deck sitting
    # 0.05 from its archetype varies from the profile by that much on an
    # average feature, so reporting smaller deviations than that manufactures
    # work rather than finding it.
    floor = max(MIN_GAP, classification.best.distance * 0.6)

    # Whatever the commander does is the deck's identity by definition - it is
    # the one card every game starts with. Never advise trimming it away.
    wants = commander_wants(analysis)
    identity.update(wants)
    blocked = set()
    for name in wants:
        blocked.update(SHAPE_CONFLICTS.get(name, ()))
    if analysis.commander_wants_high_curve:
        blocked.update(["low_curve_share", "avg_mv_norm"])

    if target_archetype is not None:
        target = dict(target_archetype.profile)
        blend_label = target_archetype.name
        identity.update(target_archetype.signature)
        declared.update(target_archetype.declared)
    else:
        target = blended_target(classification, blend_top)
        blend_label = " / ".join(m.archetype.name for m in
                                 blend_matches(classification, blend_top))
        # Anything that defines one of the deck's own top matches is part of
        # its identity, so never tell the player to cut it.
        for match in classification.top(max(blend_top, 3)):
            if match.affinity >= 0.05:
                identity.update(match.archetype.signature)
        for match in classification.top(blend_top):
            declared.update(match.archetype.declared)

    scored: List[Tuple[float, str, float]] = []
    for name in FEATURE_NAMES:
        gap = target.get(name, 0.0) - analysis.vector.get(name, 0.0)
        weight = FEATURE_WEIGHTS.get(name, 1.0)
        scored.append((abs(gap) * weight, name, gap))
    scored.sort(reverse=True)

    out: List[Recommendation] = []
    for score, name, gap in scored:
        if len(out) >= max_items:
            break
        if name in ("avg_mv_norm", "land_share"):
            continue  # covered, with better numbers, by the fundamentals pass
        # Only argue about features the matched archetypes take a position on;
        # everything else is baseline filler, not a real signal.
        if name not in declared or name in blocked:
            continue
        if abs(gap) < floor or abs(gap) * nonland < 2.0 or score < 0.03:
            continue
        if gap < 0 and (name in NEVER_TRIM or name in identity
                        or abs(gap) < TRIM_GAP):
            continue

        more, less = FEATURE_ADVICE.get(name, ("Adjust " + name, "Adjust " + name))
        cards = _cards(gap, nonland)
        why = FEATURE_WHY.get(name, "")
        title = more if gap > 0 else less
        detail = ("Roughly %d card%s worth. %s"
                  % (cards, "" if cards == 1 else "s", why)).strip()

        priority = MEDIUM if score >= 0.06 else LOW
        role = ROLES_BY_KEY.get(name)
        label = role.label if role is not None else name.replace("_", " ")
        evidence = "%s: %.0f%% of your spells vs %.0f%% for %s" % (
            label, 100 * analysis.vector.get(name, 0.0),
            100 * target.get(name, 0.0), blend_label)
        out.append(Recommendation(priority, "direction", title, detail, evidence,
                                  cards=int(round(gap * nonland))))

    out.sort(key=lambda r: _PRIORITY_ORDER[r.priority])
    return out


def focus_note(analysis: DeckAnalysis, classification: Classification,
               target_archetype: Optional[Archetype] = None) -> List[Recommendation]:
    """Commentary on whether the deck has committed to a plan at all."""
    out: List[Recommendation] = []
    best = classification.best
    second = classification.matches[1] if len(classification.matches) > 1 else None

    if target_archetype is not None:
        aimed = next((m for m in classification.matches
                      if m.archetype.key == target_archetype.key), None)
        if aimed is not None and aimed.archetype.key != best.archetype.key:
            out.append(Recommendation(
                HIGH, "focus", "Your deck does not read as %s yet"
                % target_archetype.name,
                "Measured against the reference profiles it is closest to %s "
                "(%.0f%%), with %s at %.0f%%. The recommendations below are "
                "the gap between what you have and what %s wants."
                % (best.archetype.name, 100 * best.affinity,
                   target_archetype.name, 100 * aimed.affinity,
                   target_archetype.name),
                "%s d=%.3f vs %s d=%.3f" % (best.archetype.name, best.distance,
                                            target_archetype.name, aimed.distance)))
        best = aimed or best

    if target_archetype is None and best.fit < 0.35:
        out.append(Recommendation(
            HIGH, "focus", "This deck does not have a clear plan yet",
            "Its closest match is %s, but even that is a loose fit. That "
            "usually means the deck is a pile of individually good cards. Pick "
            "the plan you enjoy most and cut cards that do not serve it - a "
            "focused deck beats a stronger unfocused one."
            % best.archetype.name,
            "best fit %.0f%%" % (100 * best.fit)))
    elif (target_archetype is None and classification.focus < 0.40
          and second is not None and second.affinity >= 0.20):
        out.append(Recommendation(
            MEDIUM, "focus", "Your deck is split between two plans",
            "It reads as %s (%.0f%%) and %s (%.0f%%) at once. That is fine if "
            "the two support each other, but if they compete for the same "
            "slots, committing to one will make the deck noticeably more "
            "consistent."
            % (best.archetype.name, 100 * best.affinity,
               second.archetype.name, 100 * second.affinity),
            "focus %.2f" % classification.focus))

    if best.archetype.watch_out and best.fit >= 0.35:
        out.append(Recommendation(
            LOW, "focus", "Watch out, as %s %s deck"
            % (_article(best.archetype.name), best.archetype.name),
            best.archetype.watch_out))

    # Signature-role check: is the deck missing the thing that defines its
    # closest archetype?
    for role_key in best.archetype.signature:
        have = analysis.vector.get(role_key, 0.0)
        want = best.archetype.profile.get(role_key, 0.0)
        if want > 0 and have < want * 0.6:
            role = ROLES_BY_KEY.get(role_key)
            label = role.label if role else role_key.replace("_", " ")
            out.append(Recommendation(
                HIGH, "focus", "Missing the core of %s %s deck"
                % (_article(best.archetype.name), best.archetype.name),
                "%s is what makes %s work, and you are at %.0f%% of the deck "
                "against about %.0f%% for the archetype. Adding roughly %d more "
                "would make the plan actually come together."
                % (label, best.archetype.name, 100 * have, 100 * want,
                   int(round((want - have) * (analysis.nonland_count or 1)))),
                "%s %.0f%% vs %.0f%%" % (label, 100 * have, 100 * want)))

    out.sort(key=lambda r: _PRIORITY_ORDER[r.priority])
    return out


def plan_roles(classification: Classification, blend_top: int = 2,
               target_archetype: Optional[Archetype] = None,
               analysis: Optional[DeckAnalysis] = None) -> set:
    """The role features the deck's plan actively wants more of than average.

    A role only counts as "on plan" if the archetype takes a position on it
    *and* asks for more of it than a generic deck carries - otherwise every
    archetype would want a bit of everything and nothing would be off-plan.
    """
    from .archetypes import BASELINE

    if target_archetype is not None:
        pool = [target_archetype]
    else:
        pool = [m.archetype for m in blend_matches(classification, blend_top)]

    wanted = set()
    for arch in pool:
        wanted.update(arch.signature)
        for name in arch.declared:
            if arch.profile.get(name, 0.0) > BASELINE.get(name, 0.0) * 1.05:
                wanted.add(name)
    # Whatever the commander cares about is on plan by definition.
    if analysis is not None:
        wanted.update(commander_wants(analysis))
    return wanted


def _tribe_credit(entry, analysis: DeckAnalysis, wanted: set) -> float:
    """Whether simply being the right creature type is a real contribution.

    In a tribal deck it is: a body of the chosen type turns on every lord.
    Nothing else about a card's shape - its cost, its card type - makes a card
    that does nothing else worth a slot.
    """
    if ("typal_concentration" in wanted and analysis.dominant_type
            and analysis.dominant_type in entry.type_line):
        return 1.0
    return 0.0


# EDHREC rank at which a card is neither notably popular nor notably ignored.
QUALITY_PIVOT = 1500.0


def _quality_bonus(rank: Optional[int]) -> float:
    """EDHREC rank as a quality proxy, on a log scale.

    Comparing this tool's suggestions against community upgrade guides showed
    that card quality drives real cut decisions at least as much as synergy
    does: the cards people actually remove from precons sit at ranks of four
    to twelve thousand while carrying the deck's theme perfectly well. Rank is
    a popularity measure rather than a power measure, but it is the only
    quality signal available, and it tracks those decisions closely.
    """
    if rank is None:
        return -1.5          # too rarely played for EDHREC to track at all
    return max(-2.5, min(2.0, -2.0 * math.log10(max(rank, 1) / QUALITY_PIVOT)))


def _shape_credit(entry, analysis: DeckAnalysis, wanted: set,
                  ignore: Sequence[str] = ()) -> float:
    """Credit a card for fitting the *shape* its plan asks for.

    Role matching alone misses this: a Big Mana deck declares a big top end,
    but "top_end_share" is not a role any card can carry, so without this a
    finisher looks like it contributes nothing to the plan that exists to cast
    it.
    """
    credit = 0.0
    wanted = wanted - set(ignore)
    type_line = entry.type_line.lower()
    is_creature = "creature" in type_line.split(" // ")[0]

    if "top_end_share" in wanted and entry.mana_value >= 6:
        credit += 1.0
    if "low_curve_share" in wanted and entry.mana_value <= 2:
        credit += 1.0
    if "creature_share" in wanted and is_creature:
        credit += 1.0
    if "instant_sorcery_share" in wanted and (
            "instant" in type_line or "sorcery" in type_line):
        credit += 1.0
    if "noncreature_permanent_share" in wanted and not is_creature and any(
            word in type_line for word in ("artifact", "enchantment",
                                           "planeswalker")):
        credit += 1.0
    if ("typal_concentration" in wanted and analysis.dominant_type
            and analysis.dominant_type in entry.type_line):
        credit += 1.0
    return credit


def cut_candidates(analysis: DeckAnalysis, classification: Classification,
                   blend_top: int = 2,
                   target_archetype: Optional[Archetype] = None,
                   limit: int = 10) -> List[CutCandidate]:
    """The cards contributing least to what this deck is trying to do.

    Commander decks are a fixed 100 cards, so every "add four of these" is
    also "cut four of those". Rather than guess, this scores each card on how
    much it does at all, how much of that is on plan, and what it costs to do
    it - then hands back the weakest, with the reason attached.

    This is the one place the tool names specific cards, and only ever cards
    the player already owns: "what should I cut" has no useful role-level
    answer.

    Cards filling a universally useful role - ramp, draw, removal, tutors,
    protection - are never listed here whatever the plan is. Sol Ring is not a
    cut candidate because a counters deck would rather have a counters card.
    Having *too much* ramp is real, but it is a role-level trim, which the
    direction pass handles.
    """
    wanted = plan_roles(classification, blend_top, target_archetype, analysis)
    plan_name = (target_archetype.name if target_archetype is not None
                 else classification.best.archetype.name)
    nonland = analysis.nonland_count or 1

    # Which roles the deck has more of than the plan calls for.  A universally
    # useful role only becomes cuttable once the deck is genuinely over-served:
    # the fifth board wipe is a cut, the first one never is.
    if target_archetype is not None:
        target = dict(target_archetype.profile)
    else:
        target = blended_target(classification, blend_top)
    oversupplied = {}
    for key in analysis.role_counts:
        count = analysis.role_counts.get(key, 0)
        wanted_count = target.get(key, 0.0) * nonland
        if key in UNIVERSAL_FLOORS:
            wanted_count = max(wanted_count, UNIVERSAL_FLOORS[key])
        surplus = count - wanted_count
        if surplus >= 2.0:
            # Remember by how much: a role that is three cards over should
            # surrender three cards, not every card that fills it.
            oversupplied[key] = int(surplus)

    # When the commander spells out what it works with, that is the deck's
    # plan and it outranks tag-derived synergy - which, in a precon, measures
    # whatever filler the deck happens to hold a lot of.
    condition = analysis.commander_condition
    trust_synergy = condition is None

    # Standardise synergy within the deck. In a deck whose theme is broad -
    # Food, say - most cards score 1.0 and synergy stops telling cards apart;
    # what matters is being on theme *relative to the rest of this deck*.
    spells = [e for e in analysis.entries if not e.is_land]
    if spells:
        mean_syn = sum(e.synergy for e in spells) / len(spells)
    else:
        mean_syn = 0.0

    scored: List[tuple] = []
    for entry in analysis.entries:
        if entry.is_land or entry.is_commander:
            continue
        roles = entry.roles
        enables = entry.satisfies(condition)
        synergy_score = (entry.synergy - mean_syn) if trust_synergy else 0.0
        rank = entry.edhrec_rank
        staple = rank is not None and rank <= STAPLE_RANK
        protected = bool((roles & UNIVERSAL_ROLES) - set(oversupplied))
        # A card carrying the deck's strongest theme is core - unless it is
        # also one of the least played cards in the format, which is exactly
        # the profile of the filler community upgrade guides cut first.
        core = enables or (trust_synergy and entry.synergy >= 0.85
                           and (rank is None or rank <= THEME_CORE_RANK))
        if core or staple or protected:
            continue

        on_plan = len(roles & wanted)
        shape = _shape_credit(entry, analysis, wanted)
        redundant_roles = roles & set(oversupplied)

        # How much this card contributes, graded rather than gated. A binary
        # "has at least one wanted role" test passes almost every creature -
        # combat_aggro alone is enough - which left the cut list nearly empty.
        score = (1.0 * on_plan + shape + 2.0 * synergy_score
                 - 0.8 * len(redundant_roles) + _quality_bonus(rank))

        if (not roles and entry.synergy < synergy_mod.SYNERGY_THRESHOLD
                and _tribe_credit(entry, analysis, wanted) == 0):
            tier, reason = "dead", "does nothing the rest of the deck can build on"
        elif roles and roles <= set(oversupplied):
            tier = "redundant"
            reason = ("you already have more %s than the plan needs"
                      % _join(sorted(ROLES_BY_KEY[r].label.lower()
                                     for r in roles if r in ROLES_BY_KEY)))
        elif condition and not enables:
            tier = "off_plan"
            reason = ("your commander cannot use it - it only works with %s of "
                      "mana value %d or more"
                      % (_join(["%ss" % t for t in condition["types"]]),
                         int(condition["min_mv"])))
        else:
            tier = "off_plan"
            reason = "contributes least to your %s plan" % plan_name
        if rank is not None and rank > 3000 and tier != "dead":
            reason += "; it is also among the least played cards here"

        scored.append((score, CutCandidate(entry.name, entry.quantity,
                                           entry.mana_value, reason,
                                           sorted(roles), tier, rank)))

    # Weakest contribution first; EDHREC rank breaks ties, since among equally
    # marginal cards the one nobody else plays is the easier cut.
    scored.sort(key=lambda pair: (pair[0], -(pair[1].rank or 100000)))
    # Above this, a card is pulling its weight; listing it would be noise.
    candidates = [c for score, c in scored if score < CUT_SCORE_CEILING]

    # A role never gives up more cards than the amount it is actually over by.
    quota = dict(oversupplied)
    out: List[CutCandidate] = []
    for candidate in candidates:
        keys = [r for r in candidate.roles if r in quota]
        if candidate.tier == "redundant":
            if not any(quota.get(k, 0) > 0 for k in keys):
                continue
            for key in keys:
                quota[key] -= 1
        out.append(candidate)
    return out[:limit]


def swap_budget(recommendations: Sequence[Recommendation]) -> int:
    """Net number of slots the advice above needs the player to find."""
    return sum(r.cards for r in recommendations if r.cards > 0)


def all_recommendations(analysis: DeckAnalysis,
                        classification: Classification,
                        blend_top: int = 2,
                        target_archetype: Optional[Archetype] = None
                        ) -> List[Recommendation]:
    recs = (focus_note(analysis, classification, target_archetype)
            + fundamentals(analysis, classification)
            + direction(analysis, classification, blend_top,
                        target_archetype=target_archetype))

    # A deck can be structurally sound and still be mostly weak cards, which is
    # the usual state of a preconstructed deck. The fundamentals and direction
    # passes both read such a deck as fine, so say it directly.
    changers = [e.name for e in analysis.entries if e.game_changer]
    if changers:
        recs.append(Recommendation(
            LOW, "bracket",
            "%d card%s here %s on the Game Changer list"
            % (len(changers), "" if len(changers) == 1 else "s",
               "is" if len(changers) == 1 else "are"),
            "%s. Wizards flags these as cards strong enough to move a deck up "
            "a Commander bracket, so they are worth a deliberate choice rather "
            "than an accident: they will make this deck noticeably stronger "
            "than an unmodified precon, which is not always what you want at a "
            "casual table."
            % _join(sorted(changers)),
            "%d game changers" % len(changers)))

    weak = cut_candidates(analysis, classification, blend_top,
                          target_archetype, limit=99)
    if len(weak) >= 12:
        recs.append(Recommendation(
            MEDIUM, "quality",
            "About %d cards here are doing very little for this deck" % len(weak),
            "They are not badly chosen so much as filler - individually "
            "replaceable cards that neither serve the plan nor stand on their "
            "own. Precons in particular improve more from swapping these than "
            "from any single upgrade. They are listed below, weakest first."
            % (), "%d low-contribution cards" % len(weak)))
    # The fundamentals pass and the direction pass can reach the same
    # conclusion by different routes; keep the first (higher priority) one.
    seen = set()
    unique: List[Recommendation] = []
    for rec in recs:
        if rec.title in seen:
            continue
        seen.add(rec.title)
        unique.append(rec)
    unique.sort(key=lambda r: _PRIORITY_ORDER[r.priority])
    return unique
