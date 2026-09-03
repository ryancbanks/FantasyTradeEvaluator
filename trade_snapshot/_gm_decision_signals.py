"""Independent, evidence-bounded decision signals for GM Insights."""

from collections import Counter
from statistics import fmean

from ._gm_statistics import partial_pool


def deal_accessibility(
    *,
    completed_trades,
    trade_weeks,
    unique_partner_count,
    package_shapes,
    observed_weeks,
    possible_partner_count,
    activity,
    coverage_complete,
):
    """Describe access to completed deals without estimating offer acceptance."""

    count = int(completed_trades)
    last_week = max(trade_weeks, default=None)
    weeks_since = (
        None
        if last_week is None or observed_weeks == 0 or last_week > observed_weeks
        else observed_weeks - last_week
    )
    partner_breadth = unique_partner_count / max(1, possible_partner_count)
    distinct_shapes = len(set(package_shapes))
    activity_percentile = activity["trades_per_10_weeks"]["league_percentile"]
    if count == 0:
        label = "No completed-deal evidence yet"
    elif activity_percentile is None:
        label = "Completed-deal accessibility unavailable"
    elif activity_percentile >= 0.75:
        label = "Higher observed completed-trade activity"
    elif activity_percentile <= 0.25:
        label = "Lower observed completed-trade activity"
    else:
        label = "Typical observed completed-trade activity"
    if unique_partner_count == 0:
        partner_label = "No completed partners observed"
    elif partner_breadth >= 0.5:
        partner_label = "Broad completed-partner history"
    else:
        partner_label = "Concentrated completed-partner history"
    if distinct_shapes == 0:
        variety_label = "No completed package shapes observed"
    elif distinct_shapes == 1:
        variety_label = "One completed package shape observed"
    else:
        variety_label = "Multiple completed package shapes observed"
    primary_metric = activity["next_two_week_trade_propensity"]
    if primary_metric["estimate"] is None:
        status = "unavailable"
    elif coverage_complete and count >= 3:
        status = "observed_tendency"
    else:
        status = "descriptive"
    return {
        "status": status,
        "label": label,
        "primary_metric": primary_metric,
        "observed_rate": activity["trades_per_10_weeks"],
        "supporting_facets": {
            "recency": {
                "last_completed_trade_scoring_period": last_week,
                "observed_scoring_periods_since": weeks_since,
            },
            "partner_breadth": {
                "unique_completed_partners": unique_partner_count,
                "possible_partners": possible_partner_count,
                "share_0_to_1": partner_breadth,
                "label": partner_label,
            },
            "package_variety": {
                "distinct_completed_headcount_shapes": distinct_shapes,
                "label": variety_label,
            },
        },
        "confidence": activity["next_two_week_trade_propensity"]["confidence"],
        "limitations": [
            "This predicts only another completed-trade week from observed activity, not whether an offer will be accepted.",
            "Recency, partner breadth, and package variety are separate descriptive facets and are not blended into a score.",
        ],
    }


def counterparty_value_opportunity(value_summary):
    """Reverse only the at-time team edge; never blend in activity evidence."""

    source = value_summary["relative_power_edge"]
    if source["estimate"] is None:
        return {
            "status": "unavailable",
            "label": "Not enough contemporaneous trade-value evidence",
            "relative_power_opportunity": _unavailable_metric(
                "counterparty_relative_power_opportunity",
                "No contemporaneous team edge is available to reverse.",
            ),
            "formula": "negative_of_team_contemporaneous_relative_power_edge",
            "limitations": ["No activity or roster-behavior input is used."],
        }
    interval = source["interval"]
    reversed_interval = None if interval is None else {
        **interval,
        "lower": -interval["upper"],
        "upper": -interval["lower"],
    }
    estimate = -source["estimate"]
    if (
        value_summary["status"] != "available"
        or source["confidence"]["status"] not in {"moderate", "strong"}
    ):
        label = "Not enough contemporaneous trades for an opportunity tendency"
    elif reversed_interval is not None and reversed_interval["lower"] > 0:
        label = "Historically favorable contemporaneous value for counterparties"
    elif reversed_interval is not None and reversed_interval["upper"] < 0:
        label = "Historically unfavorable contemporaneous value for counterparties"
    else:
        label = "No clear contemporaneous counterparty value opportunity"
    opportunity = {
        **source,
        "metric_id": "counterparty_relative_power_opportunity",
        "estimate": estimate,
        "league_percentile": (
            None
            if source["league_percentile"] is None
            else 1 - source["league_percentile"]
        ),
        "interval": reversed_interval,
        "sample": dict(source["sample"]),
        "evidence": dict(source["evidence"]),
        "confidence": {
            **source["confidence"],
            "reasons": list(source["confidence"]["reasons"]),
        },
        "limitations": [
            *source["limitations"],
            "This is exactly the negative of the team's at-time relative edge; no activity input is used.",
        ],
    }
    return {
        "status": value_summary["status"],
        "label": label,
        "relative_power_opportunity": opportunity,
        "formula": "negative_of_team_contemporaneous_relative_power_edge",
        "limitations": [
            "This describes valued completed trades, not the terms of a future offer."
        ],
    }


