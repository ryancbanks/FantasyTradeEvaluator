from dataclasses import FrozenInstanceError, replace
import math
import unittest

from trade_snapshot.draft_config import (
    DraftLeagueConfig,
    DraftStrategy,
    default_slot_eligibility,
)
from trade_snapshot.draft_history import (
    ActualPlayerWeek,
    ActualWeekStatus,
    HistoricalSeason,
    PreseasonPlayer,
)
from trade_snapshot.draft_season import (
    GameOutcome,
    SeasonStage,
    _Record,
    _preseason_weekly_score,
    _winning_percentage,
    simulate_historical_season,
)


class HistoricalSeasonSimulationTests(unittest.TestCase):
    def test_season_projection_totals_use_resolved_projected_game_horizon(self):
        points_config = league_config(
            4, regular=(1, 2, 3), playoffs=(4, 5)
        )
        ensemble = replace(
            player("ensemble", projected=0, points={1: 1}),
            preseason_features={
                "ensemble.projected_points": 170,
                "espn.projected_points": 1_000,
                "yahoo.projected_points": 1_000,
                "ensemble.projected_games": 17,
                "espn.projected_games": 1,
            },
        )
        games_played = replace(
            player("games-played", projected=0, points={1: 1}),
            preseason_features={
                "projected_points": 160,
                "projected_games_played": 16,
            },
        )
        stat_total = replace(
            player("stat-total", projected=0, points={1: 1}),
            preseason_features={
                "projected_stat.bonus": 80,
                "projected_games": 16,
            },
        )
        stat_config = league_config(
            4,
            regular=(1, 2, 3),
            playoffs=(4, 5),
            scoring_weights={"bonus": 2.0},
        )

        self.assertEqual(_preseason_weekly_score(ensemble, points_config), 10)
        self.assertEqual(_preseason_weekly_score(games_played, points_config), 10)
        self.assertEqual(_preseason_weekly_score(stat_total, stat_config), 10)

    def test_unusable_projected_game_horizon_falls_back_to_regular_weeks(self):
        config = league_config(4, regular=(1, 2, 3), playoffs=(4, 5))
        for projected_games in (None, 0, -2):
            with self.subTest(projected_games=projected_games):
                projected = replace(
                    player("fallback", projected=0, points={1: 1}),
                    preseason_features={
                        "projected_points": 90,
                        "projected_games": projected_games,
                    },
                )
                self.assertEqual(_preseason_weekly_score(projected, config), 30)

        alternate = replace(
            player("alternate", projected=0, points={1: 1}),
            preseason_features={
                "projected_points": 90,
                "projected_games": 0,
                "projected_games_played": 9,
            },
        )
        self.assertEqual(_preseason_weekly_score(alternate, config), 10)

    def test_standings_use_win_percentage_with_half_credit_for_ties(self):
        tie_heavy = _Record(wins=5, losses=0, ties=9)
        losing_record = _Record(wins=6, losses=8)
        extra_bye = _Record(wins=1, losses=0)
        self.assertGreater(
            _winning_percentage(tie_heavy), _winning_percentage(losing_record)
        )
        self.assertGreater(_winning_percentage(extra_bye), _winning_percentage(tie_heavy))

    def test_round_robin_standings_and_six_team_bracket_are_deterministic(self):
        weeks = tuple(range(1, 9))
        players = tuple(
            player(
                f"p{index}",
                projected=200 - index,
                points={week: 20 - index for week in weeks},
            )
            for index in range(1, 7)
        )
        season = historical(players, weeks)
        config = league_config(6, regular=range(1, 6), playoffs=(6, 7, 8))
        rosters = tuple((f"p{index}",) for index in range(1, 7))

        first = simulate_historical_season(rosters, season, config)
        second = simulate_historical_season(rosters, season, config)

        self.assertEqual(first, second)
        self.assertEqual(first.champion_team_id, "drafter-1")
        self.assertEqual(first.champion_team_name, "Drafter #1")
        self.assertEqual(
            tuple(row.team_id for row in first.standings),
            tuple(f"drafter-{index}" for index in range(1, 7)),
        )
        self.assertTrue(all(row.made_playoffs for row in first.standings))
        self.assertEqual([row.finish_rank for row in first.standings], [1, 2, 3, 4, 5, 6])

        regular = [
            row for team in first.teams for row in team.weekly_results
            if row.stage is SeasonStage.REGULAR
        ]
        for team in first.teams:
            weeks_played = [
                row for row in team.weekly_results if row.stage is SeasonStage.REGULAR
            ]
            self.assertEqual(len(weeks_played), 5)
            self.assertEqual(len({row.opponent_team_id for row in weeks_played}), 5)
        self.assertFalse(any(row.outcome is GameOutcome.BYE for row in regular))

        round_one = [row for row in first.bracket_games if row.round_number == 1]
        self.assertEqual(len(round_one), 4)
        self.assertEqual(sum(row.is_bye for row in round_one), 2)
        self.assertTrue(all(row.higher_score is not None for row in round_one if row.is_bye))
        playoff_bye = next(
            row for row in first.teams[0].weekly_results
            if row.stage is SeasonStage.PLAYOFF and row.outcome is GameOutcome.BYE
        )
        self.assertEqual(playoff_bye.lineup[0].player_id, "p1")
        self.assertEqual(playoff_bye.score, 19.0)
        self.assertEqual(len(first.bracket_games), 7)
        review = first.to_record()
        self.assertEqual(review["champion_team_name"], "Drafter #1")
        review["champion_team_name"] = "Changed copy"
        self.assertEqual(first.champion_team_name, "Drafter #1")
        with self.assertRaises(FrozenInstanceError):
            first.champion_team_name = "Cannot mutate trace"

    def test_lineup_uses_preseason_then_prior_results_but_not_current_or_future_stats(self):
        weeks = (1, 2, 3, 4, 5)
        high = player(
            "high",
            projected=120,
            points={1: 0, 2: 1, 3: 1, 4: 1, 5: 1},
        )
        low = player(
            "low",
            projected=0,
            points={1: 100, 2: 200, 3: 200, 4: 200, 5: 200},
        )
        fillers = tuple(
            player(
                f"f{index}{suffix}",
                projected=20 - index,
                points={week: 5 + index for week in weeks},
            )
            for index in range(2, 5)
            for suffix in ("a", "b")
        )
        season = historical((high, low, *fillers), weeks)
        changed = historical(
            (
                high,
                player(
                    "low",
                    projected=0,
                    points={1: 100, 2: -999_999, 3: -999_999, 4: -999_999, 5: -999_999},
                ),
                *fillers,
            ),
            weeks,
        )
        config = league_config(
            4,
            regular=(1, 2, 3),
            playoffs=(4, 5),
            bench_slots=1,
        )
        rosters = (("high", "low"), ("f2a", "f2b"), ("f3a", "f3b"), ("f4a", "f4b"))

        base = simulate_historical_season(rosters, season, config)
        altered = simulate_historical_season(rosters, changed, config)
        base_weeks = base.teams[0].weekly_results
        altered_weeks = altered.teams[0].weekly_results

        self.assertEqual(base_weeks[0].lineup[0].player_id, "high")
        self.assertEqual(
            tuple(slot.player_id for slot in base_weeks[0].lineup),
            tuple(slot.player_id for slot in altered_weeks[0].lineup),
        )
        self.assertEqual(base_weeks[1].lineup[0].player_id, "low")
        self.assertEqual(
            tuple(slot.player_id for slot in base_weeks[1].lineup),
            tuple(slot.player_id for slot in altered_weeks[1].lineup),
        )
        self.assertNotEqual(base_weeks[1].score, altered_weeks[1].score)

    def test_lineup_uses_known_bye_but_not_same_week_actual_status(self):
        weeks = (1, 2, 3, 4)
        statuses = {
            1: ActualWeekStatus.INACTIVE,
            2: ActualWeekStatus.BYE,
            3: ActualWeekStatus.INACTIVE,
            4: ActualWeekStatus.PLAYED,
        }
        unavailable = player(
            "unavailable",
            projected=500,
            points={4: 10},
            statuses=statuses,
            bye_week=2,
        )
        available = player(
            "available",
            projected=1,
            points={week: 30 for week in weeks},
            bye_week=6,
        )
        fillers = tuple(
            player(f"f{index}{suffix}", projected=10, points={week: index for week in weeks})
            for index in range(2, 5)
            for suffix in ("a", "b")
        )
        season = historical((unavailable, available, *fillers), weeks)
        config = league_config(4, regular=(1, 2), playoffs=(3, 4), bench_slots=1)
        result = simulate_historical_season(
            (("unavailable", "available"), ("f2a", "f2b"),
             ("f3a", "f3b"), ("f4a", "f4b")),
            season,
            config,
        )
        by_week = {row.week: row for row in result.teams[0].weekly_results}
        self.assertEqual(by_week[1].lineup[0].player_id, "unavailable")
        self.assertEqual(by_week[2].lineup[0].player_id, "available")
        self.assertEqual(by_week[3].lineup[0].player_id, "unavailable")
        self.assertEqual(by_week[1].score, 0)
        self.assertEqual(by_week[3].score, 0)

        missing = player(
            "unavailable",
            projected=500,
            points={4: 10},
            statuses={**statuses, 3: ActualWeekStatus.MISSING},
            bye_week=2,
        )
        missing_season = historical((missing, available, *fillers), weeks)
        with self.assertRaisesRegex(ValueError, "missing outcomes"):
            simulate_historical_season(
                (("unavailable", "available"), ("f2a", "f2b"),
                 ("f3a", "f3b"), ("f4a", "f4b")),
                missing_season,
                config,
            )

    def test_odd_team_schedule_gives_each_team_one_bye_and_all_opponents(self):
        weeks = tuple(range(1, 8))
        season = historical(
            tuple(
                player(f"p{index}", projected=10, points={week: index for week in weeks})
                for index in range(1, 6)
            ),
            weeks,
        )
        result = simulate_historical_season(
            tuple((f"p{index}",) for index in range(1, 6)),
            season,
            league_config(5, regular=range(1, 6), playoffs=(6, 7), playoff_teams=4),
        )

        for team in result.teams:
            regular = [row for row in team.weekly_results if row.stage is SeasonStage.REGULAR]
            self.assertEqual(sum(row.outcome is GameOutcome.BYE for row in regular), 1)
            self.assertEqual(
                len({row.opponent_team_id for row in regular if row.opponent_team_id}),
                4,
            )

    def test_equal_scores_use_stable_team_order_and_higher_playoff_seed(self):
        weeks = (1, 2, 3, 4, 5)
        season = historical(
            tuple(
                player(f"p{index}", projected=10, points={week: 10 for week in weeks})
                for index in range(1, 5)
            ),
            weeks,
        )
        result = simulate_historical_season(
            tuple((f"p{index}",) for index in range(1, 5)),
            season,
            league_config(4, regular=(1, 2, 3), playoffs=(4, 5)),
        )

        self.assertEqual(tuple(row.team_id for row in result.standings), (
            "drafter-1", "drafter-2", "drafter-3", "drafter-4"
        ))
        self.assertEqual(result.champion_team_id, "drafter-1")
        played = [row for row in result.bracket_games if not row.is_bye]
        self.assertTrue(all(row.decided_by_seed for row in played))

    def test_playoff_upset_does_not_reorder_regular_season_standings(self):
        weeks = (1, 2, 3, 4, 5)
        regular_scores = (100, 80, 60, 40)
        semifinal_scores = (0, 90, 10, 200)
        final_scores = (0, 0, 10, 200)
        season = historical(
            tuple(
                player(
                    f"p{index}",
                    projected=100 - index,
                    points={
                        1: regular_scores[index - 1],
                        2: regular_scores[index - 1],
                        3: regular_scores[index - 1],
                        4: semifinal_scores[index - 1],
                        5: final_scores[index - 1],
                    },
                )
                for index in range(1, 5)
            ),
            weeks,
        )
        result = simulate_historical_season(
            tuple((f"p{index}",) for index in range(1, 5)),
            season,
            league_config(4, regular=(1, 2, 3), playoffs=(4, 5)),
        )

        self.assertEqual(result.champion_team_id, "drafter-4")
        self.assertEqual(
            tuple(row.team_id for row in result.standings),
            ("drafter-1", "drafter-2", "drafter-3", "drafter-4"),
        )
        self.assertEqual(
            tuple(row.finish_rank for row in result.standings),
            (3, 2, 4, 1),
        )

    def test_general_bracket_finishes_every_supported_field_size(self):
        for team_count in range(2, 17):
            with self.subTest(team_count=team_count):
                rounds = math.ceil(math.log2(team_count))
                weeks = tuple(range(1, rounds + 2))
                season = historical(
                    tuple(
                        player(
                            f"p{index}",
                            projected=100 - index,
                            points={week: 100 - index for week in weeks},
                        )
                        for index in range(1, team_count + 1)
                    ),
                    weeks,
                )
                result = simulate_historical_season(
                    tuple((f"p{index}",) for index in range(1, team_count + 1)),
                    season,
                    league_config(
                        team_count,
                        regular=(1,),
                        playoffs=range(2, rounds + 2),
                    ),
                )

                self.assertEqual(
                    sum(not row.is_bye for row in result.bracket_games),
                    team_count - 1,
                )
                self.assertEqual(
                    sum(row.is_bye for row in result.bracket_games),
                    2 ** rounds - team_count,
                )
                self.assertEqual(
                    sorted(row.finish_rank for row in result.teams),
                    list(range(1, team_count + 1)),
                )

    def test_custom_scoring_and_flex_use_one_exact_lineup_assignment(self):
        weeks = (1, 2, 3)
        roster_groups = []
        players = []
        for team in range(1, 5):
            roster = (
                (f"t{team}-rb1", "RB", 100, 10),
                (f"t{team}-rb2", "RB", 90, 9),
                (f"t{team}-wr1", "WR", 80, 8),
                (f"t{team}-wr2", "WR", 70, 7),
            )
            roster_groups.append(tuple(row[0] for row in roster))
            players.extend(
                player(
                    player_id,
                    position=position,
                    projected=projected,
                    points={week: actual for week in weeks},
                )
                for player_id, position, projected, actual in roster
            )
        result = simulate_historical_season(
            tuple(roster_groups),
            historical(tuple(players), weeks),
            league_config(
                4,
                regular=(1,),
                playoffs=(2, 3),
                bench_slots=1,
                slots=("RB", "WR", "FLEX"),
                scoring_weights={"points": 2.0},
            ),
        )

        week_one = result.teams[0].weekly_results[0]
        self.assertEqual(
            tuple(row.player_id for row in week_one.lineup),
            ("t1-rb1", "t1-wr1", "t1-rb2"),
        )
        self.assertEqual(week_one.score, 54.0)

    def test_lineup_uses_canonical_ensemble_stats_without_provider_duplication(self):
        weeks = (1, 2, 3)

        def source(player_id, ensemble, espn, yahoo):
            return replace(
                player(
                    player_id,
                    projected=0,
                    points={week: 1 for week in weeks},
                ),
                preseason_features={
                    "ensemble.projected_stat.bonus": ensemble,
                    "espn.projected_stat.bonus": espn,
                    "yahoo.projected_stat.bonus": yahoo,
                },
            )

        low_ensemble = source("low-ensemble", 10, 1_000, 1_000)
        high_ensemble = source("high-ensemble", 20, 1, 1)
        fillers = tuple(
            source(f"f{index}{suffix}", 5, 5, 5)
            for index in range(2, 5)
            for suffix in ("a", "b")
        )
        result = simulate_historical_season(
            (("low-ensemble", "high-ensemble"), ("f2a", "f2b"),
             ("f3a", "f3b"), ("f4a", "f4b")),
            historical((low_ensemble, high_ensemble, *fillers), weeks),
            league_config(
                4,
                regular=(1,),
                playoffs=(2, 3),
                bench_slots=1,
                scoring_weights={"bonus": 1.0},
            ),
        )

        self.assertEqual(
            result.teams[0].weekly_results[0].lineup[0].player_id,
            "high-ensemble",
        )

    def test_rejects_invalid_or_incomplete_rosters_and_week_coverage(self):
        weeks = (1, 2, 3, 4, 5)
        season = historical(
            tuple(
                player(f"p{index}", projected=10, points={week: 10 for week in weeks})
                for index in range(1, 6)
            ),
            weeks,
        )
        config = league_config(4, regular=(1, 2, 3), playoffs=(4, 5))
        valid = (("p1",), ("p2",), ("p3",), ("p4",))
        cases = (
            (valid[:3], "every configured team"),
            ((("p1",), ("p1",), ("p3",), ("p4",)), "more than one roster"),
            ((("p1",), ("p2",), ("p3",), ("missing",)), "historical season"),
            ((("p1", "p5"), ("p2",), ("p3",), ("p4",)), "roster size"),
        )
        for rosters, message in cases:
            with self.subTest(message):
                with self.assertRaisesRegex(ValueError, message):
                    simulate_historical_season(rosters, season, config)

        short_players = tuple(
            player(
                row.player_id,
                projected=row.preseason_features["projected_fantasy_points"],
                points={week: 10 for week in (1, 2, 3, 4)},
            )
            for row in season.players
        )
        short = historical(short_players, (1, 2, 3, 4))
        with self.assertRaisesRegex(ValueError, "week coverage"):
            simulate_historical_season(valid, short, config)

        with self.assertRaisesRegex(ValueError, "one week per bracket round"):
            league_config(4, regular=(1, 2), playoffs=(3, 4, 5))


