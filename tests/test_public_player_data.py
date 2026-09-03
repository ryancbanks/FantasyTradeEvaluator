from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import copy
import csv
import gzip
from io import StringIO
import json
from threading import Lock
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trade_snapshot._public_player_http import (
    DownloadedPublicData,
    PublicPlayerDataCancelled,
    PublicPlayerDataError,
    bounded_https_get,
)
from trade_snapshot.public_player_data import (
    DataAvailability,
    PublicPlayerDataSnapshot,
    PublicPlayerDataLimits,
    collect_public_player_data,
    public_player_source_urls,
    _STAT_FIELDS,
)


NOW = datetime(2026, 9, 2, 18, tzinfo=timezone.utc)
_STATS_IDENTITY_FIELDS = (
    "player_id",
    "player_display_name",
    "position",
    "headshot_url",
    "season",
    "week",
    "season_type",
    "game_id",
    "team",
    "opponent_team",
    "fantasy_points",
    "fantasy_points_ppr",
)
_INJURY_FIELDS = (
    "season",
    "game_type",
    "team",
    "week",
    "gsis_id",
    "position",
    "full_name",
    "first_name",
    "last_name",
    "report_primary_injury",
    "report_secondary_injury",
    "report_status",
    "practice_primary_injury",
    "practice_secondary_injury",
    "practice_status",
)
_CROSSWALK_FIELDS = (
    "mfl_id", "sportradar_id", "fantasypros_id", "gsis_id", "pff_id",
    "sleeper_id", "nfl_id", "espn_id", "yahoo_id", "fleaflicker_id",
    "cbs_id", "pfr_id", "cfbref_id", "rotowire_id", "rotoworld_id",
    "ktc_id", "stats_id", "stats_global_id", "fantasy_data_id", "swish_id",
    "name", "merge_name", "position", "team", "birthdate", "age",
    "draft_year", "draft_round", "draft_pick", "draft_ovr",
    "twitter_username", "height", "weight", "college", "db_season",
)
_TEST_NFL_TEAMS = (
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH",
)


def _csv_gzip(fields, rows):
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(output.getvalue().encode("utf-8"), mtime=0)


def _csv_bytes(fields, rows):
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _stats_file(
    season,
    *,
    wrong_season=False,
    headshot_url="https://static.www.nfl.com/image/private/example.png",
    team="KC",
    opponent="LV",
):
    fields = (*_STATS_IDENTITY_FIELDS, *_STAT_FIELDS)
    base = {field: "" for field in fields}
    regular = {
        **base,
        "player_id": "00-0030001",
        "player_display_name": "Example Runner",
        "position": "RB",
        "headshot_url": headshot_url,
        "season": str(season - 1 if wrong_season else season),
        "week": "2",
        "season_type": "REG",
        "game_id": f"{season}_02_{team}_{opponent}",
        "team": team,
        "opponent_team": opponent,
        "fantasy_points": "8.5",
        "fantasy_points_ppr": "11.5",
        "carries": "12",
        "receptions": "3",
        "receiving_yards": "30",
        "fumbles_total": "0",
    }
    postseason = {
        **regular,
        "week": "20",
        "season_type": "POST",
        "game_id": f"{season}_20_KC_BUF",
    }
    anonymous_team_event = {
        **base,
        "season": str(season),
        "week": "3",
        "season_type": "REG",
        "game_id": f"{season}_03_KC_LV",
        "team": "KC",
        "opponent_team": "LV",
        "fantasy_points": "0",
        "fantasy_points_ppr": "0",
        "def_safeties": "1",
    }
    padding = tuple(
        {
            **base,
            "player_id": f"00-TEST-{index:04d}",
            "player_display_name": f"Coverage Player {index}",
            "position": "WR",
            "season": regular["season"],
            "week": str(index % 16 + 1),
            "season_type": "REG",
            "game_id": f"{season}_{index % 16 + 1:02d}_TEST_{index:04d}",
            "team": _TEST_NFL_TEAMS[index % len(_TEST_NFL_TEAMS)],
            "opponent_team": _TEST_NFL_TEAMS[(index + 1) % len(_TEST_NFL_TEAMS)],
            "fantasy_points": "1",
            "fantasy_points_ppr": "1",
        }
        for index in range(512)
    )
    return _csv_gzip(
        fields, (regular, postseason, anonymous_team_event, *padding)
    )