def hindsight_value_drift(
    rows,
    league_eligible_drifts,
    *,
    neutral_band,
    coverage_complete,
):
    """Aggregate only injury-cleared identical-context current revaluations."""

    comparable = []
    excluded = Counter()
    for valuation, at_time in rows:
        current = valuation.current_revaluation
        if current is None:
            excluded[
                valuation.current_revaluation_unavailable_reason
                or "current_revaluation_unavailable"
            ] += 1
            continue
        current_outcome = next(
            outcome for outcome in current.outcomes if outcome.team_id == at_time.team_id
        )
        if not current.foresight_eligible:
            excluded.update(current.foresight_ineligibility_reasons)
            continue
        comparable.append((at_time, current_outcome))

    count = len(comparable)
    base = {
        "raw_current_revalued_trades": sum(
            valuation.current_revaluation is not None for valuation, _ in rows
        ),
        "foresight_eligible_trades": count,
        "excluded_reasons": dict(sorted(excluded.items())),
        "limitations": [
            "This is hindsight current-value drift, not a causal measure of managerial skill.",
            "Trades with a captured physical injury or incomplete weekly health evidence are excluded before any foresight label.",
            "Role changes, performance variance, and information learned later can still move player values.",
            "Weekly status snapshots cannot prove that no health change occurred between captures.",
            "IR slots and missing projections are never treated as health evidence.",
        ],
    }
    if count < 3:
        return {
            **base,
            "status": "insufficient_sample" if rows else "unavailable",
            "label": "Not enough injury-cleared comparable trades",
            "plain_language_alias": None,
            "then_relative_power_edge_mean": None,
            "current_relative_power_edge_mean": None,
            "relative_power_edge_drift": _unavailable_metric(
                "hindsight_relative_power_edge_drift",
                "At least three injury-cleared, same-context revaluations are required.",
                sample_count=count,
            ),
        }

    then_edges = [at_time.relative_power_edge for at_time, _ in comparable]
    current_edges = [current.relative_power_edge for _, current in comparable]
    drifts = [current.relative_power_edge_drift for _, current in comparable]
    pooled = partial_pool(drifts, league_eligible_drifts)
    assert pooled is not None
    low80, high80 = pooled["interval_80"]
    low95, high95 = pooled["interval_95"]
    if not coverage_complete:
        label = "Hindsight value drift from partial transaction history"
        alias = None
        confidence = "descriptive_only"
    elif low80 > neutral_band:
        label = "Positive hindsight value drift"
        alias = "good foresight signal"
        confidence = (
            "strong" if count >= 5 and low95 > neutral_band else "moderate"
        )
    elif high80 < -neutral_band:
        label = "Negative hindsight value drift"
        alias = "bad foresight signal"
        confidence = (
            "strong" if count >= 5 and high95 < -neutral_band else "moderate"
        )
    else:
        label = "Mixed / no clear hindsight value drift"
        alias = None
        confidence = "uncertain"
    return {
        **base,
        "status": "available" if coverage_complete else "partial",
        "label": label,
        "plain_language_alias": alias,
        "then_relative_power_edge_mean": fmean(then_edges),
        "current_relative_power_edge_mean": fmean(current_edges),
        "relative_power_edge_drift": _metric(
            "hindsight_relative_power_edge_drift",
            pooled["estimate"],
            pooled["interval_95"],
            count,
            confidence,
            coverage_complete=coverage_complete,
        ),
    }


def _metric(
    metric_id,
    estimate,
    interval,
    sample_count,
    confidence,
    *,
    coverage_complete,
):
    return {
        "metric_id": metric_id,
        "estimate": estimate,
        "unit": "power_points_per_trade",
        "league_percentile": None,
        "interval": {
            "lower": interval[0],
            "upper": interval[1],
            "level": 0.95,
            "method": "normal_partial_pooling_v1",
        },
        "sample": {
            "raw_n": sample_count,
            "effective_n": sample_count,
            "exposure_team_weeks": 0,
        },
        "evidence": {"coverage_complete": coverage_complete},
        "confidence": {"status": confidence, "reasons": []},
        "limitations": [
            "Only same-package, same-pre-trade-roster comparisons with complete ACTIVE health history are included."
        ],
    }


def _unavailable_metric(metric_id, reason, *, sample_count=0):
    return {
        "metric_id": metric_id,
        "estimate": None,
        "unit": "power_points_per_trade",
        "league_percentile": None,
        "interval": None,
        "sample": {
            "raw_n": sample_count,
            "effective_n": sample_count,
            "exposure_team_weeks": 0,
        },
        "evidence": {"coverage_complete": False},
        "confidence": {"status": "unavailable", "reasons": [reason]},
        "limitations": [],
    }


__all__ = (
    "counterparty_value_opportunity",
    "deal_accessibility",
    "hindsight_value_drift",
)
