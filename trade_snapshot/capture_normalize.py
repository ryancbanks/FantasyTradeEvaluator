"""Convert sanitized weekly browser artifacts into local calculation records."""

from datetime import datetime, timezone
import re

from .capture_schema import FantasyProsECRArtifact, RankingHorizon
from .ecr import EcrPeriod, EcrPlayerRanking, EcrSnapshot
from .identity import IdentityRegistry
from .positions import normalize_player_position
from .identity_match import ProviderPlayerRecord
from ._projection_normalize import (
    ProjectionArtifactRow,
    projection_artifact_rows,
    projection_evidence_from_artifact,
    projection_provider_records,
)


__all__ = (
    "ecr_provider_records",
    "ecr_snapshot_from_artifact",
    "ProjectionArtifactRow",
    "projection_artifact_rows",
    "projection_evidence_from_artifact",
    "projection_provider_records",
)


_POSITION_RANK = re.compile(
    r"^\s*(D\s*/\s*ST|DST|DEF|[A-Z]{1,4})\s*#?\s*([1-9][0-9]*)\s*$",
    re.IGNORECASE,
)


def ecr_provider_records(
    artifact: FantasyProsECRArtifact,
    *,
    provider: str = "fantasypros",
) -> tuple[ProviderPlayerRecord, ...]:
    """Return identity evidence from an ECR artifact without fuzzy matching."""

    _artifact(artifact)
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    return tuple(
        ProviderPlayerRecord(
            provider=provider.strip(),
            provider_player_id=row.provider_player_id,
            display_name=row.player_name,
            position=row.position,
            nfl_team_id=row.nfl_team_id or "FA",
        )
        for row in artifact.rankings
    )


def ecr_snapshot_from_artifact(
    artifact: FantasyProsECRArtifact,
    registry: IdentityRegistry,
    *,
    snapshot_id: str,
    scoring_profile_id: str,
    identity_provider: str = "fantasypros",
) -> EcrSnapshot:
    """Build exact ECR evidence; every included provider row must be resolved."""

    _artifact(artifact)
    if not isinstance(registry, IdentityRegistry):
        raise ValueError("registry must be an IdentityRegistry")
    rankings = []
    for row in artifact.rankings:
        identity = registry.lookup(identity_provider, row.provider_player_id)
        if identity is None:
            raise ValueError(
                "ECR artifact contains an unresolved FantasyPros player "
                f"{row.provider_player_id!r}"
            )
        if identity.position != _position(row.position):
            raise ValueError(
                f"ECR position changed for resolved player {row.provider_player_id!r}"
            )
        rankings.append(
            EcrPlayerRanking(
                canonical_player_id=identity.canonical_player_id,
                fantasypros_player_id=row.provider_player_id,
                position=_position(row.position),
                rank_ecr=_integer_rank("rank_ecr", row.rank_ecr),
                position_rank=_position_rank(row.position_rank, _position(row.position)),
                rank_min=_integer_rank("rank_min", row.rank_min),
                rank_max=_integer_rank("rank_max", row.rank_max),
                rank_average=_required_rank("rank_avg", row.rank_avg),
                rank_stddev=_required_rank("rank_std", row.rank_std, allow_zero=True),
            )
        )
    period = (
        EcrPeriod.WEEKLY
        if artifact.horizon is RankingHorizon.WEEKLY
        else EcrPeriod.REST_OF_SEASON
    )
    return EcrSnapshot(
        snapshot_id=snapshot_id,
        scoring_profile_id=scoring_profile_id,
        season=artifact.season,
        as_of_week=artifact.week,
        period=period,
        captured_at=_time("captured_at", artifact.captured_at),
        source_updated_at=(
            None
            if artifact.last_updated_at is None
            else _time("last_updated_at", artifact.last_updated_at)
        ),
        expert_ids=artifact.expert_ids,
        total_experts=artifact.expert_count,
        rankings=tuple(rankings),
    )


def _artifact(value: object) -> FantasyProsECRArtifact:
    if not isinstance(value, FantasyProsECRArtifact):
        raise ValueError("artifact must be a FantasyProsECRArtifact")
    return value


def _position(value: str) -> str:
    return normalize_player_position(value)


def _position_rank(value: object, expected_position: str) -> int:
    if not isinstance(value, str):
        raise ValueError("position_rank must contain a position and positive rank")
    match = _POSITION_RANK.fullmatch(value)
    if match is None:
        raise ValueError("position_rank must contain a position and positive rank")
    if _position(re.sub(r"\s+", "", match.group(1))) != expected_position:
        raise ValueError("position_rank position does not match the ECR row position")
    return int(match.group(2))


def _integer_rank(name: str, value: object) -> int:
    number = _required_rank(name, value)
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer rank")
    return int(number)


def _required_rank(name: str, value: object, *, allow_zero: bool = False) -> float:
    if value is None:
        raise ValueError(f"ECR artifact is missing required {name}")
    number = float(value)
    minimum = 0 if allow_zero else 1
    if number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


def _time(name: str, value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)
