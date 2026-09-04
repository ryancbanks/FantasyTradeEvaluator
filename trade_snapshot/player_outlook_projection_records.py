"""Projection and ECR record serialization for Player Lab read models."""

from math import fsum

from .ecr import EcrPeriod
from .player_outlook_evidence import _iso_utc, _optional_time
from .projection_lineage import ProjectionLineageIndex
from .projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    WeeklyProjectionOrigin,
)


_PROVIDER_LABELS = {
    "fantasypros": "FantasyPros",
    "espn": "ESPN",
    "yahoo": "Yahoo",
    "cbs": "CBS Sports",
    "fftoday": "FFToday",
    "fantasysharks": "FantasySharks",
}
_ECR_ORDER = {EcrPeriod.WEEKLY: 0, EcrPeriod.REST_OF_SEASON: 1}


def _provider_status_coverage(values, expected_provider_count):
    status_sets = {
        row["provider"]: tuple(
            sorted(
                {
                    observation["designation"].casefold()
                    for observation in row["provider_status_observations"]
                }
            )
        )
        for row in values
        if row["provider_status_observations"]
    }
    reporting_provider_count = len(status_sets)
    unknown_provider_count = expected_provider_count - reporting_provider_count
    if unknown_provider_count < 0:
        raise AssertionError("provider status coverage exceeds the provider universe")
    return {
        "provider_status_observation_count": sum(
            len(row["provider_status_observations"]) for row in values
        ),
        "provider_status_expected_provider_count": expected_provider_count,
        "provider_status_reporting_provider_count": reporting_provider_count,
        "provider_status_unknown_provider_count": unknown_provider_count,
        "provider_status_coverage_complete": unknown_provider_count == 0,
        "provider_status_disagreement": len(set(status_sets.values())) > 1,
    }


def _ecr_snapshot_records(bundle):
    return [
        {
            "period": snapshot.period.value,
            "captured_at": _iso_utc(snapshot.captured_at),
            "source_updated_at": _optional_time(snapshot.source_updated_at),
            "expert_count": snapshot.total_experts,
            "selected_expert_count": len(snapshot.expert_ids),
            "expert_population_mode": "position_specific",
            "expert_panels": [
                {
                    "position": panel.position,
                    "expert_count": panel.total_experts,
                    "expert_ids": list(panel.expert_ids),
                    "expert_selection_policy": (
                        panel.provenance.source_details.expert_selection_policy
                    ),
                    "expert_group_title": (
                        panel.provenance.source_details.expert_group_title
                    ),
                    "expert_group_description": (
                        panel.provenance.source_details.expert_group_description
                    ),
                }
                for panel in snapshot.expert_panels
            ],
            "ranking_count": len(snapshot.rankings),
        }
        for snapshot in sorted(
            bundle.ecr_snapshots,
            key=lambda row: (
                _ECR_ORDER.get(row.period, len(_ECR_ORDER)),
                row.period.value,
            ),
        )
    ]


def _ecr_detail(ecr_by_period, period, player_id):
    ranking = ecr_by_period.get(period, {}).get(player_id)
    if ranking is None:
        return None
    return {
        "rank": ranking.rank_ecr,
        "position_rank": ranking.position_rank,
        "rank_min": ranking.rank_min,
        "rank_max": ranking.rank_max,
        "rank_average": ranking.rank_average,
        "rank_stddev": ranking.rank_stddev,
    }


def _provider_metadata(evidence, provider):
    if not isinstance(evidence, ProjectionLineageIndex):
        return evidence.provider_metadata(provider)
    rows = evidence.provider_rows(provider)
    captures = tuple(row.captured_at for row in rows)
    published = tuple(
        row.source_published_at
        for row in rows
        if row.source_published_at is not None
    )
    return {
        "provider": provider,
        "label": _PROVIDER_LABELS.get(
            provider, provider.replace("_", " ").title()
        ),
        "captured_at": _iso_utc(max(captures)) if captures else None,
        "source_published_at": _iso_utc(max(published)) if published else None,
    }


def _provider_value(evidence, projection, observation):
    lineage = evidence.lineage_for(projection, observation)
    raw = evidence.weekly.get(
        (
            projection.canonical_player_id,
            observation.provider,
            projection.week,
        )
    )
    status_sources = (
        raw,
        (
            evidence.remaining_season_for(
                projection.canonical_player_id,
                observation.provider,
            )
            if lineage.origin is WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON
            else None
        ),
    )
    return {
        "provider": observation.provider,
        "provider_player_id": observation.provider_player_id,
        "status": observation.status.value,
        "projected_points": observation.projected_fantasy_points,
        "weight": observation.weight,
        "origin": None if lineage.origin is None else lineage.origin.value,
        "captured_at": _iso_utc(lineage.captured_at),
        "source_published_at": _optional_time(lineage.source_published_at),
        "raw_projected_stats": _weekly_raw_stats(raw, observation, lineage),
        "provider_status_observations": _provider_status_records(*status_sources),
    }


