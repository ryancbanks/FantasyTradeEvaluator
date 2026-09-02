from dataclasses import replace
from datetime import datetime, timezone
import unittest

from trade_snapshot.context import EngineContext, ProjectionProviderPolicy
from trade_snapshot.league_state import (
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot.projections import ProjectionStatus, WeeklyProjection
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.strength import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    StrengthModel,
)
from trade_snapshot.trade_space import TeamRoster


CAPTURED_AT = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
BUNDLE_URL = "https://cdn.fantasypros.com/assets/trade-analyzer.js"


class EngineContextTests(unittest.TestCase):
    def test_accepts_one_complete_consistent_calculation_context(self):
        context = make_context()

        self.assertEqual(len(context.team_rosters), 2)
        self.assertEqual(len(context.weekly_projections), 2)

    def test_rejects_scoring_strength_and_projection_identity_mismatches(self):
        context = make_context()

        with self.subTest("scoring profile"):
            other_profile = ScoringProfile("espn", {"reception": 0.5})
            with self.assertRaisesRegex(ValueError, "content-addressed"):
                replace(context, scoring_profile=other_profile)

        for field, value in (
            ("snapshot_id", "other-snapshot"),
            ("season", 2027),
            ("scoring_profile_id", "other-profile"),
        ):
            with self.subTest(strength_field=field):
                model = make_strength(context, **{field: value})
                with self.assertRaisesRegex(ValueError, "strength model identity"):
                    replace(context, strength_model=model)

        with self.subTest("projection identity"):
            mixed = replace(context.weekly_projections[0], snapshot_id="other-snapshot")
            with self.assertRaisesRegex(ValueError, "projection identity"):
                replace(context, weekly_projections=(mixed, context.weekly_projections[1]))

    def test_rejects_partial_missing_duplicate_and_cross_owned_rosters(self):
        context = make_context()

        with self.subTest("missing team"):
            with self.assertRaisesRegex(ValueError, "every team"):
                replace(context, team_rosters=context.team_rosters[:1])

        with self.subTest("search pool instead of full roster"):
            partial = TeamRoster("a", ("pa",), current_size=2, roster_cap=2)
            with self.assertRaisesRegex(ValueError, "full rosters"):
                replace(context, team_rosters=(partial, context.team_rosters[1]))

        with self.subTest("wrong roster cap"):
            state = replace(
                context.league_state,
                roster_rules=RosterRules(3, ("QB",)),
            )
            with self.assertRaisesRegex(ValueError, "roster cap"):
                replace(context, league_state=state)

        with self.subTest("cross ownership"):
            other = TeamRoster("b", ("pa",), current_size=1, roster_cap=2)
            with self.assertRaisesRegex(ValueError, "more than one team"):
                replace(context, team_rosters=(context.team_rosters[0], other))

    def test_rejects_missing_calibration_or_player_week_and_duplicate_rows(self):
        context = make_context()

        with self.subTest("strength calibration"):
            model = StrengthModel(
                (qb_role(),),
                (qb_player("pa", 1),),
                10,
                snapshot_id=context.league_state.snapshot_id,
                season=context.league_state.season,
                scoring_profile_id=context.league_state.scoring_profile_id,
                calibration=calibration(),
            )
            with self.assertRaisesRegex(ValueError, "computation player_id 'pb'"):
                replace(context, strength_model=model)

        with self.subTest("normalized player/week"):
            with self.assertRaisesRegex(ValueError, "'fantasypros' projection row"):
                replace(context, weekly_projections=context.weekly_projections[:1])

        with self.subTest("duplicate provider row"):
            with self.assertRaisesRegex(ValueError, "duplicate provider player/week"):
                replace(
                    context,
                    weekly_projections=(
                        *context.weekly_projections,
                        context.weekly_projections[0],
                    ),
                )

    def test_provider_grid_rejects_collisions_unusable_rows_and_wrong_weeks(self):
        context = make_context()

        with self.subTest("required provider"):
            policy = ProjectionProviderPolicy(("fantasypros", "espn"))
            with self.assertRaisesRegex(ValueError, "'espn' projection row"):
                replace(context, projection_policy=policy)

        with self.subTest("canonical collision"):
            duplicate = replace(
                context.weekly_projections[0],
                provider_player_id="fp-other-id",
            )
            with self.assertRaisesRegex(ValueError, "two provider IDs"):
                replace(
                    context,
                    weekly_projections=(*context.weekly_projections, duplicate),
                )

        with self.subTest("only parse errors"):
            unusable = tuple(
                replace(
                    projection,
                    status=ProjectionStatus.PARSE_ERROR,
                    projected_fantasy_points=None,
                )
                for projection in context.weekly_projections
            )
            with self.assertRaisesRegex(ValueError, "too few observed providers"):
                replace(context, weekly_projections=unusable)

        with self.subTest("outside remaining window"):
            outside = replace(context.weekly_projections[0], week=2)
            with self.assertRaisesRegex(ValueError, "outside the remaining"):
                replace(
                    context,
                    weekly_projections=(outside, context.weekly_projections[1]),
                )

    def test_rejects_wrong_role_schema_denominator_and_computation_domain(self):
        context = make_context()

        with self.subTest("starter role schema"):
            wrong_role = RoleDefinition(
                "QB",
                RoleKind.STARTER,
                "SUPERFLEX",
                frozenset({"QB"}),
            )
            model = StrengthModel(
                (wrong_role,),
                context.strength_model.players.values(),
                3,
                snapshot_id=context.league_state.snapshot_id,
                season=context.league_state.season,
                scoring_profile_id=context.league_state.scoring_profile_id,
                calibration=context.strength_model.calibration,
            )
            with self.assertRaisesRegex(ValueError, "starter roles"):
                replace(context, strength_model=model)

        with self.subTest("normalization baseline"):
            model = StrengthModel(
                context.strength_model.role_definitions,
                context.strength_model.players.values(),
                4,
                snapshot_id=context.league_state.snapshot_id,
                season=context.league_state.season,
                scoring_profile_id=context.league_state.scoring_profile_id,
                calibration=context.strength_model.calibration,
            )
            with self.assertRaisesRegex(ValueError, "pre-trade league maximum"):
                replace(context, strength_model=model)

        with self.subTest("domain player calibration"):
            with self.assertRaisesRegex(ValueError, "computation player_id 'free-agent'"):
                replace(
                    context,
                    computation_player_ids=frozenset({"pa", "pb", "free-agent"}),
                )


