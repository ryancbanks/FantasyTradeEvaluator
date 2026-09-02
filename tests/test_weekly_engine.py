from dataclasses import replace
from datetime import datetime, timezone
import unittest

from tests.test_feature_engineering import (
    inputs,
    projection as feature_projection,
    rank as ecr_rank,
)
from tests.test_strength_formula import formula
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
from trade_snapshot.projections import ProjectionStatus, WeeklyProjection
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
        for observed in ensemble.provider_observations:
            rows.append(
                WeeklyProjection(
                    ensemble.canonical_player_id,
                    ensemble.snapshot_id,
                    ensemble.scoring_profile_id,
                    observed.provider,
                    observed.provider_player_id,
                    ensemble.season,
                    ensemble.week,
                    observed.status,
                    NOW,
                    observed.projected_fantasy_points,
                    {"fantasy_points": observed.projected_fantasy_points},
                    ensemble.nfl_team_id,
                    ensemble.nfl_game_id,
                    ensemble.opponent_team_id,
                    ensemble.is_home,
                )
            )
    return tuple(rows)


def nfl_schedule():
    rows = []
    for player_id in ("p1", "p2", "p3", "p4"):
        team = (
            f"NFL-{player_id}".upper()
            if player_id in {"p3", "p4"}
            else f"NFL-{player_id}"
        )
        opponent = f"OPP-{player_id}"
        for week in (1, 2):
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


def build(rows=None):
    profile_id = SCORING_PROFILE.scoring_profile_id
    ecr, ensembles, eligibility = inputs(profile_id)
    ecr = tuple(
        replace(
            snapshot,
            rankings=(
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
    return build_weekly_engine(
        state=state(profile_id),
        scoring_profile=SCORING_PROFILE,
        rosters=(
            TeamRoster("a", ("p1",), 1, 2),
            TeamRoster("b", ("p2",), 1, 2),
        ),
        projection_evidence=(
            raw_rows(ensembles) if rows is None else (*rows, *waiver_rows)
        ),
        ecr_snapshots=ecr,
        eligibilities=eligibility,
        player_positions={"p1": "RB", "p2": "RB", "p3": "RB", "p4": "RB"},
        player_nfl_team_ids={
            "p1": "NFL-p1",
            "p2": "NFL-p2",
            "p3": "NFL-P3",
            "p4": "NFL-P4",
        },
        player_names={
            "p1": "Player One",
            "p2": "Player Two",
            "p3": "Player Three",
            "p4": "Player Four",
        },
        nfl_schedule=nfl_schedule(),
        ensemble_config=EnsembleConfig(
            tuple(ProviderWeight(provider, 1) for provider in ("fantasypros", "espn", "yahoo")),
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
        self.assertEqual(len(bundle.projection_evidence), 24)
        self.assertEqual(bundle.strength_model.snapshot_id, "snapshot-1")
        self.assertEqual(bundle.strength_model.normalization_denominator, 30.5)
        self.assertTrue(bundle.bundle_id.startswith("engine_"))
        self.assertEqual(
            bundle.methodology_attestation.validated_balanced_package_sizes,
            (1, 2, 3, 4),
        )

    def test_fails_closed_when_a_provider_row_is_missing_or_identity_drifts(self):
        ecr, ensembles, _ = inputs(SCORING_PROFILE.scoring_profile_id)
        rows = raw_rows(ensembles)
        degraded = build(rows[:-1])
        p2_week2 = next(
            row
            for row in degraded.projections
            if row.canonical_player_id == "p2" and row.week == 2
        )
        self.assertTrue(
            any(
                item.provider == "yahoo" and item.status is ProjectionStatus.NOT_PUBLISHED
                for item in p2_week2.provider_observations
            )
        )
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

    def test_espn_and_yahoo_change_playoff_inputs_but_not_power(self):
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


if __name__ == "__main__":
    unittest.main()
