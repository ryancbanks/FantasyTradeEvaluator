from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, InvalidOperation, getcontext
import math
import unittest
from unittest.mock import patch

from trade_snapshot.league_state import (
    CompletedFantasyMatchup,
    FantasyMatchup,
    HeadToHeadPolicy,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)
from trade_snapshot._season_ranking import _add_score_adjustment
from trade_snapshot.season import (
    ScoreScenario,
    TeamWeekScore,
    UnresolvedTieError,
    UnsupportedTiebreakerError,
    project_remaining_season,
)


class SeasonProjectionTests(unittest.TestCase):
    def test_settles_known_schedule_and_aggregates_every_outcome(self):
        state = make_state(standings=empty_standings())
        scenarios = (
            scenario(state, "winners-ab", {"a": 110, "b": 100, "c": 80, "d": 90}),
            scenario(state, "winners-cd", {"a": 70, "b": 60, "c": 110, "d": 120}),
        )

        result = project_remaining_season(state, iter(scenarios), random_seed=17)
        teams = {team.team_id: team for team in result.teams}

        self.assertEqual(result.scenario_count, 2)
        self.assertEqual(teams["a"].expected_final_wins, 0.5)
        self.assertEqual(teams["a"].expected_final_losses, 0.5)
        self.assertEqual(teams["a"].expected_final_points_for, 90.0)
        self.assertEqual(teams["a"].expected_final_points_against, 105.0)
        self.assertEqual(teams["a"].mean_rank, 2.0)
        self.assertEqual(teams["a"].rank_distribution, (0.5, 0.0, 0.5, 0.0))
        self.assertEqual(teams["a"].seed_distribution, (0.5, 0.0))
        self.assertEqual(teams["a"].playoff_probability, 0.5)
        self.assertEqual(teams["a"].current_standing, state.standings[0])
        self.assertIn(teams["a"].current_rank, range(1, 5))
        self.assertAlmostEqual(
            sum(team.playoff_probability for team in result.teams),
            state.playoff_rules.qualifier_count,
        )
        for team in result.teams:
            self.assertAlmostEqual(sum(team.rank_distribution), 1.0)
            self.assertAlmostEqual(sum(team.seed_distribution), team.playoff_probability)

    def test_rounds_half_up_before_settling_a_tie(self):
        standings = (
            TeamStanding("a", 3, 0, 0, 300, 250),
            TeamStanding("b", 2, 1, 0, 290, 260),
            TeamStanding("c", 1, 2, 0, 280, 270),
            TeamStanding("d", 0, 3, 0, 270, 280),
        )
        state = make_state(standings=standings)
        scores = {"a": 100.004, "d": 100.003, "b": 99.995, "c": 99.994}

        result = project_remaining_season(state, (scenario(state, "rounded", scores),))
        teams = {team.team_id: team for team in result.teams}

        self.assertEqual(teams["a"].expected_final_ties, 1.0)
        self.assertEqual(teams["d"].expected_final_ties, 1.0)
        self.assertEqual(teams["b"].expected_final_wins, 3.0)
        self.assertEqual(teams["b"].expected_final_points_for, 390.0)
        self.assertEqual(teams["c"].expected_final_points_against, 370.0)

    def test_prepares_one_decimal_context_for_the_scenario_stream(self):
        state = make_state(standings=empty_standings())
        outcomes = tuple(
            scenario(
                state,
                f"stream-{index}",
                {team_id: 100.004 + index / 100 for team_id in "abcd"},
            )
            for index in range(4)
        )

        with patch(
            "trade_snapshot._season_ranking.getcontext",
            return_value=getcontext(),
        ) as current_context:
            result = project_remaining_season(state, outcomes)

        self.assertEqual(result.scenario_count, len(outcomes))
        current_context.assert_called_once_with()

    def test_team1_score_adjustment_is_applied_after_rounding_and_flips_tie(self):
        state = make_state(standings=empty_standings())
        state = replace(
            state,
            remaining_matchups=(
                replace(
                    state.remaining_matchups[0],
                    team1_score_adjustment=1,
                ),
                state.remaining_matchups[1],
            ),
        )
        equal_scores = {team_id: 100.004 for team_id in "abcd"}

        result = project_remaining_season(
            state,
            (scenario(state, "home-bonus", equal_scores),),
        )
        teams = {team.team_id: team for team in result.teams}

        self.assertEqual(teams["a"].expected_final_wins, 1.0)
        self.assertEqual(teams["a"].expected_final_ties, 0.0)
        self.assertEqual(teams["d"].expected_final_losses, 1.0)
        self.assertEqual(teams["a"].expected_final_points_for, 101.0)
        self.assertEqual(teams["d"].expected_final_points_against, 101.0)
        self.assertEqual(teams["a"].expected_final_points_against, 100.0)

    def test_fractional_adjustment_is_not_rounded_a_second_time(self):
        state = make_state(standings=empty_standings())
        state = replace(
            state,
            remaining_matchups=(
                replace(
                    state.remaining_matchups[0],
                    team1_score_adjustment=0.5,
                ),
                state.remaining_matchups[1],
            ),
        )
        equal_scores = {team_id: 99.49 for team_id in "abcd"}

        result = project_remaining_season(
            state,
            (scenario(state, "fractional-home-bonus", equal_scores),),
            score_decimal_places=0,
        )
        teams = {team.team_id: team for team in result.teams}

        self.assertEqual(teams["a"].expected_final_points_for, 99.5)
        self.assertEqual(teams["d"].expected_final_points_against, 99.5)

    def test_zero_adjustment_fast_path_does_not_accept_a_boolean(self):
        with self.assertRaises(InvalidOperation):
            _add_score_adjustment(Decimal("100.00"), False)

    def test_division_winners_receive_guaranteed_top_seeds(self):
        standings = (
            TeamStanding("a", 5, 0, 0, 500, 300),
            TeamStanding("b", 4, 1, 0, 450, 350),
            TeamStanding("c", 1, 4, 0, 300, 450),
            TeamStanding("d", 0, 5, 0, 250, 500),
        )
        state = make_state(standings=standings, division_berths=2, qualifier_count=3)
        outcome = scenario(state, "divisions", {"a": 100, "b": 100, "c": 90, "d": 90})

        teams = {
            team.team_id: team
            for team in project_remaining_season(state, (outcome,)).teams
        }

        self.assertEqual(teams["a"].seed_distribution, (1.0, 0.0, 0.0))
        self.assertEqual(teams["c"].seed_distribution, (0.0, 1.0, 0.0))
        self.assertEqual(teams["b"].seed_distribution, (0.0, 0.0, 1.0))
        self.assertEqual(teams["b"].playoff_probability, 1.0)
        self.assertEqual(teams["b"].mean_rank, 2.0)

        limited_state = make_state(
            standings=standings, division_berths=1, qualifier_count=2
        )
        limited_outcome = scenario(
            limited_state, "limited-divisions", {"a": 100, "b": 100, "c": 90, "d": 90}
        )
        limited = {
            team.team_id: team
            for team in project_remaining_season(limited_state, (limited_outcome,)).teams
        }
        self.assertEqual(limited["a"].seed_distribution, (1.0, 0.0))
        self.assertEqual(limited["b"].seed_distribution, (0.0, 1.0))
        self.assertEqual(limited["c"].playoff_probability, 0.0)

    def test_division_winners_are_ranked_inside_their_division(self):
        state = make_division_head_to_head_seed_state()

        teams = {
            team.team_id: team
            for team in project_remaining_season(
                state, (scenario(state, "division-head-to-head", {}),)
            ).teams
        }

        # League-wide points rank B ahead of A and C ahead of D, but A beat B
        # and D beat C. Division qualification must use those division-local
        # head-to-head groups rather than the already-flattened overall order.
        self.assertEqual(teams["a"].playoff_probability, 1.0)
        self.assertEqual(teams["d"].playoff_probability, 1.0)
        self.assertEqual(teams["b"].playoff_probability, 0.0)
        self.assertEqual(teams["c"].playoff_probability, 0.0)

    def test_rejects_tiebreakers_that_require_missing_history(self):
        for rule in (Tiebreaker.HEAD_TO_HEAD, Tiebreaker.DIVISION_RECORD):
            with self.subTest(rule=rule):
                state = make_state(tiebreakers=(Tiebreaker.WIN_PERCENTAGE, rule))
                outcome = scenario(
                    state, "unsupported", {"a": 100, "b": 90, "c": 80, "d": 70}
                )
                with self.assertRaisesRegex(
                    UnsupportedTiebreakerError, "without historical matchup results"
                ):
                    project_remaining_season(state, (outcome,))

    def test_head_to_head_uses_complete_history_and_simulated_matchups(self):
        state = make_head_to_head_state()
        outcome = scenario(
            state,
            "future-rematch",
            {"a": 80, "b": 90, "c": 80, "d": 120},
        )

        teams = {
            team.team_id: team
            for team in project_remaining_season(state, (outcome,)).teams
        }

        self.assertEqual(teams["c"].current_rank, 1)
        self.assertEqual(teams["d"].current_rank, 2)
        self.assertEqual(teams["d"].mean_rank, 1.0)
        self.assertEqual(teams["c"].mean_rank, 2.0)

        exact_state = replace(
            state,
            completed_matchups=(
                *state.completed_matchups[:-1],
                replace(state.completed_matchups[-1], team1_score=195),
            ),
            standings=(
                state.standings[0],
                replace(state.standings[1], points_against=265),
                state.standings[2],
                replace(state.standings[3], points_for=285),
            ),
        )
        beyond_float_precision = scenario(
            exact_state,
            "exact-decimal-rematch",
            {
                "a": 80,
                "b": 90,
                "c": 9_007_199_254_740_992,
                "d": 9_007_199_254_740_993,
            },
        )
        exact_teams = {
            team.team_id: team
            for team in project_remaining_season(
                exact_state, (beyond_float_precision,)
            ).teams
        }
        self.assertEqual(exact_teams["d"].mean_rank, 1.0)
        self.assertEqual(exact_teams["c"].mean_rank, 2.0)

    def test_balanced_group_policy_resolves_a_three_team_tie(self):
        state = make_balanced_group_state()

        teams = {
            team.team_id: team
            for team in project_remaining_season(
                state, (scenario(state, "round-robin", {}),)
            ).teams
        }

        self.assertEqual(teams["a"].mean_rank, 2.0)
        self.assertEqual(teams["b"].mean_rank, 3.0)
        self.assertEqual(teams["c"].mean_rank, 4.0)

    def test_head_to_head_fails_closed_for_missing_policy_or_unbalanced_group(self):
        state = make_head_to_head_state()
        missing_policy = replace(
            state,
            playoff_rules=replace(state.playoff_rules, head_to_head_policy=None),
        )
        with self.assertRaisesRegex(UnsupportedTiebreakerError, "explicitly captured"):
            project_remaining_season(
                missing_policy,
                (scenario(missing_policy, "missing-policy", {
                    "a": 80, "b": 90, "c": 80, "d": 120,
                }),),
            )

        inconsistent = replace(
            state,
            standings=(replace(state.standings[0], points_for=999), *state.standings[1:]),
        )
        with self.assertRaisesRegex(
            UnsupportedTiebreakerError, "standings-consistent"
        ):
            project_remaining_season(
                inconsistent,
                (scenario(inconsistent, "inconsistent", {
                    "a": 80, "b": 90, "c": 80, "d": 120,
                }),),
            )

        unbalanced = make_unbalanced_head_to_head_state()
        with self.assertRaisesRegex(UnresolvedTieError, "every tied pair"):
            project_remaining_season(
                unbalanced, (scenario(unbalanced, "unbalanced", {}),)
            )

    def test_division_record_uses_complete_historical_results(self):
        state = make_division_record_state()

        teams = {
            team.team_id: team
            for team in project_remaining_season(
                state, (scenario(state, "division-record", {}),)
            ).teams
        }

        self.assertEqual(teams["a"].mean_rank, 2.0)
        self.assertEqual(teams["c"].mean_rank, 3.0)

        missing_divisions = replace(
            state,
            teams=tuple(replace(team, division_id=None) for team in state.teams),
        )
        with self.assertRaisesRegex(UnsupportedTiebreakerError, "division_id"):
            project_remaining_season(
                missing_divisions,
                (scenario(missing_divisions, "missing-divisions", {}),),
            )

    def test_division_record_includes_simulated_division_games(self):
        state = make_future_division_state()
        outcome = scenario(
            state,
            "future-division",
            {"a": 80, "b": 120, "c": 120, "d": 80},
        )

        teams = {
            team.team_id: team
            for team in project_remaining_season(state, (outcome,)).teams
        }

        self.assertEqual(teams["a"].current_rank, 1)
        self.assertEqual(teams["c"].mean_rank, 1.0)
        self.assertEqual(teams["a"].mean_rank, 2.0)

    def test_random_draw_is_seeded_and_independent_of_scenario_order(self):
        state = make_state(
            standings=empty_standings(),
            tiebreakers=(Tiebreaker.RANDOM_DRAW,),
        )
        first = scenario(state, "first", {team: 100 for team in "abcd"})
        second = scenario(state, "second", {team: 100 for team in "abcd"})

        forward = project_remaining_season(state, (first, second), random_seed=8721)
        repeated = project_remaining_season(state, (first, second), random_seed=8721)
        reversed_rows = project_remaining_season(state, (second, first), random_seed=8721)

        self.assertEqual(forward, repeated)
        self.assertEqual(forward, reversed_rows)
        self.assertEqual(forward.random_seed, 8721)
        different_seed = project_remaining_season(state, (first, second), random_seed=8722)
        self.assertNotEqual(forward.teams, different_seed.teams)

    def test_consumes_scenario_iterables_one_row_at_a_time(self):
        state = make_state(standings=empty_standings())
        processed_first = [False]

        class ObservableScore(float):
            def __str__(self):
                processed_first[0] = True
                return super().__str__()

        first = scenario(
            state,
            "first",
            {"a": ObservableScore(110), "b": 100, "c": 80, "d": 90},
        )
        second = scenario(state, "second", {"a": 70, "b": 60, "c": 110, "d": 120})

        def guarded_rows():
            yield first
            if not processed_first[0]:
                raise AssertionError("projector buffered scenarios instead of processing one")
            yield second

        result = project_remaining_season(state, guarded_rows())

        self.assertEqual(result.scenario_count, 2)

    def test_points_against_favors_the_team_that_faced_more_points(self):
        state = make_no_op_state(
            (
                TeamStanding("a", 1, 1, 0, 200, 250),
                TeamStanding("b", 1, 1, 0, 200, 240),
                TeamStanding("c", 0, 2, 0, 190, 260),
                TeamStanding("d", 0, 2, 0, 180, 270),
            ),
            tiebreakers=(
                Tiebreaker.WIN_PERCENTAGE,
                Tiebreaker.POINTS_FOR,
                Tiebreaker.POINTS_AGAINST,
            ),
        )

        result = project_remaining_season(state, (scenario(state, "no-op", {}),))
        ranks = {team.team_id: team.mean_rank for team in result.teams}

        self.assertEqual(ranks["a"], 1.0)
        self.assertEqual(ranks["b"], 2.0)

    def test_no_op_projection_repeats_current_records_exactly(self):
        standings = (
            TeamStanding("a", 4, 0, 0, 400, 300),
            TeamStanding("b", 3, 1, 0, 390, 310),
            TeamStanding("c", 2, 2, 0, 380, 320),
            TeamStanding("d", 1, 3, 0, 370, 330),
        )
        state = make_no_op_state(standings)
        outcome = scenario(state, "no-op", {})

        first = project_remaining_season(state, (outcome,), random_seed=5)
        second = project_remaining_season(state, (outcome,), random_seed=5)

        self.assertEqual(first, second)
        for projected, current in zip(first.teams, standings):
            self.assertEqual(projected.expected_final_wins, current.wins)
            self.assertEqual(projected.expected_final_losses, current.losses)
            self.assertEqual(projected.expected_final_points_for, current.points_for)
            self.assertEqual(projected.current_rank, projected.mean_rank)

    def test_validates_scenario_identity_and_complete_score_grid(self):
        state = make_state()
        complete = scenario(state, "valid", {"a": 100, "b": 90, "c": 80, "d": 70})

        with self.subTest("empty collection"):
            with self.assertRaisesRegex(ValueError, "at least one"):
                project_remaining_season(state, ())

        with self.subTest("missing team-week"):
            incomplete = replace(complete, scenario_id="missing", scores=complete.scores[:-1])
            with self.assertRaisesRegex(ValueError, "missing=1, extra=0"):
                project_remaining_season(state, (incomplete,))

        with self.subTest("extra team-week"):
            extra = replace(
                complete,
                scenario_id="extra",
                scores=(*complete.scores, TeamWeekScore("unknown", 3, 1)),
            )
            with self.assertRaisesRegex(ValueError, "missing=0, extra=1"):
                project_remaining_season(state, (extra,))

        for field, value in (
            ("snapshot_id", "other-snapshot"),
            ("scoring_profile_id", "other-scoring"),
        ):
            with self.subTest(field=field):
                mixed = replace(complete, scenario_id=field, **{field: value})
                with self.assertRaisesRegex(ValueError, f"different {field}"):
                    project_remaining_season(state, (complete, mixed))

        with self.subTest("duplicate scenario identifiers"):
            duplicate = replace(complete)
            with self.assertRaisesRegex(ValueError, "scenario_id values must be unique"):
                project_remaining_season(state, (complete, duplicate))

    def test_rejects_invalid_scores_options_and_unresolved_ties(self):
        state = make_state(
            standings=empty_standings(),
            tiebreakers=(Tiebreaker.WIN_PERCENTAGE,),
        )
        outcome = scenario(state, "ties", {team: 100 for team in "abcd"})

        with self.assertRaises(UnresolvedTieError):
            project_remaining_season(state, (outcome,))
        with self.assertRaisesRegex(ValueError, "score_decimal_places"):
            project_remaining_season(state, (outcome,), score_decimal_places=-1)
        with self.assertRaisesRegex(ValueError, "random_seed"):
            project_remaining_season(state, (outcome,), random_seed=True)
        with self.assertRaisesRegex(ValueError, "finite"):
            TeamWeekScore("a", 3, math.inf)
        with self.assertRaisesRegex(ValueError, "duplicate team-week"):
            ScoreScenario("duplicate", "snapshot-1", "ppr-v1", (
                TeamWeekScore("a", 3, 1), TeamWeekScore("a", 3, 2),
            ))
        large_state = make_state()
        large = scenario(large_state, "large", {team: 1e308 for team in "abcd"})
        large_result = project_remaining_season(large_state, (large,))
        self.assertTrue(math.isfinite(large_result.teams[0].expected_final_points_for))
        two_week_state = replace(
            large_state,
            remaining_matchups=(
                *large_state.remaining_matchups,
                FantasyMatchup(4, "a", "b"),
                FantasyMatchup(4, "c", "d"),
            ),
            playoff_rules=replace(
                large_state.playoff_rules,
                regular_season_end_week=4,
                playoff_weeks=(5, 6),
            ),
        )
        too_large = scenario(two_week_state, "too-large", {team: 1e308 for team in "abcd"})
        with self.assertRaisesRegex(ValueError, "aggregate is too large"):
            project_remaining_season(two_week_state, (too_large,))

    def test_outputs_are_immutable(self):
        state = make_state()
        outcome = scenario(state, "immutable", {"a": 100, "b": 90, "c": 80, "d": 70})
        result = project_remaining_season(state, (outcome,))

        with self.assertRaises(FrozenInstanceError):
            result.scenario_count = 2
        with self.assertRaises(FrozenInstanceError):
            result.teams[0].mean_rank = 4


