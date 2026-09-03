from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from tests.test_engine_bundle import engine_bundle, exact_model_and_attestation
import trade_snapshot.gm_trade_valuation as valuation_module
from trade_snapshot.gm_trade_valuation import value_historical_trades
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeam,
    HistoryTeamRoster,
    HistoryTimestampBasis,
    HistoryTransaction,
    HistoryTransactionAsset,
    HistoryTransactionAssetKind,
    HistoryTransactionKind,
    LeagueHistoryCapture,
    LeagueHistorySnapshot,
    make_league_key,
)
from trade_snapshot.league_state import FantasyMatchup, LeagueTeam, TeamStanding
from trade_snapshot.strength import StrengthModel
from trade_snapshot.trade_space import TeamRoster


LEAGUE_KEY = make_league_key("espn", "gm-analytics-test-league")
TRADE_AT = datetime(2026, 8, 30, 18, tzinfo=timezone.utc)
SOURCE_AT = TRADE_AT - timedelta(hours=6)
REQUEST_AT = TRADE_AT + timedelta(days=1)


def current_bundle(source=None, suffix="current"):
    source = source or engine_bundle()
    names = dict(source.player_names)
    names["p1"] = f"P1 {suffix}"
    return replace(source, player_names=names)


def drifted_current_bundle(source):
    model = source.strength_model
    core_players = tuple(
        replace(player, residual_score=4.0)
        if player.player_id == "q1"
        else player
        for player in model.players.values()
        if player.player_id not in {"w1", "w2"}
    )
    seed = StrengthModel(
        model.role_definitions,
        core_players,
        model.normalization_denominator,
        snapshot_id=model.snapshot_id,
        season=model.season,
        scoring_profile_id=model.scoring_profile_id,
        calibration=model.calibration,
    )
    current_model, attestation = exact_model_and_attestation(seed)
    names = dict(source.player_names)
    names["q1"] = "Q1 current-value test"
    return replace(
        source,
        strength_model=current_model,
        methodology_attestation=attestation,
        player_names=names,
    )


