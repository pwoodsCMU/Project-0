"""Unit tests. Everything here runs offline against synthetic card data;
the one integration test skips itself unless the Scryfall cache is populated.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mtgcoach import (advice, archetypes, classify, corpus, decklist, features,
                      roles, scryfall, synergy)
from mtgcoach.scryfall import normalize_name


def card(name, cost="{2}", mv=2.0, type_line="Creature — Human",
         text="", colors=None, identity=None, produces=None, oracle_id=None):
    return {
        "name": name,
        "oracle_id": oracle_id or ("oid-" + name.lower().replace(" ", "-")),
        "mana_cost": cost,
        "mana_value": mv,
        "type_line": type_line,
        "oracle_text": text,
        "colors": colors or [],
        "color_identity": identity if identity is not None else (colors or []),
        "produced_mana": produces or [],
        "power": None, "toughness": None, "keywords": [], "layout": "normal",
        "rarity": "common",
    }


def build(cards_list, deck_text, tags=None):
    parsed = decklist.parse_decklist(deck_text)
    cards = {normalize_name(c["name"]): c for c in cards_list}
    return parsed, cards, (tags or {})


class TestDecklistParsing(unittest.TestCase):
    def test_quantities_sets_and_flags(self):
        deck = decklist.parse_decklist(
            "1 Sol Ring (LTC) 236 *F*\n"
            "3x Lightning Bolt\n"
            "Forest\n")
        self.assertEqual([(e.quantity, e.name) for e in deck.entries],
                         [(1, "Sol Ring"), (3, "Lightning Bolt"), (1, "Forest")])

    def test_commander_marker_and_sections(self):
        deck = decklist.parse_decklist(
            "1 Korvold, Fae-Cursed King *CMDR*\n1 Sol Ring\n")
        self.assertEqual(deck.commander_names, ["Korvold, Fae-Cursed King"])

        deck = decklist.parse_decklist(
            "Commander (1)\n1 Ephara, God of the Polis\n\nDeck\n1 Sol Ring\n")
        self.assertEqual(deck.commander_names, ["Ephara, God of the Polis"])

    def test_sideboard_is_excluded(self):
        deck = decklist.parse_decklist(
            "1 Sol Ring\nSB: 1 Pithing Needle\n\nSideboard\n1 Grafdigger's Cage\n")
        self.assertEqual([e.name for e in deck.entries], ["Sol Ring"])
        self.assertEqual(sorted(e.name for e in deck.excluded),
                         ["Grafdigger's Cage", "Pithing Needle"])

    def test_leading_block_is_treated_as_commander_in_a_100_card_list(self):
        text = "1 Sram, Senior Edificer\n\n" + "\n".join(
            ["1 Card %d" % i for i in range(98)]) + "\n1 Plains\n"
        deck = decklist.parse_decklist(text)
        self.assertEqual(deck.total_cards, 100)
        self.assertEqual(deck.commander_names, ["Sram, Senior Edificer"])

    def test_commander_override(self):
        deck = decklist.parse_decklist("1 Sol Ring\n1 Sram, Senior Edificer\n",
                                       commander_override=["Sram, Senior Edificer"])
        self.assertEqual(deck.commander_names, ["Sram, Senior Edificer"])

    def test_comments_are_ignored_but_headers_are_not(self):
        deck = decklist.parse_decklist(
            "# my deck\n// Commander\n1 Sram, Senior Edificer\n// Deck\n1 Plains\n")
        self.assertEqual(deck.commander_names, ["Sram, Senior Edificer"])
        self.assertEqual(deck.total_cards, 2)


class TestRoles(unittest.TestCase):
    def test_tag_hierarchy_parent_matches(self):
        # "mana dork" flattens up to "ramp" in the tag index, so the role
        # matcher only ever has to know the parent label.
        found = roles.card_roles(card("Llanowar Elves"), ["mana dork", "ramp"])
        self.assertIn("ramp", found)

    def test_land_tutors_are_ramp_not_tutors(self):
        found = roles.card_roles(
            card("Rampant Growth", type_line="Sorcery",
                 text="Search your library for a basic land card, put it onto "
                      "the battlefield tapped, then shuffle."),
            ["ramp", "land ramp", "tutor-land", "tutor-land-basic"])
        self.assertIn("ramp", found)
        self.assertNotIn("tutor", found)

    def test_real_tutors_are_tutors(self):
        found = roles.card_roles(
            card("Demonic Tutor", type_line="Sorcery",
                 text="Search your library for a card, put it into your hand, "
                      "then shuffle."),
            ["tutor-card"])
        self.assertIn("tutor", found)

    def test_equipment_matched_by_type_line(self):
        found = roles.card_roles(
            card("Colossus Hammer", type_line="Artifact — Equipment"), [])
        self.assertIn("equipment_auras", found)

    def test_text_fallback_when_untagged(self):
        found = roles.card_roles(
            card("Nameless Wrath", type_line="Sorcery",
                 text="Destroy all creatures."), [])
        self.assertIn("removal_mass", found)

    def test_lands_do_not_pick_up_spell_roles(self):
        found = roles.card_roles(
            card("Forest", cost="", mv=0.0, type_line="Basic Land — Forest",
                 text="({T}: Add {G}.)"), [])
        self.assertNotIn("ramp", found)


class TestFeatures(unittest.TestCase):
    def _deck(self):
        cards_list = [
            card("Forest", cost="", mv=0.0, type_line="Basic Land — Forest",
                 text="({T}: Add {G}.)", produces=["G"]),
            card("Llanowar Elves", cost="{G}", mv=1.0,
                 type_line="Creature — Elf Druid", text="{T}: Add {G}.",
                 colors=["G"], produces=["G"]),
            card("Overrun", cost="{2}{G}{G}{G}", mv=5.0, type_line="Sorcery",
                 text="Creatures you control get +3/+3 and gain trample.",
                 colors=["G"]),
            card("Ghalta, Primal Hunger", cost="{10}{G}{G}", mv=12.0,
                 type_line="Legendary Creature — Elder Dinosaur", colors=["G"]),
        ]
        text = ("// Commander\n1 Ghalta, Primal Hunger\n\n// Deck\n"
                "1 Llanowar Elves\n1 Overrun\n37 Forest\n")
        return build(cards_list, text)

    def test_counts_and_curve(self):
        parsed, cards, tags = self._deck()
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.total, 40)
        self.assertEqual(an.land_count, 37)
        self.assertEqual(an.nonland_count, 3)
        self.assertEqual(an.curve["1"], 1)
        self.assertEqual(an.curve["5"], 1)
        self.assertEqual(an.curve["7+"], 1)
        self.assertAlmostEqual(an.avg_mv, 6.0)

    def test_vector_shares_are_fractions_of_spells(self):
        parsed, cards, tags = self._deck()
        an = features.analyze(parsed, cards, tags)
        self.assertAlmostEqual(an.vector["creature_share"], 2 / 3.0)
        self.assertAlmostEqual(an.vector["land_share"], 37 / 40.0)
        self.assertTrue(0.0 <= an.vector["avg_mv_norm"] <= 1.0)

    def test_pips_counted_per_copy(self):
        parsed, cards, tags = self._deck()
        an = features.analyze(parsed, cards, tags)
        # Llanowar Elves {G} + Overrun {2}{G}{G}{G} + Ghalta {10}{G}{G}
        self.assertEqual(an.pips["G"], 1 + 3 + 2)

    def test_singleton_and_color_identity_violations(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains",
                 produces=["W"]),
            card("Sram, Senior Edificer", cost="{2}{W}", mv=3.0,
                 type_line="Legendary Creature — Dwarf Advisor", colors=["W"]),
            card("Lightning Bolt", cost="{R}", mv=1.0, type_line="Instant",
                 colors=["R"]),
            card("Sol Ring", cost="{1}", mv=1.0, type_line="Artifact"),
        ]
        text = ("// Commander\n1 Sram, Senior Edificer\n\n// Deck\n"
                "2 Sol Ring\n1 Lightning Bolt\n30 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        joined = " | ".join(an.legality)
        self.assertIn("singleton", joined)
        self.assertIn("colour identity", joined)
        self.assertIn("100", joined)

    def test_unresolved_cards_are_reported_not_counted(self):
        parsed, cards, tags = build([card("Plains", cost="", mv=0.0,
                                          type_line="Basic Land — Plains")],
                                    "1 Plains\n1 Definitely Not A Card\n")
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.unresolved, ["Definitely Not A Card"])
        self.assertEqual(an.total, 1)


class TestCreatureTypes(unittest.TestCase):
    def _analyse(self, deck_text, cards_list, tags=None):
        parsed, cards, tag_map = build(cards_list, deck_text, tags)
        return features.analyze(parsed, cards, tag_map)

    def test_concentration_of_a_tribal_deck(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Elemental A", type_line="Creature — Elemental"),
            card("Elemental B", type_line="Creature — Elemental Shaman"),
            card("Random Bear", type_line="Creature — Bear"),
        ]
        an = self._analyse(
            "1 Elemental A\n1 Elemental B\n1 Random Bear\n1 Plains\n", cards_list)
        self.assertEqual(an.dominant_type, "Elemental")
        self.assertEqual(an.creature_count, 3)
        self.assertAlmostEqual(an.vector["typal_concentration"], 2 / 3.0)

    def test_changelings_count_as_the_tribe(self):
        cards_list = [
            card("Elemental A", type_line="Creature — Elemental"),
            card("Elemental B", type_line="Creature — Elemental"),
            card("Universal Automaton",
                 type_line="Creature — Shapeshifter",
                 text="Changeling (This card is every creature type.)"),
        ]
        an = self._analyse(
            "1 Elemental A\n1 Elemental B\n1 Universal Automaton\n", cards_list)
        self.assertEqual(an.dominant_type, "Elemental")
        self.assertAlmostEqual(an.vector["typal_concentration"], 1.0)

    def test_commander_type_wins_a_near_tie(self):
        # Four Humans, three Elementals, but the commander is an Elemental
        # payoff, so the deck is measured on Elementals.
        cards_list = [card("Elemental %d" % i, type_line="Creature — Elemental")
                      for i in range(3)]
        cards_list += [card("Human %d" % i, type_line="Creature — Human")
                       for i in range(4)]
        cards_list.append(card("Ashling", type_line="Legendary Creature — Elemental"))
        text = ("// Commander\n1 Ashling\n\n// Deck\n"
                + "\n".join("1 Elemental %d" % i for i in range(3)) + "\n"
                + "\n".join("1 Human %d" % i for i in range(4)) + "\n")
        an = self._analyse(text, cards_list)
        self.assertEqual(an.dominant_type, "Elemental")

    def test_typal_tag_identifies_the_tribe_a_commander_pumps(self):
        cards_list = [card("Goblin %d" % i, type_line="Creature — Goblin")
                      for i in range(3)]
        cards_list += [card("Elf %d" % i, type_line="Creature — Elf")
                       for i in range(3)]
        cards_list.append(card("Goblin Lord",
                               type_line="Legendary Creature — Human Advisor"))
        text = ("// Commander\n1 Goblin Lord\n\n// Deck\n"
                + "\n".join("1 Goblin %d" % i for i in range(3)) + "\n"
                + "\n".join("1 Elf %d" % i for i in range(3)) + "\n")
        tags = {"oid-goblin-lord": ["typal", "typal-goblin"]}
        an = self._analyse(text, cards_list, tags)
        self.assertEqual(an.dominant_type, "Goblin")


class TestCommanderInfluence(unittest.TestCase):
    def test_commander_roles_are_weighted_above_a_single_copy(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Big Boss", type_line="Legendary Creature — Human",
                 text="Counter target spell."),
        ]
        cards_list += [card("Filler %d" % i, type_line="Creature — Bear")
                       for i in range(9)]
        text = ("// Commander\n1 Big Boss\n\n// Deck\n"
                + "\n".join("1 Filler %d" % i for i in range(9))
                + "\n36 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.role_counts["counterspell"], 1)   # honest count
        self.assertEqual(an.nonland_count, 10)
        self.assertGreater(an.vector["counterspell"], 1.0 / an.nonland_count)

    def test_cost_reduction_commander_raises_the_supported_curve(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Expensive Thing", cost="{7}", mv=7.0, type_line="Creature — Giant"),
            card("Discount Lord", cost="{2}{W}", mv=3.0,
                 type_line="Legendary Creature — Human",
                 text="Creature spells you cast cost {2} less to cast."),
        ]
        text = ("// Commander\n1 Discount Lord\n\n// Deck\n"
                "1 Expensive Thing\n36 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertTrue(an.commander_notes)
        self.assertLess(an.effective_avg_mv(), an.avg_mv)
        self.assertLess(advice.recommended_lands(an),
                        int(round(36 + 2.4 * (an.avg_mv - 3.0))))

    def test_plain_commander_gets_no_allowance(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Vanilla Boss", type_line="Legendary Creature — Human"),
        ]
        parsed, cards, tags = build(
            cards_list, "// Commander\n1 Vanilla Boss\n\n// Deck\n1 Plains\n")
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.commander_notes, [])
        self.assertAlmostEqual(an.effective_avg_mv(), an.avg_mv)


class TestXSpells(unittest.TestCase):
    def test_x_spells_are_not_counted_as_cheap(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Boss", type_line="Legendary Creature — Human"),
            card("Fireball", cost="{X}{R}", mv=1.0, type_line="Sorcery",
                 text="Fireball deals X damage divided as you choose."),
            card("Walking Ballista", cost="{X}{X}", mv=0.0,
                 type_line="Artifact Creature — Construct"),
        ]
        text = ("// Commander\n1 Boss\n\n// Deck\n1 Fireball\n"
                "1 Walking Ballista\n36 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.x_spell_count, 2)
        # {X} counts for two mana each: Fireball lands at 3, Ballista at 4.
        self.assertEqual(an.curve["3"], 1)
        self.assertEqual(an.curve["4"], 1)
        self.assertEqual(an.curve["0"], 0)
        self.assertEqual(an.curve["1"], 0)

    def test_ordinary_spells_are_unaffected(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Boss", type_line="Legendary Creature — Human"),
            card("Shock", cost="{R}", mv=1.0, type_line="Instant"),
        ]
        parsed, cards, tags = build(
            cards_list, "// Commander\n1 Boss\n\n// Deck\n1 Shock\n36 Plains\n")
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.x_spell_count, 0)
        self.assertEqual(an.curve["1"], 1)


class TestStapleProtection(unittest.TestCase):
    def _deck_with(self, extra):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Boss", type_line="Legendary Creature — Human"),
        ] + extra
        lines = "".join("1 %s\n" % c["name"] for c in extra)
        text = "// Commander\n1 Boss\n\n// Deck\n%s36 Plains\n" % lines
        parsed, cards, tags = build(cards_list, text)
        return features.analyze(parsed, cards, tags)

    def test_a_staple_is_never_pointed_at(self):
        wipes = []
        for i in range(12):
            entry = card("Wipe %d" % i, cost="{3}{W}{W}", mv=5.0,
                         type_line="Sorcery", text="Destroy all creatures.")
            entry["edhrec_rank"] = 9000
            wipes.append(entry)
        farewell = card("Farewell", cost="{4}{W}{W}", mv=6.0,
                        type_line="Sorcery", text="Destroy all creatures.")
        farewell["edhrec_rank"] = 169          # a real format staple
        an = self._deck_with(wipes + [farewell])
        result = classify.classify(an.vector)
        names = [c.name for c in advice.cut_candidates(an, result, limit=99)]
        self.assertNotIn("Farewell", names)
        self.assertTrue(names, "expected the unremarkable wipes to be listed")

    def test_a_role_never_gives_up_more_than_its_surplus(self):
        wipes = []
        for i in range(14):
            entry = card("Wipe %d" % i, cost="{3}{W}{W}", mv=5.0,
                         type_line="Sorcery", text="Destroy all creatures.")
            entry["edhrec_rank"] = 9000
            wipes.append(entry)
        an = self._deck_with(wipes)
        result = classify.classify(an.vector)
        cuts = advice.cut_candidates(an, result, limit=99)
        surplus = 14 - advice.UNIVERSAL_FLOORS["removal_mass"]
        self.assertLessEqual(len([c for c in cuts if c.tier == "redundant"]),
                             surplus)


class TestCommanderShapesThePlan(unittest.TestCase):
    def test_an_artifact_commander_protects_its_artifacts(self):
        # The complaint this fixes: an artifact/enchantment commander's deck
        # being told to trim noncreature permanents.
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Artifact Boss", type_line="Legendary Creature — Human",
                 text="Artifacts you control get +1/+1."),
        ]
        cards_list += [card("Widget %d" % i, cost="{3}", mv=4.0,
                            type_line="Artifact") for i in range(20)]
        text = ("// Commander\n1 Artifact Boss\n\n// Deck\n"
                + "\n".join("1 Widget %d" % i for i in range(20))
                + "\n36 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertIn("artifact_matters", an.commanders[0].roles)
        wants = advice.commander_wants(an)
        self.assertIn("noncreature_permanent_share", wants)
        result = classify.classify(an.vector)
        titles = [r.title for r in advice.direction(an, result)]
        self.assertNotIn("Trim noncreature permanents", titles)


class TestClassification(unittest.TestCase):
    def test_archetype_profile_matches_itself(self):
        for arch in archetypes.ARCHETYPES:
            result = classify.classify(dict(arch.profile))
            self.assertEqual(result.best.archetype.key, arch.key)
            self.assertAlmostEqual(result.best.distance, 0.0, places=9)

    def test_affinities_sum_to_one(self):
        result = classify.classify(dict(archetypes.BASELINE))
        self.assertAlmostEqual(sum(m.affinity for m in result.matches), 1.0,
                               places=6)

    def test_focus_is_higher_for_a_distinctive_deck(self):
        voltron = classify.classify(
            dict(archetypes.ARCHETYPES_BY_KEY["voltron"].profile))
        baseline = classify.classify(dict(archetypes.BASELINE))
        self.assertGreater(voltron.focus, baseline.focus)

    def test_blended_target_lies_between_the_two_archetypes(self):
        result = classify.classify(dict(archetypes.BASELINE))
        target = classify.blended_target(result, 2)
        first = result.matches[0].archetype.profile
        second = result.matches[1].archetype.profile
        for name in ("ramp", "card_draw", "creature_share"):
            low, high = sorted((first[name], second[name]))
            self.assertTrue(low - 1e-9 <= target[name] <= high + 1e-9)

    def test_distance_is_symmetric_and_zero_on_identity(self):
        a = dict(archetypes.ARCHETYPES_BY_KEY["control"].profile)
        b = dict(archetypes.ARCHETYPES_BY_KEY["combo"].profile)
        self.assertAlmostEqual(classify.distance(a, b), classify.distance(b, a))
        self.assertAlmostEqual(classify.distance(a, a), 0.0)


class TestAdvice(unittest.TestCase):
    def _analysis(self, avg_mv=3.0, lands=36, ramp=8, draw=10, spot=6, mass=2):
        an = features.DeckAnalysis()
        an.total = 100
        an.land_count = lands
        an.nonland_count = 100 - lands
        an.avg_mv = avg_mv
        an.median_mv = avg_mv
        an.curve = {"0": 0, "1": 8, "2": 12, "3": 15, "4": 10, "5": 8,
                    "6": 6, "7+": 5}
        an.type_counts = {"Land": lands, "Creature": 30, "Instant": 10,
                          "Sorcery": 10}
        an.role_counts = {r.key: 0 for r in roles.ROLES}
        an.role_counts.update({"ramp": ramp, "card_draw": draw,
                               "removal_spot": spot, "removal_mass": mass})
        an.vector = features.build_vector(an)
        return an

    def test_land_target_moves_with_the_curve(self):
        low = advice.recommended_lands(self._analysis(avg_mv=2.2))
        high = advice.recommended_lands(self._analysis(avg_mv=4.5))
        self.assertLess(low, high)

    def test_missing_draw_is_flagged_high(self):
        an = self._analysis(draw=3)
        recs = advice.fundamentals(an)
        match = [r for r in recs if "card draw" in r.title.lower()]
        self.assertTrue(match)
        self.assertEqual(match[0].priority, advice.HIGH)

    def test_missing_interaction_is_flagged(self):
        an = self._analysis(spot=1, mass=0)
        titles = [r.title for r in advice.fundamentals(an)]
        self.assertIn("Add more interaction", titles)

    def test_healthy_deck_gets_no_draw_or_interaction_warning(self):
        an = self._analysis(draw=10, spot=8, mass=3)
        titles = [r.title for r in advice.fundamentals(an)]
        self.assertNotIn("Add more card draw", titles)
        self.assertNotIn("Add more interaction", titles)

    def test_direction_never_suggests_cutting_the_decks_own_identity(self):
        arch = archetypes.ARCHETYPES_BY_KEY["aristocrats"]
        an = self._analysis()
        an.vector = dict(arch.profile)
        # Twice as many sacrifice payoffs as the archetype calls for.
        an.vector["sacrifice"] = arch.profile["sacrifice"] * 2
        result = classify.classify(an.vector)
        titles = [r.title for r in advice.direction(an, result)]
        self.assertNotIn("Trim sacrifice pieces", titles)

    def test_direction_only_argues_about_declared_features(self):
        # Lands Matter says nothing about Equipment, so a lands deck with no
        # Equipment must not be told to add any.
        arch = archetypes.ARCHETYPES_BY_KEY["lands_matter"]
        an = self._analysis()
        an.vector = dict(arch.profile)
        an.vector["equipment_auras"] = 0.0
        result = classify.classify(an.vector)
        self.assertFalse(any("Equipment" in r.title
                             for r in advice.direction(an, result)))

    def test_target_mode_measures_against_the_stated_goal(self):
        # A deck that has drifted off Voltron should still be told what
        # Voltron wants when Voltron is named as the goal.
        voltron = archetypes.ARCHETYPES_BY_KEY["voltron"]
        an = self._analysis()
        an.vector = dict(voltron.profile)
        an.vector["equipment_auras"] = 0.0     # gut the archetype's core
        result = classify.classify(an.vector)
        self.assertNotEqual(result.best.archetype.key, "voltron")

        recs = advice.all_recommendations(an, result, target_archetype=voltron)
        titles = [r.title for r in recs]
        self.assertTrue(any("Equipment" in t for t in titles), titles)
        self.assertTrue(any("does not read as Voltron" in t for t in titles),
                        titles)

    def test_commander_roles_are_protected_from_trim_advice(self):
        # Ashling makes Elemental tokens, so "trim token makers" is advice
        # against the deck's own commander.
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Token Boss", type_line="Legendary Creature — Elemental",
                 text="Whenever you sacrifice a nontoken Elemental, create a "
                      "token that's a copy of it."),
        ]
        cards_list += [card("Maker %d" % i, type_line="Creature — Elemental",
                            text="Create a 1/1 Elemental creature token.")
                       for i in range(20)]
        text = ("// Commander\n1 Token Boss\n\n// Deck\n"
                + "\n".join("1 Maker %d" % i for i in range(20))
                + "\n36 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertIn("tokens", an.commanders[0].roles)
        result = classify.classify(an.vector)
        titles = [r.title for r in advice.direction(an, result)]
        self.assertNotIn("Trim token makers", titles)

    def test_split_plan_note_needs_a_real_second_plan(self):
        # A deck that is 60/10 has a clear plan even if entropy over a dozen
        # archetypes reads low; it must not be told it is split.
        an = self._analysis()
        an.vector = dict(archetypes.ARCHETYPES_BY_KEY["voltron"].profile)
        result = classify.classify(an.vector)
        notes = advice.focus_note(an, result)
        self.assertFalse(any("split between two plans" in r.title
                             for r in notes))

    def test_recommendations_are_deduplicated_and_sorted(self):
        an = self._analysis(draw=2, spot=0, mass=0, lands=30, avg_mv=4.6)
        result = classify.classify(an.vector)
        recs = advice.all_recommendations(an, result)
        titles = [r.title for r in recs]
        self.assertEqual(len(titles), len(set(titles)))
        priorities = [advice._PRIORITY_ORDER[r.priority] for r in recs]
        self.assertEqual(priorities, sorted(priorities))


class TestBlendGating(unittest.TestCase):
    def test_a_focused_deck_blends_only_itself(self):
        # Voltron's runner-up is a poor match, so it must not dilute the target.
        result = classify.classify(
            dict(archetypes.ARCHETYPES_BY_KEY["voltron"].profile))
        kept = classify.blend_matches(result, 2)
        self.assertEqual([m.archetype.key for m in kept], ["voltron"])
        target = classify.blended_target(result, 2)
        self.assertEqual(target,
                         archetypes.ARCHETYPES_BY_KEY["voltron"].profile)

    def test_a_genuine_hybrid_keeps_both_halves(self):
        control = archetypes.ARCHETYPES_BY_KEY["control"].profile
        spells = archetypes.ARCHETYPES_BY_KEY["spellslinger"].profile
        midpoint = {k: (control[k] + spells[k]) / 2.0 for k in control}
        result = classify.classify(midpoint)
        kept = classify.blend_matches(result, 2)
        self.assertEqual(len(kept), 2)
        self.assertEqual(sorted(m.archetype.key for m in kept),
                         ["control", "spellslinger"])

    def test_runner_up_must_clear_both_floors(self):
        result = classify.classify(
            dict(archetypes.ARCHETYPES_BY_KEY["voltron"].profile))
        second = result.matches[1]
        self.assertTrue(second.affinity < classify.BLEND_MIN_AFFINITY
                        or second.fit < classify.BLEND_MIN_FIT)


class TestCutCandidates(unittest.TestCase):
    def _deck(self, extra_cards, extra_lines, commander="Big Mana Boss"):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card(commander, type_line="Legendary Creature — Human"),
        ] + extra_cards
        text = ("// Commander\n1 %s\n\n// Deck\n%s36 Plains\n"
                % (commander, extra_lines))
        parsed, cards, tags = build(cards_list, text)
        return features.analyze(parsed, cards, tags)

    def test_a_well_ranked_on_plan_card_outscores_unplayed_filler(self):
        # The guarantee is no longer a hard gate but a ranking: a card that is
        # on plan and widely played must rank above one that is neither.
        payoff = card("Tribal Payoff", cost="{5}{G}{G}", mv=7.0,
                      type_line="Instant")
        payoff["edhrec_rank"] = 2600
        filler = card("Nobody Plays This", cost="{5}{G}", mv=6.0,
                      type_line="Creature — Bear")
        filler["edhrec_rank"] = 12000
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Tribe Boss", type_line="Legendary Creature — Elemental"),
            payoff, filler,
        ]
        text = ("// Commander\n1 Tribe Boss\n\n// Deck\n1 Tribal Payoff\n"
                "1 Nobody Plays This\n36 Plains\n")
        tags = {"oid-tribal-payoff": ["typal", "typal-creature"]}
        parsed, cards, _ = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        result = classify.classify(an.vector, themes=an.themes)
        cuts = [c.name for c in advice.cut_candidates(an, result, limit=99)]
        self.assertIn("Nobody Plays This", cuts)
        if "Tribal Payoff" in cuts:
            self.assertGreater(cuts.index("Tribal Payoff"),
                               cuts.index("Nobody Plays This"))

    def test_rank_drives_the_quality_term(self):
        self.assertGreater(advice._quality_bonus(200), advice._quality_bonus(2000))
        self.assertGreater(advice._quality_bonus(2000), advice._quality_bonus(12000))
        self.assertLess(advice._quality_bonus(None), 0.0)

    def test_oversupplied_universal_cards_are_redundant_not_off_plan(self):
        an = self._deck([], "")
        an.role_counts["ramp"] = 30
        an.vector = features.build_vector(an)
        result = classify.classify(an.vector)
        for candidate in advice.cut_candidates(an, result, limit=99):
            if "ramp" in candidate.roles:
                self.assertEqual(candidate.tier, "redundant")

    def test_a_tribe_member_with_no_abilities_is_not_dead_weight(self):
        cards_list = [card("Elemental %d" % i,
                           type_line="Creature — Elemental") for i in range(20)]
        cards_list += [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Tribe Boss", type_line="Legendary Creature — Elemental"),
        ]
        text = ("// Commander\n1 Tribe Boss\n\n// Deck\n"
                + "\n".join("1 Elemental %d" % i for i in range(20))
                + "\n36 Plains\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        result = classify.classify(an.vector)
        if result.best.archetype.key == "typal":
            dead = [c.name for c in advice.cut_candidates(an, result, limit=99)
                    if c.tier == "dead"]
            self.assertEqual(dead, [])

    def test_vanilla_fatty_is_the_first_cut(self):
        an = self._deck(
            [card("Big Dumb Lizard", cost="{5}{G}", mv=6.0,
                  type_line="Creature — Dinosaur"),
             card("Useful Thing", cost="{1}{G}", mv=2.0, type_line="Sorcery",
                  text="Draw two cards.")],
            "1 Big Dumb Lizard\n1 Useful Thing\n")
        result = classify.classify(an.vector)
        cuts = advice.cut_candidates(an, result)
        self.assertTrue(cuts)
        self.assertEqual(cuts[0].name, "Big Dumb Lizard")
        self.assertIn("nothing", cuts[0].reason)

    def test_universally_useful_cards_are_never_cut_candidates(self):
        # A ramp rock is off-theme for almost every archetype, and must still
        # never be proposed as a cut.
        an = self._deck(
            [card("Sol Ring", cost="{1}", mv=1.0, type_line="Artifact",
                  text="{T}: Add {C}{C}."),
             card("Big Dumb Lizard", cost="{5}{G}", mv=6.0,
                  type_line="Creature — Dinosaur")],
            "1 Sol Ring\n1 Big Dumb Lizard\n")
        result = classify.classify(an.vector)
        names = [c.name for c in advice.cut_candidates(an, result)]
        self.assertIn("Big Dumb Lizard", names)
        self.assertNotIn("Sol Ring", names)

    def test_the_commander_is_never_a_cut_candidate(self):
        an = self._deck([card("Filler", type_line="Creature — Bear")],
                        "1 Filler\n")
        result = classify.classify(an.vector)
        names = [c.name for c in advice.cut_candidates(an, result)]
        self.assertNotIn("Big Mana Boss", names)

    def test_a_finisher_counts_toward_a_plan_that_wants_a_top_end(self):
        # "top_end_share" is not a role, so without shape credit the very card
        # a Big Mana deck ramps into looks like it contributes nothing.
        big_mana = archetypes.ARCHETYPES_BY_KEY["big_mana"]
        an = self._deck(
            [card("Huge Finisher", cost="{6}{G}{G}", mv=8.0,
                  type_line="Creature — Avatar",
                  text="Creatures you control get +2/+2 and gain trample.")],
            "1 Huge Finisher\n")
        result = classify.classify(an.vector)
        wanted = advice.plan_roles(result, target_archetype=big_mana)
        entry = next(e for e in an.entries if e.name == "Huge Finisher")
        self.assertGreater(advice._shape_credit(entry, an, wanted), 0.0)

    def test_swap_budget_counts_only_additions(self):
        recs = [
            advice.Recommendation(advice.MEDIUM, "direction", "Add", "", "", 4),
            advice.Recommendation(advice.MEDIUM, "direction", "Trim", "", "", -3),
            advice.Recommendation(advice.LOW, "focus", "Note", ""),
        ]
        self.assertEqual(advice.swap_budget(recs), 4)


class TestSplitCardIdentifiers(unittest.TestCase):
    def test_split_names_are_sent_as_their_front_face(self):
        # Scryfall's collection endpoint rejects the combined "A // B" name
        # but accepts either face on its own.
        self.assertEqual(scryfall.card_identifier("Fire // Ice"),
                         {"name": "Fire"})
        self.assertEqual(scryfall.card_identifier("Dusk // Dawn"),
                         {"name": "Dusk"})

    def test_single_slash_exports_are_handled(self):
        # Some exports write "Dusk / Dawn" rather than "Dusk // Dawn".
        self.assertEqual(scryfall.card_identifier("Dusk / Dawn"),
                         {"name": "Dusk"})

    def test_ordinary_names_are_unchanged(self):
        self.assertEqual(scryfall.card_identifier("Sol Ring"),
                         {"name": "Sol Ring"})

    def test_slashes_inside_a_real_name_are_left_alone(self):
        # Unglued's "Who/What/When/Where/Why" is one card, not five faces.
        self.assertEqual(scryfall.card_identifier("Who/What/When/Where/Why"),
                         {"name": "Who/What/When/Where/Why"})

    def test_split_names_parse_out_of_a_decklist_line(self):
        deck = decklist.parse_decklist("1 Dusk // Dawn (AKH) 210 *F*\n")
        self.assertEqual(deck.entries[0].name, "Dusk // Dawn")


class TestPartnerCommanders(unittest.TestCase):
    def _legality(self, first, second, first_text="", second_text="",
                  second_type="Legendary Creature — Human"):
        cards_list = [
            card("Island", cost="", mv=0.0, type_line="Basic Land — Island"),
            card(first, type_line="Legendary Creature — Human",
                 text=first_text),
            card(second, type_line=second_type, text=second_text),
        ]
        text = ("// Commander\n1 %s\n1 %s\n\n// Deck\n1 Island\n"
                % (first, second))
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(len(an.commanders), 2)
        return [i for i in an.legality if "paired" in i or "commanders" in i]

    def test_two_partners_are_legal(self):
        self.assertEqual(
            self._legality("Alice", "Bob", "Partner (You can have two "
                           "commanders if both have partner.)",
                           "Partner (You can have two commanders if both have "
                           "partner.)"),
            [])

    def test_two_unrelated_legends_are_flagged(self):
        issues = self._legality("Alice", "Bob", "Flying.", "Vigilance.")
        self.assertTrue(issues)
        self.assertIn("cannot be paired", issues[0])

    def test_partner_with_names_the_other_card(self):
        self.assertEqual(
            self._legality("Alice, the First", "Bob, the Second",
                           "Partner with Bob, the Second", "Flying."),
            [])

    def test_background_pairing(self):
        self.assertEqual(
            self._legality("Alice", "Noble Heritage", "Choose a Background",
                           "Commander creatures you own have vigilance.",
                           second_type="Legendary Enchantment — Background"),
            [])

    def test_both_commanders_contribute_colour_identity(self):
        cards_list = [
            card("Island", cost="", mv=0.0, type_line="Basic Land — Island"),
            card("Blue Legend", cost="{U}", mv=1.0,
                 type_line="Legendary Creature — Human", colors=["U"],
                 text="Partner"),
            card("White Legend", cost="{W}", mv=1.0,
                 type_line="Legendary Creature — Human", colors=["W"],
                 text="Partner"),
        ]
        text = ("// Commander\n1 Blue Legend\n1 White Legend\n\n"
                "// Deck\n1 Island\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(an.commander_identity, ["W", "U"])

    def test_commander_notes_name_the_commander_they_came_from(self):
        cards_list = [
            card("Island", cost="", mv=0.0, type_line="Basic Land — Island"),
            card("Discount Lord", type_line="Legendary Creature — Human",
                 text="Partner. Creature spells you cast cost {2} less to cast."),
            card("Plain Partner", type_line="Legendary Creature — Human",
                 text="Partner"),
        ]
        text = ("// Commander\n1 Discount Lord\n1 Plain Partner\n\n"
                "// Deck\n1 Island\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual(len(an.commander_notes), 1)
        self.assertTrue(an.commander_notes[0].startswith("Discount Lord"))


class TestPartnerInference(unittest.TestCase):
    def _cards(self):
        return [
            card("Island", cost="", mv=0.0, type_line="Basic Land — Island",
                 produces=["U"]),
            card("Haldan", cost="{1}{U}", mv=2.0,
                 type_line="Legendary Creature — Human",
                 text="Partner with Pako, Arcane Retriever", colors=["U"]),
            card("Pako, Arcane Retriever", cost="{2}{R}{G}", mv=4.0,
                 type_line="Legendary Creature — Dog",
                 text="Partner with Haldan", colors=["R", "G"]),
            card("Red Spell", cost="{R}", mv=1.0, type_line="Instant",
                 colors=["R"]),
        ]

    def test_a_partner_separated_by_a_blank_line_is_promoted(self):
        # One real export puts the two partners in separate blocks, so only
        # the first is detected and the rest of the deck reads as off-colour.
        text = ("// Commander\n1 Haldan\n\n// Deck\n"
                "1 Pako, Arcane Retriever\n1 Red Spell\n1 Island\n")
        parsed, cards, tags = build(self._cards(), text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual([c.name for c in an.commanders],
                         ["Haldan", "Pako, Arcane Retriever"])
        self.assertEqual(an.commander_identity, ["U", "R", "G"])
        # The Red Spell is only legal because Pako reached the command zone.
        self.assertFalse([i for i in an.legality if "colour identity" in i])
        self.assertTrue(any("second commander" in w for w in an.warnings))

    def test_a_lone_commander_without_partner_is_left_alone(self):
        cards_list = [
            card("Island", cost="", mv=0.0, type_line="Basic Land — Island"),
            card("Solo Boss", type_line="Legendary Creature — Human",
                 text="Flying."),
            card("Other Legend", type_line="Legendary Creature — Human",
                 text="Vigilance."),
        ]
        text = ("// Commander\n1 Solo Boss\n\n// Deck\n"
                "1 Other Legend\n1 Island\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual([c.name for c in an.commanders], ["Solo Boss"])

    def test_no_promotion_when_colours_do_not_call_for_it(self):
        # Two mono-blue partners: nothing in the deck needs the second one, so
        # respect what the list actually said.
        cards_list = [
            card("Island", cost="", mv=0.0, type_line="Basic Land — Island"),
            card("Blue One", cost="{U}", mv=1.0,
                 type_line="Legendary Creature — Human", text="Partner",
                 colors=["U"]),
            card("Blue Two", cost="{U}", mv=1.0,
                 type_line="Legendary Creature — Human", text="Partner",
                 colors=["U"]),
        ]
        text = ("// Commander\n1 Blue One\n\n// Deck\n1 Blue Two\n1 Island\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertEqual([c.name for c in an.commanders], ["Blue One"])


class TestHighCurveCommanders(unittest.TestCase):
    def test_a_commander_that_pays_for_expensive_cards_keeps_its_curve(self):
        cards_list = [
            card("Mountain", cost="", mv=0.0, type_line="Basic Land — Mountain"),
            card("Bello", cost="{1}{R}{G}", mv=3.0,
                 type_line="Legendary Creature — Raccoon Bard",
                 text="Non-Creature artifacts and enchantments you control "
                      "with mana value 4 or greater are Creatures."),
        ]
        cards_list += [card("Big Thing %d" % i, cost="{5}{R}", mv=6.0,
                            type_line="Artifact") for i in range(20)]
        text = ("// Commander\n1 Bello\n\n// Deck\n"
                + "\n".join("1 Big Thing %d" % i for i in range(20))
                + "\n36 Mountain\n")
        parsed, cards, tags = build(cards_list, text)
        an = features.analyze(parsed, cards, tags)
        self.assertTrue(an.commander_wants_high_curve)
        self.assertGreater(an.avg_mv, 4.0)
        titles = [r.title for r in advice.fundamentals(an)]
        self.assertNotIn("Lower your curve", titles)
        self.assertNotIn("Add more early plays", titles)


class TestSynergyThemes(unittest.TestCase):
    """Themes are discovered from tag concentration, not from a fixed list."""

    def _universe(self, rare_carriers=2, common_carriers=800):
        # 1000 cards in "Magic": a rare tag on a couple, a common one on most.
        index = {}
        for i in range(1000):
            labels = ["filler"]
            if i < rare_carriers:
                labels.append("x cost matters")
            if i < common_carriers:
                labels.append("everywhere")
            index["oid-universe-%d" % i] = labels
        return index

    def _deck(self, deck_tags):
        cards_list = [card("Plains", cost="", mv=0.0,
                           type_line="Basic Land — Plains"),
                      card("Boss", type_line="Legendary Creature — Human")]
        lines = ""
        tags = {}
        for i, labels in enumerate(deck_tags):
            name = "Spell %d" % i
            cards_list.append(card(name, cost="{2}", mv=2.0, type_line="Sorcery"))
            tags["oid-spell-%d" % i] = labels
            lines += "1 %s\n" % name
        text = "// Commander\n1 Boss\n\n// Deck\n%s36 Plains\n" % lines
        parsed, cards, _ = build(cards_list, text)
        return parsed, cards, tags

    def test_a_concentrated_tag_becomes_a_theme(self):
        parsed, cards, deck_tags = self._deck(
            [["x cost matters"]] * 6 + [["everywhere"]] * 6)
        index = self._universe()
        index.update(deck_tags)
        an = features.analyze(parsed, cards, index)
        themes = synergy.deck_themes(an, index)
        labels = [t.label for t in themes]
        self.assertIn("x cost matters", labels)
        # A tag most of Magic carries is not what this deck is "about".
        self.assertNotIn("everywhere", labels)
        self.assertEqual(themes[0].label, "x cost matters")
        self.assertGreater(themes[0].lift, 50)

    def test_cosmetic_tags_are_never_themes(self):
        parsed, cards, deck_tags = self._deck([["cycle-xyz-something"]] * 6)
        index = {"oid-universe-0": ["cycle-xyz-something"]}
        index.update(deck_tags)
        an = features.analyze(parsed, cards, index)
        themes = synergy.deck_themes(an, index,
                                     cosmetic={"cycle-xyz-something"})
        self.assertEqual(themes, [])

    def test_a_tag_on_one_card_is_a_coincidence_not_a_theme(self):
        parsed, cards, deck_tags = self._deck([["x cost matters"]]
                                              + [["filler"]] * 8)
        index = self._universe()
        index.update(deck_tags)
        an = features.analyze(parsed, cards, index)
        themes = synergy.deck_themes(an, index)
        self.assertNotIn("x cost matters", [t.label for t in themes])

    def test_card_synergy_reflects_the_themes_a_card_carries(self):
        parsed, cards, deck_tags = self._deck([["x cost matters"]] * 6
                                              + [["unrelated-thing"]])
        index = self._universe()
        index.update(deck_tags)
        an = features.analyze(parsed, cards, index)
        themes = synergy.deck_themes(an, index)
        on_theme = next(e for e in an.entries if e.name == "Spell 0")
        off_theme = next(e for e in an.entries if e.name == "Spell 6")
        self.assertGreater(synergy.card_synergy(on_theme, themes), 0.9)
        self.assertEqual(synergy.card_synergy(off_theme, themes), 0.0)

    def test_a_roleless_card_carrying_the_theme_is_not_a_cut(self):
        # The Unbound Flourishing case: no role in the fixed vocabulary, but
        # it is what the deck is built on.
        parsed, cards, deck_tags = self._deck([["x cost matters"]] * 8)
        index = self._universe()
        index.update(deck_tags)
        an = features.analyze(parsed, cards, index)
        payoff = next(e for e in an.entries if e.name == "Spell 0")
        self.assertEqual(payoff.roles, set())        # invisible to roles
        self.assertGreater(payoff.synergy, 0.9)      # visible to synergy
        result = classify.classify(an.vector)
        names = [c.name for c in advice.cut_candidates(an, result, limit=99)]
        self.assertNotIn("Spell 0", names)


class TestThemesFeedClassification(unittest.TestCase):
    def test_theme_support_matches_signature_tags(self):
        voltron = archetypes.ARCHETYPES_BY_KEY["voltron"]
        themes = [synergy.Theme("synergy-equipment", 8, 20.0, 1.0)]
        self.assertGreater(classify.theme_support(voltron, themes), 0.9)
        control = archetypes.ARCHETYPES_BY_KEY["control"]
        self.assertEqual(classify.theme_support(control, themes), 0.0)

    def test_prefix_signature_tags_match_a_family(self):
        typal = archetypes.ARCHETYPES_BY_KEY["typal"]
        themes = [synergy.Theme("typal-elemental", 10, 120.0, 1.0)]
        self.assertGreater(classify.theme_support(typal, themes), 0.9)

    def test_themes_pull_a_deck_toward_the_archetype_they_name(self):
        # Themes are evidence, not an override: they move an archetype up the
        # ranking rather than winning outright from any distance.
        aggro = archetypes.ARCHETYPES_BY_KEY["go_wide_aggro"].profile
        control = archetypes.ARCHETYPES_BY_KEY["control"].profile
        midpoint = {k: (aggro[k] + control[k]) / 2.0 for k in aggro}

        def rank_of(result, key):
            return [m.archetype.key for m in result.matches].index(key)

        plain = classify.classify(midpoint)
        themed = classify.classify(
            midpoint, themes=[synergy.Theme("counterspell", 9, 30.0, 1.0)])
        self.assertLessEqual(rank_of(themed, "control"),
                             rank_of(plain, "control"))
        self.assertGreater(
            next(m for m in themed.matches if m.archetype.key == "control").affinity,
            next(m for m in plain.matches if m.archetype.key == "control").affinity)

    def test_themes_only_help_they_never_hurt(self):
        # Theme support shortens a distance; it can never lengthen one.
        profile = dict(archetypes.ARCHETYPES_BY_KEY["voltron"].profile)
        plain = classify.classify(profile)
        themed = classify.classify(
            profile, themes=[synergy.Theme("synergy-equipment", 8, 20.0, 1.0)])
        for a, b in zip(plain.matches, themed.matches):
            if a.archetype.key == b.archetype.key:
                self.assertLessEqual(b.distance, a.distance + 1e-9)


class TestCorpusReplaceability(unittest.TestCase):
    """Comparison-class scoring: how a card stacks up against the alternatives."""

    def _corpus(self):
        # oracle_id -> (rank, mana_value, colour mask, roles, game_changer)
        green = corpus.color_mask(["G"])
        blue = corpus.color_mask(["U"])
        entries = {}
        for i in range(10):
            entries["green-ramp-%d" % i] = (
                100 * (i + 1), 3.0, green, ["ramp"], False)
        for i in range(10):
            entries["blue-ramp-%d" % i] = (
                10 + i, 3.0, blue, ["ramp"], False)
        return corpus.Corpus(entries)

    def test_best_in_class_scores_zero(self):
        index = self._corpus()
        # Rank 1 beats every green ramp spell at this cost.
        self.assertEqual(
            index.better_fraction(["ramp"], 3.0, 1, corpus.color_mask(["G"])), 0.0)

    def test_worst_in_class_scores_near_one(self):
        index = self._corpus()
        fraction = index.better_fraction(["ramp"], 3.0, 99999,
                                         corpus.color_mask(["G"]))
        self.assertGreater(fraction, 0.9)

    def test_peers_outside_the_colour_identity_are_excluded(self):
        index = self._corpus()
        # In mono-green the ten better-ranked blue cards must not count.
        mono = index.better_fraction(["ramp"], 3.0, 500,
                                     corpus.color_mask(["G"]))
        both = index.better_fraction(["ramp"], 3.0, 500,
                                     corpus.color_mask(["G", "U"]))
        self.assertLess(mono, both)

    def test_peers_at_a_very_different_cost_are_excluded(self):
        index = self._corpus()
        self.assertIsNone(
            index.better_fraction(["ramp"], 9.0, 500, corpus.color_mask(["G"])))

    def test_no_comparison_class_returns_none(self):
        index = self._corpus()
        self.assertIsNone(index.better_fraction([], 3.0, 500, 31))
        self.assertIsNone(index.better_fraction(["ramp"], 3.0, None, 31))
        self.assertIsNone(
            index.better_fraction(["counterspell"], 3.0, 500, 31))

    def test_annotate_scores_spells_and_skips_lands(self):
        cards_list = [
            card("Forest", cost="", mv=0.0, type_line="Basic Land — Forest"),
            card("Boss", type_line="Legendary Creature — Human"),
            card("Slow Ramp", cost="{2}{G}", mv=3.0, type_line="Sorcery",
                 text="Search your library for a basic land card.",
                 colors=["G"]),
        ]
        cards_list[2]["edhrec_rank"] = 900
        parsed, cards, tags = build(
            cards_list, "// Commander\n1 Boss\n\n// Deck\n1 Slow Ramp\n36 Forest\n")
        an = features.analyze(parsed, cards, tags)
        corpus.annotate(an, self._corpus())
        ramp = next(e for e in an.entries if e.name == "Slow Ramp")
        forest = next(e for e in an.entries if e.name == "Forest")
        self.assertIsNotNone(ramp.replaceability)
        self.assertIsNone(forest.replaceability)

    def test_a_missing_corpus_is_survivable(self):
        parsed, cards, tags = build(
            [card("Forest", cost="", mv=0.0, type_line="Basic Land — Forest")],
            "1 Forest\n")
        an = features.analyze(parsed, cards, tags)
        corpus.annotate(an, None)      # must not raise
        self.assertTrue(all(e.replaceability is None for e in an.entries))


class TestGameChangers(unittest.TestCase):
    """Game Changers raise a deck's power bracket, which casual tables may not
    want. They are a caution, never a reason to keep a card."""

    def _deck(self):
        cards_list = [
            card("Plains", cost="", mv=0.0, type_line="Basic Land — Plains"),
            card("Boss", type_line="Legendary Creature — Human"),
            # Deliberately fills no universal role, so the only thing that
            # could protect it is its Game Changer status.
            card("Bracket Pusher", cost="{5}{U}", mv=6.0,
                 type_line="Creature — Avatar", colors=["U"]),
        ]
        cards_list[2]["game_changer"] = True
        cards_list[2]["edhrec_rank"] = 20000
        parsed, cards, tags = build(
            cards_list,
            "// Commander\n1 Boss\n\n// Deck\n1 Bracket Pusher\n36 Plains\n")
        return features.analyze(parsed, cards, tags)

    def test_game_changers_raise_a_caution(self):
        an = self._deck()
        result = classify.classify(an.vector, themes=an.themes)
        recs = advice.all_recommendations(an, result)
        bracket = [r for r in recs if r.kind == "bracket"]
        self.assertTrue(bracket)
        self.assertIn("Bracket Pusher", bracket[0].detail)

    def test_a_game_changer_is_not_protected_from_being_cut(self):
        # Being format-warping is not a reason to keep a card in a casual
        # deck, so it must not act like staple protection.
        an = self._deck()
        entry = next(e for e in an.entries if e.name == "Bracket Pusher")
        self.assertTrue(entry.game_changer)
        result = classify.classify(an.vector, themes=an.themes)
        names = [c.name for c in advice.cut_candidates(an, result, limit=99)]
        self.assertIn("Bracket Pusher", names)


class TestEndToEnd(unittest.TestCase):
    """Runs only when the Scryfall cache has already been populated."""

    DECKS = {
        "adeline_tokens_aggro.txt": "go_wide_aggro",
        "ephara_control.txt": "control",
        "sram_voltron.txt": "voltron",
        "hearthhull_lands.txt": "lands_matter",
        "dance_of_the_elementals.txt": "typal",
        "counter_blitz.txt": "counters",
        "thrasios_tymna_partners.txt": "control",
        "peace_offering.txt": "group_hug",
        "prismari_artistry.txt": "spellslinger",
        "quandrix_unlimited.txt": "counters",
    }

    def test_sample_decks_classify_as_intended(self):
        from mtgcoach import scryfall
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for filename, expected in self.DECKS.items():
            path = os.path.join(root, "decks", filename)
            parsed = decklist.load_decklist(path)
            try:
                cards, missing = scryfall.fetch_cards(parsed.unique_names(),
                                                      offline=True)
                tags = scryfall.oracle_tag_index(offline=True)
            except scryfall.OfflineError:
                self.skipTest("Scryfall cache not populated; run an analysis "
                              "once with network access")
            self.assertEqual(missing, [], "%s has unknown cards" % filename)
            an = features.analyze(parsed, cards, tags)
            self.assertEqual(an.total, 100, "%s is not 100 cards" % filename)
            self.assertTrue(an.commanders, "%s has no commander" % filename)
            self.assertEqual(an.legality, [], "%s is not legal" % filename)
            result = classify.classify(an.vector, themes=an.themes)
            if filename == "quandrix_unlimited.txt":
                # A payoff the role vocabulary cannot see must survive.
                cuts = [c.name for c in
                        advice.cut_candidates(an, result, limit=99)]
                self.assertNotIn("Unbound Flourishing", cuts)
            self.assertEqual(result.best.archetype.key, expected,
                             "%s classified as %s" % (filename,
                                                      result.best.archetype.key))


if __name__ == "__main__":
    unittest.main()
