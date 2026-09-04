"""Build durable projection provenance from successful raw table captures."""

from .capture_normalize import projection_artifact_rows
from .capture_schema import GenericTableArtifact, RankingHorizon
from .identity import IdentityRegistry
from .positions import projection_position_in_scope
from ._projection_parse import projection_identity_provider
from .projection_source import (
    HostScoringCompatibility,
    ProjectionAttemptReason,
    ProjectionAttemptStatus,
    ProjectionInputBinding,
    ProjectionInputPresence,
    ProjectionPointBasis,
    ProjectionSource,
    ProjectionSourceAttempt,
    ProjectionSourceManifest,
    _parse_time,
    _projection_horizon,
    _projection_rows,
    _typed,
    projection_input_id,
)


def projection_source_manifest_from_artifacts(
    artifacts,
    projection_evidence,
    *,
    attempts=None,
    identities=None,
):
    raw = _typed("projection artifacts", artifacts, GenericTableArtifact)
    evidence = _projection_rows(projection_evidence)
    if not raw:
        raise ValueError("projection artifacts must not be empty")
    if identities is not None and not isinstance(identities, IdentityRegistry):
        raise ValueError("identities must be an IdentityRegistry or None")
    identities_by_canonical = (
        {}
        if identities is None
        else {row.canonical_player_id: row for row in identities.players}
    )
    artifact_players = {
        artifact.artifact_id: frozenset(
            row.provider_player_id for row in projection_artifact_rows(artifact)
        )
        for artifact in raw
    }
    bindings = {artifact.artifact_id: [] for artifact in raw}
    for row in evidence:
        direct_matches = tuple(
            artifact
            for artifact in raw
            if _artifact_matches_projection(
                artifact,
                artifact_players[artifact.artifact_id],
                row,
            )
        )
        if direct_matches:
            matches = direct_matches
            presence = ProjectionInputPresence.SOURCE_ROW
        else:
            matches = tuple(
                artifact
                for artifact in raw
                if _artifact_matches_omission(
                    artifact, row, identities_by_canonical
                )
            )
            presence = ProjectionInputPresence.OMITTED_FROM_COMPLETE_CAPTURE
        if len(matches) != 1:
            raise ValueError(
                "normalized projection input must map to exactly one raw artifact"
            )
        bindings[matches[0].artifact_id].append(
            ProjectionInputBinding(
                row.canonical_player_id,
                row.provider_player_id,
                projection_input_id(row),
                presence,
            )
        )
    attempt_rows = (
        tuple(_attempt_from_artifact(artifact) for artifact in raw)
        if attempts is None
        else _typed("projection source attempts", attempts, ProjectionSourceAttempt)
    )
    captured_ids = {
        row.artifact_id
        for row in attempt_rows
        if row.status is ProjectionAttemptStatus.CAPTURED
    }
    if captured_ids != {artifact.artifact_id for artifact in raw}:
        raise ValueError("captured projection attempts must exactly cover raw artifacts")
    manifest = ProjectionSourceManifest(
        evaluation_scoring_profile_id=_evaluation_scoring_profile_id(evidence),
        sources=tuple(
            _source_from_artifact(artifact, tuple(bindings[artifact.artifact_id]))
            for artifact in raw
            if bindings[artifact.artifact_id]
        ),
        attempts=attempt_rows,
    )
    manifest.validate_projection_evidence(evidence)
    return manifest


def _source_from_artifact(artifact, inputs):
    return ProjectionSource(
        task_id=artifact.task_id,
        artifact_id=artifact.artifact_id,
        provider=artifact.provider,
        captured_at=_parse_time("captured_at", artifact.captured_at),
        season=artifact.season,
        week=artifact.week,
        horizon=artifact.horizon,
        source_scoring_format=artifact.scoring,
        position_scope=artifact.position_scope,
        source_period_text=artifact.source_period_text,
        point_basis=ProjectionPointBasis.PROVIDER_TOTAL,
        host_scoring_compatibility=HostScoringCompatibility.BASE_FORMAT_ONLY,
        inputs=inputs,
    )


def _attempt_from_artifact(artifact):
    return ProjectionSourceAttempt(
        task_id=artifact.task_id,
        provider=artifact.provider,
        season=artifact.season,
        week=artifact.week,
        horizon=artifact.horizon,
        scoring=artifact.scoring,
        position_scope=artifact.position_scope,
        attempted_at=_parse_time("captured_at", artifact.captured_at),
        status=ProjectionAttemptStatus.CAPTURED,
        reason_code=ProjectionAttemptReason.CAPTURED,
        artifact_id=artifact.artifact_id,
    )


def _artifact_matches_projection(artifact, provider_player_ids, row):
    return (
        _artifact_dimensions_match(artifact, row)
        and row.provider_player_id in provider_player_ids
    )


def _artifact_matches_omission(artifact, row, identities_by_canonical):
    if (
        not identities_by_canonical
        or not artifact.complete
        or not _artifact_dimensions_match(artifact, row)
    ):
        return False
    identity = identities_by_canonical.get(row.canonical_player_id)
    if identity is None or not projection_position_in_scope(
        identity.position, artifact.position_scope
    ):
        return False
    reference_provider = projection_identity_provider(artifact.provider)
    references = tuple(
        reference.provider_player_id
        for reference in identity.provider_references
        if reference.provider == reference_provider
    )
    return len(references) == 1 and references[0] == row.provider_player_id


def _artifact_dimensions_match(artifact, row):
    return (
        artifact.provider.value == row.provider
        and artifact.season == row.season
        and _parse_time("captured_at", artifact.captured_at) == row.captured_at
        and artifact.horizon is _projection_horizon(row)
        and (artifact.horizon is RankingHorizon.ROS or artifact.week == row.week)
    )


def _evaluation_scoring_profile_id(evidence):
    values = {row.scoring_profile_id for row in evidence}
    if len(values) != 1:
        raise ValueError("normalized projection inputs use conflicting scoring profiles")
    return values.pop()


__all__ = ("projection_source_manifest_from_artifacts",)