def _injury_file(season, rows, *, with_modified):
    fields = (*_INJURY_FIELDS, *(("date_modified",) if with_modified else ()))
    base = {field: "" for field in fields}
    padding = tuple(
        {
            **base,
            "season": str(season),
            "game_type": "REG",
            "team": _TEST_NFL_TEAMS[index % len(_TEST_NFL_TEAMS)],
            "week": str(index % 16 + 1),
            "gsis_id": f"00-INJ-{index:04d}",
            "position": "WR",
            "full_name": f"Coverage Injury {index}",
            "report_primary_injury": "Ankle",
            "report_status": "Questionable",
            "practice_status": "Limited Participation in Practice",
            **(
                {"date_modified": "2025-10-03T12:00:00Z"}
                if with_modified
                else {}
            ),
        }
        for index in range(256)
    )
    return _csv_gzip(
        fields,
        ({**base, **row, "season": str(season)} for row in (*rows, *padding)),
    )


def _observed(body):
    return DownloadedPublicData(
        200,
        (
            ("etag", '"fixture"'),
            ("last-modified", "Wed, 02 Sep 2026 18:00:00 GMT"),
        ),
        body,
    )


def _not_published():
    return DownloadedPublicData(404, (), b"")


def _sleeper_players():
    return json.dumps(
        {
            "s1": {
                "player_id": "s1",
                "gsis_id": "00-0030001",
                "espn_id": 101,
                "full_name": "Example Runner",
                "position": "RB",
                "fantasy_positions": ["RB", "FLEX"],
                "team": "KC",
                "team_abbr": "KC",
                "active": True,
                "status": "Active",
                "injury_status": "Questionable",
                "injury_body_part": "Hamstring",
                "practice_participation": "Limited",
                "depth_chart_position": "RB",
                "depth_chart_order": 1,
                "years_exp": 3,
                "number": 22,
                "news_updated": 123456,
            },
            "KC": {
                "player_id": "KC",
                "full_name": None,
                "position": None,
                "fantasy_positions": [],
                "team": "KC",
                "active": True,
            },
            "stale": {
                "player_id": "stale",
                "full_name": "Source Outlier",
                "position": "OT",
                "fantasy_positions": ["OL"],
                "team": "OAK",
                "active": True,
                "years_exp": 122,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _crosswalk_file():
    base = {field: "NA" for field in _CROSSWALK_FIELDS}
    linked = {
        **base,
        "gsis_id": "00-0030001",
        "espn_id": "101",
        "sleeper_id": "s1",
        "name": "Example Runner",
        "position": "RB",
        "team": "KCC",
        "db_season": "2026",
    }
    return _csv_bytes(
        _CROSSWALK_FIELDS,
        (
            linked,
            {**linked, "name": "Duplicate source label"},
            {**base, "sleeper_id": "unlinked-only", "name": "One ID"},
        ),
    )


def _responses():
    sources = public_player_source_urls(2026)
    current_injuries = _injury_file(
        2026,
        (
            {
                "game_type": "REG",
                "team": "KC",
                "week": "2",
                "gsis_id": "00-0030001",
                "position": "  ",
                "full_name": "Example Runner",
                "report_primary_injury": "  Not injury related - personal matter ",
                "report_status": "Questionable",
                "practice_primary_injury": "Hamstring",
                "practice_status": "Limited Participation in Practice",
            },
        ),
        with_modified=False,
    )
    prior_injuries = _injury_file(
        2025,
        (
            {
                "game_type": "REG",
                "team": "KC",
                "week": "5",
                "gsis_id": "00-0030001",
                "position": "RB",
                "full_name": "Example Runner",
                "report_primary_injury": "Ankle",
                "report_status": "Questionable",
                "practice_status": "Limited Participation in Practice",
                "date_modified": "2025-10-01T12:00:00Z",
            },
            {
                "game_type": "REG",
                "team": "KC",
                "week": "5",
                "gsis_id": "00-0030001",
                "position": "RB",
                "full_name": "Example Runner",
                "report_primary_injury": "Ankle",
                "report_status": "Out",
                "practice_status": "Did Not Participate In Practice",
                "date_modified": "2025-10-02T12:00:00Z",
            },
        ),
        with_modified=True,
    )
    return {
        sources[0].url: _observed(_stats_file(2026)),
        sources[1].url: _observed(_stats_file(2025)),
        sources[2].url: _observed(current_injuries),
        sources[3].url: _observed(prior_injuries),
        sources[4].url: _not_published(),
        sources[5].url: _observed(_sleeper_players()),
        sources[6].url: _observed(b'[{"player_id":"s1","count":"7"}]'),
        sources[7].url: _observed(
            b'[{"player_id":"s1","count":2},{"player_id":"KC","count":1}]'
        ),
        sources[8].url: _observed(_crosswalk_file()),
    }


class _RecordingFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, **options):
        self.calls.append((url, options))
        result = self.responses[url]
        if isinstance(result, BaseException):
            raise result
        return result


class _ConcurrencyRecordingFetcher(_RecordingFetcher):
    def __init__(self, responses):
        super().__init__(responses)
        self._lock = Lock()
        self.active = 0
        self.peak = 0

    def __call__(self, url, **options):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            sleep(0.02)
            return super().__call__(url, **options)
        finally:
            with self._lock:
                self.active -= 1


class PublicPlayerDataTests(unittest.TestCase):
    def test_implausibly_empty_completed_sources_are_unavailable_not_clean_history(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[1].url] = _observed(
            _csv_gzip((*_STATS_IDENTITY_FIELDS, *_STAT_FIELDS), ())
        )
        responses[sources[3].url] = _observed(
            _csv_gzip((*_INJURY_FIELDS, "date_modified"), ())
        )

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertIs(
            snapshot.previous_stats.availability, DataAvailability.UNAVAILABLE
        )
        self.assertIs(
            snapshot.injury_history[1].availability,
            DataAvailability.UNAVAILABLE,
        )
        unavailable = {
            row.dataset: row for row in snapshot.provenance
            if row.availability is DataAvailability.UNAVAILABLE
        }
        self.assertIn("nflverse_player_stats_previous", unavailable)
        self.assertIn("nflverse_injuries_previous", unavailable)
        self.assertTrue(all(row.byte_count == 0 for row in unavailable.values()))

    def test_header_only_current_preseason_sources_remain_not_published(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[0].url] = _observed(
            _csv_gzip((*_STATS_IDENTITY_FIELDS, *_STAT_FIELDS), ())
        )
        responses[sources[2].url] = _observed(
            _csv_gzip((*_INJURY_FIELDS,), ())
        )

        snapshot = collect_public_player_data(
            2026,
            as_of_week=1,
            clock=lambda: NOW,
            fetcher=_RecordingFetcher(responses),
        )

        self.assertIs(
            snapshot.current_stats.availability, DataAvailability.NOT_PUBLISHED
        )
        self.assertIs(
            snapshot.injury_history[0].availability,
            DataAvailability.NOT_PUBLISHED,
        )

    def test_historical_team_relocations_are_canonicalized_without_losing_source(self):
        from trade_snapshot._public_player_parse import (
            parse_injury_csv,
            parse_stats_csv,
        )

        stats = parse_stats_csv(
            gzip.decompress(_stats_file(2016, team="SD", opponent="OAK")),
            2016,
            1_000,
            lambda: False,
        )
        injuries = parse_injury_csv(
            gzip.decompress(
                _injury_file(
                    2016,
                    ({
                        "game_type": "REG",
                        "team": "STL",
                        "week": "4",
                        "gsis_id": "00-0030001",
                        "position": "RB",
                        "full_name": "Example Runner",
                        "report_primary_injury": "Ankle",
                        "report_status": "Questionable",
                    },),
                    with_modified=False,
                )
            ),
            2016,
            1_000,
            lambda: False,
        )

        self.assertEqual((stats[0].nfl_team_id, stats[0].opponent_team_id), ("LAC", "LV"))
        self.assertEqual(injuries[0].nfl_team_id, "LAR")

    def test_untrusted_headshot_is_omitted_without_discarding_season_stats(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[0].url] = _observed(
            _stats_file(2026, headshot_url="https://images.example.test/player.png")
        )

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertIs(snapshot.current_stats.availability, DataAvailability.OBSERVED)
        self.assertEqual(len(snapshot.current_stats.rows), 513)
        example = next(
            row for row in snapshot.current_stats.rows
            if row.gsis_id == "00-0030001"
        )
        self.assertIsNone(example.headshot_url)

    def test_bulk_sources_use_a_bounded_parallel_pool(self):
        fetcher = _ConcurrencyRecordingFetcher(_responses())

        snapshot = collect_public_player_data(2026, clock=lambda: NOW, fetcher=fetcher)

        self.assertTrue(snapshot.current_stats.rows)
        self.assertGreater(fetcher.peak, 1)
        self.assertLessEqual(fetcher.peak, 4)

    def test_collects_each_bulk_source_once_and_retains_profile_evidence(self):
        fetcher = _RecordingFetcher(_responses())

        snapshot = collect_public_player_data(2026, clock=lambda: NOW, fetcher=fetcher)

        source_urls = tuple(row.url for row in public_player_source_urls(2026))
        self.assertCountEqual(tuple(url for url, _ in fetcher.calls), source_urls)
        self.assertEqual(len(set(source_urls)), 9)
        self.assertTrue(all(call[1]["timeout_seconds"] == 30 for call in fetcher.calls))
        self.assertEqual(len(snapshot.current_stats.rows), 513)
        stats = next(
            row for row in snapshot.current_stats.rows
            if row.gsis_id == "00-0030001"
        )
        self.assertEqual(stats.fantasy_points_ppr, 11.5)
        self.assertEqual(dict(stats.stat_values)["receptions"], 3.0)
        self.assertNotIn("fumbles_total", dict(stats.stat_values))
        self.assertEqual(len(snapshot.injury_history), 3)
        current_report = next(
            row for row in snapshot.injury_history[0].rows
            if row.gsis_id == "00-0030001"
        )
        self.assertEqual(
            current_report.report_primary_injury,
            "Not injury related - personal matter",
        )
        self.assertIsNone(current_report.source_modified_at)
        self.assertIsNone(current_report.position)
        prior_report = next(
            row for row in snapshot.injury_history[1].rows
            if row.gsis_id == "00-0030001"
        )
        self.assertEqual(prior_report.report_status, "out")
        self.assertEqual(prior_report.practice_status, "did_not_participate")
        self.assertEqual(
            prior_report.source_modified_at,
            datetime(2025, 10, 2, 12, tzinfo=timezone.utc),
        )
        self.assertIs(
            snapshot.injury_history[2].availability,
            DataAvailability.UNAVAILABLE,
        )
        players = {row.sleeper_player_id: row for row in snapshot.sleeper_players}
        self.assertEqual(players["s1"].espn_id, "101")
        self.assertEqual(players["s1"].injury_status, "Questionable")
        self.assertEqual(players["KC"].display_name, "KC D/ST")
        self.assertEqual(players["KC"].position, "DEF")
        self.assertIsNone(players["stale"].nfl_team_id)
        self.assertIsNone(players["stale"].years_experience)
        trends = {row.sleeper_player_id: row for row in snapshot.trends}
        self.assertEqual((trends["s1"].adds, trends["s1"].drops), (7, 2))
        self.assertEqual((trends["KC"].adds, trends["KC"].drops), (None, 1))
        self.assertEqual(len(snapshot.id_crosswalk), 1)
        self.assertEqual(
            snapshot.id_crosswalk[0].key, ("00-0030001", "101", "s1")
        )
        record = snapshot.to_record()
        json.dumps(record, allow_nan=False)
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(PublicPlayerDataSnapshot.from_record(record), snapshot)
        self.assertEqual(
            PublicPlayerDataSnapshot.from_json(json.dumps(record)), snapshot
        )
        self.assertRegex(snapshot.data_id, r"^public_player_data_[0-9a-f]{64}$")
        self.assertEqual(
            snapshot,
            collect_public_player_data(
                2026, clock=lambda: NOW, fetcher=_RecordingFetcher(_responses())
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            snapshot.season = 2025

        unknown = copy.deepcopy(record)
        unknown["current_stats"]["extra"] = True
        with self.assertRaisesRegex(ValueError, "missing or unknown"):
            PublicPlayerDataSnapshot.from_record(unknown)
        with self.assertRaisesRegex(ValueError, "invalid or non-unique"):
            PublicPlayerDataSnapshot.from_json(
                '{"kind":"public_player_data","kind":"public_player_data"}'
            )

    def test_404_semantics_distinguish_preseason_from_required_sources(self):
        sources = public_player_source_urls(2026)
        responses = {source.url: _not_published() for source in sources}

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertIs(snapshot.current_stats.availability, DataAvailability.NOT_PUBLISHED)
        self.assertIs(snapshot.previous_stats.availability, DataAvailability.UNAVAILABLE)
        self.assertTrue(all(not season.rows for season in snapshot.injury_history))
        self.assertEqual(
            [season.availability for season in snapshot.injury_history],
            [
                DataAvailability.NOT_PUBLISHED,
                DataAvailability.UNAVAILABLE,
                DataAvailability.UNAVAILABLE,
            ],
        )
        self.assertEqual(snapshot.sleeper_players, ())
        self.assertEqual(snapshot.trends, ())
        self.assertEqual(snapshot.id_crosswalk, ())
        self.assertTrue(all(row.byte_count == 0 for row in snapshot.provenance))
        provenance = {row.dataset: row.availability for row in snapshot.provenance}
        for dataset in (
            "sleeper_active_players",
            "sleeper_trending_adds",
            "sleeper_trending_drops",
            "dynastyprocess_player_ids",
        ):
            with self.subTest(dataset=dataset):
                self.assertIs(provenance[dataset], DataAvailability.UNAVAILABLE)

    def test_current_nflverse_404_after_preseason_is_unavailable(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[0].url] = _not_published()
        responses[sources[2].url] = _not_published()

        snapshot = collect_public_player_data(
            2026,
            as_of_week=2,
            clock=lambda: NOW,
            fetcher=_RecordingFetcher(responses),
        )

        self.assertIs(
            snapshot.current_stats.availability, DataAvailability.UNAVAILABLE
        )
        self.assertIs(
            snapshot.injury_history[0].availability,
            DataAvailability.UNAVAILABLE,
        )

    def test_implausibly_empty_evergreen_catalogs_are_unavailable(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[5].url] = _observed(b"{}")
        responses[sources[6].url] = _observed(b"[]")
        responses[sources[7].url] = _observed(b"[]")
        responses[sources[8].url] = _observed(
            _csv_bytes(_CROSSWALK_FIELDS, ())
        )

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        provenance = {row.dataset: row.availability for row in snapshot.provenance}
        self.assertIs(
            provenance["sleeper_active_players"], DataAvailability.UNAVAILABLE
        )
        self.assertIs(
            provenance["dynastyprocess_player_ids"], DataAvailability.UNAVAILABLE
        )
        self.assertIs(
            provenance["sleeper_trending_adds"], DataAvailability.OBSERVED
        )
        self.assertIs(
            provenance["sleeper_trending_drops"], DataAvailability.OBSERVED
        )

    def test_source_schema_and_gzip_failures_degrade_independently(self):
        responses = _responses()
        first = public_player_source_urls(2026)[0].url
        responses[first] = _observed(_stats_file(2026, wrong_season=True))
        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )
        self.assertIs(snapshot.current_stats.availability, DataAvailability.UNAVAILABLE)
        self.assertIs(snapshot.previous_stats.availability, DataAvailability.OBSERVED)
        self.assertTrue(snapshot.sleeper_players)

        responses[first] = _observed(b"not gzip")
        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )
        self.assertIs(snapshot.current_stats.availability, DataAvailability.UNAVAILABLE)

        responses[first] = _observed(_stats_file(2026))
        snapshot = collect_public_player_data(
            2026,
            limits=PublicPlayerDataLimits(max_stats_decoded_bytes=100),
            clock=lambda: NOW,
            fetcher=_RecordingFetcher(responses),
        )
        self.assertIs(snapshot.current_stats.availability, DataAvailability.UNAVAILABLE)

    def test_drop_network_failure_keeps_observed_add_counts_as_partial(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[7].url] = PublicPlayerDataError("offline")

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertTrue(snapshot.current_stats.rows)
        self.assertTrue(snapshot.sleeper_players)
        self.assertTrue(snapshot.id_crosswalk)
        trends = {row.sleeper_player_id: row for row in snapshot.trends}
        self.assertEqual((trends["s1"].adds, trends["s1"].drops), (7, None))
        drop_source = next(
            row
            for row in snapshot.provenance
            if row.dataset == "sleeper_trending_drops"
        )
        self.assertIs(drop_source.availability, DataAvailability.UNAVAILABLE)
        self.assertEqual(drop_source.byte_count, 0)

    def test_add_network_failure_keeps_observed_drop_counts_as_partial(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[6].url] = PublicPlayerDataError("offline")

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        trends = {row.sleeper_player_id: row for row in snapshot.trends}
        self.assertEqual((trends["s1"].adds, trends["s1"].drops), (None, 2))
        self.assertEqual((trends["KC"].adds, trends["KC"].drops), (None, 1))
        add_source = next(
            row
            for row in snapshot.provenance
            if row.dataset == "sleeper_trending_adds"
        )
        self.assertIs(add_source.availability, DataAvailability.UNAVAILABLE)

    def test_crosswalk_schema_failure_is_explicit_without_losing_other_profiles(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[8].url] = _observed(b"wrong_header\nvalue\n")

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertEqual(snapshot.id_crosswalk, ())
        self.assertTrue(snapshot.sleeper_players)
        source = next(
            row for row in snapshot.provenance
            if row.dataset == "dynastyprocess_player_ids"
        )
        self.assertIs(source.availability, DataAvailability.UNAVAILABLE)

    def test_deep_sleeper_json_degrades_without_losing_other_sources(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        responses[sources[5].url] = _observed(
            b'{' + b'"nested":' + (b'[' * 100_000) + b'0'
            + (b']' * 100_000) + b'}'
        )

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertTrue(snapshot.current_stats.rows)
        self.assertEqual(snapshot.sleeper_players, ())
        source = next(
            row for row in snapshot.provenance
            if row.dataset == "sleeper_active_players"
        )
        self.assertIs(source.availability, DataAvailability.UNAVAILABLE)

    def test_malformed_crosswalk_csv_degrades_without_losing_other_sources(self):
        sources = public_player_source_urls(2026)
        responses = _responses()
        malformed = b'gsis_id,espn_id,sleeper_id\n"g1","101","s1'
        responses[sources[8].url] = _observed(malformed)

        snapshot = collect_public_player_data(
            2026, clock=lambda: NOW, fetcher=_RecordingFetcher(responses)
        )

        self.assertTrue(snapshot.current_stats.rows)
        self.assertTrue(snapshot.sleeper_players)
        self.assertEqual(snapshot.id_crosswalk, ())
        source = next(
            row for row in snapshot.provenance
            if row.dataset == "dynastyprocess_player_ids"
        )
        self.assertIs(source.availability, DataAvailability.UNAVAILABLE)

    def test_cancellation_stops_before_a_fetch(self):
        fetcher = _RecordingFetcher(_responses())
        with self.assertRaises(PublicPlayerDataCancelled):
            collect_public_player_data(
                2026, cancelled=lambda: True, clock=lambda: NOW, fetcher=fetcher
            )
        self.assertEqual(fetcher.calls, [])

    def test_limits_reject_unsafe_configuration(self):
        with self.assertRaisesRegex(ValueError, "max_injury_download_bytes"):
            PublicPlayerDataLimits(max_injury_download_bytes=0)
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            PublicPlayerDataLimits(timeout_seconds=121)
        with self.assertRaisesRegex(ValueError, "max_crosswalk_rows"):
            PublicPlayerDataLimits(max_crosswalk_rows=0)


class _StreamResponse:
    def __init__(
        self,
        body,
        *,
        final_url="https://api.sleeper.app/v1/players/nfl",
        status=200,
        headers=None,
        on_read=lambda: None,
        max_read=None,
    ):
        self._body = bytearray(body)
        self._final_url = final_url
        self._status = status
        self._on_read = on_read
        self._max_read = max_read
        self.headers = headers or {}
        self.closed = False
        self.socket_timeouts = []
        socket = SimpleNamespace(settimeout=self.socket_timeouts.append)
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))

    def geturl(self):
        return self._final_url

    def getcode(self):
        return self._status

    def read(self, amount):
        self._on_read()
        if self._max_read is not None:
            amount = min(amount, self._max_read)
        result = bytes(self._body[:amount])
        del self._body[:amount]
        return result

    def read1(self, amount):
        return self.read(amount)

    def close(self):
        self.closed = True


class PublicPlayerHttpTests(unittest.TestCase):
    def test_transport_opener_disables_automatic_redirects(self):
        response = _StreamResponse(b"payload")
        opener = SimpleNamespace(open=lambda request, *, timeout: response)
        with patch(
            "trade_snapshot._public_player_http.build_opener", return_value=opener
        ) as factory:
            result = bounded_https_get(
                "https://api.sleeper.app/v1/players/nfl",
                timeout_seconds=1,
                max_bytes=100,
                cancelled=lambda: False,
            )

        redirect_handler = factory.call_args.args[0]
        self.assertIsNone(
            redirect_handler.redirect_request(
                None, None, 302, "Found", {}, "https://example.test/data"
            )
        )
        self.assertEqual(result.body, b"payload")

    def test_transport_enforces_streaming_size_limit_and_closes(self):
        response = _StreamResponse(b"12345")
        with patch(
            "trade_snapshot._public_player_http._open_without_redirects",
            return_value=response,
            create=True,
        ):
            with self.assertRaisesRegex(PublicPlayerDataError, "size limit"):
                bounded_https_get(
                    "https://api.sleeper.app/v1/players/nfl",
                    timeout_seconds=1,
                    max_bytes=4,
                    cancelled=lambda: False,
                )
        self.assertTrue(response.closed)

    def test_transport_rejects_a_short_body_with_declared_content_length(self):
        response = _StreamResponse(
            b"abc", headers={"Content-Length": "10"}
        )
        with patch(
            "trade_snapshot._public_player_http._open_without_redirects",
            return_value=response,
            create=True,
        ):
            with self.assertRaisesRegex(PublicPlayerDataError, "Content-Length"):
                bounded_https_get(
                    "https://api.sleeper.app/v1/players/nfl",
                    timeout_seconds=1,
                    max_bytes=100,
                    cancelled=lambda: False,
                )
        self.assertTrue(response.closed)

    def test_transport_checks_cancellation_before_network(self):
        with patch(
            "trade_snapshot._public_player_http._open_without_redirects", create=True
        ) as opener:
            with self.assertRaises(PublicPlayerDataCancelled):
                bounded_https_get(
                    "https://api.sleeper.app/v1/players/nfl",
                    timeout_seconds=1,
                    max_bytes=100,
                    cancelled=lambda: True,
                )
        opener.assert_not_called()

    def test_transport_never_contacts_a_disallowed_redirect_destination(self):
        source = "https://github.com/nflverse/data"
        response = _StreamResponse(
            b"",
            final_url=source,
            status=302,
            headers={"Location": "https://example.test/stolen"},
        )
        contacted = []

        def open_request(request, *, timeout):
            contacted.append(request.full_url)
            if len(contacted) > 1:
                self.fail("a disallowed redirect destination was contacted")
            return response

        with patch(
            "trade_snapshot._public_player_http._open_without_redirects",
            side_effect=open_request,
        ):
            with self.assertRaisesRegex(PublicPlayerDataError, "unsupported host"):
                bounded_https_get(
                    source,
                    timeout_seconds=1,
                    max_bytes=100,
                    cancelled=lambda: False,
                )

        self.assertEqual(contacted, [source])
        self.assertTrue(response.closed)

    def test_transport_follows_only_a_bounded_allowlisted_redirect_chain(self):
        source = "https://github.com/nflverse/data"
        release = "https://release-assets.githubusercontent.com/download"
        responses = [
            redirect_response := _StreamResponse(
                b"",
                final_url=source,
                status=302,
                headers={"Location": release},
            ),
            payload_response := _StreamResponse(
                b"payload",
                final_url=release,
                status=200,
                headers={"Content-Type": "application/octet-stream"},
            ),
        ]
        contacted = []

        def open_request(request, *, timeout):
            contacted.append(request.full_url)
            return responses.pop(0)

        with patch(
            "trade_snapshot._public_player_http._open_without_redirects",
            side_effect=open_request,
            create=True,
        ):
            result = bounded_https_get(
                source,
                timeout_seconds=1,
                max_bytes=100,
                cancelled=lambda: False,
            )

        self.assertEqual(result.body, b"payload")
        self.assertEqual(contacted, [source, release])
        self.assertTrue(redirect_response.closed)
        self.assertTrue(payload_response.closed)

    def test_transport_stops_after_three_redirects(self):
        source = "https://github.com/nflverse/data"
        responses = [
            _StreamResponse(
                b"",
                final_url=f"https://github.com/nflverse/hop-{index}",
                status=302,
                headers={"Location": f"/nflverse/hop-{index + 1}"},
            )
            for index in range(4)
        ]
        contacted = []

        def open_request(request, *, timeout):
            contacted.append(request.full_url)
            return responses[len(contacted) - 1]

        with patch(
            "trade_snapshot._public_player_http._open_without_redirects",
            side_effect=open_request,
            create=True,
        ):
            with self.assertRaisesRegex(PublicPlayerDataError, "too many redirects"):
                bounded_https_get(
                    source,
                    timeout_seconds=1,
                    max_bytes=100,
                    cancelled=lambda: False,
                )

        self.assertEqual(len(contacted), 4)
        self.assertTrue(all(response.closed for response in responses))

    def test_transport_rejects_a_non_source_host_before_network(self):
        with patch(
            "trade_snapshot._public_player_http._open_without_redirects", create=True
        ) as opener:
            with self.assertRaisesRegex(ValueError, "allowlisted"):
                bounded_https_get(
                    "https://example.test/data",
                    timeout_seconds=1,
                    max_bytes=100,
                    cancelled=lambda: False,
                )
        opener.assert_not_called()

    def test_transport_uses_one_total_deadline_for_slow_drip_reads(self):
        now = [100.0]

        def advance():
            now[0] += 0.4

        response = _StreamResponse(b"12345", on_read=advance, max_read=1)
        with (
            patch(
                "trade_snapshot._public_player_http._open_without_redirects",
                return_value=response,
                create=True,
            ),
            patch("trade_snapshot._public_player_http.monotonic", side_effect=lambda: now[0]),
        ):
            with self.assertRaisesRegex(PublicPlayerDataError, "timed out"):
                bounded_https_get(
                    "https://api.sleeper.app/v1/players/nfl",
                    timeout_seconds=1,
                    max_bytes=100,
                    cancelled=lambda: False,
                )

        self.assertGreaterEqual(len(response.socket_timeouts), 2)
        self.assertGreater(response.socket_timeouts[0], response.socket_timeouts[-1])
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
