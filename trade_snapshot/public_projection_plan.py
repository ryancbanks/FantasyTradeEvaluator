"""Deterministic capture tasks for public independent projection publishers."""

from collections.abc import Iterable

from ._capture_task_policy import FFTODAY_POSITION_SCOPES
from .capture_schema import PageCaptureTask, ProjectionTableSpec


CBS_ROOT = "https://www.cbssports.com/fantasy/football/stats"
FFTODAY_WEEKLY = "https://www.fftoday.com/rankings/playerwkproj.php"
FFTODAY_SEASON = "https://www.fftoday.com/rankings/playerproj.php"
FANTASYSHARKS = (
    "https://www.fantasysharks.com/apps/bert/forecasts/projections.php"
)

_CBS_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DST"})
_FANTASYSHARKS_POSITIONS = frozenset(
    {"QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB"}
)


def public_projection_tasks(
    *,
    season: int,
    week: int,
    horizon: str,
    scoring: str,
    positions: Iterable[str],
) -> tuple[PageCaptureTask, ...]:
    """Return one bounded visible-table task for each supported position/source."""

    position_values = tuple(sorted(set(positions)))
    if not position_values:
        raise ValueError("public projection positions cannot be empty")
    if horizon not in FFTODAY_POSITION_SCOPES:
        raise ValueError("horizon must be weekly or ros")
    tasks = []
    if horizon == "ros":
        tasks.extend(
            PageCaptureTask(
                "cbs",
                season,
                week,
                "visible_table",
                _cbs_url(season, scoring, position),
                projection=ProjectionTableSpec(horizon, scoring, (position,)),
            )
            for position in position_values
            if position in _CBS_POSITIONS
        )
    fftoday_positions = FFTODAY_POSITION_SCOPES[horizon]
    fftoday_url = FFTODAY_WEEKLY if horizon == "weekly" else FFTODAY_SEASON
    tasks.extend(
        PageCaptureTask(
            "fftoday",
            season,
            week,
            "visible_table",
            fftoday_url,
            projection=ProjectionTableSpec(horizon, scoring, (position,)),
        )
        for position in position_values
        if position in fftoday_positions
    )
    tasks.extend(
        PageCaptureTask(
            "fantasysharks",
            season,
            week,
            "visible_table",
            FANTASYSHARKS,
            projection=ProjectionTableSpec(horizon, scoring, (position,)),
        )
        for position in position_values
        if position in _FANTASYSHARKS_POSITIONS
    )
    return tuple(tasks)


def _cbs_url(season: int, scoring: str, position: str) -> str:
    source_scoring = "nonppr" if scoring == "STD" else "ppr"
    return (
        f"{CBS_ROOT}/{position}/{season}/season/projections/"
        f"{source_scoring}/"
    )


__all__ = (
    "CBS_ROOT",
    "FANTASYSHARKS",
    "FFTODAY_SEASON",
    "FFTODAY_WEEKLY",
    "public_projection_tasks",
)
