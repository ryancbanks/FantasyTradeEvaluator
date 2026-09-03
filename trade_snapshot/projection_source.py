"""Content-addressed provenance for normalized projection inputs."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from ._projection_source_types import (
    HostScoringCompatibility,
    ProjectionAttemptReason,
    ProjectionAttemptStatus,
    ProjectionInputBinding,
    ProjectionInputPresence,
    ProjectionPointBasis,
    ProjectionSourceAttempt,
    _aware,
    _content_id,
    _integer,
    _iso,
    _parse_time,
    _text,
    _typed,
)
from ._scenario_random import content_id
from .capture_schema import (
    CaptureProvider,
    ProjectionTableSpec,
    RankingHorizon,
)
from .identity import IdentityRegistry
from .projection_io import projection_to_record
from .projections import ProjectionStatus, RemainingSeasonProjection, WeeklyProjection

_MANIFEST_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ProjectionSource:
    """Sanitized page provenance for one raw projection-table artifact."""

    task_id: str
    artifact_id: str
    provider: CaptureProvider | str
    captured_at: datetime
    season: int
    week: int
    horizon: RankingHorizon | str
    source_scoring_format: str
    position_scope: tuple[str, ...]
    source_period_text: str
    point_basis: ProjectionPointBasis | str
    host_scoring_compatibility: HostScoringCompatibility | str
    inputs: tuple[ProjectionInputBinding, ...]

    def __post_init__(self) -> None:
        task_id = _content_id("task_id", self.task_id, "captask")
        artifact_id = _content_id("artifact_id", self.artifact_id, "captable")
        try:
            provider = CaptureProvider(self.provider)
        except (TypeError, ValueError):
            raise ValueError("projection source provider is invalid") from None
        captured_at = _aware("captured_at", self.captured_at)
        season = _integer("season", self.season, minimum=2012, maximum=9999)
        week = _integer("week", self.week, minimum=1, maximum=25)
        dimensions = ProjectionTableSpec(
            self.horizon, self.source_scoring_format, self.position_scope
        )
        try:
            point_basis = ProjectionPointBasis(self.point_basis)
            compatibility = HostScoringCompatibility(self.host_scoring_compatibility)
        except (TypeError, ValueError):
            raise ValueError("projection scoring provenance enum value is invalid") from None
        expected_compatibility = (
            HostScoringCompatibility.BASE_FORMAT_ONLY
            if point_basis is ProjectionPointBasis.PROVIDER_TOTAL
            else HostScoringCompatibility.EXACT_HOST_RULES
        )
        if compatibility is not expected_compatibility:
            raise ValueError("projection point basis and scoring compatibility conflict")
        if point_basis is ProjectionPointBasis.LOCALLY_RECOMPUTED:
            raise ValueError(
                "locally recomputed exact-host projection provenance requires a "
                "proof-bound recomputation artifact; only provider_total with "
                "base_format_only compatibility is currently supported"
            )
        source_period_text = _text("source_period_text", self.source_period_text)
        if len(source_period_text) > 512 or "//" in source_period_text:
            raise ValueError("source_period_text must be short URL-free evidence")
        inputs = _typed("inputs", self.inputs, ProjectionInputBinding)
        if not inputs:
            raise ValueError("projection source must bind at least one normalized input")
        input_ids = tuple(row.projection_input_id for row in inputs)
        player_keys = tuple(
            (row.canonical_player_id, row.provider_player_id) for row in inputs
        )
        if len(set(input_ids)) != len(input_ids) or len(set(player_keys)) != len(player_keys):
            raise ValueError("projection source contains duplicate normalized inputs")
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "week", week)
        object.__setattr__(self, "horizon", dimensions.horizon)
        object.__setattr__(self, "source_scoring_format", dimensions.scoring)
        object.__setattr__(self, "position_scope", dimensions.position_scope)
        object.__setattr__(self, "source_period_text", source_period_text)
        object.__setattr__(self, "point_basis", point_basis)
        object.__setattr__(self, "host_scoring_compatibility", compatibility)
        object.__setattr__(
            self,
            "inputs",
            tuple(sorted(inputs, key=lambda row: row.projection_input_id)),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "captured_at": _iso(self.captured_at),
            "horizon": self.horizon.value,
            "host_scoring_compatibility": self.host_scoring_compatibility.value,
            "inputs": [row.to_record() for row in self.inputs],
            "point_basis": self.point_basis.value,
            "position_scope": list(self.position_scope),
            "provider": self.provider.value,
            "source_scoring_format": self.source_scoring_format,
            "season": self.season,
            "source_period_text": self.source_period_text,
            "task_id": self.task_id,
            "week": self.week,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionSource":
        fields = {
            "artifact_id",
            "captured_at",
            "horizon",
            "host_scoring_compatibility",
            "inputs",
            "point_basis",
            "position_scope",
            "provider",
            "source_scoring_format",
            "season",
            "source_period_text",
            "task_id",
            "week",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("projection source fields are invalid")
        inputs = record["inputs"]
        positions = record["position_scope"]
        if not isinstance(inputs, list) or not isinstance(positions, list):
            raise ValueError("projection source arrays are invalid")
        return cls(
            task_id=record["task_id"],
            artifact_id=record["artifact_id"],
            provider=record["provider"],
            captured_at=_parse_time("captured_at", record["captured_at"]),
            season=record["season"],
            week=record["week"],
            horizon=record["horizon"],
            source_scoring_format=record["source_scoring_format"],
            position_scope=tuple(positions),
            source_period_text=record["source_period_text"],
            point_basis=record["point_basis"],
            host_scoring_compatibility=record["host_scoring_compatibility"],
            inputs=tuple(ProjectionInputBinding.from_record(row) for row in inputs),
        )


@dataclass(frozen=True, slots=True)
class ProjectionSourceManifest:
    """Exact raw-artifact lineage for every normalized projection input in a bundle."""

    evaluation_scoring_profile_id: str
    sources: tuple[ProjectionSource, ...]
    attempts: tuple[ProjectionSourceAttempt, ...]
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        scoring_profile_id = _text(
            "evaluation_scoring_profile_id", self.evaluation_scoring_profile_id
        )
        sources = _typed("sources", self.sources, ProjectionSource)
        attempts = _typed("attempts", self.attempts, ProjectionSourceAttempt)
        if not sources:
            raise ValueError("projection source manifest must contain sources")
        if not attempts:
            raise ValueError("projection source manifest must contain attempts")
        artifact_ids = tuple(row.artifact_id for row in sources)
        task_ids = tuple(row.task_id for row in sources)
        input_ids = tuple(
            item.projection_input_id for source in sources for item in source.inputs
        )
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("projection source manifest repeats an artifact")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("projection source manifest repeats a capture task")
        if len(set(input_ids)) != len(input_ids):
            raise ValueError("projection source manifest binds one input more than once")
        attempt_tasks = tuple(row.task_id for row in attempts)
        captured_artifacts = tuple(
            row.artifact_id
            for row in attempts
            if row.status is ProjectionAttemptStatus.CAPTURED
        )
        if len(set(attempt_tasks)) != len(attempt_tasks):
            raise ValueError("projection source manifest repeats a requested task")
        if len(set(captured_artifacts)) != len(captured_artifacts):
            raise ValueError("projection source manifest repeats a captured artifact")
        attempts_by_artifact = {
            row.artifact_id: row
            for row in attempts
            if row.status is ProjectionAttemptStatus.CAPTURED
        }
        for source in sources:
            attempt = attempts_by_artifact.get(source.artifact_id)
            if attempt is None or not _attempt_matches_source(attempt, source):
                raise ValueError(
                    "projection source lacks its matching captured task attempt"
                )
        sources = tuple(sorted(sources, key=lambda row: row.artifact_id))
        attempts = tuple(sorted(attempts, key=lambda row: row.task_id))
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(
            self, "evaluation_scoring_profile_id", scoring_profile_id
        )
        object.__setattr__(
            self,
            "manifest_id",
            content_id("projection-source-manifest", self._content_record()),
        )

    @classmethod
    def from_artifacts(
        cls,
        artifacts: Iterable[object],
        projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
        *,
        attempts: Iterable[ProjectionSourceAttempt] | None = None,
        identities: IdentityRegistry | None = None,
    ) -> "ProjectionSourceManifest":
        from ._projection_source_build import projection_source_manifest_from_artifacts

        return projection_source_manifest_from_artifacts(
            artifacts,
            projection_evidence,
            attempts=attempts,
            identities=identities,
        )

    def validate_projection_evidence(
        self,
        projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    ) -> None:
        evidence = _projection_rows(projection_evidence)
        if any(
            row.scoring_profile_id != self.evaluation_scoring_profile_id
            for row in evidence
        ):
            raise ValueError(
                "projection source manifest evaluation scoring does not match evidence"
            )
        by_id = {projection_input_id(row): row for row in evidence}
        if len(by_id) != len(evidence):
            raise ValueError("projection evidence repeats an exact normalized input")
        bound = {
            item.projection_input_id: (source, item)
            for source in self.sources
            for item in source.inputs
        }
        if set(bound) != set(by_id):
            raise ValueError(
                "projection source manifest does not exactly cover normalized evidence"
            )
        for input_id, row in by_id.items():
            source, binding = bound[input_id]
            if (
                binding.canonical_player_id != row.canonical_player_id
                or binding.provider_player_id != row.provider_player_id
                or not _source_matches_projection(source, row)
            ):
                raise ValueError(
                    "projection source manifest metadata does not match normalized evidence"
                )
            if (
                binding.presence
                is ProjectionInputPresence.OMITTED_FROM_COMPLETE_CAPTURE
                and row.status is not ProjectionStatus.NOT_PUBLISHED
            ):
                raise ValueError(
                    "a complete-capture omission must remain explicitly not published"
                )

    def _content_record(self) -> dict[str, object]:
        return {
            "evaluation_scoring_profile_id": self.evaluation_scoring_profile_id,
            "attempts": [row.to_record() for row in self.attempts],
            "sources": [row.to_record() for row in self.sources],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "projection_source_manifest",
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            **self._content_record(),
            "manifest_id": self.manifest_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ProjectionSourceManifest":
        fields = {
            "attempts",
            "evaluation_scoring_profile_id",
            "kind",
            "manifest_id",
            "schema_version",
            "sources",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("projection source manifest fields are invalid")
        if record["kind"] != "projection_source_manifest":
            raise ValueError("projection source manifest kind is invalid")
        if record["schema_version"] != _MANIFEST_SCHEMA_VERSION:
            raise ValueError("projection source manifest schema version is unsupported")
        sources = record["sources"]
        attempts = record["attempts"]
        if not isinstance(sources, list) or not isinstance(attempts, list):
            raise ValueError("projection source manifest arrays are invalid")
        manifest = cls(
            evaluation_scoring_profile_id=record["evaluation_scoring_profile_id"],
            sources=tuple(ProjectionSource.from_record(row) for row in sources),
            attempts=tuple(ProjectionSourceAttempt.from_record(row) for row in attempts),
        )
        if record["manifest_id"] != manifest.manifest_id:
            raise ValueError("projection source manifest content does not match manifest_id")
        return manifest


def projection_input_id(
    row: WeeklyProjection | RemainingSeasonProjection,
) -> str:
    if not isinstance(row, (WeeklyProjection, RemainingSeasonProjection)):
        raise ValueError("projection input must be a normalized projection")
    return content_id("projection-input", projection_to_record(row))


def _attempt_matches_source(attempt, source):
    return (
        attempt.task_id == source.task_id
        and attempt.artifact_id == source.artifact_id
        and attempt.provider is source.provider
        and attempt.season == source.season
        and attempt.week == source.week
        and attempt.horizon is source.horizon
        and attempt.scoring == source.source_scoring_format
        and attempt.position_scope == source.position_scope
        and attempt.attempted_at == source.captured_at
    )


def _source_matches_projection(source, row):
    return (
        source.provider.value == row.provider
        and source.season == row.season
        and source.captured_at == row.captured_at
        and source.horizon is _projection_horizon(row)
        and (source.horizon is RankingHorizon.ROS or source.week == row.week)
    )


def _projection_horizon(row):
    return (
        RankingHorizon.WEEKLY
        if isinstance(row, WeeklyProjection)
        else RankingHorizon.ROS
    )


def _projection_rows(rows):
    result = tuple(rows)
    if not result or any(
        not isinstance(row, (WeeklyProjection, RemainingSeasonProjection))
        or row.canonical_player_id is None
        for row in result
    ):
        raise ValueError(
            "projection evidence must contain resolved normalized projections"
        )
    return result


__all__ = (
    "HostScoringCompatibility",
    "ProjectionAttemptReason",
    "ProjectionAttemptStatus",
    "ProjectionInputBinding",
    "ProjectionInputPresence",
    "ProjectionPointBasis",
    "ProjectionSource",
    "ProjectionSourceAttempt",
    "ProjectionSourceManifest",
    "projection_input_id",
)
