from itertools import product
import unittest

from trade_snapshot.positions import normalize_player_position
from trade_snapshot.three_way_trade import (
    ThreeWayTradeCandidate,
    ThreeWayTradeSpace,
    TradeTransfer,
)
from trade_snapshot.trade_filters import (
    TradeFilterExpression,
    TradeFilterMode,
    TradeFilterOperator,
    TradePackageFilter,
)
from trade_snapshot.trade_space import TeamRoster, TradeConstraints


def roster(team_id, player_ids, *, cap=None, exempt=()):
    players = tuple(player_ids)
    capacity = len(players) if cap is None else cap
    return TeamRoster(team_id, players, len(players), capacity, frozenset(exempt))


def signature(candidate):
    return tuple(
        (leg.source_team_id, leg.destination_team_id, leg.player_ids)
        for leg in candidate.transfers
    )


def brute_force(rosters, constraints, positions=None):
    """Small independent oracle over stay/partner/partner player decisions."""

    rows = (rosters[0], *sorted(rosters[1:], key=lambda row: row.team_id))
    players = tuple(
        (team_index, player_id)
        for team_index, row in enumerate(rows)
        for player_id in row.player_ids
    )
    choices = tuple(
        (origin, *(index for index in range(3) if index != origin))
        for origin, _ in players
    )
    results = []
    for destinations in product(*choices):
        outgoing = [[] for _ in rows]
        incoming = [[] for _ in rows]
        active_outgoing = [0, 0, 0]
        routes = {}
        invalid = False
        for (origin, player_id), destination in zip(players, destinations):
            if destination == origin:
                continue
            if player_id in constraints.locked_player_ids:
                invalid = True
                break
            outgoing[origin].append(player_id)
            incoming[destination].append(player_id)
            active_outgoing[origin] += int(
                player_id not in rows[origin].capacity_exempt_player_ids
            )
            routes.setdefault((origin, destination), []).append(player_id)
        if invalid:
            continue
        out_sizes = tuple(map(len, outgoing))
        in_sizes = tuple(map(len, incoming))
        if any(
            not constraints.min_outgoing <= size <= constraints.max_outgoing
            for size in out_sizes
        ) or any(
            not constraints.min_incoming <= size <= constraints.max_incoming
            for size in in_sizes
        ):
            continue
        if (
            constraints.max_total_players is not None
            and sum(out_sizes) > constraints.max_total_players
        ):
            continue
        if constraints.balanced_only and out_sizes != in_sizes:
            continue
        if constraints.max_imbalance is not None and any(
            abs(sent - received) > constraints.max_imbalance
            for sent, received in zip(out_sizes, in_sizes)
        ):
            continue
        if constraints.require_no_drops and any(
            row.active_size - sent_active + received > row.roster_cap
            for row, sent_active, received in zip(
                rows, active_outgoing, in_sizes
            )
        ):
            continue
        if not package_matches(
            outgoing[0], constraints.outgoing_filter, positions
        ) or not package_matches(
            incoming[0], constraints.incoming_filter, positions
        ):
            continue
        results.append(
            tuple(
                (
                    rows[source].team_id,
                    rows[destination].team_id,
                    tuple(routes[(source, destination)]),
                )
                for source in range(3)
                for destination in range(3)
                if (source, destination) in routes
            )
        )
    return tuple(results)


def package_matches(player_ids, rule, positions):
    if rule is None:
        return True
    if isinstance(rule, TradeFilterExpression):
        matches = tuple(
            package_matches(player_ids, operand, positions)
            for operand in rule.operands
        )
        if rule.operator is TradeFilterOperator.AND:
            return all(matches)
        if rule.operator is TradeFilterOperator.OR:
            return any(matches)
        if rule.operator is TradeFilterOperator.XOR:
            return sum(matches) == 1
        return not matches[0]
    selected = set(player_ids)
    if rule.player_mode is TradeFilterMode.INCLUDE and not rule.player_ids <= selected:
        return False
    if rule.player_mode is TradeFilterMode.ONLY and selected != rule.player_ids:
        return False
    if rule.player_mode is TradeFilterMode.EXCLUDE and selected & rule.player_ids:
        return False
    if rule.position_mode is None:
        return True
    eligible = {
        player_id: {
            normalize_player_position(value)
            for value in positions[player_id]
        }
        for player_id in player_ids
    }
    if rule.position_mode is TradeFilterMode.INCLUDE:
        covered = set().union(*eligible.values()) if eligible else set()
        return rule.positions <= covered
    matches = tuple(
        bool(values & rule.positions) for values in eligible.values()
    )
    if rule.position_mode is TradeFilterMode.ONLY:
        return all(matches)
    return not any(matches)


