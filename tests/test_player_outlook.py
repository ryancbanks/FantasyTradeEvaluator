from dataclasses import replace
from datetime import timedelta
import json
import unittest

from tests.test_engine_bundle import (
    NOW,
    engine_bundle,
    nfl_schedule_for,
    rebuild_bundle_inputs,
    ros_derived_bundle,
)
from tests.source_fixtures import projection_source_manifest
from trade_snapshot.ecr import EcrPeriod
from trade_snapshot.engine_bundle import EngineBundle
from trade_snapshot.ensemble import (
    EnsembleConfig,
    ProviderWeight,
    fuse_weekly_projections,
)
from trade_snapshot.nfl_schedule import NflSchedule, NflTeamWeek, NflTeamWeekStatus
from trade_snapshot.player_outlook import build_player_outlook
from trade_snapshot.projections import (
    ProjectionStatus,
    ProviderStatusObservation,
    ProviderStatusScope,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)


PROVIDERS = ("fantasypros", "espn", "yahoo")
CAPTURED = {
    provider: NOW + timedelta(minutes=10 + index)
    for index, provider in enumerate(PROVIDERS)
}
PUBLISHED = {
    provider: CAPTURED[provider] - timedelta(hours=1)
    for provider in PROVIDERS
}


def _weekly(source, provider, points, status=ProjectionStatus.OBSERVED, *, origin=None):
    return WeeklyProjection(
        canonical_player_id=source.canonical_player_id,
        snapshot_id=source.snapshot_id,
        scoring_profile_id=source.scoring_profile_id,
        provider=provider,
        provider_player_id=f"{provider}-{source.canonical_player_id}",
        season=source.season,
        week=source.week,
        status=status,
        captured_at=CAPTURED[provider],
        projected_fantasy_points=points,
        raw_projected_stats={} if points is None else {"points": points},
        nfl_team_id=source.nfl_team_id,
        nfl_game_id=source.nfl_game_id,
        opponent_team_id=source.opponent_team_id,
        is_home=source.is_home,
        source_published_at=PUBLISHED[provider],
        origin=origin or WeeklyProjectionOrigin.PROVIDER_PUBLISHED,
    )


def _bye(source, provider):
    return WeeklyProjection(
        canonical_player_id=source.canonical_player_id,
        snapshot_id=source.snapshot_id,
        scoring_profile_id=source.scoring_profile_id,
        provider=provider,
        provider_player_id=f"{provider}-{source.canonical_player_id}",
        season=source.season,
        week=source.week,
        status=ProjectionStatus.BYE,
        captured_at=CAPTURED[provider],
        nfl_team_id=source.nfl_team_id,
        source_published_at=PUBLISHED[provider],
    )


def _remaining(
    source,
    provider,
    points,
    status=ProjectionStatus.OBSERVED,
    *,
    applicable_weeks=None,
):
    return RemainingSeasonProjection(
        canonical_player_id=source.canonical_player_id,
        snapshot_id=source.snapshot_id,
        scoring_profile_id=source.scoring_profile_id,
        provider=provider,
        provider_player_id=f"{provider}-{source.canonical_player_id}",
        season=source.season,
        applicable_weeks=(
            tuple(applicable_weeks)
            if applicable_weeks is not None
            else (source.week,) if status is not ProjectionStatus.NOT_APPLICABLE else ()
        ),
        status=status,
        origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
        captured_at=CAPTURED[provider],
        projected_fantasy_points=points,
        raw_projected_stats={} if points is None else {"points": points},
        source_published_at=PUBLISHED[provider],
    )


