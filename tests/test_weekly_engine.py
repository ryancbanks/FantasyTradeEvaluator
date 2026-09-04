from dataclasses import replace
from datetime import datetime, timezone
import unittest

from tests.test_feature_engineering import (
    inputs,
    projection as feature_projection,
    rank as ecr_rank,
)
from tests.test_strength_formula import formula
from tests.source_fixtures import (
    fantasypros_league_benchmark,
    projection_source_manifest,
    weekly_source_manifest,
)
from tests.ecr_fixtures import with_ecr_rankings
from trade_snapshot.analyzer_contract import BundleFingerprint
from trade_snapshot.ensemble import EnsembleConfig, ProviderWeight
from trade_snapshot.methodology import PowerMethodology
from trade_snapshot.methodology_reuse import (
    FormulaAction,
    FormulaReuseDecision,
    MethodologyFingerprint,
)
from trade_snapshot.league_state import (
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot.nfl_schedule import NflSchedule, NflTeamWeek, NflTeamWeekStatus
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
)
from trade_snapshot.projection_schedule import normalize_ros_active_weeks
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.scenario_config import (
    CorrelatedScenarioConfig,
    FactorLoadings,
    PlayerEligibility,
)
from trade_snapshot.strength import CalibrationStatus
from trade_snapshot.strength_calibration import CalibrationMetadata
from trade_snapshot.trade_space import TeamRoster
from trade_snapshot.waiver_pool import WaiverPool, WaiverPoolPlayer, WaiverPoolSource
from trade_snapshot.weekly_engine import build_weekly_engine


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
SCORING_PROFILE = ScoringProfile("espn", {"reception": 1, "passing_td": 4})
FORECAST_PROVIDERS = ("fantasypros", "espn", "yahoo")


