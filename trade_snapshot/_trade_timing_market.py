"""Projection-shape context for trade-timing candidates."""

from statistics import mean

from .projections import ProjectionStatus


def market_pattern(bundle, swap, effective_week):
    incoming = _projection_position(
        bundle, swap.counterparty_player_id, effective_week
    )
    outgoing = _projection_position(bundle, swap.primary_player_id, effective_week)
    primary_actions = []
    if incoming["projection_band"] == "low":
        primary_actions.append("primary_buys_projected_low")
    if outgoing["projection_band"] == "high":
        primary_actions.append("primary_sells_projected_high")
    partner_actions = []
    if outgoing["projection_band"] == "high":
        partner_actions.append("partner_buys_projected_high")
    if incoming["projection_band"] == "low":
        partner_actions.append("partner_sells_projected_low")
    return {
        "basis": "within_player_remaining_active_week_projection_percentile",
        "not_market_price_or_future_ecr": True,
        "primary_receives": incoming,
        "primary_sends": outgoing,
        "primary_pattern": primary_actions
        or ["no_clear_projected_high_low_pattern"],
        "partner_pattern": partner_actions
        or ["no_clear_projected_high_low_pattern"],
        "summary": market_summary(primary_actions, partner_actions),
    }


def market_summary(primary_actions, partner_actions):
    primary = (
        "You buy at a projected low and sell at a projected high"
        if len(primary_actions) == 2
        else "You buy at a projected low"
        if primary_actions == ["primary_buys_projected_low"]
        else "You sell at a projected high"
        if primary_actions == ["primary_sells_projected_high"]
        else "Your side has no clear projected high/low pattern"
    )
    partner = (
        "the partner buys at a projected high and sells at a projected low"
        if len(partner_actions) == 2
        else "the partner buys at a projected high"
        if partner_actions == ["partner_buys_projected_high"]
        else "the partner sells at a projected low"
        if partner_actions == ["partner_sells_projected_low"]
        else "the partner has no complete buy-high/sell-low projection pattern"
    )
    return f"{primary}; {partner}."


def _projection_position(bundle, player_id, week):
    rows = [
        row
        for row in bundle.projections
        if row.canonical_player_id == player_id
        and row.week >= week
        and row.status is not ProjectionStatus.BYE
        and row.projected_fantasy_points is not None
    ]
    by_week = {row.week: row.projected_fantasy_points for row in rows}
    target = by_week.get(week)
    values = sorted(by_week.values())
    percentile = None if target is None else _value_percentile(target, values)
    band = (
        "unavailable"
        if percentile is None
        else "low"
        if percentile <= 0.35
        else "high"
        if percentile >= 0.65
        else "middle"
    )
    return {
        "player_id": player_id,
        "player_name": bundle.player_names[player_id],
        "effective_week": week,
        "projected_points": target,
        "remaining_active_week_mean": mean(values) if values else None,
        "within_player_percentile": percentile,
        "projection_band": band,
    }


def _value_percentile(value, values):
    if len(values) <= 1:
        return 0.5
    less = sum(candidate < value for candidate in values)
    equal = sum(candidate == value for candidate in values)
    return (less + (equal - 1) / 2) / (len(values) - 1)


__all__ = ("market_pattern", "market_summary")
