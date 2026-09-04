from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trade_snapshot.ensemble import EnsembleProjection, ProviderObservation
from trade_snapshot.league_state import (
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot.projections import ProjectionStatus
from trade_snapshot.roster_adjustment import PreparedRosterAdjuster
from trade_snapshot.scenario_config import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
)
from trade_snapshot.search_runner import TradeSearchSettings
from trade_snapshot.trade_filters import TradePackageFilter
from trade_snapshot.strength import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    StrengthModel,
)
from trade_snapshot.three_way_search import (
    PreparedThreeWayTrade,
    ResumableThreeWayTradeSearch,
    THREE_WAY_SEARCH_ALGORITHM,
    ThreeWayQualifiedResult,
    ThreeWaySearchOutcome,
    ThreeWaySearchRunDefinition,
    ThreeWaySearchRunMismatchError,
    ThreeWaySearchStore,
    ThreeWaySearchStoreError,
    ThreeWayTeamResult,
)
from trade_snapshot.three_way_trade import (
    ThreeWayTradeCandidate,
    ThreeWayTradeSpace,
    TradeTransfer,
)
from trade_snapshot.three_way_search_store import (
    THREE_WAY_DATABASE_APPLICATION_ID,
)
from trade_snapshot.trade_impact import PreparedSeasonBaseline, prepare_season_baseline
from trade_snapshot.trade_space import TeamRoster, TradeConstraints


POINTS = {
    "a1": 14.0,
    "a2": 4.0,
    "b1": 12.0,
    "b2": 3.0,
    "c1": 10.0,
    "c2": 2.0,
    "d1": 8.0,
    "d2": 1.0,
    "fa1": 7.0,
    "fa2": 6.0,
}


