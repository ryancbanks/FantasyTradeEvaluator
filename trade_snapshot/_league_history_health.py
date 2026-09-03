"""Shared freshness and health-status boundaries for league history."""

from .league_history import HISTORY_CAPTURE_BINDING_TOLERANCE


PHYSICAL_INJURY_STATUSES = frozenset(
    {
        "DAY_TO_DAY",
        "DOUBTFUL",
        "DTD",
        "INJURED_RESERVE",
        "INJURY_RESERVE",
        "IR",
        "NFI",
        "OUT",
        "PUP",
        "QUESTIONABLE",
    }
)
NON_PHYSICAL_UNAVAILABLE_STATUSES = frozenset({"SUSPENDED"})
RECOGNIZED_HEALTH_STATUSES = frozenset(
    {"ACTIVE"}
).union(PHYSICAL_INJURY_STATUSES, NON_PHYSICAL_UNAVAILABLE_STATUSES)


def capture_is_fresh(capture, as_of):
    return bool(
        capture is not None
        and capture.captured_at <= as_of
        and as_of - capture.captured_at <= HISTORY_CAPTURE_BINDING_TOLERANCE
        and capture.coverage_end <= as_of
        and as_of - capture.coverage_end <= HISTORY_CAPTURE_BINDING_TOLERANCE
    )


def latest_physical_injury_ids(captures, as_of):
    latest = max(
        (
            row
            for row in captures
            if row.captured_at <= as_of and row.roster_complete
        ),
        key=lambda row: (row.captured_at, row.capture_id),
        default=None,
    )
    if latest is None or not capture_is_fresh(latest, as_of):
        return ()
    return tuple(
        sorted(
            player.canonical_player_id
            for roster in latest.rosters
            for player in roster.players
            if player.injury_status in PHYSICAL_INJURY_STATUSES
        )
    )


__all__ = (
    "NON_PHYSICAL_UNAVAILABLE_STATUSES",
    "PHYSICAL_INJURY_STATUSES",
    "RECOGNIZED_HEALTH_STATUSES",
    "capture_is_fresh",
    "latest_physical_injury_ids",
)
