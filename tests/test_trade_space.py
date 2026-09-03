import inspect
import math
from itertools import combinations
import unittest
from unittest.mock import patch

from trade_snapshot.trade_filters import (
    MAX_TRADE_FILTER_EXPRESSION_DEPTH,
    MAX_TRADE_FILTER_EXPRESSION_NODES,
    TRADE_FILTER_EXPRESSION_SEMANTICS_VERSION,
    TRADE_FILTER_SEMANTICS_VERSION,
    TradeFilterExpression,
    TradeFilterMode,
    TradeFilterOperator,
    TradePackageFilter,
    parse_trade_filter,
)
from trade_snapshot.trade_filter_compiler import CompiledTradeFilter
from trade_snapshot.trade_space import (
    TradeCandidate,
    TeamRoster,
    TradeConstraints,
    TradeSpace,
)


class TradeSpaceTests(unittest.TestCase):
    def test_counts_exactly_without_enumerating_candidates(self):
        primary = TeamRoster(
            team_id="primary",
            player_ids=tuple(f"p{number}" for number in range(30)),
            current_size=30,
            roster_cap=30,
        )
        counterparty = TeamRoster(
            team_id="counterparty",
            player_ids=tuple(f"c{number}" for number in range(30)),
            current_size=30,
            roster_cap=30,
        )
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                min_outgoing=15,
                max_outgoing=15,
                min_incoming=15,
                max_incoming=15,
            ),
        )

        self.assertEqual(space.candidate_count, math.comb(30, 15) ** 2)
        self.assertTrue(inspect.isgenerator(iter(space)))

    def test_iterator_yields_each_owned_package_pair_once(self):
        primary = roster("primary", ("a", "b", "c"))
        counterparty = roster("counterparty", ("x", "y", "z"))
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=2,
                min_incoming=1,
                max_incoming=2,
            ),
        )

        candidates = list(space)

        self.assertEqual(space.candidate_count, 36)
        self.assertEqual(len(candidates), 36)
        self.assertEqual(
            len({(candidate.outgoing_player_ids, candidate.incoming_player_ids) for candidate in candidates}),
            36,
        )
        self.assertTrue(
            all(set(candidate.outgoing_player_ids) <= set(primary.player_ids) for candidate in candidates)
        )
        self.assertTrue(
            all(
                set(candidate.incoming_player_ids) <= set(counterparty.player_ids)
                for candidate in candidates
            )
        )

    def test_iter_from_matches_every_filtered_no_drop_suffix(self):
        primary = TeamRoster(
            "primary", ("p0", "p1", "p-ir"), 3, 2, {"p-ir"}
        )
        counterparty = TeamRoster(
            "counterparty", ("c0", "c1", "c-ir"), 3, 2, {"c-ir"}
        )
        outgoing = TradeFilterExpression(
            "or",
            tuple(
                TradePackageFilter(frozenset({player_id}), "include")
                for player_id in ("p0", "p1")
            ),
        )
        incoming = TradeFilterExpression(
            "or",
            tuple(
                TradePackageFilter(frozenset({player_id}), "include")
                for player_id in ("c0", "c1")
            ),
        )
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=2,
                min_incoming=1,
                max_incoming=2,
                require_no_drops=True,
                outgoing_filter=outgoing,
                incoming_filter=incoming,
            ),
        )
        candidates = tuple(space)

        for index in range(space.candidate_count + 1):
            with self.subTest(index=index):
                self.assertEqual(tuple(space.iter_from(index)), candidates[index:])
        for invalid in (-1, space.candidate_count + 1, 1.5, True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "start_candidate_index"):
                    space.iter_from(invalid)

    def test_iter_from_skips_prior_cartesian_blocks_without_building_candidates(self):
        space = TradeSpace(
            roster("primary", ("a", "b", "c")),
            roster("counterparty", ("x", "y", "z")),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=2,
                min_incoming=1,
                max_incoming=2,
            ),
        )
        expected = tuple(space)[-1]

        with (
            patch(
                "trade_snapshot.trade_space.TradeCandidate", wraps=TradeCandidate
            ) as candidate_type,
            patch.object(
                space._outgoing_pool,
                "iter_packages",
                wraps=space._outgoing_pool.iter_packages,
            ) as outgoing_packages,
            patch.object(
                space._incoming_pool,
                "iter_packages",
                wraps=space._incoming_pool.iter_packages,
            ) as incoming_packages,
        ):
            actual = tuple(space.iter_from(space.candidate_count - 1))

        self.assertEqual(actual, (expected,))
        self.assertEqual(candidate_type.call_count, 1)
        self.assertEqual(outgoing_packages.call_count, 1)
        self.assertEqual(incoming_packages.call_count, 1)

    def test_combines_size_total_imbalance_and_exclusion_filters(self):
        space = TradeSpace(
            roster("primary", tuple(f"p{number}" for number in range(5))),
            roster("counterparty", tuple(f"c{number}" for number in range(5))),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=4,
                min_incoming=1,
                max_incoming=4,
                max_total_players=7,
                max_imbalance=1,
                excluded_size_pairs=frozenset({(1, 1), (2, 2), (3, 3)}),
            ),
        )

        self.assertEqual(space.candidate_count, 400)
        self.assertTrue(
            all(
                len(candidate.outgoing_player_ids) + len(candidate.incoming_player_ids) <= 7
                and abs(
                    len(candidate.outgoing_player_ids) - len(candidate.incoming_player_ids)
                )
                <= 1
                and (
                    len(candidate.outgoing_player_ids),
                    len(candidate.incoming_player_ids),
                )
                not in {(1, 1), (2, 2), (3, 3)}
                for candidate in space
            )
        )

    def test_balanced_only_keeps_equal_package_sizes(self):
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("w", "x", "y", "z")),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=4,
                min_incoming=1,
                max_incoming=4,
                balanced_only=True,
                excluded_size_pairs=frozenset({(1, 1), (2, 2), (3, 3)}),
            ),
        )

        self.assertEqual(space.candidate_count, 1)
        candidate = next(iter(space))
        self.assertEqual(len(candidate.outgoing_player_ids), 4)
        self.assertEqual(len(candidate.incoming_player_ids), 4)

    def test_locked_players_are_never_in_a_package(self):
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("x", "y", "z")),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=1,
                min_incoming=1,
                max_incoming=1,
                locked_player_ids=frozenset({"a", "y"}),
            ),
        )

        candidates = list(space)

        self.assertEqual(space.candidate_count, 6)
        self.assertTrue(
            all(
                "a" not in candidate.outgoing_player_ids
                and "y" not in candidate.incoming_player_ids
                for candidate in candidates
            )
        )

    def test_empty_package_filters_preserve_count_and_iteration_exactly(self):
        primary = roster("primary", ("a", "b", "c"))
        counterparty = roster("counterparty", ("x", "y", "z"))
        package_sizes = dict(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
        )

        baseline = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(**package_sizes),
        )
        filtered = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                outgoing_filter=TradePackageFilter(
                    player_ids={"a"},
                    positions={"RB"},
                ),
                incoming_filter=TradePackageFilter(),
                **package_sizes,
            ),
        )

        self.assertIsNone(filtered.constraints.outgoing_filter)
        self.assertIsNone(filtered.constraints.incoming_filter)
        self.assertEqual(filtered.candidate_count, baseline.candidate_count)
        self.assertEqual(tuple(filtered), tuple(baseline))

    def test_side_specific_player_include_and_exclude_rules_filter_before_pairing(self):
        primary = roster("primary", ("a", "b", "c", "d", "e"))
        counterparty = roster("counterparty", ("w", "x", "y", "z"))
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                min_outgoing=2,
                max_outgoing=3,
                outgoing_filter=TradePackageFilter(
                    player_ids={"a", "c"},
                    player_mode=TradeFilterMode.INCLUDE,
                ),
                incoming_filter=TradePackageFilter(
                    player_ids={"z"},
                    player_mode=TradeFilterMode.EXCLUDE,
                ),
            ),
        )

        candidates = tuple(space)

        self.assertEqual(space.candidate_count, 12)
        self.assertEqual(len(candidates), 12)
        self.assertTrue(
            all(
                {"a", "c"}.issubset(candidate.outgoing_player_ids)
                and "z" not in candidate.incoming_player_ids
                for candidate in candidates
            )
        )
        self.assertEqual(
            candidates[:3],
            (
                candidate(("a", "c"), ("w",)),
                candidate(("a", "c"), ("x",)),
                candidate(("a", "c"), ("y",)),
            ),
        )

    def test_player_only_requires_the_exact_selected_set(self):
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("x", "y")),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=3,
                outgoing_filter=TradePackageFilter(
                    player_ids={"b", "d"},
                    player_mode="only",
                ),
            ),
        )

        self.assertEqual(space.candidate_count, 2)
        self.assertEqual(
            tuple(row.outgoing_player_ids for row in space),
            (("b", "d"), ("b", "d")),
        )

    def test_unknown_required_player_returns_zero_for_each_required_mode(self):
        for mode in (TradeFilterMode.INCLUDE, TradeFilterMode.ONLY):
            with self.subTest(mode=mode.value):
                space = TradeSpace(
                    roster("primary", ("a", "b")),
                    roster("counterparty", ("x",)),
                    TradeConstraints(
                        outgoing_filter=TradePackageFilter(
                            player_ids={"owned-by-another-team"},
                            player_mode=mode,
                            positions={"RB"},
                            position_mode="include",
                        )
                    ),
                )

                self.assertEqual(space.candidate_count, 0)
                self.assertEqual(tuple(space), ())

    def test_player_include_requires_every_selected_player(self):
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("x",)),
            TradeConstraints(
                min_outgoing=2,
                max_outgoing=3,
                outgoing_filter=TradePackageFilter(
                    player_ids={"b", "d"},
                    player_mode="include",
                ),
            ),
        )

        self.assertEqual(
            tuple(row.outgoing_player_ids for row in space),
            (("b", "d"), ("a", "b", "d"), ("b", "c", "d")),
        )

    def test_position_rules_use_multi_position_eligibility(self):
        positions = {
            "a": {"QB"},
            "b": {"RB", "FLEX"},
            "c": {"RB", "WR", "FLEX"},
            "d": {"WR", "FLEX"},
            "x": {"RB"},
            "y": {"WR"},
            "z": {"TE"},
            "w": {"RB", "WR"},
        }
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("x", "y", "z", "w")),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=1,
                min_incoming=1,
                max_incoming=2,
                outgoing_filter=TradePackageFilter(
                    positions={"RB", "WR"},
                    position_mode="include",
                ),
                incoming_filter=TradePackageFilter(
                    positions={"RB"},
                    position_mode="only",
                ),
            ),
            eligible_positions_by_player=positions,
        )

        candidates = tuple(space)

        self.assertEqual(space.candidate_count, 3)
        self.assertEqual(
            candidates,
            (
                candidate(("c",), ("x",)),
                candidate(("c",), ("w",)),
                candidate(("c",), ("x", "w")),
            ),
        )

    def test_player_and_position_dimensions_are_both_required(self):
        positions = {
            "a": {"QB"},
            "b": {"RB"},
            "c": {"RB", "WR"},
            "d": {"WR"},
            "x": {"TE"},
        }
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("x",)),
            TradeConstraints(
                min_outgoing=2,
                max_outgoing=2,
                outgoing_filter=TradePackageFilter(
                    player_ids={"a"},
                    player_mode="include",
                    positions={"RB"},
                    position_mode="include",
                ),
            ),
            eligible_positions_by_player=positions,
        )

        self.assertEqual(
            tuple(row.outgoing_player_ids for row in space),
            (("a", "b"), ("a", "c")),
        )

    def test_position_exclude_rejects_every_matching_player(self):
        positions = {
            "a": {"QB"},
            "b": {"RB"},
            "c": {"RB", "WR"},
            "d": {"WR"},
            "x": {"TE"},
        }
        space = TradeSpace(
            roster("primary", ("a", "b", "c", "d")),
            roster("counterparty", ("x",)),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=2,
                outgoing_filter=TradePackageFilter(
                    positions={"RB"},
                    position_mode="exclude",
                ),
            ),
            eligible_positions_by_player=positions,
        )

        self.assertEqual(
            tuple(row.outgoing_player_ids for row in space),
            (("a",), ("d",), ("a", "d")),
        )

    def test_filtered_count_stays_combinatorial_for_large_rosters(self):
        primary_ids = tuple(f"p{number}" for number in range(30))
        counterparty_ids = tuple(f"c{number}" for number in range(30))
        space = TradeSpace(
            roster("primary", primary_ids),
            roster("counterparty", counterparty_ids),
            TradeConstraints(
                min_outgoing=15,
                max_outgoing=15,
                min_incoming=15,
                max_incoming=15,
                outgoing_filter=TradePackageFilter(
                    player_ids={"p0", "p1", "p2"},
                    player_mode="include",
                ),
                incoming_filter=TradePackageFilter(
                    player_ids={"c0", "c1"},
                    player_mode="exclude",
                ),
            ),
        )

        self.assertEqual(
            space.candidate_count,
            math.comb(27, 12) * math.comb(28, 15),
        )
        self.assertTrue(inspect.isgenerator(iter(space)))

    def test_filtered_no_drop_count_honors_capacity_exempt_players(self):
        primary = TeamRoster(
            "primary",
            ("p-rb", "p-wr", "p-ir"),
            current_size=3,
            roster_cap=2,
            capacity_exempt_player_ids={"p-ir"},
        )
        counterparty = roster("counterparty", ("c-rb", "c-wr"))
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                outgoing_filter=TradePackageFilter(
                    positions={"TE"},
                    position_mode="only",
                ),
                require_no_drops=True,
            ),
            eligible_positions_by_player={
                "p-rb": {"RB"},
                "p-wr": {"WR"},
                "p-ir": {"TE"},
                "c-rb": {"RB"},
                "c-wr": {"WR"},
            },
        )

        self.assertEqual(space.candidate_count, 0)
        self.assertEqual(tuple(space), ())

    def test_position_coverage_count_combines_distinct_players_and_no_drop_rule(self):
        primary = TeamRoster(
            "primary",
            ("p-rb", "p-wr", "p-dual-ir", "p-te"),
            current_size=4,
            roster_cap=3,
            capacity_exempt_player_ids={"p-dual-ir"},
        )
        counterparty = roster("counterparty", ("c-qb", "c-te"))
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                min_outgoing=2,
                max_outgoing=2,
                min_incoming=2,
                max_incoming=2,
                outgoing_filter=TradePackageFilter(
                    positions={"RB", "WR"},
                    position_mode="include",
                ),
                require_no_drops=True,
            ),
            eligible_positions_by_player={
                "p-rb": {"RB"},
                "p-wr": {"WR"},
                "p-dual-ir": {"RB", "WR"},
                "p-te": {"TE"},
                "c-qb": {"QB"},
                "c-te": {"TE"},
            },
        )

        self.assertEqual(space.candidate_count, 1)
        self.assertEqual(
            tuple(space),
            (candidate(("p-rb", "p-wr"), ("c-qb", "c-te")),),
        )

    def test_position_filter_requires_complete_eligibility_for_that_side(self):
        with self.assertRaisesRegex(ValueError, "eligible_positions_by_player"):
            TradeSpace(
                roster("primary", ("a", "b")),
                roster("counterparty", ("x",)),
                TradeConstraints(
                    outgoing_filter=TradePackageFilter(
                        positions={"RB"},
                        position_mode="include",
                    )
                ),
            )

        with self.assertRaisesRegex(ValueError, "missing player 'b'"):
            TradeSpace(
                roster("primary", ("a", "b")),
                roster("counterparty", ("x",)),
                TradeConstraints(
                    outgoing_filter=TradePackageFilter(
                        positions={"RB"},
                        position_mode="include",
                    )
                ),
                eligible_positions_by_player={"a": {"RB"}},
            )

    def test_package_filter_normalizes_modes_positions_and_invalid_values(self):
        rule = TradePackageFilter(
            player_ids={"a"},
            player_mode="include",
            positions={"D/ST"},
            position_mode="only",
        )

        self.assertIs(rule.player_mode, TradeFilterMode.INCLUDE)
        self.assertIs(rule.position_mode, TradeFilterMode.ONLY)
        self.assertEqual(TRADE_FILTER_SEMANTICS_VERSION, 1)
        self.assertEqual(rule.positions, frozenset({"DST"}))
        self.assertEqual(
            rule.to_record(),
            {
                "player_ids": ["a"],
                "player_mode": "include",
                "positions": ["DST"],
                "position_mode": "only",
            },
        )
        with self.assertRaisesRegex(ValueError, "player_mode"):
            TradePackageFilter(player_ids={"a"}, player_mode="sometimes")
        with self.assertRaisesRegex(ValueError, "unsupported player position"):
            TradePackageFilter(positions={"FLEX"}, position_mode="only")
        with self.assertRaisesRegex(ValueError, "player_ids"):
            TradePackageFilter(player_ids={None}, player_mode="include")

    def test_expression_records_are_recursive_canonical_and_strict(self):
        include_a = TradePackageFilter(player_ids={"a"}, player_mode="include")
        exclude_qb = TradePackageFilter(positions={"QB"}, position_mode="exclude")
        expression = TradeFilterExpression(
            "and",
            (
                TradeFilterExpression("not", (exclude_qb,)),
                TradeFilterExpression("xor", (exclude_qb, include_a)),
            ),
        )

        record = expression.to_record()

        self.assertIs(expression.operator, TradeFilterOperator.AND)
        self.assertEqual(parse_trade_filter("filter", record), expression)
        self.assertEqual(
            record,
            TradeFilterExpression("and", tuple(reversed(expression.operands))).to_record(),
        )
        self.assertEqual(
            TradeConstraints(outgoing_filter=expression).to_record()[
                "package_filter_semantics_version"
            ],
            TRADE_FILTER_EXPRESSION_SEMANTICS_VERSION,
        )
        self.assertEqual(
            TradeConstraints(outgoing_filter=include_a).to_record()[
                "package_filter_semantics_version"
            ],
            TRADE_FILTER_SEMANTICS_VERSION,
        )
        with self.assertRaisesRegex(ValueError, "at least two"):
            parse_trade_filter(
                "filter",
                {"operator": "xor", "operands": [include_a.to_record()]},
            )
        with self.assertRaisesRegex(ValueError, "active package rule"):
            TradeFilterExpression("not", (TradePackageFilter(),))

        too_deep = include_a.to_record()
        for _ in range(MAX_TRADE_FILTER_EXPRESSION_DEPTH + 1):
            too_deep = {"operator": "not", "operands": [too_deep]}
        with self.assertRaisesRegex(ValueError, "maximum nesting depth"):
            parse_trade_filter("filter", too_deep)

        too_many = {
            "operator": "or",
            "operands": [
                include_a.to_record()
                for _ in range(MAX_TRADE_FILTER_EXPRESSION_NODES)
            ],
        }
        with self.assertRaisesRegex(ValueError, "maximum node count"):
            parse_trade_filter("filter", too_many)

    def test_nested_boolean_filters_match_exhaustive_two_team_oracle(self):
        primary = roster("primary", ("a", "b", "c", "d"))
        counterparty = roster("counterparty", ("x", "y", "z"))
        positions = {
            "a": {"RB"},
            "b": {"WR"},
            "c": {"TE"},
            "d": {"RB", "WR"},
            "x": {"RB"},
            "y": {"WR"},
            "z": {"TE"},
        }
        include_a = TradePackageFilter(player_ids={"a"}, player_mode="include")
        has_wr = TradePackageFilter(positions={"WR"}, position_mode="include")
        excludes_c = TradePackageFilter(player_ids={"c"}, player_mode="exclude")
        rb_only = TradePackageFilter(positions={"RB"}, position_mode="only")
        outgoing_filter = TradeFilterExpression(
            "and",
            (
                TradeFilterExpression("or", (include_a, has_wr)),
                TradeFilterExpression(
                    "not",
                    (TradeFilterExpression("xor", (excludes_c, rb_only)),),
                ),
            ),
        )
        incoming_filter = TradeFilterExpression(
            "xor",
            (
                TradePackageFilter(player_ids={"x"}, player_mode="include"),
                TradePackageFilter(positions={"WR"}, position_mode="include"),
                TradePackageFilter(player_ids={"z"}, player_mode="exclude"),
            ),
        )
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=3,
            min_incoming=1,
            max_incoming=3,
            outgoing_filter=outgoing_filter,
            incoming_filter=incoming_filter,
        )
        expected = tuple(
            candidate(outgoing, incoming)
            for outgoing_size in range(1, 4)
            for incoming_size in range(1, 4)
            for outgoing in combinations(primary.player_ids, outgoing_size)
            if package_matches(outgoing, outgoing_filter, positions)
            for incoming in combinations(counterparty.player_ids, incoming_size)
            if package_matches(incoming, incoming_filter, positions)
        )

        space = TradeSpace(
            primary,
            counterparty,
            constraints,
            eligible_positions_by_player=positions,
        )

        self.assertTrue(expected)
        self.assertEqual(space.candidate_count, len(expected))
        self.assertEqual(tuple(space), expected)

    def test_wrapping_a_legacy_filter_preserves_two_team_results(self):
        positions = {"a": {"RB"}, "b": {"WR"}, "c": {"TE"}, "x": {"QB"}}
        legacy_filter = TradePackageFilter(
            player_ids={"a"},
            player_mode="include",
            positions={"TE"},
            position_mode="exclude",
        )
        inputs = dict(
            min_outgoing=1,
            max_outgoing=3,
            outgoing_filter=legacy_filter,
        )
        legacy = TradeSpace(
            roster("primary", ("a", "b", "c")),
            roster("counterparty", ("x",)),
            TradeConstraints(**inputs),
            eligible_positions_by_player=positions,
        )
        composed = TradeSpace(
            roster("primary", ("a", "b", "c")),
            roster("counterparty", ("x",)),
            TradeConstraints(
                **{
                    **inputs,
                    "outgoing_filter": TradeFilterExpression(
                        "and", (legacy_filter, legacy_filter)
                    ),
                }
            ),
            eligible_positions_by_player=positions,
        )

        self.assertEqual(composed.candidate_count, legacy.candidate_count)
        self.assertEqual(tuple(composed), tuple(legacy))

    def test_legacy_only_iteration_prunes_before_combination_scanning(self):
        selected = tuple(f"c{number}" for number in range(10))
        primary = roster("primary", ("p0", "p1"))
        counterparty = roster(
            "counterparty", tuple(f"c{number}" for number in range(20))
        )
        real_combinations = combinations
        examined = 0

        def counted_combinations(*args):
            nonlocal examined
            for package in real_combinations(*args):
                examined += 1
                yield package

        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(
                min_incoming=10,
                max_incoming=10,
                incoming_filter=TradePackageFilter(
                    player_ids=selected, player_mode="only"
                ),
            ),
        )
        with patch(
            "trade_snapshot.trade_package_enumeration.combinations",
            counted_combinations,
        ):
            rows = tuple(space)

        self.assertEqual(len(rows), 2)
        self.assertLess(examined, 10)
        self.assertTrue(
            all(row.incoming_player_ids == selected for row in rows)
        )

    def test_composed_incoming_packages_are_scanned_once_and_replayed(self):
        selected = tuple(f"c{number}" for number in range(10))
        only_selected = TradePackageFilter(
            player_ids=selected, player_mode="only"
        )
        expression = TradeFilterExpression(
            "and", (only_selected, only_selected)
        )
        original_matches = CompiledTradeFilter.matches
        match_checks = 0

        def counted_matches(compiled_filter, evidence):
            nonlocal match_checks
            match_checks += 1
            return original_matches(compiled_filter, evidence)

        with patch.object(CompiledTradeFilter, "matches", counted_matches):
            space = TradeSpace(
                roster("primary", ("p0", "p1")),
                roster(
                    "counterparty",
                    tuple(f"c{number}" for number in range(20)),
                ),
                TradeConstraints(
                    min_incoming=10,
                    max_incoming=10,
                    incoming_filter=expression,
                ),
            )
            match_checks = 0
            rows = tuple(space)

        self.assertEqual(len(rows), 2)
        self.assertEqual(match_checks, math.comb(20, 10))
        self.assertTrue(
            all(row.incoming_player_ids == selected for row in rows)
        )

    def test_no_drops_uses_each_teams_post_trade_roster_size(self):
        primary = TeamRoster("primary", ("a", "b"), current_size=14, roster_cap=14)
        counterparty = TeamRoster("counterparty", ("x", "y"), current_size=13, roster_cap=14)
        package_sizes = dict(
            min_outgoing=1,
            max_outgoing=2,
            min_incoming=1,
            max_incoming=2,
        )

        unrestricted = TradeSpace(primary, counterparty, TradeConstraints(**package_sizes))
        no_drops = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(require_no_drops=True, **package_sizes),
        )

        self.assertEqual(unrestricted.candidate_count, 9)
        self.assertEqual(no_drops.candidate_count, 7)
        self.assertTrue(
            all(
                primary.current_size
                - len(candidate.outgoing_player_ids)
                + len(candidate.incoming_player_ids)
                <= primary.roster_cap
                and counterparty.current_size
                - len(candidate.incoming_player_ids)
                + len(candidate.outgoing_player_ids)
                <= counterparty.roster_cap
                for candidate in no_drops
            )
        )

    def test_capacity_exempt_player_is_owned_but_does_not_consume_active_cap(self):
        player_ids = (*tuple(f"p{number}" for number in range(14)), "p-ir")

        roster_with_ir = TeamRoster(
            "primary",
            player_ids,
            current_size=15,
            roster_cap=14,
            capacity_exempt_player_ids={"p-ir"},
        )

        self.assertEqual(roster_with_ir.current_size, 15)
        self.assertEqual(roster_with_ir.active_size, 14)
        self.assertEqual(roster_with_ir.capacity_exempt_player_ids, frozenset({"p-ir"}))
        with self.assertRaisesRegex(ValueError, "roster_cap"):
            TeamRoster("primary", player_ids, current_size=15, roster_cap=14)
        with self.assertRaisesRegex(ValueError, "must be owned"):
            TeamRoster(
                "primary",
                player_ids,
                current_size=15,
                roster_cap=14,
                capacity_exempt_player_ids={"not-owned"},
            )

    def test_no_drop_trade_sending_ir_does_not_free_an_active_slot(self):
        primary_ids = (*tuple(f"p{number}" for number in range(14)), "p-ir")
        primary = TeamRoster(
            "primary",
            primary_ids,
            15,
            14,
            {"p-ir"},
        )
        counterparty = TeamRoster(
            "counterparty",
            tuple(f"c{number}" for number in range(13)),
            13,
            14,
        )

        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(require_no_drops=True),
        )
        candidates = tuple(space)

        self.assertEqual(space.candidate_count, 14 * 13)
        self.assertEqual(len(candidates), space.candidate_count)
        self.assertTrue(
            all("p-ir" not in row.outgoing_player_ids for row in candidates)
        )

    def test_no_drop_trade_treats_an_incoming_ir_player_as_active(self):
        primary_ids = tuple(f"p{number}" for number in range(14))
        counterparty_ids = (*tuple(f"c{number}" for number in range(14)), "c-ir")
        unlocked = {"p0", "p1", "c0", "c1", "c-ir"}
        space = TradeSpace(
            TeamRoster("primary", primary_ids, 14, 14),
            TeamRoster(
                "counterparty",
                counterparty_ids,
                15,
                14,
                {"c-ir"},
            ),
            TradeConstraints(
                min_outgoing=2,
                max_outgoing=2,
                min_incoming=3,
                max_incoming=3,
                require_no_drops=True,
                locked_player_ids=frozenset(
                    set(primary_ids).union(counterparty_ids).difference(unlocked)
                ),
            ),
        )

        self.assertEqual(space.candidate_count, 0)
        self.assertEqual(tuple(space), ())

    def test_thirteen_active_players_can_receive_one_net_active_player(self):
        primary_ids = (*tuple(f"p{number}" for number in range(13)), "p-ir")
        counterparty_ids = tuple(f"c{number}" for number in range(14))
        unlocked = {"p0", "c0", "c1"}
        space = TradeSpace(
            TeamRoster("primary", primary_ids, 14, 14, {"p-ir"}),
            TeamRoster("counterparty", counterparty_ids, 14, 14),
            TradeConstraints(
                min_outgoing=1,
                max_outgoing=1,
                min_incoming=2,
                max_incoming=2,
                require_no_drops=True,
                locked_player_ids=frozenset(
                    set(primary_ids).union(counterparty_ids).difference(unlocked)
                ),
            ),
        )

        self.assertEqual(space.primary.active_size, 13)
        self.assertEqual(space.candidate_count, 1)
        self.assertEqual(
            next(iter(space)).outgoing_player_ids,
            ("p0",),
        )

    def test_rejects_duplicate_or_cross_owned_player_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate player_id"):
            roster("primary", ("a", "a"))

        with self.assertRaisesRegex(ValueError, "both teams"):
            TradeSpace(
                roster("primary", ("a", "b")),
                roster("counterparty", ("b", "c")),
                TradeConstraints(),
            )

    def test_validates_constraint_and_roster_bounds(self):
        with self.assertRaisesRegex(ValueError, "max_outgoing"):
            TradeConstraints(min_outgoing=2, max_outgoing=1)
        with self.assertRaisesRegex(ValueError, "max_total_players"):
            TradeConstraints(max_total_players=1)
        with self.assertRaisesRegex(ValueError, "current_size"):
            TeamRoster("primary", ("a", "b"), current_size=1, roster_cap=14)
        with self.assertRaisesRegex(ValueError, "roster_cap"):
            TeamRoster("primary", ("a",), current_size=2, roster_cap=1)
        with self.assertRaisesRegex(ValueError, "team_id must be a non-empty string"):
            TeamRoster(None, ("a",), current_size=1, roster_cap=1)
        with self.assertRaisesRegex(ValueError, "team_id must be a non-empty string"):
            TeamRoster([], ("a",), current_size=1, roster_cap=1)
        with self.assertRaisesRegex(ValueError, "player_ids must be non-empty strings"):
            TeamRoster("primary", (None,), current_size=1, roster_cap=1)


def roster(team_id, player_ids):
    return TeamRoster(
        team_id=team_id,
        player_ids=player_ids,
        current_size=len(player_ids),
        roster_cap=len(player_ids),
    )


def candidate(outgoing, incoming):
    return TradeCandidate(outgoing, incoming)


def package_matches(player_ids, rule, positions):
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
    eligible = {player_id: set(positions[player_id]) for player_id in player_ids}
    if rule.position_mode is TradeFilterMode.INCLUDE:
        covered = set().union(*eligible.values()) if eligible else set()
        return rule.positions <= covered
    matching = tuple(
        bool(values & rule.positions) for values in eligible.values()
    )
    if rule.position_mode is TradeFilterMode.ONLY:
        return all(matching)
    return not any(matching)


if __name__ == "__main__":
    unittest.main()
