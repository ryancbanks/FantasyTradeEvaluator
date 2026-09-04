"""Cohesive team-level calculations for General Manager Insights."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import fmean, median

from ._league_history_evidence import transaction_executed_by
from ._gm_statistics import (
    gini,
    partial_pool,
    percentile,
    poisson_rate_interval,
    predictive_active_probability,
    wilson_interval,
)
from .league_history import HistoryTransactionKind

_NEUTRAL_POWER_BAND = 0.1
_NONSTARTING_SLOTS = frozenset({"BENCH", "IR", "ROOKIE_RESERVE"})
_RESERVE_SLOTS = frozenset({"IR", "ROOKIE_RESERVE"})
_POSITION_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "K", "DST", "DL", "LB", "DB")


@dataclass(slots=True)
class _TeamFacts:
    team_id: str
    trades: tuple
    trade_weeks: frozenset[int]
    partners: Counter
    sent_sizes: tuple[int, ...]
    received_sizes: tuple[int, ...]
    sent_positions: Counter
    received_positions: Counter
    additions: tuple
    drops: tuple
    acquisition_positions: Counter
    roster_snapshots: tuple
    first_observed_at: dict[str, datetime]


def _team_facts(
    team_id,
    transactions,
    captures,
    positions,
    first_observed_at,
):
    trades = tuple(row for row in transactions if row.kind is HistoryTransactionKind.TRADE and team_id in row.participant_team_ids)
    partners = Counter()
    sent_sizes, received_sizes = [], []
    sent_positions, received_positions = Counter(), Counter()
    for trade in trades:
        partners.update(partner for partner in trade.participant_team_ids if partner != team_id)
        if any(asset.canonical_player_id is None for asset in trade.assets):
            continue
        sent = [asset for asset in trade.assets if asset.from_team_id == team_id]
        received = [asset for asset in trade.assets if asset.to_team_id == team_id]
        sent_sizes.append(len(sent))
        received_sizes.append(len(received))
        sent_positions.update(_asset_positions(sent, positions))
        received_positions.update(_asset_positions(received, positions))
    waiver_events = tuple(row for row in transactions if row.kind is HistoryTransactionKind.WAIVER and team_id in row.participant_team_ids)
    free_events = tuple(row for row in transactions if row.kind in {HistoryTransactionKind.FREE_AGENT, HistoryTransactionKind.DROP} and team_id in row.participant_team_ids)
    acquisition_events = (*waiver_events, *free_events)
    additions = tuple((event, asset) for event in acquisition_events for asset in event.assets if asset.to_team_id == team_id and asset.from_team_id != team_id)
    drops = tuple((event, asset) for event in acquisition_events for asset in event.assets if asset.from_team_id == team_id and asset.to_team_id != team_id)
    roster_snapshots = tuple((capture, roster) for capture in captures if capture.roster_complete for roster in capture.rosters if roster.team_id == team_id)
    return _TeamFacts(
        team_id,
        trades,
        frozenset(row.effective_week for row in trades),
        partners,
        tuple(sent_sizes),
        tuple(received_sizes),
        sent_positions,
        received_positions,
        additions,
        drops,
        Counter(_asset_positions((asset for _, asset in additions), positions)),
        roster_snapshots,
        dict(first_observed_at),
    )


def _trade_activity(row, weeks, league_rates, complete):
    count = len(row.trades)
    eligible_trades = tuple(
        event for event in row.trades if 1 <= event.effective_week <= weeks
    )
    eligible_count = len(eligible_trades)
    active = len({event.effective_week for event in eligible_trades})
    rate = league_rates[row.team_id]
    rate_interval = None if weeks == 0 else poisson_rate_interval(eligible_count, weeks)
    week_interval = None if weeks == 0 else wilson_interval(active, weeks)
    predicted = None if weeks == 0 else predictive_active_probability(active, weeks, 2)
    confidence = _rate_confidence(complete, weeks, eligible_count)
    return {
        "completed_trades": count,
        "frequency_eligible_completed_trades": eligible_count,
        "unique_partners": len(row.partners),
        "last_trade_at": (
            None
            if not row.trades
            else _iso(
                max(
                    transaction_executed_by(event, row.first_observed_at)
                    for event in row.trades
                )
            )
        ),
        "trades_per_10_weeks": _metric(
            "trades_per_10_weeks",
            rate,
            "completed_trades_per_10_observed_weeks",
            (
                None
                if rate is None
                else percentile(
                    rate,
                    (value for value in league_rates.values() if value is not None),
                )
            ),
            rate_interval,
            0.95,
            "poisson_rate_v1",
            eligible_count,
            weeks,
            complete,
            confidence,
        ),
        "trade_active_week_rate": _metric(
            "trade_active_week_rate",
            None if weeks == 0 else active / weeks,
            "share_0_to_1",
            None,
            week_interval,
            0.95,
            "wilson_score_v1",
            active,
            weeks,
            complete,
            confidence,
        ),
        "next_two_week_trade_propensity": _metric(
            "next_two_week_trade_propensity",
            predicted,
            "probability_0_to_1",
            None,
            None,
            None,
            "jeffreys_beta_predictive_v1",
            active,
            weeks,
            complete,
            confidence,
            limitations=[
                "This is completed-trade propensity, not a proposal acceptance probability.",
                "Only fully completed scoring periods contribute to normalized frequency; raw completed-trade counts remain visible.",
            ],
        ),
    }


def _trade_value_summary(rows, league_edges, league_estimates, complete):
    if not rows:
        return {
            "status": "unavailable",
            "label": "Not enough contemporaneous evidence",
            "plain_language_alias": None,
            "valued_trades": 0,
            "exact_valued_trades": 0,
            "relative_power_edge": _unavailable_metric(
                "relative_power_edge",
                "No strictly prior compatible weekly model can value these trades.",
            ),
            "mean_power_change": None,
            "median_power_change": None,
            "mean_playoff_probability_change": None,
            "own_benefit_rate": None,
            "counterparty_benefit_rate": None,
            "all_participants_benefit_rate": None,
            "joint_surplus": None,
            "all_methodologies_relative_power_edge_mean": None,
            "methodology_counts": {},
        }
    exact_rows = tuple(
        pair for pair in rows if pair[0].methodology_status == "exact"
    )
    exact_edges = [outcome.relative_power_edge for _, outcome in exact_rows]
    all_edges = [outcome.relative_power_edge for _, outcome in rows]
    own = [outcome.power_delta for _, outcome in rows]
    playoff = [outcome.playoff_probability_delta for _, outcome in rows if outcome.playoff_probability_delta is not None]
    pooled = partial_pool(exact_edges, league_edges)
    if pooled is None:
        inferred = {
            "label": "No tendency without sufficient exact at-time evidence",
            "alias": None,
            "confidence": "unavailable",
            "reasons": [
                "At least three exact at-time valuations are required for a value tendency label."
            ],
        }
        edge_metric = _unavailable_metric(
            "relative_power_edge",
            inferred["reasons"][0],
        )
    else:
        inferred = _value_label(
            pooled,
            len(exact_rows),
            complete,
        )
        edge_metric = _metric(
            "relative_power_edge",
            pooled["estimate"],
            "power_points_per_trade",
            percentile(pooled["estimate"], league_estimates.values()),
            pooled["interval_95"],
            0.95,
            "normal_partial_pooling_exact_at_time_v1",
            len(exact_rows),
            len(exact_rows),
            complete,
            inferred["confidence"],
            reasons=inferred["reasons"],
            limitations=[
                "Only exact at-time valuations drive this tendency metric; approximate valuations remain descriptive."
            ],
        )
    team_id = rows[0][1].team_id
    partners_positive = []
    all_positive = []
    joint = []
    for valuation, outcome in rows:
        other = next(candidate for candidate in valuation.outcomes if candidate.team_id != team_id)
        partners_positive.append(other.power_delta > 0)
        all_positive.append(outcome.power_delta > 0 and other.power_delta > 0)
        joint.append(fmean((outcome.power_delta, other.power_delta)))
    return {
        "status": (
            "available"
            if len(exact_rows) >= 3
            else "insufficient_sample"
            if exact_rows
            else "unavailable"
        ),
        "label": inferred["label"],
        "plain_language_alias": inferred["alias"],
        "valued_trades": len(rows),
        "exact_valued_trades": len(exact_rows),
        "relative_power_edge": edge_metric,
        "mean_power_change": fmean(own),
        "median_power_change": median(own),
        "mean_playoff_probability_change": None if not playoff else fmean(playoff),
        "own_benefit_rate": sum(value > 0 for value in own) / len(own),
        "counterparty_benefit_rate": sum(partners_positive) / len(partners_positive),
        "all_participants_benefit_rate": sum(all_positive) / len(all_positive),
        "joint_surplus": fmean(joint),
        "all_methodologies_relative_power_edge_mean": fmean(all_edges),
        "methodology_counts": dict(sorted(Counter(valuation.methodology_status for valuation, _ in rows).items())),
    }


def _value_label(pooled, count, complete):
    reasons = []
    if not complete:
        reasons.append("Transaction coverage is incomplete.")
        return {
            "label": "No tendency from partial transaction history",
            "alias": None,
            "confidence": "uncertain",
            "reasons": reasons,
        }
    if count < 3:
        reasons.append(
            "At least three exact at-time valued trades are required for a tendency label."
        )
        return {
            "label": "Mixed / no clear lean",
            "alias": None,
            "confidence": "uncertain",
            "reasons": reasons,
        }
    low80, high80 = pooled["interval_80"]
    low90, high90 = pooled["interval_90"]
    low95, high95 = pooled["interval_95"]
    if low90 >= -_NEUTRAL_POWER_BAND and high90 <= _NEUTRAL_POWER_BAND:
        return {
            "label": "Consistently even",
            "alias": "even",
            "confidence": "moderate" if complete else "uncertain",
            "reasons": reasons,
        }
    if low80 > 0:
        strong = low95 > 0 and count >= 5 and complete
        return {
            "label": "Value-capturing",
            "alias": "stingy",
            "confidence": "strong" if strong else "moderate" if complete else "uncertain",
            "reasons": reasons,
        }
    if high80 < 0:
        strong = high95 < 0 and count >= 5 and complete
        return {
            "label": "Concessionary",
            "alias": "generous",
            "confidence": "strong" if strong else "moderate" if complete else "uncertain",
            "reasons": reasons,
        }
    reasons.append("The estimated interval crosses the even boundary.")
    return {
        "label": "Mixed / no clear lean",
        "alias": None,
        "confidence": "uncertain",
        "reasons": reasons,
    }


def _trade_style(row, team_names):
    supported_count = len(row.sent_sizes)
    if not supported_count:
        return {
            "status": "insufficient_sample",
            "average_sent": None,
            "average_received": None,
            "package_shape": None,
            "consolidation_rate": None,
            "depth_seeking_rate": None,
            "positions_received": [],
            "positions_sent": [],
            "frequent_partners": [],
        }
    consolidation = sum(received < sent for sent, received in zip(row.sent_sizes, row.received_sizes))
    depth = sum(received > sent for sent, received in zip(row.sent_sizes, row.received_sizes))
    balanced = supported_count - consolidation - depth
    shape = max(
        (
            ("consolidation", consolidation),
            ("depth seeking", depth),
            ("balanced headcount", balanced),
        ),
        key=lambda item: (item[1], item[0]),
    )[0]
    return {
        "status": "descriptive" if supported_count < 3 else "observed_tendency",
        "average_sent": fmean(row.sent_sizes),
        "average_received": fmean(row.received_sizes),
        "package_shape": shape,
        "consolidation_rate": consolidation / supported_count,
        "depth_seeking_rate": depth / supported_count,
        "positions_received": _counter_rows(row.received_positions),
        "positions_sent": _counter_rows(row.sent_positions),
        "frequent_partners": [
            {
                "team_id": team_id,
                "team_name": team_names.get(team_id, "Unknown team"),
                "completed_trades": value,
            }
            for team_id, value in sorted(
                row.partners.items(),
                key=lambda item: (
                    -item[1],
                    team_names.get(item[0], "").casefold(),
                    item[0],
                ),
            )[:3]
        ],
    }


def _acquisition_behavior(row, weeks, league_rates, complete, bundle):
    additions = len(row.additions)
    eligible_additions = tuple(
        pair for pair in row.additions if 1 <= pair[0].effective_week <= weeks
    )
    eligible_count = len(eligible_additions)
    waiver = sum(event.kind is HistoryTransactionKind.WAIVER for event, _ in row.additions)
    free = additions - waiver
    rate = league_rates[row.team_id]
    roster = next(value for value in bundle.rosters if value.team_id == row.team_id)
    retention = _acquisition_retention(
        row.additions,
        row.drops,
        row.roster_snapshots,
        row.first_observed_at,
    )
    confidence = _rate_confidence(complete, weeks, eligible_count)
    return {
        "waiver_awards": waiver,
        "free_agent_additions": free,
        "drops": len(row.drops),
        "acquisitions_per_10_weeks": _metric(
            "acquisitions_per_10_weeks",
            rate,
            "adds_per_10_observed_weeks",
            (
                None
                if rate is None
                else percentile(
                    rate,
                    (value for value in league_rates.values() if value is not None),
                )
            ),
            None if weeks == 0 else poisson_rate_interval(eligible_count, weeks),
            0.95,
            "poisson_rate_v1",
            eligible_count,
            weeks,
            complete,
            confidence,
        ),
        "waiver_share": None if additions == 0 else waiver / additions,
        "roster_turnover": (
            None
            if weeks == 0
            else eligible_count / (max(1, roster.roster_cap) * weeks)
        ),
        "positions_acquired": _counter_rows(row.acquisition_positions),
        "next_snapshot_retention": retention,
        "limitations": [
            "Only successful waiver awards are visible; failed claims and success rate are not inferred.",
            "Only fully completed scoring periods contribute to normalized acquisition frequency.",
        ],
    }


def _acquisition_retention(additions, drops, snapshots, first_observed_at):
    ordered = sorted(snapshots, key=lambda pair: pair[0].captured_at)
    verified_drops = {
        (
            asset.canonical_player_id,
            transaction_executed_by(event, first_observed_at),
        )
        for event, asset in drops
        if asset.canonical_player_id is not None
        and transaction_executed_by(event, first_observed_at) is not None
    }
    observed, retained, streamed = 0, 0, 0
    for event, asset in additions:
        event_observed_at = transaction_executed_by(event, first_observed_at)
        if asset.canonical_player_id is None or event_observed_at is None:
            continue
        later = [
            (capture, roster)
            for capture, roster in ordered
            if capture.captured_at > event_observed_at
        ]
        if not later:
            continue
        observed += 1
        first_players = {row.canonical_player_id for row in later[0][1].players}
        kept = asset.canonical_player_id in first_players
        retained += kept
        if kept and len(later) > 1:
            first = next(row for row in later[0][1].players if row.canonical_player_id == asset.canonical_player_id)
            second_players = {row.canonical_player_id for row in later[1][1].players}
            was_dropped = any(
                player_id == asset.canonical_player_id and later[0][0].captured_at < dropped_at <= later[1][0].captured_at for player_id, dropped_at in verified_drops
            )
            streamed += first.lineup_slot not in _NONSTARTING_SLOTS and asset.canonical_player_id not in second_players and was_dropped
    return {
        "status": "available" if observed else "insufficient_follow_up",
        "eligible_additions": observed,
        "retained_rate": None if observed == 0 else retained / observed,
        "verified_streams": streamed,
    }


def _roster_outlooks(bundle, captures, as_of, positions):
    projection_value = defaultdict(float)
    for row in bundle.projections:
        if row.projected_fantasy_points is not None:
            projection_value[row.canonical_player_id] += row.projected_fantasy_points
    latest_rosters = {}
    for capture in sorted(
        (row for row in captures if row.captured_at <= as_of and row.roster_complete),
        key=lambda row: row.captured_at,
    ):
        for roster in capture.rosters:
            latest_rosters[roster.team_id] = (capture, roster)
    result = {}
    for roster in bundle.rosters:
        history_pair = latest_rosters.get(roster.team_id)
        history_capture = None if history_pair is None else history_pair[0]
        history_roster = None if history_pair is None else history_pair[1]
        players = roster.player_ids if history_roster is None else tuple(row.canonical_player_id for row in history_roster.players if row.canonical_player_id is not None)
        slots = (
            {}
            if history_roster is None or not history_capture.lineup_complete
            else {row.canonical_player_id: row.lineup_slot for row in history_roster.players if row.canonical_player_id is not None}
        )
        counts = Counter(positions.get(player_id, "UNKNOWN") for player_id in players)
        starter_count = sum(slot not in _NONSTARTING_SLOTS for slot in slots.values())
        bench_count = sum(slot == "BENCH" for slot in slots.values())
        reserve_count = sum(slot in _RESERVE_SLOTS for slot in slots.values())
        result[roster.team_id] = {
            "status": "captured" if history_roster is not None else "current_bundle_only",
            "total_players": len(players),
            "active_players": roster.active_size,
            "roster_cap": roster.roster_cap,
            "active_fullness": roster.active_size / roster.roster_cap,
            "captured_starters": starter_count if slots else None,
            "captured_bench": bench_count if slots else None,
            "captured_reserve": reserve_count if slots else None,
            "position_counts": [{"position": position, "players": count} for position, count in _sorted_counter(counts)],
            "projected_value_concentration_gini": gini(projection_value[player_id] for player_id in players),
        }
    return result


def _lineup_behavior(snapshots, complete):
    ordered = sorted(
        (pair for pair in snapshots if pair[0].lineup_complete),
        key=lambda pair: (pair[0].captured_at, pair[0].capture_id),
    )
    starter_sets = [
        frozenset(player.canonical_player_id for player in roster.players if player.canonical_player_id is not None and player.lineup_slot not in _NONSTARTING_SLOTS)
        for _, roster in ordered
    ]
    comparisons = []
    changes = []
    for previous, current in zip(starter_sets, starter_sets[1:]):
        union = previous | current
        comparisons.append(1.0 if not union else len(previous & current) / len(union))
        changes.append(len(previous ^ current) / 2)
    return {
        "status": ("available" if comparisons and complete else "insufficient_history" if complete else "partial"),
        "captured_lineup_snapshots": len(starter_sets),
        "starter_continuity": None if not comparisons else fmean(comparisons),
        "average_starter_changes": None if not changes else fmean(changes),
        "limitations": ["These are captured starter settings, not hindsight-optimal lineup scores."],
    }


def _position_needs(bundle):
    owners = {player_id: roster.team_id for roster in bundle.rosters for player_id in roster.player_ids}
    totals = defaultdict(float)
    positions = set()
    for row in bundle.projections:
        team_id = owners.get(row.canonical_player_id)
        if team_id is not None and row.projected_fantasy_points is not None:
            totals[(team_id, row.position)] += row.projected_fantasy_points
            positions.add(row.position)
    result = {}
    for team in bundle.state.teams:
        needs = []
        for position in positions:
            value = totals[(team.team_id, position)]
            population = [totals[(other.team_id, position)] for other in bundle.state.teams]
            rank = percentile(value, population)
            if rank is not None and rank <= 0.35:
                needs.append(
                    {
                        "position": position,
                        "league_percentile": rank,
                        "projected_points": value,
                    }
                )
        result[team.team_id] = tuple(
            sorted(
                needs,
                key=lambda row: (
                    row["league_percentile"],
                    _position_key(row["position"]),
                ),
            )
        )
    return result


def _proposal_guidance(
    team_name,
    activity,
    value,
    style,
    acquisition,
    roster,
    hindsight,
    needs,
    complete,
):
    support, counter, actions = [], [], []
    activity_metric = activity["trades_per_10_weeks"]
    activity_percentile = activity_metric["league_percentile"]
    if activity["completed_trades"]:
        trade_count = activity["completed_trades"]
        partner_count = activity["unique_partners"]
        support.append(
            f"Completed {trade_count} {'trade' if trade_count == 1 else 'trades'} "
            f"with {partner_count} {'partner' if partner_count == 1 else 'partners'} "
            "in the observed season."
        )
    else:
        counter.append("No completed trade is present in the verified season ledger yet.")
    if needs:
        need = needs[0]
        actions.append(
            f"Lead with credible {need['position']} help; that current position room "
            f"is at the {round(need['league_percentile'] * 100)}th league percentile "
            "by remaining projection."
        )
        support.append(f"Current {need['position']} depth is one of this roster's clearest " "relative needs.")
    if style["status"] != "insufficient_sample":
        actions.append(f"Use a {style['package_shape']} offer shape; it is the most common " "headcount pattern in this team's completed trades.")
        support.append(f"Observed packages average {style['average_sent']:.1f} sent and " f"{style['average_received']:.1f} received.")
    else:
        actions.append("Start with a simple balanced-headcount offer and make the benefit easy to verify.")
        counter.append(
            "There are too few fully resolved player-only completed trades to "
            "establish a package-shape preference."
        )
    if value["plain_language_alias"] == "stingy":
        actions.append("Show a visible roster-power gain on their side before asking for a " "premium; valued history leans value-capturing.")
        support.append(_edge_sentence(value))
    elif value["plain_language_alias"] == "generous":
        actions.append("Keep the opening offer mutually beneficial; the valued history is " "concessionary, but it does not establish intent on a new offer.")
        support.append(_edge_sentence(value))
    elif value["plain_language_alias"] == "even":
        actions.append("Lead with a visibly balanced exchange; valued history has stayed inside " "the app's even-value band.")
        support.append(_edge_sentence(value))
    else:
        counter.append("Historical value evidence does not establish a stingy, generous, or even tendency.")
    if hindsight["plain_language_alias"] == "good foresight signal":
        actions.append("Account for changing player roles explicitly; injury-cleared comparable " "trades show positive hindsight drift without establishing why.")
        support.append(_hindsight_sentence(hindsight))
    elif hindsight["plain_language_alias"] == "bad foresight signal":
        actions.append("Anchor the proposal in current role and usage evidence; injury-cleared " "comparable trades show negative hindsight value drift.")
        support.append(_hindsight_sentence(hindsight))
    if acquisition["acquisitions_per_10_weeks"]["league_percentile"] is not None and acquisition["acquisitions_per_10_weeks"]["league_percentile"] >= 0.7:
        actions.append("Do not rely on fringe bench pieces as the headline return; this roster " "adds players more often than most of the league.")
        support.append(f"Recorded {acquisition['waiver_awards'] + acquisition['free_agent_additions']} " "successful additions in the observed window.")
    if roster["active_fullness"] >= 0.99:
        actions.append("Keep active-player headcount balanced or include an obvious roster " "solution to reduce forced-drop friction.")
    if activity_percentile is not None and activity_percentile < 0.35:
        actions.append("Make the first proposal concise and need-specific; completed-trade " "activity is below the league median.")
    if not complete:
        counter.append("Transaction coverage is partial, so frequency and style conclusions are descriptive only.")
    confidence = "moderate" if complete and activity["completed_trades"] >= 3 else "uncertain" if activity["completed_trades"] else "descriptive_only"
    headline = (
        f"Approach {team_name} with a need-matched, {style['package_shape']} structure."
        if style["status"] != "insufficient_sample"
        else f"Approach {team_name} with a clear, balanced opening offer."
    )
    return {
        "headline": headline,
        "confidence": confidence,
        "actions": actions,
        "supporting_evidence": support,
        "counterevidence": counter,
        "caveats": ["This is a historical-style match, not a promise or prediction that a manager will accept."],
    }


def _summary(activity, value, acquisition, complete):
    trade_percentile = activity["trades_per_10_weeks"]["league_percentile"]
    if trade_percentile is None:
        likelihood = "Unavailable"
    elif trade_percentile >= 0.75:
        likelihood = "High completed-trade activity"
    elif trade_percentile <= 0.25:
        likelihood = "Low completed-trade activity"
    else:
        likelihood = "Typical completed-trade activity"
    evidence = "descriptive_only" if not complete else "moderate" if activity["completed_trades"] >= 3 else "uncertain"
    return {
        "trade_activity_label": likelihood,
        "value_style_label": value["label"],
        "plain_language_value_alias": value["plain_language_alias"],
        "evidence_strength": evidence,
        "successful_roster_moves": (acquisition["waiver_awards"] + acquisition["free_agent_additions"]),
    }


def _metric(
    metric_id,
    estimate,
    unit,
    league_percentile,
    interval,
    level,
    method,
    raw_n,
    exposure,
    complete,
    confidence,
    *,
    reasons=(),
    limitations=(),
):
    return {
        "metric_id": metric_id,
        "estimate": estimate,
        "unit": unit,
        "league_percentile": league_percentile,
        "interval": (
            None
            if interval is None
            else {
                "lower": interval[0],
                "upper": interval[1],
                "level": level,
                "method": method,
            }
        ),
        "sample": {
            "raw_n": raw_n,
            "effective_n": raw_n,
            "exposure_team_weeks": exposure,
        },
        "evidence": {"coverage_complete": complete},
        "confidence": {"status": confidence, "reasons": list(reasons)},
        "limitations": list(limitations),
    }


def _unavailable_metric(metric_id, reason):
    return {
        "metric_id": metric_id,
        "estimate": None,
        "unit": "power_points_per_trade",
        "league_percentile": None,
        "interval": None,
        "sample": {"raw_n": 0, "effective_n": 0, "exposure_team_weeks": 0},
        "evidence": {"coverage_complete": False},
        "confidence": {"status": "unavailable", "reasons": [reason]},
        "limitations": [],
    }


def _rate_confidence(complete, weeks, count):
    if weeks < 1:
        return "unavailable"
    if not complete:
        return "descriptive_only"
    if weeks >= 8 and count >= 3:
        return "moderate"
    return "uncertain"


def _valuations_by_team(valuations):
    result = defaultdict(list)
    for valuation in valuations:
        for outcome in valuation.outcomes:
            result[outcome.team_id].append((valuation, outcome))
    return {key: tuple(value) for key, value in result.items()}


def _player_positions(bundle):
    positions = {}
    for row in bundle.projections:
        positions.setdefault(row.canonical_player_id, row.position)
    return positions


def _asset_positions(assets, positions):
    return [positions[asset.canonical_player_id] for asset in assets if asset.canonical_player_id in positions]


def _counter_rows(counter):
    total = sum(counter.values())
    return [{"position": position, "count": count, "share": count / total} for position, count in _sorted_counter(counter)]


def _sorted_counter(counter):
    return sorted(
        counter.items(),
        key=lambda item: (-item[1], _position_key(item[0])),
    )


def _position_key(position):
    try:
        return _POSITION_ORDER.index(position), position
    except ValueError:
        return len(_POSITION_ORDER), position


def _edge_sentence(value):
    metric = value["relative_power_edge"]
    count = value["valued_trades"]
    return (
        f"The shrunk estimate is {metric['estimate']:+.2f} relative power points "
        f"per valued trade across {count} {'trade' if count == 1 else 'trades'}."
    )


def _hindsight_sentence(value):
    metric = value["relative_power_edge_drift"]
    count = value["foresight_eligible_trades"]
    return (
        f"The injury-cleared hindsight drift estimate is {metric['estimate']:+.2f} "
        f"power points per trade across {count} comparable "
        f"{'trade' if count == 1 else 'trades'}; "
        "this does not establish skill or causality."
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ()