class TradeTransferTests(unittest.TestCase):
    def test_candidate_canonicalizes_legs_and_exposes_aggregate_packages(self):
        candidate = ThreeWayTradeCandidate(
            ("A", "B", "C"),
            (
                TradeTransfer("C", "A", ("c2", "c1")),
                TradeTransfer("B", "C", ("b1",)),
                TradeTransfer("A", "B", ("a1",)),
            ),
        )

        self.assertEqual(candidate.participant_team_ids, ("A", "B", "C"))
        self.assertEqual(
            tuple((row.source_team_id, row.destination_team_id) for row in candidate.transfers),
            (("A", "B"), ("B", "C"), ("C", "A")),
        )
        self.assertEqual(candidate.outgoing_for("C"), ("c2", "c1"))
        self.assertEqual(candidate.incoming_for("A"), ("c2", "c1"))
        with self.assertRaises(KeyError):
            candidate.incoming_for("missing")

    def test_candidate_rejects_invalid_participants_routes_and_duplicate_players(self):
        valid = (
            TradeTransfer("A", "B", ("a",)),
            TradeTransfer("B", "C", ("b",)),
            TradeTransfer("C", "A", ("c",)),
        )
        with self.assertRaisesRegex(ValueError, "canonical"):
            ThreeWayTradeCandidate(("A", "C", "B"), valid)
        with self.assertRaisesRegex(ValueError, "send and receive"):
            ThreeWayTradeCandidate(
                ("A", "B", "C"),
                (
                    TradeTransfer("A", "B", ("a",)),
                    TradeTransfer("B", "A", ("b",)),
                    TradeTransfer("C", "A", ("c",)),
                ),
            )
        with self.assertRaisesRegex(ValueError, "more than once"):
            ThreeWayTradeCandidate(
                ("A", "B", "C"),
                (*valid, TradeTransfer("A", "C", ("a",))),
            )
        with self.assertRaises(ValueError):
            TradeTransfer("A", "A", ("a",))


