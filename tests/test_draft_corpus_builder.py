import csv
import gc
import gzip
import json
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory

from trade_snapshot.draft_corpus_builder import (
    SourceStamp,
    StarterCorpusFiles,
    _dst_week,
    build_starter_corpus,
)
from trade_snapshot.draft_config import DraftLeagueConfig, score_raw_stats
from trade_snapshot.draft_corpus_sources import (
    ScheduleSeason,
    load_ffc_adp,
    load_player_week_stats,
    load_schedules,
    normalized_player_name,
)
from trade_snapshot.draft_features import candidate_feature_values, fit_feature_schema
from trade_snapshot.draft_preseason_projection import build_preseason_projection

TEAMS = tuple(f"T{letter}" for letter in "ABCDEFGHIJKL")
POSITIONS = ("QB", "RB", "WR", "TE", "K")


class DraftCorpusSourceTests(unittest.TestCase):
    def test_exact_name_normalization_is_punctuation_and_suffix_stable(self):
        self.assertEqual(normalized_player_name("D.J. Moore Jr."), "djmoore")
        self.assertEqual(normalized_player_name("D’J Moore II"), "djmoore")
        self.assertNotEqual(
            normalized_player_name("DJ Moore"), normalized_player_name("DJ More")
        )

    def test_ffc_requires_a_genuine_preseason_window(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "adp.json"
            _write_ffc(path, end_date="2025-09-05")
            with self.assertRaisesRegex(ValueError, "not preseason"):
                load_ffc_adp(path, 2025, "2025-09-04T12:00:00+00:00")

    def test_csv_adapters_reject_duplicate_header_and_preserve_zero_aliases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.csv"
            bad.write_text("season,season,week\n2025,2025,1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fields are incompatible"):
                load_schedules(bad, (2025,))

            stats = root / "stats.csv.gz"
            _write_csv(
                stats,
                ["player_id", "season", "week", "season_type", "passing_interceptions"],
                [["p1", 2025, 1, "REG", 0]],
            )
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                row = load_player_week_stats(stats, 2025)["p1"][1]
                gc.collect()
            self.assertEqual(row["interceptions"], 0.0)
            self.assertEqual(row["fumbles_lost"], 0.0)
            self.assertFalse(
                [warning for warning in caught if warning.category is ResourceWarning]
            )


class DraftCorpusBuilderTests(unittest.TestCase):
    def test_dst_excludes_team_offense_from_outcomes_and_preseason_points(self):
        schedule = ScheduleSeason(
            2016, "2016-09-08T00:00:00+00:00", (1,), {"NYG": 8}, {("NYG", 1): 19}
        )
        stats = _dst_week("NYG", 1, {("NYG", 1): {
            "passing_yards": 207, "passing_tds": 3, "receptions": 19,
            "receiving_yards": 207, "receiving_tds": 3, "rushing_yards": 113,
            "def_sacks": 2, "def_fumbles": 3, "fumble_recovery_opp": 1,
            "fumble_recovery_tds": 1,
        }}, schedule)
        self.assertTrue(all(name.startswith("dst_") for name in stats))
        self.assertEqual(stats["dst_fumble_recoveries"], 1)
        self.assertEqual(stats["dst_unclassified_recovery_touchdowns"], 1)
        self.assertEqual(stats["dst_touchdowns"], 0)
        self.assertEqual(score_raw_stats(stats, DraftLeagueConfig.standard_ppr().scoring_weights), 5)
        projected = build_preseason_projection({1: stats}, projected_games=16)
        self.assertEqual(projected["projected_stat.passing_yards"], 0)
        self.assertEqual(projected["projected_fantasy_points"], 80)

    def test_builds_a_strict_corpus_with_transparent_gaps(self):
        with TemporaryDirectory() as directory:
            files = _starter_files(Path(directory))
            result = build_starter_corpus(files, years=(2025,))

        self.assertEqual(result.status, "ready_with_gaps")
        self.assertEqual(result.corpus.available_seasons, (2025,))
        season = result.corpus.seasons[0]
        self.assertEqual(len(season.players), 77)
        self.assertLess(result.serialized_bytes, 128 * 1024 * 1024)
        coverage = result.coverage["seasons"][0]
        self.assertEqual(coverage["installed_by_position"]["DST"], 12)
        self.assertGreater(coverage["gaps"]["roster_player_without_adp"], 0)
        self.assertEqual(coverage["projected_players"], 76)
        self.assertEqual(coverage["missing_projection_players"], 1)
        self.assertEqual(
            coverage["gaps"]["preseason_projection_unavailable"], 1
        )
        self.assertEqual(result.coverage["projected_player_seasons"], 76)
        self.assertIn(
            "projected_fantasy_points_per_game",
            result.coverage["preseason_feature_names"],
        )
        projected = next(
            row for row in season.players if row.display_name == "QB Player 2"
        )
        missing = next(
            row for row in season.players if row.display_name == "QB Player 1"
        )
        defense = next(row for row in season.players if row.position == "DST")
        self.assertEqual(projected.preseason_features["projected_games"], 2)
        self.assertEqual(
            projected.preseason_features["projected_fantasy_points"], 4
        )
        self.assertEqual(
            projected.preseason_features["projected_fantasy_points_per_game"], 2
        )
        self.assertEqual(
            projected.preseason_features["projected_stat.rushing_yards"], 20
        )
        self.assertEqual(
            defense.preseason_features["projected_stat.dst_sacks"], 4
        )
        self.assertIsNone(
            missing.preseason_features["projected_fantasy_points"]
        )
        model_inputs = candidate_feature_values(
            projected,
            config=DraftLeagueConfig.standard_ppr(),
            round_number=1,
            overall_pick=1,
            roster_player_positions=(),
            available_position_counts={
                position: count
                for position, count in coverage["installed_by_position"].items()
            },
            picks_until_next=22,
        )
        for name in (
            "position.qb",
            "preseason.adp",
            "preseason.projected_fantasy_points",
            "preseason.projected_fantasy_points_per_game",
            "preseason.projected_games",
            "preseason.projected_stat.rushing_yards",
            "bio.bye_week",
            "bio.experience_years",
            "bio.rookie",
            "bio.first_year_on_team",
        ):
            self.assertIn(name, model_inputs)
        self.assertFalse(
            any(
                token in name
                for name in model_inputs
                for token in ("player_id", "display_name", "nfl_team")
            )
        )
        schema = fit_feature_schema(
            result.corpus,
            DraftLeagueConfig.standard_ppr(),
            training_years=(2025,),
        )
        for name in (
            "position.qb",
            "preseason.adp",
            "preseason.projected_fantasy_points",
            "preseason.projected_fantasy_points_per_game",
            "preseason.projected_games",
            "preseason.projected_stat.rushing_yards",
            "bio.bye_week",
            "bio.experience_years",
            "bio.rookie",
            "bio.first_year_on_team",
        ):
            self.assertIn(name, schema.names)
        self.assertEqual(
            result.corpus, type(result.corpus).from_record(result.corpus.to_record())
        )

    def test_excludes_a_player_who_plays_during_fixed_preseason_bye(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            files = _starter_files(root, bye_conflict=True)
            result = build_starter_corpus(files, years=(2025,))

        coverage = result.coverage["seasons"][0]
        self.assertEqual(len(result.corpus.seasons[0].players), 76)
        self.assertEqual(coverage["gaps"]["played_during_fixed_preseason_bye"], 1)

    def test_target_season_outcomes_cannot_change_preseason_projections(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = build_starter_corpus(
                _starter_files(root, target_rushing_yards=10), years=(2025,)
            )
            extreme = build_starter_corpus(
                _starter_files(root, target_rushing_yards=10_000), years=(2025,)
            )

        def player(result):
            return next(
                row
                for row in result.corpus.seasons[0].players
                if row.display_name == "QB Player 2"
            )

        ordinary_player = player(ordinary)
        extreme_player = player(extreme)
        self.assertEqual(
            ordinary_player.preseason_features,
            extreme_player.preseason_features,
        )
        self.assertNotEqual(
            ordinary_player.actual_weeks[0].stats["rushing_yards"],
            extreme_player.actual_weeks[0].stats["rushing_yards"],
        )


def _starter_files(root: Path, *, bye_conflict=False, target_rushing_yards=10):
    schedule = root / "games.csv.gz"
    matchups = {
        1: ((0, 4), (1, 5), (2, 6), (3, 7)),
        2: ((4, 8), (5, 9), (6, 10), (7, 11)),
        3: ((0, 8), (1, 9), (2, 10), (3, 11)),
    }
    games = []
    for season in (2024, 2025):
        for week, pairs in matchups.items():
            for away, home in pairs:
                games.append(
                    [
                        season,
                        "REG",
                        week,
                        f"{season}-09-{week + 3:02d}",
                        TEAMS[away],
                        TEAMS[home],
                        10,
                        20,
                    ]
                )
    _write_csv(
        schedule,
        [
            "season",
            "game_type",
            "week",
            "gameday",
            "away_team",
            "home_team",
            "away_score",
            "home_score",
        ],
        games,
    )
    ffc = root / "ffc.json"
    _write_ffc(ffc)
    current = root / "roster_2025.csv.gz"
    prior = root / "roster_2024.csv.gz"
    roster_rows = []
    prior_rows = []
    players = []
    for position_index, position in enumerate(POSITIONS):
        for index in range(13):
            player_id = f"00-{position_index:02d}{index:04d}"
            name = f"{position} Player {index + 1}"
            team = TEAMS[index % len(TEAMS)]
            players.append((player_id, name, position, team))
            roster_rows.append(
                [
                    2025,
                    1,
                    "REG",
                    team,
                    position,
                    name,
                    player_id,
                    index % 5,
                    2020,
                    2020,
                    "ACT",
                ]
            )
            prior_rows.append(
                [
                    2024,
                    18,
                    "REG",
                    team,
                    position,
                    name,
                    player_id,
                    max(0, index % 5 - 1),
                    2020,
                    2020,
                    "ACT",
                ]
            )
    roster_fields = [
        "season",
        "week",
        "game_type",
        "team",
        "position",
        "full_name",
        "gsis_id",
        "years_exp",
        "entry_year",
        "rookie_year",
        "status",
    ]
    _write_csv(current, roster_fields, roster_rows)
    _write_csv(prior, roster_fields, prior_rows)

    stats = root / "player_stats.csv.gz"
    stat_rows = []
    schedules = load_schedules(schedule, (2025,))[2025]
    for index, (player_id, _, _, team) in enumerate(players):
        week = (
            schedules.bye_by_team[team]
            if bye_conflict and index == 0
            else next(
                value
                for value in schedules.available_weeks
                if value != schedules.bye_by_team[team]
            )
        )
        stat_rows.append(
            [player_id, 2025, week, "REG", target_rushing_yards, 1, 0]
        )
    _write_csv(
        stats,
        [
            "player_id",
            "season",
            "week",
            "season_type",
            "rushing_yards",
            "receptions",
            "fumbles_lost_total",
        ],
        stat_rows,
    )
    prior_stats = root / "player_stats_2024.csv.gz"
    _write_csv(
        prior_stats,
        [
            "player_id",
            "season",
            "week",
            "season_type",
            "rushing_yards",
            "receptions",
            "fumbles_lost_total",
        ],
        [
            [player_id, 2024, 1, "REG", 10, 1, 0]
            for index, (player_id, _, _, _) in enumerate(players)
            if index != 0
        ],
    )
    team_stats = root / "team_stats.csv.gz"
    team_rows = []
    for week, pairs in matchups.items():
        for first, second in pairs:
            for team in (TEAMS[first], TEAMS[second]):
                team_rows.append([2025, week, team, "REG", 2, 1, 0, 0, 0])
    _write_csv(
        team_stats,
        [
            "season",
            "week",
            "team",
            "season_type",
            "def_sacks",
            "def_interceptions",
            "def_fumbles",
            "def_tds",
            "def_safeties",
        ],
        team_rows,
    )
    prior_team_stats = root / "team_stats_2024.csv.gz"
    _write_csv(
        prior_team_stats,
        [
            "season",
            "week",
            "team",
            "season_type",
            "def_sacks",
            "def_interceptions",
            "def_fumbles",
            "def_tds",
            "def_safeties",
        ],
        [
            [2024, week, team, "REG", 2, 1, 0, 0, 0]
            for week, pairs in matchups.items()
            for first, second in pairs
            for team in (TEAMS[first], TEAMS[second])
        ],
    )
    stamp = SourceStamp(
        "https://github.com/nflverse/nflverse-data",
        "a" * 64,
        1,
        "2026-01-01T00:00:00+00:00",
    )
    return StarterCorpusFiles(
        schedule,
        {2025: ffc},
        {2024: prior_stats, 2025: stats},
        {2024: prior_team_stats, 2025: team_stats},
        {2024: prior, 2025: current},
        {"schedule": stamp},
    )


def _write_ffc(path: Path, *, end_date="2025-09-03"):
    players = []
    for index, position in enumerate((*POSITIONS, "DEF")):
        players.append(
            {
                "player_id": index + 1,
                "name": f"{position if position != 'DEF' else 'TA'} Player 1"
                if position != "DEF"
                else "TA Defense",
                "position": position,
                "team": "TA",
                "adp": float(index + 1),
                "stdev": 1.0,
                "high": 1,
                "low": 12,
                "bye": 3,
            }
        )
    path.write_text(
        json.dumps(
            {
                "status": "Success",
                "meta": {
                    "type": "PPR",
                    "teams": 12,
                    "rounds": 15,
                    "total_drafts": 10,
                    "start_date": "2025-08-01",
                    "end_date": end_date,
                },
                "players": players,
            }
        ),
        encoding="utf-8",
    )


def _write_csv(path: Path, fields, rows):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(fields)
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