def state(scoring_profile_id="profile-1"):
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id=scoring_profile_id,
        first_remaining_week=1,
        teams=(LeagueTeam("a", "Alpha"), LeagueTeam("b", "Bravo")),
        standings=(
            TeamStanding("a", 0, 0, 0, 0, 0),
            TeamStanding("b", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(
            FantasyMatchup(1, "a", "b"),
            FantasyMatchup(2, "a", "b"),
        ),
        roster_rules=RosterRules(2, ("RB", "FLEX")),
        playoff_rules=PlayoffRules(
            1,
            2,
            (3,),
            False,
            0,
            (Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def raw_rows(ensembles):
    rows = []
    for ensemble in ensembles:
        fantasypros = ensemble.provider_observations[0]
        for provider, offset in (
            ("fantasypros", 0),
            ("espn", 2),
            ("yahoo", 4),
        ):
            points = (
                fantasypros.projected_fantasy_points + offset
                if fantasypros.status is ProjectionStatus.OBSERVED
                else None
            )
            rows.append(
                WeeklyProjection(
                    ensemble.canonical_player_id,
                    ensemble.snapshot_id,
                    ensemble.scoring_profile_id,
                    provider,
                    f"{provider}-{ensemble.canonical_player_id}",
                    ensemble.season,
                    ensemble.week,
                    fantasypros.status,
                    NOW,
                    points,
                    {"fantasy_points": points},
                    ensemble.nfl_team_id,
                    ensemble.nfl_game_id,
                    ensemble.opponent_team_id,
                    ensemble.is_home,
                )
            )
    return tuple(rows)


def complete_ros_rows(source_rows):
    ros_pairs = {
        (row.canonical_player_id, row.provider)
        for row in source_rows
        if isinstance(row, RemainingSeasonProjection)
    }
    weekly_by_pair = {}
    for row in source_rows:
        if not isinstance(row, WeeklyProjection):
            continue
        weekly_by_pair.setdefault(
            (row.canonical_player_id, row.provider), []
        ).append(row)
    return tuple(
        RemainingSeasonProjection(
            canonical_player_id=player_id,
            snapshot_id=pair_rows[0].snapshot_id,
            scoring_profile_id=pair_rows[0].scoring_profile_id,
            provider=provider,
            provider_player_id=pair_rows[0].provider_player_id,
            season=pair_rows[0].season,
            applicable_weeks=tuple(range(1, 19)),
            status=ProjectionStatus.OBSERVED,
            origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
            captured_at=NOW,
            projected_fantasy_points=sum(
                row.projected_fantasy_points for row in pair_rows
            ),
        )
        for (player_id, provider), pair_rows in sorted(weekly_by_pair.items())
        if (player_id, provider) not in ros_pairs
        and {row.week for row in pair_rows} == {1, 2}
        and all(row.status is ProjectionStatus.OBSERVED for row in pair_rows)
    )


def nfl_schedule(*, final_week=18, p1_bye_week=None):
    rows = []
    for player_id in ("p1", "p2", "p3", "p4"):
        team = (
            f"NFL-{player_id}".upper()
            if player_id in {"p3", "p4"}
            else f"NFL-{player_id}"
        )
        opponent = f"OPP-{player_id}"
        for week in range(1, final_week + 1):
            if player_id == "p1" and week == p1_bye_week:
                rows.extend(
                    (
                        NflTeamWeek(team, week, NflTeamWeekStatus.BYE),
                        NflTeamWeek(opponent, week, NflTeamWeekStatus.BYE),
                    )
                )
                continue
            game_id = f"G{week}-{player_id}"
            rows.extend(
                (
                    NflTeamWeek(
                        team,
                        week,
                        NflTeamWeekStatus.SCHEDULED,
                        game_id,
                        opponent,
                        True,
                    ),
                    NflTeamWeek(
                        opponent,
                        week,
                        NflTeamWeekStatus.SCHEDULED,
                        game_id,
                        team,
                        False,
                    ),
                )
            )
    return NflSchedule(2026, NOW, "espn", tuple(rows))


def methodology(profile_id):
    base = formula(profile_id)
    calibration = CalibrationMetadata(
        "https://cdn.fantasypros.com/assets/js/trade-analyzer.js",
        "a" * 64,
        "b" * 64,
        NOW,
        CalibrationStatus.EXACT,
        100,
        0,
        1,
    )
    exact = replace(
        base,
        trained_snapshot_id="snapshot-1",
        calibration=calibration,
        held_out_trade_ids=tuple(f"holdout-{index}" for index in range(100)),
        held_out_balanced_package_sizes=(1, 2, 3, 4),
    )
    power = PowerMethodology(
        tuple(exact.residual_weights),
        tuple(next(iter(exact.role_weights.values()))),
    )
    fingerprint = MethodologyFingerprint(
        BundleFingerprint(calibration.analyzer_bundle_url, "a" * 64),
        "b" * 64,
        power,
        exact.role_definitions,
    )
    decision = FormulaReuseDecision(
        FormulaAction.RECALIBRATE,
        ("test calibration",),
        fingerprint.fingerprint_id,
    )
    return exact, fingerprint, decision


def waiver_pool(profile_id):
    return WaiverPool(
        "snapshot-1",
        profile_id,
        ("RB",),
        2,
        tuple(
            WaiverPoolPlayer(
                player_id,
                provider_id,
                display_name,
                "RB",
                f"NFL-{player_id}",
                ("RB", "FLEX"),
                rank,
                WaiverPoolSource.FANTASYPROS_BEST,
                order,
            )
            for order, (player_id, provider_id, display_name, rank) in enumerate(
                (
                    ("p3", "303", "Player Three", 3),
                    ("p4", "304", "Player Four", 4),
                ),
                start=1,
            )
        ),
    )


def build(rows=None, *, schedule=None, ensemble_config=None):
    profile_id = SCORING_PROFILE.scoring_profile_id
    ecr, ensembles, eligibility = inputs(profile_id)
    ecr = tuple(
        with_ecr_rankings(
            snapshot,
            (
                *snapshot.rankings,
                ecr_rank("p3", "303", 3, 3),
                ecr_rank("p4", "304", 4, 4),
            ),
        )
        for snapshot in ecr
    )
    ensembles = (
        *ensembles,
        replace(feature_projection("p3", 1, 4, scoring_profile_id=profile_id), nfl_team_id="NFL-P3"),
        replace(feature_projection("p3", 2, 4, scoring_profile_id=profile_id), nfl_team_id="NFL-P3"),
        replace(feature_projection("p4", 1, 3, scoring_profile_id=profile_id), nfl_team_id="NFL-P4"),
        replace(feature_projection("p4", 2, 3, scoring_profile_id=profile_id), nfl_team_id="NFL-P4"),
    )
    waiver_rows = raw_rows(ensembles[-4:])
    eligibility = (
        *eligibility,
        PlayerEligibility("p3", ("RB", "FLEX")),
        PlayerEligibility("p4", ("RB", "FLEX")),
    )
    strength_formula, fingerprint, decision = methodology(profile_id)
    source_rows = tuple(raw_rows(ensembles) if rows is None else (*rows, *waiver_rows))
    full_ros_rows = complete_ros_rows(source_rows)
    projection_evidence = (*source_rows, *full_ros_rows)
    schedule = schedule or nfl_schedule()
    player_nfl_team_ids = {
        "p1": "NFL-p1",
        "p2": "NFL-p2",
        "p3": "NFL-P3",
        "p4": "NFL-P4",
    }
    normalized_evidence = tuple(
        normalize_ros_active_weeks(
            row,
            nfl_team_id=player_nfl_team_ids[row.canonical_player_id],
            nfl_schedule=schedule,
        )
        if isinstance(row, RemainingSeasonProjection)
        else row
        for row in projection_evidence
    )
    return build_weekly_engine(
        state=state(profile_id),
        scoring_profile=SCORING_PROFILE,
        rosters=(
            TeamRoster("a", ("p1",), 1, 2),
            TeamRoster("b", ("p2",), 1, 2),
        ),
        projection_evidence=projection_evidence,
        ecr_snapshots=ecr,
        eligibilities=eligibility,
        player_positions={"p1": "RB", "p2": "RB", "p3": "RB", "p4": "RB"},
        player_nfl_team_ids=player_nfl_team_ids,
        player_names={
            "p1": "Player One",
            "p2": "Player Two",
            "p3": "Player Three",
            "p4": "Player Four",
        },
        nfl_schedule=schedule,
        source_manifest=weekly_source_manifest(),
        projection_source_manifest=projection_source_manifest(normalized_evidence),
        fantasypros_benchmark=fantasypros_league_benchmark(team_ids=("a", "b")),
        ensemble_config=ensemble_config or EnsembleConfig(
            tuple(ProviderWeight(provider, 1) for provider in FORECAST_PROVIDERS),
            2,
            {"RB": 0},
        ),
        scenario_config=CorrelatedScenarioConfig(
            100, 7, FactorLoadings(0, 0, 0, 1)
        ),
        strength_formula=strength_formula,
        waiver_pool=waiver_pool(profile_id),
        methodology_fingerprint=fingerprint,
        formula_decision=decision,
        reuse_verification=None,
    )


class WeeklyEngineTests(unittest.TestCase):
    def test_builds_content_addressed_bundle_without_analyzer_calls(self):
        bundle = build()
        self.assertIs(bundle.scoring_profile, SCORING_PROFILE)
        self.assertEqual(bundle.state.first_remaining_week, 1)
        self.assertEqual(len(bundle.projections), 8)
        self.assertEqual(len(bundle.projection_evidence), 36)
        self.assertEqual(bundle.strength_model.snapshot_id, "snapshot-1")
        self.assertEqual(bundle.strength_model.normalization_denominator, 30.5)
        self.assertTrue(bundle.bundle_id.startswith("engine_"))
        self.assertEqual(
            bundle.methodology_attestation.validated_balanced_package_sizes,
            (1, 2, 3, 4),
        )

    def test_optional_provider_gap_is_explicit_and_respects_source_quorum(self):
        ecr, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = raw_rows(ensembles)
        optional_gap = tuple(
            row
            for row in rows
            if not (
                row.canonical_player_id == "p2"
                and row.week == 2
                and row.provider == "yahoo"
            )
        )

        bundle = build(
            optional_gap,
            ensemble_config=EnsembleConfig(
                tuple(
                    ProviderWeight(provider, 1)
                    for provider in FORECAST_PROVIDERS
                ),
                2,
                {"RB": 0},
            ),
        )

        p2_week_two = next(
            row
            for row in bundle.projections
            if row.canonical_player_id == "p2" and row.week == 2
        )
        yahoo = next(
            row for row in p2_week_two.provider_observations if row.provider == "yahoo"
        )
        self.assertIs(yahoo.status, ProjectionStatus.NOT_PUBLISHED)
        self.assertIs(p2_week_two.status, ProjectionStatus.OBSERVED)

    def test_required_forecast_quorum_gap_fails_closed(self):
        _, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = raw_rows(ensembles)
        missing_observation = tuple(
            row
            for row in rows
            if not (
                row.canonical_player_id == "p2"
                and row.week == 2
                and row.provider in {"espn", "yahoo"}
            )
        )
        with self.assertRaisesRegex(ValueError, "insufficient observed provider sources"):
            build(missing_observation)

    def test_formula_required_provider_gap_and_identity_drift_fail_closed(self):
        ecr, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = raw_rows(ensembles)
        required_gap = tuple(
            row
            for row in rows
            if not (
                row.canonical_player_id == "p2"
                and row.week == 2
                and row.provider == "fantasypros"
            )
        )
        with self.assertRaisesRegex(ValueError, "required feature.*unavailable"):
            build(required_gap)

        missing_identity = tuple(
            row
            for row in rows
            if not (row.canonical_player_id == "p2" and row.provider == "yahoo")
        )
        with self.assertRaisesRegex(ValueError, "lacks provider identity"):
            build(missing_identity)
        changed = list(rows)
        row = changed[0]
        changed[0] = WeeklyProjection(
            row.canonical_player_id,
            "other-snapshot",
            row.scoring_profile_id,
            row.provider,
            row.provider_player_id,
            row.season,
            row.week,
            row.status,
            row.captured_at,
            row.projected_fantasy_points,
            row.raw_projected_stats,
            row.nfl_team_id,
            row.nfl_game_id,
            row.opponent_team_id,
            row.is_home,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            build(tuple(changed))

    def test_forecast_sources_change_playoff_inputs_but_not_power(self):
        ecr, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = raw_rows(ensembles)
        changed = tuple(
            replace(
                row,
                projected_fantasy_points=row.projected_fantasy_points + 100,
                raw_projected_stats={
                    "fantasy_points": row.projected_fantasy_points + 100
                },
            )
            if row.provider in {"espn", "yahoo"}
            and row.status is ProjectionStatus.OBSERVED
            else row
            for row in rows
        )

        baseline = build(rows)
        updated = build(changed)

        self.assertEqual(baseline.strength_model, updated.strength_model)
        self.assertNotEqual(baseline.projections, updated.projections)

    def test_rejects_reference_only_provider_before_ensemble_calculation(self):
        with self.assertRaisesRegex(ValueError, "reference-only"):
            build(
                ensemble_config=EnsembleConfig(
                    (ProviderWeight("ffa", 1),),
                    1,
                    {"RB": 0},
                )
            )

    def test_full_scope_ros_survives_bundle_validation_with_known_bye(self):
        _, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = tuple(
            row
            for row in raw_rows(ensembles)
            if row.canonical_player_id != "p1"
        )
        template = next(
            row
            for row in raw_rows(ensembles)
            if row.canonical_player_id == "p1" and row.week == 1
        )
        rows += tuple(
            RemainingSeasonProjection(
                canonical_player_id=player_id,
                snapshot_id=template.snapshot_id,
                scoring_profile_id=template.scoring_profile_id,
                provider=provider,
                provider_player_id=f"{provider}-{player_id}",
                season=template.season,
                applicable_weeks=tuple(range(1, 19)),
                status=ProjectionStatus.OBSERVED,
                origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                captured_at=NOW,
                projected_fantasy_points=total,
            )
            for player_id, total in (
                ("p1", 85.0),
                ("p2", 40.0),
                ("p3", 16.0),
                ("p4", 12.0),
            )
            for provider in ("fantasypros", "espn", "yahoo")
        )

        bundle = build(
            rows,
            schedule=nfl_schedule(p1_bye_week=3),
        )
        p1 = tuple(
            row for row in bundle.projections if row.canonical_player_id == "p1"
        )

        self.assertEqual(
            [row.projected_fantasy_points for row in p1],
            [5.0, 5.0],
        )
        ros = tuple(
            row
            for row in bundle.projection_evidence
            if isinstance(row, RemainingSeasonProjection)
            and row.canonical_player_id == "p1"
        )
        self.assertTrue(
            all(
                row.applicable_weeks
                == tuple(week for week in range(1, 19) if week != 3)
                for row in ros
            )
        )

    def test_not_applicable_ros_evidence_survives_engine_normalization(self):
        _, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = raw_rows(ensembles)
        template = next(
            row
            for row in rows
            if row.canonical_player_id == "p1"
            and row.provider == "yahoo"
        )
        unavailable = RemainingSeasonProjection(
            canonical_player_id=template.canonical_player_id,
            snapshot_id=template.snapshot_id,
            scoring_profile_id=template.scoring_profile_id,
            provider=template.provider,
            provider_player_id=template.provider_player_id,
            season=template.season,
            applicable_weeks=(),
            status=ProjectionStatus.NOT_APPLICABLE,
            origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
            captured_at=NOW,
        )

        bundle = build((*rows, unavailable))

        self.assertIn(unavailable, bundle.projection_evidence)


if __name__ == "__main__":
    unittest.main()
