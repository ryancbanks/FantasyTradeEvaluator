"""Build a complete weekly grid while preserving published-versus-derived provenance."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from math import fsum

from .league_state import LeagueState
from .nfl_schedule import NflSchedule, NflTeamWeek, NflTeamWeekStatus
from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)


_ROS_DERIVATION_STATUSES = frozenset(
    {ProjectionStatus.OBSERVED, ProjectionStatus.NOT_PUBLISHED}
)


def materialize_weekly_grid(
    state: LeagueState,
    evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    *,
    player_ids: Iterable[str],
    provider_names: Iterable[str],
    nfl_schedule: NflSchedule,
    player_nfl_team_ids: Mapping[str, str],
) -> tuple[WeeklyProjection, ...]:
    """Fill the weekly grid and attach only verified schedule context."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be an NflSchedule")
    if nfl_schedule.season != state.season:
        raise ValueError("NFL schedule season does not match league state")
    players = _texts("player_ids", player_ids)
    providers = _texts("provider_names", provider_names)
    player_teams = _player_teams(player_nfl_team_ids, players)
    for player in players:
        for week in state.remaining_regular_season_weeks:
            nfl_schedule.team_week(player_teams[player], week)
    rows = tuple(evidence)
    if not rows or any(
        not isinstance(row, (WeeklyProjection, RemainingSeasonProjection)) for row in rows
    ):
        raise ValueError("evidence must contain normalized source projection rows")
    identity = (state.snapshot_id, state.scoring_profile_id, state.season)
    weekly, ros, references = {}, {}, {}
    for row in rows:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("projection evidence identity does not match league state")
        if row.canonical_player_id is None:
            continue
        key = (row.canonical_player_id, row.provider)
        if key not in references:
            references[key] = row.provider_player_id
        elif references[key] != row.provider_player_id:
            raise ValueError("one player/provider has conflicting provider IDs")
        if isinstance(row, WeeklyProjection):
            if row.week not in state.remaining_regular_season_weeks:
                continue
            row_key = (*key, row.week)
            if row_key in weekly:
                raise ValueError("projection evidence contains a duplicate weekly row")
            weekly[row_key] = row
            expected_team = player_teams.get(row.canonical_player_id)
            if (
                expected_team is not None
                and row.nfl_team_id is not None
                and row.nfl_team_id != expected_team
            ):
                raise ValueError("projection NFL team conflicts with player identity")
        else:
            if key in ros:
                raise ValueError("projection evidence contains a duplicate ROS row")
            ros[key] = row
    required = {(player, provider) for player in players for provider in providers}
    if not required.issubset(references):
        missing = min(required.difference(references))
        raise ValueError(f"projection evidence lacks provider identity {missing!r}")
    materialized = []
    for player, provider in sorted(required):
        materialized.extend(
            _player_provider_rows(
                state,
                player,
                provider,
                references[(player, provider)],
                player_teams[player],
                nfl_schedule,
                weekly,
                ros.get((player, provider)),
            )
        )
    return _harmonize_game_context(tuple(materialized))


def _player_provider_rows(
    state, player, provider, provider_id, nfl_team, nfl_schedule, weekly, ros
) -> tuple[WeeklyProjection, ...]:
    weeks = state.remaining_regular_season_weeks
    published = {
        week: weekly[(player, provider, week)]
        for week in weeks
        if (player, provider, week) in weekly
    }
    schedule = {week: nfl_schedule.team_week(nfl_team, week) for week in weeks}
    active = {
        week
        for week, team_week in schedule.items()
        if team_week.status is NflTeamWeekStatus.SCHEDULED
    }
    _validate_ros_weeks(weeks, active, ros, player, provider)
    derived_points, derived_stats = _ros_derivation(
        weeks, active, published, ros
    )
    result = []
    for week in weeks:
        published_row = published.get(week)
        team_week = schedule[week]
        if team_week.status is NflTeamWeekStatus.BYE:
            result.append(
                _bye_row(
                    state,
                    player,
                    provider,
                    provider_id,
                    nfl_team,
                    week,
                    published_row,
                    published,
                    ros,
                )
            )
            continue
        if (
            published_row is not None
            and published_row.status is not ProjectionStatus.NOT_PUBLISHED
        ):
            result.append(_scheduled_row(published_row, team_week))
            continue
        if derived_points is None:
            if published_row is not None:
                result.append(_scheduled_row(published_row, team_week))
                continue
            captured = max(
                (row.captured_at for row in published.values()),
                default=ros.captured_at if ros else None,
            )
            if captured is None:
                raise ValueError("unpublished weekly row lacks capture evidence")
            result.append(
                WeeklyProjection(
                    player,
                    state.snapshot_id,
                    state.scoring_profile_id,
                    provider,
                    provider_id,
                    state.season,
                    week,
                    ProjectionStatus.NOT_PUBLISHED,
                    captured,
                    nfl_team_id=nfl_team,
                    nfl_game_id=team_week.nfl_game_id,
                    opponent_team_id=team_week.opponent_team_id,
                    is_home=team_week.is_home,
                    origin=WeeklyProjectionOrigin.PROVIDER_PUBLISHED,
                )
            )
            continue
        result.append(
            WeeklyProjection(
                player,
                state.snapshot_id,
                state.scoring_profile_id,
                provider,
                provider_id,
                state.season,
                week,
                ProjectionStatus.OBSERVED,
                ros.captured_at,
                derived_points,
                derived_stats,
                nfl_team_id=nfl_team,
                nfl_game_id=team_week.nfl_game_id,
                opponent_team_id=team_week.opponent_team_id,
                is_home=team_week.is_home,
                source_published_at=ros.source_published_at,
                origin=WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON,
            )
        )
    return tuple(result)


