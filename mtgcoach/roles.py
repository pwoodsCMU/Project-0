"""Functional roles: what a card *does* for the deck.

Every card is scored against a fixed vocabulary of roles ("ramp", "spot
removal", "sacrifice payoff", ...).  A card can hold several roles at once -
Beast Within is both spot removal and an answer to any permanent type - so
roles are a multi-label view of the card, not a partition of it.

Two independent signals decide a role:

* **Scryfall Tagger oracle tags.**  Primary signal.  Tags are flattened up
  their hierarchy first (see :mod:`mtgcoach.scryfall`), so listing the parent
  label ``ramp`` also catches ``mana dork``, ``ritual``, ``land ramp`` and so on.
* **Oracle-text and type-line patterns.**  Backstop for cards Tagger has not
  reached yet, and for roles the tag vocabulary does not name directly.

Roles marked ``axis=True`` become dimensions of the archetype feature vector in
:mod:`mtgcoach.features`; the rest are descriptive only.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Set


class Role(object):
    """One functional category, plus the evidence that assigns a card to it."""

    def __init__(self, key: str, label: str, blurb: str,
                 tags: Sequence[str] = (),
                 exclude_tags: Sequence[str] = (),
                 text: Optional[str] = None,
                 type_line: Optional[str] = None,
                 axis: bool = False,
                 lands_eligible: bool = False):
        self.key = key
        self.label = label
        self.blurb = blurb
        self.tags = frozenset(tags)
        self.exclude_tags = frozenset(exclude_tags)
        self.text_re = re.compile(text, re.IGNORECASE) if text else None
        self.type_re = re.compile(type_line, re.IGNORECASE) if type_line else None
        self.axis = axis
        self.lands_eligible = lands_eligible

    def matches(self, card: dict, card_tags: Set[str]) -> bool:
        if self.exclude_tags & card_tags:
            return False
        if self.tags & card_tags:
            return True
        if self.type_re and self.type_re.search(card.get("type_line", "")):
            return True
        if self.text_re and self.text_re.search(card.get("oracle_text", "")):
            return True
        return False


ROLES: List[Role] = [
    Role("ramp", "Ramp / mana acceleration",
         "Extra mana: rocks, dorks, rituals, land fetch, extra land drops.",
         tags=["ramp"],
         text=r"(add \{[WUBRGC0-9]|search your library for (a|up to \w+)[^.]*basic land|"
              r"put[^.]*lands?[^.]*onto the battlefield|play an additional land)",
         axis=True),

    Role("card_draw", "Card draw / advantage",
         "Refills your hand: cantrips, draw engines, impulse and rummaging.",
         tags=["draw", "repeatable card advantage", "impulsive draw", "tome",
               "life for cards", "impulse"],
         text=r"\bdraw(s)? (a|one|two|three|four|five|\w+|that many) cards?\b",
         axis=True),

    Role("tutor", "Tutors / selection",
         "Fetches a specific nonland card, or digs deep for it.",
         tags=["tutor-card", "tutor-creature", "tutor-artifact", "tutor-enchantment",
               "tutor-instant", "tutor-sorcery", "tutor-permanent", "tutor-planeswalker",
               "tutor-mv", "tutor-legendary", "tutor-copy", "tutor-from-opponent",
               "tutor-interaction", "seek-nonland"],
         exclude_tags=["tutor-land"],
         text=r"search your library for a[n]? (card|creature|artifact|enchantment|"
              r"instant|sorcery|permanent|planeswalker)",
         axis=True),

    Role("removal_spot", "Spot removal / interaction",
         "Answers a single problem permanent.",
         tags=["spot removal"],
         text=r"(destroy target|exile target (creature|permanent|artifact|enchantment|"
              r"planeswalker|nonland)|deals \d+ damage to target creature|"
              r"target creature gets -\d+/-\d+)",
         axis=True),

    Role("removal_mass", "Board wipes / mass removal",
         "Resets the board or answers several permanents at once.",
         tags=["sweeper", "multi removal", "board-reset", "mass land denial"],
         text=r"(destroy all|exile all|destroy each|all creatures get -\d+/-\d+|"
              r"each player sacrifices)",
         axis=True),

    Role("counterspell", "Counterspells / stack interaction",
         "Stops spells and abilities before they resolve.",
         tags=["counterspell", "remove-from-stack"],
         exclude_tags=["hate-counterspell"],
         text=r"counter target (spell|activated|triggered|ability)",
         axis=True),

    Role("protection", "Protection / resilience",
         "Keeps your key pieces alive: hexproof, indestructible, fogs, phasing.",
         tags=["protection", "damage prevention", "fog", "pseudo-hexproof",
               "pseudo-shroud", "gains indestructible", "gains protection"],
         text=r"(hexproof|shroud|indestructible|protection from|"
              r"prevent all (combat )?damage|can't be countered)",
         axis=True),

    Role("recursion", "Recursion / reanimation",
         "Buys cards back from the graveyard.",
         tags=["recursion", "reanimate", "regrowth", "mass reanimation",
               "temporary reanimation", "castable from graveyard", "unexile"],
         text=r"return (target |another target |up to \w+ target )?[^.]*"
              r"from your graveyard to (your hand|the battlefield)",
         axis=True),

    Role("graveyard_matters", "Graveyard synergy",
         "Uses the graveyard as a resource: self-mill, discard outlets, delve.",
         tags=["graveyard fuel", "cards in graveyard matter", "mill-self",
               "discard outlet", "delve", "activate from graveyard",
               "trigger from graveyard", "synergy-graveyard-cast",
               "leaving graveyard matters", "self-discard matters"],
         text=r"(cards? in your graveyard|mill \w+ cards?|from your graveyard)",
         axis=True),

    Role("sacrifice", "Sacrifice / death payoffs",
         "Sacrifice outlets and the death triggers that pay them off.",
         tags=["sacrifice outlet", "death trigger", "sacrifice matters",
               "removal-sacrifice"],
         text=r"(sacrifice (a|an|another|one|two|three)\b|whenever [^.]*dies)",
         axis=True),

    Role("tokens", "Token generation",
         "Makes bodies out of nothing - the engine behind go-wide boards.",
         tags=["repeatable token generator", "synergy-token", "token increaser"],
         text=r"creates? (a|an|one|two|three|four|\w+|that many)[^.]*token",
         axis=True),

    Role("combat_aggro", "Combat / pressure",
         "Evasion, anthems, haste, attack triggers, extra combats.",
         tags=["evasion", "gives evasion", "attacking matters", "attacking matters-self",
               "anthem", "keyword anthem", "power boost to all", "overrun",
               "gives haste", "extra combat phase", "combat trick", "saboteur",
               "gives trample"],
         text=r"(whenever [^.]*attacks|can't be blocked|"
              r"creatures you control get \+\d+/\+\d+|gains haste)",
         axis=True),

    Role("equipment_auras", "Equipment & Auras",
         "Suits up one creature - the Voltron toolkit.",
         tags=["synergy-equipment", "synergy-aura", "quick attach", "auraify"],
         type_line=r"\b(Equipment|Aura)\b",
         axis=True),

    Role("stax", "Taxes / lockdown",
         "Slows everyone else down: tax effects, tappers, prison pieces.",
         tags=["lockdown", "tax", "cast tax", "rule of law", "stasis", "pillowfort",
               "hatebear", "cost increaser", "tapper", "mass land denial",
               "kismet effect", "doesn't untap", "skip turn", "prevent cast",
               "prevent etb", "prevent trigger", "prevent activation",
               "hand size decrease", "blood moon effect", "exile with tax"],
         text=r"(players? can't|spells cost \{\d+\} more|"
              r"don't untap during|skips? (their|his or her) (untap|draw) step)",
         axis=True),

    Role("lands_matter", "Lands matter",
         "Landfall, land recursion, extra land drops, lands as a resource.",
         tags=["lands matter", "landfall", "play additional land",
               "crucible of worlds", "reanimate-land", "lands in graveyard matter"],
         text=r"(landfall|whenever a land (you control )?enters|"
              r"lands? from your graveyard)",
         axis=True),

    Role("combo_enabler", "Combo pieces / engines",
         "Cost reduction, untappers, rituals, extra turns, alternate wins.",
         tags=["cost reducer", "extra turn", "untapper", "extra untap", "ritual",
               "storm count matters", "storm-like", "alternate win condition",
               "polymorph", "trigger doubler"],
         text=r"(take an extra turn|you win the game|"
              r"untap (target|all|up to)[^.]*(permanent|creature|land)|"
              r"spells you cast cost \{\d+\} less)",
         axis=True),

    Role("lifegain", "Lifegain",
         "Life as a buffer or as a resource to spend.",
         tags=["lifegain", "lifegain matters", "drain life"],
         text=r"(gain \d+ life|gains? \w+ life\b|lifelink)",
         axis=True),

    Role("group_slug", "Group slug / reach damage",
         "Drains and burns the whole table without attacking.",
         tags=["group slug", "burn player", "opponent loses life", "burn-you"],
         text=r"(each opponent loses \d+ life|deals \d+ damage to each opponent|"
              r"each opponent sacrifices)",
         axis=True),

    Role("typal", "Typal / tribal payoffs",
         "Lords and cards that reward a specific creature type. This counts "
         "payoffs only - how concentrated the creatures themselves are is "
         "measured separately.",
         tags=["typal", "typal coupling", "noncreature typal", "typal-creature"],
         axis=True),

    Role("counters_matter", "+1/+1 counters",
         "Counter placement, proliferate, and the payoffs that scale off them.",
         tags=["counters matter", "pp counters matter", "repeatable-proliferate",
               "gives pp counters", "repeatable pp counters", "gains pp counters",
               "counter increaser", "move counters", "counter fuel-pp"],
         text=r"(\+1/\+1 counter|proliferate)",
         axis=True),

    # --- descriptive-only roles (not distance axes) ------------------------- #
    Role("graveyard_hate", "Graveyard hate",
         "Answers other people's graveyards.",
         tags=["hate-graveyard"],
         text=r"exile (all cards from|target card from|that card)[^.]*graveyard"),

    Role("politics", "Politics / multiplayer levers",
         "Group hug, voting, goad, monarch, theft - table-facing effects.",
         tags=["group hug", "selective group hug", "voting", "monarch matters",
               "multiplayer", "theft", "control changing effects"],
         text=r"(goad|becomes? the monarch|vote|gain control of target)"),

    Role("mill_opponent", "Mill",
         "Attacks opposing libraries.",
         tags=["mill-opponent"],
         text=r"target (player|opponent) mills"),

    Role("hand_disruption", "Discard / hand attack",
         "Strips cards out of opposing hands.",
         tags=["hand disruption"],
         text=r"(each opponent discards|target (player|opponent) discards)"),

    Role("artifact_matters", "Artifact synergy",
         "Artifact count and artifact recursion payoffs.",
         tags=["artifact matters", "synergy-artifact", "repeatable artifact tokens",
               "animate artifact", "protects-artifact"],
         text=r"artifacts?( and \w+)? you control"),

    Role("enchantment_matters", "Enchantment synergy",
         "Constellation and enchantress-style payoffs.",
         tags=["synergy-enchantment", "enchantment engine", "animate enchantment",
               "protects-enchantment"],
         text=r"enchantments?( and \w+)? you control"),



    Role("utility_land", "Utility lands",
         "Lands that do more than make mana.",
         tags=["utility land"],
         lands_eligible=True),
]

ROLES_BY_KEY: Dict[str, Role] = {r.key: r for r in ROLES}
AXIS_ROLES: List[str] = [r.key for r in ROLES if r.axis]


def is_land(card: dict) -> bool:
    """True for cards whose *front* face is a land (MDFC land backs excluded)."""
    front = (card.get("type_line") or "").split(" // ")[0]
    return "land" in front.lower()


def card_roles(card: dict, card_tags: Sequence[str]) -> Set[str]:
    """The set of role keys a single card fills."""
    tags = set(card_tags)
    land = is_land(card)
    out: Set[str] = set()
    for role in ROLES:
        if land and not role.lands_eligible:
            continue
        if role.matches(card, tags):
            out.add(role.key)
    return out
