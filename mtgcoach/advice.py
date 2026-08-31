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

from typing import Dict, List, Optional, Sequence, Tuple

from .archetypes import Archetype
from .classify import Classification, blended_target
from .features import (COLOR_NAMES, DeckAnalysis, FEATURE_NAMES,
                       FEATURE_WEIGHTS)
from .roles import ROLES_BY_KEY

HIGH, MEDIUM, LOW = "high", "medium", "low"
_PRIORITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

# Roles almost no Commander deck regrets having more of.
NEVER_TRIM = frozenset(["card_draw", "removal_spot", "protection"])

# "You have too much of X" needs a much bigger gap than "you need more X"
# before it is worth saying - over-investment is usually the deck's identity.
TRIM_GAP = 0.11


def color_sources_needed(pips: int) -> int:
    """Rule-of-thumb source count for a given number of coloured pips."""
    return int(min(24, round(9 + 0.35 * pips)))


class Recommendation(object):
    __slots__ = ("priority", "kind", "title", "detail", "evidence")

    def __init__(self, priority: str, kind: str, title: str, detail: str,
                 evidence: str = ""):
        self.priority = priority
        self.kind = kind              # "mana" | "fundamentals" | "direction" | "focus"
        self.title = title
        self.detail = detail
        self.evidence = evidence

    def as_dict(self) -> Dict[str, str]:
        return {"priority": self.priority, "kind": self.kind, "title": self.title,
                "detail": self.detail, "evidence": self.evidence}


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
    target = 36.0 + 2.4 * (analysis.avg_mv - 3.0)

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
            "You have %d lands with an average mana value of %.2f. For that "
            "curve, %d lands is the usual floor. Missing land drops is the "
            "most common reason a deck feels like it does nothing."
            % (analysis.land_count, analysis.avg_mv, target),
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
    if interaction < 8:
        out.append(Recommendation(
            HIGH, "fundamentals", "Add more interaction",
            "You have %d cards that answer an opponent (%d spot removal, %d "
            "mass removal). Commander decks want 8-12; without them you simply "
            "lose to whoever plays the strongest permanent."
            % (interaction, spot, mass),
            "%d answers" % interaction))
    if mass < 2 and not aggro_ish:
        out.append(Recommendation(
            MEDIUM, "fundamentals", "Add a board wipe or two",
            "You have %d mass removal effects. Two or three give you an out "
            "when a creature deck goes wide on you." % mass,
            "%d board wipes" % mass))

    # --- curve ------------------------------------------------------------ #
    if analysis.avg_mv > 3.6:
        heavy = analysis.curve.get("6", 0) + analysis.curve.get("7+", 0)
        out.append(Recommendation(
            HIGH if analysis.avg_mv > 4.0 else MEDIUM, "fundamentals",
            "Lower your curve",
            "Average mana value is %.2f with %d cards at 6+. Trading a few of "
            "the expensive cards for cheaper effects that do most of the same "
            "job makes the deck far more consistent."
            % (analysis.avg_mv, heavy),
            "avg MV %.2f" % analysis.avg_mv))

    cheap = sum(analysis.curve.get(b, 0) for b in ("0", "1", "2"))
    if cheap / float(nonland) < 0.25:
        out.append(Recommendation(
            MEDIUM, "fundamentals", "Add more early plays",
            "Only %d of your %d spells cost 2 or less (%.0f%%). Aim for about "
            "a third, so your first three turns are not blank."
            % (cheap, nonland, 100.0 * cheap / nonland),
            "%d cheap spells" % cheap))

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

    if target_archetype is not None:
        target = dict(target_archetype.profile)
        blend_label = target_archetype.name
        identity.update(target_archetype.signature)
        declared.update(target_archetype.declared)
    else:
        target = blended_target(classification, blend_top)
        blend_label = " / ".join(m.archetype.name
                                 for m in classification.top(blend_top))
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
        if name not in declared:
            continue
        # Ignore differences smaller than ~2 cards; they are noise.
        if abs(gap) * nonland < 2.0 or score < 0.03:
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
        out.append(Recommendation(priority, "direction", title, detail, evidence))

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
          and second is not None):
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
            LOW, "focus", "Watch out, as a %s deck" % best.archetype.name,
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
                HIGH, "focus", "Missing the core of a %s deck"
                % best.archetype.name,
                "%s is what makes %s work, and you are at %.0f%% of the deck "
                "against about %.0f%% for the archetype. Adding roughly %d more "
                "would make the plan actually come together."
                % (label, best.archetype.name, 100 * have, 100 * want,
                   int(round((want - have) * (analysis.nonland_count or 1)))),
                "%s %.0f%% vs %.0f%%" % (label, 100 * have, 100 * want)))

    out.sort(key=lambda r: _PRIORITY_ORDER[r.priority])
    return out


def all_recommendations(analysis: DeckAnalysis,
                        classification: Classification,
                        blend_top: int = 2,
                        target_archetype: Optional[Archetype] = None
                        ) -> List[Recommendation]:
    recs = (focus_note(analysis, classification, target_archetype)
            + fundamentals(analysis, classification)
            + direction(analysis, classification, blend_top,
                        target_archetype=target_archetype))
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
