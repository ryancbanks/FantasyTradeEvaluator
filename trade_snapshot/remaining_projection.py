"""One shared full-horizon projection contract for power and Player Lab."""

from collections.abc import Iterable
from dataclasses import dataclass
from math import fsum

from ._ensemble_math import weighted_metrics
from .ensemble import EnsembleProjection, ProviderObservation
from .projection_lineage import ProjectionLineageIndex
from .projections import ProjectionStatus, WeeklyProjectionOrigin


@dataclass(frozen=True, slots=True)
class RemainingProjectionSummary:
    """A weighted player projection over every retained active NFL week."""

    applicable_weeks: tuple[int, ...]
    provider_observations: tuple[ProviderObservation, ...]
    minimum_observed_sources: int
    projected_fantasy_points: float | None

    @property
    def observed_source_count(self) -> int:
        return sum(
            row.status is ProjectionStatus.OBSERVED
            for row in self.provider_observations
        )

    @property
    def is_complete(self) -> bool:
        return self.observed_source_count == len(self.provider_observations)

    @property
    def average_active_week(self) -> float | None:
        if self.projected_fantasy_points is None:
            return None
        return self.projected_fantasy_points / len(self.applicable_weeks)


def summarize_remaining_projection(
    projections: Iterable[EnsembleProjection],
    lineage: ProjectionLineageIndex,
    *,
    applicable_weeks: Iterable[int] | None = None,
    require_all_providers: bool = False,
) -> RemainingProjectionSummary | None:
    """Fuse provider ROS totals without confusing them with the fantasy schedule."""

    rows = tuple(projections)
    if not rows or any(not isinstance(row, EnsembleProjection) for row in rows):
        raise ValueError("projections must contain EnsembleProjection values")
    if not isinstance(lineage, ProjectionLineageIndex):
        raise ValueError("lineage must be a ProjectionLineageIndex")
    if not isinstance(require_all_providers, bool):
        raise ValueError("require_all_providers must be a boolean")
    player_ids = {row.canonical_player_id for row in rows}
    if len(player_ids) != 1:
        raise ValueError("remaining projection rows must describe one player")
    player_id = next(iter(player_ids))
    providers = tuple(
        sorted(row.provider for row in rows[0].provider_observations)
    )
    minimums = {row.minimum_observed_sources for row in rows}
    if len(minimums) != 1:
        raise ValueError("ensemble source quorum changes across weeks")
    minimum = next(iter(minimums))
    metadata = _provider_metadata(rows, providers)
    remaining = {
        provider: lineage.remaining_season_for(player_id, provider)
        for provider in providers
    }
    scope = _projection_scope(remaining.values(), applicable_weeks)
    if scope is None:
        return None

    observations = []
    for provider in providers:
        source = remaining[provider]
        identity = metadata[provider]
        if source is not None and source.status is ProjectionStatus.OBSERVED:
            observations.append(
                ProviderObservation(
                    provider,
                    source.provider_player_id,
                    ProjectionStatus.OBSERVED,
                    source.projected_fantasy_points,
                    identity.weight,
                )
            )
            continue
        direct = tuple(
            lineage.weekly.get((player_id, provider, week)) for week in scope
        )
        if all(
            row is not None
            and row.status is ProjectionStatus.OBSERVED
            and row.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED
            for row in direct
        ):
            observations.append(
                ProviderObservation(
                    provider,
                    identity.provider_player_id,
                    ProjectionStatus.OBSERVED,
                    fsum(row.projected_fantasy_points for row in direct),
                    identity.weight,
                )
            )
        else:
            observations.append(
                ProviderObservation(
                    provider,
                    identity.provider_player_id,
                    ProjectionStatus.NOT_PUBLISHED,
                    None,
                    identity.weight,
                )
            )

    observed_count = sum(
        row.status is ProjectionStatus.OBSERVED for row in observations
    )
    if require_all_providers and observed_count != len(providers):
        missing = min(
            row.provider
            for row in observations
            if row.status is not ProjectionStatus.OBSERVED
        )
        raise ValueError(
            f"full-season projection evidence is unavailable for "
            f"{player_id!r}/{missing!r}"
        )
    points = (
        weighted_metrics(observations, 0.0)[0]
        if observed_count >= minimum
        else None
    )
    return RemainingProjectionSummary(scope, tuple(observations), minimum, points)


def _provider_metadata(rows, providers):
    expected = set(providers)
    if any(
        {item.provider for item in row.provider_observations} != expected
        for row in rows
    ):
        raise ValueError("provider set changes across player weeks")
    result = {}
    for provider in providers:
        observations = tuple(
            next(
                item
                for item in row.provider_observations
                if item.provider == provider
            )
            for row in rows
        )
        first = observations[0]
        if any(
            item.provider_player_id != first.provider_player_id
            or item.weight != first.weight
            for item in observations[1:]
        ):
            raise ValueError(
                "one player/provider has inconsistent identity or weight across weeks"
            )
        result[provider] = first
    return result


def _projection_scope(remaining_rows, supplied):
    observed_scopes = {
        row.applicable_weeks
        for row in remaining_rows
        if row is not None and row.status is ProjectionStatus.OBSERVED
    }
    if supplied is None:
        if not observed_scopes:
            return None
        if len(observed_scopes) != 1:
            raise ValueError(
                "observed full-season providers must share one player-specific week scope"
            )
        return next(iter(observed_scopes))
    try:
        scope = tuple(supplied)
    except TypeError:
        raise ValueError("applicable_weeks must be an iterable") from None
    if (
        not scope
        or scope != tuple(sorted(set(scope)))
        or any(type(week) is not int or not 1 <= week <= 25 for week in scope)
    ):
        raise ValueError("applicable_weeks must be unique and increasing")
    if any(value != scope for value in observed_scopes):
        raise ValueError("full-season projection scope conflicts with the NFL schedule")
    return scope


__all__ = ("RemainingProjectionSummary", "summarize_remaining_projection")
