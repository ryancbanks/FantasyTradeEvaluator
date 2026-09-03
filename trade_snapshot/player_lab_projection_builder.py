"""Build bounded Player Lab projection snapshots from weekly source evidence."""

from collections import defaultdict
from collections.abc import Iterable, Mapping

from .ensemble import EnsembleConfig, fuse_weekly_projections
from .league_state import LeagueState
from .nfl_schedule import NflSchedule
from .player_lab_projections import (
    MAX_PLAYER_LAB_PROJECTION_PLAYERS,
    PlayerLabProjectionSnapshot,
    PlayerLabProviderProvenance,
)
from .positions import normalize_player_position
from .projection_schedule import materialize_weekly_grid
from .projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
)


def build_player_lab_projection_snapshot(
    *,
    state: LeagueState,
    projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    player_names: Mapping[str, str],
    player_positions: Mapping[str, str | None],
    player_nfl_team_ids: Mapping[str, str | None],
    nfl_schedule: NflSchedule,
    ensemble_config: EnsembleConfig,
    exclude_player_ids: Iterable[str] = (),
) -> PlayerLabProjectionSnapshot:
    """Build full-catalog projections without expanding calculation scope."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be an NflSchedule")
    if not isinstance(ensemble_config, EnsembleConfig):
        raise ValueError("ensemble_config must be an EnsembleConfig")
    if (
        not isinstance(player_names, Mapping)
        or not isinstance(player_positions, Mapping)
        or not isinstance(player_nfl_team_ids, Mapping)
    ):
        raise ValueError("player metadata must be mappings")
    excluded = _text_set("exclude_player_ids", exclude_player_ids)
    evidence = tuple(projection_evidence)
    if any(
        not isinstance(row, (WeeklyProjection, RemainingSeasonProjection))
        for row in evidence
    ):
        raise ValueError("projection_evidence contains an invalid row")
    providers = tuple(row.provider for row in ensemble_config.provider_weights)
    selected = tuple(row for row in evidence if row.provider in providers)
    missing_providers = tuple(
        provider
        for provider in providers
        if not any(row.provider == provider for row in selected)
    )
    if missing_providers:
        raise ValueError(
            f"projection evidence is missing configured provider {missing_providers[0]!r}"
        )
    captured_by_provider = {
        provider: max(row.captured_at for row in selected if row.provider == provider)
        for provider in providers
    }
    provider_provenance = tuple(
        PlayerLabProviderProvenance(
            provider,
            captured_by_provider[provider],
            max(
                (
                    row.source_published_at
                    for row in selected
                    if row.provider == provider
                    and row.source_published_at is not None
                ),
                default=None,
            ),
        )
        for provider in providers
    )
    observed_ids = {
        row.canonical_player_id
        for row in selected
        if row.canonical_player_id is not None
        and row.status is ProjectionStatus.OBSERVED
    }
    positions = {}
    teams = {}
    for player_id in sorted(observed_ids - excluded):
        position = player_positions.get(player_id)
        team = player_nfl_team_ids.get(player_id)
        if not isinstance(position, str) or not isinstance(team, str):
            continue
        try:
            normalized = normalize_player_position(position, require_supported=True)
            for week in state.remaining_regular_season_weeks:
                nfl_schedule.team_week(team, week)
        except ValueError:
            continue
        positions[player_id] = normalized
        teams[player_id] = team
    if len(positions) > MAX_PLAYER_LAB_PROJECTION_PLAYERS:
        raise ValueError("Player Lab projection player limit exceeded")
    if positions:
        complete = _complete_evidence(
            selected,
            tuple(positions),
            providers,
            captured_by_provider,
            state,
        )
        weekly = materialize_weekly_grid(
            state,
            complete,
            player_ids=tuple(positions),
            provider_names=providers,
            nfl_schedule=nfl_schedule,
            player_nfl_team_ids=teams,
        )
        projections = _complete_ensembles(weekly, positions, ensemble_config)
    else:
        projections = ()
    projected_ids = set(positions)
    return PlayerLabProjectionSnapshot(
        league_snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        season=state.season,
        as_of_week=state.first_remaining_week,
        remaining_weeks=state.remaining_regular_season_weeks,
        provider_names=providers,
        projections=projections,
        player_names=_projected_player_names(player_names, projected_ids),
        player_positions={player_id: positions[player_id] for player_id in projected_ids},
        player_nfl_team_ids={player_id: teams[player_id] for player_id in projected_ids},
        provider_provenance=provider_provenance,
    )


def _complete_evidence(rows, player_ids, providers, captured, state):
    result = [row for row in rows if row.canonical_player_id in player_ids]
    covered = {(row.provider, row.canonical_player_id) for row in result}
    for player_id in player_ids:
        for provider in providers:
            if (provider, player_id) in covered:
                continue
            result.append(
                RemainingSeasonProjection(
                    canonical_player_id=player_id,
                    snapshot_id=state.snapshot_id,
                    scoring_profile_id=state.scoring_profile_id,
                    provider=provider,
                    provider_player_id=f"not-published:{player_id}",
                    season=state.season,
                    applicable_weeks=state.remaining_regular_season_weeks,
                    status=ProjectionStatus.NOT_PUBLISHED,
                    origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                    captured_at=captured[provider],
                )
            )
    return tuple(result)


def _complete_ensembles(rows, positions, config):
    groups = defaultdict(list)
    for row in rows:
        groups[(row.canonical_player_id, row.week)].append(row)
    result = []
    for (player_id, _), values in sorted(groups.items()):
        if _meets_quorum(values, config.minimum_observed_sources):
            result.append(fuse_weekly_projections(values, positions[player_id], config))
    return tuple(result)


def _projected_player_names(player_names, player_ids):
    result = {}
    for player_id in sorted(player_ids):
        display_name = player_names.get(player_id)
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError(
                f"projected Player Lab player {player_id!r} is missing a display name"
            )
        result[player_id] = display_name
    return result


def _meets_quorum(rows, minimum):
    statuses = tuple(row.status for row in rows)
    return all(status is ProjectionStatus.BYE for status in statuses) or sum(
        status is ProjectionStatus.OBSERVED for status in statuses
    ) >= minimum


def _text_set(name, values):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        result = set(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable of strings") from None
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


__all__ = ("build_player_lab_projection_snapshot",)
