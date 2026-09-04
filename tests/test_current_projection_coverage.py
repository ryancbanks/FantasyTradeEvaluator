from dataclasses import replace
from datetime import datetime, timezone
import unittest

from tests.test_feature_engineering import inputs
from tests.test_weekly_engine import (
    SCORING_PROFILE,
    build as build_weekly_bundle,
    raw_rows,
)
from trade_snapshot.data_readiness import build_bundle_data_readiness
from trade_snapshot.identity import ProviderReference
from trade_snapshot.player_profiles import (
    PlayerProfile,
    PlayerProfileProvenance,
    PlayerProfileSnapshot,
    ProfileMaterializationIssue,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


class CurrentProjectionCoverageTests(unittest.TestCase):
    def test_reconciles_reference_projection_and_roster_gaps_separately(self):
        _, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = tuple(
            row
            for row in raw_rows(ensembles)
            if not (
                row.canonical_player_id == "p2"
                and row.week == 1
                and row.provider == "yahoo"
            )
        )
        bundle = build_weekly_bundle(rows)
        enriched = _with_sleeper_reference(
            bundle,
            extras=(
                _profile("missing-rb", "Missing Runner", "RB", "NFL-MISSING"),
                _profile("missing-dst", "Missing Defense", "DST", "ARI"),
                _profile("unsupported", "Unsupported Lineman", "OL", "ARI"),
            ),
            issues=(
                ProfileMaterializationIssue(
                    "sleeper", "unresolved-source-id", "conflicting exact IDs"
                ),
            ),
        )

        audit = build_bundle_data_readiness(enriched)["coverage"][
            "current_player_projection_audit"
        ]

        self.assertEqual(audit["status"], "incomplete")
        self.assertEqual(audit["completeness_claim"], "not_complete")
        self.assertEqual(audit["reference"]["status"], "observed")
        self.assertFalse(audit["reference"]["identity_reconciliation_complete"])
        self.assertEqual(audit["reference"]["identity_issue_count"], 1)
        self.assertEqual(
            audit["counts"],
            {
                "reference_count": 6,
                "matched_count": 4,
                "projected_count": 4,
                "covered_count": 4,
                "partial_count": 0,
                "missing_count": 2,
                "unmatched_count": 2,
                "unsupported_count": 1,
            },
        )
        self.assertEqual(audit["unsupported_by_position"], {"OL": 1})
        self.assertIn("DST", audit["configured_positions"])
        self.assertIn("never counted as a numeric projection", audit["counting_policy"])

        rb_remaining = _coverage_row(
            audit, "RB", "ensemble", "remaining_season"
        )
        dst_remaining = _coverage_row(
            audit, "DST", "ensemble", "remaining_season"
        )
        yahoo_current = _coverage_row(audit, "ALL", "yahoo", "current_week")
        self.assertEqual(
            (rb_remaining["reference_count"], rb_remaining["covered_count"]),
            (5, 4),
        )
        self.assertEqual(
            (dst_remaining["reference_count"], dst_remaining["missing_count"]),
            (1, 1),
        )
        self.assertEqual(yahoo_current["projected_count"], 3)
        self.assertEqual(yahoo_current["missing_count"], 3)
        # P2 has a reconciled Yahoo identity but no usable current projection;
        # the two extra reference players have neither a match nor a projection.
        self.assertEqual(yahoo_current["unmatched_count"], 2)

        self.assertEqual(audit["rostered_player_count"], 2)
        self.assertEqual(audit["rostered_missing_current_projection_count"], 0)
        self.assertEqual(
            audit["rostered_missing_provider_current_projection_count"], 1
        )
        self.assertEqual(
            audit["rostered_players_missing_provider_current_projection"][0][
                "canonical_player_id"
            ],
            "p2",
        )
        self.assertEqual(
            audit["rostered_players_missing_provider_current_projection"][0][
                "missing_providers"
            ],
            ["yahoo"],
        )

    def test_claims_complete_only_when_every_scoped_identity_and_provider_is_complete(self):
        bundle = build_weekly_bundle()
        enriched = _with_sleeper_reference(bundle)

        audit = build_bundle_data_readiness(enriched)["coverage"][
            "current_player_projection_audit"
        ]

        self.assertEqual(audit["status"], "complete")
        self.assertEqual(
            audit["completeness_claim"], "complete_for_declared_scope"
        )
        self.assertTrue(audit["reference"]["identity_reconciliation_complete"])
        self.assertEqual(audit["counts"]["reference_count"], 4)
        self.assertEqual(audit["counts"]["projected_count"], 4)
        self.assertEqual(audit["counts"]["missing_count"], 0)
        self.assertTrue(
            all(
                row["missing_count"] == 0 and row["unmatched_count"] == 0
                for row in audit["coverage_rows"]
                if row["position"] == "ALL"
            )
        )

    def test_scope_is_configurable_and_absent_reference_never_claims_complete(self):
        bundle = build_weekly_bundle()
        enriched = _with_sleeper_reference(
            bundle,
            extras=(
                _profile("missing-dst", "Missing Defense", "DEF", "ARI"),
            ),
        )

        audit = build_bundle_data_readiness(
            enriched, player_projection_positions=("DST",)
        )["coverage"]["current_player_projection_audit"]

        self.assertEqual(audit["configured_positions"], ["DST"])
        self.assertEqual(audit["counts"]["reference_count"], 1)
        self.assertEqual(audit["counts"]["missing_count"], 1)
        self.assertEqual(audit["counts"]["unsupported_count"], 4)
        self.assertEqual(audit["status"], "incomplete")

        unavailable = build_bundle_data_readiness(bundle)["coverage"][
            "current_player_projection_audit"
        ]
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["completeness_claim"], "not_complete")
        with self.assertRaisesRegex(ValueError, "positions"):
            build_bundle_data_readiness(
                enriched, player_projection_positions=("RB", "RB")
            )


def _with_sleeper_reference(bundle, *, extras=(), issues=()):
    projections = {
        row.canonical_player_id: row for row in bundle.projections
    }
    players = tuple(
        _profile(
            player_id,
            bundle.player_names[player_id],
            projection.position,
            projection.nfl_team_id,
        )
        for player_id, projection in sorted(projections.items())
    ) + tuple(extras)
    snapshot = PlayerProfileSnapshot(
        league_snapshot_id=bundle.state.snapshot_id,
        season=bundle.state.season,
        as_of_week=bundle.state.first_remaining_week,
        captured_at=NOW,
        identity_registry_id="identity-reference-test",
        source_data_id="public-player-reference-test",
        current_stats_availability="observed",
        previous_stats_availability="observed",
        players=players,
        provenance=(
            PlayerProfileProvenance(
                provider="sleeper",
                dataset="sleeper_active_players",
                source_url="https://api.sleeper.app/v1/players/nfl?active=true",
                captured_at=NOW,
                source_updated_at=None,
                etag=None,
                status="observed",
                content_sha256="a" * 64,
                byte_count=123,
            ),
        ),
        materialization_issues=tuple(issues),
    )
    return replace(bundle, player_profiles=snapshot)


def _profile(player_id, name, position, team):
    return PlayerProfile(
        canonical_player_id=player_id,
        display_name=name,
        position=position,
        nfl_team_id=team,
        provider_references=(ProviderReference("sleeper", f"sleeper-{player_id}"),),
        fantasy_positions=(position,),
        active=True,
    )


def _coverage_row(audit, position, provider, horizon):
    return next(
        row
        for row in audit["coverage_rows"]
        if (row["position"], row["provider"], row["horizon"])
        == (position, provider, horizon)
    )


if __name__ == "__main__":
    unittest.main()
