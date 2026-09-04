"""Candidate-level simulations and evidence records for trade timing."""

from dataclasses import dataclass, replace

from ._trade_timing_market import market_pattern


_WATCH_WINDOWS_PER_PARTNER = 2
_MIN_PLAYOFF_GAIN = 0.0025
_MIN_PLAYOFF_GAIN_SCENARIO_STEPS = 2
_PLAYOFF_GAIN_EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class TimingShortlistEvaluation:
    """Successful candidate simulations plus individually skipped candidates."""

    options: tuple[dict[str, object], ...]
    skipped_options: tuple[dict[str, object], ...]


def evaluate_shortlist(
    bundle,
    delayed_baseline,
    primary_id,
    partner_id,
    swaps,
    windows,
    trigger_index,
    *,
    evidence_index=None,
):
    """Evaluate each candidate independently so one infeasible swap is not fatal."""

    first_week = bundle.state.first_remaining_week
    conditioned_windows = [
        row
        for row in windows
        if row["conditional_trade_simulation_status"] == "ready"
    ][:_WATCH_WINDOWS_PER_PARTNER]
    options = []
    skipped = []
    for swap in swaps:
        try:
            after_rosters = _after_swap(
                bundle.rosters, primary_id, partner_id, swap
            )
            delayed = delayed_baseline.roster_change(
                after_rosters, (primary_id, partner_id)
            )
        except ValueError:
            skipped.append(
                _skipped_option(
                    swap,
                    first_week,
                    "candidate_roster_change_infeasible",
                )
            )
            continue
        try:
            immediate = _option_record(
                bundle,
                swap,
                delayed.project(first_week),
                primary_id,
                partner_id,
                first_week,
                first_week,
                None,
                evidence_index=evidence_index,
            )
        except ValueError:
            skipped.append(
                _skipped_option(
                    swap,
                    first_week,
                    "candidate_projection_inputs_infeasible",
                )
            )
        else:
            options.append(immediate)
        for window in conditioned_windows:
            trigger = trigger_index[(partner_id, window["result_week"])]
            try:
                conditioned = delayed.project_conditioned_many(
                    (first_week, window["effective_week"]),
                    trigger.scenario_indexes,
                )
                conditional_now = conditioned[first_week]
                future_impact = conditioned[window["effective_week"]]
                row = _option_record(
                    bundle,
                    swap,
                    future_impact,
                    primary_id,
                    partner_id,
                    window["effective_week"],
                    first_week,
                    window,
                    evidence_index=evidence_index,
                )
            except ValueError:
                skipped.append(
                    _skipped_option(
                        swap,
                        window["effective_week"],
                        "conditional_candidate_projection_infeasible",
                    )
                )
                continue
            row["delay_cost_primary"] = (
                conditional_now.for_team(primary_id).playoff_probability_delta
                - row["primary_playoff_probability_delta"]
            )
            row["delay_cost_partner"] = (
                conditional_now.for_team(partner_id).playoff_probability_delta
                - row["partner_playoff_probability_delta"]
            )
            row["delay_comparison_scope"] = (
                "execute_now_vs_wait_within_same_pre_trade_trigger_scenarios"
            )
            options.append(row)
    return TimingShortlistEvaluation(tuple(options), tuple(skipped))


def minimum_playoff_gain(scenario_count):
    if type(scenario_count) is not int or scenario_count < 1:
        raise ValueError("scenario_count must be a positive integer")
    return max(
        _MIN_PLAYOFF_GAIN,
        _MIN_PLAYOFF_GAIN_SCENARIO_STEPS / scenario_count,
    )


