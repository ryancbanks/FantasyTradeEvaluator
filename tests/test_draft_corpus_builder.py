import csv
import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trade_snapshot.draft_corpus_builder import (
    SourceStamp,
    StarterCorpusFiles,
    build_starter_corpus,
)
from trade_snapshot.draft_corpus_sources import (
    load_ffc_adp,
    load_player_week_stats,
    load_schedules,
    normalized_player_name,
)

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
            row = load_player_week_stats(stats, 2025)["p1"][1]
            self.assertEqual(row["interceptions"], 0.0)
            self.assertEqual(row["fumbles_lost"], 0.0)


class DraftCorpusBuilderTests(unittest.TestCase):
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


def _starter_files(root: Path, *, bye_conflict=False):
    schedule = root / "games.csv.gz"
    matchups = {
        1: ((0, 4), (1, 5), (2, 6), (3, 7)),
        2: ((4, 8), (5, 9), (6, 10), (7, 11)),
        3: ((0, 8), (1, 9), (2, 10), (3, 11)),
    }
    games = []
    for week, pairs in matchups.items():
        for away, home in pairs:
            games.append(
                [
                    2025,
                    "REG",
                    week,
                    f"2025-09-{week + 3:02d}",
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
        stat_rows.append([player_id, 2025, week, "REG", 10, 1, 0])
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
    stamp = SourceStamp(
        "https://github.com/nflverse/nflverse-data",
        "a" * 64,
        1,
        "2026-01-01T00:00:00+00:00",
    )
    return StarterCorpusFiles(
        schedule,
        {2025: ffc},
        {2025: stats},
        {2025: team_stats},
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