def make_head_to_head_state() -> LeagueState:
    completed = (
        CompletedFantasyMatchup(1, "a", "b", 70, 70),
        CompletedFantasyMatchup(1, "c", "d", 100, 90),
        CompletedFantasyMatchup(2, "c", "a", 100, 80),
        CompletedFantasyMatchup(2, "d", "b", 95, 85),
    )
    standings = (
        TeamStanding("a", 0, 1, 1, 150, 170),
        TeamStanding("b", 0, 1, 1, 155, 165),
        TeamStanding("c", 2, 0, 0, 200, 170),
        TeamStanding("d", 1, 1, 0, 185, 185),
    )
    return historical_state(
        standings,
        completed,
        first_remaining_week=3,
        regular_season_end_week=3,
        remaining_matchups=(
            FantasyMatchup(3, "a", "b"),
            FantasyMatchup(3, "c", "d"),
        ),
        tiebreakers=(
            Tiebreaker.WIN_PERCENTAGE,
            Tiebreaker.HEAD_TO_HEAD,
            Tiebreaker.POINTS_FOR,
            Tiebreaker.RANDOM_DRAW,
        ),
    )


def make_balanced_group_state() -> LeagueState:
    teams = tuple(LeagueTeam(team, team.upper()) for team in "abcdef")
    completed = (
        CompletedFantasyMatchup(1, "a", "f", 100, 90),
        CompletedFantasyMatchup(1, "b", "e", 100, 90),
        CompletedFantasyMatchup(1, "c", "d", 100, 90),
        CompletedFantasyMatchup(2, "e", "a", 100, 90),
        CompletedFantasyMatchup(2, "d", "f", 100, 90),
        CompletedFantasyMatchup(2, "b", "c", 100, 90),
        CompletedFantasyMatchup(3, "d", "a", 100, 90),
        CompletedFantasyMatchup(3, "c", "e", 100, 90),
        CompletedFantasyMatchup(3, "b", "f", 100, 90),
        CompletedFantasyMatchup(4, "a", "c", 100, 90),
        CompletedFantasyMatchup(4, "d", "b", 100, 90),
        CompletedFantasyMatchup(4, "e", "f", 100, 90),
        CompletedFantasyMatchup(5, "a", "b", 100, 90),
        CompletedFantasyMatchup(5, "c", "f", 100, 90),
        CompletedFantasyMatchup(5, "d", "e", 100, 90),
    )
    standings = (
        TeamStanding("a", 3, 2, 0, 480, 470),
        TeamStanding("b", 3, 2, 0, 480, 470),
        TeamStanding("c", 3, 2, 0, 480, 470),
        TeamStanding("d", 4, 1, 0, 490, 460),
        TeamStanding("e", 2, 3, 0, 470, 480),
        TeamStanding("f", 0, 5, 0, 450, 500),
    )
    return historical_state(
        standings,
        completed,
        teams=teams,
        first_remaining_week=6,
        regular_season_end_week=5,
        qualifier_count=3,
        tiebreakers=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.HEAD_TO_HEAD),
    )


