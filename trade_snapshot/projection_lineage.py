"""Prove how fused provider values trace back to captured projection evidence."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import fsum
from types import MappingProxyType

from .ensemble import EnsembleProjection, ProviderObservation
from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)


@dataclass(frozen=True, slots=True)
class ProjectionLineage:
    """The captured source and timestamps supporting one fused provider cell.

    ``origin`` is absent for schedule-derived availability states because this
    index describes projection publication, not schedule provenance. The
    portable bundle validates those states against its separately retained NFL
    schedule. Observed numeric values always require a weekly or ROS origin.
    """

    origin: WeeklyProjectionOrigin | None
    captured_at: datetime
    source_published_at: datetime | None


class ProjectionLineageIndex:
    """Index source rows and fail closed when fused observed values are unproven."""

    def __init__(
        self,
        projections: Iterable[EnsembleProjection],
        evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    ) -> None:
        projection_rows = tuple(projections)
        evidence_rows = tuple(evidence)
        if not projection_rows or any(
            not isinstance(row, EnsembleProjection) for row in projection_rows
        ):
            raise ValueError("projections must contain EnsembleProjection values")
        if not evidence_rows or any(
            not isinstance(row, (WeeklyProjection, RemainingSeasonProjection))
            for row in evidence_rows
        ):
            raise ValueError("projection evidence contains an unsupported row")

        weekly, remaining, weekly_by_pair, rows_by_provider, provider_ids = (
            _index_evidence(evidence_rows)
        )
        player_rows = _index_projections(projection_rows)
        lineages = {}
        observations = {}
        for projection in projection_rows:
            for observation in projection.provider_observations:
                key = (
                    projection.canonical_player_id,
                    observation.provider,
                    projection.week,
                )
                if key in lineages:
                    raise ValueError("projections contain a duplicate provider cell")
                pair = key[:2]
                if provider_ids.get(pair) != observation.provider_player_id:
                    raise ValueError(
                        "ensemble provider identity lacks matching projection evidence"
                    )
                lineages[key] = _classify(
                    projection,
                    observation,
                    player_rows[projection.canonical_player_id],
                    weekly,
                    remaining,
                    weekly_by_pair,
                )
                observations[key] = observation

        self.weekly: Mapping[tuple[str, str, int], WeeklyProjection] = (
            MappingProxyType(weekly)
        )
        self.remaining: Mapping[
            tuple[str, str], RemainingSeasonProjection
        ] = MappingProxyType(remaining)
        self._rows_by_provider = MappingProxyType(
            {key: tuple(rows) for key, rows in rows_by_provider.items()}
        )
        weekly_nfl_teams_by_player = defaultdict(list)
        for (player_id, _, _), row in weekly.items():
            if row.nfl_team_id is not None:
                weekly_nfl_teams_by_player[player_id].append(row.nfl_team_id)
        self._weekly_nfl_teams_by_player = MappingProxyType(
            {
                player_id: tuple(values)
                for player_id, values in weekly_nfl_teams_by_player.items()
            }
        )
        self._lineages = MappingProxyType(lineages)
        self._observations = MappingProxyType(observations)

    def lineage_for(
        self,
        projection: EnsembleProjection,
        observation: ProviderObservation,
    ) -> ProjectionLineage:
        key = (
            projection.canonical_player_id,
            observation.provider,
            projection.week,
        )
        if self._observations.get(key) != observation:
            raise ValueError("provider observation is not part of this lineage index")
        return self._lineages[key]

    def provider_rows(
        self, provider: str
    ) -> tuple[WeeklyProjection | RemainingSeasonProjection, ...]:
        return self._rows_by_provider.get(provider, ())

    def remaining_season_for(
        self, canonical_player_id: str, provider: str
    ) -> RemainingSeasonProjection | None:
        """Return the typed full-horizon ROS evidence for one provider pair."""

        return self.remaining.get((canonical_player_id, provider))

    def remaining_season_rows(self) -> tuple[RemainingSeasonProjection, ...]:
        return tuple(self.remaining.values())


def _index_evidence(rows):
    weekly = {}
    remaining = {}
    weekly_by_pair = defaultdict(list)
    rows_by_provider = defaultdict(list)
    provider_ids = {}
    for row in rows:
        rows_by_provider[row.provider].append(row)
        player_id = row.canonical_player_id
        if player_id is None:
            continue
        pair = (player_id, row.provider)
        known_id = provider_ids.setdefault(pair, row.provider_player_id)
        if known_id != row.provider_player_id:
            raise ValueError("one player/provider has conflicting provider IDs")
        if isinstance(row, WeeklyProjection):
            key = (*pair, row.week)
            if key in weekly:
                raise ValueError("projection evidence contains duplicate weekly evidence")
            weekly[key] = row
            weekly_by_pair[pair].append(row)
        else:
            if pair in remaining:
                raise ValueError(
                    "projection evidence contains duplicate remaining-season evidence"
                )
            remaining[pair] = row
    return weekly, remaining, weekly_by_pair, rows_by_provider, provider_ids


def _index_projections(rows):
    by_player = defaultdict(list)
    seen = set()
    for row in rows:
        key = row.canonical_player_id, row.week
        if key in seen:
            raise ValueError("projections contains a duplicate player/week")
        seen.add(key)
        by_player[row.canonical_player_id].append(row)
    return {
        player_id: tuple(sorted(values, key=lambda row: row.week))
        for player_id, values in by_player.items()
    }


def _classify(
    projection,
    observation,
    player_rows,
    weekly,
    remaining,
    weekly_by_pair,
):
    pair = projection.canonical_player_id, observation.provider
    raw = weekly.get((*pair, projection.week))
    direct = (
        raw
        if raw is not None
        and raw.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED
        else None
    )
    if projection.status is ProjectionStatus.BYE:
        return _classify_bye(pair, observation, direct, remaining, weekly_by_pair)
    if direct is not None and direct.status is ProjectionStatus.BYE:
        raise ValueError("provider evidence conflicts with the ensemble schedule status")
    if direct is not None and direct.status is not ProjectionStatus.NOT_PUBLISHED:
        _require_observation_match(direct, observation)
        return _lineage_from_weekly(direct)

    derived = _derived_lineage(
        pair,
        projection.week,
        observation,
        player_rows,
        weekly,
        remaining,
        weekly_by_pair,
    )
    if derived is not None:
        if (
            raw is not None
            and raw.origin is WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON
            and not _observation_matches(raw, observation)
        ):
            raise ValueError("derived weekly evidence conflicts with the fused value")
        return derived
    if direct is not None:
        _require_observation_match(direct, observation)
        return _lineage_from_weekly(direct)
    if raw is not None:
        raise ValueError("derived weekly evidence lacks matching ROS support")
    if observation.status is not ProjectionStatus.NOT_PUBLISHED:
        raise ValueError("ensemble provider value lacks matching projection evidence")
    captures = _capture_times(pair, remaining, weekly_by_pair)
    if not captures:
        raise ValueError("unpublished provider value lacks capture evidence")
    return ProjectionLineage(
        None,
        max(captures),
        None,
    )


def _classify_bye(pair, observation, direct, remaining, weekly_by_pair):
    if observation.status is not ProjectionStatus.BYE:
        raise ValueError("ensemble bye has a non-bye provider value")
    if direct is not None and direct.status is ProjectionStatus.BYE:
        return _lineage_from_weekly(direct)
    if direct is not None and direct.status is not ProjectionStatus.NOT_PUBLISHED:
        raise ValueError("provider evidence conflicts with an ensemble bye")
    ros = remaining.get(pair)
    captures = _capture_times(pair, remaining, weekly_by_pair)
    if not captures:
        raise ValueError("derived bye lacks capture evidence")
    return ProjectionLineage(
        None,
        max(captures),
        ros.source_published_at
        if ros is not None
        else direct.source_published_at if direct is not None else None,
    )


def _derived_lineage(
    pair,
    target_week,
    observation,
    player_rows,
    weekly,
    remaining,
    weekly_by_pair,
):
    ros = remaining.get(pair)
    if ros is None or ros.status is not ProjectionStatus.OBSERVED:
        return None
    active_weeks = _active_weeks(player_rows, ros)
    published = {
        row.week: row
        for row in weekly_by_pair.get(pair, ())
        if row.week in active_weeks
        and row.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED
    }
    if any(
        row.status
        not in {ProjectionStatus.OBSERVED, ProjectionStatus.NOT_PUBLISHED}
        for row in published.values()
    ):
        return None
    missing = tuple(
        week
        for week in sorted(active_weeks)
        if week not in published
        or published[week].status is ProjectionStatus.NOT_PUBLISHED
    )
    if target_week not in missing or not missing:
        return None
    expected = (
        ros.projected_fantasy_points
        - fsum(
            row.projected_fantasy_points
            for row in published.values()
            if row.status is ProjectionStatus.OBSERVED
        )
    ) / len(missing)
    if (
        observation.status is not ProjectionStatus.OBSERVED
        or observation.projected_fantasy_points != expected
    ):
        raise ValueError("derived ensemble value does not reconcile to ROS evidence")
    return ProjectionLineage(
        WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON,
        ros.captured_at,
        ros.source_published_at,
    )


def _active_weeks(player_rows, ros):
    # Weekly assembly seals extended ROS scopes as player-active weeks. The two
    # exact output-scope forms remain accepted for bundles with no later weeks.
    output_weeks = {row.week for row in player_rows}
    output_active_weeks = {
        row.week for row in player_rows if row.status is not ProjectionStatus.BYE
    }
    declared_weeks = set(ros.applicable_weeks)
    if min(declared_weeks) < min(output_weeks):
        raise ValueError("remaining-season evidence predates the player weeks")
    if declared_weeks in (output_weeks, output_active_weeks):
        return output_active_weeks
    known_output_byes = output_weeks.difference(output_active_weeks)
    normalized_declared = declared_weeks.difference(known_output_byes)
    if output_active_weeks.issubset(normalized_declared):
        return normalized_declared
    raise ValueError("remaining-season evidence conflicts with player weeks")


def _capture_times(pair, remaining, weekly_by_pair):
    captures = [
        row.captured_at
        for row in weekly_by_pair.get(pair, ())
        if row.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED
    ]
    ros = remaining.get(pair)
    if ros is not None:
        captures.append(ros.captured_at)
    return captures


def _lineage_from_weekly(row):
    return ProjectionLineage(row.origin, row.captured_at, row.source_published_at)


def _require_observation_match(row, observation):
    if not _observation_matches(row, observation):
        raise ValueError("ensemble provider value conflicts with captured weekly evidence")


def _observation_matches(row, observation):
    return (
        row.provider_player_id == observation.provider_player_id
        and row.status is observation.status
        and row.projected_fantasy_points == observation.projected_fantasy_points
    )


__all__ = ("ProjectionLineage", "ProjectionLineageIndex")