def player_bundle():
    base = engine_bundle()
    floors = {row.position: 0.5 for row in base.projections}
    config = EnsembleConfig(
        tuple(ProviderWeight(provider, 1.0) for provider in PROVIDERS),
        2,
        floors,
    )
    projections = []
    evidence = []
    for source in base.projections:
        base_points = source.projected_fantasy_points
        fantasypros = _weekly(source, "fantasypros", base_points)
        espn = _weekly(source, "espn", base_points + 1)
        yahoo = _weekly(
            source,
            "yahoo",
            base_points - 1,
            origin=WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON,
        )
        rows = (fantasypros, espn, yahoo)
        evidence.extend((fantasypros, espn))
        evidence.extend(
            (
                _remaining(source, "fantasypros", base_points),
                _remaining(source, "espn", base_points + 1),
                _remaining(source, "yahoo", base_points - 1),
            )
        )
        projections.append(fuse_weekly_projections(rows, source.position, config))
    return rebuild_bundle_inputs(
        base,
        projections=tuple(projections),
        projection_evidence=tuple(evidence),
        nfl_schedule=_schedule_for(projections),
        ensemble_config=config,
    )


def _schedule_for(projections):
    rows = []
    seen = set()
    weeks = set(range(1, 19))
    for projection in projections:
        key = (projection.nfl_team_id, projection.week)
        if key in seen:
            continue
        seen.add(key)
        if projection.status is ProjectionStatus.BYE:
            rows.append(
                NflTeamWeek(
                    projection.nfl_team_id,
                    projection.week,
                    NflTeamWeekStatus.BYE,
                )
            )
            continue
        rows.extend(
            (
                NflTeamWeek(
                    projection.nfl_team_id,
                    projection.week,
                    NflTeamWeekStatus.SCHEDULED,
                    projection.nfl_game_id,
                    projection.opponent_team_id,
                    projection.is_home,
                ),
                NflTeamWeek(
                    projection.opponent_team_id,
                    projection.week,
                    NflTeamWeekStatus.SCHEDULED,
                    projection.nfl_game_id,
                    projection.nfl_team_id,
                    not projection.is_home,
                ),
            )
        )
    teams = {row.nfl_team_id for row in rows}
    covered = {(row.nfl_team_id, row.week) for row in rows}
    rows.extend(
        NflTeamWeek(team_id, week, NflTeamWeekStatus.BYE)
        for team_id in teams
        for week in weeks
        if (team_id, week) not in covered
    )
    return NflSchedule(2026, NOW, "espn", tuple(rows))


def multiweek_player_bundle():
    base = engine_bundle()
    weeks = (1, 2, 3, 4)
    state = replace(
        base.state,
        playoff_rules=replace(
            base.state.playoff_rules,
            regular_season_end_week=4,
            playoff_weeks=(5,),
        ),
        remaining_matchups=tuple(
            replace(matchup, week=week)
            for week in weeks
            for matchup in base.state.remaining_matchups
        ),
    )
    config = EnsembleConfig(
        tuple(ProviderWeight(provider, 1.0) for provider in PROVIDERS),
        2,
        {row.position: 0.5 for row in base.projections},
    )
    projections = []
    evidence = []
    for source in base.projections:
        base_points = source.projected_fantasy_points
        provider_points = {provider: [] for provider in PROVIDERS}
        for week in weeks:
            scheduled = replace(
                source,
                week=week,
                nfl_game_id=f"G{week}-{source.canonical_player_id}",
                is_home=week % 2 == 1,
            )
            if week == 4:
                rows = tuple(_bye(scheduled, provider) for provider in PROVIDERS)
                evidence.extend(rows)
            else:
                points = {
                    "fantasypros": base_points + week,
                    "espn": base_points + week + 1,
                    "yahoo": base_points + 2 if week == 1 else base_points - 0.5,
                }
                rows = tuple(
                    _weekly(
                        scheduled,
                        provider,
                        points[provider],
                        origin=(
                            WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON
                            if provider == "yahoo" and week in {2, 3}
                            else WeeklyProjectionOrigin.PROVIDER_PUBLISHED
                        ),
                    )
                    for provider in PROVIDERS
                )
                evidence.extend(rows[:2])
                if week == 1:
                    evidence.append(rows[2])
                for provider in PROVIDERS:
                    provider_points[provider].append(points[provider])
            projections.append(fuse_weekly_projections(rows, source.position, config))
        for provider in PROVIDERS:
            evidence.append(
                _remaining(
                    source,
                    provider,
                    sum(provider_points[provider]),
                    applicable_weeks=(1, 2, 3),
                )
            )
    projections = tuple(projections)
    return rebuild_bundle_inputs(
        base,
        state=state,
        projections=projections,
        projection_evidence=tuple(evidence),
        nfl_schedule=_schedule_for(projections),
        ensemble_config=config,
    )


