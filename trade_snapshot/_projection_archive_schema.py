"""Strict immutable records for full, sanitized projection-table archives."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import re

from ._capture_common import (
    MAX_SAFE_INTEGER,
    content_id,
    is_forbidden_capture_key,
    looks_like_url,
    require_captured_at,
    require_content_id,
    require_json_int,
    require_text,
    schema_fingerprint,
)
from ._capture_dimensions import ProjectionTableSpec
from ._capture_plan import CaptureProvider
from ._capture_validation import exact_fields
from ._projection_parse import projection_artifact_rows
from .capture_schema import GenericTableArtifact
from .identity import IdentityRegistry


PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT = schema_fingerprint(
    "full_projection_archive",
    {
        "sources": [
            "artifact_id", "task_id", "provider", "season", "week", "horizon",
            "scoring", "position_scope", "captured_at", "source_period_text",
            "segments_captured", "table_count", "row_count",
        ],
        "rows": [
            "source_artifact_id", "provider", "identity_provider",
            "provider_player_id", "display_name", "position", "nfl_team_id",
            "opponent_team_id", "is_home", "is_bye",
            "projected_fantasy_points", "raw_projected_stats",
        ],
        "privacy": "generic-table-derived-numeric-and-public-identity-fields-only-v1",
    },
)

_ARCHIVE_FIELDS = {
    "kind", "schema_version", "schema_fingerprint", "sources", "rows", "archive_id",
}
_SOURCE_FIELDS = {
    "artifact_id", "task_id", "provider", "season", "week", "horizon", "scoring",
    "position_scope", "captured_at", "source_period_text", "segments_captured",
    "table_count", "row_count",
}
_ROW_FIELDS = {
    "source_artifact_id", "provider", "identity_provider", "provider_player_id",
    "display_name", "position", "nfl_team_id", "opponent_team_id", "is_home",
    "is_bye", "projected_fantasy_points", "raw_projected_stats",
}
_PROVIDER_IDENTITIES = {
    "fantasypros": "fantasypros_projection",
    "espn": "espn",
    "yahoo": "yahoo",
    "cbs": "cbs",
    "fftoday": "fftoday",
    "fantasysharks": "fantasysharks",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$")
_STAT_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_TEAM = re.compile(r"^(?:[A-Z]{2,3}|FA)$")
_POSITION = re.compile(r"^[A-Z][A-Z0-9/]{0,7}$")
_PRIVATE_EVIDENCE = re.compile(
    r"\b(?:auth(?:orization)?|cookie|credential|espns2|member(?:\s+id)?|oauth|"
    r"password|session|signature|swid|ticket|token)\b\s*[:=]",
    flags=re.IGNORECASE,
)
_MAX_SOURCES = 10_000
_MAX_ROWS = 250_000
_MAX_STATS_PER_ROW = 64


@dataclass(frozen=True, slots=True)
class ProjectionArchiveSource:
    artifact_id: str
    task_id: str
    provider: str
    season: int
    week: int
    horizon: str
    scoring: str
    position_scope: tuple[str, ...]
    captured_at: str
    source_period_text: str
    segments_captured: int
    table_count: int
    row_count: int

    def __post_init__(self) -> None:
        require_content_id("artifact_id", self.artifact_id, "captable")
        require_content_id("task_id", self.task_id, "captask")
        try:
            provider = CaptureProvider(self.provider).value
        except (TypeError, ValueError):
            raise ValueError("projection archive source provider is invalid") from None
        season = require_json_int("season", self.season, minimum=2000, maximum=2200)
        week = require_json_int("week", self.week, minimum=1, maximum=25)
        spec = ProjectionTableSpec(self.horizon, self.scoring, self.position_scope)
        captured_at = require_captured_at(self.captured_at)
        period = _safe_text("source_period_text", self.source_period_text, 512)
        if _PRIVATE_EVIDENCE.search(period):
            raise ValueError("source_period_text cannot contain private session evidence")
        segments = require_json_int(
            "segments_captured", self.segments_captured, minimum=1, maximum=10_000
        )
        tables = require_json_int("table_count", self.table_count, minimum=1, maximum=256)
        rows = require_json_int("row_count", self.row_count, minimum=1, maximum=_MAX_ROWS)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "week", week)
        object.__setattr__(self, "horizon", spec.horizon.value)
        object.__setattr__(self, "scoring", spec.scoring)
        object.__setattr__(self, "position_scope", spec.position_scope)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "source_period_text", period)
        object.__setattr__(self, "segments_captured", segments)
        object.__setattr__(self, "table_count", tables)
        object.__setattr__(self, "row_count", rows)

    def to_record(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "provider": self.provider,
            "season": self.season,
            "week": self.week,
            "horizon": self.horizon,
            "scoring": self.scoring,
            "position_scope": list(self.position_scope),
            "captured_at": self.captured_at,
            "source_period_text": self.source_period_text,
            "segments_captured": self.segments_captured,
            "table_count": self.table_count,
            "row_count": self.row_count,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionArchiveSource":
        exact_fields(record, _SOURCE_FIELDS, "projection archive source")
        if not isinstance(record["position_scope"], list):
            raise ValueError("projection archive position_scope must be a JSON array")
        return cls(
            record["artifact_id"], record["task_id"], record["provider"],
            record["season"], record["week"], record["horizon"], record["scoring"],
            tuple(record["position_scope"]), record["captured_at"],
            record["source_period_text"], record["segments_captured"],
            record["table_count"], record["row_count"],
        )


@dataclass(frozen=True, slots=True)
class ProjectionArchiveRow:
    source_artifact_id: str
    provider: str
    identity_provider: str
    provider_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    opponent_team_id: str | None
    is_home: bool | None
    is_bye: bool
    projected_fantasy_points: float | None
    raw_projected_stats: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        require_content_id("source_artifact_id", self.source_artifact_id, "captable")
        try:
            provider = CaptureProvider(self.provider).value
        except (TypeError, ValueError):
            raise ValueError("projection archive row provider is invalid") from None
        identity_provider = _safe_identifier(
            "identity_provider", self.identity_provider, maximum=64
        )
        if identity_provider not in _PROVIDER_IDENTITIES.values():
            raise ValueError("projection archive identity_provider is invalid")
        player_id = _safe_text(
            "provider_player_id", self.provider_player_id, 256
        )
        if not (
            _SAFE_ID.fullmatch(player_id)
            or provider == CaptureProvider.ESPN.value
            and re.fullmatch(r"-[1-9][0-9]{0,19}", player_id)
        ):
            raise ValueError("provider_player_id contains unsafe characters")
        display_name = _safe_text("display_name", self.display_name, 200)
        position = _pattern_text("position", self.position, _POSITION)
        team = _pattern_text("nfl_team_id", self.nfl_team_id, _TEAM)
        opponent = (
            None
            if self.opponent_team_id is None
            else _pattern_text("opponent_team_id", self.opponent_team_id, _TEAM)
        )
        if opponent == team:
            raise ValueError("projection archive opponent cannot equal NFL team")
        if self.is_home is not None and type(self.is_home) is not bool:
            raise ValueError("projection archive is_home must be boolean or null")
        if type(self.is_bye) is not bool:
            raise ValueError("projection archive is_bye must be a boolean")
        if self.is_bye and (opponent is not None or self.is_home is not None):
            raise ValueError("bye projection rows cannot have opponent context")
        points = _optional_number("projected_fantasy_points", self.projected_fantasy_points)
        stats = _stats(self.raw_projected_stats)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "identity_provider", identity_provider)
        object.__setattr__(self, "provider_player_id", player_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "nfl_team_id", team)
        object.__setattr__(self, "opponent_team_id", opponent)
        object.__setattr__(self, "projected_fantasy_points", points)
        object.__setattr__(self, "raw_projected_stats", stats)

    def to_record(self) -> dict[str, object]:
        return {
            "source_artifact_id": self.source_artifact_id,
            "provider": self.provider,
            "identity_provider": self.identity_provider,
            "provider_player_id": self.provider_player_id,
            "display_name": self.display_name,
            "position": self.position,
            "nfl_team_id": self.nfl_team_id,
            "opponent_team_id": self.opponent_team_id,
            "is_home": self.is_home,
            "is_bye": self.is_bye,
            "projected_fantasy_points": self.projected_fantasy_points,
            "raw_projected_stats": dict(self.raw_projected_stats),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionArchiveRow":
        exact_fields(record, _ROW_FIELDS, "projection archive row")
        raw_stats = record["raw_projected_stats"]
        if not isinstance(raw_stats, Mapping):
            raise ValueError("projection archive raw_projected_stats must be an object")
        return cls(
            record["source_artifact_id"], record["provider"],
            record["identity_provider"], record["provider_player_id"],
            record["display_name"], record["position"], record["nfl_team_id"],
            record["opponent_team_id"], record["is_home"], record["is_bye"],
            record["projected_fantasy_points"], tuple(raw_stats.items()),
        )


@dataclass(frozen=True, slots=True)
class ProjectionArchive:
    sources: tuple[ProjectionArchiveSource, ...]
    rows: tuple[ProjectionArchiveRow, ...]
    archive_id: str = field(init=False)

    def __post_init__(self) -> None:
        sources = _typed_tuple("sources", self.sources, ProjectionArchiveSource, _MAX_SOURCES)
        rows = _typed_tuple("rows", self.rows, ProjectionArchiveRow, _MAX_ROWS)
        if not sources or not rows:
            raise ValueError("projection archive must contain sources and rows")
        if (
            len({source.artifact_id for source in sources}) != len(sources)
            or len({source.task_id for source in sources}) != len(sources)
        ):
            raise ValueError("projection archive repeats a source artifact or task")
        sources = tuple(sorted(sources, key=_source_key))
        source_by_id = {source.artifact_id: source for source in sources}
        row_keys = [
            (row.source_artifact_id, row.identity_provider, row.provider_player_id)
            for row in rows
        ]
        if len(set(row_keys)) != len(row_keys):
            raise ValueError("projection archive repeats a provider player row")
        for row in rows:
            source = source_by_id.get(row.source_artifact_id)
            if (
                source is None
                or source.provider != row.provider
                or _PROVIDER_IDENTITIES[row.provider] != row.identity_provider
            ):
                raise ValueError("projection archive row does not match its source")
        counts = Counter(row.source_artifact_id for row in rows)
        if any(counts[source.artifact_id] != source.row_count for source in sources):
            raise ValueError("projection archive source row count is invalid")
        rows = tuple(sorted(rows, key=_row_key))
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "archive_id", content_id("projection_archive", self._content()))

    def _content(self) -> dict[str, object]:
        return {
            "sources": [source.to_record() for source in self.sources],
            "rows": [row.to_record() for row in self.rows],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "full_projection_archive",
            "schema_version": 1,
            "schema_fingerprint": PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT,
            **self._content(),
            "archive_id": self.archive_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionArchive":
        exact_fields(record, _ARCHIVE_FIELDS, "projection archive")
        if (
            record["kind"] != "full_projection_archive"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["schema_fingerprint"] != PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT
        ):
            raise ValueError("projection archive kind, version, or fingerprint is invalid")
        if not isinstance(record["sources"], list) or not isinstance(record["rows"], list):
            raise ValueError("projection archive sources and rows must be JSON arrays")
        if len(record["sources"]) > _MAX_SOURCES or len(record["rows"]) > _MAX_ROWS:
            raise ValueError("projection archive record exceeds its item limit")
        archive = cls(
            tuple(ProjectionArchiveSource.from_record(row) for row in record["sources"]),
            tuple(ProjectionArchiveRow.from_record(row) for row in record["rows"]),
        )
        if record["archive_id"] != archive.archive_id:
            raise ValueError("projection archive content does not match archive_id")
        if record["sources"] != [row.to_record() for row in archive.sources] or record[
            "rows"
        ] != [row.to_record() for row in archive.rows]:
            raise ValueError("projection archive records are not in canonical order")
        return archive

    def summary(self) -> dict[str, object]:
        return {
            "archive_id": self.archive_id,
            "providers": sorted({source.provider for source in self.sources}),
            "seasons": sorted({source.season for source in self.sources}),
            "periods": sorted(
                {f"{source.season}-W{source.week:02d}" for source in self.sources}
            ),
            "horizons": sorted({source.horizon for source in self.sources}),
            "scoring": sorted({source.scoring for source in self.sources}),
            "positions": sorted({row.position for row in self.rows}),
            "source_count": len(self.sources),
            "segments_captured": sum(source.segments_captured for source in self.sources),
            "table_count": sum(source.table_count for source in self.sources),
            "row_count": len(self.rows),
            "projected_points_count": sum(
                row.projected_fantasy_points is not None for row in self.rows
            ),
            "stat_names": sorted(
                {name for row in self.rows for name, _ in row.raw_projected_stats}
            ),
            "captured_at_first": min(
                (source.captured_at for source in self.sources), key=_timestamp_key
            ),
            "captured_at_last": max(
                (source.captured_at for source in self.sources), key=_timestamp_key
            ),
        }

    @classmethod
    def from_artifacts(
        cls,
        artifacts: Iterable[GenericTableArtifact],
        *,
        known_registry: IdentityRegistry | None = None,
    ) -> "ProjectionArchive":
        if isinstance(artifacts, (str, bytes)):
            raise ValueError("projection artifacts must be an iterable")
        try:
            captured = tuple(artifacts)
        except TypeError:
            raise ValueError("projection artifacts must be an iterable") from None
        if (
            not captured
            or len(captured) > _MAX_SOURCES
            or any(not isinstance(row, GenericTableArtifact) for row in captured)
        ):
            raise ValueError("projection artifacts must contain GenericTableArtifact values")
        sources, rows = [], []
        for artifact in captured:
            parsed = projection_artifact_rows(artifact, known_registry=known_registry)
            source = ProjectionArchiveSource(
                artifact.artifact_id, artifact.task_id, artifact.provider.value,
                artifact.season, artifact.week, artifact.horizon.value, artifact.scoring,
                artifact.position_scope, artifact.captured_at, artifact.source_period_text,
                artifact.segments_captured, len(artifact.tables), len(parsed),
            )
            sources.append(source)
            rows.extend(
                ProjectionArchiveRow(
                    artifact.artifact_id, artifact.provider.value,
                    row.identity_provider, row.provider_player_id, row.display_name,
                    row.position, row.nfl_team_id, row.opponent_team_id, row.is_home,
                    row.is_bye, row.projected_fantasy_points, row.raw_projected_stats,
                )
                for row in parsed
            )
            if len(rows) > _MAX_ROWS:
                raise ValueError("projection archive exceeds its row limit")
        return cls(tuple(sources), tuple(rows))


def _source_key(source: ProjectionArchiveSource) -> tuple[object, ...]:
    return (
        source.season, source.week, source.horizon, source.scoring, source.provider,
        source.position_scope, source.artifact_id,
    )


def _row_key(row: ProjectionArchiveRow) -> tuple[str, ...]:
    return row.source_artifact_id, row.identity_provider, row.provider_player_id


def _timestamp_key(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _safe_text(name: str, value: object, maximum: int) -> str:
    text = require_text(name, value)
    if (
        text != text.strip()
        or any(ord(character) < 32 for character in text)
        or len(text) > maximum
        or looks_like_url(text)
    ):
        raise ValueError(f"{name} must be short and URL-free")
    return text


def _safe_identifier(name: str, value: object, *, maximum: int) -> str:
    text = _safe_text(name, value, maximum)
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"{name} contains unsafe characters")
    return text


def _pattern_text(name: str, value: object, pattern: re.Pattern[str]) -> str:
    text = _safe_text(name, value, 16)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{name} is invalid")
    return text


def _optional_number(name: str, value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite JSON number or null")
    number = float(value)
    if not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
        raise ValueError(f"{name} must be a finite portable JSON number")
    return number


def _stats(values: Iterable[tuple[str, float]]) -> tuple[tuple[str, float], ...]:
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError("raw_projected_stats must be key/value pairs")
    try:
        items = tuple(values)
    except TypeError:
        raise ValueError("raw_projected_stats must be key/value pairs") from None
    if len(items) > _MAX_STATS_PER_ROW:
        raise ValueError("raw_projected_stats exceeds its field limit")
    result = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("raw_projected_stats must be key/value pairs")
        name, value = item
        if not isinstance(name, str) or _STAT_NAME.fullmatch(name) is None:
            raise ValueError("raw projected stat name is invalid")
        if is_forbidden_capture_key(name) or _private_numeric_field(name):
            raise ValueError("raw projected stats contain a private or transport field")
        result.append((name, _optional_number(f"raw projected stat {name!r}", value)))
    if any(value is None for _, value in result):
        raise ValueError("raw projected stats must contain finite numbers")
    if len({name for name, _ in result}) != len(result):
        raise ValueError("raw_projected_stats contains duplicate names")
    return tuple(sorted((name, value) for name, value in result if value is not None))


def _private_numeric_field(value: str) -> bool:
    normalized = value.replace("_", "")
    return normalized in {
        "accountid", "leagueid", "memberid", "ownerid", "teamid", "userid"
    }


def _typed_tuple(name: str, values, expected_type: type, maximum: int):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"projection archive {name} must be an iterable")
    try:
        copied = tuple(values)
    except TypeError:
        raise ValueError(f"projection archive {name} must be an iterable") from None
    if len(copied) > maximum or any(not isinstance(row, expected_type) for row in copied):
        raise ValueError(f"projection archive {name} has invalid values")
    return copied


__all__ = (
    "PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT",
    "ProjectionArchive",
    "ProjectionArchiveRow",
    "ProjectionArchiveSource",
)
