"""Shared robust slope semantics for cumulative fantasy records."""

from collections.abc import Iterable


RECORD_SLOPE_WINDOW = 4
RECORD_SLOPE_NEUTRAL_BAND = 0.02


def trailing_record_slope(
    points: Iterable[tuple[int, float]],
    *,
    window: int = RECORD_SLOPE_WINDOW,
) -> float | None:
    """Return a Theil-Sen slope for the latest record observations."""

    rows = points[-window:] if type(points) in (list, tuple) else tuple(points)[-window:]
    if len(rows) < 3:
        return None
    slopes = [
        (right_value - left_value) / (right_week - left_week)
        for index, (left_week, left_value) in enumerate(rows)
        for right_week, right_value in rows[index + 1 :]
        if right_week != left_week
    ]
    if not slopes:
        return None
    slopes.sort()
    middle = len(slopes) // 2
    return (
        slopes[middle]
        if len(slopes) % 2
        else (slopes[middle - 1] + slopes[middle]) / 2
    )


def record_slope_direction(
    slope: float | None,
    *,
    unavailable: str = "insufficient_history",
) -> str:
    if slope is None:
        return unavailable
    if slope < -RECORD_SLOPE_NEUTRAL_BAND:
        return "downward"
    if slope > RECORD_SLOPE_NEUTRAL_BAND:
        return "upward"
    return "neutral"


__all__ = (
    "RECORD_SLOPE_NEUTRAL_BAND",
    "RECORD_SLOPE_WINDOW",
    "record_slope_direction",
    "trailing_record_slope",
)