def _option_record(
    bundle,
    swap,
    impact,
    primary_id,
    partner_id,
    effective_week,
    first_week,
    window,
    *,
    evidence_index=None,
):
    primary = impact.for_team(primary_id)
    partner = impact.for_team(partner_id)
    is_now = effective_week == first_week
    scenario_count = impact.before.scenario_count
    minimum_gain = minimum_playoff_gain(scenario_count)
    point_estimate_gain = (
        primary.playoff_probability_delta > 0
        and partner.playoff_probability_delta > 0
    )
    material_gain = (
        primary.playoff_probability_delta >= minimum_gain - _PLAYOFF_GAIN_EPSILON
        and partner.playoff_probability_delta >= minimum_gain - _PLAYOFF_GAIN_EPSILON
    )
    trigger = (
        {
            "kind": "current_window_after_verification",
            "label": (
                f"If league trades are still open, propose before Week "
                f"{effective_week} locks"
            ),
            "probability": None,
            "probability_status": "unmodeled_trade_legality",
        }
        if is_now
        else {
            "kind": "loss_and_downward_slope",
            "label": (
                f"Planning only—if {_team_name(bundle, partner_id)} loses Week "
                f"{window['result_week']} and its record slope is downward, propose "
                f"before Week {effective_week} locks, after verifying the trade deadline"
            ),
            "probability": window["trigger_probability"],
            "probability_status": "modeled_outcome_trigger_only",
        }
    )
    return {
        "effective_week": effective_week,
        "timing_status": (
            "current_window_verification_required"
            if is_now
            else "conditional_watch_deadline_unverified"
        ),
        "impact_scope": (
            "all_shared_scenarios"
            if is_now
            else "partner_loss_and_downward_slope_trigger_scenarios"
        ),
        "trigger_selected_from_pre_trade_baseline": not is_now,
        "trigger": trigger,
        "primary_sends": [_player_record(bundle, swap.primary_player_id)],
        "primary_receives": [
            _player_record(bundle, swap.counterparty_player_id)
        ],
        "primary_power_delta": swap.primary_power_delta,
        "partner_power_delta": swap.counterparty_power_delta,
        "primary_display_power_delta": swap.primary_display_power_delta,
        "partner_display_power_delta": swap.counterparty_display_power_delta,
        "primary_playoff_probability_delta": primary.playoff_probability_delta,
        "partner_playoff_probability_delta": partner.playoff_probability_delta,
        "primary_expected_wins_delta": primary.expected_wins_delta,
        "partner_expected_wins_delta": partner.expected_wins_delta,
        "mutual_playoff_point_estimate_gain": point_estimate_gain,
        "mutual_playoff_gain": material_gain,
        "minimum_playoff_probability_gain_each_team": minimum_gain,
        "playoff_gain_evidence": "paired_monte_carlo_point_estimate",
        "playoff_gain_confidence_certified": False,
        "scenario_evidence": {
            "impact_id": getattr(impact, "impact_id", None),
            "before_scenario_run_id": getattr(
                impact, "before_scenario_run_id", None
            ),
            "after_scenario_run_id": getattr(
                impact, "after_scenario_run_id", None
            ),
            "draw_space_id": getattr(impact, "draw_space_id", None),
        },
        "pressure_percentile": (
            None if window is None else window["pressure_percentile"]
        ),
        "market_pattern": market_pattern(
            bundle,
            swap,
            effective_week,
            evidence_index=evidence_index,
        ),
        "scenario_count": scenario_count,
        "conditional_trigger_scenario_count": (
            None if window is None else window["trigger_scenario_count"]
        ),
        "delay_cost_primary": 0.0,
        "delay_cost_partner": 0.0,
        "delay_comparison_scope": None,
        "reasons": _option_reasons(window, is_now, minimum_gain),
        "verification_requirements": {
            "trade_deadline": "not_captured",
            "transaction_processing_rules": "not_captured",
            "player_locks_and_tradeability": "not_captured",
            "current_health": "verify_before_proposal",
            "host_legality_verified": False,
        },
    }


def _after_swap(rosters, primary_id, partner_id, swap):
    result = []
    for roster in rosters:
        if roster.team_id == primary_id:
            player_ids = tuple(
                swap.counterparty_player_id
                if player_id == swap.primary_player_id
                else player_id
                for player_id in roster.player_ids
            )
        elif roster.team_id == partner_id:
            player_ids = tuple(
                swap.primary_player_id
                if player_id == swap.counterparty_player_id
                else player_id
                for player_id in roster.player_ids
            )
        else:
            result.append(roster)
            continue
        result.append(replace(roster, player_ids=player_ids))
    return tuple(result)


def _option_reasons(window, is_now, minimum_gain):
    materiality = (
        f"Both teams clear the {minimum_gain:.2%} per-team Monte Carlo "
        "materiality floor; this is still a point estimate, not certainty."
    )
    if is_now:
        return [
            "Both teams' delayed-impact result is measured against the same scenario draws.",
            materiality,
            "Verify that trades remain open and current player health before proposing.",
        ]
    return [
        "This is a conditional schedule-pressure watch point, not a prediction of manager acceptance.",
        "Trade impact is recalculated only inside the pre-trade paths where the named loss/downturn trigger occurs.",
        materiality,
        (
            f"The partner's simulated pressure percentile is "
            f"{window['pressure_percentile']:.1%}."
            if window["pressure_percentile"] is not None
            else "The partner's pressure percentile is unavailable for this week."
        ),
    ]


def _skipped_option(swap, effective_week, reason_code):
    return {
        "primary_player_id": swap.primary_player_id,
        "counterparty_player_id": swap.counterparty_player_id,
        "effective_week": effective_week,
        "reason_code": reason_code,
    }


def _player_record(bundle, player_id):
    return {
        "player_id": player_id,
        "player_name": bundle.player_names[player_id],
    }


def _team_name(bundle, team_id):
    return next(row.name for row in bundle.state.teams if row.team_id == team_id)


__all__ = (
    "TimingShortlistEvaluation",
    "evaluate_shortlist",
    "minimum_playoff_gain",
)