def model(points=POINTS):
    return StrengthModel(
        role_definitions=(
            RoleDefinition("FLEX", RoleKind.STARTER, "FLEX", frozenset({"FLEX"})),
        ),
        players=tuple(
            PlayerStrength(player_id, value, frozenset({"FLEX"}), {"FLEX": 0})
            for player_id, value in points.items()
        ),
        normalization_denominator=100,
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="profile-1",
        calibration=CalibrationMetadata(
            "https://cdn.fantasypros.com/assets/trade-analyzer.js",
            "1" * 64,
            "2" * 64,
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
    )


def league_rosters():
    return (
        TeamRoster("a", ("a1", "a2"), 2, 2),
        TeamRoster("b", ("b1", "b2"), 2, 2),
        TeamRoster("c", ("c1", "c2"), 2, 2),
        TeamRoster("d", ("d1", "d2"), 2, 2),
    )


def state():
    team_ids = ("a", "b", "c", "d")
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="profile-1",
        first_remaining_week=1,
        teams=tuple(LeagueTeam(team_id, team_id.upper()) for team_id in team_ids),
        standings=tuple(
            TeamStanding(team_id, 0, 0, 0, 0, 0) for team_id in team_ids
        ),
        remaining_matchups=(
            FantasyMatchup(1, "a", "b"),
            FantasyMatchup(1, "c", "d"),
        ),
        roster_rules=RosterRules(2, ("FLEX",)),
        playoff_rules=PlayoffRules(
            qualifier_count=2,
            regular_season_end_week=1,
            playoff_weeks=(2,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def projection(player_id, value):
    return EnsembleProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-1",
        scoring_profile_id="profile-1",
        season=2026,
        week=1,
        position="FLEX",
        status=ProjectionStatus.OBSERVED,
        provider_observations=(
            ProviderObservation(
                "source", f"source-{player_id}", ProjectionStatus.OBSERVED, value, 1
            ),
        ),
        minimum_observed_sources=1,
        position_stddev_floor=0,
        projected_fantasy_points=value,
        between_provider_stddev=0,
        predictive_stddev=0,
        nfl_team_id=f"NFL-{player_id}",
        nfl_game_id="G1",
        opponent_team_id=f"OPP-{player_id}",
        is_home=True,
    )


def components(*, threshold=-100.0, checkpoint_interval=3):
    rosters = league_rosters()
    strength = model()
    baseline = prepare_season_baseline(
        state(),
        rosters,
        tuple(projection(player_id, value) for player_id, value in POINTS.items()),
        tuple(PlayerEligibility(player_id, ("FLEX",)) for player_id in POINTS),
        CorrelatedScenarioConfig(5, 19, FactorLoadings(0, 0, 0, 1)),
    )
    constraints = TradeConstraints(
        min_outgoing=1,
        max_outgoing=1,
        min_incoming=1,
        max_incoming=1,
        max_total_players=3,
        balanced_only=True,
        require_no_drops=True,
    )
    space = ThreeWayTradeSpace((rosters[0], rosters[2], rosters[1]), constraints)
    prepared = PreparedThreeWayTrade(strength, space.rosters)
    runner = ResumableThreeWayTradeSearch(
        space,
        prepared,
        baseline,
        TradeSearchSettings(threshold, checkpoint_interval),
    )
    return space, prepared, baseline, runner


def saved_result(index, *, gains=(1.0, 1.0, 1.0)):
    transfers = (
        TradeTransfer("a", "b", ("a1",)),
        TradeTransfer("b", "c", ("b1",)),
        TradeTransfer("c", "a", ("c1",)),
    )
    packages = {
        "a": (("a1",), ("c1",)),
        "b": (("b1",), ("a1",)),
        "c": (("c1",), ("b1",)),
    }
    results = tuple(
        ThreeWayTeamResult(
            team_id,
            packages[team_id][0],
            packages[team_id][1],
            (),
            (),
            gain,
            gain,
            40.0,
            40.0 + gain,
        )
        for team_id, gain in zip(("a", "b", "c"), gains)
    )
    return ThreeWayQualifiedResult(index, transfers, results)


def run_definition(total=100):
    return ThreeWaySearchRunDefinition(
        "snapshot-1",
        "strength-1",
        ("a", "b", "c"),
        {"algorithm": "test", "constraints": {"balanced_only": True}},
        total,
    )


class ThreeWayEvaluationTests(unittest.TestCase):
    def test_prepared_evaluation_scores_three_conserved_rosters(self):
        space, prepared, _, _ = components()
        candidate = next(iter(space))
        result = prepared.evaluate(candidate, candidate_index=7)

        before = {player for roster in prepared.rosters for player in roster.player_ids}
        after_rows = tuple(row.roster.player_ids for row in result.adjustments)
        after = {player for roster in after_rows for player in roster}
        self.assertEqual(before, after)
        self.assertEqual(sum(map(len, after_rows)), len(after))
        self.assertEqual(tuple(row.team_id for row in result.changes), ("a", "b", "c"))
        self.assertEqual(result.candidate_index, 7)

    def test_adjusted_trade_uses_globally_unique_additions_and_valid_drops(self):
        points = {
            **{key: value for key, value in POINTS.items() if not key.startswith("d")},
            "c3": 1.5,
            "c4": 1.0,
        }
        strength = model(points)
        rosters = (
            TeamRoster("a", ("a1", "a2"), 2, 2),
            TeamRoster("b", ("b1", "b2"), 2, 2),
            TeamRoster("c", ("c1", "c2", "c3", "c4"), 4, 4),
        )
        candidate = ThreeWayTradeCandidate(
            ("a", "b", "c"),
            (
                TradeTransfer("a", "c", ("a1", "a2")),
                TradeTransfer("b", "c", ("b1", "b2")),
                TradeTransfer("c", "a", ("c1",)),
                TradeTransfer("c", "b", ("c2",)),
            ),
        )
        prepared = PreparedThreeWayTrade(
            strength,
            rosters,
            PreparedRosterAdjuster(strength, rosters),
        )
        result = prepared.evaluate(candidate, candidate_index=0)

        additions = tuple(
            player for row in result.adjustments for player in row.added_player_ids
        )
        owned = tuple(
            player for row in result.adjustments for player in row.roster.player_ids
        )
        self.assertEqual(len(additions), 2)
        self.assertEqual(len(set(additions)), 2)
        self.assertEqual(len(result.adjustments[2].dropped_player_ids), 2)
        self.assertEqual(len(owned), len(set(owned)))

        alternate_rosters = (rosters[2], rosters[0], rosters[1])
        reverse_prepared = PreparedThreeWayTrade(
            strength,
            alternate_rosters,
            PreparedRosterAdjuster(strength, rosters),
        )
        reverse_candidate = ThreeWayTradeCandidate(
            tuple(row.team_id for row in alternate_rosters), candidate.transfers
        )
        reverse = reverse_prepared.evaluate(reverse_candidate, candidate_index=0)
        expected = {
            row.roster.team_id: (row.added_player_ids, row.dropped_player_ids)
            for row in result.adjustments
        }
        actual = {
            row.roster.team_id: (row.added_player_ids, row.dropped_player_ids)
            for row in reverse.adjustments
        }
        self.assertEqual(actual, expected)

    def test_pure_adjustment_transfers_and_assigns_incoming_ir_status(self):
        strength = model()
        rosters = (
            TeamRoster(
                "a",
                ("a1", "a2"),
                2,
                2,
                reserve_slot_counts={"IR": 1},
            ),
            TeamRoster(
                "b",
                ("b1", "b2"),
                2,
                1,
                reserve_slot_by_player={"b2": "IR"},
                reserve_slot_counts={"IR": 1},
            ),
            TeamRoster("c", ("c1", "c2"), 2, 2),
        )
        candidate = ThreeWayTradeCandidate(
            ("a", "b", "c"),
            (
                TradeTransfer("a", "b", ("a1",)),
                TradeTransfer("b", "a", ("b2",)),
                TradeTransfer("b", "c", ("b1",)),
                TradeTransfer("c", "a", ("c1",)),
            ),
        )

        result = PreparedThreeWayTrade(strength, rosters).evaluate(
            candidate, candidate_index=0
        )
        by_team = {row.roster.team_id: row.roster for row in result.adjustments}

        self.assertEqual(dict(by_team["a"].reserve_slot_by_player), {"b2": "IR"})
        self.assertEqual(dict(by_team["a"].reserve_slot_counts), {"IR": 1})
        self.assertEqual(by_team["a"].active_size, 2)
        self.assertEqual(dict(by_team["b"].reserve_slot_by_player), {})

    def test_no_drop_runner_rejects_an_adjuster(self):
        space, prepared, baseline, _ = components()
        adjusted = PreparedThreeWayTrade(
            prepared.model,
            space.rosters,
            PreparedRosterAdjuster(prepared.model, league_rosters()),
        )
        with self.assertRaisesRegex(ValueError, "pure simultaneous"):
            ResumableThreeWayTradeSearch(space, adjusted, baseline)
        for invalid in (0, "", []):
            with self.subTest(settings=invalid), self.assertRaisesRegex(
                ValueError, "settings"
            ):
                ResumableThreeWayTradeSearch(space, prepared, baseline, invalid)


class ThreeWayRunnerTests(unittest.TestCase):
    def test_run_identity_uses_typed_rosters_and_bumped_algorithm(self):
        _, _, _, runner = components()
        record = runner.run_definition.trade_constraint_record
        roster_record = record["candidate_order"]["rosters"][0]

        self.assertEqual(
            THREE_WAY_SEARCH_ALGORITHM,
            "local-three-way-power-paired-playoffs-v2",
        )
        self.assertEqual(record["algorithm"], THREE_WAY_SEARCH_ALGORITHM)
        self.assertIn("reserve_slot_by_player", roster_record)
        self.assertIn("reserve_slot_counts", roster_record)
        self.assertNotIn("capacity_exempt_player_ids", roster_record)

    def test_run_identity_binds_position_evidence_that_changes_candidate_order(self):
        _, prepared, baseline, _ = components()
        constraints = TradeConstraints(
            min_outgoing=1,
            max_outgoing=1,
            min_incoming=1,
            max_incoming=1,
            max_total_players=3,
            balanced_only=True,
            require_no_drops=True,
            outgoing_filter=TradePackageFilter(
                positions={"QB"}, position_mode="only"
            ),
        )
        first_positions = {"a1": {"QB"}, "a2": {"RB"}}
        second_positions = {"a1": {"RB"}, "a2": {"QB"}}
        first = ThreeWayTradeSpace(
            prepared.rosters,
            constraints,
            eligible_positions_by_player=first_positions,
        )
        second = ThreeWayTradeSpace(
            prepared.rosters,
            constraints,
            eligible_positions_by_player=second_positions,
        )

        self.assertEqual(first.candidate_count, second.candidate_count)
        self.assertNotEqual(
            next(iter(first)).outgoing_for("a"),
            next(iter(second)).outgoing_for("a"),
        )
        self.assertNotEqual(
            ResumableThreeWayTradeSearch(first, prepared, baseline).run_definition.run_id,
            ResumableThreeWayTradeSearch(second, prepared, baseline).run_definition.run_id,
        )

    def test_runs_all_candidates_and_reads_best_first_after_close(self):
        space, _, _, runner = components()
        updates = []
        with TemporaryDirectory() as directory:
            outcome = runner.run(
                Path(directory) / "three.sqlite3", on_progress=updates.append
            )
            rows = outcome.results()
            limited = outcome.results(1)

        self.assertEqual(space.candidate_count, 16)
        self.assertEqual(outcome.progress.next_candidate_index, 16)
        self.assertEqual(outcome.progress.power_qualified_count, len(rows))
        self.assertEqual(outcome.progress.playoff_evaluated_count, len(rows))
        self.assertEqual(len(limited), min(1, len(rows)))
        self.assertEqual(updates[-1], outcome.progress)
        self.assertTrue(all(len(row.team_results) == 3 for row in rows))
        ordering = tuple(
            (not row.all_teams_gain, -row.combined_playoff_delta, row.candidate_index)
            for row in rows
        )
        self.assertEqual(ordering, tuple(sorted(ordering)))

    def test_power_floor_checks_all_three_and_simulates_only_survivors(self):
        space, prepared, baseline, _ = components()
        threshold = -1.0
        expected = sum(
            all(change.display_delta >= threshold for change in prepared.evaluate(
                candidate, candidate_index=index
            ).changes)
            for index, candidate in enumerate(space)
        )
        runner = ResumableThreeWayTradeSearch(
            space,
            prepared,
            baseline,
            TradeSearchSettings(threshold, 2),
        )
        original_project = PreparedSeasonBaseline.project
        call_count = 0

        def counted_project(instance, after_rosters):
            nonlocal call_count
            call_count += 1
            return original_project(instance, after_rosters)

        with TemporaryDirectory() as directory, patch.object(
            PreparedSeasonBaseline, "project", counted_project
        ):
            outcome = runner.run(Path(directory) / "power.sqlite3")
            rows = outcome.results()

        self.assertEqual(outcome.progress.power_qualified_count, expected)
        self.assertEqual(call_count, expected)
        self.assertTrue(
            all(
                team.display_power_delta >= threshold
                for row in rows
                for team in row.team_results
            )
        )

    def test_cancel_resumes_through_seekable_space_without_replaying(self):
        space, _, _, runner = components(checkpoint_interval=100)
        starts = []
        original_iter_from = space.iter_from
        space.iter_from = lambda index: (
            starts.append(index) or original_iter_from(index)
        )
        cancel_calls = 0

        def cancel_after_three():
            nonlocal cancel_calls
            cancel_calls += 1
            return cancel_calls > 3

        with TemporaryDirectory() as directory:
            path = Path(directory) / "resume.sqlite3"
            partial = runner.run(path, should_cancel=cancel_after_three)
            complete = runner.run(path)

        self.assertTrue(partial.progress.cancelled)
        self.assertEqual(partial.progress.next_candidate_index, 3)
        self.assertFalse(complete.progress.cancelled)
        self.assertEqual(starts, [0, 3])


class ThreeWayStoreTests(unittest.TestCase):
    def test_result_rejects_conflicting_adjustment_players(self):
        original = saved_result(1)
        first, second, third = original.team_results
        conflicts = (
            (
                replace(first, added_player_ids=("fa1",)),
                replace(second, added_player_ids=("fa1",)),
                third,
            ),
            (
                replace(first, dropped_player_ids=("bench",)),
                replace(second, dropped_player_ids=("bench",)),
                third,
            ),
            (
                replace(first, added_player_ids=("fa1",)),
                replace(second, dropped_player_ids=("fa1",)),
                third,
            ),
            (replace(first, added_player_ids=("a1",)), second, third),
            (replace(first, dropped_player_ids=("a1",)), second, third),
        )
        for team_results in conflicts:
            with self.subTest(team_results=team_results), self.assertRaises(ValueError):
                ThreeWayQualifiedResult(
                    original.candidate_index, original.transfers, team_results
                )

    def test_round_trips_indices_beyond_int64_and_orders_best_results(self):
        total = 1 << 70
        definition = run_definition(total)
        high = 1 << 69
        with TemporaryDirectory() as directory:
            path = Path(directory) / "large.sqlite3"
            with ThreeWaySearchStore(path, definition) as store:
                store.checkpoint(high)
                store.upsert_qualified_result(
                    saved_result(high, gains=(2, 2, 2)),
                    next_candidate_index=high + 1,
                )
                store.upsert_qualified_result(
                    saved_result(3, gains=(1, -1, 5))
                )
                state = store.resume()
                with self.assertRaises(ValueError):
                    store.results(0)
            outcome = ThreeWaySearchOutcome(
                progress=runner_progress(definition, state), database_path=path
            )
            rows = outcome.results()
            with self.assertRaises(ValueError):
                outcome.results(0)

        self.assertEqual(state.next_candidate_index, high + 1)
        self.assertEqual(tuple(row.candidate_index for row in rows), (high, 3))
        self.assertEqual(
            ThreeWaySearchRunDefinition.from_record(definition.to_record()), definition
        )
        invalid_record = definition.to_record()
        invalid_record["total_candidate_count"] = "١٠٠"
        with self.assertRaisesRegex(ValueError, "canonical"):
            ThreeWaySearchRunDefinition.from_record(invalid_record)

    def test_store_binds_results_and_closed_reads_to_one_run(self):
        definition = run_definition()
        other = ThreeWaySearchRunDefinition(
            "snapshot-1", "strength-1", ("x", "y", "z"), {}, 100
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bound.sqlite3"
            with ThreeWaySearchStore(path, definition) as store:
                store.upsert_qualified_result(saved_result(2))
            with ThreeWaySearchStore(path, definition) as store:
                with self.assertRaisesRegex(ValueError, "result teams"):
                    store.upsert_qualified_result(
                        ThreeWayQualifiedResult(
                            3,
                            (
                                TradeTransfer("x", "y", ("x1",)),
                                TradeTransfer("y", "z", ("y1",)),
                                TradeTransfer("z", "x", ("z1",)),
                            ),
                            tuple(
                                ThreeWayTeamResult(
                                    team_id,
                                    (sent,),
                                    (received,),
                                    (),
                                    (),
                                    1,
                                    1,
                                    40,
                                    41,
                                )
                                for team_id, sent, received in (
                                    ("x", "x1", "z1"),
                                    ("y", "y1", "x1"),
                                    ("z", "z1", "y1"),
                                )
                            ),
                        )
                    )
            with self.assertRaises(ThreeWaySearchRunMismatchError):
                ThreeWaySearchOutcome(runner_progress(other), path).results()

    def test_missing_run_row_cannot_rebind_stale_results(self):
        definition = run_definition()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "orphan.sqlite3"
            with ThreeWaySearchStore(path, definition) as store:
                store.upsert_qualified_result(saved_result(2))
            with closing(sqlite3.connect(path)) as database:
                database.execute("DELETE FROM search_run")
                database.commit()
            with self.assertRaisesRegex(ThreeWaySearchStoreError, "missing"):
                ThreeWaySearchStore(path, definition)
            with self.assertRaisesRegex(ThreeWaySearchStoreError, "missing"):
                ThreeWaySearchOutcome(runner_progress(definition), path).results()

    def test_wrong_version_one_table_layout_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-schema.sqlite3"
            with closing(sqlite3.connect(path)) as database:
                database.execute("CREATE TABLE search_run (wrong TEXT)")
                database.execute("CREATE TABLE qualified_result (wrong TEXT)")
                database.execute("PRAGMA journal_mode = DELETE")
                database.execute(
                    f"PRAGMA application_id = {THREE_WAY_DATABASE_APPLICATION_ID}"
                )
                database.execute("PRAGMA user_version = 1")
                database.commit()
            with self.assertRaisesRegex(ThreeWaySearchStoreError, "table schema"):
                ThreeWaySearchStore(path, run_definition())
            with closing(sqlite3.connect(path)) as database:
                self.assertEqual(
                    database.execute("PRAGMA journal_mode").fetchone()[0], "delete"
                )

    def test_limited_read_validates_rows_hidden_by_corrupt_ranking_data(self):
        definition = run_definition()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rank-corruption.sqlite3"
            with ThreeWaySearchStore(path, definition) as store:
                store.upsert_qualified_result(saved_result(2, gains=(3, 3, 3)))
                store.upsert_qualified_result(saved_result(3, gains=(2, 2, 2)))
            with closing(sqlite3.connect(path)) as database:
                database.execute(
                    "UPDATE qualified_result SET combined_playoff_delta=-99 "
                    "WHERE candidate_index_text='2'"
                )
                database.commit()
            with self.assertRaisesRegex(ThreeWaySearchStoreError, "invalid"):
                ThreeWaySearchOutcome(runner_progress(definition), path).results(1)

    def test_run_mismatch_and_corrupt_result_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.sqlite3"
            definition = run_definition()
            with ThreeWaySearchStore(path, definition) as store:
                store.upsert_qualified_result(saved_result(2))
            with self.assertRaises(ThreeWaySearchRunMismatchError):
                ThreeWaySearchStore(
                    path,
                    ThreeWaySearchRunDefinition(
                        "snapshot-1", "strength-2", ("a", "b", "c"), {}, 100
                    ),
                )
            with closing(sqlite3.connect(path)) as database:
                database.execute(
                    "UPDATE qualified_result SET result_json='NaN' "
                    "WHERE candidate_index_text='2'"
                )
                database.commit()
            with ThreeWaySearchStore(path, definition) as store:
                with self.assertRaisesRegex(ThreeWaySearchStoreError, "invalid"):
                    store.resume()
            with self.assertRaisesRegex(ThreeWaySearchStoreError, "invalid"):
                ThreeWaySearchOutcome(runner_progress(definition), path).results()

    def test_tampered_adjustment_conflict_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conflict.sqlite3"
            definition = run_definition()
            result = saved_result(2)
            with ThreeWaySearchStore(path, definition) as store:
                store.upsert_qualified_result(result)
            record = result.to_record()
            record["team_results"][0]["added_player_ids"] = ["a1"]
            with closing(sqlite3.connect(path)) as database:
                database.execute(
                    "UPDATE qualified_result SET result_json=? "
                    "WHERE candidate_index_text='2'",
                    (json.dumps(record, separators=(",", ":"), sort_keys=True),),
                )
                database.commit()
            with self.assertRaisesRegex(ThreeWaySearchStoreError, "invalid"):
                ThreeWaySearchOutcome(runner_progress(definition), path).results()


def runner_progress(definition, state=None):
    from trade_snapshot.three_way_search import ThreeWaySearchProgress

    next_index = 0 if state is None else state.next_candidate_index
    count = 0 if state is None else state.qualified_result_count
    gains = 0 if state is None else state.all_playoff_gain_count
    return ThreeWaySearchProgress(
        definition.run_id,
        next_index,
        definition.total_candidate_count,
        count,
        count,
        gains,
    )


if __name__ == "__main__":
    unittest.main()
