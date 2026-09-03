"""Deterministic player-level projection and ECR outlooks for the local app."""

from collections import defaultdict
from datetime import datetime, timezone
from math import fsum
import json

from .ecr import EcrPeriod
from .engine_bundle import EngineBundle
from .nfl_schedule import NflTeamWeekStatus
from .projection_lineage import ProjectionLineageIndex
from .remaining_projection import summarize_remaining_projection
from .projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    WeeklyProjectionOrigin,
)


_SCHEMA_VERSION = 5
_PROVIDER_LABELS = {
    "fantasypros": "FantasyPros",
    "espn": "ESPN",
    "yahoo": "Yahoo",
}
_PROVIDER_ORDER = {name: index for index, name in enumerate(_PROVIDER_LABELS)}
_ECR_ORDER = {EcrPeriod.WEEKLY: 0, EcrPeriod.REST_OF_SEASON: 1}
_WAIVER_SCOPE_NOTICE = (
    "Available players are limited to this bundle's bounded waiver pool, not the "
    "host platform's complete free-agent list."
)
_PROVIDER_STATUS_POLICY = (
    "Provider injury/status designations are timestamped source observations only. "
    "They are not converted into certain availability or appearance probabilities. "
    "Disagreement compares only providers that reported a designation; explicit "
    "coverage fields identify missing labels, so incomplete coverage is not an "
    "agreement claim."
)