def make_unbalanced_head_to_head_state() -> LeagueState:
    completed = (
        CompletedFantasyMatchup(1, "a", "b", 100, 90),
        CompletedFantasyMatchup(1, "c", "d", 100, 90),
        CompletedFantasyMatchup(2, "c", "a", 100, 90),
        CompletedFantasyMatchup(2, "d", "b", 100, 90),
    )
    standings = (
        TeamStanding("a", 1, 1, 0, 190, 190),
        TeamStanding("b", 0, 2, 0, 180, 200),
        TeamStanding("c", 2, 0, 0, 200, 180),
        TeamStanding("d", 1, 1, 0, 190, 190),
    )
    return historical_state(
        standings,
        completed,
        first_remaining_week=3,
        regular_season_end_week=2,
        tiebreakers=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.HEAD_TO_HEAD),
    )


def make_division_record_state() -> LeagueState:
    completed = (
        CompletedFantasyMatchup(1, "a", "b", 100, 90),
        CompletedFantasyMatchup(1, "d", "c", 100, 90),
        CompletedFantasyMatchup(2, "c", "a", 100, 90),
        CompletedFantasyMatchup(2, "d", "b", 100, 90),
    )
    standings = (
        TeamStanding("a", 1, 1, 0, 190, 190),
        TeamStanding("b", 0, 2, 0, 180, 200),
        TeamStanding("c", 1, 1, 0, 190, 190),
        TeamStanding("d", 2, 0, 0, 200, 180),
    )
    return historical_state(
        standings,
        completed,
        first_remaining_week=3,
        regular_season_end_week=2,
        tiebreakers=(Tiebreaker.WIN_PERCENTAGE, Tiebreaker.DIVISION_RECORD),
    )


