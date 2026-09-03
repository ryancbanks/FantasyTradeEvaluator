"""Pure ranking and selection rules for simulated trade-timing options."""


def build_recommendation(
    options,
    first_week,
    *,
    shortlist_is_exhaustive=True,
    alternative_limit=3,
):
    mutual = [row for row in options if row["mutual_playoff_gain"]]
    current = [row for row in mutual if row["effective_week"] == first_week]
    future = [row for row in mutual if row["effective_week"] != first_week]
    default = max(current, key=_default_option_key, default=None)
    watch = max(future, key=_watch_option_key, default=None)
    frontier = _pareto_frontier(mutual)
    selected = {id(row) for row in (default, watch) if row is not None}
    alternatives = [
        row
        for row in sorted(frontier, key=_default_option_key, reverse=True)
        if id(row) not in selected
    ][:alternative_limit]
    status = (
        "current_window_candidate_verification_required"
        if default is not None
        else "conditional_watch_only"
        if watch is not None
        else "no_mutual_gain_in_exhaustive_screen"
        if options and shortlist_is_exhaustive
        else "no_mutual_gain_in_simulated_shortlist"
        if options
        else "no_power_screened_candidate"
    )
    return {
        "status": status,
        "default_plan": default,
        "conditional_watch_plan": watch,
        "alternatives": alternatives,
        "simulated_option_count": len(options),
        "mutual_playoff_gain_option_count": len(mutual),
        "shortlist_is_exhaustive": shortlist_is_exhaustive,
        "future_plan_is_recommendation": False,
        "future_plan_reason": "league_trade_deadline_not_captured",
    }


def dominates(left, right):
    left_values = (
        left["primary_playoff_probability_delta"],
        left["partner_playoff_probability_delta"],
        left["pressure_percentile"] or 0.0,
        -max(left["delay_cost_primary"], 0.0),
    )
    right_values = (
        right["primary_playoff_probability_delta"],
        right["partner_playoff_probability_delta"],
        right["pressure_percentile"] or 0.0,
        -max(right["delay_cost_primary"], 0.0),
    )
    return all(a >= b for a, b in zip(left_values, right_values, strict=True)) and any(
        a > b for a, b in zip(left_values, right_values, strict=True)
    )


def partner_plan_key(plan):
    recommendation = plan["recommendation"]
    default = recommendation["default_plan"]
    watch = recommendation["conditional_watch_plan"]
    best = default or watch
    return (
        0 if default else 1 if watch else 2,
        -(best["primary_playoff_probability_delta"] if best else -1.0),
        -(best["partner_playoff_probability_delta"] if best else -1.0),
        plan["partner_team_name"].casefold(),
        plan["partner_team_id"],
    )


def _pareto_frontier(options):
    return [
        row
        for row in options
        if not any(dominates(other, row) for other in options if other is not row)
    ]


def _default_option_key(row):
    return (
        row["primary_playoff_probability_delta"],
        row["partner_playoff_probability_delta"],
        -row["effective_week"],
    )


def _watch_option_key(row):
    return (
        row["pressure_percentile"] or 0.0,
        row["trigger"]["probability"] or 0.0,
        row["primary_playoff_probability_delta"],
        row["partner_playoff_probability_delta"],
        -max(row["delay_cost_primary"], 0.0),
    )


__all__ = ("build_recommendation", "dominates", "partner_plan_key")
