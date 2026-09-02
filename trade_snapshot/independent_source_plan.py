"""Deterministic public projection capture plans for independent refreshes."""

from collections.abc import Iterable

from .capture_schema import CapturePlan, PageCaptureTask, ProjectionTableSpec
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_player_position
from .public_projection_plan import public_projection_tasks


_ESPN_PAGE = "https://fantasy.espn.com/football/players/projections"
_YAHOO_PAGE = "https://football.fantasysports.yahoo.com/f1/players"


def build_independent_weekly_source_plan(
    *,
    season: int,
    as_of_week: int,
    remaining_weeks: Iterable[int],
    scoring: str,
    player_positions: Iterable[str],
    include_future_weekly: bool = True,
    broad_consensus: bool = True,
) -> CapturePlan:
    """Capture ESPN plus public CBS ROS tables without an analyzer source."""

    weeks = _weeks(remaining_weeks, as_of_week, include_future_weekly)
    positions = _validate_positions(player_positions)
    tasks = []
    for week in weeks:
        tasks.append(
            _projection_task("espn", _ESPN_PAGE, season, week, "weekly", scoring)
        )
        tasks.extend(
            _projection_task(
                "yahoo", _YAHOO_PAGE, season, week, "weekly", scoring, (position,)
            )
            for position in positions
        )
    tasks.append(
        _projection_task("espn", _ESPN_PAGE, season, as_of_week, "ros", scoring)
    )
    tasks.extend(
        _projection_task(
            "yahoo", _YAHOO_PAGE, season, as_of_week, "ros", scoring, (position,)
        )
        for position in positions
    )
    if broad_consensus:
        for week in weeks:
            tasks.extend(
                public_projection_tasks(
                    season=season,
                    week=week,
                    horizon="weekly",
                    scoring=scoring,
                    positions=positions,
                )
            )
        tasks.extend(
            public_projection_tasks(
                season=season,
                week=as_of_week,
                horizon="ros",
                scoring=scoring,
                positions=positions,
            )
        )
    return CapturePlan(tasks)


def _projection_task(
    provider, url, season, week, horizon, scoring, positions=("ALL",)
):
    return PageCaptureTask(
        provider,
        season,
        week,
        "visible_table",
        url,
        projection=ProjectionTableSpec(horizon, scoring, positions),
    )


def _validate_positions(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("player_positions must be an iterable")
    try:
        positions = {
            normalize_player_position(value, require_supported=True)
            for value in values
        }
    except TypeError:
        raise ValueError("player_positions must be an iterable") from None
    if "IDP" in positions:
        positions.remove("IDP")
        positions.update(("DL", "LB", "DB"))
    if not positions or not positions <= CANONICAL_PLAYER_POSITIONS:
        raise ValueError("player_positions must contain supported positions")
    return tuple(sorted(positions))


def _weeks(values, as_of_week, include_future):
    if type(as_of_week) is not int or not 1 <= as_of_week <= 25:
        raise ValueError("as_of_week must be between 1 and 25")
    if not isinstance(include_future, bool):
        raise ValueError("include_future_weekly must be a boolean")
    if isinstance(values, (str, bytes)):
        raise ValueError("remaining_weeks must be an iterable")
    try:
        weeks = tuple(values)
    except TypeError:
        raise ValueError("remaining_weeks must be an iterable") from None
    if not weeks or any(
        type(week) is not int or not as_of_week <= week <= 25 for week in weeks
    ):
        raise ValueError(
            "remaining_weeks must contain unique weeks at or after as_of_week"
        )
    if len(set(weeks)) != len(weeks):
        raise ValueError(
            "remaining_weeks must contain unique weeks at or after as_of_week"
        )
    return tuple(sorted(weeks)) if include_future else (as_of_week,)


__all__ = ("build_independent_weekly_source_plan",)
