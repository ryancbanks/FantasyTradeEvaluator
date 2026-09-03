"""Immutable, portable NFL player profile data for one weekly bundle."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from numbers import Real
from types import MappingProxyType
from typing import Any

from ._scenario_random import content_id, require_json_int, require_text
from .identity import ProviderReference
from .positions import normalize_player_position


_SCHEMA_VERSION = 1
_AVAILABILITY_STATES = frozenset({"observed", "not_published", "unavailable"})


@dataclass(frozen=True, slots=True)
class PlayerGameStats:
    """One player's actual public stat line for one NFL week."""

    season: int
    week: int
    game_id: str | None
    nfl_team_id: str | None
    opponent_team_id: str | None
    fantasy_points_standard: float | None
    fantasy_points_ppr: float | None
    stat_values: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_json_int("season", self.season, minimum=2000)
        require_json_int("week", self.week, minimum=1)
        if self.week > 25:
            raise ValueError("week must not exceed 25")
        for name in ("game_id", "nfl_team_id", "opponent_team_id"):
            _optional_text(name, getattr(self, name))
        for name in ("fantasy_points_standard", "fantasy_points_ppr"):
            value = getattr(self, name)
            if value is not None and not _is_finite_number(value):
                raise ValueError(f"{name} must be a finite number or None")
            if value is not None:
                object.__setattr__(self, name, float(value))
        object.__setattr__(self, "stat_values", _finite_number_map(self.stat_values))

    def to_record(self) -> dict[str, object]:
        return {
            "season": self.season,
            "week": self.week,
            "game_id": self.game_id,
            "nfl_team_id": self.nfl_team_id,
            "opponent_team_id": self.opponent_team_id,
            "fantasy_points_standard": self.fantasy_points_standard,
            "fantasy_points_ppr": self.fantasy_points_ppr,
            "stat_values": dict(self.stat_values),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerGameStats":
        _require_fields(
            "player game stats",
            record,
            {
                "season",
                "week",
                "game_id",
                "nfl_team_id",
                "opponent_team_id",
                "fantasy_points_standard",
                "fantasy_points_ppr",
                "stat_values",
            },
        )
        return cls(
            season=record["season"],
            week=record["week"],
            game_id=record["game_id"],
            nfl_team_id=record["nfl_team_id"],
            opponent_team_id=record["opponent_team_id"],
            fantasy_points_standard=record["fantasy_points_standard"],
            fantasy_points_ppr=record["fantasy_points_ppr"],
            stat_values=_mapping("stat_values", record["stat_values"]),
        )


@dataclass(frozen=True, slots=True)
class PlayerAvailabilityEvent:
    """One source-documented weekly injury or practice report, without inference."""

    season: int
    week: int
    nfl_team_id: str | None
    report_primary_injury: str | None
    report_secondary_injury: str | None
    report_status: str | None
    practice_primary_injury: str | None
    practice_secondary_injury: str | None
    practice_status: str | None
    source_modified_at: datetime | None

    def __post_init__(self) -> None:
        require_json_int("season", self.season, minimum=2000)
        require_json_int("week", self.week, minimum=1)
        if self.week > 25:
            raise ValueError("week must not exceed 25")
        object.__setattr__(self, "nfl_team_id", _normalized_optional_team(self.nfl_team_id))
        for name in (
            "report_primary_injury",
            "report_secondary_injury",
            "report_status",
            "practice_primary_injury",
            "practice_secondary_injury",
            "practice_status",
        ):
            _optional_text(name, getattr(self, name))
        if self.report_status not in {None, "out", "doubtful", "questionable", "probable", "note"}:
            raise ValueError("report_status is invalid")
        if self.practice_status not in {
            None, "did_not_participate", "limited", "full", "note"
        }:
            raise ValueError("practice_status is invalid")
        if self.source_modified_at is not None:
            _require_aware("source_modified_at", self.source_modified_at)

    def to_record(self) -> dict[str, object]:
        return {
            "season": self.season,
            "week": self.week,
            "nfl_team_id": self.nfl_team_id,
            "report_primary_injury": self.report_primary_injury,
            "report_secondary_injury": self.report_secondary_injury,
            "report_status": self.report_status,
            "practice_primary_injury": self.practice_primary_injury,
            "practice_secondary_injury": self.practice_secondary_injury,
            "practice_status": self.practice_status,
            "source_modified_at": _optional_time(self.source_modified_at),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerAvailabilityEvent":
        fields = {
            "season", "week", "nfl_team_id", "report_primary_injury",
            "report_secondary_injury", "report_status", "practice_primary_injury",
            "practice_secondary_injury", "practice_status", "source_modified_at",
        }
        _require_fields("player availability event", record, fields)
        return cls(
            season=record["season"], week=record["week"],
            nfl_team_id=record["nfl_team_id"],
            report_primary_injury=record["report_primary_injury"],
            report_secondary_injury=record["report_secondary_injury"],
            report_status=record["report_status"],
            practice_primary_injury=record["practice_primary_injury"],
            practice_secondary_injury=record["practice_secondary_injury"],
            practice_status=record["practice_status"],
            source_modified_at=_parse_optional_time(
                "source_modified_at", record["source_modified_at"]
            ),
        )


@dataclass(frozen=True, slots=True)
class PlayerAvailabilitySeason:
    """Whether one season's public injury-report file was actually available."""

    season: int
    availability: str

    def __post_init__(self) -> None:
        require_json_int("season", self.season, minimum=2000)
        _availability("availability", self.availability)

    def to_record(self) -> dict[str, object]:
        return {"season": self.season, "availability": self.availability}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerAvailabilitySeason":
        _require_fields("player availability season", record, {"season", "availability"})
        return cls(record["season"], record["availability"])


@dataclass(frozen=True, slots=True)
class PlayerProfileProvenance:
    """Sanitized source evidence carried with a portable profile snapshot."""

    provider: str
    dataset: str
    source_url: str
    captured_at: datetime
    source_updated_at: datetime | None
    etag: str | None
    status: str
    content_sha256: str | None
    byte_count: int

    def __post_init__(self) -> None:
        for name in ("provider", "dataset", "source_url"):
            require_text(name, getattr(self, name))
        _availability("status", self.status)
        _require_aware("captured_at", self.captured_at)
        if self.source_updated_at is not None:
            _require_aware("source_updated_at", self.source_updated_at)
        _optional_text("etag", self.etag)
        _optional_text("content_sha256", self.content_sha256)
        if self.content_sha256 is not None and (
            len(self.content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.content_sha256)
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        require_json_int("byte_count", self.byte_count, minimum=0)
        if self.status == "observed":
            if self.content_sha256 is None or self.byte_count == 0:
                raise ValueError("observed provenance requires captured bytes")
        elif self.content_sha256 is not None or self.byte_count != 0:
            raise ValueError("unobserved provenance cannot contain captured bytes")

    def to_record(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "dataset": self.dataset,
            "source_url": self.source_url,
            "captured_at": _iso_utc(self.captured_at),
            "source_updated_at": _optional_time(self.source_updated_at),
            "etag": self.etag,
            "status": self.status,
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerProfileProvenance":
        _require_fields(
            "player profile provenance",
            record,
            {
                "provider",
                "dataset",
                "source_url",
                "captured_at",
                "source_updated_at",
                "etag",
                "status",
                "content_sha256",
                "byte_count",
            },
        )
        return cls(
            provider=record["provider"],
            dataset=record["dataset"],
            source_url=record["source_url"],
            captured_at=_parse_time("captured_at", record["captured_at"]),
            source_updated_at=_parse_optional_time(
                "source_updated_at", record["source_updated_at"]
            ),
            etag=record["etag"],
            status=record["status"],
            content_sha256=record["content_sha256"],
            byte_count=record["byte_count"],
        )


@dataclass(frozen=True, slots=True)
class PlayerProfile:
    """Canonical metadata, history, depth, and public-interest signals."""

    canonical_player_id: str
    display_name: str
    position: str | None
    nfl_team_id: str | None
    provider_references: tuple[ProviderReference, ...] = ()
    fantasy_positions: tuple[str, ...] = ()
    active: bool | None = None
    status: str | None = None
    injury_status: str | None = None
    injury_body_part: str | None = None
    practice_participation: str | None = None
    depth_chart_position: str | None = None
    depth_chart_order: int | None = None
    years_experience: int | None = None
    jersey_number: int | None = None
    headshot_url: str | None = None
    adds: int | None = None
    drops: int | None = None
    current_season_stats: tuple[PlayerGameStats, ...] = ()
    previous_season_stats: tuple[PlayerGameStats, ...] = ()
    availability_history: tuple[PlayerAvailabilityEvent, ...] = ()

    def __post_init__(self) -> None:
        require_text("canonical_player_id", self.canonical_player_id)
        require_text("display_name", self.display_name)
        position = _optional_position(self.position)
        team = _normalized_optional_team(self.nfl_team_id)
        references = _typed_tuple(
            "provider_references", self.provider_references, ProviderReference
        )
        keys = tuple(reference.key for reference in references)
        if len(set(keys)) != len(keys):
            raise ValueError("player profile has a duplicate provider reference")
        fantasy_positions = tuple(
            sorted({_position(value) for value in self.fantasy_positions})
        )
        if self.active is not None and not isinstance(self.active, bool):
            raise ValueError("active must be a boolean or None")
        for name in (
            "status",
            "injury_status",
            "injury_body_part",
            "practice_participation",
            "depth_chart_position",
            "headshot_url",
        ):
            _optional_text(name, getattr(self, name))
        for name, minimum, maximum in (
            ("depth_chart_order", 1, None),
            ("years_experience", 0, None),
            ("jersey_number", 0, 99),
            ("adds", 0, None),
            ("drops", 0, None),
        ):
            _optional_json_int(name, getattr(self, name), minimum, maximum)
        current = _season_rows(
            "current-season", self.current_season_stats, PlayerGameStats
        )
        previous = _season_rows(
            "previous-season", self.previous_season_stats, PlayerGameStats
        )
        availability = _availability_rows(self.availability_history)
        if current and len({row.season for row in current}) != 1:
            raise ValueError("current-season stats must use one season")
        if previous and len({row.season for row in previous}) != 1:
            raise ValueError("previous-season stats must use one season")
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "nfl_team_id", team)
        object.__setattr__(self, "provider_references", tuple(sorted(references, key=lambda row: row.key)))
        object.__setattr__(self, "fantasy_positions", fantasy_positions)
        object.__setattr__(self, "current_season_stats", current)
        object.__setattr__(self, "previous_season_stats", previous)
        object.__setattr__(self, "availability_history", availability)

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "display_name": self.display_name,
            "position": self.position,
            "nfl_team_id": self.nfl_team_id,
            "provider_references": [row.to_record() for row in self.provider_references],
            "fantasy_positions": list(self.fantasy_positions),
            "active": self.active,
            "status": self.status,
            "injury_status": self.injury_status,
            "injury_body_part": self.injury_body_part,
            "practice_participation": self.practice_participation,
            "depth_chart_position": self.depth_chart_position,
            "depth_chart_order": self.depth_chart_order,
            "years_experience": self.years_experience,
            "jersey_number": self.jersey_number,
            "headshot_url": self.headshot_url,
            "adds": self.adds,
            "drops": self.drops,
            "current_season_stats": [row.to_record() for row in self.current_season_stats],
            "previous_season_stats": [row.to_record() for row in self.previous_season_stats],
            "availability_history": [row.to_record() for row in self.availability_history],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerProfile":
        fields = {
            "canonical_player_id", "display_name", "position", "nfl_team_id",
            "provider_references", "fantasy_positions", "active", "status",
            "injury_status", "injury_body_part", "practice_participation",
            "depth_chart_position", "depth_chart_order", "years_experience",
            "jersey_number", "headshot_url", "adds", "drops",
            "current_season_stats", "previous_season_stats",
            "availability_history",
        }
        _require_fields("player profile", record, fields)
        references = _record_list("provider_references", record["provider_references"])
        current = _record_list("current_season_stats", record["current_season_stats"])
        previous = _record_list("previous_season_stats", record["previous_season_stats"])
        availability = _record_list("availability_history", record["availability_history"])
        return cls(
            canonical_player_id=record["canonical_player_id"],
            display_name=record["display_name"],
            position=record["position"],
            nfl_team_id=record["nfl_team_id"],
            provider_references=tuple(ProviderReference.from_record(row) for row in references),
            fantasy_positions=tuple(_json_list("fantasy_positions", record["fantasy_positions"])),
            active=record["active"], status=record["status"],
            injury_status=record["injury_status"], injury_body_part=record["injury_body_part"],
            practice_participation=record["practice_participation"],
            depth_chart_position=record["depth_chart_position"],
            depth_chart_order=record["depth_chart_order"],
            years_experience=record["years_experience"],
            jersey_number=record["jersey_number"], headshot_url=record["headshot_url"],
            adds=record["adds"], drops=record["drops"],
            current_season_stats=tuple(PlayerGameStats.from_record(row) for row in current),
            previous_season_stats=tuple(PlayerGameStats.from_record(row) for row in previous),
            availability_history=tuple(
                PlayerAvailabilityEvent.from_record(row) for row in availability
            ),
        )


@dataclass(frozen=True, slots=True)
class ProfileMaterializationIssue:
    """One external identity row withheld rather than guessed into a profile."""

    provider: str
    provider_player_id: str
    reason: str

    def __post_init__(self) -> None:
        require_text("provider", self.provider)
        require_text("provider_player_id", self.provider_player_id)
        require_text("reason", self.reason)

    def to_record(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "provider_player_id": self.provider_player_id,
            "reason": self.reason,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProfileMaterializationIssue":
        _require_fields(
            "profile materialization issue",
            record,
            {"provider", "provider_player_id", "reason"},
        )
        return cls(record["provider"], record["provider_player_id"], record["reason"])


@dataclass(frozen=True, slots=True)
class PlayerProfileSnapshot:
    """The full captured player catalog attached to one league-week engine."""

    league_snapshot_id: str
    season: int
    as_of_week: int
    captured_at: datetime
    identity_registry_id: str
    source_data_id: str
    current_stats_availability: str
    previous_stats_availability: str
    players: tuple[PlayerProfile, ...]
    provenance: tuple[PlayerProfileProvenance, ...]
    injury_history_availability: tuple[PlayerAvailabilitySeason, ...] = ()
    materialization_issues: tuple[ProfileMaterializationIssue, ...] = ()
    profile_snapshot_id: str = field(init=False)
    players_by_id: Mapping[str, PlayerProfile] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_text("league_snapshot_id", self.league_snapshot_id)
        require_json_int("season", self.season, minimum=2012)
        require_json_int("as_of_week", self.as_of_week, minimum=1)
        if self.as_of_week > 25:
            raise ValueError("as_of_week must not exceed 25")
        _require_aware("captured_at", self.captured_at)
        for name in ("identity_registry_id", "source_data_id"):
            require_text(name, getattr(self, name))
        _availability("current_stats_availability", self.current_stats_availability)
        _availability("previous_stats_availability", self.previous_stats_availability)
        players = _typed_tuple("players", self.players, PlayerProfile)
        provenance = _typed_tuple("provenance", self.provenance, PlayerProfileProvenance)
        injury_availability = _typed_tuple(
            "injury_history_availability",
            self.injury_history_availability,
            PlayerAvailabilitySeason,
        )
        issues = _typed_tuple(
            "materialization_issues",
            self.materialization_issues,
            ProfileMaterializationIssue,
        )
        issue_keys = tuple((row.provider, row.provider_player_id) for row in issues)
        if len(set(issue_keys)) != len(issue_keys):
            raise ValueError("materialization issues contain a duplicate external identity")
        injury_seasons = tuple(row.season for row in injury_availability)
        if len(set(injury_seasons)) != len(injury_seasons):
            raise ValueError("injury history contains duplicate season availability")
        if any(
            season not in {self.season, self.season - 1, self.season - 2}
            for season in injury_seasons
        ):
            raise ValueError("injury availability must cover only the current or prior two seasons")
        player_ids = tuple(row.canonical_player_id for row in players)
        if not players or len(set(player_ids)) != len(player_ids):
            raise ValueError("players must be non-empty with unique canonical player IDs")
        for player in players:
            if any(row.season != self.season for row in player.current_season_stats):
                raise ValueError("current-season stats do not match profile season")
            if any(
                row.week >= self.as_of_week for row in player.current_season_stats
            ):
                raise ValueError(
                    "current-season stats must precede the profile as-of week"
                )
            if any(row.season != self.season - 1 for row in player.previous_season_stats):
                raise ValueError("previous-season stats do not match profile season")
            if any(
                row.season not in {self.season, self.season - 1, self.season - 2}
                for row in player.availability_history
            ):
                raise ValueError("availability history must cover only the current or prior two seasons")
            if any(
                row.season == self.season and row.week > self.as_of_week
                for row in player.availability_history
            ):
                raise ValueError(
                    "current-season availability must not follow the profile as-of week"
                )
        players = tuple(sorted(players, key=lambda row: row.canonical_player_id))
        provenance = tuple(sorted(provenance, key=lambda row: (row.provider, row.dataset, row.source_url)))
        object.__setattr__(self, "players", players)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(
            self,
            "injury_history_availability",
            tuple(sorted(injury_availability, key=lambda row: row.season, reverse=True)),
        )
        object.__setattr__(
            self,
            "materialization_issues",
            tuple(sorted(issues, key=lambda row: (row.provider, row.provider_player_id))),
        )
        object.__setattr__(self, "players_by_id", MappingProxyType({row.canonical_player_id: row for row in players}))
        object.__setattr__(self, "profile_snapshot_id", content_id("profiles", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        return {
            "kind": "nfl_player_profile_snapshot",
            "schema_version": _SCHEMA_VERSION,
            "league_snapshot_id": self.league_snapshot_id,
            "season": self.season,
            "as_of_week": self.as_of_week,
            "captured_at": _iso_utc(self.captured_at),
            "identity_registry_id": self.identity_registry_id,
            "source_data_id": self.source_data_id,
            "current_stats_availability": self.current_stats_availability,
            "previous_stats_availability": self.previous_stats_availability,
            "players": [row.to_record() for row in self.players],
            "provenance": [row.to_record() for row in self.provenance],
            "injury_history_availability": [
                row.to_record() for row in self.injury_history_availability
            ],
            "materialization_issues": [
                row.to_record() for row in self.materialization_issues
            ],
        }

    def to_record(self) -> dict[str, object]:
        return {**self._content_record(), "profile_snapshot_id": self.profile_snapshot_id}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerProfileSnapshot":
        expected = {
            "kind", "schema_version", "league_snapshot_id", "season", "as_of_week",
            "captured_at", "identity_registry_id", "source_data_id",
            "current_stats_availability", "previous_stats_availability", "players",
            "provenance", "profile_snapshot_id",
            "injury_history_availability",
            "materialization_issues",
        }
        _require_fields("player profile snapshot", record, expected)
        if (
            record["kind"] != "nfl_player_profile_snapshot"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != _SCHEMA_VERSION
        ):
            raise ValueError("player profile snapshot kind or schema version is invalid")
        players = _record_list("players", record["players"])
        provenance = _record_list("provenance", record["provenance"])
        injury_availability = _record_list(
            "injury_history_availability", record["injury_history_availability"]
        )
        issues = _record_list("materialization_issues", record["materialization_issues"])
        snapshot = cls(
            league_snapshot_id=record["league_snapshot_id"], season=record["season"],
            as_of_week=record["as_of_week"],
            captured_at=_parse_time("captured_at", record["captured_at"]),
            identity_registry_id=record["identity_registry_id"],
            source_data_id=record["source_data_id"],
            current_stats_availability=record["current_stats_availability"],
            previous_stats_availability=record["previous_stats_availability"],
            players=tuple(PlayerProfile.from_record(row) for row in players),
            provenance=tuple(PlayerProfileProvenance.from_record(row) for row in provenance),
            injury_history_availability=tuple(
                PlayerAvailabilitySeason.from_record(row)
                for row in injury_availability
            ),
            materialization_issues=tuple(
                ProfileMaterializationIssue.from_record(row) for row in issues
            ),
        )
        if record["profile_snapshot_id"] != snapshot.profile_snapshot_id:
            raise ValueError("player profile content does not match profile_snapshot_id")
        return snapshot


def _season_rows(name: str, values: Iterable[Any], expected_type: type) -> tuple[Any, ...]:
    rows = _typed_tuple(name, values, expected_type)
    weeks = tuple(row.week for row in rows)
    if len(set(weeks)) != len(weeks):
        raise ValueError(f"player profile contains a duplicate {name} player week")
    return tuple(sorted(rows, key=lambda row: (row.season, row.week, row.game_id or "")))


def _availability_rows(values: Iterable[PlayerAvailabilityEvent]) -> tuple[PlayerAvailabilityEvent, ...]:
    rows = _typed_tuple("availability_history", values, PlayerAvailabilityEvent)
    keys = tuple((row.season, row.week) for row in rows)
    if len(set(keys)) != len(keys):
        raise ValueError("player profile contains a duplicate availability season/week")
    return tuple(sorted(rows, key=lambda row: (row.season, row.week)))


def _typed_tuple(name: str, values: Iterable[Any], expected_type: type) -> tuple[Any, ...]:
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(not isinstance(row, expected_type) for row in rows):
        raise ValueError(f"{name} must contain only {expected_type.__name__} values")
    return rows


def _finite_number_map(value: object) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("stat_values must be a mapping")
    result = {}
    for name, number in value.items():
        require_text("stat name", name)
        if not _is_finite_number(number):
            raise ValueError(f"stat value {name!r} must be finite")
        result[name] = float(number)
    return MappingProxyType(dict(sorted(result.items())))


def _position(value: object) -> str:
    return normalize_player_position(value)


def _optional_position(value: object) -> str | None:
    return None if value is None else _position(value)


def _normalized_optional_team(value: object) -> str | None:
    if value is None:
        return None
    require_text("nfl_team_id", value)
    return value.strip().upper()


def _optional_json_int(name: str, value: object, minimum: int, maximum: int | None) -> None:
    if value is None:
        return
    require_json_int(name, value, minimum=minimum)
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must not exceed {maximum}")


def _optional_text(name: str, value: object) -> None:
    if value is not None:
        require_text(name, value)


def _availability(name: str, value: object) -> None:
    if not isinstance(value, str) or value not in _AVAILABILITY_STATES:
        raise ValueError(
            f"{name} must be observed, not_published, or unavailable"
        )


def _is_finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)


def _require_aware(name: str, value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else _iso_utc(value)


def _parse_time(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 string") from None
    _require_aware(name, parsed)
    return parsed


def _parse_optional_time(name: str, value: object) -> datetime | None:
    return None if value is None else _parse_time(name, value)


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _json_list(name: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _record_list(name: str, value: object) -> list[Mapping[str, object]]:
    rows = _json_list(name, value)
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError(f"{name} must contain JSON objects")
    return rows


def _require_fields(name: str, record: object, expected: set[str]) -> None:
    if not isinstance(record, Mapping) or set(record) != expected:
        raise ValueError(f"{name} fields are invalid")


__all__ = (
    "PlayerAvailabilityEvent",
    "PlayerAvailabilitySeason",
    "PlayerGameStats",
    "PlayerProfile",
    "PlayerProfileProvenance",
    "PlayerProfileSnapshot",
    "ProfileMaterializationIssue",
)