def _validate_ros_weeks(weeks, active, ros, player, provider):
    if ros is None or ros.status is not ProjectionStatus.OBSERVED:
        return
    declared = set(ros.applicable_weeks)
    if declared not in (set(weeks), set(active)):
        raise ValueError(
            f"ROS projection for {(player, provider)!r} has applicable weeks "
            "that conflict with the verified NFL schedule"
        )


def _ros_derivation(weeks, active, published, ros):
    missing = tuple(
        week
        for week in weeks
        if week in active
        and (
            week not in published
            or published[week].status is ProjectionStatus.NOT_PUBLISHED
        )
    )
    unsafe = any(
        week in active and row.status not in _ROS_DERIVATION_STATUSES
        for week, row in published.items()
    )
    if unsafe:
        return None, {}
    return _derived_points(ros, published, missing, active), _derived_stats(
        ros, published, missing, active
    )


def _derived_points(ros, published, missing_weeks, active):
    if not missing_weeks or ros is None or ros.status is not ProjectionStatus.OBSERVED:
        return None
    observed = fsum(
        row.projected_fantasy_points
        for week, row in published.items()
        if week in active and row.status is ProjectionStatus.OBSERVED
    )
    return (ros.projected_fantasy_points - observed) / len(missing_weeks)


def _derived_stats(ros, published, missing_weeks, active):
    if not missing_weeks or ros is None or ros.status is not ProjectionStatus.OBSERVED:
        return {}
    result = {}
    for name, total in ros.raw_projected_stats.items():
        observed = fsum(
            row.raw_projected_stats.get(name, 0)
            for week, row in published.items()
            if week in active and row.status is ProjectionStatus.OBSERVED
        )
        result[name] = (total - observed) / len(missing_weeks)
    return result


def _scheduled_row(row: WeeklyProjection, team_week: NflTeamWeek) -> WeeklyProjection:
    if row.status is ProjectionStatus.BYE:
        raise ValueError("provider bye conflicts with the verified NFL schedule")
    if row.nfl_team_id is not None and row.nfl_team_id != team_week.nfl_team_id:
        raise ValueError("provider NFL team conflicts with the verified NFL schedule")
    context = (row.nfl_game_id, row.opponent_team_id, row.is_home)
    expected = (
        team_week.nfl_game_id,
        team_week.opponent_team_id,
        team_week.is_home,
    )
    if any(value is not None for value in context) and context != expected:
        raise ValueError("provider game context conflicts with the verified NFL schedule")
    return replace(
        row,
        nfl_team_id=team_week.nfl_team_id,
        nfl_game_id=team_week.nfl_game_id,
        opponent_team_id=team_week.opponent_team_id,
        is_home=team_week.is_home,
    )


def _bye_row(
    state,
    player,
    provider,
    provider_id,
    nfl_team,
    week,
    published_row,
    published,
    ros,
) -> WeeklyProjection:
    if published_row is not None and published_row.status not in {
        ProjectionStatus.BYE,
        ProjectionStatus.NOT_PUBLISHED,
    }:
        raise ValueError("provider projection conflicts with a verified NFL bye")
    if published_row is not None and published_row.nfl_team_id not in {None, nfl_team}:
        raise ValueError("provider NFL team conflicts with the verified NFL schedule")
    if published_row is not None and published_row.status is ProjectionStatus.BYE:
        return replace(published_row, nfl_team_id=nfl_team)
    capture_times = [row.captured_at for row in published.values()]
    if ros is not None:
        capture_times.append(ros.captured_at)
    if not capture_times:
        raise ValueError("derived bye lacks capture evidence")
    return WeeklyProjection(
        player,
        state.snapshot_id,
        state.scoring_profile_id,
        provider,
        provider_id,
        state.season,
        week,
        ProjectionStatus.BYE,
        max(capture_times),
        nfl_team_id=nfl_team,
        source_published_at=(
            ros.source_published_at
            if ros is not None
            else published_row.source_published_at if published_row is not None else None
        ),
        origin=WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON,
    )


def _harmonize_game_context(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row.canonical_player_id, row.week)].append(row)
    result = []
    for key in sorted(groups):
        group = groups[key]
        contexts = {
            (row.nfl_team_id, row.nfl_game_id, row.opponent_team_id, row.is_home)
            for row in group
            if row.nfl_game_id is not None
        }
        if len(contexts) > 1:
            raise ValueError(f"providers disagree on NFL game context for {key!r}")
        context = next(iter(contexts), None)
        for row in group:
            if context is not None and row.nfl_game_id is None and row.status is not ProjectionStatus.BYE:
                row = replace(
                    row,
                    nfl_team_id=context[0],
                    nfl_game_id=context[1],
                    opponent_team_id=context[2],
                    is_home=context[3],
                )
            result.append(row)
    return tuple(sorted(result, key=lambda row: (row.canonical_player_id, row.week, row.provider)))


def _texts(name, values):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of non-empty strings")
    rows = tuple(values)
    if not rows or any(not isinstance(value, str) or not value for value in rows):
        raise ValueError(f"{name} must contain non-empty strings")
    if len(set(rows)) != len(rows):
        raise ValueError(f"{name} contains a duplicate")
    return rows


def _player_teams(value, players):
    if not isinstance(value, Mapping):
        raise ValueError("player_nfl_team_ids must be a mapping")
    result = {}
    for player_id, team_id in value.items():
        if not isinstance(player_id, str) or not player_id:
            raise ValueError("player_nfl_team_ids keys must be non-empty strings")
        if not isinstance(team_id, str) or not team_id:
            raise ValueError("player_nfl_team_ids values must be non-empty strings")
        result[player_id] = team_id
    if set(result) != set(players):
        raise ValueError("player_nfl_team_ids must cover exactly the materialized players")
    return result
