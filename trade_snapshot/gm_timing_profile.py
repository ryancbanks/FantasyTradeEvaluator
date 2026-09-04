"""Evidence-bounded completed-deal timing profiles.

The profile treats a scoring period as a binary decision window: a team either
participated in at least one eventually completed trade assigned to that period
or it did not.  It deliberately cannot estimate offer acceptance because the
history ledger contains neither rejected offers nor verified accepter roles.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

from ._gm_statistics import wilson_interval
from ._league_history_evidence import captured_transaction_evidence
from ._record_trend import (
    RECORD_SLOPE_NEUTRAL_BAND,
    record_slope_direction,
    trailing_record_slope,
)
from ._scenario_random import content_id
from .league_history import (
    HISTORY_CAPTURE_BINDING_TOLERANCE,
    HistoryTransactionKind,
    LeagueHistorySnapshot,
)
from .league_state import LeagueState


_SCHEMA_VERSION = 2
_WILSON_80_Z = 1.281551565545
_WILSON_95_Z = 1.95996398454
_MIN_COMPARISON_EXPOSURES = 5
_MIN_COMPARISON_ACTIVE_WINDOWS = 3


def build_completed_deal_timing_profiles(
    state: LeagueState,
    history: LeagueHistorySnapshot | None,
) -> dict[str, dict[str, object]]:
    """Return JSON-ready, team-keyed completed-trade timing profiles.

    A trade assigned to effective week ``W`` uses only results through ``W-1``.
    Mixed provider timestamp meanings are returned as separate strata and are
    never combined into a timing rate.
    """

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if history is not None and not isinstance(history, LeagueHistorySnapshot):
        raise ValueError("history must be a LeagueHistorySnapshot or null")
    if history is not None and history.season != state.season:
        raise ValueError("history season does not match league state")

    team_ids = tuple(sorted(team.team_id for team in state.teams))
    as_of = None if history is None else history.bundle_captured_at
    captures = (
        ()
        if history is None
        else tuple(row for row in history.captures if row.captured_at <= as_of)
    )
    _validate_team_identity(team_ids, captures)
    latest = max(
        captures,
        key=lambda row: (row.captured_at, row.capture_id),
        default=None,
    )
    coverage_status = _coverage_status(state, latest, as_of)
    normalized = coverage_status == "complete_and_fresh"
    transactions = _visible_completed_trades(captures, as_of)
    timestamp_bases = tuple(
        sorted({row.timestamp_basis.value for row in transactions})
    )
    trajectory = _record_trajectory(state) if state.completed_history_is_usable else {}
    elapsed_periods = tuple(range(1, state.first_remaining_week))

    profiles: dict[str, dict[str, object]] = {}
    for team_id in team_ids:
        team_trades = tuple(
            row for row in transactions if team_id in row.participant_team_ids
        )
        by_basis = {
            basis: _timing_stratum(
                tuple(row for row in team_trades if row.timestamp_basis.value == basis),
                team_id,
                elapsed_periods,
                trajectory.get(team_id, ()),
                normalized,
            )
            for basis in timestamp_bases
        }
        if len(timestamp_bases) == 1:
            selected_basis = timestamp_bases[0]
            timing = by_basis[selected_basis]
        elif not timestamp_bases:
            selected_basis = None
            timing = _timing_stratum(
                (), team_id, elapsed_periods, trajectory.get(team_id, ()), normalized
            )
        else:
            selected_basis = None
            timing = _mixed_basis_timing()

        profiles[team_id] = {
            "schema_version": _SCHEMA_VERSION,
            "team_id": team_id,
            "status": (
                "descriptive"
                if normalized
                else "partial"
                if captures
                else "unavailable"
            ),
            "as_of": None if as_of is None else _iso(as_of),
            "analysis_as_of": None if as_of is None else _iso(as_of),
            "evidence": _evidence_record(
                state, history, captures, latest, team_trades, team_id
            ),
            "manager_acceptance_modeled": False,
            "use_for_personalization": False,
            "behavioral_label": None,
            "health_screen_status": "unknown_not_aligned_to_decision_windows",
            "coverage": {
                "status": coverage_status,
                "normalized_rates_available": normalized,
                "completed_history_usable": state.completed_history_is_usable,
                "elapsed_scoring_period_count": len(elapsed_periods),
                "latest_capture_id": None if latest is None else latest.capture_id,
                "transaction_history_complete": (
                    False if latest is None else latest.transaction_history_complete
                ),
                "roster_history_complete": (
                    False if latest is None else latest.roster_complete
                ),
                "lineup_history_complete": (
                    False if latest is None else latest.lineup_complete
                ),
                "incomplete_dimensions": _incomplete_dimensions(
                    state, latest, coverage_status
                ),
            },
            "trade_legality": {
                "status": "not_captured",
                "trade_deadline_status": "not_captured",
                "transaction_processing_rules_status": "not_captured",
                "player_lock_status": "not_captured",
                "undroppable_player_status": "not_captured",
                "host_legality_verified": False,
            },
            "scoring_period_context": {
                "rule": (
                    "A trade effective in week W is classified only by the team's "
                    "completed record through week W-1."
                ),
                "elapsed_effective_weeks": list(elapsed_periods),
                "multiple_completed_trades_count_once": True,
                "trade_deadline_status": "not_captured",
                "record_slope": {
                    "method": "theil_sen_last_4_cumulative_win_equivalent_v1",
                    "minimum_points": 3,
                    "neutral_band_per_week": RECORD_SLOPE_NEUTRAL_BAND,
                },
            },
            "observed_record_trajectory": list(trajectory.get(team_id, ())),
            "completed_trade_count": len(team_trades),
            "timestamp_bases": list(timestamp_bases),
            "selected_timestamp_basis": selected_basis,
            "timing": timing,
            "timing_by_timestamp_basis": by_basis,
            "limitations": [
                "Completed trades do not reveal rejected offers or the probability that a new offer will be accepted.",
                "Participant history does not prove which team initiated or accepted a completed trade.",
                "Historical health is not reliably aligned to each decision window, so directional behavioral labels and personalization are disabled.",
                "Associations with prior results are descriptive and do not establish that winning or losing caused a trade.",
                "The trade deadline is not captured, so elapsed scoring periods may include weeks when trades were not allowed.",
                "Transaction processing rules, player locks, undroppable status, and historical trade legality are not captured.",
                "Roster and lineup capture completeness is disclosed but is not evidence of historical player health at each decision window.",
            ],
        }
        json.dumps(profiles[team_id], allow_nan=False, sort_keys=True)
    return profiles


def _evidence_record(state, history, captures, latest, transactions, team_id):
    timestamp_coverage = {
        "proposed_at": len(transactions),
        "accepted_at": sum(row.accepted_at is not None for row in transactions),
        "processed_at": sum(row.processed_at is not None for row in transactions),
        "expires_at": sum(row.expires_at is not None for row in transactions),
    }
    record = {
        "team_id": team_id,
        "host_snapshot_id": state.snapshot_id,
        "scoring_profile_id": state.scoring_profile_id,
        "history_revision": None if history is None else history.history_revision,
        "history_capture_ids": sorted(row.capture_id for row in captures),
        "latest_history_capture_id": None if latest is None else latest.capture_id,
        "completed_trade_transaction_ids": sorted(
            row.transaction_id for row in transactions
        ),
        "source_timestamp_coverage": timestamp_coverage,
    }
    return {
        **record,
        "evidence_id": content_id("gm-timing-evidence", record),
    }


def _incomplete_dimensions(state, latest, coverage_status):
    dimensions = []
    if coverage_status == "not_collected":
        dimensions.append("history_not_collected")
    elif coverage_status == "latest_capture_stale":
        dimensions.append("latest_capture_stale")
    if not state.completed_history_is_usable:
        dimensions.append("completed_matchups")
    if latest is None or not latest.transaction_history_complete:
        dimensions.append("transactions")
    if latest is None or not latest.roster_complete:
        dimensions.append("rosters")
    if latest is None or not latest.lineup_complete:
        dimensions.append("lineups")
    return dimensions


def _coverage_status(state, latest, as_of):
    if as_of is None or latest is None:
        return "not_collected"
    if not state.completed_history_is_usable:
        return "completed_matchups_unusable"
    if not latest.transaction_history_complete:
        return "transaction_history_partial"
    if (
        latest.captured_at > as_of
        or latest.coverage_end > as_of
        or as_of - latest.captured_at > HISTORY_CAPTURE_BINDING_TOLERANCE
        or as_of - latest.coverage_end > HISTORY_CAPTURE_BINDING_TOLERANCE
    ):
        return "latest_capture_stale"
    return "complete_and_fresh"


def _visible_completed_trades(captures, as_of):
    if as_of is None:
        return ()
    transactions, _ = captured_transaction_evidence(captures)
    return tuple(
        row
        for row in transactions
        if row.kind is HistoryTransactionKind.TRADE and row.recorded_at <= as_of
    )


def _validate_team_identity(team_ids, captures):
    if not captures:
        return
    latest = max(captures, key=lambda row: (row.captured_at, row.capture_id))
    captured_ids = tuple(sorted(team.team_id for team in latest.teams))
    if captured_ids != team_ids:
        raise ValueError("history teams do not match league state")


def _record_trajectory(state):
    outcomes = {team.team_id: {} for team in state.teams}
    for matchup in state.completed_matchups:
        if matchup.team1_score > matchup.team2_score:
            left, right = 1.0, 0.0
        elif matchup.team2_score > matchup.team1_score:
            left, right = 0.0, 1.0
        else:
            left = right = 0.5
        outcomes[matchup.team1_id][matchup.week] = left
        outcomes[matchup.team2_id][matchup.week] = right

    result = {}
    for team_id in sorted(outcomes):
        total = 0.0
        cumulative = []
        rows = []
        for week in range(1, state.first_remaining_week):
            points = outcomes[team_id][week]
            total += points
            cumulative.append(total / week)
            slope = trailing_record_slope(enumerate(cumulative, start=1))
            rows.append(
                {
                    "week": week,
                    "result": "win" if points == 1 else "loss" if points == 0 else "tie",
                    "win_equivalent": points,
                    "cumulative_win_equivalent_pct": cumulative[-1],
                    "record_slope_per_week": slope,
                    "record_slope_direction": record_slope_direction(
                        slope, unavailable="unavailable"
                    ),
                }
            )
        result[team_id] = tuple(rows)
    return result


def _timing_stratum(trades, team_id, elapsed_periods, trajectory, normalized):
    active_weeks = {
        row.effective_week
        for row in trades
        if row.effective_week in elapsed_periods and team_id in row.participant_team_ids
    }
    by_week = {row["week"]: row for row in trajectory}
    after_loss = tuple(
        week
        for week in elapsed_periods
        if week > 1 and by_week.get(week - 1, {}).get("result") == "loss"
    )
    after_nonloss = tuple(
        week
        for week in elapsed_periods
        if week > 1 and by_week.get(week - 1, {}).get("result") in {"win", "tie"}
    )
    downward = tuple(
        week
        for week in elapsed_periods
        if by_week.get(week - 1, {}).get("record_slope_direction") == "downward"
    )
    non_downward = tuple(
        week
        for week in elapsed_periods
        if by_week.get(week - 1, {}).get("record_slope_direction") in {"neutral", "upward"}
    )
    rates = {
        "unconditional": _rate(active_weeks, elapsed_periods, normalized),
        "after_loss": _rate(active_weeks, after_loss, normalized),
        "after_nonloss": _rate(active_weeks, after_nonloss, normalized),
        "downward": _rate(active_weeks, downward, normalized),
        "non_downward": _rate(active_weeks, non_downward, normalized),
    }
    status = "descriptive" if normalized and elapsed_periods else "unavailable"
    return {
        "status": status,
        "reason": (
            None
            if status == "descriptive"
            else "no_elapsed_scoring_periods"
            if normalized
            else "verified_completed_trade_timing_coverage_unavailable"
        ),
        "transaction_count": len(trades),
        "active_scoring_period_count": len(active_weeks),
        "active_effective_weeks": sorted(active_weeks),
        "rates": rates,
        "comparisons": {
            "after_loss_minus_nonloss": _comparison(
                rates["after_loss"], rates["after_nonloss"], normalized
            ),
            "downward_minus_non_downward": _comparison(
                rates["downward"], rates["non_downward"], normalized
            ),
        },
        "behavioral_label": None,
        "use_for_personalization": False,
    }


def _rate(active_weeks, exposed_weeks, normalized):
    exposed = tuple(exposed_weeks)
    successes = sum(week in active_weeks for week in exposed)
    estimate = (successes + 0.5) / (len(exposed) + 1) if normalized and exposed else None
    interval80 = (
        wilson_interval(successes, len(exposed), _WILSON_80_Z)
        if normalized and exposed
        else None
    )
    interval95 = (
        wilson_interval(successes, len(exposed), _WILSON_95_Z)
        if normalized and exposed
        else None
    )
    return {
        "estimate": estimate,
        "unit": "completed_trade_participation_per_elapsed_scoring_period",
        "successes": successes,
        "exposures": len(exposed),
        "estimate_method": "jeffreys_smoothed_rate_v1" if estimate is not None else None,
        "interval_80": _interval(interval80, 0.80),
        "interval_95": _interval(interval95, 0.95),
        "confidence": "descriptive_only" if estimate is not None else "unavailable",
    }


def _comparison(left, right, normalized):
    estimate = (
        left["estimate"] - right["estimate"]
        if left["estimate"] is not None and right["estimate"] is not None
        else None
    )
    interval80 = _difference_interval(left["interval_80"], right["interval_80"])
    interval95 = _difference_interval(left["interval_95"], right["interval_95"])
    sample_gate = (
        normalized
        and left["exposures"] >= _MIN_COMPARISON_EXPOSURES
        and right["exposures"] >= _MIN_COMPARISON_EXPOSURES
        and left["successes"] + right["successes"]
        >= _MIN_COMPARISON_ACTIVE_WINDOWS
    )
    clears80 = sample_gate and _excludes_zero(interval80)
    clears95 = sample_gate and _excludes_zero(interval95)
    separation = (
        "heuristic_95_bound_excludes_zero"
        if clears95
        else "heuristic_80_bound_excludes_zero"
        if clears80
        else "insufficient"
    )
    return {
        "estimate": estimate,
        "unit": "completed_trade_participation_rate_difference",
        "heuristic_difference_bound_80": interval80,
        "heuristic_difference_bound_95": interval95,
        "sample_gate_met": sample_gate,
        "descriptive_separation": separation,
        "nominal_difference_coverage_claimed": False,
        "confidence": "heuristic_descriptive_only",
        "behavioral_label": None,
        "use_for_personalization": False,
        "suppressed_reason": "historical_health_not_aligned_to_decision_windows",
    }


def _mixed_basis_timing():
    return {
        "status": "unavailable",
        "reason": "mixed_timestamp_bases_are_not_pooled",
        "rates": {},
        "comparisons": {},
        "behavioral_label": None,
        "use_for_personalization": False,
    }


def _interval(value, level):
    return (
        None
        if value is None
        else {"lower": value[0], "upper": value[1], "level": level, "method": "wilson_v1"}
    )


def _difference_interval(left, right):
    if left is None or right is None:
        return None
    return {
        "lower": max(-1.0, left["lower"] - right["upper"]),
        "upper": min(1.0, left["upper"] - right["lower"]),
        "level": left["level"],
        "method": "heuristic_difference_of_marginal_wilson_bounds_v1",
        "nominal_coverage_claimed": False,
    }


def _excludes_zero(interval):
    return interval is not None and (interval["lower"] > 0 or interval["upper"] < 0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ("build_completed_deal_timing_profiles",)