def _player(result, player_id):
    return next(row for row in result["players"] if row["player_id"] == player_id)


class PlayerOutlookTests(unittest.TestCase):
    def test_valid_bundle_reports_matching_raw_provider_provenance(self):
        result = build_player_outlook(engine_bundle())
        self.assertEqual(result["providers"], [{
            "provider": "fantasypros",
            "label": "FantasyPros",
            "captured_at": "2026-09-01T18:00:00.000000Z",
            "source_published_at": None,
        }])
        value = result["players"][0]["weeks"][0]["provider_values"][0]
        self.assertEqual(value["origin"], "provider_published")
        self.assertEqual(value["captured_at"], "2026-09-01T18:00:00.000000Z")
        self.assertEqual(result["players"][0]["all_direct_week_count"], 1)
        self.assertEqual(value["status"], "observed")
        ros = result["players"][0]["provider_remaining_season"][0]
        self.assertEqual(ros["status"], "observed")
        self.assertEqual(
            ros["provider_player_id"],
            f"fantasypros-{result['players'][0]['player_id']}",
        )

    def test_missing_provider_evidence_cannot_enter_a_portable_bundle(self):
        bundle = player_bundle()
        target = next(
            row for row in bundle.projections if row.canonical_player_id == "p1"
        )
        reduced = fuse_weekly_projections(
            (
                _weekly(target, "fantasypros", 12.0),
                _weekly(
                    target,
                    "espn",
                    None,
                    ProjectionStatus.NOT_PUBLISHED,
                ),
                _weekly(
                    target,
                    "yahoo",
                    11.0,
                    origin=WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON,
                ),
            ),
            target.position,
            bundle.ensemble_config,
        )
        projections = tuple(
            reduced if row is target else row for row in bundle.projections
        )
        evidence = tuple(
            row
            for row in bundle.projection_evidence
            if not (
                isinstance(row, WeeklyProjection)
                and row.canonical_player_id == "p1"
                and row.provider == "espn"
            )
        )

        with self.assertRaisesRegex(ValueError, "does not reconcile to ROS evidence"):
            rebuild_bundle_inputs(
                bundle,
                projections=projections,
                projection_evidence=evidence,
            )

    def test_builds_deterministic_json_safe_contract_for_every_player(self):
        bundle = player_bundle()
        first = build_player_outlook(bundle)
        second = build_player_outlook(bundle)

        self.assertEqual(first, second)
        json.dumps(first, allow_nan=False)
        self.assertEqual(
            set(first),
            {
                "schema_version",
                "bundle_id",
                "snapshot_id",
                "season",
                "first_remaining_week",
                "weeks",
                "providers",
                "raw_stat_key_fields",
                "provider_status_observation_policy",
                "ecr_snapshots",
                "waiver_scope_notice",
                "players",
            },
        )
        self.assertEqual(first["schema_version"], 5)
        self.assertEqual(first["raw_stat_key_fields"], ["provider", "stat_name"])
        self.assertIn(
            "not converted into certain availability",
            first["provider_status_observation_policy"],
        )
        self.assertEqual(first["bundle_id"], bundle.bundle_id)
        self.assertEqual(first["weeks"], [1])
        self.assertEqual(
            [row["provider"] for row in first["providers"]],
            ["fantasypros", "espn", "yahoo"],
        )
        self.assertEqual(
            {row["player_id"] for row in first["players"]},
            {row.canonical_player_id for row in bundle.projections},
        )
        self.assertEqual(len(first["players"]), len(bundle.projections))

        player = _player(first, "p1")
        self.assertEqual(
            set(player),
            {
                "player_id",
                "name",
                "position",
                "eligible_slots",
                "nfl_team_id",
                "owner",
                "availability",
                "weekly_ecr",
                "rest_of_season_ecr",
                "remaining_projected_points",
                "remaining_projected_week_count",
                "remaining_projection_status",
                "remaining_fantasy_regular_season_points",
                "unmaterialized_remaining_points",
                "average_weekly_points",
                "average_fantasy_regular_season_points",
                "average_provider_disagreement",
                "average_predictive_uncertainty",
                "provider_complete_week_count",
                "all_direct_week_count",
                "provider_status_disagreement_week_count",
                "provider_status_coverage_complete_week_count",
                "provider_status_unknown_provider_week_count",
                "total_week_count",
                "weeks",
                "provider_remaining_season",
            },
        )
        self.assertEqual(player["owner"], {"team_id": "primary", "team_name": "Primary"})
        self.assertEqual(player["availability"], "rostered")
        self.assertEqual(player["provider_complete_week_count"], 1)
        self.assertEqual(player["all_direct_week_count"], 0)
        self.assertEqual(player["remaining_projected_points"], 12.0)
        self.assertEqual(player["remaining_projected_week_count"], 1)
        self.assertEqual(player["remaining_projection_status"], "complete")
        self.assertEqual(player["remaining_fantasy_regular_season_points"], 12.0)
        self.assertEqual(player["unmaterialized_remaining_points"], 0.0)
        self.assertEqual(player["average_weekly_points"], 12.0)
        self.assertEqual(player["average_fantasy_regular_season_points"], 12.0)
        self.assertEqual(
            set(player["weeks"][0]),
            {
                "week",
                "status",
                "opponent_team_id",
                "is_home",
                "projected_points",
                "between_provider_stddev",
                "predictive_stddev",
                "observed_source_count",
                "minimum_observed_sources",
                "usable_source_count",
                "direct_source_count",
                "derived_source_count",
                "unattributed_source_count",
                "not_retained_source_count",
                "provider_status_observation_count",
                "provider_status_expected_provider_count",
                "provider_status_reporting_provider_count",
                "provider_status_unknown_provider_count",
                "provider_status_coverage_complete",
                "provider_status_disagreement",
                "provider_values",
            },
        )
        self.assertEqual(
            set(player["weeks"][0]["provider_values"][0]),
            {
                "provider",
                "provider_player_id",
                "status",
                "projected_points",
                "weight",
                "origin",
                "captured_at",
                "source_published_at",
                "raw_projected_stats",
                "provider_status_observations",
            },
        )
        provider_values = {
            row["provider"]: row for row in player["weeks"][0]["provider_values"]
        }
        self.assertEqual(
            provider_values["fantasypros"]["raw_projected_stats"],
            {"points": 12.0},
        )
        self.assertEqual(
            provider_values["espn"]["raw_projected_stats"],
            {"points": 13.0},
        )
        self.assertEqual(provider_values["yahoo"]["raw_projected_stats"], {})
        self.assertEqual(player["provider_status_coverage_complete_week_count"], 0)
        self.assertEqual(player["provider_status_unknown_provider_week_count"], 3)
        self.assertEqual(
            player["weeks"][0]["provider_status_expected_provider_count"], 3
        )
        self.assertEqual(
            player["weeks"][0]["provider_status_reporting_provider_count"], 0
        )
        self.assertEqual(
            player["weeks"][0]["provider_status_unknown_provider_count"], 3
        )
        self.assertFalse(player["weeks"][0]["provider_status_coverage_complete"])

        waiver = _player(first, "w1")
        self.assertIsNone(waiver["owner"])
        self.assertEqual(waiver["availability"], "waiver_pool")
        self.assertIn("bounded waiver pool", first["waiver_scope_notice"].lower())

    def test_reconciles_ecr_and_source_metadata(self):
        result = build_player_outlook(player_bundle())
        player = _player(result, "p1")

        expected_ecr = {
            "rank": 1,
            "position_rank": 1,
            "rank_min": 1,
            "rank_max": 3,
            "rank_average": 2.0,
            "rank_stddev": 1.0,
        }
        self.assertEqual(player["weekly_ecr"], expected_ecr)
        self.assertEqual(player["rest_of_season_ecr"], expected_ecr)
        self.assertEqual(
            [row["period"] for row in result["ecr_snapshots"]],
            [EcrPeriod.WEEKLY.value, EcrPeriod.REST_OF_SEASON.value],
        )
        self.assertTrue(all(row["expert_count"] == 2 for row in result["ecr_snapshots"]))
        self.assertTrue(
            all(row["selected_expert_count"] == 2 for row in result["ecr_snapshots"])
        )
        self.assertTrue(
            all(
                row["expert_population_mode"] == "position_specific"
                for row in result["ecr_snapshots"]
            )
        )
        self.assertTrue(
            all(
                row["expert_panels"]
                == [{
                    "position": "FLEX",
                    "expert_count": 2,
                    "expert_ids": ["22", "9"],
                    "expert_selection_policy": "fantasypros_latest_ecr_v1",
                    "expert_group_title": "Latest ECR",
                    "expert_group_description": (
                        "More accurate experts with recent updates"
                    ),
                }]
                for row in result["ecr_snapshots"]
            )
        )
        fantasypros = result["providers"][0]
        self.assertEqual(fantasypros["label"], "FantasyPros")
        self.assertEqual(fantasypros["captured_at"], "2026-09-01T18:10:00.000000Z")
        self.assertEqual(
            fantasypros["source_published_at"],
            "2026-09-01T17:10:00.000000Z",
        )

    def test_infers_direct_and_rest_of_season_derived_week_provenance(self):
        player = _player(build_player_outlook(player_bundle()), "p1")
        week = player["weeks"][0]
        values = {row["provider"]: row for row in week["provider_values"]}

        self.assertEqual(values["fantasypros"]["origin"], "provider_published")
        self.assertEqual(
            values["fantasypros"]["captured_at"],
            "2026-09-01T18:10:00.000000Z",
        )
        self.assertEqual(values["yahoo"]["origin"], "derived_rest_of_season")
        self.assertEqual(values["yahoo"]["projected_points"], 11.0)
        self.assertEqual(week["usable_source_count"], 3)
        self.assertEqual(week["direct_source_count"], 2)
        self.assertEqual(week["derived_source_count"], 1)
        self.assertEqual(
            values["yahoo"]["source_published_at"],
            "2026-09-01T17:12:00.000000Z",
        )
        ros = {row["provider"]: row for row in player["provider_remaining_season"]}
        self.assertEqual(
            set(ros["yahoo"]),
            {
                "provider",
                "provider_player_id",
                "status",
                "projected_points",
                "origin",
                "applicable_weeks",
                "captured_at",
                "source_published_at",
                "raw_projected_stats",
                "provider_status_observations",
            },
        )
        self.assertEqual(ros["yahoo"]["origin"], "provider_published")
        self.assertEqual(ros["yahoo"]["applicable_weeks"], [1])
        self.assertEqual(ros["yahoo"]["projected_points"], 11.0)
        self.assertEqual(ros["fantasypros"]["raw_projected_stats"], {"points": 12.0})
        self.assertEqual(ros["espn"]["raw_projected_stats"], {"points": 13.0})
        self.assertEqual(ros["yahoo"]["raw_projected_stats"], {"points": 11.0})

    def test_exposes_provider_status_as_scoped_observations_not_availability(self):
        bundle = player_bundle()
        designations = {
            ("fantasypros", "weekly"): "Questionable",
            ("espn", "weekly"): "Out",
            ("yahoo", "remaining"): "Doubtful",
        }
        evidence = []
        for row in bundle.projection_evidence:
            key = (
                row.provider,
                "weekly" if isinstance(row, WeeklyProjection) else "remaining",
            )
            if row.canonical_player_id == "p1" and key in designations:
                scope = (
                    ProviderStatusScope.WEEKLY
                    if isinstance(row, WeeklyProjection)
                    else ProviderStatusScope.REST_OF_SEASON
                )
                row = replace(
                    row,
                    provider_status_observations=(
                        ProviderStatusObservation(
                            designations[key],
                            row.captured_at,
                            scope,
                            row.week if isinstance(row, WeeklyProjection) else None,
                        ),
                    ),
                )
            evidence.append(row)

        result = build_player_outlook(
            rebuild_bundle_inputs(bundle, projection_evidence=tuple(evidence))
        )
        player = _player(result, "p1")
        values = {
            row["provider"]: row
            for row in player["weeks"][0]["provider_values"]
        }

        self.assertEqual(player["availability"], "rostered")
        self.assertEqual(
            player["provider_status_disagreement_week_count"],
            1,
        )
        self.assertTrue(player["weeks"][0]["provider_status_disagreement"])
        self.assertEqual(
            player["weeks"][0]["provider_status_reporting_provider_count"], 3
        )
        self.assertEqual(
            player["weeks"][0]["provider_status_unknown_provider_count"], 0
        )
        self.assertTrue(player["weeks"][0]["provider_status_coverage_complete"])
        self.assertEqual(
            values["fantasypros"]["provider_status_observations"],
            [{
                "designation": "Questionable",
                "captured_at": "2026-09-01T18:10:00.000000Z",
                "source_scope": "weekly",
                "source_week": 1,
            }],
        )
        self.assertEqual(
            values["yahoo"]["provider_status_observations"][0]["source_scope"],
            "ros",
        )
        self.assertIn(
            "not converted into certain availability",
            result["provider_status_observation_policy"],
        )

    def test_incomplete_status_coverage_is_not_presented_as_agreement(self):
        bundle = player_bundle()
        evidence = []
        for row in bundle.projection_evidence:
            if (
                row.canonical_player_id == "p1"
                and row.provider == "fantasypros"
                and isinstance(row, WeeklyProjection)
            ):
                row = replace(
                    row,
                    provider_status_observations=(
                        ProviderStatusObservation(
                            "Questionable",
                            row.captured_at,
                            ProviderStatusScope.WEEKLY,
                            row.week,
                        ),
                    ),
                )
            evidence.append(row)

        result = build_player_outlook(
            rebuild_bundle_inputs(bundle, projection_evidence=tuple(evidence))
        )
        player = _player(result, "p1")
        week = player["weeks"][0]

        self.assertEqual(week["provider_status_reporting_provider_count"], 1)
        self.assertEqual(week["provider_status_unknown_provider_count"], 2)
        self.assertFalse(week["provider_status_coverage_complete"])
        self.assertFalse(week["provider_status_disagreement"])
        self.assertEqual(player["provider_status_coverage_complete_week_count"], 0)
        self.assertEqual(player["provider_status_unknown_provider_week_count"], 2)
        self.assertIn(
            "incomplete coverage is not an agreement",
            result["provider_status_observation_policy"],
        )

    def test_full_horizon_card_uses_complete_published_weekly_rows(self):
        bundle = player_bundle()
        target = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, RemainingSeasonProjection)
            and row.canonical_player_id == "p1"
            and row.provider == "fantasypros"
        )
        evidence = tuple(
            replace(
                row,
                status=ProjectionStatus.NOT_PUBLISHED,
                projected_fantasy_points=None,
                raw_projected_stats={},
            )
            if row is target
            else row
            for row in bundle.projection_evidence
        )

        player = _player(
            build_player_outlook(
                rebuild_bundle_inputs(bundle, projection_evidence=evidence)
            ),
            "p1",
        )
        fantasypros = next(
            row
            for row in player["provider_remaining_season"]
            if row["provider"] == "fantasypros"
        )

        self.assertEqual(fantasypros["status"], "observed")
        self.assertEqual(fantasypros["origin"], "derived_weekly")
        self.assertEqual(fantasypros["applicable_weeks"], [1])
        self.assertEqual(fantasypros["projected_points"], 12.0)
        self.assertEqual(fantasypros["raw_projected_stats"], {"points": 12.0})
        self.assertEqual(
            fantasypros["captured_at"],
            "2026-09-01T18:10:00.000000Z",
        )

    def test_weekly_sum_keeps_only_raw_stats_published_in_every_week(self):
        bundle = multiweek_player_bundle()
        evidence = []
        for row in bundle.projection_evidence:
            if (
                isinstance(row, RemainingSeasonProjection)
                and row.canonical_player_id == "p1"
                and row.provider == "fantasypros"
            ):
                row = replace(
                    row,
                    status=ProjectionStatus.NOT_PUBLISHED,
                    projected_fantasy_points=None,
                    raw_projected_stats={},
                )
            elif (
                isinstance(row, WeeklyProjection)
                and row.canonical_player_id == "p1"
                and row.provider == "fantasypros"
                and row.status is ProjectionStatus.OBSERVED
            ):
                row = replace(
                    row,
                    raw_projected_stats={
                        "points": row.projected_fantasy_points,
                        **({"carries": 5.0} if row.week == 1 else {}),
                    },
                )
            evidence.append(row)

        player = _player(
            build_player_outlook(
                rebuild_bundle_inputs(
                    bundle,
                    projection_evidence=tuple(evidence),
                )
            ),
            "p1",
        )
        fantasypros = next(
            row
            for row in player["provider_remaining_season"]
            if row["provider"] == "fantasypros"
        )

        self.assertEqual(fantasypros["origin"], "derived_weekly")
        self.assertEqual(fantasypros["applicable_weeks"], [1, 2, 3])
        self.assertEqual(
            fantasypros["raw_projected_stats"],
            {
                "points": sum(
                    row.projected_fantasy_points
                    for row in evidence
                    if isinstance(row, WeeklyProjection)
                    and row.canonical_player_id == "p1"
                    and row.provider == "fantasypros"
                    and row.status is ProjectionStatus.OBSERVED
                )
            },
        )

    def test_reports_ros_lineage_that_accounts_for_future_direct_rows(self):
        player = _player(build_player_outlook(ros_derived_bundle()), "p1")
        source = player["weeks"][0]["provider_values"][0]

        self.assertEqual(source["origin"], "derived_rest_of_season")
        self.assertEqual(source["projected_points"], 5.0)
        self.assertEqual(source["captured_at"], "2026-09-01T18:00:00.000000Z")

    def test_rejects_partial_direct_weekly_evidence_without_ros_rows(self):
        base = engine_bundle()
        evidence = tuple(
            row
            for row in base.projection_evidence
            if isinstance(row, WeeklyProjection)
        )
        with self.assertRaisesRegex(ValueError, "full_ros|full-season|remaining-season|horizon"):
            rebuild_bundle_inputs(base, projection_evidence=evidence)

    def test_separates_full_nfl_ros_from_the_fantasy_regular_season_slice(self):
        bundle = engine_bundle()
        source = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, RemainingSeasonProjection)
            and row.canonical_player_id == "p1"
        )
        evidence = tuple(
            replace(
                row,
                applicable_weeks=tuple(range(1, 19)),
                projected_fantasy_points=(
                    40.0 if row is source else row.projected_fantasy_points
                ),
            )
            if isinstance(row, RemainingSeasonProjection)
            else row
            for row in bundle.projection_evidence
        )
        player = _player(
            build_player_outlook(
                rebuild_bundle_inputs(
                    bundle,
                    projection_evidence=evidence,
                    nfl_schedule=nfl_schedule_for(bundle.projections),
                )
            ),
            "p1",
        )

        self.assertEqual(player["remaining_projected_points"], 40.0)
        self.assertEqual(player["remaining_projected_week_count"], 18)
        self.assertEqual(player["remaining_projection_status"], "complete")
        self.assertEqual(player["remaining_fantasy_regular_season_points"], 12.0)
        self.assertEqual(player["unmaterialized_remaining_points"], 28.0)
        self.assertAlmostEqual(player["average_weekly_points"], 40.0 / 18.0)
        self.assertEqual(player["average_fantasy_regular_season_points"], 12.0)

    def test_preserves_bye_values_as_null(self):
        bye = _player(build_player_outlook(multiweek_player_bundle()), "p1")
        bye_week = next(row for row in bye["weeks"] if row["week"] == 4)
        self.assertEqual(bye_week["status"], "bye")
        self.assertIsNone(bye_week["projected_points"])
        self.assertEqual(bye_week["usable_source_count"], 3)
        self.assertEqual(bye_week["direct_source_count"], 3)
        self.assertTrue(
            all(value["projected_points"] is None for value in bye_week["provider_values"])
        )

    def test_reconciles_multiweek_residual_allocations_and_direct_coverage(self):
        player = _player(build_player_outlook(multiweek_player_bundle()), "p1")
        self.assertEqual(player["provider_complete_week_count"], 4)
        self.assertEqual(player["all_direct_week_count"], 2)

        weeks = {row["week"]: row for row in player["weeks"]}
        yahoo = {
            week: next(
                value
                for value in row["provider_values"]
                if value["provider"] == "yahoo"
            )
            for week, row in weeks.items()
        }
        self.assertEqual(yahoo[1]["origin"], "provider_published")
        self.assertEqual(yahoo[2]["origin"], "derived_rest_of_season")
        self.assertEqual(yahoo[3]["origin"], "derived_rest_of_season")
        self.assertEqual(yahoo[4]["status"], "bye")
        self.assertEqual(
            [weeks[week]["direct_source_count"] for week in (1, 2, 3, 4)],
            [3, 2, 2, 3],
        )
        self.assertEqual(
            [weeks[week]["derived_source_count"] for week in (1, 2, 3, 4)],
            [0, 1, 1, 0],
        )
        yahoo_ros = next(
            row
            for row in player["provider_remaining_season"]
            if row["provider"] == "yahoo"
        )
        self.assertAlmostEqual(
            sum(
                value["projected_points"]
                for value in yahoo.values()
                if value["status"] == "observed"
            ),
            yahoo_ros["projected_points"],
        )

    def test_schedule_derived_bye_is_not_claimed_as_portably_proven(self):
        bundle = multiweek_player_bundle()
        evidence = tuple(
            row
            for row in bundle.projection_evidence
            if not (
                isinstance(row, WeeklyProjection)
                and row.canonical_player_id == "p1"
                and row.provider == "yahoo"
                and row.week == 4
            )
        )

        player = _player(
            build_player_outlook(
                replace(
                    bundle,
                    projection_evidence=evidence,
                    projection_source_manifest=projection_source_manifest(evidence),
                )
            ),
            "p1",
        )
        bye = next(row for row in player["weeks"] if row["week"] == 4)
        yahoo = next(
            row for row in bye["provider_values"] if row["provider"] == "yahoo"
        )

        self.assertIsNone(yahoo["origin"])
        self.assertEqual(bye["unattributed_source_count"], 1)

    def test_rejects_duplicate_raw_weekly_and_remaining_season_evidence(self):
        bundle = player_bundle()
        weekly = next(
            row for row in bundle.projection_evidence if isinstance(row, WeeklyProjection)
        )
        remaining = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, RemainingSeasonProjection)
        )
        for row, message in (
            (weekly, "duplicate weekly"),
            (remaining, "duplicate remaining-season"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    replace(
                        bundle,
                        projection_evidence=(*bundle.projection_evidence, row),
                    )

    def test_rejects_parse_error_evidence_for_an_observed_ensemble_value(self):
        bundle = player_bundle()
        target = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, WeeklyProjection)
            and row.canonical_player_id == "p1"
            and row.provider == "fantasypros"
        )
        parse_error = replace(
            target,
            status=ProjectionStatus.PARSE_ERROR,
            projected_fantasy_points=None,
            raw_projected_stats={},
        )
        evidence = tuple(
            parse_error if row is target else row
            for row in bundle.projection_evidence
        )
        with self.assertRaisesRegex(ValueError, "conflicts with captured weekly"):
            build_player_outlook(replace(bundle, projection_evidence=evidence))

    def test_rejects_inconsistent_player_position_and_nfl_team(self):
        bundle = player_bundle()
        first = bundle.projections[0]
        inconsistent_position = replace(first, position="QB")
        with self.assertRaisesRegex(ValueError, "primary position"):
            replace(
                bundle,
                projections=(inconsistent_position, *bundle.projections[1:]),
            )

        target = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, WeeklyProjection)
            and row.canonical_player_id == "p1"
            and row.provider == "fantasypros"
        )
        conflicting = replace(target, nfl_team_id="WRONG")
        evidence = tuple(
            conflicting if row is target else row
            for row in bundle.projection_evidence
        )
        with self.assertRaisesRegex(ValueError, "NFL team"):
            replace(bundle, projection_evidence=evidence)


if __name__ == "__main__":
    unittest.main()