def build_player_outlook(bundle: EngineBundle) -> dict[str, object]:
    """Build the complete local Player Lab contract for one immutable bundle."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    weeks = bundle.state.remaining_regular_season_weeks
    projections = _projection_groups(bundle, weeks)
    providers = _provider_names(projections)
    evidence = ProjectionLineageIndex(
        bundle.projections,
        bundle.projection_evidence,
    )
    owners = _owners(bundle)
    eligibilities = {
        row.canonical_player_id: row.eligible_slots for row in bundle.eligibilities
    }
    waiver_players = {
        row.canonical_player_id: row for row in bundle.waiver_pool.players
    }
    ecr_by_period = _ecr_rankings(bundle)
    players = [
        _player_record(
            bundle,
            player_id,
            projections[player_id],
            providers,
            evidence,
            owners,
            eligibilities,
            waiver_players,
            ecr_by_period,
        )
        for player_id in sorted(
            projections,
            key=lambda value: (bundle.player_names[value].casefold(), value),
        )
    ]
    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "snapshot_id": bundle.state.snapshot_id,
        "season": bundle.state.season,
        "first_remaining_week": bundle.state.first_remaining_week,
        "weeks": list(weeks),
        "providers": [_provider_metadata(evidence, provider) for provider in providers],
        "raw_stat_key_fields": ["provider", "stat_name"],
        "provider_status_observation_policy": _PROVIDER_STATUS_POLICY,
        "ecr_snapshots": _ecr_snapshot_records(bundle),
        "waiver_scope_notice": _WAIVER_SCOPE_NOTICE,
        "players": players,
    }
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AssertionError("player outlook must contain strict JSON data") from error
    return result


def _projection_groups(bundle, weeks):
    groups = defaultdict(list)
    for row in bundle.projections:
        groups[row.canonical_player_id].append(row)
    if not groups:
        raise ValueError("bundle contains no calculation players")
    expected = set(weeks)
    for player_id, rows in groups.items():
        if len(rows) != len(expected) or {row.week for row in rows} != expected:
            raise ValueError(f"player {player_id!r} does not cover every remaining week")
        positions = {row.position for row in rows}
        if len(positions) != 1:
            raise ValueError(f"player {player_id!r} has inconsistent positions")
        nfl_teams = {row.nfl_team_id for row in rows if row.nfl_team_id is not None}
        if len(nfl_teams) > 1:
            raise ValueError(f"player {player_id!r} has inconsistent NFL teams")
        rows.sort(key=lambda row: row.week)
    return groups


def _provider_names(projections):
    providers = {
        observation.provider
        for rows in projections.values()
        for row in rows
        for observation in row.provider_observations
    }
    return tuple(sorted(providers, key=_provider_sort_key))


def _owners(bundle):
    team_names = {row.team_id: row.name for row in bundle.state.teams}
    result = {}
    for roster in bundle.rosters:
        for player_id in roster.player_ids:
            if player_id in result:
                raise ValueError("a calculation player has multiple fantasy owners")
            result[player_id] = {
                "team_id": roster.team_id,
                "team_name": team_names[roster.team_id],
            }
    return result


def _ecr_rankings(bundle):
    result = {}
    for snapshot in bundle.ecr_snapshots:
        if snapshot.period in result:
            raise ValueError("bundle contains duplicate ECR periods")
        result[snapshot.period] = {
            row.canonical_player_id: row for row in snapshot.rankings
        }
    return result


def _player_record(
    bundle,
    player_id,
    rows,
    providers,
    evidence,
    owners,
    eligibilities,
    waiver_players,
    ecr_by_period,
):
    position = rows[0].position
    eligible_slots = eligibilities.get(player_id)
    if eligible_slots is None or position not in eligible_slots:
        raise ValueError(f"player {player_id!r} position conflicts with eligibility")
    nfl_team_id = _player_nfl_team(player_id, rows, evidence, waiver_players)
    owner = owners.get(player_id)
    waiver = waiver_players.get(player_id)
    if (owner is None) == (waiver is None):
        raise ValueError("calculation player must be rostered or in the waiver pool")
    if waiver is not None and waiver.position != position:
        raise ValueError(f"player {player_id!r} has inconsistent positions")
    weekly_records = [_week_record(row, providers, evidence) for row in rows]
    projected = [
        row.projected_fantasy_points
        for row in rows
        if row.projected_fantasy_points is not None
    ]
    remaining = summarize_remaining_projection(
        rows,
        evidence,
        applicable_weeks=tuple(
            row.week
            for row in bundle.nfl_schedule.team_weeks
            if row.nfl_team_id == nfl_team_id
            and row.week >= bundle.state.first_remaining_week
            and row.status is NflTeamWeekStatus.SCHEDULED
        ),
    )
    remaining_points = (
        None if remaining is None else remaining.projected_fantasy_points
    )
    regular_season_points = fsum(projected)
    disagreements = [
        row.between_provider_stddev
        for row in rows
        if row.between_provider_stddev is not None
    ]
    uncertainties = [
        row.predictive_stddev
        for row in rows
        if row.predictive_stddev is not None
    ]
    return {
        "player_id": player_id,
        "name": bundle.player_names[player_id],
        "position": position,
        "eligible_slots": list(eligible_slots),
        "nfl_team_id": nfl_team_id,
        "owner": owner,
        "availability": "rostered" if owner is not None else "waiver_pool",
        "weekly_ecr": _ecr_detail(ecr_by_period, EcrPeriod.WEEKLY, player_id),
        "rest_of_season_ecr": _ecr_detail(
            ecr_by_period, EcrPeriod.REST_OF_SEASON, player_id
        ),
        "remaining_projected_points": remaining_points,
        "remaining_projected_week_count": (
            None if remaining is None else len(remaining.applicable_weeks)
        ),
        "remaining_projection_status": _remaining_status(remaining),
        "remaining_fantasy_regular_season_points": regular_season_points,
        "unmaterialized_remaining_points": (
            None
            if remaining_points is None
            else remaining_points - regular_season_points
        ),
        "average_weekly_points": (
            None if remaining is None else remaining.average_active_week
        ),
        "average_fantasy_regular_season_points": _average(projected),
        "average_provider_disagreement": _average(disagreements),
        "average_predictive_uncertainty": _average(uncertainties),
        "provider_complete_week_count": sum(
            bool(providers) and row["usable_source_count"] == len(providers)
            for row in weekly_records
        ),
        "all_direct_week_count": sum(
            bool(providers) and row["direct_source_count"] == len(providers)
            for row in weekly_records
        ),
        "provider_status_disagreement_week_count": sum(
            row["provider_status_disagreement"] for row in weekly_records
        ),
        "provider_status_coverage_complete_week_count": sum(
            row["provider_status_coverage_complete"] for row in weekly_records
        ),
        "provider_status_unknown_provider_week_count": sum(
            row["provider_status_unknown_provider_count"] for row in weekly_records
        ),
        "total_week_count": len(rows),
        "weeks": weekly_records,
        "provider_remaining_season": _remaining_records(
            evidence, player_id, providers, remaining
        ),
    }


def _player_nfl_team(player_id, rows, evidence, waiver_players):
    values = [row.nfl_team_id for row in rows if row.nfl_team_id is not None]
    values.extend(
        row.nfl_team_id
        for (raw_player_id, _, _), row in evidence.weekly.items()
        if raw_player_id == player_id and row.nfl_team_id is not None
    )
    waiver = waiver_players.get(player_id)
    if waiver is not None:
        values.append(waiver.nfl_team_id)
    if len({value.casefold() for value in values}) > 1:
        raise ValueError(f"player {player_id!r} has inconsistent NFL teams")
    return values[0] if values else None


def _week_record(projection, providers, evidence):
    by_provider = {
        row.provider: row for row in projection.provider_observations
    }
    values = [
        _provider_value(evidence, projection, by_provider[provider])
        for provider in providers
        if provider in by_provider
    ]
    by_value_provider = {row["provider"]: row for row in values}
    values = [
        by_value_provider.get(provider, _not_retained_weekly_record(provider))
        for provider in providers
    ]
    source_counts = _source_counts(values)
    return {
        "week": projection.week,
        "status": projection.status.value,
        "opponent_team_id": projection.opponent_team_id,
        "is_home": projection.is_home,
        "projected_points": projection.projected_fantasy_points,
        "between_provider_stddev": projection.between_provider_stddev,
        "predictive_stddev": projection.predictive_stddev,
        "observed_source_count": projection.observed_source_count,
        "minimum_observed_sources": projection.minimum_observed_sources,
        **source_counts,
        **_provider_status_coverage(values, len(providers)),
        "provider_values": values,
    }


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
            key=lambda row: (_ECR_ORDER.get(row.period, len(_ECR_ORDER)), row.period.value),
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
    rows = evidence.provider_rows(provider)
    captures = tuple(row.captured_at for row in rows)
    published = tuple(
        row.source_published_at
        for row in rows
        if row.source_published_at is not None
    )
    return {
        "provider": provider,
        "label": _PROVIDER_LABELS.get(provider, provider.replace("_", " ").title()),
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
        "source_published_at": (
            _iso_utc(max(published)) if all(published) else None
        ),
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


def _provider_sort_key(provider):
    return (_PROVIDER_ORDER.get(provider, len(_PROVIDER_ORDER)), provider.casefold())


def _optional_time(value):
    return None if value is None else _iso_utc(value)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = ("build_player_outlook",)
