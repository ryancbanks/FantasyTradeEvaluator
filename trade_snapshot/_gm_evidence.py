"""Per-transaction evidence formatting for General Manager Insights."""

from datetime import datetime, timezone

from .league_history import HistoryTransactionAssetKind


def build_trade_evidence(
    team_id,
    trades,
    valuations,
    unvalued_transactions,
    team_names,
    player_names,
    first_observed_at,
):
    """Return every completed trade in newest-first order."""

    valued = {row.transaction_id: row for row in valuations}
    rows = []
    ordered = sorted(
        trades,
        key=lambda row: (row.recorded_at, row.transaction_id),
        reverse=True,
    )
    for trade in ordered:
        sent = [
            _asset_label(asset, player_names)
            for asset in trade.assets
            if asset.from_team_id == team_id
        ]
        received = [
            _asset_label(asset, player_names)
            for asset in trade.assets
            if asset.to_team_id == team_id
        ]
        partners = [
            team_names.get(value, "Unknown team")
            for value in trade.participant_team_ids
            if value != team_id
        ]
        valuation = valued.get(trade.transaction_id)
        at_time = _at_time_record(valuation, team_id)
        current = _current_record(valuation, team_id)
        unavailable_reason = (
            unvalued_transactions.get(trade.transaction_id)
            if valuation is None
            else valuation.current_revaluation_unavailable_reason
        )
        comparison = _comparison_record(
            valuation, team_id, unavailable_reason
        )
        rows.append(
            {
                "source_event_at": _iso(trade.recorded_at),
                "first_observed_completed_at": (
                    None
                    if trade.transaction_id not in first_observed_at
                    else _iso(first_observed_at[trade.transaction_id])
                ),
                "timestamp_basis": trade.timestamp_basis.value,
                "scoring_period": trade.effective_week,
                "counterparties": partners,
                "sent": sent,
                "received": received,
                "valuation": {
                    # Preserve flat at-time fields while clients migrate to the
                    # explicit then/current records.
                    **({} if at_time is None else at_time),
                    "at_time": at_time,
                    "current_revaluation": current,
                    "comparison": comparison,
                },
            }
        )
    return rows


def _at_time_record(valuation, team_id):
    if valuation is None:
        return None
    outcome = next(row for row in valuation.outcomes if row.team_id == team_id)
    return {
        "status": valuation.methodology_status,
        "source_bundle_captured_at": _iso(valuation.source_bundle_captured_at),
        "valuation_lag_hours": valuation.valuation_lag_hours,
        "power_delta": outcome.power_delta,
        "relative_power_edge": outcome.relative_power_edge,
        "playoff_probability_delta": outcome.playoff_probability_delta,
        "playoff_probability_unavailable_reason": (
            valuation.playoff_unavailable_reason
        ),
    }


def _current_record(valuation, team_id):
    if valuation is None or valuation.current_revaluation is None:
        return None
    current = valuation.current_revaluation
    outcome = next(row for row in current.outcomes if row.team_id == team_id)
    return {
        "status": current.methodology_status,
        "selected_bundle_captured_at": _iso(current.bundle_captured_at),
        "power_delta": outcome.power_delta,
        "relative_power_edge": outcome.relative_power_edge,
    }


def _comparison_record(valuation, team_id, unavailable_reason):
    current = None if valuation is None else valuation.current_revaluation
    current_outcome = (
        None
        if current is None
        else next(row for row in current.outcomes if row.team_id == team_id)
    )
    return {
        "status": "comparable_raw" if current is not None else "unavailable",
        "relative_power_edge_drift": (
            None
            if current_outcome is None
            else current_outcome.relative_power_edge_drift
        ),
        "foresight_eligible": bool(
            current is not None and current.foresight_eligible
        ),
        "foresight_ineligibility_reasons": (
            [unavailable_reason]
            if current is None and unavailable_reason is not None
            else []
            if current is None
            else list(current.foresight_ineligibility_reasons)
        ),
        "interpretation": "hindsight_current_value_drift_not_at_time_fairness",
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _asset_label(asset, player_names):
    if asset.asset_kind is HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER:
        return "Unsupported non-player asset"
    if asset.canonical_player_id is None:
        return "Unresolved player"
    return player_names.get(asset.canonical_player_id, "Unknown player")


__all__ = ("build_trade_evidence",)
