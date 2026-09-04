from dataclasses import replace
import unittest

from trade_snapshot.draft_availability import (
    AvailabilityStatus as Status, RosterAvailabilityReport as Report,
    infer_zero_point_absences, prepare_availability,
)
from trade_snapshot.draft_config import DraftLeagueConfig
from trade_snapshot.draft_history import DataProvenance, HistoricalCorpus
from trade_snapshot.draft_season import _prepare_scoring_context, simulate_historical_season
from tests.test_draft_season import historical, league_config, player


class RosterManagementTests(unittest.TestCase):
    def test_bench_return_and_season_ending_waiver_including_playoffs(self):
        weeks = (1, 2, 3, 4)
        players = tuple(player(name, projected=projection,
                               points={week: 10 for week in weeks}, position=position)
                        for name, projection, position in (
                            ("starter", 400, "RB"), ("bench", 80, "RB"),
                            ("opponent", 300, "RB"), ("oppbench", 70, "RB"),
                            ("replacement", 64, "RB"), ("wrongposition", 5000, "QB"),
                            ("futurestar", 56, "RB")))
        season = replace(historical(players, weeks), availability_reports=(
            Report("starter", 2, Status.IR, 1, "Explicit IR report"),
            Report("starter", 3, Status.ACTIVE, 2, "Activated from IR"),
            Report("starter", 4, Status.SEASON_ENDING_IR, 3, "Explicit season-ending report"),
        ))
        config = league_config(2, regular=(1, 2, 3), playoffs=(4,), bench_slots=1)
        rosters = (("starter", "bench"), ("opponent", "oppbench"))
        trace = simulate_historical_season(rosters, season, config)
        team = trace.teams[0]
        self.assertEqual([row.lineup[0].player_id for row in team.weekly_results],
                         ["starter", "bench", "starter", "bench"])
        self.assertEqual(team.roster_player_ids, ("bench", "replacement"))
        self.assertEqual([(row.week, row.action) for row in team.roster_moves],
                         [(2, "bench"), (4, "drop"), (4, "add")])
        changed = replace(season, players=tuple(
            player("futurestar", projected=56, points={1: 10, 2: 10, 3: 10, 4: 100000})
            if row.player_id == "futurestar" else row for row in season.players))
        self.assertEqual(simulate_historical_season(rosters, changed, config).teams[0].roster_moves,
                         team.roster_moves)

    def test_shared_waivers_reverse_standings_and_one_claim_per_pass(self):
        weeks = (1, 2)
        players = tuple(player(name, projected=points * 10, points={1: points, 2: points})
                        for name, points in (("a", 20), ("b", 10), ("c", 8),
                                             ("d", 7), ("fa1", 6), ("fa2", 5), ("fa3", 4)))
        season = replace(historical(players, weeks), availability_reports=tuple(
            Report(name, 2, Status.SEASON_ENDING_IR, 1, "Confirmed season-ending")
            for name in ("a", "c", "d")))
        config = replace(league_config(2, regular=(1,), playoffs=(2,), bench_slots=1),
                         position_limits={"RB": 2})
        trace = simulate_historical_season((("a", "b"), ("c", "d")), season, config)
        self.assertEqual(trace.teams[0].roster_player_ids, ("b", "fa2"))
        self.assertEqual(trace.teams[1].roster_player_ids, ("fa1", "fa3"))
        all_ids = [pid for team in trace.teams for pid in team.roster_player_ids]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_empty_waiver_pool_stays_open_without_readding_ir(self):
        weeks = (1, 2)
        season = replace(historical(tuple(player(name, projected=10, points={1: 1, 2: 1})
                                          for name in ("a", "b")), weeks),
                         availability_reports=(Report("a", 2, Status.SEASON_ENDING_IR, 1, "IR"),))
        trace = simulate_historical_season((("a",), ("b",)), season,
                                          league_config(2, regular=(1,), playoffs=(2,)))
        self.assertEqual(trace.teams[0].roster_player_ids, ())
        self.assertEqual(trace.teams[0].weekly_results[-1].score, 0)
        self.assertEqual(trace.teams[0].roster_moves[-1].action, "unfilled")

    def test_zero_point_fallback_is_opt_in_past_only_and_bye_safe(self):
        from trade_snapshot.draft_history import ActualWeekStatus
        weeks = (1, 2, 3, 4, 5)
        candidate = player("a", projected=100, points={1: 0, 3: 0, 4: 20, 5: 0},
                           statuses={2: ActualWeekStatus.BYE}, bye_week=2)
        season = historical((candidate,), weeks)
        base = league_config(2, regular=(1, 2, 3, 4), playoffs=(5,))
        scores = _prepare_scoring_context(season, base).actual_scores
        self.assertEqual(infer_zero_point_absences(candidate, scores, weeks, base), {})
        config = replace(base, zero_point_out_weeks=1, zero_point_ir_weeks=2, zero_point_drop_weeks=3)
        reports = infer_zero_point_absences(candidate, scores, weeks, config)
        self.assertNotIn(1, reports)
        self.assertEqual(reports[3].status, Status.INFERRED_OUT)
        self.assertEqual(reports[4].status, Status.INFERRED_IR)
        self.assertNotIn(5, reports)
        self.assertTrue(all(row.source_week < row.week for row in reports.values()))
        kicker = replace(candidate, position="K", eligible_positions=("K",))
        self.assertEqual(infer_zero_point_absences(kicker, scores, weeks, config), {})
        sparse = replace(candidate, actual_weeks=tuple(row for row in candidate.actual_weeks if row.week != 2))
        self.assertEqual(infer_zero_point_absences(sparse, scores, weeks, config)[4].status,
                         Status.INFERRED_OUT)
        self.assertEqual(DraftLeagueConfig.from_record(config.to_record()), config)
        self.assertEqual(base.to_record()["schema_version"], 1)
        with self.assertRaises(ValueError):
            replace(base, zero_point_out_weeks=4, zero_point_drop_weeks=2)

    def test_availability_evidence_and_corpus_version_boundaries(self):
        reports = (Report("a", 2, Status.IR, 1, "IR"), Report("a", 3, Status.OUT, 2, "Reserve"))
        views = prepare_availability(reports, (1, 2, 3, 4))
        self.assertEqual(views[4]["a"].status, Status.IR)
        with self.assertRaises(ValueError):
            Report("a", 2, Status.IR, 2, "Not available before lock")
        with self.assertRaises(ValueError):
            Report.from_record(Report("a", 2, Status.EXTENDED_ABSENCE, 1, "Not an explicit injury").to_record())
        season = historical((player("a", projected=100, points={1: 1, 2: 0, 3: 0, 4: 0}),), (1, 2, 3, 4))
        provenance = (DataProvenance("Synthetic verification", "2019-08-01T00:00:00+00:00",
                                    "Synthetic demonstration, not real NFL injury data",
                                    preseason_feature_names=("projected_fantasy_points",),
                                    preseason_source_as_of={2019: "2019-08-01T00:00:00+00:00"}),)
        legacy = HistoricalCorpus((season,), provenance)
        enhanced = HistoricalCorpus((replace(season, availability_reports=reports),), provenance)
        self.assertEqual(legacy.to_record()["schema_version"], 1)
        self.assertEqual(enhanced.to_record()["schema_version"], 2)
        self.assertEqual(HistoricalCorpus.from_record(legacy.to_record()), legacy)
        self.assertEqual(HistoricalCorpus.from_record(enhanced.to_record()), enhanced)

    def test_enabled_long_zero_streak_can_escalate_explicit_ir_to_drop(self):
        season = historical(tuple(player(name, projected=100, points={1: 0, 2: 0})
                                   for name in ("a", "b")), (1, 2))
        season = replace(season, availability_reports=(Report("a", 2, Status.IR, 1, "Explicit IR"),))
        config = replace(league_config(2, regular=(1,), playoffs=(2,)), zero_point_drop_weeks=1)
        trace = simulate_historical_season((("a",), ("b",)), season, config)
        self.assertEqual(trace.teams[0].roster_player_ids, ())
        self.assertEqual(trace.teams[0].roster_moves[0].action, "drop")
        self.assertIn("not a confirmed injury", trace.teams[0].roster_moves[0].reason)
        self.assertIn("drop 1 week", trace.roster_management_policy)


if __name__ == "__main__":
    unittest.main()
