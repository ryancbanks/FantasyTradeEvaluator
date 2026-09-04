"""Deterministic player-level projection and ECR outlooks for the local app."""

from collections import defaultdict
from collections.abc import Mapping
from math import fsum

from .ecr import EcrPeriod
from .engine_bundle import EngineBundle
from .nfl_schedule import NflTeamWeekStatus
from .player_profile_outlook import (
    PROFILE_SCOPE_NOTICE,
    assign_player_ranks,
    outside_calculation_record,
    profile_record,
)
from .player_outlook_evidence import (
    _EvidenceIndex,
    _iso_utc,
    _provider_names,
)
from .player_outlook_projection_records import (
    _average,
    _ecr_detail,
    _ecr_snapshot_records,
    _not_retained_weekly_record,
    _provider_metadata,
    _provider_status_coverage,
    _provider_value,
    _remaining_records,
    _remaining_status,
    _source_counts,
)
from .player_outlook_views import (
    _catalog_player_record,
    _require_strict_json,
    build_player_outlook_catalog,
    select_player_outlook_detail,
)
from .projection_lineage import ProjectionLineageIndex
from .remaining_projection import summarize_remaining_projection
from .projections import ProjectionStatus


_SCHEMA_VERSION = 5
_WAIVER_SCOPE_NOTICE = (
    "Available players are limited to this bundle's bounded waiver pool, not the "
    "host platform's complete free-agent list."
)
_PROJECTION_CATALOG_NOTICE = (
    "Captured projections outside the bounded trade pool remain available in "
    "Player Lab. Public history and depth enrichment were unavailable for this bundle."
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
    scoring_mode = _outlook_scoring_mode(bundle.scoring_profile.settings)
    weeks = bundle.state.remaining_regular_season_weeks
    projections = _projection_groups(bundle.projections, weeks)
    lab_snapshot = bundle.player_lab_projections
    lab_projections = _projection_groups(
        () if lab_snapshot is None else lab_snapshot.projections,
        weeks,
        require_nonempty=False,
        require_complete=False,
    )
    lab_player_ids = () if lab_snapshot is None else lab_snapshot.player_ids
    providers = _provider_names({**projections, **lab_projections})
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
    calculation_players = [
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
    profiles = bundle.player_profiles
    by_player = {row["player_id"]: row for row in calculation_players}
    for player_id in lab_player_ids:
        retained = lab_projections.get(player_id, ())
        by_player[player_id] = _outside_projected_record(
            player_id,
            lab_snapshot.player_names[player_id],
            lab_snapshot.player_positions[player_id],
            lab_snapshot.player_nfl_team_ids[player_id],
            (lab_snapshot.player_positions[player_id],),
            retained,
            weeks,
            providers,
            lab_snapshot.provider_provenance_by_name,
            _ecr_detail(ecr_by_period, EcrPeriod.WEEKLY, player_id),
            _ecr_detail(ecr_by_period, EcrPeriod.REST_OF_SEASON, player_id),
        )
    if profiles is None:
        players = sorted(
            ({**row, "profile": None} for row in by_player.values()),
            key=lambda row: (row["name"].casefold(), row["player_id"]),
        )
        profile_metadata = None
        scope_notice = (
            _PROJECTION_CATALOG_NOTICE if lab_player_ids else _WAIVER_SCOPE_NOTICE
        )
    else:
        for profile in profiles.players:
            row = by_player.get(profile.canonical_player_id)
            if row is None:
                weekly_ecr = _ecr_detail(
                    ecr_by_period, EcrPeriod.WEEKLY, profile.canonical_player_id
                )
                rest_of_season_ecr = _ecr_detail(
                    ecr_by_period,
                    EcrPeriod.REST_OF_SEASON,
                    profile.canonical_player_id,
                )
                retained = lab_projections.get(profile.canonical_player_id)
                row = (
                    outside_calculation_record(
                        profile, weekly_ecr, rest_of_season_ecr
                    )
                    if retained is None
                    else _outside_projected_record(
                        profile.canonical_player_id,
                        profile.display_name,
                        profile.position,
                        profile.nfl_team_id,
                        profile.fantasy_positions,
                        retained,
                        weeks,
                        providers,
                        lab_snapshot.provider_provenance_by_name,
                        weekly_ecr,
                        rest_of_season_ecr,
                    )
                )
                by_player[profile.canonical_player_id] = row
            elif profile.canonical_player_id in lab_player_ids:
                row["name"] = profile.display_name
                row["eligible_slots"] = list(profile.fantasy_positions)
            row["profile"] = profile_record(profile, profiles, scoring_mode)
        for row in by_player.values():
            row.setdefault("profile", None)
        players = sorted(
            by_player.values(),
            key=lambda row: (row["name"].casefold(), row["player_id"]),
        )
        profile_metadata = _profile_snapshot_record(profiles, lab_player_ids)
        scope_notice = PROFILE_SCOPE_NOTICE
    assign_player_ranks(players)
    result = _outlook_header(
        bundle,
        scoring_mode,
        weeks,
        providers,
        evidence,
        profiles,
        lab_player_ids,
        scope_notice,
        profile_metadata,
    )
    result["players"] = players
    _require_strict_json(result, "player outlook")
    return result


def _profile_snapshot_record(profiles, lab_projections):
    return {
        "profile_snapshot_id": profiles.profile_snapshot_id,
        "captured_at": _iso_utc(profiles.captured_at),
        "source_data_id": profiles.source_data_id,
        "current_stats_availability": profiles.current_stats_availability,
        "previous_stats_availability": profiles.previous_stats_availability,
        "player_count": len(profiles.players),
        "projected_outside_calculation_count": len(lab_projections),
        "injury_history_availability": [
            row.to_record() for row in profiles.injury_history_availability
        ],
        "provenance": [row.to_record() for row in profiles.provenance],
        "materialization_issues": [
            row.to_record() for row in profiles.materialization_issues
        ],
    }


def _outlook_header(
    bundle,
    scoring_mode,
    weeks,
    providers,
    evidence,
    profiles,
    lab_projections,
    scope_notice,
    profile_metadata,
):
    return {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "snapshot_id": bundle.state.snapshot_id,
        "season": bundle.state.season,
        "scoring_mode": scoring_mode,
        "first_remaining_week": bundle.state.first_remaining_week,
        "weeks": list(weeks),
        "providers": [_provider_metadata(evidence, provider) for provider in providers],
        "raw_stat_key_fields": ["provider", "stat_name"],
        "provider_status_observation_policy": _PROVIDER_STATUS_POLICY,
        "ecr_snapshots": _ecr_snapshot_records(bundle),
        "waiver_scope_notice": scope_notice,
        "profile_scope": (
            "captured_public_catalog"
            if profiles is not None
            else "captured_projection_catalog"
            if lab_projections
            else "calculation_pool_only"
        ),
        "profile_snapshot": profile_metadata,
    }


def _outlook_scoring_mode(settings):
    scoring_settings = settings.get("scoring_settings")
    rank_type = (
        scoring_settings.get("playerRankType")
        if isinstance(scoring_settings, Mapping)
        else None
    )
    mode = {
        "STANDARD": "STD",
        "HALF_PPR": "HALF",
        "PPR": "PPR",
    }.get(rank_type)
    if mode is not None:
        return mode
    receptions = settings.get("reception")
    if receptions == 0:
        return "STD"
    if receptions == 0.5:
        return "HALF"
    if receptions == 1:
        return "PPR"
    return "UNKNOWN"


def _projection_groups(
    rows, weeks, *, require_nonempty=True, require_complete=True
):
    groups = defaultdict(list)
    for row in rows:
        groups[row.canonical_player_id].append(row)
    if require_nonempty and not groups:
        raise ValueError("bundle contains no calculation players")
    expected = set(weeks)
    for player_id, rows in groups.items():
        row_weeks = tuple(row.week for row in rows)
        if len(set(row_weeks)) != len(row_weeks) or not set(row_weeks) <= expected:
            raise ValueError(f"player {player_id!r} has invalid remaining weeks")
        if require_complete and set(row_weeks) != expected:
            raise ValueError(f"player {player_id!r} does not cover every remaining week")
        positions = {row.position for row in rows}
        if len(positions) != 1:
            raise ValueError(f"player {player_id!r} has inconsistent positions")
        nfl_teams = {row.nfl_team_id for row in rows if row.nfl_team_id is not None}
        if len(nfl_teams) > 1:
            raise ValueError(f"player {player_id!r} has inconsistent NFL teams")
        rows.sort(key=lambda row: row.week)
    return groups


def _outside_projected_record(
    player_id,
    name,
    position,
    nfl_team_id,
    eligible_slots,
    rows,
    remaining_weeks,
    providers,
    provider_provenance,
    weekly_ecr,
    rest_of_season_ecr,
):
    by_week = {row.week: row for row in rows}
    weekly_records = [
        _retained_ensemble_week(by_week[week], providers, provider_provenance)
        if week in by_week
        else _retained_missing_week(week, providers, provider_provenance)
        for week in remaining_weeks
    ]
    points = [
        row.projected_fantasy_points
        for row in rows
        if row.projected_fantasy_points is not None
    ]
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
    complete = len(rows) == len(remaining_weeks)
    remaining_points = fsum(points) if complete else None
    return {
        "player_id": player_id,
        "name": name,
        "position": position or rows[0].position,
        "eligible_slots": list(eligible_slots),
        "nfl_team_id": nfl_team_id or rows[0].nfl_team_id,
        "owner": None,
        "availability": "outside_calculation_pool",
        "weekly_ecr": weekly_ecr,
        "rest_of_season_ecr": rest_of_season_ecr,
        "remaining_projected_points": remaining_points,
        "remaining_projected_week_count": (
            sum(row.status is not ProjectionStatus.BYE for row in rows)
            if complete
            else None
        ),
        "remaining_projection_status": "complete" if complete else "partial",
        "remaining_fantasy_regular_season_points": remaining_points,
        "unmaterialized_remaining_points": 0.0 if complete else None,
        "average_weekly_points": (
            _average(points) if complete else None
        ),
        "average_fantasy_regular_season_points": (
            _average(points) if complete else None
        ),
        "average_provider_disagreement": (
            _average(disagreements) if complete else None
        ),
        "average_predictive_uncertainty": (
            _average(uncertainties) if complete else None
        ),
        "provider_complete_week_count": sum(
            row["usable_source_count"] == len(providers) for row in weekly_records
        ),
        "all_direct_week_count": 0,
        "provider_status_disagreement_week_count": 0,
        "provider_status_coverage_complete_week_count": 0,
        "provider_status_unknown_provider_week_count": len(remaining_weeks) * len(providers),
        "total_week_count": len(remaining_weeks),
        "weeks": weekly_records,
        "provider_remaining_season": _retained_provider_totals(
            rows, remaining_weeks, providers, provider_provenance
        ),
    }


def _retained_ensemble_week(projection, providers, provider_provenance):
    by_provider = {
        row.provider: {
            "provider": row.provider,
            "provider_player_id": row.provider_player_id,
            "status": row.status.value,
            "projected_points": row.projected_fantasy_points,
            "weight": row.weight,
            "origin": None,
            "captured_at": _provider_capture(
                provider_provenance, row.provider, "captured_at"
            ),
            "source_published_at": _provider_capture(
                provider_provenance, row.provider, "source_published_at"
            ),
            "raw_projected_stats": {},
            "provider_status_observations": [],
        }
        for row in projection.provider_observations
    }
    values = [by_provider[provider] for provider in providers]
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
        **_source_counts(values),
        "provider_values": values,
    }


def _retained_missing_week(week, providers, provider_provenance):
    values = [
        {
            "provider": provider,
            "provider_player_id": None,
            "status": "not_retained",
            "projected_points": None,
            "weight": None,
            "origin": None,
            "captured_at": _provider_capture(
                provider_provenance, provider, "captured_at"
            ),
            "source_published_at": _provider_capture(
                provider_provenance, provider, "source_published_at"
            ),
            "raw_projected_stats": {},
            "provider_status_observations": [],
        }
        for provider in providers
    ]
    return {
        "week": week,
        "status": "insufficient_sources",
        "opponent_team_id": None,
        "is_home": None,
        "projected_points": None,
        "between_provider_stddev": None,
        "predictive_stddev": None,
        "observed_source_count": None,
        "minimum_observed_sources": None,
        **_source_counts(values),
        "provider_values": values,
    }


def _retained_provider_totals(
    rows, remaining_weeks, providers, provider_provenance
):
    result = []
    for provider in providers:
        observations = [
            next(
                value
                for value in row.provider_observations
                if value.provider == provider
            )
            for row in rows
        ]
        scheduled = [
            (row, observation)
            for row, observation in zip(rows, observations)
            if row.status is not ProjectionStatus.BYE
        ]
        applicable = bool(scheduled) or len(rows) < len(remaining_weeks)
        complete = all(
            value.status is ProjectionStatus.OBSERVED for _, value in scheduled
        ) and len(rows) == len(remaining_weeks)
        provider_ids = {value.provider_player_id for value in observations}
        result.append(
            {
                "provider": provider,
                "provider_player_id": (
                    next(iter(provider_ids)) if len(provider_ids) == 1 else None
                ),
                "status": (
                    ProjectionStatus.NOT_APPLICABLE.value
                    if not applicable
                    else
                    ProjectionStatus.OBSERVED.value
                    if complete
                    else ProjectionStatus.NOT_PUBLISHED.value
                ),
                "projected_points": (
                    fsum(value.projected_fantasy_points for _, value in scheduled)
                    if applicable and complete
                    else None
                ),
                "origin": "derived_weekly" if applicable and complete else None,
                "applicable_weeks": [row.week for row, _ in scheduled],
                "captured_at": _provider_capture(
                    provider_provenance, provider, "captured_at"
                ),
                "source_published_at": _provider_capture(
                    provider_provenance, provider, "source_published_at"
                ),
                "raw_projected_stats": {},
                "provider_status_observations": [],
            }
        )
    return result


def _provider_capture(provenance, provider, field):
    row = provenance.get(provider)
    value = None if row is None else getattr(row, field)
    return None if value is None else _iso_utc(value)


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
    # ``player_outlook_lazy`` retains the earlier evidence-only read context.
    # Bind that context to the exact fused rows before producing audited detail;
    # eager callers already pass the bundle-wide lineage index.
    if not isinstance(evidence, ProjectionLineageIndex):
        evidence = ProjectionLineageIndex(
            rows,
            (
                row
                for row in bundle.projection_evidence
                if row.canonical_player_id == player_id
            ),
        )
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
        applicable_weeks=(
            tuple(row.week for row in rows if row.status is not ProjectionStatus.BYE)
            if bundle.nfl_schedule is None
            else tuple(
                row.week
                for row in bundle.nfl_schedule.team_weeks
                if row.nfl_team_id == nfl_team_id
                and row.week >= bundle.state.first_remaining_week
                and row.status is NflTeamWeekStatus.SCHEDULED
            )
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
        "owner": None if owner is None else dict(owner),
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


def _week_record(*args):
    if len(args) == 3:
        projection, providers, evidence = args
        values_for = lambda observation: _provider_value(  # noqa: E731
            evidence, projection, observation
        )
        include_status_coverage = True
    elif len(args) == 5:
        # Compatibility for the bounded catalog builder. Exact-player detail
        # rebinds this legacy context to ``ProjectionLineageIndex`` above.
        player_id, projection, providers, evidence, player_weeks = args
        values_for = lambda observation: evidence.provider_value(  # noqa: E731
            player_id, projection, observation, player_weeks
        )
        include_status_coverage = False
    else:
        raise TypeError("_week_record expects 3 or 5 arguments")
    by_provider = {
        row.provider: row for row in projection.provider_observations
    }
    values = [
        values_for(by_provider[provider])
        for provider in providers
        if provider in by_provider
    ]
    by_value_provider = {row["provider"]: row for row in values}
    values = [
        by_value_provider.get(provider, _not_retained_weekly_record(provider))
        for provider in providers
    ]
    source_counts = _source_counts(values)
    result = {
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
        "provider_values": values,
    }
    if include_status_coverage:
        result.update(_provider_status_coverage(values, len(providers)))
    return result


__all__ = (
    "build_player_outlook",
    "build_player_outlook_catalog",
    "select_player_outlook_detail",
)
