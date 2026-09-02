"""Deterministic ESPN/Yahoo projection capture plans for independent refreshes."""

from collections.abc import Iterable

from .capture_schema import CapturePlan, PageCaptureTask, ProjectionTableSpec
from .positions import normalize_player_position


_PROVIDER_PAGES = (
    ("espn", "https://fantasy.espn.com/football/players/projections"),
    ("yahoo", "https://football.fantasysports.yahoo.com/f1/players"),
)


def build_independent_weekly_source_plan(
    *,
    season: int,
    as_of_week: int,
    remaining_weeks: Iterable[int],
    scoring: str,
    player_positions: Iterable[str],
    include_future_weekly: bool = True,
) -> CapturePlan:
    """Capture ESPN/Yahoo weekly and ROS tables without an analyzer source."""

    weeks = _weeks(remaining_weeks, as_of_week, include_future_weekly)
    _validate_positions(player_positions)
    tasks = [
        _projection_task(provider, url, season, week, "weekly", scoring)
        for week in weeks
        for provider, url in _PROVIDER_PAGES
    ]
    tasks.extend(
        _projection_task(provider, url, season, as_of_week, "ros", scoring)
        for provider, url in _PROVIDER_PAGES
    )
    return CapturePlan(tasks)


def _projection_task(provider, url, season, week, horizon, scoring):
    return PageCaptureTask(
        provider,
        season,
        week,
        "visible_table",
        url,
        projection=ProjectionTableSpec(horizon, scoring, ("ALL",)),
    )


def _validate_positions(values: Iterable[str]) -> None:
    if isinstance(values, (str, bytes)):
        raise ValueError("player_positions must be an iterable")
    try:
        positions = {
            normalize_player_position(value, require_supported=True)
            for value in values
        }
    except TypeError:
        raise ValueError("player_positions must be an iterable") from None
    if not positions:
        raise ValueError("player_positions must contain supported positions")


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
