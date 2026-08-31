"""Unit tests. Everything here runs offline against synthetic card data;
the one integration test skips itself unless the Scryfall cache is populated.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mtgcoach import advice, archetypes, classify, decklist, features, roles
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

    def test_recommendations_are_deduplicated_and_sorted(self):
        an = self._analysis(draw=2, spot=0, mass=0, lands=30, avg_mv=4.6)
        result = classify.classify(an.vector)
        recs = advice.all_recommendations(an, result)
        titles = [r.title for r in recs]
        self.assertEqual(len(titles), len(set(titles)))
        priorities = [advice._PRIORITY_ORDER[r.priority] for r in recs]
        self.assertEqual(priorities, sorted(priorities))


class TestEndToEnd(unittest.TestCase):
    """Runs only when the Scryfall cache has already been populated."""

    DECKS = {
        "adeline_tokens_aggro.txt": "go_wide_aggro",
        "ephara_control.txt": "control",
        "sram_voltron.txt": "voltron",
        "hearthhull_lands.txt": "lands_matter",
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
            self.assertEqual(an.legality, [], "%s is not legal" % filename)
            result = classify.classify(an.vector)
            self.assertEqual(result.best.archetype.key, expected,
                             "%s classified as %s" % (filename,
                                                      result.best.archetype.key))


if __name__ == "__main__":
    unittest.main()