def make_future_division_state() -> LeagueState:
    completed = (
        CompletedFantasyMatchup(1, "a", "b", 100, 90),
        CompletedFantasyMatchup(1, "d", "c", 100, 90),
        CompletedFantasyMatchup(2, "a", "d", 100, 90),
        CompletedFantasyMatchup(2, "c", "b", 100, 90),
    )
    standings = (
        TeamStanding("a", 2, 0, 0, 200, 180),
        TeamStanding("b", 0, 2, 0, 180, 200),
        TeamStanding("c", 1, 1, 0, 190, 190),
        TeamStanding("d", 1, 1, 0, 190, 190),
    )
    return historical_state(
        standings,
        completed,
        first_remaining_week=3,
        regular_season_end_week=3,
        remaining_matchups=(
            FantasyMatchup(3, "a", "b"),
            FantasyMatchup(3, "c", "d"),
        ),
        tiebreakers=(
            Tiebreaker.WIN_PERCENTAGE,
            Tiebreaker.DIVISION_RECORD,
            Tiebreaker.POINTS_FOR,
            Tiebreaker.RANDOM_DRAW,
        ),
    )


def make_division_head_to_head_seed_state() -> LeagueState:
    completed = (
        CompletedFantasyMatchup(1, "a", "b", 120, 100),
        CompletedFantasyMatchup(1, "c", "d", 90, 110),
        CompletedFantasyMatchup(2, "a", "c", 80, 110),
        CompletedFantasyMatchup(2, "b", "d", 130, 100),
        CompletedFantasyMatchup(3, "a", "d", 90, 90),
        CompletedFantasyMatchup(3, "b", "c", 120, 120),
    )
    standings = (
        TeamStanding("a", 1, 1, 1, 290, 300),
        TeamStanding("b", 1, 1, 1, 350, 340),
        TeamStanding("c", 1, 1, 1, 320, 310),
        TeamStanding("d", 1, 1, 1, 300, 310),
    )
    return historical_state(
        standings,
        completed,
        first_remaining_week=4,
        regular_season_end_week=3,
        qualifier_count=2,
        division_berths=2,
        tiebreakers=(
            Tiebreaker.WIN_PERCENTAGE,
            Tiebreaker.HEAD_TO_HEAD,
            Tiebreaker.POINTS_FOR,
            Tiebreaker.RANDOM_DRAW,
        ),
    )


