"""Conservative, one-week-lagged availability from nflverse weekly rosters.

These retrospective roster snapshots are not timestamped pregame injury reports.
Only exact injured-reserve codes establish IR; no source field proves season end.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

from .draft_availability import AvailabilityStatus, RosterAvailabilityReport
from .draft_corpus_sources import _MAX_ROSTER_ROWS, _csv_integer, _csv_rows, _text

# Meanings: nflreadr.nflverse.com/articles/dictionary_roster_status.html and
# github.com/nflverse/nflreadr/issues/232. Unclear codes deliberately stay unknown.
_ACTIVE_CODES = frozenset({"ACT", "ACTIVE"})
_OUT_CODES = frozenset({
    "EXE", "DEV", "CUT", "INA", "INACTIVE", "OUT", "PUP", "RES", "RESERVE",
    "RET", "SUS", "TRC", "TRD", "TRT", "UFA",
})
_OUT_DETAILS = frozenset({
    "E02", "P01", "P02", "P03", "P06", "P07", "R02", "R03", "R04", "R05",
    "R06", "R23", "R27", "R30", "R33", "R40", "R47", "W03",
})
_IR_DETAILS = frozenset({"R01", "R48"})
_IR_COMPATIBLE_CODES = frozenset({"", "RES", "RESERVE", "INA", "INACTIVE", "OUT"})
_LIMITATIONS = (
    "Weekly rosters are retrospective, without immutable pregame timestamps; "
    "observations are delayed one week and cannot identify same-week injuries.",
    "ACTIVE means active NFL roster, not confirmed healthy. Generic reserve or "
    "other unavailable status is only a next-week proxy, not an injury diagnosis.",
    "Missing or conflicting rows remain unknown. No season-ending IR is inferred "
    "from reserve status or future absence; Week 1 has no prior-week evidence.",
)


def load_roster_availability(
    path: str | Path,
    season: int,
    player_ids: Iterable[str],
    available_weeks: Iterable[int],
) -> tuple[tuple[RosterAvailabilityReport, ...], Mapping[str, object]]:
    """Load a complete season calendar once, retaining no raw record cache."""
    if type(season) is not int or not 1900 <= season <= 9999:
        raise ValueError("roster availability season is invalid")
    selected = frozenset(_text("availability player_id", value) for value in player_ids)
    weeks = tuple(available_weeks)
    if any(type(week) is not int or not 1 <= week <= 25 for week in weeks):
        raise ValueError("roster availability weeks must be integers from 1 through 25")
    if weeks and set(weeks) != set(range(1, max(weeks) + 1)):
        raise ValueError("roster availability requires a complete calendar from Week 1")
    source_weeks = {week - 1 for week in weeks if week > 1}
    suffix = ".csv.gz" if Path(path).name.endswith(".gz") else ".csv"
    source_url = (
        "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/"
        f"roster_weekly_{season}{suffix}"
    )
    coverage = {
        "method": "prior_week_roster_snapshot",
        "source_url": source_url,
        "report_count": 0,
        "observed_player_weeks": 0,
        "exact_ir_player_weeks": 0,
        "reserve_proxy_player_weeks": 0,
        "other_out_player_weeks": 0,
        "active_roster_player_weeks": 0,
        "missing_player_weeks": len(selected) * len(source_weeks),
        "unknown_status_player_weeks": 0,
        "ambiguous_player_weeks": 0,
        "duplicate_rows": 0,
        "unusable_week_rows": 0,
        "limitations": list(_LIMITATIONS),
    }
    if season <= 2015:
        coverage["method"] = "unavailable_reconstructed_season_status"
        coverage["limitations"].append(
            "Through 2015 the source replaces weekly status with reconstructed "
            "season-level status; no point-in-time availability is imported. "
            "https://github.com/nflverse/nflverse-rosters/blob/main/R/rosters.R"
        )
        return (), coverage
    if not selected or not source_weeks:
        return (), coverage

    observations, ambiguous, counts = _roster_observations(
        path, season, selected, source_weeks
    )
    reports = []
    previous_status = {}
    for (player_id, source_week), (coarse, detail) in sorted(observations.items()):
        if (player_id, source_week) in ambiguous:
            counts["ambiguous_player_weeks"] += 1
            continue
        status, category = _classify_status(coarse, detail)
        counts[category] += 1
        if status is None:
            continue
        if status is AvailabilityStatus.OUT or previous_status.get(player_id) != status:
            reports.append(RosterAvailabilityReport(
                player_id, source_week + 1, status, source_week,
                f"{source_url} (observed week {source_week}; "
                f"status {coarse or 'missing'}/{detail or 'missing'})",
            ))
        previous_status[player_id] = status
    coverage.update(counts)
    coverage["observed_player_weeks"] = len(observations)
    coverage["missing_player_weeks"] -= len(observations)
    coverage["report_count"] = len(reports)
    return tuple(sorted(reports, key=lambda row: (row.week, row.player_id))), coverage


def _roster_observations(path, season, selected, source_weeks):
    observations = {}
    ambiguous = set()
    counts = Counter()
    required = {"season", "week", "game_type", "gsis_id", "status"}
    for row in _csv_rows(path, required, _MAX_ROSTER_ROWS):
        player_id = (row["gsis_id"] or "").strip()
        if player_id not in selected or row["game_type"] != "REG":
            continue
        if _csv_integer("roster season", row["season"], 1900, 9999) != season:
            continue
        try:
            source_week = _csv_integer("roster week", row["week"], 1, 25)
        except ValueError:
            counts["unusable_week_rows"] += 1
            continue
        if source_week not in source_weeks:
            continue
        key = player_id, source_week
        value = (
            (row["status"] or "").strip().upper(),
            (row.get("status_description_abbr") or "").strip().upper(),
        )
        if key in observations:
            counts["duplicate_rows"] += 1
            if observations[key] != value:
                ambiguous.add(key)
        else:
            observations[key] = value
    return observations, ambiguous, counts


def _classify_status(coarse, detail):
    active = coarse in _ACTIVE_CODES
    unavailable = coarse in _OUT_CODES
    detail_active = detail == "A01"
    detail_ir = detail in _IR_DETAILS
    detail_out = detail in _OUT_DETAILS
    if (active and (detail_ir or detail_out)) or (unavailable and detail_active):
        return None, "ambiguous_player_weeks"
    if detail_ir and coarse not in _IR_COMPATIBLE_CODES:
        return None, "ambiguous_player_weeks"
    if coarse and not (active or unavailable):
        return None, "unknown_status_player_weeks"
    if detail_ir:
        return AvailabilityStatus.IR, "exact_ir_player_weeks"
    if detail_out or unavailable:
        category = (
            "reserve_proxy_player_weeks" if coarse in {"RES", "RESERVE"}
            else "other_out_player_weeks"
        )
        return AvailabilityStatus.OUT, category
    if detail_active or (active and not detail):
        return AvailabilityStatus.ACTIVE, "active_roster_player_weeks"
    return None, "unknown_status_player_weeks"


__all__ = ("load_roster_availability",)
