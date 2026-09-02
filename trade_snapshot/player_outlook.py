"""Deterministic player-level projection and ECR outlooks for the local app."""

from collections import defaultdict
from datetime import datetime, timezone
from math import fsum, isclose
import json

from .ecr import EcrPeriod
from .engine_bundle import EngineBundle
from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)


_SCHEMA_VERSION = 1
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


class _EvidenceIndex:
    """Index captured projection rows and recover materialized-week provenance."""

    def __init__(self, rows) -> None:
        self.weekly = {}
        self.remaining = {}
        self.weekly_by_pair = defaultdict(list)
        self.rows_by_provider = defaultdict(list)
        self.provider_ids = {}
        for row in rows:
            self.rows_by_provider[row.provider].append(row)
            player_id = row.canonical_player_id
            if player_id is None:
                continue
            pair = (player_id, row.provider)
            known_id = self.provider_ids.setdefault(pair, row.provider_player_id)
            if known_id != row.provider_player_id:
                raise ValueError("one player/provider has conflicting provider IDs")
            if isinstance(row, WeeklyProjection):
                key = (*pair, row.week)
                if key in self.weekly:
                    raise ValueError("projection evidence contains duplicate weekly evidence")
                self.weekly[key] = row
                self.weekly_by_pair[pair].append(row)
            elif isinstance(row, RemainingSeasonProjection):
                if pair in self.remaining:
                    raise ValueError(
                        "projection evidence contains duplicate remaining-season evidence"
                    )
                self.remaining[pair] = row
            else:
                raise ValueError("projection evidence contains an unsupported row")

    def provider_metadata(self, provider):
        rows = self.rows_by_provider.get(provider, ())
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

    def remaining_records(self, player_id, providers):
        return [
            _remaining_record(row)
            if (row := self.remaining.get((player_id, provider))) is not None
            else _not_retained_remaining_record(provider)
            for provider in providers
        ]

    def provider_value(self, player_id, projection, observation, player_weeks):
        pair = (player_id, observation.provider)
        known_id = self.provider_ids.get(pair)
        if known_id is not None and known_id != observation.provider_player_id:
            raise ValueError("ensemble provider ID conflicts with captured evidence")
        origin, captured_at, source_published_at = self._provenance(
            pair, projection, observation, player_weeks
        )
        return {
            "provider": observation.provider,
            "provider_player_id": observation.provider_player_id,
            "status": observation.status.value,
            "projected_points": observation.projected_fantasy_points,
            "weight": observation.weight,
            "origin": origin,
            "captured_at": captured_at,
            "source_published_at": source_published_at,
        }

    def _provenance(self, pair, projection, observation, player_weeks):
        raw = self.weekly.get((*pair, projection.week))
        remaining = self.remaining.get(pair)
        if raw is not None and raw.status is not ProjectionStatus.NOT_PUBLISHED:
            _require_observation_match(raw, observation)
            return _weekly_provenance(raw)
        if projection.status is ProjectionStatus.BYE:
            return self._inferred_bye_provenance(pair, raw, remaining)
        if raw is not None and _observation_matches(raw, observation):
            return _weekly_provenance(raw)
        if observation.status is ProjectionStatus.OBSERVED and remaining is not None:
            self._validate_derived_value(pair, projection, observation, player_weeks)
            return (
                WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON.value,
                _iso_utc(remaining.captured_at),
                _optional_time(remaining.source_published_at),
            )
        if raw is None and remaining is None:
            return None, None, None
        if observation.status is ProjectionStatus.NOT_PUBLISHED:
            captures = self._pair_capture_times(pair, remaining)
            return (
                WeeklyProjectionOrigin.PROVIDER_PUBLISHED.value,
                _iso_utc(max(captures)) if captures else None,
                None,
            )
        raise ValueError("ensemble provider value conflicts with captured evidence")

    def _inferred_bye_provenance(self, pair, raw, remaining):
        if raw is not None and raw.status not in {
            ProjectionStatus.BYE,
            ProjectionStatus.NOT_PUBLISHED,
        }:
            raise ValueError("provider evidence conflicts with an ensemble bye")
        captures = self._pair_capture_times(pair, remaining)
        if not captures:
            return None, None, None
        return (
            WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON.value,
            _iso_utc(max(captures)),
            _optional_time(
                remaining.source_published_at
                if remaining is not None
                else raw.source_published_at if raw is not None else None
            ),
        )

    def _validate_derived_value(self, pair, projection, observation, player_weeks):
        remaining = self.remaining[pair]
        if remaining.status is not ProjectionStatus.OBSERVED:
            raise ValueError("observed ensemble value lacks observed source evidence")
        active_weeks = {
            row.week for row in player_weeks if row.status is not ProjectionStatus.BYE
        }
        if set(remaining.applicable_weeks) not in (
            {row.week for row in player_weeks},
            active_weeks,
        ):
            raise ValueError("remaining-season evidence conflicts with player weeks")
        published = {
            row.week: row
            for row in self.weekly_by_pair.get(pair, ())
            if row.week in active_weeks
        }
        if any(
            row.status not in {
                ProjectionStatus.OBSERVED,
                ProjectionStatus.NOT_PUBLISHED,
            }
            for row in published.values()
        ):
            raise ValueError("unsafe weekly evidence cannot be derived from ROS")
        missing = tuple(
            week
            for week in active_weeks
            if week not in published
            or published[week].status is ProjectionStatus.NOT_PUBLISHED
        )
        if projection.week not in missing or not missing:
            raise ValueError("ensemble derivation does not match captured evidence")
        observed = fsum(
            row.projected_fantasy_points
            for row in published.values()
            if row.status is ProjectionStatus.OBSERVED
        )
        expected = (remaining.projected_fantasy_points - observed) / len(missing)
        if not isclose(
            observation.projected_fantasy_points,
            expected,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("derived ensemble value does not reconcile to ROS evidence")

    def _pair_capture_times(self, pair, remaining):
        captures = [row.captured_at for row in self.weekly_by_pair.get(pair, ())]
        if remaining is not None:
            captures.append(remaining.captured_at)
        return captures


def build_player_outlook(bundle: EngineBundle) -> dict[str, object]:
    """Build the complete local Player Lab contract for one immutable bundle."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    weeks = bundle.state.remaining_regular_season_weeks
    projections = _projection_groups(bundle, weeks)
    providers = _provider_names(projections)
    evidence = _EvidenceIndex(bundle.projection_evidence)
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
        "providers": [evidence.provider_metadata(provider) for provider in providers],
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
    weekly_records = [
        _week_record(player_id, row, providers, evidence, rows) for row in rows
    ]
    projected = [
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
        "remaining_projected_points": fsum(projected),
        "average_weekly_points": _average(projected),
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
        "total_week_count": len(rows),
        "weeks": weekly_records,
        "provider_remaining_season": evidence.remaining_records(
            player_id, providers
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


def _week_record(player_id, projection, providers, evidence, player_weeks):
    by_provider = {
        row.provider: row for row in projection.provider_observations
    }
    values = [
        evidence.provider_value(
            player_id, projection, by_provider[provider], player_weeks
        )
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
        "provider_values": values,
    }


def _ecr_snapshot_records(bundle):
    return [
        {
            "period": snapshot.period.value,
            "captured_at": _iso_utc(snapshot.captured_at),
            "source_updated_at": _optional_time(snapshot.source_updated_at),
            "expert_count": snapshot.total_experts,
            "selected_expert_count": len(snapshot.expert_ids),
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
    }


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


def _weekly_provenance(row):
    return (
        row.origin.value,
        _iso_utc(row.captured_at),
        _optional_time(row.source_published_at),
    )


def _require_observation_match(row, observation):
    if not _observation_matches(row, observation):
        raise ValueError("ensemble provider value conflicts with captured weekly evidence")


def _observation_matches(row, observation):
    if row.status is not observation.status:
        return False
    if row.projected_fantasy_points is None:
        return observation.projected_fantasy_points is None
    return isclose(
        row.projected_fantasy_points,
        observation.projected_fantasy_points,
        rel_tol=0.0,
        abs_tol=1e-12,
    )


def _average(values):
    return fsum(values) / len(values) if values else None


def _provider_sort_key(provider):
    return (_PROVIDER_ORDER.get(provider, len(_PROVIDER_ORDER)), provider.casefold())


def _optional_time(value):
    return None if value is None else _iso_utc(value)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = ("build_player_outlook",)
