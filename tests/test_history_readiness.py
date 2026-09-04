from dataclasses import replace
from datetime import timedelta
import json
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot.history_readiness import build_history_data_readiness
from trade_snapshot._league_history_acquisition import HistoryAcquisitionEvidence
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeam,
    HistoryTeamRoster,
    LeagueHistoryCapture,
    LeagueHistorySnapshot,
)
from trade_snapshot.weekly_collection import (
    WeeklyHistoryAttempt,
    WeeklyHistoryReason,
)


def exact_history(bundle):
    captured_at = bundle.source_manifest.host_captured_at
    teams = tuple(
        HistoryTeam(team.team_id, team.name) for team in bundle.state.teams
    )
    capture = LeagueHistoryCapture(
        league_key=bundle.source_manifest.league_binding_id,
        season=bundle.state.season,
        captured_at=captured_at,
        coverage_start=captured_at,
        coverage_end=captured_at,
        transaction_history_complete=True,
        roster_complete=True,
        lineup_complete=True,
        teams=teams,
        transactions=(),
        rosters=tuple(
            HistoryTeamRoster(
                roster.team_id,
                tuple(
                    HistoryRosterPlayer(player_id, "BENCH", "ACTIVE")
                    for player_id in roster.player_ids
                ),
            )
            for roster in bundle.rosters
        ),
        host_snapshot_id=bundle.source_manifest.host_snapshot_id,
    )
    binding = HistoryBundleBinding(
        bundle.source_manifest.league_binding_id,
        bundle.state.season,
        bundle.bundle_id,
        captured_at,
        bundle.source_manifest.host_snapshot_id,
        bundle.source_manifest.host_captured_at,
        capture.capture_id,
        capture.roster_ownership_id,
    )
    return LeagueHistorySnapshot(binding, (binding,), (capture,))


class HistoryReadinessTests(unittest.TestCase):
    def test_missing_optional_history_does_not_disable_current_roster_fit(self):
        result = build_history_data_readiness(engine_bundle(), None)

        self.assertEqual(result["status"], "history_unavailable_core_features_ready")
        self.assertEqual(
            result["capabilities"]["current_roster_compatibility"]["status"],
            "ready_with_holdout_validated_scope",
        )
        self.assertEqual(
            result["capabilities"]["completed_deal_activity"]["status"],
            "not_ready",
        )
        self.assertEqual(result["capabilities"]["trade_legality"]["status"], "not_ready")
        json.dumps(result, allow_nan=False, sort_keys=True)

    def test_failed_collection_reason_reaches_history_consumers(self):
        bundle = engine_bundle()
        attempt = WeeklyHistoryAttempt.unavailable(
            WeeklyHistoryReason.ACTIVITY_SCHEMA_UNSUPPORTED,
            bundle.source_manifest.host_captured_at,
        )

        result = build_history_data_readiness(
            bundle,
            None,
            collection_attempt=attempt,
        )

        activity = result["capabilities"]["completed_deal_activity"]
        self.assertEqual(result["collection_attempt"], attempt.to_record())
        self.assertIn(
            "history_collection_activity_schema_unsupported",
            activity["missing"],
        )

    def test_exact_capture_binding_enables_history_consumers(self):
        bundle = engine_bundle()
        result = build_history_data_readiness(bundle, exact_history(bundle))

        self.assertTrue(result["identity"]["bundle_history_identity_bound"])
        self.assertTrue(result["identity"]["exact_host_capture_binding"])
        self.assertEqual(
            result["capabilities"]["completed_deal_activity"]["status"],
            "ready",
        )
        self.assertEqual(
            result["capabilities"]["historical_trade_valuation"]["status"],
            "ready_with_per_transaction_gates",
        )
        self.assertEqual(
            result["capabilities"]["manager_roster_history"]["status"],
            "ready",
        )
        self.assertEqual(
            result["capabilities"]["manager_lineup_history"]["status"],
            "ready_at_capture_times",
        )
        self.assertEqual(
            result["capabilities"]["historical_foresight"]["status"],
            "eligible_for_per_trade_screening",
        )

    def test_readiness_uses_the_capture_named_by_the_bundle_binding(self):
        bundle = engine_bundle()
        history = exact_history(bundle)
        bound_capture = history.captures[0]
        analysis_at = bound_capture.captured_at + timedelta(minutes=30)
        binding = replace(history.requested_binding, captured_at=analysis_at)
        unrelated_at = bound_capture.captured_at + timedelta(minutes=15)
        unrelated = replace(
            bound_capture,
            captured_at=unrelated_at,
            coverage_start=unrelated_at,
            coverage_end=unrelated_at,
            transaction_history_complete=False,
            roster_complete=False,
            lineup_complete=False,
            host_snapshot_id="unrelated-host-snapshot",
            acquisition_evidence=HistoryAcquisitionEvidence.legacy_unknown(
                unrelated_at,
                0,
            ),
        )
        history = LeagueHistorySnapshot(
            binding,
            (binding,),
            (bound_capture, unrelated),
        )

        result = build_history_data_readiness(bundle, history)

        activity = result["capabilities"]["completed_deal_activity"]
        self.assertEqual(activity["status"], "ready")
        self.assertEqual(
            activity["evidence"]["bound_capture_id"],
            bound_capture.capture_id,
        )
        self.assertTrue(activity["evidence"]["transaction_history_complete"])

    def test_bundle_identity_mismatch_fails_closed(self):
        bundle = engine_bundle()
        history = exact_history(bundle)
        wrong_binding = replace(
            history.requested_binding,
            bundle_id="engine_" + "9" * 64,
        )
        mismatched = LeagueHistorySnapshot(
            wrong_binding,
            (wrong_binding,),
            history.captures,
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            build_history_data_readiness(bundle, mismatched)


if __name__ == "__main__":
    unittest.main()
