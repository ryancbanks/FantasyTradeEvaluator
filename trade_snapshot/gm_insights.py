"""Evidence-backed team management profiles for one locally captured league."""

from collections import Counter
from collections.abc import Callable
from datetime import timezone
import json
from statistics import median

from ._league_history_evidence import captured_transaction_evidence
from ._league_history_health import capture_is_fresh, latest_physical_injury_ids
from ._gm_decision_signals import (
    counterparty_value_opportunity,
    deal_accessibility,
    hindsight_value_drift,
)
from ._gm_evidence import build_trade_evidence
from ._gm_statistics import partial_pool
from ._gm_team_profiles import (
    _NEUTRAL_POWER_BAND,
    _acquisition_behavior,
    _iso,
    _lineup_behavior,
    _player_positions,
    _position_needs,
    _proposal_guidance,
    _roster_outlooks,
    _summary,
    _team_facts,
    _trade_activity,
    _trade_style,
    _trade_value_summary,
    _valuations_by_team,
)
from .engine_bundle import EngineBundle
from .gm_trade_valuation import value_historical_trades
from .league_history import (
    HistoryTransactionKind,
    LeagueHistorySnapshot,
)
from .roster_compatibility import build_roster_compatibility


_SCHEMA_VERSION = 1


def build_gm_insights(
    bundle: EngineBundle,
    history: LeagueHistorySnapshot | None,
    *,
    bundle_loader: Callable[[str], EngineBundle] | None = None,
) -> dict[str, object]:
    """Build a finite JSON-ready GM report without inferring unobserved intent."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    if history is None:
        return _empty_result(bundle, build_roster_compatibility(bundle))
    if not isinstance(history, LeagueHistorySnapshot):
        raise ValueError("history must be a LeagueHistorySnapshot or null")
    if history.bundle_id != bundle.bundle_id or history.season != bundle.state.season:
        raise ValueError("history does not match the selected weekly bundle")

    as_of = history.bundle_captured_at.astimezone(timezone.utc)
    captures = tuple(row for row in history.captures if row.captured_at <= as_of)
    if not captures:
        return _empty_result(bundle, build_roster_compatibility(bundle))
    transactions, first_observed_at = captured_transaction_evidence(captures)
    transactions = tuple(row for row in transactions if row.recorded_at <= as_of)
    latest_capture = max(captures, key=lambda row: (row.captured_at, row.capture_id), default=None)
    capture_fresh = capture_is_fresh(latest_capture, as_of)
    transaction_complete = bool(
        capture_fresh and latest_capture.transaction_history_complete
    )
    roster_complete = bool(capture_fresh and latest_capture.roster_complete)
    lineup_complete = bool(capture_fresh and latest_capture.lineup_complete)
    observed_weeks = max(0, min(
        bundle.state.first_remaining_week - 1,
        bundle.state.playoff_rules.regular_season_end_week,
    ))
    team_names = {team.team_id: team.name for team in bundle.state.teams}
    positions = _player_positions(bundle)
    facts = {
        team_id: _team_facts(
            team_id,
            transactions,
            captures,
            positions,
            first_observed_at,
        )
        for team_id in team_names
    }

    valuation_result = None
    if bundle_loader is not None:
        valuation_result = value_historical_trades(
            history,
            as_of=as_of,
            bundle_loader=bundle_loader,
            current_bundle=bundle,
        )
    valuations = () if valuation_result is None else valuation_result.valuations
    unvalued_transactions = (
        {} if valuation_result is None else valuation_result.unvalued_transactions
    )
    valuations_by_team = _valuations_by_team(valuations)
    league_edges = tuple(
        outcome.relative_power_edge
        for row in valuations
        if row.methodology_status == "exact"
        for outcome in row.outcomes
    )
    current_roster_captures = (
        (latest_capture,)
        if roster_complete
        else ()
    )
    roster_outlooks = _roster_outlooks(
        bundle, current_roster_captures, as_of, positions
    )
    position_needs = _position_needs(bundle)
    current_injuries = latest_physical_injury_ids(
        current_roster_captures, as_of
    )
    compatibility_report = build_roster_compatibility(
        bundle,
        physically_injured_player_ids=current_injuries,
    )
    roster_compatibility = _compatibility_teams(compatibility_report)

    trade_rates = {
        team_id: (
            None
            if observed_weeks == 0
            else sum(
                1 <= event.effective_week <= observed_weeks
                for event in row.trades
            )
            / observed_weeks
            * 10
        )
        for team_id, row in facts.items()
    }
    acquisition_rates = {
        team_id: (
            None
            if observed_weeks == 0
            else sum(
                1 <= event.effective_week <= observed_weeks
                for event, _ in row.additions
            )
            / observed_weeks
            * 10
        )
        for team_id, row in facts.items()
    }
    edge_estimates = {
        team_id: pooled["estimate"]
        for team_id, rows in valuations_by_team.items()
        if (pooled := partial_pool(
            [
                outcome.relative_power_edge
                for valuation, outcome in rows
                if valuation.methodology_status == "exact"
            ],
            league_edges,
        )) is not None
    }
    eligible_drifts = tuple(
        current_outcome.relative_power_edge_drift
        for valuation in valuations
        if valuation.current_revaluation is not None
        and valuation.current_revaluation.foresight_eligible
        for current_outcome in valuation.current_revaluation.outcomes
    )

    teams = []
    for team_id in sorted(team_names, key=lambda value: (team_names[value].casefold(), value)):
        row = facts[team_id]
        valued = valuations_by_team.get(team_id, ())
        value_summary = _trade_value_summary(
            valued,
            league_edges,
            edge_estimates,
            transaction_complete,
        )
        activity = _trade_activity(
            row,
            observed_weeks,
            trade_rates,
            transaction_complete,
        )
        acquisition = _acquisition_behavior(
            row,
            observed_weeks,
            acquisition_rates,
            transaction_complete,
            bundle,
        )
        trade_style = _trade_style(row, team_names)
        accessibility = deal_accessibility(
            completed_trades=len(row.trades),
            trade_weeks=row.trade_weeks,
            unique_partner_count=len(row.partners),
            package_shapes=zip(row.sent_sizes, row.received_sizes),
            observed_weeks=observed_weeks,
            possible_partner_count=len(team_names) - 1,
            activity=activity,
            coverage_complete=transaction_complete,
        )
        counterparty_opportunity = counterparty_value_opportunity(value_summary)
        hindsight = hindsight_value_drift(
            valued,
            eligible_drifts,
            neutral_band=_NEUTRAL_POWER_BAND,
            coverage_complete=transaction_complete,
        )
        lineup = _lineup_behavior(row.roster_snapshots, lineup_complete)
        roster = roster_outlooks[team_id]
        guidance = _proposal_guidance(
            team_names[team_id],
            activity,
            value_summary,
            trade_style,
            acquisition,
            roster,
            hindsight,
            position_needs.get(team_id, ()),
            transaction_complete,
        )
        teams.append(
            {
                "team_id": team_id,
                "team_name": team_names[team_id],
                "summary": _summary(activity, value_summary, acquisition, transaction_complete),
                "trade_activity": activity,
                "deal_accessibility": accessibility,
                "trade_value": value_summary,
                "counterparty_value_opportunity": counterparty_opportunity,
                "hindsight_value_drift": hindsight,
                "trade_style": trade_style,
                "acquisition_behavior": acquisition,
                "roster_construction": roster,
                "lineup_behavior": lineup,
                "roster_compatibility": roster_compatibility[team_id],
                "proposal_guidance": guidance,
                "evidence": build_trade_evidence(
                    team_id,
                    row.trades,
                    valuations,
                    unvalued_transactions,
                    team_names,
                    bundle.player_names,
                    row.first_observed_at,
                ),
            }
        )

    trade_count = sum(
        row.kind is HistoryTransactionKind.TRADE for row in transactions
    )
    transaction_count = len(transactions)
    valued_count = len(valuations)
    current_revalued_count = sum(
        row.current_revaluation is not None for row in valuations
    )
    current_unavailable = Counter(
        row.current_revaluation_unavailable_reason
        for row in valuations
        if row.current_revaluation_unavailable_reason is not None
    )
    foresight_eligible_count = sum(
        row.current_revaluation is not None
        and row.current_revaluation.foresight_eligible
        for row in valuations
    )
    status = (
        "partial"
        if not transaction_complete or not roster_complete
        else "insufficient_sample"
        if transaction_count == 0
        else "ready"
    )
    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "league_history_id": history.history_revision,
        "as_of": _iso(as_of),
        "status": status,
        "scope": {
            "season": history.season,
            "identity_mode": "team_season",
            "completed_transactions_only": True,
            "offers_observed": False,
            "observed_scoring_periods": observed_weeks,
        },
        "coverage": {
            "capture_count": len(captures),
            "current_capture_fresh_for_bundle": capture_fresh,
            "coverage_start": None if latest_capture is None else _iso(latest_capture.coverage_start),
            "coverage_end": None if latest_capture is None else _iso(latest_capture.coverage_end),
            "transactions": {
                "status": "complete" if transaction_complete else "partial",
                "completed_events": transaction_count,
                "completed_trades": trade_count,
            },
            "valuations": {
                "status": "available" if valued_count else "insufficient_historical_evidence",
                "valued_trades": valued_count,
                "unvalued_trades": max(0, trade_count - valued_count),
                "coverage_ratio": None if trade_count == 0 else valued_count / trade_count,
                "unvalued_reasons": (
                    {} if valuation_result is None else dict(valuation_result.unvalued_reasons)
                ),
                "current_revalued_trades": current_revalued_count,
                "current_revaluation_unavailable": valued_count - current_revalued_count,
                "current_revaluation_unavailable_reasons": dict(
                    sorted(current_unavailable.items())
                ),
                "foresight_eligible_trades": foresight_eligible_count,
            },
            "rosters": {
                "status": "complete" if roster_complete else "partial",
                "snapshot_count": len(captures),
            },
            "lineups": {
                "status": "complete_at_capture_times" if lineup_complete else "partial",
                "snapshot_count": len(captures),
            },
        },
        "methodology": {
            "trade_propensity": (
                "Completed-trade weeks use a Jeffreys beta posterior; the two-week "
                "figure predicts another completed trade from observed league activity, "
                "not whether a proposal will be accepted."
            ),
            "trade_value": (
                "Power and playoff effects use only the latest compatible weekly model "
                "captured strictly before ESPN's proposal timestamp, within eight days. "
                "The first scan containing the completed transaction supplies only an "
                "executed-by bound; overlapping event windows remain unvalued."
            ),
            "current_revaluation": (
                "The selected current model re-scores the identical traded packages in "
                "the same reconstructed pre-trade rosters. This is hindsight value drift, "
                "never a replacement for missing at-time evidence."
            ),
            "decision_dimensions": {
                "deal_accessibility": "Completed-deal activity, recency, partner breadth, and package variety only.",
                "counterparty_value_opportunity": "Exactly the negative of exact contemporaneous relative power edge; no activity input.",
                "roster_compatibility": "Current positional complement, roster capacity, and mutually positive current power screens only; no behavior input.",
            },
            "value_style_neutral_band": {
                "unit": "power_points_per_trade",
                "lower": -_NEUTRAL_POWER_BAND,
                "upper": _NEUTRAL_POWER_BAND,
                "basis": "the app's one-decimal displayed power precision",
            },
            "limitations": [
                "Completed transactions do not reveal rejected offers, asking prices, or intent.",
                "Profiles describe a team slot for this season; they are not claims about a private person.",
                "Captured lineups are point-in-time settings, not proof of decision quality.",
                "Older trades without a strictly prior weekly model remain unvalued rather than using today's data.",
                "Foresight labels exclude trades with captured physical injuries or incomplete weekly health evidence; raw then/current comparisons remain visible.",
                "Role changes, performance variance, and information learned after a trade can still drive hindsight value drift, so the result is not causal.",
            ],
        },
        "league_baselines": {
            "median_completed_trades": median(len(row.trades) for row in facts.values()),
            "median_trades_per_10_weeks": (
                None if observed_weeks == 0 else median(trade_rates.values())
            ),
            "median_acquisitions_per_10_weeks": (
                None if observed_weeks == 0 else median(acquisition_rates.values())
            ),
        },
        "teams": teams,
    }
    json.dumps(result, allow_nan=False, sort_keys=True)
    return result


def _empty_result(bundle, compatibility_report):
    compatibility = _compatibility_teams(compatibility_report)
    return {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "league_history_id": None,
        "as_of": None,
        "status": "not_collected",
        "scope": {
            "season": bundle.state.season,
            "identity_mode": "team_season",
            "completed_transactions_only": True,
            "offers_observed": False,
            "observed_scoring_periods": 0,
        },
        "coverage": {
            "capture_count": 0,
            "coverage_start": None,
            "coverage_end": None,
            "transactions": {"status": "not_collected", "completed_events": None, "completed_trades": None},
            "valuations": {
                "status": "not_collected",
                "valued_trades": None,
                "unvalued_trades": None,
                "coverage_ratio": None,
                "unvalued_reasons": {},
                "current_revalued_trades": None,
                "current_revaluation_unavailable": None,
                "current_revaluation_unavailable_reasons": {},
                "foresight_eligible_trades": None,
            },
            "rosters": {"status": "not_collected", "snapshot_count": 0},
            "lineups": {"status": "not_collected", "snapshot_count": 0},
        },
        "methodology": {
            "trade_propensity": "Requires a verified completed-transaction ledger.",
            "trade_value": "Requires a weekly model captured before a completed trade.",
            "current_revaluation": "Requires at-time evidence before hindsight comparison is allowed.",
            "decision_dimensions": {
                "deal_accessibility": "Requires locally captured completed-trade history.",
                "counterparty_value_opportunity": "Requires contemporaneous at-time valuations.",
                "roster_compatibility": "Requires the selected current weekly bundle.",
            },
            "value_style_neutral_band": {"unit": "power_points_per_trade", "lower": -_NEUTRAL_POWER_BAND, "upper": _NEUTRAL_POWER_BAND, "basis": "the app's one-decimal displayed power precision"},
            "limitations": ["This imported or older weekly model has no locally bound league activity history."],
        },
        "league_baselines": {},
        "teams": [
            {
                "team_id": team.team_id,
                "team_name": team.name,
                "roster_compatibility": compatibility[team.team_id],
                "history_insights": {
                    "status": "not_collected",
                    "reason": "No locally bound league activity history exists for this weekly bundle.",
                    "unavailable_sections": [
                        "deal_accessibility",
                        "counterparty_value_opportunity",
                        "hindsight_value_drift",
                        "trade_activity",
                        "trade_style",
                        "trade_value",
                        "acquisition_behavior",
                        "lineup_behavior",
                        "proposal_guidance",
                    ],
                },
            }
            for team in sorted(
                bundle.state.teams,
                key=lambda row: (row.name.casefold(), row.team_id),
            )
        ],
    }


def _compatibility_teams(report):
    scope = dict(report["scope"])
    return {
        row["team_id"]: {
            **row,
            "scope": scope,
            "power_methodology_status": report["power_methodology_status"],
        }
        for row in report["teams"]
    }


__all__ = ("build_gm_insights",)
