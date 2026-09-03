import math
import random
import unittest

from trade_snapshot.lineup import LineupPlayer, optimize_lineup


def _prior_dp_lineup(slots, players):
    player_count = len(players)
    empty = (None,) * len(slots)
    states = {0: (empty, 0.0)}

    def tie_key(assignment):
        return tuple(
            player_count if index is None else index for index in assignment
        )

    for player_index, player in enumerate(players):
        next_states = dict(states)
        for occupied, (assignment, _) in states.items():
            considered = set()
            for slot_index, slot in enumerate(slots):
                if occupied & (1 << slot_index) or slot in considered:
                    continue
                considered.add(slot)
                weight = player.slot_weights.get(slot)
                if weight is None or weight < 0:
                    continue
                candidate_assignment = list(assignment)
                candidate_assignment[slot_index] = player_index
                candidate_assignment = tuple(candidate_assignment)
                candidate_total = math.fsum(
                    players[index].slot_weights[candidate_slot]
                    for candidate_slot, index in zip(slots, candidate_assignment)
                    if index is not None
                )
                mask = occupied | (1 << slot_index)
                incumbent = next_states.get(mask)
                if incumbent is None or (
                    candidate_total > incumbent[1]
                    or candidate_total == incumbent[1]
                    and tie_key(candidate_assignment) < tie_key(incumbent[0])
                ):
                    next_states[mask] = candidate_assignment, candidate_total
        states = next_states
    return min(
        states.values(),
        key=lambda row: (-row[1], tie_key(row[0])),
    )


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

    def test_score_and_tie_break_match_exact_slot_order_summation(self):
        slots = ("A", "B", "C")
        players = (
            LineupPlayer("large", {"A": 1e16}),
            LineupPlayer("small-b", {"B": 1.0}),
            LineupPlayer("small-c", {"C": 1.0}),
        )

        result = optimize_lineup(slots, players)

        self.assertEqual(result.total_weight.hex(), math.fsum((1e16, 1.0, 1.0)).hex())
        self.assertEqual(
            tuple(row.player_id for row in result.assignments),
            ("large", "small-b", "small-c"),
        )

    def test_randomized_results_match_the_prior_dp_contract(self):
        generator = random.Random(4217)
        slots = ("A", "B", "C")
        values = (
            0.0,
            math.ulp(0.0),
            0.1,
            1.0,
            math.nextafter(1.0, 2.0),
            1e16,
            float.fromhex("0x1.fffffffffffffp+1020"),
        )
        for case in range(80):
            players = tuple(
                LineupPlayer(
                    f"p{index}",
                    {
                        slot: generator.choice(values)
                        for slot in slots
                        if generator.randrange(4) != 0
                    },
                )
                for index in range(5)
            )
            expected_assignment, expected_total = _prior_dp_lineup(slots, players)

            result = optimize_lineup(slots, players)

            with self.subTest(case=case):
                self.assertEqual(result.total_weight.hex(), expected_total.hex())
                self.assertEqual(
                    tuple(row.player_id for row in result.assignments),
                    tuple(
                        None if index is None else players[index].player_id
                        for index in expected_assignment
                    ),
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

        maximum = float.fromhex("0x1.fffffffffffffp+1023")
        with self.assertRaisesRegex(ValueError, "non-finite total"):
            optimize_lineup(
                ("QB", "RB"),
                (
                    LineupPlayer("maximum-qb", {"QB": maximum}),
                    LineupPlayer("maximum-rb", {"RB": maximum}),
                ),
            )


if __name__ == "__main__":
    unittest.main()