def historical_state(
    standings,
    completed_matchups,
    *,
    teams=None,
    first_remaining_week,
    regular_season_end_week,
    remaining_matchups=(),
    qualifier_count=2,
    division_berths=0,
    tiebreakers,
) -> LeagueState:
    teams = teams or (
        LeagueTeam("a", "Alpha", "east"),
        LeagueTeam("b", "Bravo", "east"),
        LeagueTeam("c", "Charlie", "west"),
        LeagueTeam("d", "Delta", "west"),
    )
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="ppr-v1",
        first_remaining_week=first_remaining_week,
        teams=teams,
        standings=standings,
        remaining_matchups=remaining_matchups,
        roster_rules=RosterRules(14, ("QB",)),
        playoff_rules=PlayoffRules(
            qualifier_count=qualifier_count,
            regular_season_end_week=regular_season_end_week,
            playoff_weeks=(regular_season_end_week + 1,),
            reseed_each_round=False,
            division_winner_qualifier_count=division_berths,
            tiebreaker_order=tiebreakers,
            head_to_head_policy=HeadToHeadPolicy.BALANCED_GROUP_WIN_PERCENTAGE,
        ),
        completed_matchups=completed_matchups,
    )


def make_state(
    *,
    standings=None,
    tiebreakers=(
        Tiebreaker.WIN_PERCENTAGE,
        Tiebreaker.POINTS_FOR,
        Tiebreaker.POINTS_AGAINST,
        Tiebreaker.RANDOM_DRAW,
    ),
    division_berths=0,
    qualifier_count=2,
) -> LeagueState:
    teams = (
        LeagueTeam("a", "Alpha", "east"),
        LeagueTeam("b", "Bravo", "east"),
        LeagueTeam("c", "Charlie", "west"),
        LeagueTeam("d", "Delta", "west"),
    )
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="ppr-v1",
        first_remaining_week=3,
        teams=teams,
        standings=standings or (
            TeamStanding("a", 3, 0, 0, 300, 250),
            TeamStanding("b", 2, 1, 0, 290, 260),
            TeamStanding("c", 1, 2, 0, 280, 270),
            TeamStanding("d", 0, 3, 0, 270, 280),
        ),
        remaining_matchups=(
            FantasyMatchup(3, "a", "d"),
            FantasyMatchup(3, "b", "c"),
        ),
        roster_rules=RosterRules(14, ("QB",)),
        playoff_rules=PlayoffRules(
            qualifier_count=qualifier_count,
            regular_season_end_week=3,
            playoff_weeks=(4, 5),
            reseed_each_round=False,
            division_winner_qualifier_count=division_berths,
            tiebreaker_order=tiebreakers,
        ),
    )


def make_no_op_state(standings, *, tiebreakers=None) -> LeagueState:
    state = make_state(standings=standings, tiebreakers=tiebreakers or (
        Tiebreaker.WIN_PERCENTAGE,
        Tiebreaker.POINTS_FOR,
        Tiebreaker.RANDOM_DRAW,
    ))
    return replace(
        state,
        first_remaining_week=4,
        remaining_matchups=(),
    )


def empty_standings():
    return tuple(TeamStanding(team, 0, 0, 0, 0, 0) for team in "abcd")


def scenario(state: LeagueState, scenario_id: str, scores: dict[str, float]):
    return ScoreScenario(
        scenario_id=scenario_id,
        snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        scores=tuple(
            TeamWeekScore(team.team_id, week, scores[team.team_id])
            for week in state.remaining_regular_season_weeks
            for team in state.teams
        ),
    )


if __name__ == "__main__":
    unittest.main()
