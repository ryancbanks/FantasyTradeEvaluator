"""Presentation records derived from immutable player profile history."""

from collections import defaultdict
from math import fsum
import re


PROFILE_SCOPE_NOTICE = (
    "Player profiles cover the full captured public catalog. Captured projections "
    "outside the bounded trade pool remain available for Player Lab only; trade "
    "calculations and waiver availability use the smaller calculation pool."
)
_MIN_LOWER_TIER_EXPOSURE = 8
_DESIGNATION_WEIGHTS = {"out": 1.0, "doubtful": 0.6, "questionable": 0.3}
_CATALOG_TREND_FIELDS = frozenset(
    {"status", "direction", "change", "adds", "drops", "net_adds"}
)
_DST_STAT_NOTICE = (
    "D/ST is a team unit; the retained nflverse player-stat table does not publish "
    "a complete defense scoring line. Historical D/ST actuals are not applicable."
)
AVAILABILITY_METHOD = (
    "Descriptive documented game-report burden: a weighted report index equal to "
    "20 times recency-weighted "
    "injury-coded documented game-report "
    "designations (out 1.0, doubtful 0.6, questionable 0.3; current/prior/two-years-prior "
    "season weights 1.0/0.65/0.4), capped at 100. Missing stat lines and practice DNP are "
    "never treated as injuries. Practice-only and explicitly non-injury contexts are shown "
    "as evidence but are not weighted. The tier stays unknown without a qualifying game-report "
    "designation or at least eight recorded stat-line exposures during seasons whose report "
    "datasets were observed. This cumulative burden is not normalized by career length and "
    "is not a probability or an estimate of future injury proneness."
)


def outside_calculation_record(profile, weekly_ecr, rest_of_season_ecr):
    """Create a Player Lab row for a catalog player outside the trade pool."""

    return {
        "player_id": profile.canonical_player_id,
        "name": profile.display_name,
        "position": profile.position,
        "eligible_slots": list(profile.fantasy_positions),
        "nfl_team_id": profile.nfl_team_id,
        "owner": None,
        "availability": "outside_calculation_pool",
        "weekly_ecr": weekly_ecr,
        "rest_of_season_ecr": rest_of_season_ecr,
        "remaining_projected_points": None,
        "average_weekly_points": None,
        "average_provider_disagreement": None,
        "average_predictive_uncertainty": None,
        "provider_complete_week_count": 0,
        "all_direct_week_count": 0,
        "total_week_count": 0,
        "weeks": [],
        "provider_remaining_season": [],
    }


def assign_player_ranks(players):
    """Attach ECR-first overall rank and local projection ranks in-place."""

    projected = [
        row for row in players if row["remaining_projected_points"] is not None
    ]
    projected.sort(
        key=lambda row: (
            -row["remaining_projected_points"],
            row["name"].casefold(),
            row["player_id"],
        )
    )
    overall = {row["player_id"]: index for index, row in enumerate(projected, start=1)}
    position_counts = defaultdict(int)
    position_ranks = {}
    for row in projected:
        position_counts[row["position"]] += 1
        position_ranks[row["player_id"]] = position_counts[row["position"]]
    for row in players:
        player_id = row["player_id"]
        row["projection_overall_rank"] = overall.get(player_id)
        row["projection_position_rank"] = position_ranks.get(player_id)
        row["overall_rank"] = overall.get(player_id)
        row["overall_rank_basis"] = (
            "local remaining projection" if player_id in overall else None
        )


def profile_catalog_record(profile, snapshot, scoring_mode):
    """Build only the profile fields needed by the catalog listing."""

    player_stats_supported = profile.position != "DST"
    observed_seasons, _, qualifying_events, _, exposure = _availability_evidence(
        profile, snapshot
    )
    return {
        "status": profile.status,
        "depth_chart": _depth_chart(profile),
        "market_trend": _catalog_trend(
            _market_trend(profile.adds, profile.drops)
        ),
        "performance_trend": _catalog_trend(
            _performance_trend(
                profile.current_season_stats,
                scoring_mode,
                supported=player_stats_supported,
            )
        ),
        "historical_availability": _availability_burden_record(
            qualifying_events,
            exposure,
            observed_seasons,
            snapshot.season,
        ),
    }


def _catalog_trend(record):
    return {
        field: value
        for field, value in record.items()
        if field in _CATALOG_TREND_FIELDS
    }


