from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ._analyzer_types import BundleFingerprint
from .bundle_provenance import validate_analyzer_bundle_url

from ._capture_common import (
    content_id,
    freeze_json,
    looks_like_url,
    normalize_json,
    require_captured_at,
    require_content_id,
    require_json_int,
    require_text,
    sanitized_visible_link,
    schema_fingerprint,
    thaw_json,
)
from ._capture_plan import (
    AnalyzerCapturePhase,
    CaptureKind,
    CaptureProvider,
    _enum_value,
    _exact_fields,
)
from ._capture_dimensions import ProjectionTableSpec, RankingHorizon
from ._capture_policy import (
    ANALYZER_BODY_POLICY_DESCRIPTOR,
    project_analyzer_body,
    validate_public_player_links,
)


GENERIC_TABLE_SCHEMA_FINGERPRINT = schema_fingerprint(
    "generic_visible_table_artifact",
    {
        "fields": ["metadata", "tables[].caption=null", "tables[].rows[].cells[].text", "cells[].links"],
        "source_fields": [
            "horizon", "scoring", "position_scope", "source_period_text",
            "segments_captured", "complete",
        ],
        "policy_version": "projection-stats-allowlist-complete-traversal-v5",
        "link_policy": "provider-player-or-provider-dst-team-path-only-v5",
    },
)
ANALYZER_RESPONSE_SCHEMA_FINGERPRINT = schema_fingerprint(
    "fantasypros_analyzer_response_artifact",
    {
        "fields": [
            "metadata", "analyzer_phase", "bundle_url", "bundle_sha256",
            "sanitized_response_body",
        ],
        "bundle_provenance": {
            "origin": "https://cdn.fantasypros.com",
            "path": "/assets/js/min/pages/myplaybook/trade-analyzer/bundle-<hex>.js",
            "digest": "sha256_exact_public_bytes",
        },
        "phases": [phase.value for phase in AnalyzerCapturePhase],
        "persistence_policy": ANALYZER_BODY_POLICY_DESCRIPTOR,
    },
)


@dataclass(frozen=True, slots=True)
class VisibleTableCell:
    text: str
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("cell text must be a string")
        if looks_like_url(self.text) or any(
            ord(character) < 32 and character not in "\t\n\r" for character in self.text
        ):
            raise ValueError("cell text cannot contain controls or transport URLs")
        if isinstance(self.links, (str, bytes)):
            raise ValueError("cell links must be an iterable of HTTPS URLs")
        try:
            links = tuple(sanitized_visible_link(link) for link in self.links)
        except TypeError:
            raise ValueError("cell links must be an iterable of HTTPS URLs") from None
        if len(set(links)) != len(links):
            raise ValueError("cell links cannot contain duplicates")
        object.__setattr__(self, "links", links)

    def to_record(self) -> dict[str, object]:
        return {"text": self.text, "links": list(self.links)}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "VisibleTableCell":
        _exact_fields(record, {"text", "links"}, "visible table cell")
        if not isinstance(record["links"], list):
            raise ValueError("visible table cell links must be a list")
        return cls(record["text"], tuple(record["links"]))