def four_team_bundle():
    source = engine_bundle()
    cloned_from = {"r1": "p1", "r2": "p2", "s1": "q1", "s2": "q2"}
    core_players = [
        player
        for player in source.strength_model.players.values()
        if player.player_id not in {"w1", "w2"}
    ]
    core_players.extend(
        replace(
            source.strength_model.players[template_id],
            player_id=player_id,
        )
        for player_id, template_id in cloned_from.items()
    )
    seed = StrengthModel(
        source.strength_model.role_definitions,
        core_players,
        source.strength_model.normalization_denominator,
        snapshot_id=source.strength_model.snapshot_id,
        season=source.strength_model.season,
        scoring_profile_id=source.strength_model.scoring_profile_id,
        calibration=source.strength_model.calibration,
    )
    strength_model, attestation = exact_model_and_attestation(seed)
    projection_by_player = {
        row.canonical_player_id: row for row in source.projections
    }
    projections = list(source.projections)
    eligibilities = list(source.eligibilities)
    eligibility_by_player = {
        row.canonical_player_id: row for row in source.eligibilities
    }
    evidence = list(source.projection_evidence)
    evidence_by_player = {
        row.canonical_player_id: row for row in source.projection_evidence
    }
    for player_id, template_id in cloned_from.items():
        template = projection_by_player[template_id]
        projections.append(
            replace(
                template,
                canonical_player_id=player_id,
                provider_observations=tuple(
                    replace(
                        observation,
                        provider_player_id=f"source-{player_id}",
                    )
                    for observation in template.provider_observations
                ),
                nfl_team_id=f"NFL-{player_id}",
                opponent_team_id=f"OPP-{player_id}",
            )
        )
        eligibilities.append(
            replace(
                eligibility_by_player[template_id],
                canonical_player_id=player_id,
            )
        )
        evidence.append(
            replace(
                evidence_by_player[template_id],
                canonical_player_id=player_id,
                provider_player_id=f"fp-{player_id}",
                nfl_team_id=f"NFL-{player_id}",
                opponent_team_id=f"OPP-{player_id}",
            )
        )
    state = replace(
        source.state,
        teams=(
            *source.state.teams,
            LeagueTeam("third", "Third"),
            LeagueTeam("fourth", "Fourth"),
        ),
        standings=(
            *source.state.standings,
            TeamStanding("third", 0, 0, 0, 0, 0),
            TeamStanding("fourth", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(
            *source.state.remaining_matchups,
            FantasyMatchup(1, "third", "fourth"),
        ),
    )
    return replace(
        source,
        state=state,
        rosters=(
            *source.rosters,
            TeamRoster("third", ("r1", "r2"), 2, 2),
            TeamRoster("fourth", ("s1", "s2"), 2, 2),
        ),
        projections=tuple(projections),
        eligibilities=tuple(eligibilities),
        strength_model=strength_model,
        projection_evidence=tuple(evidence),
        player_names={
            **source.player_names,
            **{player_id: player_id.upper() for player_id in cloned_from},
        },
        methodology_attestation=attestation,
    )


def trade(transaction_id="trade-1", recorded_at=TRADE_AT):
    return HistoryTransaction(
        transaction_id,
        recorded_at,
        HistoryTimestampBasis.ESPN_PROPOSED_DATE,
        1,
        HistoryTransactionKind.TRADE,
        (
            HistoryTransactionAsset(0, "p2", "primary", "other"),
            HistoryTransactionAsset(1, "q1", "other", "primary"),
        ),
    )


def acquisition(recorded_at):
    return HistoryTransaction(
        "free-agent-1",
        recorded_at,
        HistoryTimestampBasis.ESPN_PROPOSED_DATE,
        1,
        HistoryTransactionKind.FREE_AGENT,
        (HistoryTransactionAsset(0, "w1", None, "primary"),),
    )


def third_team_move(recorded_at):
    return HistoryTransaction(
        "third-team-free-agent-1",
        recorded_at,
        HistoryTimestampBasis.ESPN_PROPOSED_DATE,
        1,
        HistoryTransactionKind.FREE_AGENT,
        (
            HistoryTransactionAsset(0, "w1", None, "third"),
            HistoryTransactionAsset(1, "r2", "third", None),
        ),
    )


def capture(
    transactions,
    captured_at=REQUEST_AT,
    *,
    complete=True,
    injury_status=None,
):
    return LeagueHistoryCapture(
        league_key=LEAGUE_KEY,
        season=2026,
        captured_at=captured_at,
        coverage_start=min(SOURCE_AT - timedelta(days=1), captured_at - timedelta(days=1)),
        coverage_end=captured_at,
        transaction_history_complete=complete,
        roster_complete=complete,
        lineup_complete=complete,
        teams=(HistoryTeam("primary", "Primary"), HistoryTeam("other", "Other")),
        transactions=tuple(transactions),
        rosters=(
            HistoryTeamRoster(
                "primary",
                (
                    HistoryRosterPlayer("p1", "FLEX", injury_status),
                    HistoryRosterPlayer("p2", "BENCH", injury_status),
                ),
            ),
            HistoryTeamRoster(
                "other",
                (
                    HistoryRosterPlayer("q1", "FLEX", injury_status),
                    HistoryRosterPlayer("q2", "BENCH", injury_status),
                ),
            ),
        ),
    )


def history(source, requested, transactions, *, source_at=SOURCE_AT, complete=True):
    requested_binding = HistoryBundleBinding(
        LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
    )
    source_binding = HistoryBundleBinding(
        LEAGUE_KEY, 2026, source.bundle_id, source_at
    )
    return LeagueHistorySnapshot(
        requested_binding,
        (source_binding, requested_binding),
        (capture(transactions, complete=complete),),
    )


class HistoricalTradeValuationTests(unittest.TestCase):
    def test_history_indexes_preserve_strict_prior_binding_tie_break(self):
        lower = HistoryBundleBinding(
            LEAGUE_KEY, 2026, "engine_" + "1" * 64, SOURCE_AT
        )
        higher = HistoryBundleBinding(
            LEAGUE_KEY, 2026, "engine_" + "2" * 64, SOURCE_AT
        )
        at_trade = HistoryBundleBinding(
            LEAGUE_KEY, 2026, "engine_" + "3" * 64, TRADE_AT
        )
        indexes = valuation_module._HistoryIndexes.build(
            (at_trade, lower, higher), (), (), {}
        )

        self.assertEqual(
            valuation_module._prior_binding(indexes, TRADE_AT), higher
        )
        self.assertIsNone(
            valuation_module._prior_binding(indexes, SOURCE_AT)
        )

    def test_history_indexes_reuse_transaction_and_health_preparation(self):
        source = engine_bundle()
        requested = current_bundle(source)
        source_capture = capture(
            (), SOURCE_AT - timedelta(minutes=1), injury_status="ACTIVE"
        )
        requested_capture = capture((trade(),), injury_status="ACTIVE")
        captures = (source_capture, requested_capture)
        transactions, first_observed_at = (
            valuation_module.captured_transaction_evidence(captures)
        )
        bindings = (
            HistoryBundleBinding(
                LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
            ),
            HistoryBundleBinding(
                LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
            ),
        )

        with patch.object(
            valuation_module,
            "transaction_executed_by",
            wraps=valuation_module.transaction_executed_by,
        ) as executed_by:
            indexes = valuation_module._HistoryIndexes.build(
                bindings, captures, transactions, first_observed_at
            )
            for _ in range(2):
                self.assertTrue(
                    valuation_module._has_complete_transaction_window(
                        indexes, SOURCE_AT, trade()
                    )
                )
                _, _, playoff_context_known = valuation_module._rosters_at(
                    source,
                    indexes,
                    SOURCE_AT,
                    trade(),
                    ("other", "primary"),
                )
                self.assertTrue(playoff_context_known)
                self.assertEqual(
                    valuation_module._health_eligibility_reasons(
                        indexes,
                        frozenset({"p2", "q1"}),
                        SOURCE_AT,
                        REQUEST_AT,
                    ),
                    (),
                )

        self.assertEqual(executed_by.call_count, len(transactions))

    def test_playoff_baseline_is_reused_across_trades_from_one_weekly_model(self):
        source = engine_bundle()
        owners = {
            roster.team_id: list(roster.player_ids) for roster in source.rosters
        }
        exempt = {
            roster.team_id: set(roster.capacity_exempt_player_ids)
            for roster in source.rosters
        }
        second_trade = replace(
            trade("trade-2"),
            assets=(
                HistoryTransactionAsset(0, "p1", "primary", "other"),
                HistoryTransactionAsset(1, "q2", "other", "primary"),
            ),
        )
        cache = {}

        with patch.object(
            valuation_module,
            "prepare_season_baseline",
            wraps=valuation_module.prepare_season_baseline,
        ) as prepare:
            first = valuation_module._playoff_deltas(
                source,
                owners,
                exempt,
                trade(),
                ("other", "primary"),
                cache,
            )
            second = valuation_module._playoff_deltas(
                source,
                owners,
                exempt,
                second_trade,
                ("other", "primary"),
                cache,
            )

        self.assertIsNone(first[2])
        self.assertIsNone(second[2])
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(len(cache), 1)

    def test_two_team_trade_uses_strictly_prior_model_and_paired_playoff_run(self):
        source = engine_bundle()
        requested = current_bundle(source)
        snapshot = history(source, requested, (trade(),))

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        )

        self.assertEqual(result.unvalued_reasons, {})
        self.assertEqual(len(result.valuations), 1)
        valuation = result.valuations[0]
        self.assertEqual(valuation.source_bundle_id, source.bundle_id)
        self.assertEqual(valuation.source_bundle_captured_at, SOURCE_AT)
        self.assertEqual(valuation.valuation_lag_hours, 6)
        self.assertEqual(valuation.methodology_status, "exact")
        self.assertEqual(valuation.playoff_scenario_count, 5)
        self.assertIsNone(valuation.playoff_unavailable_reason)
        self.assertEqual([row.team_id for row in valuation.outcomes], ["other", "primary"])
        self.assertAlmostEqual(
            sum(row.relative_power_edge for row in valuation.outcomes), 0
        )
        self.assertTrue(
            all(row.playoff_probability_delta is not None for row in valuation.outcomes)
        )
        current = valuation.current_revaluation
        self.assertIsNotNone(current)
        self.assertEqual(current.bundle_id, requested.bundle_id)
        self.assertFalse(current.foresight_eligible)
        self.assertIn(
            "source_health_capture_missing",
            current.foresight_ineligibility_reasons,
        )
        for at_time, now in zip(valuation.outcomes, current.outcomes):
            self.assertEqual(at_time.team_id, now.team_id)
            self.assertAlmostEqual(
                now.relative_power_edge_drift,
                now.relative_power_edge - at_time.relative_power_edge,
            )

    def test_unsupported_trade_asset_rejects_the_entire_package(self):
        source = engine_bundle()
        requested = current_bundle(source)
        original = trade()
        mixed_trade = replace(
            original,
            assets=(
                *original.assets,
                HistoryTransactionAsset(
                    2,
                    None,
                    "other",
                    "primary",
                    "source_asset_" + "a" * 64,
                    HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER,
                ),
            ),
        )
        snapshot = history(source, requested, (mixed_trade,))

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader=lambda _bundle_id: self.fail(
                "an unsupported package must be rejected before model loading"
            ),
            current_bundle=requested,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_transactions,
            {"trade-1": "trade_contains_unsupported_or_unresolved_asset"},
        )

    def test_bundle_captured_at_trade_time_is_not_prior_evidence(self):
        source = engine_bundle()
        binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, TRADE_AT
        )
        snapshot = LeagueHistorySnapshot(
            binding,
            (binding,),
            (capture((trade(),), captured_at=TRADE_AT),),
        )

        result = value_historical_trades(
            snapshot,
            as_of=TRADE_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_reasons, {"no_strictly_prior_weekly_model": 1}
        )
        self.assertEqual(
            result.unvalued_transactions,
            {"trade-1": "no_strictly_prior_weekly_model"},
        )

    def test_intervening_participant_move_fails_closed_when_order_is_ambiguous(self):
        source = engine_bundle()
        requested = current_bundle(source)
        transactions = (
            acquisition(SOURCE_AT + timedelta(hours=2)),
            trade(),
        )
        snapshot = history(source, requested, transactions)

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_reasons,
            {"roster_or_player_evidence_is_incomplete": 1},
        )

    def test_intervening_third_team_move_suppresses_only_playoff_delta(self):
        source = four_team_bundle()
        requested = current_bundle(source)
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        source_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
        )
        transactions = (
            third_team_move(SOURCE_AT + timedelta(hours=2)),
            trade(),
        )
        complete_capture = LeagueHistoryCapture(
            league_key=LEAGUE_KEY,
            season=2026,
            captured_at=REQUEST_AT,
            coverage_start=SOURCE_AT - timedelta(days=1),
            coverage_end=REQUEST_AT,
            transaction_history_complete=True,
            roster_complete=True,
            lineup_complete=True,
            teams=(
                HistoryTeam("primary", "Primary"),
                HistoryTeam("other", "Other"),
                HistoryTeam("third", "Third"),
                HistoryTeam("fourth", "Fourth"),
            ),
            transactions=transactions,
            rosters=(
                HistoryTeamRoster(
                    "primary",
                    (
                        HistoryRosterPlayer("p1", "FLEX", None),
                        HistoryRosterPlayer("q1", "BENCH", None),
                    ),
                ),
                HistoryTeamRoster(
                    "other",
                    (
                        HistoryRosterPlayer("q2", "FLEX", None),
                        HistoryRosterPlayer("p2", "BENCH", None),
                    ),
                ),
                HistoryTeamRoster(
                    "third",
                    (
                        HistoryRosterPlayer("r1", "FLEX", None),
                        HistoryRosterPlayer("w1", "BENCH", None),
                    ),
                ),
                HistoryTeamRoster(
                    "fourth",
                    (
                        HistoryRosterPlayer("s1", "FLEX", None),
                        HistoryRosterPlayer("s2", "BENCH", None),
                    ),
                ),
            ),
        )
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (source_binding, requested_binding),
            (complete_capture,),
        )

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        )

        self.assertEqual(result.unvalued_reasons, {})
        self.assertEqual(len(result.valuations), 1)
        valuation = result.valuations[0]
        self.assertEqual(valuation.methodology_status, "exact")
        self.assertIsNone(valuation.playoff_scenario_count)
        self.assertEqual(
            valuation.playoff_unavailable_reason,
            "intervening_league_move_order_is_ambiguous",
        )
        self.assertTrue(
            all(
                outcome.playoff_probability_delta is None
                for outcome in valuation.outcomes
            )
        )
        self.assertIsNotNone(valuation.current_revaluation)

    def test_incomplete_transaction_window_withholds_historical_value(self):
        source = engine_bundle()
        requested = current_bundle(source)
        snapshot = history(source, requested, (trade(),), complete=False)

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader=lambda _bundle_id: self.fail(
                "an incomplete ledger must be rejected before model loading"
            ),
            current_bundle=requested,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_transactions,
            {"trade-1": "transaction_history_is_incomplete"},
        )

    def test_first_capture_execution_bound_catches_prebinding_proposal(self):
        source = engine_bundle()
        requested = current_bundle(source)
        proposed_before_binding = acquisition(SOURCE_AT - timedelta(hours=1))
        snapshot = history(
            source,
            requested,
            (proposed_before_binding, trade()),
        )

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_transactions,
            {"trade-1": "roster_or_player_evidence_is_incomplete"},
        )

    def test_same_timestamp_participant_move_also_fails_closed(self):
        source = engine_bundle()
        requested = current_bundle(source)
        same_time_move = acquisition(TRADE_AT)
        snapshot = history(source, requested, (same_time_move, trade()))

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_reasons,
            {"roster_or_player_evidence_is_incomplete": 1},
        )

    def test_loader_cannot_substitute_a_different_bundle_for_prior_evidence(self):
        source = engine_bundle()
        requested = current_bundle(source)
        snapshot = history(source, requested, (trade(),))

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader=lambda _bundle_id: requested,
            current_bundle=requested,
        )

        self.assertEqual(result.valuations, ())
        self.assertEqual(
            result.unvalued_reasons,
            {"prior_weekly_model_is_unavailable": 1},
        )

    def test_current_revaluation_uses_same_rosters_and_packages_but_current_values(self):
        source = engine_bundle()
        requested = drifted_current_bundle(source)
        snapshot = history(source, requested, (trade(),))

        result = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        )

        valuation = result.valuations[0]
        at_time = {row.team_id: row for row in valuation.outcomes}
        current = {
            row.team_id: row for row in valuation.current_revaluation.outcomes
        }
        self.assertNotEqual(
            at_time["primary"].relative_power_edge,
            current["primary"].relative_power_edge,
        )
        self.assertAlmostEqual(
            current["primary"].relative_power_edge_drift,
            current["primary"].relative_power_edge
            - at_time["primary"].relative_power_edge,
        )
        self.assertAlmostEqual(
            current["primary"].relative_power_edge_drift,
            -current["other"].relative_power_edge_drift,
        )

    def test_complete_active_health_captures_make_comparison_foresight_eligible(self):
        source = engine_bundle()
        requested = drifted_current_bundle(source)
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        source_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
        )
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (source_binding, requested_binding),
            (
                capture(
                    (),
                    SOURCE_AT - timedelta(minutes=1),
                    injury_status="ACTIVE",
                ),
                capture((trade(),), injury_status="ACTIVE"),
            ),
        )

        valuation = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        ).valuations[0]

        self.assertTrue(valuation.current_revaluation.foresight_eligible)
        self.assertEqual(
            valuation.current_revaluation.foresight_ineligibility_reasons, ()
        )

    def test_injury_observation_excludes_foresight_but_keeps_raw_revaluation(self):
        source = engine_bundle()
        requested = drifted_current_bundle(source)
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        source_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
        )
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (source_binding, requested_binding),
            (
                capture(
                    (),
                    SOURCE_AT - timedelta(minutes=1),
                    injury_status="ACTIVE",
                ),
                capture(
                    (),
                    TRADE_AT + timedelta(hours=2),
                    injury_status="QUESTIONABLE",
                ),
                capture((trade(),), injury_status="ACTIVE"),
            ),
        )

        valuation = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        ).valuations[0]

        self.assertIsNotNone(valuation.current_revaluation)
        self.assertFalse(valuation.current_revaluation.foresight_eligible)
        self.assertIn(
            "physical_injury_status_observed",
            valuation.current_revaluation.foresight_ineligibility_reasons,
        )

    def test_suspension_is_excluded_without_being_mislabeled_as_physical_injury(self):
        source = engine_bundle()
        requested = drifted_current_bundle(source)
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        source_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
        )
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (source_binding, requested_binding),
            (
                capture(
                    (),
                    SOURCE_AT - timedelta(minutes=1),
                    injury_status="ACTIVE",
                ),
                capture(
                    (),
                    TRADE_AT + timedelta(hours=2),
                    injury_status="SUSPENDED",
                ),
                capture((trade(),), injury_status="ACTIVE"),
            ),
        )

        valuation = value_historical_trades(
            snapshot,
            as_of=REQUEST_AT,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        ).valuations[0]
        reasons = valuation.current_revaluation.foresight_ineligibility_reasons

        self.assertIn("non_physical_unavailability_observed", reasons)
        self.assertNotIn("physical_injury_status_observed", reasons)

    def test_health_capture_gap_over_eight_days_excludes_foresight(self):
        source = engine_bundle()
        requested = drifted_current_bundle(source)
        late_request_at = TRADE_AT + timedelta(days=9)
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, late_request_at
        )
        source_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
        )
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (source_binding, requested_binding),
            (
                capture(
                    (),
                    SOURCE_AT - timedelta(minutes=1),
                    injury_status="ACTIVE",
                ),
                capture(
                    (trade(),), late_request_at, injury_status="ACTIVE"
                ),
            ),
        )

        valuation = value_historical_trades(
            snapshot,
            as_of=late_request_at,
            bundle_loader={source.bundle_id: source}.__getitem__,
            current_bundle=requested,
        ).valuations[0]

        self.assertIsNotNone(valuation.current_revaluation)
        self.assertFalse(valuation.current_revaluation.foresight_eligible)
        self.assertIn(
            "health_capture_gap_exceeds_eight_days",
            valuation.current_revaluation.foresight_ineligibility_reasons,
        )

    def test_current_revaluation_rejects_as_of_after_selected_bundle(self):
        source = engine_bundle()
        requested = current_bundle(source)
        snapshot = history(source, requested, (trade(),))

        with self.assertRaisesRegex(ValueError, "as_of must equal"):
            value_historical_trades(
                snapshot,
                as_of=REQUEST_AT + timedelta(minutes=1),
                bundle_loader={source.bundle_id: source}.__getitem__,
                current_bundle=requested,
            )


if __name__ == "__main__":
    unittest.main()