def league_config(
    team_count,
    *,
    regular,
    playoffs,
    bench_slots=0,
    playoff_teams=None,
    slots=("FLEX",),
    scoring_weights=None,
):
    return DraftLeagueConfig(
        name="Test league",
        team_count=team_count,
        starting_slots=slots,
        bench_slots=bench_slots,
        slot_eligibility=default_slot_eligibility(slots),
        position_limits={},
        scoring_weights=scoring_weights or {"points": 1.0},
        regular_season_weeks=tuple(regular),
        playoff_team_count=playoff_teams or team_count,
        playoff_weeks=tuple(playoffs),
        strategy_counts={DraftStrategy.NONE: team_count},
    )


def historical(players, weeks):
    return HistoricalSeason(
        season=2019,
        preseason_as_of="2019-08-01T00:00:00+00:00",
        season_kickoff_at="2019-09-05T00:00:00+00:00",
        available_weeks=tuple(weeks),
        players=tuple(players),
    )


def player(
    player_id,
    *,
    projected,
    points,
    statuses=None,
    bye_week=20,
    position="RB",
):
    statuses = statuses or {}
    weeks = tuple(sorted(set(points) | set(statuses)))
    actual = []
    for week in weeks:
        status = statuses.get(week, ActualWeekStatus.PLAYED)
        stats = {"points": points[week]} if status is ActualWeekStatus.PLAYED else {}
        actual.append(ActualPlayerWeek(week, status, stats))
    return PreseasonPlayer(
        player_id=player_id,
        display_name=f"Player {player_id}",
        position=position,
        eligible_positions=(position,),
        nfl_team_id="NFL",
        bye_week=bye_week,
        nfl_experience_years=2,
        rookie=False,
        first_year_on_team=False,
        preseason_features={"projected_fantasy_points": projected},
        actual_weeks=tuple(actual),
    )


if __name__ == "__main__":
    unittest.main()