@dataclass(frozen=True, slots=True)
class VisibleTable:
    rows: tuple[tuple[VisibleTableCell, ...], ...]
    caption: str | None = None

    def __init__(
        self,
        rows: Iterable[Iterable[VisibleTableCell]],
        caption: str | None = None,
    ):
        if caption is not None:
            raise ValueError("captured projection tables cannot persist captions")
        try:
            normalized_rows = tuple(tuple(row) for row in rows)
        except TypeError:
            raise ValueError("table rows must be an iterable of cell iterables") from None
        if not normalized_rows or any(not row for row in normalized_rows):
            raise ValueError("visible table must contain non-empty rows")
        if any(
            not isinstance(cell, VisibleTableCell)
            for row in normalized_rows
            for cell in row
        ):
            raise ValueError("visible table rows must contain VisibleTableCell values")
        object.__setattr__(self, "rows", normalized_rows)
        object.__setattr__(self, "caption", caption)

    def to_record(self) -> dict[str, object]:
        return {
            "caption": self.caption,
            "rows": [[cell.to_record() for cell in row] for row in self.rows],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "VisibleTable":
        _exact_fields(record, {"caption", "rows"}, "visible table")
        raw_rows = record["rows"]
        if not isinstance(raw_rows, list) or any(not isinstance(row, list) for row in raw_rows):
            raise ValueError("visible table rows must be lists")
        return cls(
            tuple(tuple(VisibleTableCell.from_record(cell) for cell in row) for row in raw_rows),
            record["caption"],
        )


@dataclass(frozen=True, slots=True)
class GenericTableArtifact:
    task_id: str
    provider: CaptureProvider | str
    season: int
    week: int
    kind: CaptureKind | str
    captured_at: str
    horizon: RankingHorizon | str
    scoring: str
    position_scope: tuple[str, ...]
    source_period_text: str
    segments_captured: int
    complete: bool
    tables: tuple[VisibleTable, ...]
    artifact_id: str = field(init=False)

    def __post_init__(self) -> None:
        metadata = _validate_metadata(
            self.task_id,
            self.provider,
            self.season,
            self.week,
            self.kind,
            self.captured_at,
            CaptureKind.VISIBLE_TABLE,
        )
        dimensions = ProjectionTableSpec(self.horizon, self.scoring, self.position_scope)
        source_period_text = require_text("source_period_text", self.source_period_text)
        if len(source_period_text) > 512 or looks_like_url(source_period_text):
            raise ValueError("source_period_text must be short URL-free provider evidence")
        segments = require_json_int(
            "segments_captured", self.segments_captured, minimum=1, maximum=10000
        )
        if self.complete is not True:
            raise ValueError("projection table capture must prove complete traversal")
        if isinstance(self.tables, (str, bytes)):
            raise ValueError("tables must be an iterable of VisibleTable values")
        try:
            tables = tuple(self.tables)
        except TypeError:
            raise ValueError("tables must be an iterable of VisibleTable values") from None
        if not tables or any(not isinstance(table, VisibleTable) for table in tables):
            raise ValueError("tables must contain at least one VisibleTable")
        validate_public_player_links(
            metadata[1],
            (
                link
                for table in tables
                for row in table.rows
                for cell in row
                for link in cell.links
            ),
        )
        _set_metadata(self, metadata)
        object.__setattr__(self, "horizon", dimensions.horizon)
        object.__setattr__(self, "scoring", dimensions.scoring)
        object.__setattr__(self, "position_scope", dimensions.position_scope)
        object.__setattr__(self, "source_period_text", source_period_text)
        object.__setattr__(self, "segments_captured", segments)
        object.__setattr__(self, "complete", True)
        object.__setattr__(self, "tables", tables)
        object.__setattr__(self, "artifact_id", content_id("captable", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        return {
            **_metadata_record(self),
            "horizon": self.horizon.value,
            "scoring": self.scoring,
            "position_scope": list(self.position_scope),
            "source_period_text": self.source_period_text,
            "segments_captured": self.segments_captured,
            "complete": self.complete,
            "tables": [table.to_record() for table in self.tables],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema_fingerprint": GENERIC_TABLE_SCHEMA_FINGERPRINT,
            **self._content_record(),
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "GenericTableArtifact":
        fields = {
            "tables", "horizon", "scoring", "position_scope", "source_period_text",
            "segments_captured", "complete",
        }
        _exact_fields(record, _ARTIFACT_FIELDS | fields, "generic table artifact")
        _require_artifact_header(record, GENERIC_TABLE_SCHEMA_FINGERPRINT)
        if not isinstance(record["tables"], list) or not isinstance(record["position_scope"], list):
            raise ValueError("generic table artifact tables must be a list")
        artifact = cls(
            **_metadata_arguments(record),
            horizon=record["horizon"], scoring=record["scoring"],
            position_scope=tuple(record["position_scope"]),
            source_period_text=record["source_period_text"],
            segments_captured=record["segments_captured"], complete=record["complete"],
            tables=tuple(VisibleTable.from_record(table) for table in record["tables"]),
        )
        _require_matching_id(record, artifact.artifact_id)
        return artifact


@dataclass(frozen=True, slots=True)
class AnalyzerResponseArtifact:
    task_id: str
    provider: CaptureProvider | str
    season: int
    week: int
    kind: CaptureKind | str
    captured_at: str
    analyzer_phase: AnalyzerCapturePhase | str
    bundle_url: str
    bundle_sha256: str
    body: Mapping[str, object]
    artifact_id: str = field(init=False)

    def __post_init__(self) -> None:
        metadata = _validate_metadata(
            self.task_id,
            self.provider,
            self.season,
            self.week,
            self.kind,
            self.captured_at,
            CaptureKind.ANALYZER_RESPONSE,
        )
        if metadata[1] is not CaptureProvider.FANTASYPROS:
            raise ValueError("analyzer response artifacts must use FantasyPros")
        phase = _enum_value(AnalyzerCapturePhase, "analyzer_phase", self.analyzer_phase)
        try:
            bundle = BundleFingerprint(self.bundle_url, self.bundle_sha256)
            validate_analyzer_bundle_url(bundle.url)
        except ValueError:
            raise ValueError("analyzer response bundle fingerprint is invalid") from None
        if not isinstance(self.body, Mapping):
            raise ValueError("analyzer response body must be a JSON object")
        projected = project_analyzer_body(phase, self.body)
        _set_metadata(self, metadata)
        object.__setattr__(self, "analyzer_phase", phase)
        object.__setattr__(self, "bundle_url", bundle.url)
        object.__setattr__(self, "bundle_sha256", bundle.sha256)
        object.__setattr__(self, "body", freeze_json(projected))
        object.__setattr__(self, "artifact_id", content_id("capanalyzer", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        return {
            **_metadata_record(self),
            "analyzer_phase": self.analyzer_phase.value,
            "bundle_url": self.bundle_url,
            "bundle_sha256": self.bundle_sha256,
            "body": thaw_json(self.body),
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema_fingerprint": ANALYZER_RESPONSE_SCHEMA_FINGERPRINT,
            **self._content_record(),
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "AnalyzerResponseArtifact":
        _exact_fields(
            record,
            _ARTIFACT_FIELDS | {
                "analyzer_phase", "bundle_url", "bundle_sha256", "body",
            },
            "analyzer response artifact",
        )
        _require_artifact_header(record, ANALYZER_RESPONSE_SCHEMA_FINGERPRINT)
        raw_body = normalize_json(record["body"], "analyzer response body")
        try:
            projected = project_analyzer_body(record["analyzer_phase"], raw_body)
        except ValueError:
            raise ValueError("stored analyzer response body violates the phase policy") from None
        if raw_body != projected:
            raise ValueError("stored analyzer response body contains non-allowlisted fields")
        artifact = cls(
            **_metadata_arguments(record),
            analyzer_phase=record["analyzer_phase"],
            bundle_url=record["bundle_url"],
            bundle_sha256=record["bundle_sha256"],
            body=raw_body,
        )
        _require_matching_id(record, artifact.artifact_id)
        return artifact


_ARTIFACT_FIELDS = {
    "schema_version",
    "schema_fingerprint",
    "artifact_id",
    "task_id",
    "provider",
    "season",
    "week",
    "kind",
    "captured_at",
}


def _validate_metadata(task_id, provider, season, week, kind, captured_at, expected_kind):
    task_id = require_content_id("task_id", task_id, "captask")
    provider = _enum_value(CaptureProvider, "provider", provider)
    season = require_json_int("season", season, minimum=2000, maximum=2200)
    week = require_json_int("week", week, minimum=1, maximum=25)
    kind = _enum_value(CaptureKind, "kind", kind)
    if kind is not expected_kind:
        raise ValueError(f"artifact kind must be {expected_kind.value}")
    return task_id, provider, season, week, kind, require_captured_at(captured_at)


def _set_metadata(instance, values) -> None:
    for name, value in zip(("task_id", "provider", "season", "week", "kind", "captured_at"), values):
        object.__setattr__(instance, name, value)


def _metadata_record(artifact) -> dict[str, object]:
    return {
        "task_id": artifact.task_id,
        "provider": artifact.provider.value,
        "season": artifact.season,
        "week": artifact.week,
        "kind": artifact.kind.value,
        "captured_at": artifact.captured_at,
    }


def _metadata_arguments(record: Mapping[str, object]) -> dict[str, object]:
    return {name: record[name] for name in ("task_id", "provider", "season", "week", "kind", "captured_at")}


def _require_artifact_header(record: Mapping[str, object], fingerprint: str) -> None:
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("artifact schema_version is invalid")
    if record["schema_fingerprint"] != fingerprint:
        raise ValueError("artifact schema fingerprint is invalid")


def _require_matching_id(record: Mapping[str, object], artifact_id: str) -> None:
    if record["artifact_id"] != artifact_id:
        raise ValueError("artifact content does not match artifact_id")
