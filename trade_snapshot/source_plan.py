"""Efficient dimension-bound plan for one weekly local-data refresh."""

from collections.abc import Iterable

from .capture_schema import (
    CapturePlan,
    FantasyProsECRTask,
    PageCaptureTask,
    ProjectionTableSpec,
)
from ._capture_dimensions import fantasypros_ecr_source_scoring
from ._capture_task_policy import fantasypros_projection_url
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_player_position


_PROVIDER_PAGES = {
    "espn": "https://fantasy.espn.com/football/players/projections",
    "yahoo": "https://football.fantasysports.yahoo.com/f1/players",
}
_FP_RANKING_ROOT = "https://www.fantasypros.com/nfl/rankings"
_LEAGUE_PAGE = "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"


def build_weekly_source_plan(
    *,
    season: int,
    as_of_week: int,
    remaining_weeks: Iterable[int],
    scoring: str,
    player_positions: Iterable[str],
    include_future_weekly: bool = False,
) -> CapturePlan:
    """Capture tables once; every trade calculation after this is local."""

    weeks = _weeks(remaining_weeks, as_of_week)
    if not isinstance(include_future_weekly, bool):
        raise ValueError("include_future_weekly must be a boolean")
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
    # FantasyPros publishes no provable in-season ROS table. Attempt each
    # remaining weekly page so published values are retained directly and
    # unpublished future pages become explicit, timestamped attempt outcomes.
    for week in weeks:
        tasks.extend(_fantasypros_weekly_tasks(season, week, scoring, positions))
    optional_weeks = weeks if include_future_weekly else (as_of_week,)
    for week in optional_weeks:
        tasks.extend(_optional_provider_tasks(season, week, "weekly", scoring, positions))
    tasks.extend(_optional_provider_tasks(season, as_of_week, "ros", scoring, positions))
    return CapturePlan(tasks)


def _fantasypros_weekly_tasks(season, week, scoring, positions):
    tasks = []
    for position in positions:
        spec = ProjectionTableSpec("weekly", scoring, (position,))
        tasks.append(PageCaptureTask(
            "fantasypros",
            season,
            week,
            "visible_table",
            fantasypros_projection_url(
                position,
                week=week,
                horizon=spec.horizon.value,
                scoring=spec.scoring,
            ),
            projection=spec,
        ))
    return tasks


def _optional_provider_tasks(season, week, horizon, scoring, positions):
    tasks = []
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
    source_scoring = fantasypros_ecr_source_scoring(scoring, (position,))
    prefix = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}[
        source_scoring
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


def _weeks(values: Iterable[int], as_of_week: int) -> tuple[int, ...]:
    if type(as_of_week) is not int or not 1 <= as_of_week <= 25:
        raise ValueError("as_of_week must be between 1 and 25")
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
    return tuple(sorted(weeks))


__all__ = ("build_weekly_source_plan",)
