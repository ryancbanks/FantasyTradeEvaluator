import math
import unittest

from trade_snapshot.lineup import LineupPlayer, optimize_lineup


class LineupOptimizerTests(unittest.TestCase):
    def test_flex_competition_finds_the_global_optimum(self):
        result = optimize_lineup(
            ("RB", "FLEX"),
            (
                LineupPlayer("rb-only", {"RB": 9.0}),
                LineupPlayer("dual", {"RB": 12.0, "FLEX": 12.0}),
                LineupPlayer("wr", {"FLEX": 11.0}),
            ),
        )

        self.assertEqual(result.total_weight, 23.0)
        self.assertEqual(
            tuple(assignment.player_id for assignment in result.assignments),
            ("dual", "wr"),
        )

    def test_duplicate_slot_names_are_distinct_positions(self):
        result = optimize_lineup(
            ("RB", "RB"),
            (
                LineupPlayer("first", {"RB": 7}),
                LineupPlayer("second", {"RB": 10}),
                LineupPlayer("bench", {"RB": 4}),
            ),
        )

        self.assertEqual(result.total_weight, 17.0)
        self.assertEqual(len(result.assignments), 2)
        self.assertEqual(
            {assignment.player_id for assignment in result.assignments},
            {"first", "second"},
        )
        self.assertEqual(
            tuple(assignment.slot_index for assignment in result.assignments),
            (0, 1),
        )

    def test_weights_can_vary_by_player_and_slot(self):
        result = optimize_lineup(
            ("QB", "SUPERFLEX"),
            (
                LineupPlayer("alpha", {"QB": 20, "SUPERFLEX": 5}),
                LineupPlayer("bravo", {"QB": 19, "SUPERFLEX": 18}),
            ),
        )

        self.assertEqual(result.total_weight, 38.0)
        self.assertEqual(
            tuple(assignment.player_id for assignment in result.assignments),
            ("alpha", "bravo"),
        )
        self.assertEqual(
            tuple(assignment.weight for assignment in result.assignments),
            (20.0, 18.0),
        )

    def test_empty_slots_and_benched_players_are_allowed(self):
        result = optimize_lineup(
            ("QB", "RB", "FLEX"),
            (
                LineupPlayer("qb", {"QB": 16}),
                LineupPlayer("negative-rb", {"RB": -2, "FLEX": -2}),
                LineupPlayer("ineligible", {}),
            ),
        )

        self.assertEqual(result.total_weight, 16.0)
        self.assertEqual(
            tuple(assignment.player_id for assignment in result.assignments),
            ("qb", None, None),
        )
        self.assertEqual(
            tuple(assignment.weight for assignment in result.assignments),
            (16.0, 0.0, 0.0),
        )
        self.assertEqual(optimize_lineup((), ()).total_weight, 0.0)

    def test_ties_use_player_input_order_then_slot_order(self):
        players = (
            LineupPlayer("first", {"FLEX": 5}),
            LineupPlayer("second", {"FLEX": 5}),
        )

        first_run = optimize_lineup(("FLEX", "FLEX"), players)
        second_run = optimize_lineup(("FLEX", "FLEX"), players)

        self.assertEqual(first_run, second_run)
        self.assertEqual(
            tuple(assignment.player_id for assignment in first_run.assignments),
            ("first", "second"),
        )

    def test_rejects_duplicate_or_unhashable_player_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate player_id"):
            optimize_lineup(
                ("QB",),
                (LineupPlayer("same", {"QB": 1}), LineupPlayer("same", {"QB": 2})),
            )

        with self.assertRaisesRegex(ValueError, "hashable"):
            LineupPlayer(["not", "hashable"], {"QB": 1})

    def test_rejects_bad_slots_players_and_nonfinite_weights(self):
        for bad_slot in ("", 1, None):
            with self.subTest(slot=bad_slot):
                with self.assertRaisesRegex(ValueError, "slot names"):
                    optimize_lineup((bad_slot,), ())

        for bad_weight in (math.inf, -math.inf, math.nan, True, "10"):
            with self.subTest(weight=bad_weight):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    LineupPlayer("player", {"QB": bad_weight})

        with self.assertRaisesRegex(ValueError, "finite number"):
            LineupPlayer("player", {"QB": 10**10000})

        with self.assertRaisesRegex(ValueError, "LineupPlayer"):
            optimize_lineup(("QB",), ("not-a-player",))

        with self.assertRaisesRegex(ValueError, "mapping"):
            LineupPlayer("player", (("QB", 10),))


if __name__ == "__main__":
    unittest.main()
