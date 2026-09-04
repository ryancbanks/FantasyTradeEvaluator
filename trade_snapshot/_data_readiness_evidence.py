"""Summaries of retained source, schedule, and status evidence."""

from collections import defaultdict

from ._bundle_evidence import retained_source_times
from ._data_readiness_policy import (
    _CONFIGURED_CORRELATION_LIMITATION,
    _INDEPENDENT_CORRELATION_LIMITATION,
)
from ._data_readiness_time import timestamp_text as _timestamp
from .nfl_schedule import NflTeamWeekStatus
from .projection_source import (
    HostScoringCompatibility,
    ProjectionAttemptStatus,
    ProjectionPointBasis,
)
from .projections import ProjectionStatus


def _source_capture_times(bundle):
    return retained_source_times(bundle)


def _projection_source_coverage(bundle):
    manifest = bundle.projection_source_manifest
    if manifest is None:
        evidence_counts = defaultdict(int)
        for row in bundle.projection_evidence:
            evidence_counts[row.provider] += 1
        providers = {
            provider: {
                "source_count": 0,
                "captured_attempts": 0,
                "not_published_attempts": 0,
                "unavailable_attempts": 0,
                "point_bases": [],
                "source_scoring_formats": [],
                "host_scoring_compatibilities": [],
                "normalized_evidence_rows": row_count,
            }
            for provider, row_count in evidence_counts.items()
        }
        return {
            "evidence_basis": "retained_normalized_projection_evidence",
            "manifest_id": None,
            "evaluation_scoring_profile_id": None,
            "source_count": 0,
            "captured_attempts": 0,
            "not_published_attempts": 0,
            "unavailable_attempts": 0,
            "provider_total_sources": 0,
            "locally_recomputed_sources": 0,
            "base_format_only_sources": 0,
            "exact_host_rules_sources": 0,
            "source_scoring_formats": [],
            "providers": dict(sorted(providers.items())),
        }
    provider_names = {
        row.provider.value for row in (*manifest.sources, *manifest.attempts)
    }
    providers = {}
    for provider in sorted(provider_names):
        sources = tuple(
            row for row in manifest.sources if row.provider.value == provider
        )
        attempts = tuple(
            row for row in manifest.attempts if row.provider.value == provider
        )
        providers[provider] = {
            "source_count": len(sources),
            "captured_attempts": sum(
                row.status is ProjectionAttemptStatus.CAPTURED for row in attempts
            ),
            "not_published_attempts": sum(
                row.status is ProjectionAttemptStatus.NOT_PUBLISHED
                for row in attempts
            ),
            "unavailable_attempts": sum(
                row.status is ProjectionAttemptStatus.UNAVAILABLE for row in attempts
            ),
            "point_bases": sorted({row.point_basis.value for row in sources}),
            "source_scoring_formats": sorted(
                {row.source_scoring_format for row in sources}
            ),
            "host_scoring_compatibilities": sorted(
                {row.host_scoring_compatibility.value for row in sources}
            ),
        }
    sources = manifest.sources
    attempts = manifest.attempts
    return {
        "evidence_basis": "projection_source_manifest",
        "manifest_id": manifest.manifest_id,
        "evaluation_scoring_profile_id": manifest.evaluation_scoring_profile_id,
        "source_count": len(sources),
        "captured_attempts": sum(
            row.status is ProjectionAttemptStatus.CAPTURED for row in attempts
        ),
        "not_published_attempts": sum(
            row.status is ProjectionAttemptStatus.NOT_PUBLISHED for row in attempts
        ),
        "unavailable_attempts": sum(
            row.status is ProjectionAttemptStatus.UNAVAILABLE for row in attempts
        ),
        "provider_total_sources": sum(
            row.point_basis is ProjectionPointBasis.PROVIDER_TOTAL for row in sources
        ),
        "locally_recomputed_sources": sum(
            row.point_basis is ProjectionPointBasis.LOCALLY_RECOMPUTED
            for row in sources
        ),
        "base_format_only_sources": sum(
            row.host_scoring_compatibility
            is HostScoringCompatibility.BASE_FORMAT_ONLY
            for row in sources
        ),
        "exact_host_rules_sources": sum(
            row.host_scoring_compatibility
            is HostScoringCompatibility.EXACT_HOST_RULES
            for row in sources
        ),
        "source_scoring_formats": sorted(
            {row.source_scoring_format for row in sources}
        ),
        "providers": providers,
    }


def _provider_status_coverage(rows):
    retained = {
        (
            row.canonical_player_id,
            row.provider,
            observation,
        )
        for row in rows
        for observation in row.provider_status_observations
    }
    by_scope = defaultdict(lambda: defaultdict(set))
    by_provider = defaultdict(int)
    players = set()
    for player_id, provider, observation in retained:
        players.add(player_id)
        by_provider[provider] += 1
        scope = (
            player_id,
            observation.source_scope.value,
            observation.source_week,
        )
        by_scope[scope][provider].add(observation.designation.casefold())
    disagreements = sum(
        len({tuple(sorted(labels)) for labels in providers.values()}) > 1
        for providers in by_scope.values()
        if len(providers) > 1
    )
    observed_times = tuple(observation.captured_at for _, _, observation in retained)
    return {
        "observation_count": len(retained),
        "player_count": len(players),
        "disagreement_scope_count": disagreements,
        "latest_observed_at": (
            _timestamp(max(observed_times)) if observed_times else None
        ),
        "by_provider": dict(sorted(by_provider.items())),
        "interpretation": "observation_only_not_appearance_probability",
    }


def _covers_scheduled_playoff_weeks(row, nfl_team_id, bundle, playoff_weeks):
    if bundle.nfl_schedule is None:
        team_weeks = {
            item.week: item
            for item in bundle.projections
            if item.nfl_team_id == nfl_team_id and item.week in playoff_weeks
        }
        if set(team_weeks) != playoff_weeks:
            return False
        scheduled = {
            week
            for week, item in team_weeks.items()
            if item.status is not ProjectionStatus.BYE
        }
        return scheduled.issubset(row.applicable_weeks)
    team_weeks = {
        item.week: item
        for item in bundle.nfl_schedule.team_weeks
        if item.nfl_team_id == nfl_team_id and item.week in playoff_weeks
    }
    if set(team_weeks) != playoff_weeks:
        return False
    scheduled = {
        week
        for week, item in team_weeks.items()
        if item.status is NflTeamWeekStatus.SCHEDULED
    }
    return scheduled.issubset(row.applicable_weeks)


def _correlation_limitation(independent_loadings):
    return (
        _INDEPENDENT_CORRELATION_LIMITATION
        if independent_loadings
        else _CONFIGURED_CORRELATION_LIMITATION
    )


def _first_week_games(bundle):
    games = {}
    if bundle.nfl_schedule is None:
        for row in bundle.projections:
            if (
                row.week == bundle.state.first_remaining_week
                and row.status is ProjectionStatus.OBSERVED
                and row.nfl_game_id is not None
            ):
                games.setdefault(row.nfl_game_id, row)
        return tuple(games.values())
    for row in bundle.nfl_schedule.team_weeks:
        if (
            row.week == bundle.state.first_remaining_week
            and row.status is NflTeamWeekStatus.SCHEDULED
        ):
            games.setdefault(row.nfl_game_id, row)
    return tuple(games.values())
