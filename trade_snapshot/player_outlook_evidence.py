"""Projection-evidence indexing and provenance for Player Lab read models."""

from collections import defaultdict
from datetime import datetime, timezone
from math import fsum, isclose

from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjection,
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
_PROVIDER_ORDER = {name: index for index, name in enumerate(_PROVIDER_LABELS)}


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
            "label": _PROVIDER_LABELS.get(
                provider, provider.replace("_", " ").title()
            ),
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


def _provider_names(projections):
    providers = {
        observation.provider
        for rows in projections.values()
        for row in rows
        for observation in row.provider_observations
    }
    return tuple(sorted(providers, key=_provider_sort_key))


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


def _weekly_provenance(row):
    return (
        row.origin.value,
        _iso_utc(row.captured_at),
        _optional_time(row.source_published_at),
    )


def _require_observation_match(row, observation):
    if not _observation_matches(row, observation):
        raise ValueError(
            "ensemble provider value conflicts with captured weekly evidence"
        )


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


def _provider_sort_key(provider):
    return (_PROVIDER_ORDER.get(provider, len(_PROVIDER_ORDER)), provider.casefold())


def _optional_time(value):
    return None if value is None else _iso_utc(value)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