def profile_record(profile, snapshot, scoring_mode):
    """Build history, trends, depth, and status for one Player Lab row."""

    player_stats_supported = profile.position != "DST"
    return {
        "provider_references": [
            {"provider": row.provider, "provider_player_id": row.provider_player_id}
            for row in profile.provider_references
        ],
        "fantasy_positions": list(profile.fantasy_positions),
        "active": profile.active,
        "status": profile.status,
        "injury_status": profile.injury_status,
        "injury_body_part": profile.injury_body_part,
        "practice_participation": profile.practice_participation,
        "depth_chart": _depth_chart(profile),
        "years_experience": profile.years_experience,
        "jersey_number": profile.jersey_number,
        "headshot_url": profile.headshot_url,
        "market_trend": _market_trend(profile.adds, profile.drops),
        "performance_trend": _performance_trend(
            profile.current_season_stats,
            scoring_mode,
            supported=player_stats_supported,
        ),
        "current_season": _season_stats_record(
            snapshot.season,
            snapshot.current_stats_availability,
            profile.current_season_stats,
            scoring_mode,
            supported=player_stats_supported,
        ),
        "previous_season": _season_stats_record(
            snapshot.season - 1,
            snapshot.previous_stats_availability,
            profile.previous_season_stats,
            scoring_mode,
            supported=player_stats_supported,
        ),
        "historical_availability": _availability_summary(profile, snapshot),
    }


def _depth_chart(profile):
    if profile.depth_chart_position is None and profile.depth_chart_order is None:
        return None
    return {"position": profile.depth_chart_position, "order": profile.depth_chart_order}


def _season_stats_record(season, availability, rows, scoring_mode, *, supported):
    if not supported:
        return {
            "season": season,
            "availability": "not_applicable",
            "scoring_mode": scoring_mode,
            "recorded_stat_lines": None,
            "weeks": [],
            "note": _DST_STAT_NOTICE,
        }
    return {
        "season": season,
        "availability": availability,
        "scoring_mode": scoring_mode,
        "recorded_stat_lines": len(rows) if availability == "observed" else None,
        "weeks": [
            {
                "week": row.week,
                "game_id": row.game_id,
                "nfl_team_id": row.nfl_team_id,
                "opponent_team_id": row.opponent_team_id,
                "fantasy_points_standard": row.fantasy_points_standard,
                "fantasy_points_ppr": row.fantasy_points_ppr,
                "fantasy_points_selected": _selected_fantasy_points(
                    row, scoring_mode
                ),
                "stat_values": dict(row.stat_values),
            }
            for row in rows
        ],
    }


def _market_trend(adds, drops):
    if adds is None and drops is None:
        return {
            "status": "unknown", "adds": adds, "drops": drops,
            "net_adds": None, "direction": "unknown",
        }
    if adds is None or drops is None:
        return {
            "status": "partial", "adds": adds, "drops": drops,
            "net_adds": None, "direction": "unknown",
            "method": (
                "Sleeper publishes bounded top-add and top-drop lists; absence from "
                "one list is unknown, not zero."
            ),
        }
    net = adds - drops
    return {
        "status": "observed", "adds": adds, "drops": drops, "net_adds": net,
        "direction": "rising" if net > 0 else "falling" if net < 0 else "steady",
    }