def make_context() -> EngineContext:
    profile = ScoringProfile("espn", {"reception": 1, "passing_td": 4})
    state = LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id=profile.scoring_profile_id,
        first_remaining_week=1,
        teams=(LeagueTeam("a", "Alpha"), LeagueTeam("b", "Bravo")),
        standings=(
            TeamStanding("a", 0, 0, 0, 0, 0),
            TeamStanding("b", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(FantasyMatchup(1, "a", "b"),),
        roster_rules=RosterRules(2, ("QB",)),
        playoff_rules=PlayoffRules(
            qualifier_count=1,
            regular_season_end_week=1,
            playoff_weeks=(2,),
            reseed_each_round=False,
            division_winner_qualifier_count=0,
            tiebreaker_order=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.POINTS_FOR),
        ),
    )
    rosters = (
        TeamRoster("a", ("pa",), current_size=1, roster_cap=2),
        TeamRoster("b", ("pb",), current_size=1, roster_cap=2),
    )
    projections = tuple(
        WeeklyProjection(
            canonical_player_id=player_id,
            snapshot_id=state.snapshot_id,
            scoring_profile_id=state.scoring_profile_id,
            provider="fantasypros",
            provider_player_id=f"fp-{player_id}",
            season=state.season,
            week=1,
            status=ProjectionStatus.OBSERVED,
            captured_at=CAPTURED_AT,
            projected_fantasy_points=10,
            nfl_team_id="GB" if player_id == "pa" else "CHI",
            nfl_game_id="2026-W01-GB-CHI",
            opponent_team_id="CHI" if player_id == "pa" else "GB",
            is_home=player_id == "pa",
        )
        for player_id in ("pa", "pb")
    )
    model = StrengthModel(
        (qb_role(),),
        (
            qb_player("pa", 2),
            qb_player("pb", 2),
        ),
        3,
        snapshot_id=state.snapshot_id,
        season=state.season,
        scoring_profile_id=state.scoring_profile_id,
        calibration=calibration(),
    )
    return EngineContext(
        scoring_profile=profile,
        league_state=state,
        team_rosters=rosters,
        computation_player_ids=frozenset({"pa", "pb"}),
        projection_policy=ProjectionProviderPolicy(("fantasypros",)),
        weekly_projections=projections,
        strength_model=model,
    )


def make_strength(context: EngineContext, **changes) -> StrengthModel:
    identity = {
        "snapshot_id": context.league_state.snapshot_id,
        "season": context.league_state.season,
        "scoring_profile_id": context.league_state.scoring_profile_id,
    }
    identity.update(changes)
    return StrengthModel(
        context.strength_model.role_definitions,
        context.strength_model.players.values(),
        context.strength_model.normalization_denominator,
        calibration=context.strength_model.calibration,
        **identity,
    )


def qb_role() -> RoleDefinition:
    return RoleDefinition("QB", RoleKind.STARTER, "QB", frozenset({"QB"}))


def qb_player(player_id: str, score: float) -> PlayerStrength:
    return PlayerStrength(player_id, 1, frozenset({"QB"}), {"QB": score})


def calibration() -> CalibrationMetadata:
    return CalibrationMetadata(
        analyzer_bundle_url=BUNDLE_URL,
        analyzer_bundle_sha256="1" * 64,
        response_schema_sha256="2" * 64,
        captured_at=CAPTURED_AT,
    )


if __name__ == "__main__":
    unittest.main()
