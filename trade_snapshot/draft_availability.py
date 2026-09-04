"""Explicit, past-observed availability evidence for historical roster decisions."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class AvailabilityStatus(str, Enum):
    ACTIVE = "active"
    OUT = "out"
    IR = "ir"
    SEASON_ENDING_IR = "season_ending_ir"
    INFERRED_OUT = "inferred_out"
    INFERRED_IR = "inferred_ir"
    EXTENDED_ABSENCE = "extended_absence"

    @property
    def inferred(self):
        return self in {self.INFERRED_OUT, self.INFERRED_IR, self.EXTENDED_ABSENCE}


@dataclass(frozen=True, slots=True)
class RosterAvailabilityReport:
    player_id: str
    week: int
    status: AvailabilityStatus
    source_week: int
    source: str

    def __post_init__(self):
        for name in ("player_id", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise ValueError(f"availability {name} must be non-empty bounded text")
        if type(self.week) is not int or not 1 <= self.week <= 25:
            raise ValueError("availability week must be an integer from 1 through 25")
        if type(self.source_week) is not int or not 0 <= self.source_week < self.week:
            raise ValueError("availability source_week must precede the decision week")
        if not isinstance(self.status, AvailabilityStatus):
            raise ValueError("availability status is invalid")

    def to_record(self):
        return {
            "player_id": self.player_id, "week": self.week,
            "status": self.status.value, "source_week": self.source_week,
            "source": self.source,
        }

    @classmethod
    def from_record(cls, record):
        if not isinstance(record, Mapping) or set(record) != {
            "player_id", "week", "status", "source_week", "source",
        }:
            raise ValueError("availability report fields are invalid")
        try:
            status = AvailabilityStatus(record["status"])
        except (ValueError, TypeError):
            raise ValueError("availability status is invalid") from None
        if status.inferred:
            raise ValueError("inferred absence is generated only by enabled league rules, not imported reports")
        return cls(record["player_id"], record["week"], status,
                   record["source_week"], record["source"])


def prepare_availability(reports, weeks):
    """Build immutable-by-owner weekly views; only IR persists until a new report.

    OUT is a one-week absence. Missing evidence is not an injury diagnosis.
    Season-ending IR must be explicitly supplied, never derived from future play.
    """
    ordered = iter(sorted(reports, key=lambda row: (row.week, row.player_id)))
    pending = next(ordered, None)
    persistent = {}
    one_week = {}
    result = {}
    for week in weeks:
        while pending is not None and pending.week <= week:
            if pending.status is AvailabilityStatus.ACTIVE:
                persistent.pop(pending.player_id, None)
                one_week.pop(pending.player_id, None)
            elif pending.status is AvailabilityStatus.OUT or pending.status.inferred:
                one_week[pending.player_id] = pending
            else:
                persistent[pending.player_id] = pending
            pending = next(ordered, None)
        result[week] = {player_id: report for player_id, report in one_week.items()
                        if report.week == week}
        result[week].update(persistent)
    return result


def infer_zero_point_absences(player, scores, weeks, config):
    """Optional fantasy absence proxy: no medical claim and no look-ahead."""
    thresholds = (
        (config.zero_point_drop_weeks, AvailabilityStatus.EXTENDED_ABSENCE),
        (config.zero_point_ir_weeks, AvailabilityStatus.INFERRED_IR),
        (config.zero_point_out_weeks, AvailabilityStatus.INFERRED_OUT),
    )
    if player.position not in {"QB", "RB", "WR", "TE"} or not any(n for n, _ in thresholds):
        return {}
    outcomes = iter(player.actual_weeks)
    pending = next(outcomes, None)
    streak = 0
    source_week = 0
    observed_week = 0
    result = {}
    for week in weeks:
        while pending is not None and pending.week < week:
            if pending.week != observed_week + 1:
                streak = 0
            observed_week = pending.week
            status = pending.status.value
            if status != "bye":
                streak = (streak + 1 if status in {"played", "inactive"}
                          and scores[player.player_id, pending.week] == 0.0 else 0)
                source_week = pending.week
            pending = next(outcomes, None)
        if observed_week != week - 1:
            streak = 0
        status = next((status for threshold, status in thresholds if threshold and streak >= threshold), None)
        if status is not None:
            result[week] = RosterAvailabilityReport(
                player.player_id, week, status, source_week,
                f"Inferred zero-point absence: {streak} completed non-bye weeks through Week {source_week}; "
                "not a confirmed injury or IR designation.",
            )
    return result