def _performance_trend(rows, scoring_mode, *, supported):
    if not supported:
        return {
            "status": "unknown",
            "direction": "unknown",
            "sample_size": 0,
            "recent_average": None,
            "prior_average": None,
            "change": None,
            "scoring_mode": scoring_mode,
            "basis": "Historical D/ST player statistics are not applicable",
            "method": _DST_STAT_NOTICE,
        }
    points = [
        selected
        for row in sorted(rows, key=lambda value: value.week)
        if (selected := _selected_fantasy_points(row, scoring_mode)) is not None
    ]
    basis = {
        "STD": "Standard fantasy points per recorded stat line",
        "HALF": "Half-PPR fantasy points per recorded stat line",
        "PPR": "PPR fantasy points per recorded stat line",
    }.get(scoring_mode, "Fantasy points unavailable for this scoring mode")
    if len(points) < 4:
        return {
            "status": "unknown", "direction": "unknown", "sample_size": len(points),
            "recent_average": None, "prior_average": None, "change": None,
            "scoring_mode": scoring_mode,
            "basis": basis,
            "method": "Requires at least two recent and two prior recorded stat lines.",
        }
    window = min(3, len(points) // 2)
    recent_average = _average(points[-window:])
    prior_average = _average(points[-(window * 2):-window])
    change = recent_average - prior_average
    return {
        "status": "observed",
        "direction": (
            "rising" if change >= 1.0 else "falling" if change <= -1.0 else "steady"
        ),
        "sample_size": window * 2,
        "recent_average": recent_average,
        "prior_average": prior_average,
        "change": change,
        "scoring_mode": scoring_mode,
        "basis": basis,
        "method": (
            f"Compares the latest {window} recorded stat lines with the preceding {window}; "
            "changes smaller than one point per game are steady."
        ),
    }


def _selected_fantasy_points(row, scoring_mode):
    if scoring_mode == "STD":
        return row.fantasy_points_standard
    if scoring_mode == "PPR":
        return row.fantasy_points_ppr
    if scoring_mode == "HALF":
        if row.fantasy_points_standard is None or row.fantasy_points_ppr is None:
            return None
        return (row.fantasy_points_standard + row.fantasy_points_ppr) / 2.0
    return None


def _availability_summary(profile, snapshot):
    coverage = [row.to_record() for row in snapshot.injury_history_availability]
    observed_seasons, events, qualifying_events, played, exposure = (
        _availability_evidence(profile, snapshot)
    )
    unavailable_seasons = [
        row.season
        for row in snapshot.injury_history_availability
        if row.availability != "observed"
    ]
    status_weeks = {
        value: [
            {"season": row.season, "week": row.week}
            for row in qualifying_events
            if row.report_status == value
        ]
        for value in ("out", "doubtful", "questionable")
    }
    burden = _availability_burden_record(
        qualifying_events,
        exposure,
        observed_seasons,
        snapshot.season,
    )
    observed_stat_seasons = set()
    if snapshot.current_stats_availability == "observed":
        observed_stat_seasons.add(snapshot.season)
    if snapshot.previous_stats_availability == "observed":
        observed_stat_seasons.add(snapshot.season - 1)
    out_without_game = [
        {"season": row.season, "week": row.week}
        for row in qualifying_events
        if row.report_status == "out"
        and row.season in observed_stat_seasons
        and (row.season, row.week) not in played
    ]
    body_areas = _body_area_counts(events)
    exposure_status = (
        "report_evidence"
        if qualifying_events
        else "sufficient"
        if len(exposure) >= _MIN_LOWER_TIER_EXPOSURE
        else "insufficient"
        if exposure
        else "none"
    )
    return {
        **burden,
        "method": AVAILABILITY_METHOD,
        "seasons": coverage,
        "seasons_observed": sorted(observed_seasons, reverse=True),
        "seasons_unavailable": sorted(unavailable_seasons, reverse=True),
        "player_evidence_seasons": sorted(
            {row.season for row in events} | {season for season, _ in exposure},
            reverse=True,
        ),
        "recorded_stat_line_exposure": [
            {"season": season, "week": week} for season, week in exposure
        ],
        "exposure_status": exposure_status,
        "minimum_exposure_for_lower_tier": _MIN_LOWER_TIER_EXPOSURE,
        "recorded_stat_lines": _recorded_stat_lines(profile, snapshot),
        "distinct_report_weeks": len(
            {(row.season, row.week) for row in qualifying_events}
        ),
        "out_weeks": status_weeks["out"],
        "doubtful_weeks": status_weeks["doubtful"],
        "questionable_weeks": status_weeks["questionable"],
        "documented_inactive_weeks": None,
        "out_report_without_recorded_stat_line": out_without_game,
        "affected_body_areas": body_areas,
        "recurrent_body_areas": [row for row in body_areas if row["documented_weeks"] > 1],
        "availability_contexts": _availability_context_counts(events),
        "current_designation": {
            "status": profile.injury_status,
            "body_part": profile.injury_body_part,
            "practice_participation": profile.practice_participation,
        },
        "weekly_evidence": [row.to_record() for row in events],
    }


def _availability_evidence(profile, snapshot):
    observed_seasons = {
        row.season
        for row in snapshot.injury_history_availability
        if row.availability == "observed"
    }
    events = [
        row for row in profile.availability_history if row.season in observed_seasons
    ]
    qualifying_events = _qualifying_designation_events(events)
    played = {
        (row.season, row.week)
        for row in (*profile.current_season_stats, *profile.previous_season_stats)
    }
    exposure = sorted(pair for pair in played if pair[0] in observed_seasons)
    return observed_seasons, events, qualifying_events, played, exposure


def _availability_burden_record(events, exposure, observed_seasons, current_season):
    score, tier, status = _availability_burden(
        events, exposure, observed_seasons, current_season
    )
    return {"status": status, "burden_index": score, "burden_tier": tier}


def _availability_burden(events, exposure, observed_seasons, current_season):
    if (
        not observed_seasons
        or (not events and len(exposure) < _MIN_LOWER_TIER_EXPOSURE)
    ):
        return None, "unknown", "unknown"
    season_weights = {current_season: 1.0, current_season - 1: 0.65, current_season - 2: 0.4}
    burden = fsum(
        season_weights.get(row.season, 0.0)
        * _DESIGNATION_WEIGHTS[row.report_status]
        for row in events
    )
    score = min(100.0, round(20.0 * burden, 1))
    tier = "lower" if score < 15 else "moderate" if score < 35 else "elevated"
    return score, tier, "observed"


def _qualifying_designation_events(events):
    return tuple(
        row
        for row in events
        if row.report_status in _DESIGNATION_WEIGHTS
        and _has_injury_coded_label(row)
    )


def _has_injury_coded_label(row):
    labels = _report_first_labels(row)
    return bool(labels) and any(
        _availability_context(value) is None for value in labels
    )


def _report_first_labels(row):
    report = tuple(
        value for value in (
            row.report_primary_injury, row.report_secondary_injury,
        )
        if value is not None and value.strip()
    )
    if report:
        return report
    return tuple(
        value for value in (
            row.practice_primary_injury, row.practice_secondary_injury,
        )
        if value is not None and value.strip()
    )


def _recorded_stat_lines(profile, snapshot):
    return [
        {
            "season": season,
            "count": len(rows) if availability == "observed" else None,
        }
        for season, rows, availability in (
            (snapshot.season, profile.current_season_stats, snapshot.current_stats_availability),
            (snapshot.season - 1, profile.previous_season_stats, snapshot.previous_stats_availability),
        )
    ]


def _body_area_counts(events):
    weeks_by_area = defaultdict(set)
    labels = {}
    for row in events:
        areas = {
            body_area
            for value in _report_first_labels(row)
            for body_area in _injury_body_areas(value)
        }
        for area in areas:
            key = area.casefold()
            labels.setdefault(key, area)
            weeks_by_area[key].add((row.season, row.week))
    return [
        {"body_area": labels[key], "documented_weeks": len(weeks)}
        for key, weeks in sorted(
            weeks_by_area.items(),
            key=lambda item: (-len(item[1]), labels[item[0]].casefold()),
        )
    ]


def _availability_context_counts(events):
    weeks_by_context = defaultdict(set)
    for row in events:
        for value in {
            row.report_primary_injury,
            row.report_secondary_injury,
            row.practice_primary_injury,
            row.practice_secondary_injury,
        }:
            context = _availability_context(value)
            if context is not None:
                weeks_by_context[context].add((row.season, row.week))
    return [
        {"context": context, "documented_weeks": len(weeks)}
        for context, weeks in sorted(
            weeks_by_context.items(), key=lambda item: (-len(item[1]), item[0])
        )
    ]


def _availability_context(value):
    if value is None:
        return None
    text = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    if not text:
        return None
    explicitly_non_injury = False
    for prefix in ("not injury related", "nir"):
        if text == prefix:
            return "Not injury related"
        if text.startswith(prefix + " "):
            explicitly_non_injury = True
            text = text.removeprefix(prefix).strip()
            break
    if text == "illness" or text.startswith("illness covid"):
        return "Illness"
    if text in {"personal", "personal matter"} or (
        explicitly_non_injury and text.startswith("personal ")
    ):
        return "Personal matter"
    if text in {"rest", "resting player", "veteran rest"}:
        return "Rest"
    if text in {
        "coach decision",
        "coach s decision",
        "coaches decision",
        "coaching decision",
    }:
        return "Coach decision"
    if text == "travel" or (explicitly_non_injury and text.startswith("travel ")):
        return "Travel"
    if explicitly_non_injury:
        return "Not injury related"
    return None


_NIR_BRACKET_SUFFIX = re.compile(
    r"\s*\[\s*(?:not[\s-]+injury[\s-]+related|nir)\b[^\]]*\]\s*$",
    re.IGNORECASE,
)


_BODY_AREA_PATTERNS = (
    (re.compile(r"\babdomen\b|\babdominal\b|\bstomach\b", re.IGNORECASE), "Abdomen"),
    (re.compile(r"\bachilles\b", re.IGNORECASE), "Achilles"),
    (re.compile(r"\b(?:acl|mcl|pcl|lcl|meniscus|knees?)\b", re.IGNORECASE), "Knee"),
    (re.compile(r"\bankles?\b", re.IGNORECASE), "Ankle"),
    (re.compile(r"\bbacks?\b", re.IGNORECASE), "Back"),
    (re.compile(r"\bbiceps?\b", re.IGNORECASE), "Biceps"),
    (re.compile(r"\bcalves?\b|\bcalf\b", re.IGNORECASE), "Calf"),
    (re.compile(r"\bchests?\b|\bsternum\b", re.IGNORECASE), "Chest"),
    (re.compile(r"\bclavicle\b|\bcollarbone\b", re.IGNORECASE), "Collarbone"),
    (re.compile(r"\bconcussions?\b", re.IGNORECASE), "Concussion"),
    (re.compile(r"\belbows?\b", re.IGNORECASE), "Elbow"),
    (re.compile(r"\beyes?\b", re.IGNORECASE), "Eye"),
    (re.compile(r"\bfaces?\b|\bjaws?\b", re.IGNORECASE), "Face"),
    (re.compile(r"\bfingers?\b", re.IGNORECASE), "Finger"),
    (re.compile(r"\bfeet\b|\bfoot\b", re.IGNORECASE), "Foot"),
    (re.compile(r"\bforearms?\b", re.IGNORECASE), "Forearm"),
    (re.compile(r"\bglutes?\b|\bgluteal\b", re.IGNORECASE), "Glute"),
    (re.compile(r"\bgroins?\b", re.IGNORECASE), "Groin"),
    (re.compile(r"\bhamstrings?\b", re.IGNORECASE), "Hamstring"),
    (re.compile(r"\bhands?\b", re.IGNORECASE), "Hand"),
    (re.compile(r"\bheads?\b", re.IGNORECASE), "Head"),
    (re.compile(r"\bheels?\b", re.IGNORECASE), "Heel"),
    (re.compile(r"\bhips?\b", re.IGNORECASE), "Hip"),
    (re.compile(r"\bhernias?\b", re.IGNORECASE), "Hernia"),
    (re.compile(r"\bkidneys?\b", re.IGNORECASE), "Kidney"),
    (re.compile(r"\blegs?\b|\bshin\b", re.IGNORECASE), "Leg"),
    (re.compile(r"\bfibula\b|\btibia\b", re.IGNORECASE), "Lower leg"),
    (re.compile(r"\blungs?\b", re.IGNORECASE), "Lung"),
    (re.compile(r"\bnecks?\b", re.IGNORECASE), "Neck"),
    (re.compile(r"\bobliques?\b", re.IGNORECASE), "Oblique"),
    (re.compile(r"\bpectorals?\b|\bpec\b", re.IGNORECASE), "Pectoral"),
    (re.compile(r"\bpelvis\b|\bpelvic\b", re.IGNORECASE), "Pelvis"),
    (re.compile(r"\bquadriceps?\b|\bquad\b", re.IGNORECASE), "Quadriceps"),
    (re.compile(r"\bribs?\b", re.IGNORECASE), "Rib"),
    (re.compile(r"\bshoulders?\b", re.IGNORECASE), "Shoulder"),
    (re.compile(r"\bthighs?\b", re.IGNORECASE), "Thigh"),
    (re.compile(r"\bthroats?\b", re.IGNORECASE), "Throat"),
    (re.compile(r"\bthumbs?\b", re.IGNORECASE), "Thumb"),
    (re.compile(r"\btoes?\b|\bturf toe\b", re.IGNORECASE), "Toe"),
    (re.compile(r"\btriceps?\b", re.IGNORECASE), "Triceps"),
    (re.compile(r"\bwrists?\b", re.IGNORECASE), "Wrist"),
)


def _injury_body_areas(value):
    if value is None or _availability_context(value) is not None:
        return ()
    label = _NIR_BRACKET_SUFFIX.sub("", value).strip()
    if not label:
        return ()
    return tuple(
        canonical
        for pattern, canonical in _BODY_AREA_PATTERNS
        if pattern.search(label)
    )


def _average(values):
    return fsum(values) / len(values)


__all__ = (
    "PROFILE_SCOPE_NOTICE",
    "assign_player_ranks",
    "outside_calculation_record",
    "profile_catalog_record",
    "profile_record",
)
