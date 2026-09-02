"""Public browser-capture schema without browser or filesystem dependencies."""

from collections.abc import Mapping

from ._capture_artifacts import (
    ANALYZER_RESPONSE_SCHEMA_FINGERPRINT,
    GENERIC_TABLE_SCHEMA_FINGERPRINT,
    AnalyzerResponseArtifact,
    GenericTableArtifact,
    VisibleTable,
    VisibleTableCell,
)
from ._capture_common import sanitize_capture_body
from ._capture_dimensions import AnalyzerTradeSpec, ProjectionTableSpec
from ._capture_league import (
    LEAGUE_SOURCE_SCHEMA_FINGERPRINT,
    FantasyProsLeagueArtifact,
    LeagueSource,
    LeagueSourceKind,
)
from ._capture_ecr import (
    FANTASYPROS_ECR_SCHEMA_FINGERPRINT,
    ECRRankingRow,
    FantasyProsECRArtifact,
)
from ._capture_plan import (
    CAPTURE_PLAN_SCHEMA_FINGERPRINT,
    ECR_TASK_SCHEMA_FINGERPRINT,
    PROVIDER_HOST_ALLOWLISTS,
    TASK_SCHEMA_FINGERPRINT,
    AnalyzerCapturePhase,
    CaptureKind,
    CapturePlan,
    CaptureProvider,
    CaptureTask,
    ECRCaptureMethod,
    FantasyProsECRTask,
    PageCaptureTask,
    RankingHorizon,
)
from ._capture_policy import analyzer_body_matches_phase, public_player_link


CaptureArtifact = (
    GenericTableArtifact
    | AnalyzerResponseArtifact
    | FantasyProsECRArtifact
    | FantasyProsLeagueArtifact
)


def capture_plan_to_record(plan: CapturePlan) -> dict[str, object]:
    if not isinstance(plan, CapturePlan):
        raise ValueError("plan must be a CapturePlan")
    return plan.to_record()


def capture_plan_from_record(record: Mapping[str, object]) -> CapturePlan:
    return CapturePlan.from_record(record)


def artifact_to_record(
    artifact: CaptureArtifact,
    task: CaptureTask,
) -> dict[str, object]:
    if not isinstance(
        artifact,
        (
            GenericTableArtifact,
            AnalyzerResponseArtifact,
            FantasyProsECRArtifact,
            FantasyProsLeagueArtifact,
        ),
    ):
        raise ValueError("artifact has an unsupported capture artifact type")
    validate_artifact_for_task(artifact, task)
    return artifact.to_record()


def artifact_from_record(
    record: Mapping[str, object],
    task: CaptureTask,
) -> CaptureArtifact:
    if not isinstance(record, Mapping):
        raise ValueError("artifact record must be a mapping")
    kind = record.get("kind")
    if kind == CaptureKind.VISIBLE_TABLE.value:
        artifact: CaptureArtifact = GenericTableArtifact.from_record(record)
    elif kind == CaptureKind.ANALYZER_RESPONSE.value:
        artifact = AnalyzerResponseArtifact.from_record(record)
    elif kind == CaptureKind.ECR_RANKINGS.value:
        artifact = FantasyProsECRArtifact.from_record(record)
    elif kind == CaptureKind.LEAGUE_SOURCE.value:
        artifact = FantasyProsLeagueArtifact.from_record(record)
    else:
        raise ValueError("artifact record kind is unsupported")
    validate_artifact_for_task(artifact, task)
    return artifact


def validate_artifact_for_task(
    artifact: CaptureArtifact,
    task: CaptureTask,
) -> None:
    """Prove an artifact belongs to its originating capture task."""

    expected_pair = (
        (GenericTableArtifact, PageCaptureTask),
        (AnalyzerResponseArtifact, PageCaptureTask),
        (FantasyProsECRArtifact, FantasyProsECRTask),
        (FantasyProsLeagueArtifact, PageCaptureTask),
    )
    if not any(isinstance(artifact, left) and isinstance(task, right) for left, right in expected_pair):
        raise ValueError("artifact type does not match capture task type")
    repeated = ("task_id", "provider", "season", "week", "kind")
    mismatched = [name for name in repeated if getattr(artifact, name) != getattr(task, name)]
    if isinstance(artifact, AnalyzerResponseArtifact):
        if task.kind is not CaptureKind.ANALYZER_RESPONSE:
            mismatched.append("kind")
        elif artifact.analyzer_phase != task.analyzer_phase:
            mismatched.append("analyzer_phase")
    elif isinstance(artifact, GenericTableArtifact):
        if task.kind is not CaptureKind.VISIBLE_TABLE:
            mismatched.append("kind")
        else:
            for name in ("horizon", "scoring", "position_scope"):
                if getattr(artifact, name) != getattr(task.projection, name):
                    mismatched.append(name)
    elif isinstance(artifact, FantasyProsECRArtifact):
        for name in (
            "horizon",
            "scoring",
            "position_scope",
            "capture_method",
        ):
            if getattr(artifact, name) != getattr(task, name):
                mismatched.append(name)
        if task.expert_ids and artifact.expert_ids != task.expert_ids:
            mismatched.append("expert_ids")
        if task.expert_count is not None and artifact.expert_count != task.expert_count:
            mismatched.append("expert_count")
    elif isinstance(artifact, FantasyProsLeagueArtifact):
        if task.kind is not CaptureKind.LEAGUE_SOURCE:
            mismatched.append("kind")
    if mismatched:
        fields = ", ".join(sorted(set(mismatched)))
        raise ValueError(f"artifact does not match originating capture task: {fields}")


__all__ = [
    "ANALYZER_RESPONSE_SCHEMA_FINGERPRINT",
    "CAPTURE_PLAN_SCHEMA_FINGERPRINT",
    "ECR_TASK_SCHEMA_FINGERPRINT",
    "FANTASYPROS_ECR_SCHEMA_FINGERPRINT",
    "GENERIC_TABLE_SCHEMA_FINGERPRINT",
    "LEAGUE_SOURCE_SCHEMA_FINGERPRINT",
    "PROVIDER_HOST_ALLOWLISTS",
    "TASK_SCHEMA_FINGERPRINT",
    "AnalyzerResponseArtifact",
    "AnalyzerCapturePhase",
    "AnalyzerTradeSpec",
    "CaptureArtifact",
    "CaptureKind",
    "CapturePlan",
    "CaptureProvider",
    "CaptureTask",
    "ECRCaptureMethod",
    "ECRRankingRow",
    "FantasyProsECRArtifact",
    "FantasyProsECRTask",
    "FantasyProsLeagueArtifact",
    "GenericTableArtifact",
    "PageCaptureTask",
    "LeagueSource",
    "LeagueSourceKind",
    "ProjectionTableSpec",
    "RankingHorizon",
    "VisibleTable",
    "VisibleTableCell",
    "artifact_from_record",
    "artifact_to_record",
    "analyzer_body_matches_phase",
    "capture_plan_from_record",
    "capture_plan_to_record",
    "sanitize_capture_body",
    "public_player_link",
    "validate_artifact_for_task",
]
