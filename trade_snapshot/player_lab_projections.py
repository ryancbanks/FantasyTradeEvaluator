"""Compact full-catalog projections kept outside calculation inputs."""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType

from ._scenario_random import content_id, require_json_int, require_text
from .ensemble import (
    EnsembleProjection,
    ensemble_from_record,
    ensemble_to_record,
)
from .positions import normalize_player_position
from .projection_source_policy import (
    validate_no_composite_double_count,
    validate_selectable_projection_providers,
)


_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
MAX_PLAYER_LAB_PROJECTION_PLAYERS = 4096
MAX_PLAYER_LAB_PROJECTION_ROWS = MAX_PLAYER_LAB_PROJECTION_PLAYERS * 25


@dataclass(frozen=True, slots=True)
class PlayerLabProviderProvenance:
    """Compact freshness evidence for one projection publisher capture."""

    provider: str
    captured_at: datetime
    source_published_at: datetime | None = None

    def __post_init__(self) -> None:
        provider = validate_selectable_projection_providers((self.provider,))[0]
        _aware_time("captured_at", self.captured_at)
        if self.source_published_at is not None:
            _aware_time("source_published_at", self.source_published_at)
        object.__setattr__(self, "provider", provider)

    def to_record(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "captured_at": _iso_utc(self.captured_at),
            "source_published_at": (
                None
                if self.source_published_at is None
                else _iso_utc(self.source_published_at)
            ),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerLabProviderProvenance":
        if not isinstance(record, Mapping) or set(record) != {
            "provider",
            "captured_at",
            "source_published_at",
        }:
            raise ValueError("Player Lab provider provenance fields are invalid")
        return cls(
            provider=record["provider"],
            captured_at=_parse_time("captured_at", record["captured_at"]),
            source_published_at=(
                None
                if record["source_published_at"] is None
                else _parse_time(
                    "source_published_at", record["source_published_at"]
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class PlayerLabProjectionSnapshot:
    """Validated ensemble rows for projected players outside the trade pool.

    Raw provider evidence remains bounded to the calculation pool.  These rows
    retain the already-normalized weekly ensemble and its one-observation-per-
    publisher status, which is enough for Player Lab filtering and detail views.
    """

    league_snapshot_id: str
    scoring_profile_id: str
    season: int
    as_of_week: int
    remaining_weeks: tuple[int, ...]
    provider_names: tuple[str, ...]
    projections: tuple[EnsembleProjection, ...] = ()
    player_names: Mapping[str, str] = field(default_factory=dict)
    player_positions: Mapping[str, str] = field(default_factory=dict)
    player_nfl_team_ids: Mapping[str, str] = field(default_factory=dict)
    provider_provenance: tuple[PlayerLabProviderProvenance, ...] = ()
    projection_snapshot_id: str = field(init=False)
    projections_by_player: Mapping[str, tuple[EnsembleProjection, ...]] = field(
        init=False, repr=False, compare=False
    )
    provider_provenance_by_name: Mapping[str, PlayerLabProviderProvenance] = field(
        init=False, repr=False, compare=False
    )
    insufficient_weeks_by_player: Mapping[str, tuple[int, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        require_text("league_snapshot_id", self.league_snapshot_id)
        require_text("scoring_profile_id", self.scoring_profile_id)
        require_json_int("season", self.season, minimum=2012)
        require_json_int("as_of_week", self.as_of_week, minimum=1)
        if self.as_of_week > 25:
            raise ValueError("as_of_week must not exceed 25")
        weeks = _weeks(self.remaining_weeks)
        if not weeks or weeks[0] != self.as_of_week:
            raise ValueError("remaining_weeks must begin with as_of_week")
        providers = _providers(self.provider_names)
        provenance = _provenance(self.provider_provenance, providers)
        rows = _typed_rows(self.projections)
        if len(rows) > MAX_PLAYER_LAB_PROJECTION_ROWS:
            raise ValueError("Player Lab projection row limit exceeded")
        groups = defaultdict(list)
        identity = (
            self.league_snapshot_id,
            self.scoring_profile_id,
            self.season,
        )
        for row in rows:
            if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
                raise ValueError("Player Lab projection context does not match its snapshot")
            if tuple(value.provider for value in row.provider_observations) != providers:
                raise ValueError("Player Lab projection publishers do not match the snapshot")
            groups[row.canonical_player_id].append(row)
        names = _names(self.player_names)
        player_ids = set(names)
        if len(player_ids) > MAX_PLAYER_LAB_PROJECTION_PLAYERS:
            raise ValueError("Player Lab projection player limit exceeded")
        if not set(groups).issubset(player_ids):
            raise ValueError("projection rows contain a player without metadata")
        positions = _metadata_map(
            "player_positions",
            self.player_positions,
            player_ids,
            fallback={
                player_id: player_rows[0].position
                for player_id, player_rows in groups.items()
            },
            normalize_position=True,
        )
        teams = _metadata_map(
            "player_nfl_team_ids",
            self.player_nfl_team_ids,
            player_ids,
            fallback={
                player_id: player_rows[0].nfl_team_id
                for player_id, player_rows in groups.items()
            },
        )
        expected_weeks = set(weeks)
        for player_id in player_ids:
            player_rows = groups[player_id]
            row_weeks = tuple(row.week for row in player_rows)
            if len(set(row_weeks)) != len(row_weeks) or not set(row_weeks) <= expected_weeks:
                raise ValueError(f"Player Lab projection {player_id!r} has invalid weeks")
            if len({row.position for row in player_rows}) != 1:
                if player_rows:
                    raise ValueError(
                        f"Player Lab projection {player_id!r} has inconsistent positions"
                    )
            if any(row.position != positions[player_id] for row in player_rows):
                raise ValueError(f"Player Lab projection {player_id!r} position conflicts")
            if any(row.nfl_team_id != teams[player_id] for row in player_rows):
                raise ValueError(f"Player Lab projection {player_id!r} NFL team conflicts")
        ordered = tuple(
            sorted(rows, key=lambda row: (row.canonical_player_id, row.week))
        )
        frozen_groups = MappingProxyType(
            {
                player_id: tuple(sorted(groups[player_id], key=lambda row: row.week))
                for player_id in sorted(player_ids)
            }
        )
        object.__setattr__(self, "remaining_weeks", weeks)
        object.__setattr__(self, "provider_names", providers)
        object.__setattr__(self, "projections", ordered)
        object.__setattr__(self, "player_names", names)
        object.__setattr__(self, "player_positions", positions)
        object.__setattr__(self, "player_nfl_team_ids", teams)
        object.__setattr__(self, "provider_provenance", provenance)
        object.__setattr__(self, "projections_by_player", frozen_groups)
        object.__setattr__(
            self,
            "provider_provenance_by_name",
            MappingProxyType({row.provider: row for row in provenance}),
        )
        object.__setattr__(
            self,
            "insufficient_weeks_by_player",
            MappingProxyType(
                {
                    player_id: tuple(
                        week
                        for week in weeks
                        if week not in {row.week for row in groups[player_id]}
                    )
                    for player_id in sorted(player_ids)
                }
            ),
        )
        object.__setattr__(
            self,
            "projection_snapshot_id",
            content_id("player-lab-projections", self._content_record()),
        )

    @property
    def player_ids(self) -> tuple[str, ...]:
        return tuple(self.player_names)

    def _content_record(self) -> dict[str, object]:
        return {
            "kind": "player_lab_projection_snapshot",
            "schema_version": _SCHEMA_VERSION,
            "league_snapshot_id": self.league_snapshot_id,
            "scoring_profile_id": self.scoring_profile_id,
            "season": self.season,
            "as_of_week": self.as_of_week,
            "remaining_weeks": list(self.remaining_weeks),
            "provider_names": list(self.provider_names),
            "projections": [ensemble_to_record(row) for row in self.projections],
            "player_names": dict(self.player_names),
            "player_positions": dict(self.player_positions),
            "player_nfl_team_ids": dict(self.player_nfl_team_ids),
            "provider_provenance": [row.to_record() for row in self.provider_provenance],
        }

    def to_record(self) -> dict[str, object]:
        return {
            **self._content_record(),
            "projection_snapshot_id": self.projection_snapshot_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerLabProjectionSnapshot":
        common = {
            "kind",
            "schema_version",
            "league_snapshot_id",
            "scoring_profile_id",
            "season",
            "as_of_week",
            "remaining_weeks",
            "provider_names",
            "projections",
            "player_names",
            "projection_snapshot_id",
        }
        if not isinstance(record, Mapping):
            raise ValueError("Player Lab projection snapshot fields are invalid")
        version = record.get("schema_version")
        if type(version) is not int or version not in {
            _LEGACY_SCHEMA_VERSION,
            _SCHEMA_VERSION,
        }:
            raise ValueError("Player Lab projection snapshot schema is invalid")
        expected = (
            common
            if version == _LEGACY_SCHEMA_VERSION
            else common
            | {"player_positions", "player_nfl_team_ids", "provider_provenance"}
            if version == _SCHEMA_VERSION
            else set()
        )
        if set(record) != expected or record.get("kind") != "player_lab_projection_snapshot":
            raise ValueError("Player Lab projection snapshot schema is invalid")
        if version == _LEGACY_SCHEMA_VERSION:
            legacy_content = {
                key: record[key] for key in common if key != "projection_snapshot_id"
            }
            if record["projection_snapshot_id"] != content_id(
                "player-lab-projections", legacy_content
            ):
                raise ValueError("Player Lab projection content does not match its ID")
        weeks = _json_list("remaining_weeks", record["remaining_weeks"])
        providers = _json_list("provider_names", record["provider_names"])
        raw_rows = _json_list("projections", record["projections"])
        snapshot = cls(
            league_snapshot_id=record["league_snapshot_id"],
            scoring_profile_id=record["scoring_profile_id"],
            season=record["season"],
            as_of_week=record["as_of_week"],
            remaining_weeks=tuple(weeks),
            provider_names=tuple(providers),
            projections=tuple(
                ensemble_from_record(_mapping("projection", row)) for row in raw_rows
            ),
            player_names=_mapping("player_names", record["player_names"]),
            player_positions=(
                {}
                if version == _LEGACY_SCHEMA_VERSION
                else _mapping("player_positions", record["player_positions"])
            ),
            player_nfl_team_ids=(
                {}
                if version == _LEGACY_SCHEMA_VERSION
                else _mapping(
                    "player_nfl_team_ids", record["player_nfl_team_ids"]
                )
            ),
            provider_provenance=(
                ()
                if version == _LEGACY_SCHEMA_VERSION
                else tuple(
                    PlayerLabProviderProvenance.from_record(
                        _mapping("provider_provenance", row)
                    )
                    for row in _json_list(
                        "provider_provenance", record["provider_provenance"]
                    )
                )
            ),
        )
        if (
            version == _SCHEMA_VERSION
            and record["projection_snapshot_id"] != snapshot.projection_snapshot_id
        ):
            raise ValueError("Player Lab projection content does not match its ID")
        return snapshot


def _providers(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("provider_names must be an iterable")
    providers = tuple(values)
    if not providers or any(not isinstance(value, str) or not value for value in providers):
        raise ValueError("provider_names must contain non-empty strings")
    if len(set(providers)) != len(providers):
        raise ValueError("provider_names contains a duplicate")
    validate_selectable_projection_providers(providers)
    validate_no_composite_double_count(providers)
    return providers


def _weeks(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("remaining_weeks must be an iterable")
    weeks = tuple(values)
    for week in weeks:
        require_json_int("remaining week", week, minimum=1)
        if week > 25:
            raise ValueError("remaining week must not exceed 25")
    if len(set(weeks)) != len(weeks) or weeks != tuple(sorted(weeks)):
        raise ValueError("remaining_weeks must be unique and sorted")
    return weeks


def _typed_rows(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("projections must be an iterable")
    rows = tuple(values)
    if any(not isinstance(row, EnsembleProjection) for row in rows):
        raise ValueError("projections must contain EnsembleProjection rows")
    return rows


def _json_list(name, value):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _mapping(name, value):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _names(value):
    if not isinstance(value, Mapping):
        raise ValueError("player_names must be a mapping")
    result = {}
    for player_id, display_name in value.items():
        require_text("player ID", player_id)
        require_text("player display name", display_name)
        result[player_id] = display_name
    return MappingProxyType(dict(sorted(result.items())))


def _metadata_map(
    name,
    value,
    expected_ids,
    *,
    fallback,
    normalize_position=False,
):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    source = value if value else fallback
    if set(source) != expected_ids:
        raise ValueError(f"{name} must exactly cover Player Lab projection players")
    result = {}
    for player_id, item in source.items():
        require_text("player ID", player_id)
        require_text(name, item)
        result[player_id] = (
            normalize_player_position(item)
            if normalize_position
            else item
        )
    return MappingProxyType(dict(sorted(result.items())))


def _provenance(values, providers):
    if isinstance(values, (str, bytes)):
        raise ValueError("provider_provenance must be an iterable")
    rows = tuple(values)
    if any(not isinstance(row, PlayerLabProviderProvenance) for row in rows):
        raise ValueError(
            "provider_provenance must contain PlayerLabProviderProvenance rows"
        )
    if rows and tuple(row.provider for row in rows) != providers:
        raise ValueError("provider provenance must exactly cover publishers in order")
    return rows


def _aware_time(name, value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a valid datetime") from None
    if offset is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _parse_time(name, value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{name} must be a canonical UTC timestamp") from None
    if _iso_utc(parsed) != value:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    return parsed


def _iso_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = (
    "MAX_PLAYER_LAB_PROJECTION_PLAYERS",
    "MAX_PLAYER_LAB_PROJECTION_ROWS",
    "PlayerLabProjectionSnapshot",
    "PlayerLabProviderProvenance",
)