def _remaining_records(evidence, player_id, providers, summary):
    observations = (
        {}
        if summary is None
        else {row.provider: row for row in summary.provider_observations}
    )
    return [
        _remaining_source_record(
            evidence,
            player_id,
            provider,
            observations.get(provider),
            summary,
        )
        for provider in providers
    ]


def _remaining_source_record(evidence, player_id, provider, observation, summary):
    retained = evidence.remaining_season_for(player_id, provider)
    if retained is not None and retained.status is ProjectionStatus.OBSERVED:
        return _remaining_record(retained)
    if observation is not None and observation.status is ProjectionStatus.OBSERVED:
        rows = tuple(
            evidence.weekly.get((player_id, provider, week))
            for week in summary.applicable_weeks
        )
        if not all(
            row is not None
            and row.status is ProjectionStatus.OBSERVED
            and row.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED
            for row in rows
        ):
            raise AssertionError("full-horizon provider observation lacks source rows")
        return _weekly_sum_remaining_record(rows, observation, summary.applicable_weeks)
    if retained is not None:
        return _remaining_record(retained)
    return _not_retained_remaining_record(provider)


def _remaining_record(row):
    return {
        "provider": row.provider,
        "provider_player_id": row.provider_player_id,
        "status": row.status.value,
        "projected_points": row.projected_fantasy_points,
        "origin": row.origin.value,
        "applicable_weeks": list(row.applicable_weeks),
        "captured_at": _iso_utc(row.captured_at),
        "source_published_at": _optional_time(row.source_published_at),
        "raw_projected_stats": dict(row.raw_projected_stats),
        "provider_status_observations": _provider_status_records(row),
    }


def _weekly_sum_remaining_record(rows, observation, applicable_weeks):
    common_stats = set(rows[0].raw_projected_stats)
    for row in rows[1:]:
        common_stats.intersection_update(row.raw_projected_stats)
    published = tuple(row.source_published_at for row in rows)
    return {
        "provider": observation.provider,
        "provider_player_id": observation.provider_player_id,
        "status": observation.status.value,
        "projected_points": observation.projected_fantasy_points,
        "origin": RemainingSeasonOrigin.DERIVED_WEEKLY.value,
        "applicable_weeks": list(applicable_weeks),
        "captured_at": _iso_utc(max(row.captured_at for row in rows)),
        "source_published_at": _iso_utc(max(published)) if all(published) else None,
        "raw_projected_stats": {
            name: fsum(row.raw_projected_stats[name] for row in rows)
            for name in sorted(common_stats)
        },
        "provider_status_observations": _provider_status_records(*rows),
    }


def _not_retained_weekly_record(provider):
    return {
        "provider": provider,
        "provider_player_id": None,
        "status": "not_retained",
        "projected_points": None,
        "weight": None,
        "origin": None,
        "captured_at": None,
        "source_published_at": None,
        "raw_projected_stats": {},
        "provider_status_observations": [],
    }


def _not_retained_remaining_record(provider):
    return {
        "provider": provider,
        "provider_player_id": None,
        "status": "not_retained",
        "projected_points": None,
        "origin": None,
        "applicable_weeks": [],
        "captured_at": None,
        "source_published_at": None,
        "raw_projected_stats": {},
        "provider_status_observations": [],
    }


def _provider_status_records(*rows):
    observations = {
        observation
        for row in rows
        if row is not None
        for observation in row.provider_status_observations
    }
    return [
        {
            "designation": observation.designation,
            "captured_at": _iso_utc(observation.captured_at),
            "source_scope": observation.source_scope.value,
            "source_week": observation.source_week,
        }
        for observation in sorted(
            observations,
            key=lambda value: (
                value.captured_at,
                value.source_scope.value,
                value.source_week or 0,
                value.designation.casefold(),
                value.designation,
            ),
        )
    ]


def _weekly_raw_stats(row, observation, lineage):
    if (
        row is None
        or observation.status is not ProjectionStatus.OBSERVED
        or row.status is not ProjectionStatus.OBSERVED
        or row.origin is not lineage.origin
    ):
        return {}
    return dict(row.raw_projected_stats)


def _source_counts(values):
    usable_statuses = {ProjectionStatus.OBSERVED.value, ProjectionStatus.BYE.value}
    derived_origins = {
        WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON.value,
    }
    usable = [row for row in values if row["status"] in usable_statuses]
    direct = [
        row
        for row in usable
        if row["origin"] == WeeklyProjectionOrigin.PROVIDER_PUBLISHED.value
    ]
    derived = [row for row in usable if row["origin"] in derived_origins]
    return {
        "usable_source_count": len(usable),
        "direct_source_count": len(direct),
        "derived_source_count": len(derived),
        "unattributed_source_count": len(usable) - len(direct) - len(derived),
        "not_retained_source_count": sum(
            row["status"] == "not_retained" for row in values
        ),
    }


def _average(values):
    return fsum(values) / len(values) if values else None


def _remaining_status(summary):
    if summary is None:
        return "not_retained"
    if summary.projected_fantasy_points is None:
        return "insufficient_sources"
    return "complete" if summary.is_complete else "partial"
