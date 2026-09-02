"""Efficient queryless plan for one weekly local-data refresh."""

from collections.abc import Iterable

from .capture_schema import (
    CapturePlan,
    FantasyProsECRTask,
    PageCaptureTask,
    ProjectionTableSpec,
)
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_player_position


_PROVIDER_PAGES = {
    "espn": "https://fantasy.espn.com/football/players/projections",
    "yahoo": "https://football.fantasysports.yahoo.com/f1/players",
}
_FP_PROJECTION_ROOT = "https://www.fantasypros.com/nfl/projections"
_FP_RANKING_ROOT = "https://www.fantasypros.com/nfl/rankings"
_LEAGUE_PAGE = "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"


def build_weekly_source_plan(
    *,
    season: int,
    as_of_week: int,
    remaining_weeks: Iterable[int],
    scoring: str,
    player_positions: Iterable[str],
    include_future_weekly: bool = True,
) -> CapturePlan:
    """Capture tables once; every trade calculation after this is local."""

    weeks = _weeks(remaining_weeks, as_of_week, include_future_weekly)
    positions = _positions(player_positions)
    tasks = [
        PageCaptureTask("fantasypros", season, as_of_week, "league_source", _LEAGUE_PAGE)
    ]
    for position in positions:
        tasks.extend(
            FantasyProsECRTask(
                season,
                as_of_week,
                horizon,
                scoring,
                (position,),
                (),
                None,
                _ecr_url(horizon, scoring, position),
            )
            for horizon in ("weekly", "ros")
        )
    for week in weeks:
        tasks.extend(_projection_tasks(season, week, "weekly", scoring, positions))
    tasks.extend(
        _projection_tasks(season, as_of_week, "ros", scoring, positions)
    )
    return CapturePlan(tasks)


def _projection_tasks(season, week, horizon, scoring, positions):
    tasks = [
        PageCaptureTask(
            "fantasypros",
            season,
            week,
            "visible_table",
            f"{_FP_PROJECTION_ROOT}/{position.casefold()}.php",
            projection=ProjectionTableSpec(horizon, scoring, (position,)),
        )
        for position in positions
    ]
    tasks.append(
        PageCaptureTask(
            "espn",
            season,
            week,
            "visible_table",
            _PROVIDER_PAGES["espn"],
            projection=ProjectionTableSpec(horizon, scoring, ("ALL",)),
        )
    )
    tasks.extend(
        PageCaptureTask(
            "yahoo",
            season,
            week,
            "visible_table",
            _PROVIDER_PAGES["yahoo"],
            projection=ProjectionTableSpec(horizon, scoring, (position,)),
        )
        for position in positions
    )
    return tasks


def _ecr_url(horizon: str, scoring: str, position: str) -> str:
    prefix = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}[
        ProjectionTableSpec("weekly", scoring, (position,)).scoring
    ]
    if horizon == "ros":
        slug = f"ros-{prefix}{position.casefold()}"
    else:
        slug = f"{prefix}{position.casefold()}"
    return f"{_FP_RANKING_ROOT}/{slug}.php"


def _positions(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("player_positions must be an iterable")
    try:
        positions = {normalize_player_position(value, require_supported=True) for value in values}
    except TypeError:
        raise ValueError("player_positions must be an iterable") from None
    if "IDP" in positions:
        positions.remove("IDP")
        positions.update(("DL", "LB", "DB"))
    if not positions or not positions <= CANONICAL_PLAYER_POSITIONS:
        raise ValueError("player_positions must contain supported positions")
    return tuple(sorted(positions))


def _weeks(values: Iterable[int], as_of_week: int, include_future: bool) -> tuple[int, ...]:
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
    if (
        not weeks
        or len(set(weeks)) != len(weeks)
        or any(type(week) is not int or not as_of_week <= week <= 25 for week in weeks)
    ):
        raise ValueError("remaining_weeks must contain unique weeks at or after as_of_week")
    return tuple(sorted(weeks)) if include_future else (as_of_week,)


__all__ = ("build_weekly_source_plan",)
