"""Public, bulk-only NFL player metadata, stats, trends, and injury reports.

This boundary deliberately has no dependency on the trade engine.  One weekly
collection downloads nine bounded datasets, validates them, and returns
immutable evidence that a profile materializer can join later.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
import re
from urllib.parse import urlsplit

from ._public_player_http import (
    DownloadedPublicData,
    PublicPlayerDataCancelled,
    PublicPlayerDataError,
    bounded_https_get,
)


_SCHEMA_VERSION = 2
_NFLVERSE_STATS = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv.gz"
)
_NFLVERSE_INJURIES = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "injuries/injuries_{season}.csv.gz"
)
_SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl?active=true"
_SLEEPER_ADDS = (
    "https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=168&limit=100"
)
_SLEEPER_DROPS = (
    "https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours=168&limit=100"
)
_DYNASTYPROCESS_PLAYER_IDS = (
    "https://raw.githubusercontent.com/dynastyprocess/data/master/"
    "files/db_playerids.csv"
)
_DATASETS = (
    "nflverse_player_stats_current",
    "nflverse_player_stats_previous",
    "nflverse_injuries_current",
    "nflverse_injuries_previous",
    "nflverse_injuries_two_seasons_prior",
    "sleeper_active_players",
    "sleeper_trending_adds",
    "sleeper_trending_drops",
    "dynastyprocess_player_ids",
)
_DATASET_PROVIDERS = {
    dataset: (
        "nflverse"
        if dataset.startswith("nflverse_")
        else "sleeper"
        if dataset.startswith("sleeper_")
        else "dynastyprocess"
    )
    for dataset in _DATASETS
}
_IDENTITY_FIELDS = frozenset(
    {
        "player_id", "player_display_name", "position", "headshot_url", "season",
        "week", "season_type", "game_id", "team", "opponent_team",
        "fantasy_points", "fantasy_points_ppr",
    }
)
_STAT_FIELDS = (
    "completions", "attempts", "passing_yards", "passing_tds",
    "passing_interceptions", "passing_air_yards", "passing_yards_after_catch",
    "passing_first_downs", "carries", "rushing_yards", "rushing_tds",
    "rushing_first_downs", "receptions", "targets", "receiving_yards",
    "receiving_tds", "receiving_air_yards", "receiving_yards_after_catch",
    "receiving_first_downs", "target_share", "air_yards_share", "wopr",
    "fumbles_total", "fumbles_lost_total", "special_teams_tds",
    "def_tackles_solo", "def_tackle_assists", "def_tackles_for_loss",
    "def_fumbles_forced", "def_sacks", "def_interceptions",
    "def_pass_defended", "def_tds", "def_safeties", "fg_made", "fg_att",
    "fg_long", "pat_made", "pat_att", "punt_returns", "punt_return_yards",
    "kickoff_returns", "kickoff_return_yards",
)
_REQUIRED_STATS_HEADERS = _IDENTITY_FIELDS | frozenset(_STAT_FIELDS)
_TEAM_ALIASES = {"JAC": "JAX", "LA": "LAR", "WAS": "WSH"}
_NFL_TEAMS = frozenset(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL",
        "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR",
        "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT",
        "SEA", "SF", "TB", "TEN", "WSH",
    }
)
_REPORT_STATUS_VALUES = frozenset(
    {"out", "doubtful", "questionable", "probable", "note"}
)
_PRACTICE_STATUS_VALUES = frozenset(
    {"did_not_participate", "limited", "full", "note"}
)


class DataAvailability(str, Enum):
    OBSERVED = "observed"
    NOT_PUBLISHED = "not_published"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PublicPlayerSource:
    provider: str
    dataset: str
    url: str

    def __post_init__(self) -> None:
        if self.provider not in {"nflverse", "sleeper", "dynastyprocess"}:
            raise ValueError("public player-data provider is unsupported")
        if self.dataset not in _DATASETS:
            raise ValueError("public player-data dataset is unsupported")
        if _DATASET_PROVIDERS[self.dataset] != self.provider:
            raise ValueError("public player-data dataset/provider combination is invalid")
        _public_url(self.url)

    def to_record(self) -> dict[str, str]:
        return {"provider": self.provider, "dataset": self.dataset, "url": self.url}


def public_player_source_urls(season: int) -> tuple[PublicPlayerSource, ...]:
    """Return the exact bounded source catalog used for a weekly collection."""

    _season(season)
    if season < 2001:
        raise ValueError("season must allow a two-season history")
    sources = (
        PublicPlayerSource(
            "nflverse", _DATASETS[0], _NFLVERSE_STATS.format(season=season)
        ),
        PublicPlayerSource(
            "nflverse", _DATASETS[1], _NFLVERSE_STATS.format(season=season - 1)
        ),
        PublicPlayerSource(
            "nflverse", _DATASETS[2], _NFLVERSE_INJURIES.format(season=season)
        ),
        PublicPlayerSource(
            "nflverse", _DATASETS[3], _NFLVERSE_INJURIES.format(season=season - 1)
        ),
        PublicPlayerSource(
            "nflverse", _DATASETS[4], _NFLVERSE_INJURIES.format(season=season - 2)
        ),
        PublicPlayerSource("sleeper", _DATASETS[5], _SLEEPER_PLAYERS),
        PublicPlayerSource("sleeper", _DATASETS[6], _SLEEPER_ADDS),
        PublicPlayerSource("sleeper", _DATASETS[7], _SLEEPER_DROPS),
        PublicPlayerSource(
            "dynastyprocess", _DATASETS[8], _DYNASTYPROCESS_PLAYER_IDS
        ),
    )
    if len({source.url for source in sources}) != len(sources):
        raise AssertionError("public player-data source catalog repeated a URL")
    return sources


@dataclass(frozen=True, slots=True)
class PublicPlayerDataLimits:
    timeout_seconds: float = 30.0
    max_stats_download_bytes: int = 16 * 1024 * 1024
    max_stats_decoded_bytes: int = 64 * 1024 * 1024
    max_injury_download_bytes: int = 4 * 1024 * 1024
    max_injury_decoded_bytes: int = 32 * 1024 * 1024
    max_sleeper_players_bytes: int = 16 * 1024 * 1024
    max_sleeper_trends_bytes: int = 1024 * 1024
    max_crosswalk_download_bytes: int = 8 * 1024 * 1024
    max_stat_rows_per_season: int = 100_000
    max_injury_rows_per_season: int = 50_000
    max_sleeper_players: int = 20_000
    max_trend_rows: int = 1_000
    max_crosswalk_rows: int = 50_000

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= 120
        ):
            raise ValueError("timeout_seconds must be greater than zero and at most 120")
        for name, maximum in (
            ("max_stats_download_bytes", 256 * 1024 * 1024),
            ("max_stats_decoded_bytes", 512 * 1024 * 1024),
            ("max_injury_download_bytes", 64 * 1024 * 1024),
            ("max_injury_decoded_bytes", 256 * 1024 * 1024),
            ("max_sleeper_players_bytes", 256 * 1024 * 1024),
            ("max_sleeper_trends_bytes", 64 * 1024 * 1024),
            ("max_crosswalk_download_bytes", 64 * 1024 * 1024),
            ("max_stat_rows_per_season", 1_000_000),
            ("max_injury_rows_per_season", 500_000),
            ("max_sleeper_players", 100_000),
            ("max_trend_rows", 100_000),
            ("max_crosswalk_rows", 250_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside its supported bound")


@dataclass(frozen=True, slots=True)
class PublicDataProvenance:
    provider: str
    dataset: str
    requested_url: str
    availability: DataAvailability
    captured_at: datetime
    source_updated_at: datetime | None
    etag: str | None
    content_sha256: str | None
    byte_count: int

    def __post_init__(self) -> None:
        if self.provider not in {"nflverse", "sleeper", "dynastyprocess"}:
            raise ValueError("public player-data provider is unsupported")
        if self.dataset not in _DATASETS:
            raise ValueError("public player-data dataset is unsupported")
        if _DATASET_PROVIDERS[self.dataset] != self.provider:
            raise ValueError("public player-data dataset/provider combination is invalid")
        _public_url(self.requested_url)
        if not isinstance(self.availability, DataAvailability):
            raise ValueError("availability must be a DataAvailability")
        _aware_time("captured_at", self.captured_at)
        if self.source_updated_at is not None:
            _aware_time("source_updated_at", self.source_updated_at)
        if self.etag is not None and (
            not isinstance(self.etag, str) or not self.etag or len(self.etag) > 512
        ):
            raise ValueError("etag must be bounded text or None")
        if self.content_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.content_sha256
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 or None")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("byte_count must be a non-negative integer")
        if self.availability is DataAvailability.OBSERVED:
            if self.content_sha256 is None or self.byte_count == 0:
                raise ValueError("observed source provenance requires captured bytes")
        elif self.content_sha256 is not None or self.byte_count:
            raise ValueError("unpublished source provenance cannot claim captured bytes")

    def to_record(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "requested_url": self.requested_url,
            "availability": self.availability.value,
            "captured_at": _iso(self.captured_at),
            "source_updated_at": (
                None if self.source_updated_at is None else _iso(self.source_updated_at)
            ),
            "etag": self.etag,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class PlayerWeekStats:
    """One public weekly stat row; omitted raw stat fields mean zero."""

    gsis_id: str
    display_name: str
    position: str
    season: int
    week: int
    game_id: str
    nfl_team_id: str
    opponent_team_id: str
    headshot_url: str | None
    fantasy_points_standard: float | None
    fantasy_points_ppr: float | None
    stat_values: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        _bounded_text("gsis_id", self.gsis_id, 64)
        _bounded_text("display_name", self.display_name, 160)
        _position(self.position)
        _season(self.season)
        _integer("week", self.week, 1, 25)
        _bounded_text("game_id", self.game_id, 64)
        _team(self.nfl_team_id)
        _team(self.opponent_team_id)
        if self.headshot_url is not None:
            _headshot_url(self.headshot_url)
        for name in ("fantasy_points_standard", "fantasy_points_ppr"):
            value = getattr(self, name)
            if value is not None:
                _finite(name, value)
        try:
            values = tuple(self.stat_values)
        except TypeError:
            raise ValueError("stat_values must be iterable") from None
        normalized = []
        for item in values:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("stat_values entries must be name/value pairs")
            name, value = item
            if name not in _STAT_FIELDS:
                raise ValueError("stat_values contains an unsupported field")
            normalized.append((name, _finite(f"stat_values[{name}]", value)))
        if len({name for name, _ in normalized}) != len(normalized):
            raise ValueError("stat_values contains duplicate fields")
        object.__setattr__(self, "stat_values", tuple(sorted(normalized)))

    def to_record(self) -> dict[str, object]:
        return {
            "gsis_id": self.gsis_id,
            "display_name": self.display_name,
            "position": self.position,
            "season": self.season,
            "week": self.week,
            "game_id": self.game_id,
            "nfl_team_id": self.nfl_team_id,
            "opponent_team_id": self.opponent_team_id,
            "headshot_url": self.headshot_url,
            "fantasy_points_standard": self.fantasy_points_standard,
            "fantasy_points_ppr": self.fantasy_points_ppr,
            "stat_values": dict(self.stat_values),
        }


@dataclass(frozen=True, slots=True)
class SeasonPlayerStats:
    season: int
    availability: DataAvailability
    rows: tuple[PlayerWeekStats, ...]

    def __post_init__(self) -> None:
        _season(self.season)
        if not isinstance(self.availability, DataAvailability):
            raise ValueError("availability must be a DataAvailability")
        rows = _typed_tuple("rows", self.rows, PlayerWeekStats)
        if any(row.season != self.season for row in rows):
            raise ValueError("season stats contain a row from another season")
        keys = [(row.gsis_id, row.week, row.game_id) for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError("season stats contain duplicate player-game rows")
        if self.availability is not DataAvailability.OBSERVED and rows:
            raise ValueError("unobserved season stats cannot contain rows")
        object.__setattr__(self, "rows", tuple(sorted(
            rows, key=lambda row: (row.week, row.gsis_id, row.game_id)
        )))

    def to_record(self) -> dict[str, object]:
        return {
            "season": self.season,
            "availability": self.availability.value,
            "rows": [row.to_record() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class PlayerInjuryReport:
    """One documented NFL injury/practice report, not a medical diagnosis."""

    gsis_id: str
    display_name: str
    position: str | None
    season: int
    week: int
    nfl_team_id: str
    report_primary_injury: str | None
    report_secondary_injury: str | None
    report_status: str | None
    practice_primary_injury: str | None
    practice_secondary_injury: str | None
    practice_status: str | None
    source_modified_at: datetime | None

    def __post_init__(self) -> None:
        _bounded_text("gsis_id", self.gsis_id, 64)
        _bounded_text("display_name", self.display_name, 160)
        if self.position is not None:
            _position(self.position)
        _season(self.season)
        _integer("week", self.week, 1, 25)
        _team(self.nfl_team_id)
        for name in (
            "report_primary_injury",
            "report_secondary_injury",
            "practice_primary_injury",
            "practice_secondary_injury",
        ):
            value = getattr(self, name)
            if value is not None:
                _bounded_text(name, value, 256)
        if (
            self.report_status is not None
            and self.report_status not in _REPORT_STATUS_VALUES
        ):
            raise ValueError("report_status is unsupported")
        if (
            self.practice_status is not None
            and self.practice_status not in _PRACTICE_STATUS_VALUES
        ):
            raise ValueError("practice_status is unsupported")
        if self.source_modified_at is not None:
            _aware_time("source_modified_at", self.source_modified_at)

    def to_record(self) -> dict[str, object]:
        return {
            "gsis_id": self.gsis_id,
            "display_name": self.display_name,
            "position": self.position,
            "season": self.season,
            "week": self.week,
            "nfl_team_id": self.nfl_team_id,
            "report_primary_injury": self.report_primary_injury,
            "report_secondary_injury": self.report_secondary_injury,
            "report_status": self.report_status,
            "practice_primary_injury": self.practice_primary_injury,
            "practice_secondary_injury": self.practice_secondary_injury,
            "practice_status": self.practice_status,
            "source_modified_at": (
                None
                if self.source_modified_at is None
                else _iso(self.source_modified_at)
            ),
        }


@dataclass(frozen=True, slots=True)
class SeasonInjuryReports:
    season: int
    availability: DataAvailability
    rows: tuple[PlayerInjuryReport, ...]

    def __post_init__(self) -> None:
        _season(self.season)
        if not isinstance(self.availability, DataAvailability):
            raise ValueError("availability must be a DataAvailability")
        rows = _typed_tuple("rows", self.rows, PlayerInjuryReport)
        if any(row.season != self.season for row in rows):
            raise ValueError("injury reports contain a row from another season")
        keys = [(row.gsis_id, row.week) for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError("injury reports contain duplicate player/week rows")
        if self.availability is not DataAvailability.OBSERVED and rows:
            raise ValueError("unobserved injury reports cannot contain rows")
        object.__setattr__(
            self,
            "rows",
            tuple(sorted(rows, key=lambda row: (row.week, row.gsis_id))),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "season": self.season,
            "availability": self.availability.value,
            "rows": [row.to_record() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class SleeperPlayerMetadata:
    sleeper_player_id: str
    gsis_id: str | None
    espn_id: str | None
    display_name: str
    position: str | None
    fantasy_positions: tuple[str, ...]
    nfl_team_id: str | None
    active: bool
    status: str | None
    injury_status: str | None
    injury_body_part: str | None
    practice_participation: str | None
    depth_chart_position: str | None
    depth_chart_order: int | None
    years_experience: int | None
    jersey_number: int | None
    news_updated_ms: int | None

    def __post_init__(self) -> None:
        _bounded_text("sleeper_player_id", self.sleeper_player_id, 64)
        for name in ("gsis_id", "espn_id"):
            value = getattr(self, name)
            if value is not None:
                _bounded_text(name, value, 64)
        _bounded_text("display_name", self.display_name, 160)
        if self.position is not None:
            _position(self.position)
        positions = tuple(self.fantasy_positions)
        if len(set(positions)) != len(positions) or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Z/]{1,8}", value)
            for value in positions
        ):
            raise ValueError("fantasy_positions contains invalid values")
        if self.nfl_team_id is not None:
            _team(self.nfl_team_id)
        if type(self.active) is not bool:
            raise ValueError("active must be a boolean")
        for name in (
            "status", "injury_status", "injury_body_part", "practice_participation",
            "depth_chart_position",
        ):
            value = getattr(self, name)
            if value is not None:
                _bounded_text(name, value, 256)
        for name, maximum in (
            ("depth_chart_order", 100), ("years_experience", 100),
            ("jersey_number", 999), ("news_updated_ms", 10**16),
        ):
            value = getattr(self, name)
            if value is not None:
                _integer(name, value, 0, maximum)
        object.__setattr__(self, "fantasy_positions", positions)

    def to_record(self) -> dict[str, object]:
        return {
            "sleeper_player_id": self.sleeper_player_id,
            "gsis_id": self.gsis_id,
            "espn_id": self.espn_id,
            "display_name": self.display_name,
            "position": self.position,
            "fantasy_positions": list(self.fantasy_positions),
            "nfl_team_id": self.nfl_team_id,
            "active": self.active,
            "status": self.status,
            "injury_status": self.injury_status,
            "injury_body_part": self.injury_body_part,
            "practice_participation": self.practice_participation,
            "depth_chart_position": self.depth_chart_position,
            "depth_chart_order": self.depth_chart_order,
            "years_experience": self.years_experience,
            "jersey_number": self.jersey_number,
            "news_updated_ms": self.news_updated_ms,
        }


@dataclass(frozen=True, slots=True)
class SleeperPlayerTrend:
    sleeper_player_id: str
    adds: int | None
    drops: int | None

    def __post_init__(self) -> None:
        _bounded_text("sleeper_player_id", self.sleeper_player_id, 64)
        for name in ("adds", "drops"):
            value = getattr(self, name)
            if value is not None:
                _integer(name, value, 0, 10**12)
        if self.adds is None and self.drops is None:
            raise ValueError("Sleeper trend requires at least one observed side")

    def to_record(self) -> dict[str, object]:
        return {
            "sleeper_player_id": self.sleeper_player_id,
            "adds": self.adds,
            "drops": self.drops,
        }


@dataclass(frozen=True, slots=True)
class PublicPlayerIdCrosswalk:
    """One source-published exact-ID link; names never participate in matching."""

    gsis_id: str | None
    espn_id: str | None
    sleeper_id: str | None

    def __post_init__(self) -> None:
        for name in ("gsis_id", "espn_id", "sleeper_id"):
            value = getattr(self, name)
            if value is not None:
                _bounded_text(name, value, 64)
        if sum(value is not None for value in self.key) < 2:
            raise ValueError("player ID crosswalk requires at least two provider IDs")

    @property
    def key(self) -> tuple[str | None, str | None, str | None]:
        return self.gsis_id, self.espn_id, self.sleeper_id

    def to_record(self) -> dict[str, str | None]:
        return {
            "gsis_id": self.gsis_id,
            "espn_id": self.espn_id,
            "sleeper_id": self.sleeper_id,
        }


@dataclass(frozen=True, slots=True)
class PublicPlayerDataSnapshot:
    season: int
    captured_at: datetime
    current_stats: SeasonPlayerStats
    previous_stats: SeasonPlayerStats
    injury_history: tuple[SeasonInjuryReports, ...]
    sleeper_players: tuple[SleeperPlayerMetadata, ...]
    trends: tuple[SleeperPlayerTrend, ...]
    id_crosswalk: tuple[PublicPlayerIdCrosswalk, ...]
    provenance: tuple[PublicDataProvenance, ...]
    data_id: str = field(init=False)

    def __post_init__(self) -> None:
        _season(self.season)
        _aware_time("captured_at", self.captured_at)
        if (
            not isinstance(self.current_stats, SeasonPlayerStats)
            or self.current_stats.season != self.season
            or not isinstance(self.previous_stats, SeasonPlayerStats)
            or self.previous_stats.season != self.season - 1
        ):
            raise ValueError("player-data seasons do not match the requested season")
        injury_history = _typed_tuple(
            "injury_history", self.injury_history, SeasonInjuryReports
        )
        if tuple(row.season for row in injury_history) != (
            self.season,
            self.season - 1,
            self.season - 2,
        ):
            raise ValueError("injury history must cover current and two prior seasons")
        players = _typed_tuple("sleeper_players", self.sleeper_players, SleeperPlayerMetadata)
        trends = _typed_tuple("trends", self.trends, SleeperPlayerTrend)
        crosswalk = _typed_tuple(
            "id_crosswalk", self.id_crosswalk, PublicPlayerIdCrosswalk
        )
        provenance = _typed_tuple("provenance", self.provenance, PublicDataProvenance)
        _unique("Sleeper player", (row.sleeper_player_id for row in players))
        _unique("Sleeper trend", (row.sleeper_player_id for row in trends))
        _unique("player crosswalk", (row.key for row in crosswalk))
        if {row.dataset for row in provenance} != set(_DATASETS) or len(provenance) != len(_DATASETS):
            raise ValueError("player-data snapshot requires one provenance row per dataset")
        expected_sources = public_player_source_urls(self.season)
        provenance_by_dataset = {row.dataset: row for row in provenance}
        if any(
            (
                provenance_by_dataset[source.dataset].provider,
                provenance_by_dataset[source.dataset].dataset,
                provenance_by_dataset[source.dataset].requested_url,
            )
            != (source.provider, source.dataset, source.url)
            for source in expected_sources
        ):
            raise ValueError("player-data provenance does not match the source catalog")
        if any(row.captured_at != self.captured_at for row in provenance):
            raise ValueError("player-data provenance capture times do not match")
        current = next(row for row in provenance if row.dataset == _DATASETS[0])
        previous = next(row for row in provenance if row.dataset == _DATASETS[1])
        if current.availability is not self.current_stats.availability:
            raise ValueError("current stats availability conflicts with provenance")
        if previous.availability is not self.previous_stats.availability:
            raise ValueError("previous stats availability conflicts with provenance")
        for index, injury_season in enumerate(injury_history, start=2):
            source = next(row for row in provenance if row.dataset == _DATASETS[index])
            if source.availability is not injury_season.availability:
                raise ValueError("injury availability conflicts with provenance")
        sleeper_source = provenance_by_dataset[_DATASETS[5]]
        add_source = provenance_by_dataset[_DATASETS[6]]
        drop_source = provenance_by_dataset[_DATASETS[7]]
        crosswalk_source = provenance_by_dataset[_DATASETS[8]]
        if sleeper_source.availability is not DataAvailability.OBSERVED and players:
            raise ValueError("unobserved Sleeper metadata cannot contain players")
        if add_source.availability is not DataAvailability.OBSERVED and any(
            row.adds is not None for row in trends
        ):
            raise ValueError("unobserved Sleeper add evidence cannot contain counts")
        if drop_source.availability is not DataAvailability.OBSERVED and any(
            row.drops is not None for row in trends
        ):
            raise ValueError("unobserved Sleeper drop evidence cannot contain counts")
        if crosswalk_source.availability is not DataAvailability.OBSERVED and crosswalk:
            raise ValueError("unobserved player-ID crosswalk cannot contain rows")
        object.__setattr__(self, "injury_history", injury_history)
        object.__setattr__(self, "sleeper_players", tuple(sorted(
            players, key=lambda row: row.sleeper_player_id
        )))
        object.__setattr__(self, "trends", tuple(sorted(
            trends, key=lambda row: row.sleeper_player_id
        )))
        object.__setattr__(self, "id_crosswalk", tuple(sorted(
            crosswalk,
            key=lambda row: tuple(value or "" for value in row.key),
        )))
        object.__setattr__(self, "provenance", tuple(sorted(
            provenance, key=lambda row: _DATASETS.index(row.dataset)
        )))
        digest = sha256(_canonical_json(self._content_record())).hexdigest()
        object.__setattr__(self, "data_id", f"public_player_data_{digest}")

    def _content_record(self) -> dict[str, object]:
        return {
            "season": self.season,
            "captured_at": _iso(self.captured_at),
            "current_stats": self.current_stats.to_record(),
            "previous_stats": self.previous_stats.to_record(),
            "injury_history": [row.to_record() for row in self.injury_history],
            "sleeper_players": [row.to_record() for row in self.sleeper_players],
            "trends": [row.to_record() for row in self.trends],
            "id_crosswalk": [row.to_record() for row in self.id_crosswalk],
            "provenance": [row.to_record() for row in self.provenance],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "public_player_data",
            "schema_version": _SCHEMA_VERSION,
            **self._content_record(),
            "data_id": self.data_id,
        }

    @classmethod
    def from_record(cls, record):
        """Rebuild and content-verify a strict serialized public-data snapshot."""

        from ._public_player_record import snapshot_from_record

        return snapshot_from_record(record)

    @classmethod
    def from_json(cls, payload):
        """Decode strict JSON, rejecting duplicate keys before reconstruction."""

        from ._public_player_record import snapshot_from_json

        return snapshot_from_json(payload)


def collect_public_player_data(
    season: int,
    *,
    as_of_week: int = 1,
    limits: PublicPlayerDataLimits = PublicPlayerDataLimits(),
    cancelled: Callable[[], bool] = lambda: False,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    fetcher: Callable[..., DownloadedPublicData] = bounded_https_get,
) -> PublicPlayerDataSnapshot:
    """Fetch each public dataset exactly once and return one strict snapshot."""

    from ._public_player_collect import collect_public_player_data as collect

    return collect(
        season,
        as_of_week=as_of_week,
        limits=limits,
        cancelled=cancelled,
        clock=clock,
        fetcher=fetcher,
    )


def _integer(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _headshot_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("headshot URL must be bounded text")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "static.www.nfl.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("headshot URL must use the NFL static-image host")


def _position(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z/]{1,16}", value):
        raise ValueError("position must be a compact uppercase code")
    return value


def _team(value):
    if value not in _NFL_TEAMS:
        raise ValueError("NFL team must be a supported team code")
    return value


def _season(value):
    return _integer("season", value, 1999, 2200)


def _finite(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be finite numeric data")
    return float(value)


def _bounded_text(name, value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{name} must be non-empty text of at most {maximum} characters")
    return value.strip()


def _public_url(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("requested_url must be bounded text")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {
            "github.com", "api.sleeper.app", "raw.githubusercontent.com"
        }
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("requested_url is not an allowlisted public data URL")


def _aware_time(name, value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _typed_tuple(name, values, expected_type):
    try:
        result = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be iterable") from None
    if any(not isinstance(value, expected_type) for value in result):
        raise ValueError(f"{name} contains an invalid value")
    return result


def _unique(label, values: Iterable[str]):
    rows = tuple(values)
    if len(set(rows)) != len(rows):
        raise ValueError(f"{label} IDs must be unique")


def _canonical_json(value):
    try:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise AssertionError("public player-data snapshot was not strict JSON") from None


__all__ = (
    "DataAvailability",
    "PlayerInjuryReport",
    "PlayerWeekStats",
    "PublicDataProvenance",
    "PublicPlayerDataCancelled",
    "PublicPlayerDataError",
    "PublicPlayerDataLimits",
    "PublicPlayerDataSnapshot",
    "PublicPlayerIdCrosswalk",
    "PublicPlayerSource",
    "SeasonInjuryReports",
    "SeasonPlayerStats",
    "SleeperPlayerMetadata",
    "SleeperPlayerTrend",
    "collect_public_player_data",
    "public_player_source_urls",
)
