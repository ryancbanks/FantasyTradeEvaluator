from dataclasses import replace
from datetime import datetime, timezone
import copy
import unittest

from tests.test_engine_bundle import engine_bundle
from trade_snapshot._scenario_random import content_id
from trade_snapshot.engine_bundle import EngineBundle, UnsupportedEngineBundleSchema
from trade_snapshot.ensemble import EnsembleConfig, ProviderWeight
from trade_snapshot.player_lab_projection_builder import (
    build_player_lab_projection_snapshot,
)
from trade_snapshot.player_lab_projections import (
    MAX_PLAYER_LAB_PROJECTION_PLAYERS,
    PlayerLabProjectionSnapshot,
)
from trade_snapshot.projections import ProjectionStatus, WeeklyProjection


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _weekly(player_id, provider, week, points, *, team="ARI"):
    return WeeklyProjection(
        canonical_player_id=player_id,
        snapshot_id="snapshot-1",
        scoring_profile_id=engine_bundle().state.scoring_profile_id,
        provider=provider,
        provider_player_id=f"{provider}-{player_id}",
        season=2026,
        week=week,
        status=ProjectionStatus.OBSERVED,
        captured_at=NOW,
        projected_fantasy_points=points,
        nfl_team_id=team,
    )


class PlayerLabProjectionSnapshotTests(unittest.TestCase):
    def test_builds_only_outside_calculation_players_and_round_trips(self):
        base = engine_bundle()
        state = base.state
        evidence = tuple(
            _weekly(player_id, provider, 1, points)
            for player_id, points in (("outside", 12.0), ("p1", 99.0))
            for provider in ("espn", "cbs")
        )
        config = EnsembleConfig(
            (ProviderWeight("espn", 1), ProviderWeight("cbs", 1)),
            2,
            {"RB": 1.0},
        )
        # This fixture covers ARI for the bundle's one remaining week.
        from tests.test_weekly_assembly import nfl_schedule

        snapshot = build_player_lab_projection_snapshot(
            state=state,
            projection_evidence=evidence,
            player_names={"outside": "Outside", "p1": "P1"},
            player_positions={"outside": "RB", "p1": "RB"},
            player_nfl_team_ids={"outside": "ARI", "p1": "ARI"},
            nfl_schedule=nfl_schedule(),
            ensemble_config=config,
            exclude_player_ids={"p1"},
        )

        self.assertEqual(snapshot.player_ids, ("outside",))
        self.assertEqual(snapshot.provider_names, ("espn", "cbs"))
        self.assertEqual(snapshot.projections[0].projected_fantasy_points, 12.0)
        self.assertEqual(
            {row.provider: row.captured_at for row in snapshot.provider_provenance},
            {"espn": NOW, "cbs": NOW},
        )
        self.assertEqual(
            PlayerLabProjectionSnapshot.from_record(snapshot.to_record()), snapshot
        )

    def test_rejects_duplicate_publishers_and_tampered_content(self):
        base = engine_bundle()
        source = next(row for row in base.projections if row.position == "RB")
        projection = replace(
            source,
            canonical_player_id="outside",
            nfl_team_id="NFL-outside",
            provider_observations=(
                replace(
                    source.provider_observations[0],
                    provider="espn",
                    provider_player_id="espn-outside",
                ),
            ),
        )
        snapshot = PlayerLabProjectionSnapshot(
            league_snapshot_id=base.state.snapshot_id,
            scoring_profile_id=base.state.scoring_profile_id,
            season=base.state.season,
            as_of_week=base.state.first_remaining_week,
            remaining_weeks=base.state.remaining_regular_season_weeks,
            provider_names=("espn",),
            projections=(projection,),
            player_names={"outside": "Outside"},
        )
        tampered = copy.deepcopy(snapshot.to_record())
        tampered["projections"][0]["projected_fantasy_points"] += 1
        with self.assertRaises(ValueError):
            PlayerLabProjectionSnapshot.from_record(tampered)

        wrong_schema = copy.deepcopy(snapshot.to_record())
        wrong_schema["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "schema"):
            PlayerLabProjectionSnapshot.from_record(wrong_schema)

        with self.assertRaisesRegex(ValueError, "duplicate"):
            replace(snapshot, provider_names=("espn", "espn"))

    def test_enforces_explicit_catalog_cardinality_bound(self):
        base = engine_bundle()
        projection = next(row for row in base.projections if row.position == "RB")
        rows = tuple(
            replace(projection, canonical_player_id=f"outside-{index}")
            for index in range(MAX_PLAYER_LAB_PROJECTION_PLAYERS + 1)
        )
        with self.assertRaisesRegex(ValueError, "player limit"):
            PlayerLabProjectionSnapshot(
                league_snapshot_id=base.state.snapshot_id,
                scoring_profile_id=base.state.scoring_profile_id,
                season=base.state.season,
                as_of_week=base.state.first_remaining_week,
                remaining_weeks=base.state.remaining_regular_season_weeks,
                provider_names=("fantasypros",),
                projections=rows,
                player_names={
                    f"outside-{index}": f"Outside {index}"
                    for index in range(MAX_PLAYER_LAB_PROJECTION_PLAYERS + 1)
                },
            )

    def test_rejects_incomplete_provider_and_player_identity_inputs(self):
        base = engine_bundle()
        from tests.test_weekly_assembly import nfl_schedule

        evidence = (_weekly("outside", "espn", 1, 12.0),)
        two_sources = EnsembleConfig(
            (ProviderWeight("espn", 1), ProviderWeight("cbs", 1)),
            1,
            {"RB": 1.0},
        )
        arguments = {
            "state": base.state,
            "projection_evidence": evidence,
            "player_names": {"outside": "Outside"},
            "player_positions": {"outside": "RB"},
            "player_nfl_team_ids": {"outside": "ARI"},
            "nfl_schedule": nfl_schedule(),
            "ensemble_config": two_sources,
        }
        with self.assertRaisesRegex(ValueError, "missing configured provider 'cbs'"):
            build_player_lab_projection_snapshot(**arguments)

        arguments["ensemble_config"] = EnsembleConfig(
            (ProviderWeight("espn", 1),), 1, {"RB": 1.0}
        )
        arguments["player_names"] = {}
        with self.assertRaisesRegex(ValueError, "missing a display name"):
            build_player_lab_projection_snapshot(**arguments)

    def test_retains_valid_weeks_when_another_week_misses_quorum(self):
        base = engine_bundle()
        state = replace(
            base.state,
            playoff_rules=replace(
                base.state.playoff_rules,
                regular_season_end_week=2,
                playoff_weeks=(3,),
            ),
            remaining_matchups=tuple(
                replace(matchup, week=week)
                for week in (1, 2)
                for matchup in base.state.remaining_matchups
            ),
        )
        from tests.test_weekly_assembly import nfl_schedule

        snapshot = build_player_lab_projection_snapshot(
            state=state,
            projection_evidence=tuple(
                _weekly("outside", provider, 1, 12.0)
                for provider in ("espn", "cbs")
            ),
            player_names={"outside": "Outside"},
            player_positions={"outside": "RB"},
            player_nfl_team_ids={"outside": "ARI"},
            nfl_schedule=nfl_schedule(),
            ensemble_config=EnsembleConfig(
                (ProviderWeight("espn", 1), ProviderWeight("cbs", 1)),
                2,
                {"RB": 1.0},
            ),
        )

        self.assertEqual(snapshot.player_ids, ("outside",))
        self.assertEqual([row.week for row in snapshot.projections], [1])
        self.assertEqual(snapshot.remaining_weeks, (1, 2))
        self.assertEqual(snapshot.insufficient_weeks_by_player["outside"], (2,))
        self.assertEqual(
            PlayerLabProjectionSnapshot.from_record(snapshot.to_record()), snapshot
        )


class EngineBundlePlayerLabProjectionTests(unittest.TestCase):
    def test_schema_ten_round_trip_nested_migration_and_schema_nine_rescan(self):
        base = engine_bundle()
        source = next(row for row in base.projections if row.position == "RB")
        projection = replace(
            source,
            canonical_player_id="outside",
            nfl_team_id="NFL-outside",
            provider_observations=(
                replace(
                    source.provider_observations[0],
                    provider="fantasypros",
                    provider_player_id="fantasypros-outside",
                ),
            ),
        )
        snapshot = PlayerLabProjectionSnapshot(
            league_snapshot_id=base.state.snapshot_id,
            scoring_profile_id=base.state.scoring_profile_id,
            season=base.state.season,
            as_of_week=base.state.first_remaining_week,
            remaining_weeks=base.state.remaining_regular_season_weeks,
            provider_names=("fantasypros",),
            projections=(projection,),
            player_names={"outside": "OUTSIDE"},
        )
        # A projection snapshot is paired with a public profile snapshot in a
        # production bundle; identity validation is covered in profile tests.
        from tests.test_player_profiles import profile_snapshot

        enriched = replace(
            base,
            player_profiles=profile_snapshot(*base.player_names, "outside"),
            player_lab_projections=snapshot,
        )
        record = enriched.to_record()
        self.assertEqual(record["schema_version"], 10)
        self.assertEqual(EngineBundle.from_record(record), enriched)

        legacy_snapshot = copy.deepcopy(snapshot.to_record())
        legacy_snapshot.pop("player_positions")
        legacy_snapshot.pop("player_nfl_team_ids")
        legacy_snapshot.pop("provider_provenance")
        legacy_snapshot["schema_version"] = 1
        legacy_snapshot["projection_snapshot_id"] = content_id(
            "player-lab-projections",
            {
                key: value
                for key, value in legacy_snapshot.items()
                if key != "projection_snapshot_id"
            },
        )
        migrated_snapshot = PlayerLabProjectionSnapshot.from_record(legacy_snapshot)
        self.assertEqual(migrated_snapshot.player_positions["outside"], "RB")
        self.assertEqual(migrated_snapshot.provider_provenance, ())

        nested_legacy = copy.deepcopy(record)
        nested_legacy["player_lab_projections"] = legacy_snapshot
        nested_legacy["bundle_id"] = content_id(
            "engine",
            {
                key: value
                for key, value in nested_legacy.items()
                if key not in {"kind", "schema_version", "bundle_id"}
            },
        )
        migrated_bundle = EngineBundle.from_record(nested_legacy)
        self.assertNotEqual(migrated_bundle.bundle_id, nested_legacy["bundle_id"])
        self.assertEqual(migrated_bundle.player_lab_projections, migrated_snapshot)

        legacy = base.to_record()
        legacy.pop("player_lab_projections")
        legacy["schema_version"] = 9
        legacy["bundle_id"] = content_id(
            "engine",
            {
                key: value
                for key, value in legacy.items()
                if key not in {"kind", "schema_version", "bundle_id"}
            },
        )
        with self.assertRaises(UnsupportedEngineBundleSchema) as raised:
            EngineBundle.from_record(legacy)
        self.assertEqual(raised.exception.schema_version, 9)


if __name__ == "__main__":
    unittest.main()
