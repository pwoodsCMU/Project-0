"""Archetype reference profiles.

Each archetype is a point in the same feature space a real deck is measured in
(see :mod:`mtgcoach.features`), so "how close is this deck to Control?" is just
a weighted distance.

The numbers are **expert priors, not fitted parameters**: they were authored by
hand and then anchored against measured exemplar decks in ``decks/`` so that
each archetype's own exemplar lands closest to it.  ``BASELINE`` is a typical
mid-power Commander deck; every archetype only states the features where it
departs from that baseline, which keeps the profiles readable and keeps
un-stated features from silently dragging the distance around.

Profiles can be replaced or extended at runtime with ``--profiles file.json``
(see ``mtgcoach fit``), so the priors are a starting point rather than a
hard-coded verdict.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .features import FEATURE_NAMES

# A typical, unfocused mid-power Commander deck.
BASELINE: Dict[str, float] = {
    "ramp": 0.18,
    "card_draw": 0.20,
    "tutor": 0.04,
    "removal_spot": 0.13,
    "removal_mass": 0.07,
    "counterspell": 0.04,
    "protection": 0.12,
    "recursion": 0.09,
    "graveyard_matters": 0.10,
    "sacrifice": 0.08,
    "tokens": 0.12,
    "combat_aggro": 0.28,
    "equipment_auras": 0.05,
    "stax": 0.04,
    "lands_matter": 0.05,
    "combo_enabler": 0.05,
    "lifegain": 0.09,
    "group_slug": 0.05,
    "typal": 0.03,
    "creature_share": 0.34,
    "instant_sorcery_share": 0.32,
    "noncreature_permanent_share": 0.30,
    "avg_mv_norm": 0.47,
    "low_curve_share": 0.45,
    "top_end_share": 0.06,
    "land_share": 0.40,
}


class Archetype(object):
    def __init__(self, key: str, name: str, blurb: str, plan: str,
                 overrides: Dict[str, float], signature: Sequence[str],
                 watch_out: str = ""):
        self.key = key
        self.name = name
        self.blurb = blurb            # one line: what the deck is
        self.plan = plan              # how it wins
        self.signature = list(signature)   # the roles that define it
        self.watch_out = watch_out
        # Features this archetype actually takes a position on.  Everything
        # else falls back to BASELINE and is treated as "no opinion", so the
        # recommender never argues from a number nobody chose.
        self.declared = set(overrides)
        self.profile: Dict[str, float] = dict(BASELINE)
        self.profile.update(overrides)

    def vector(self) -> List[float]:
        return [self.profile.get(name, 0.0) for name in FEATURE_NAMES]


ARCHETYPES: List[Archetype] = [
    Archetype(
        "go_wide_aggro", "Go-Wide Aggro",
        "A wide board of small creatures backed by anthems and token makers.",
        "Flood the board, pump the team, and win with a big attack step before "
        "slower decks set up.",
        {"tokens": 0.32, "combat_aggro": 0.52, "creature_share": 0.42,
         "typal": 0.12, "low_curve_share": 0.48, "avg_mv_norm": 0.42,
         "removal_spot": 0.10, "removal_mass": 0.04, "counterspell": 0.01,
         "card_draw": 0.16, "ramp": 0.17, "equipment_auras": 0.07,
         "noncreature_permanent_share": 0.32, "instant_sorcery_share": 0.22,
         "top_end_share": 0.04, "lands_matter": 0.02, "combo_enabler": 0.02,
         "tutor": 0.02, "graveyard_matters": 0.05, "recursion": 0.04,
         "land_share": 0.38},
        signature=["tokens", "combat_aggro"],
        watch_out="One board wipe undoes your whole turn sequence - hold some "
                  "creatures back and carry protection."),

    Archetype(
        "voltron", "Voltron",
        "One creature, usually the commander, wearing every Equipment and Aura "
        "you own.",
        "Stack buffs and evasion onto a single threat and close the game with "
        "commander damage.",
        {"equipment_auras": 0.50, "protection": 0.32, "combat_aggro": 0.40,
         "creature_share": 0.24, "tutor": 0.09, "removal_spot": 0.12,
         "removal_mass": 0.04, "noncreature_permanent_share": 0.50,
         "low_curve_share": 0.58, "avg_mv_norm": 0.38, "tokens": 0.06,
         "card_draw": 0.14, "ramp": 0.15, "instant_sorcery_share": 0.26,
         "sacrifice": 0.03, "top_end_share": 0.03},
        signature=["equipment_auras", "protection", "combat_aggro"],
        watch_out="Everything rides on one creature. Without protection and a "
                  "backup threat, a single Swords to Plowshares ends you."),

    Archetype(
        "midrange_value", "Midrange Value",
        "Efficient creatures and answers, grinding out card advantage.",
        "Trade one-for-one, out-card everyone with value permanents, and win "
        "with whatever is left standing.",
        {"card_draw": 0.22, "removal_spot": 0.16, "removal_mass": 0.08,
         "tutor": 0.05, "creature_share": 0.36, "combat_aggro": 0.30,
         "recursion": 0.10, "tokens": 0.12,
         "noncreature_permanent_share": 0.28, "instant_sorcery_share": 0.30,
         "avg_mv_norm": 0.48, "top_end_share": 0.08},
        signature=["card_draw", "removal_spot"],
        watch_out="Good cards are not a game plan. If nothing here builds "
                  "toward a specific win, games go long and you lose to decks "
                  "that are actually trying to do something."),

    Archetype(
        "control", "Control",
        "Counterspells, wraths and card draw, winning late with a small number "
        "of finishers.",
        "Answer everything that matters, out-draw the table, then close with "
        "one or two threats.",
        {"counterspell": 0.20, "card_draw": 0.30, "removal_spot": 0.18,
         "removal_mass": 0.13, "creature_share": 0.14,
         "instant_sorcery_share": 0.55, "stax": 0.10, "combat_aggro": 0.10,
         "tokens": 0.10, "protection": 0.10, "avg_mv_norm": 0.50,
         "low_curve_share": 0.42, "top_end_share": 0.12,
         "noncreature_permanent_share": 0.28, "ramp": 0.16, "tutor": 0.05,
         "land_share": 0.42},
        signature=["counterspell", "removal_mass", "card_draw"],
        watch_out="Answering everything is not a win condition. Control decks "
                  "lose by drawing the game out with nothing to end it."),

    Archetype(
        "combo", "Combo",
        "Assembles a specific two- or three-card interaction and wins on the "
        "spot.",
        "Dig for the pieces, protect the turn you go off, and win outside "
        "combat.",
        {"tutor": 0.18, "combo_enabler": 0.20, "card_draw": 0.26, "ramp": 0.22,
         "counterspell": 0.10, "removal_spot": 0.10, "removal_mass": 0.04,
         "creature_share": 0.28, "instant_sorcery_share": 0.40,
         "low_curve_share": 0.55, "avg_mv_norm": 0.38, "protection": 0.14,
         "recursion": 0.10, "noncreature_permanent_share": 0.35,
         "land_share": 0.36, "combat_aggro": 0.12, "tokens": 0.06,
         "top_end_share": 0.04},
        signature=["tutor", "combo_enabler"],
        watch_out="Combo decks live and die by consistency. Too few tutors and "
                  "you never assemble; no protection and the first counterspell "
                  "wins."),

    Archetype(
        "stax_prison", "Stax / Prison",
        "Taxes, tappers and lockdown pieces that make the game unplayable for "
        "everyone else.",
        "Deny resources, break parity with your own engine, and win slowly "
        "while nobody else can act.",
        {"stax": 0.35, "removal_spot": 0.14, "removal_mass": 0.10, "tutor": 0.10,
         "ramp": 0.18, "card_draw": 0.14, "creature_share": 0.26,
         "noncreature_permanent_share": 0.45, "instant_sorcery_share": 0.20,
         "combat_aggro": 0.10, "tokens": 0.08, "protection": 0.10,
         "low_curve_share": 0.55, "avg_mv_norm": 0.40, "counterspell": 0.06},
        signature=["stax", "tutor"],
        watch_out="You have to break the lock you build, and you have to be "
                  "able to actually finish. Also: this archetype makes games "
                  "less fun for a casual table - read the room."),

    Archetype(
        "big_mana", "Big Mana / Ramp",
        "Accelerate hard, then deploy expensive haymakers well ahead of curve.",
        "Ramp into threats nobody can answer profitably, and let raw card "
        "quality take over.",
        {"ramp": 0.35, "top_end_share": 0.18, "avg_mv_norm": 0.60,
         "card_draw": 0.20, "removal_spot": 0.12, "removal_mass": 0.08,
         "creature_share": 0.38, "tokens": 0.10, "combat_aggro": 0.25,
         "lands_matter": 0.12, "combo_enabler": 0.08,
         "noncreature_permanent_share": 0.30, "instant_sorcery_share": 0.25,
         "low_curve_share": 0.30, "land_share": 0.42, "tutor": 0.06},
        signature=["ramp", "top_end_share"],
        watch_out="Ramping into nothing is the classic failure. Every ramp "
                  "spell needs a payoff worth the two turns you spent."),

    Archetype(
        "lands_matter", "Lands Matter",
        "Treats lands as spells: landfall triggers, land recursion, extra land "
        "drops.",
        "Turn land drops into value and win with landfall payoffs or a huge "
        "recursive engine.",
        {"lands_matter": 0.35, "ramp": 0.38, "recursion": 0.22,
         "graveyard_matters": 0.18, "card_draw": 0.22, "creature_share": 0.45,
         "removal_spot": 0.14, "land_share": 0.44, "low_curve_share": 0.42,
         "avg_mv_norm": 0.46, "tokens": 0.12, "sacrifice": 0.15,
         "combat_aggro": 0.18, "instant_sorcery_share": 0.32,
         "noncreature_permanent_share": 0.14},
        signature=["lands_matter", "ramp", "recursion"],
        watch_out="Land ramp is not card advantage on its own. Make sure the "
                  "landfall payoffs actually threaten to win."),

    Archetype(
        "aristocrats", "Aristocrats / Sacrifice",
        "Free sacrifice outlets, expendable bodies, and drain payoffs.",
        "Convert creatures into damage and life swings that ignore blockers "
        "and board wipes.",
        {"sacrifice": 0.40, "tokens": 0.30, "graveyard_matters": 0.20,
         "recursion": 0.18, "creature_share": 0.48, "card_draw": 0.20,
         "group_slug": 0.18, "lifegain": 0.16, "removal_spot": 0.12,
         "combat_aggro": 0.25, "ramp": 0.16,
         "noncreature_permanent_share": 0.22, "instant_sorcery_share": 0.22,
         "low_curve_share": 0.45, "avg_mv_norm": 0.42, "land_share": 0.38},
        signature=["sacrifice", "tokens", "group_slug"],
        watch_out="You need all three legs: a free outlet, a stream of bodies, "
                  "and a drain payoff. Missing one and the deck does nothing."),

    Archetype(
        "reanimator", "Graveyard / Reanimator",
        "Fills the graveyard on purpose, then cheats the best cards back into "
        "play.",
        "Discard or mill a huge threat early and reanimate it far ahead of "
        "schedule.",
        {"graveyard_matters": 0.38, "recursion": 0.35, "card_draw": 0.22,
         "tutor": 0.10, "creature_share": 0.40, "ramp": 0.18,
         "top_end_share": 0.16, "avg_mv_norm": 0.52, "removal_spot": 0.12,
         "sacrifice": 0.18, "instant_sorcery_share": 0.35,
         "noncreature_permanent_share": 0.20, "low_curve_share": 0.40,
         "removal_mass": 0.06},
        signature=["graveyard_matters", "recursion"],
        watch_out="Graveyard hate is common and hits you hardest. Keep a plan "
                  "that works with an empty graveyard."),

    Archetype(
        "spellslinger", "Spellslinger",
        "Instants and sorceries as the core of the deck, with permanents that "
        "reward casting them.",
        "Chain cheap spells into payoff triggers and burn or beat down with "
        "the tokens and prowess they generate.",
        {"instant_sorcery_share": 0.62, "creature_share": 0.12,
         "card_draw": 0.32, "counterspell": 0.14, "removal_spot": 0.20,
         "combo_enabler": 0.12, "tokens": 0.16, "combat_aggro": 0.14,
         "ramp": 0.18, "avg_mv_norm": 0.45, "low_curve_share": 0.50,
         "noncreature_permanent_share": 0.25, "recursion": 0.12,
         "graveyard_matters": 0.10, "group_slug": 0.10},
        signature=["instant_sorcery_share", "card_draw"],
        watch_out="Spell payoffs are fragile creatures. If they keep dying "
                  "before you untap, you are just playing a pile of cantrips."),

    Archetype(
        "typal", "Typal / Tribal",
        "One creature type, plus the lords and payoffs that reward playing it.",
        "Build a critical mass of one type, drop a lord, and attack.",
        {"typal": 0.35, "creature_share": 0.50, "combat_aggro": 0.40,
         "tokens": 0.15, "card_draw": 0.18, "removal_spot": 0.12,
         "tutor": 0.06, "ramp": 0.17, "low_curve_share": 0.45,
         "avg_mv_norm": 0.44, "instant_sorcery_share": 0.20,
         "noncreature_permanent_share": 0.24, "removal_mass": 0.04},
        signature=["typal", "creature_share"],
        watch_out="Typal decks routinely run creatures that are only playable "
                  "because of their type. Cut the weakest ones for interaction."),
]

ARCHETYPES_BY_KEY: Dict[str, Archetype] = {a.key: a for a in ARCHETYPES}


def load_profiles(path: str) -> None:
    """Merge learned/overriding profiles from JSON produced by ``mtgcoach fit``.

    Format: ``{"key": {"name": ..., "blurb": ..., "plan": ...,
    "profile": {feature: value}}}``.  Unknown keys are added as new archetypes.
    """
    import json

    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    for key, spec in payload.items():
        profile = spec.get("profile", {})
        existing = ARCHETYPES_BY_KEY.get(key)
        if existing is not None:
            existing.profile.update(profile)
            existing.declared |= set(profile)
            for attr in ("name", "blurb", "plan", "watch_out"):
                if spec.get(attr):
                    setattr(existing, attr, spec[attr])
            if spec.get("signature"):
                existing.signature = list(spec["signature"])
        else:
            arch = Archetype(key, spec.get("name", key), spec.get("blurb", ""),
                             spec.get("plan", ""), profile,
                             spec.get("signature", []), spec.get("watch_out", ""))
            ARCHETYPES.append(arch)
            ARCHETYPES_BY_KEY[key] = arch