class ThreeWayTradeSpaceTests(unittest.TestCase):
    def test_exact_count_and_iteration_match_all_route_brute_force(self):
        rows = (
            roster("A", ("a1", "a2")),
            roster("C", ("c1", "c2")),
            roster("B", ("b1", "b2")),
        )
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
            max_total_players=4,
            max_imbalance=1,
        )
        expected = brute_force(rows, constraints)

        space = ThreeWayTradeSpace(rows, constraints)
        actual = tuple(space)

        self.assertEqual(space.participant_team_ids, ("A", "B", "C"))
        self.assertEqual(space.candidate_count, len(expected))
        self.assertEqual(tuple(map(signature, actual)), expected)
        self.assertEqual(len(set(map(signature, actual))), len(actual))
        by_team = {row.team_id: row for row in space.rosters}
        for candidate in actual:
            for leg in candidate.transfers:
                source_order = by_team[leg.source_team_id].player_ids
                self.assertEqual(
                    leg.player_ids,
                    tuple(player for player in source_order if player in leg.player_ids),
                )
            self.assertEqual(
                candidate.incoming_for("A"),
                tuple(
                    player
                    for source in space.partners
                    for player in source.player_ids
                    if any(
                        transfer.source_team_id == source.team_id
                        and transfer.destination_team_id == "A"
                        and player in transfer.player_ids
                        for transfer in candidate.transfers
                    )
                ),
            )
        self.assertTrue(
            any(
                len(
                    {
                        leg.destination_team_id
                        for leg in row.transfers
                        if leg.source_team_id == "A"
                    }
                )
                == 2
                for row in actual
            ),
            "fully directed routing must include split packages, not only cycles",
        )

    def test_iter_from_seeks_to_every_candidate_boundary(self):
        space = ThreeWayTradeSpace(
            (
                roster("A", ("a1", "a2")),
                roster("B", ("b1", "b2")),
                roster("C", ("c1",)),
            ),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=2,
                min_incoming=1,
                max_incoming=2,
                max_total_players=4,
            ),
        )
        candidates = tuple(space)

        for index in range(len(candidates) + 1):
            with self.subTest(index=index):
                self.assertEqual(tuple(space.iter_from(index)), candidates[index:])
        for invalid in (-1, 1.5, True, len(candidates) + 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                space.iter_from(invalid)

    def test_legacy_enumeration_record_shape_is_unchanged(self):
        space = ThreeWayTradeSpace(
            (
                roster("A", ("a",)),
                roster("B", ("b",)),
                roster("C", ("c",)),
            ),
            TradeConstraints(),
        )

        self.assertEqual(
            space.enumeration_record(),
            {
                "incoming_target": 0,
                "outgoing_target": 0,
                "player_decisions": [
                    {
                        "active": 1,
                        "destinations": [0, 1, 2],
                        "incoming_coverage": 0,
                        "origin": 0,
                        "outgoing_coverage": 0,
                        "player_id": "a",
                    },
                    {
                        "active": 1,
                        "destinations": [1, 0, 2],
                        "incoming_coverage": 0,
                        "origin": 1,
                        "outgoing_coverage": 0,
                        "player_id": "b",
                    },
                    {
                        "active": 1,
                        "destinations": [2, 0, 1],
                        "incoming_coverage": 0,
                        "origin": 2,
                        "outgoing_coverage": 0,
                        "player_id": "c",
                    },
                ],
            },
        )

    def test_primary_filters_apply_to_aggregate_packages_across_both_partners(self):
        rows = (
            roster("A", ("a-rb", "a-wr")),
            roster("B", ("b-rb", "b-wr")),
            roster("C", ("c-rb", "c-wr")),
        )
        positions = {
            "a-rb": {"RB"},
            "a-wr": {"WR"},
            "b-rb": {"RB"},
            "b-wr": {"WR"},
            "c-rb": {"RB"},
            "c-wr": {"WR"},
        }
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
            max_total_players=6,
            outgoing_filter=TradePackageFilter(
                player_ids={"a-rb"},
                player_mode="only",
                positions={"RB"},
                position_mode="only",
            ),
            incoming_filter=TradePackageFilter(
                player_ids={"b-wr"},
                player_mode="include",
                positions={"RB"},
                position_mode="include",
            ),
        )
        expected = brute_force(rows, constraints, positions)

        space = ThreeWayTradeSpace(
            rows,
            constraints,
            eligible_positions_by_player=positions,
        )
        candidates = tuple(space)

        self.assertEqual(tuple(map(signature, candidates)), expected)
        self.assertTrue(candidates)
        self.assertTrue(all(row.outgoing_for("A") == ("a-rb",) for row in candidates))
        self.assertTrue(all("b-wr" in row.incoming_for("A") for row in candidates))
        self.assertTrue(
            any(
                {leg.source_team_id for leg in row.transfers if leg.destination_team_id == "A"}
                == {"B", "C"}
                for row in candidates
            )
        )

    def test_nested_boolean_filters_match_all_route_brute_force(self):
        rows = (
            roster("A", ("a1", "a2")),
            roster("B", ("b1", "b2")),
            roster("C", ("c1", "c2")),
        )
        positions = {
            "a1": {"RB"},
            "a2": {"WR"},
            "b1": {"RB"},
            "b2": {"WR"},
            "c1": {"TE"},
            "c2": {"RB", "WR"},
        }
        includes_a1 = TradePackageFilter(
            player_ids={"a1"}, player_mode="include"
        )
        outgoing_wr = TradePackageFilter(
            positions={"WR"}, position_mode="include"
        )
        outgoing_filter = TradeFilterExpression(
            "and",
            (
                TradeFilterExpression("or", (includes_a1, outgoing_wr)),
                TradeFilterExpression(
                    "not",
                    (
                        TradeFilterExpression(
                            "xor", (includes_a1, outgoing_wr)
                        ),
                    ),
                ),
            ),
        )
        incoming_filter = TradeFilterExpression(
            "xor",
            (
                TradePackageFilter(
                    player_ids={"b1"}, player_mode="include"
                ),
                TradePackageFilter(
                    positions={"WR"}, position_mode="include"
                ),
                TradePackageFilter(
                    player_ids={"c1"}, player_mode="exclude"
                ),
            ),
        )
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
            max_total_players=6,
            outgoing_filter=outgoing_filter,
            incoming_filter=incoming_filter,
        )
        expected = brute_force(rows, constraints, positions)

        space = ThreeWayTradeSpace(
            rows,
            constraints,
            eligible_positions_by_player=positions,
        )
        actual = tuple(space)

        self.assertTrue(expected)
        self.assertEqual(space.candidate_count, len(expected))
        self.assertEqual(tuple(map(signature, actual)), expected)
        enumeration_record = space.enumeration_record()
        self.assertEqual(
            enumeration_record["outgoing_filter_expression"],
            outgoing_filter.to_record(),
        )
        self.assertEqual(
            enumeration_record["incoming_filter_expression"],
            incoming_filter.to_record(),
        )

    def test_wrapping_a_legacy_filter_preserves_three_way_results(self):
        rows = (
            roster("A", ("a1", "a2")),
            roster("B", ("b1", "b2")),
            roster("C", ("c1", "c2")),
        )
        legacy_filter = TradePackageFilter(
            player_ids={"a1"}, player_mode="include"
        )
        base = dict(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
            outgoing_filter=legacy_filter,
        )
        legacy = ThreeWayTradeSpace(rows, TradeConstraints(**base))
        composed = ThreeWayTradeSpace(
            rows,
            TradeConstraints(
                **{
                    **base,
                    "outgoing_filter": TradeFilterExpression(
                        "and", (legacy_filter, legacy_filter)
                    ),
                }
            ),
        )

        self.assertEqual(composed.candidate_count, legacy.candidate_count)
        self.assertEqual(
            tuple(map(signature, composed)), tuple(map(signature, legacy))
        )

    def test_filter_ownership_and_position_evidence_are_validated(self):
        rows = (
            roster("A", ("a",)),
            roster("B", ("b",)),
            roster("C", ("c",)),
        )
        with self.assertRaisesRegex(ValueError, "primary roster"):
            ThreeWayTradeSpace(
                rows,
                TradeConstraints(
                    outgoing_filter=TradePackageFilter(
                        player_ids={"b"}, player_mode="include"
                    )
                ),
            )
        with self.assertRaisesRegex(ValueError, "partner rosters"):
            ThreeWayTradeSpace(
                rows,
                TradeConstraints(
                    incoming_filter=TradePackageFilter(
                        player_ids={"a"}, player_mode="include"
                    )
                ),
            )
        with self.assertRaisesRegex(ValueError, "eligible_positions_by_player"):
            ThreeWayTradeSpace(
                rows,
                TradeConstraints(
                    incoming_filter=TradePackageFilter(
                        positions={"RB"}, position_mode="include"
                    )
                ),
                eligible_positions_by_player={"b": {"RB"}},
            )
        with self.assertRaisesRegex(ValueError, "do not apply"):
            ThreeWayTradeSpace(
                rows,
                TradeConstraints(excluded_size_pairs={(1, 1)}),
            )

    def test_locked_balanced_and_imbalanced_rules_match_brute_force(self):
        rows = (
            roster("A", ("a1", "a2")),
            roster("B", ("b1", "b2")),
            roster("C", ("c1", "c2")),
        )
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
            max_total_players=6,
            max_imbalance=0,
            balanced_only=True,
            locked_player_ids={"a1", "b2"},
        )
        space = ThreeWayTradeSpace(rows, constraints)
        candidates = tuple(space)

        self.assertEqual(tuple(map(signature, candidates)), brute_force(rows, constraints))
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertNotIn("a1", candidate.outgoing_for("A"))
            self.assertNotIn("b2", candidate.outgoing_for("B"))
            self.assertTrue(
                all(
                    len(candidate.outgoing_for(team_id))
                    == len(candidate.incoming_for(team_id))
                    for team_id in candidate.participant_team_ids
                )
            )

    def test_no_drop_uses_active_outgoing_and_treats_incoming_ir_as_active(self):
        rows = (
            roster("A", ("a1", "a2", "a-ir"), cap=2, exempt={"a-ir"}),
            roster("B", ("b1", "b2")),
            roster("C", ("c1", "c2")),
        )
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
            max_total_players=4,
            require_no_drops=True,
        )
        space = ThreeWayTradeSpace(rows, constraints)
        candidates = tuple(space)

        self.assertEqual(tuple(map(signature, candidates)), brute_force(rows, constraints))
        self.assertTrue(candidates)
        self.assertFalse(
            any(candidate.outgoing_for("A") == ("a-ir",) for candidate in candidates)
        )
        for candidate in candidates:
            for row in space.rosters:
                active_sent = sum(
                    player_id not in row.capacity_exempt_player_ids
                    for player_id in candidate.outgoing_for(row.team_id)
                )
                self.assertLessEqual(
                    row.active_size
                    - active_sent
                    + len(candidate.incoming_for(row.team_id)),
                    row.roster_cap,
                )

    def test_count_is_not_limited_to_sqlite_or_javascript_integers(self):
        rows = tuple(
            roster(team, tuple(f"{team}{index}" for index in range(40)))
            for team in ("A", "B", "C")
        )
        space = ThreeWayTradeSpace(
            rows,
            TradeConstraints(
                min_outgoing=6,
                max_outgoing=6,
                min_incoming=6,
                max_incoming=6,
                max_total_players=18,
                balanced_only=True,
            ),
        )

        self.assertGreater(space.candidate_count, (1 << 63) - 1)
        self.assertEqual(len(tuple(space.iter_from(space.candidate_count - 1))), 1)


if __name__ == "__main__":
    unittest.main()
