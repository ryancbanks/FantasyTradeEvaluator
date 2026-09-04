"""Forward-looking trade windows grounded in local league simulations."""

import json
from math import ceil

from ._league_history_health import (
    RECOGNIZED_HEALTH_STATUSES,
    capture_is_fresh,
    latest_physical_injury_ids,
)
from ._record_trend import record_slope_direction
from ._trade_timing_evaluation import (
    TimingShortlistEvaluation,
    _option_record,
    evaluate_shortlist as _evaluate_shortlist,
    minimum_playoff_gain as _minimum_playoff_gain,
)
from ._trade_timing_evidence import (
    analysis_as_of as _analysis_as_of,
    history_coverage as _history_coverage,
    iso_utc as _iso,
    timing_evidence as _timing_evidence,
)
from ._trade_timing_market import prepare_market_evidence, projection_lineage_summary
from ._trade_timing_selection import build_recommendation, partner_plan_key
from .delayed_trade_impact import prepare_delayed_baseline
from .engine_bundle import EngineBundle
from .gm_timing_profile import build_completed_deal_timing_profiles
from .league_history import LeagueHistorySnapshot
from .roster_compatibility import screened_roster_swaps
from .scenario_config import CorrelatedScenarioConfig
from .season_trajectory import (
    build_loss_and_downward_scenario_index,
    build_season_trajectory,
)
from .trade_impact import prepare_season_baseline


_SCHEMA_VERSION = 2
_DEFAULT_SCENARIO_LIMIT = 1_000
_POWER_FLOOR = -5.0
_SHORTLIST_PER_PARTNER = 3
_MIN_CONDITIONAL_SCENARIOS = 100
_MIN_CONDITIONAL_SCENARIO_FRACTION = 0.05


def build_trade_timing(
    bundle: EngineBundle,
    history: LeagueHistorySnapshot | None,
    primary_team_id: str,
    *,
    scenario_limit: int = _DEFAULT_SCENARIO_LIMIT,
) -> dict[str, object]:
    """Build honest, conditional week plans from a bounded local simulation."""

    _validate_inputs(bundle, history, primary_team_id, scenario_limit)
    names = {row.team_id: row.name for row in bundle.state.teams}
    if not bundle.state.remaining_regular_season_weeks:
        return _season_complete(bundle, history, primary_team_id, names)

    config = _bounded_config(bundle, scenario_limit)
    baseline = prepare_season_baseline(
        bundle.state,
        bundle.rosters,
        bundle.projections,
        bundle.eligibilities,
        config,
    )
    delayed_baseline = prepare_delayed_baseline(baseline)
    before_scenarios = delayed_baseline.before_scenarios
    trajectory = build_season_trajectory(
        bundle.state,
        before_scenarios,
        score_decimal_places=baseline.score_decimal_places,
        random_seed=baseline.tiebreak_random_seed,
    )
    trigger_index = build_loss_and_downward_scenario_index(
        bundle.state,
        before_scenarios,
        score_decimal_places=baseline.score_decimal_places,
    )
    trajectory_by_team = {row["team_id"]: row for row in trajectory["teams"]}
    timing_profiles = build_completed_deal_timing_profiles(bundle.state, history)
    injuries, injury_status = _current_injuries(history)
    market_evidence = prepare_market_evidence(bundle)
    primary_week = bundle.state.first_remaining_week
    plans = []
    for partner_id in sorted(
        (team_id for team_id in names if team_id != primary_team_id),
        key=lambda team_id: (names[team_id].casefold(), team_id),
    ):
        partner_trajectory = trajectory_by_team[partner_id]
        vulnerable_windows = _vulnerable_windows(
            partner_trajectory["projected"],
            primary_week,
            partner_id,
            trigger_index,
        )
        all_swaps = screened_roster_swaps(
            bundle,
            primary_team_id,
            partner_id,
            minimum_displayed_power_delta=_POWER_FLOOR,
            physically_injured_player_ids=injuries,
        )
        evaluated = _evaluate_shortlist(
            bundle,
            delayed_baseline,
            primary_team_id,
            partner_id,
            all_swaps[:_SHORTLIST_PER_PARTNER],
            vulnerable_windows,
            trigger_index,
            evidence_index=market_evidence,
        )
        completed_timing = _completed_timing_record(timing_profiles[partner_id])
        plans.append(
            {
                "partner_team_id": partner_id,
                "partner_team_name": names[partner_id],
                "record_trajectory": {
                    "history_status": trajectory["history_status"],
                    "direction": _near_term_direction(partner_trajectory),
                    "current_direction": partner_trajectory["current_direction"],
                    "current_record": partner_trajectory["current_record"],
                    "observed": partner_trajectory["observed"],
                    "projected": partner_trajectory["projected"],
                },
                "vulnerable_windows": vulnerable_windows,
                "completed_deal_timing": completed_timing,
                "candidate_screen": {
                    "trade_shape": "1_for_1",
                    "minimum_displayed_power_delta_each_team": _POWER_FLOOR,
                    "eligible_swap_count": len(all_swaps),
                    "simulated_shortlist_count": min(
                        len(all_swaps), _SHORTLIST_PER_PARTNER
                    ),
                    "successfully_simulated_option_count": len(evaluated.options),
                    "skipped_infeasible_option_count": len(
                        evaluated.skipped_options
                    ),
                    "simulation_coverage_status": (
                        "complete"
                        if not evaluated.skipped_options
                        else "partial_candidate_failures"
                    ),
                    "skipped_infeasible_options": list(
                        evaluated.skipped_options
                    ),
                    "shortlist_is_exhaustive": len(all_swaps)
                    <= _SHORTLIST_PER_PARTNER,
                    "current_physical_injuries_verified": injury_status
                    == "complete_and_fresh",
                    "known_physical_injury_count": len(injuries),
                    "current_health_screen_status": injury_status,
                },
                "recommendation": build_recommendation(
                    evaluated.options,
                    primary_week,
                    shortlist_is_exhaustive=len(all_swaps)
                    <= _SHORTLIST_PER_PARTNER,
                    alternative_limit=_SHORTLIST_PER_PARTNER,
                    skipped_option_count=len(evaluated.skipped_options),
                ),
            }
        )
    plans.sort(key=partner_plan_key)
    for rank, plan in enumerate(plans, 1):
        plan["timing_partner_rank"] = rank

    result = {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "history_revision": None if history is None else history.history_revision,
        "analysis_as_of": _iso(_analysis_as_of(bundle, history)),
        "status": "ready",
        "primary_team_id": primary_team_id,
        "primary_team_name": names[primary_team_id],
        "scenario_sampling": {
            "bundle_scenario_count": bundle.scenario_config.scenario_count,
            "timing_scenario_count": config.scenario_count,
            "scenario_count": config.scenario_count,
            "capped": config.scenario_count < bundle.scenario_config.scenario_count,
            "policy": "deterministic_prefix_of_shared_correlated_draws",
            "probability_resolution": 1 / config.scenario_count,
        },
        "current_health_screen": {
            "status": injury_status,
            "excluded_physical_injury_count": len(injuries),
            "verification_required": injury_status != "complete_and_fresh",
        },
        "history_coverage": _history_coverage(history, timing_profiles),
        "evidence": _timing_evidence(
            bundle,
            history,
            baseline,
        ),
        "projection_lineage": projection_lineage_summary(bundle),
        "trade_deadline": {
            "status": "not_captured",
            "current_window_is_legality_verified": False,
            "future_windows_are_legality_verified": False,
            "handling": (
                "Every proposal requires confirming that the league still permits "
                "trades. Future weeks remain conditional watch plans."
            ),
        },
        "trade_legality": {
            "status": "manual_verification_required",
            "trade_deadline_status": "not_captured",
            "transaction_processing_rules_status": "not_captured",
            "player_lock_status": "not_captured",
            "undroppable_player_status": "not_captured",
            "candidate_player_tradeability_status": "not_captured",
            "host_legality_verified": False,
        },
        "methodology": {
            "manager_acceptance_modeled": False,
            "pressure_is_acceptance_probability": False,
            "completed_deal_timing_interpretation": (
                "Historical participation in eventually completed trades; rejected "
                "offers and verified accepter roles are not observed."
            ),
            "recommendation_rule": (
                "A trade must pass the current displayed-power screen and increase "
                "both teams' simulated playoff point estimates by at least the larger "
                "of 0.25 percentage points or two scenario steps."
            ),
            "future_trigger_rule": (
                "A future plan is conditional on the projected loss/downturn occurring "
                "and assumes today's rosters otherwise remain unchanged."
            ),
            "market_pattern_rule": (
                "High/low describes the player's projected scoring spot within his own "
                "remaining active weeks; it is not a forecast market price or future ECR."
            ),
            "power_methodology_status": bundle.methodology_evidence.power_result_status(
                outgoing_count=1,
                incoming_count=1,
                has_roster_adjustment=False,
            ),
            "automatic_preview_scope": (
                "The timing preview screens 1-for-1 trades. Use the full trade search "
                "for custom package size, imbalance, no-drop, lock, and filter rules."
            ),
            "playoff_gain_uncertainty": (
                "Candidate gains are paired Monte Carlo point estimates with a declared "
                "materiality floor, not confidence-certified improvements."
            ),
            "limitations": [
                "Every plan requires verifying that the league trade deadline has not passed.",
                "Delayed simulations assume today's rosters otherwise remain unchanged.",
                "The automatic preview simulates only the three strongest power-screened 1-for-1 swaps per opponent, not the full configurable trade space.",
                "Only explicitly captured current physical injuries can be excluded when current health coverage is incomplete.",
                "Monte Carlo playoff changes are resolution-limited point estimates and do not establish statistical certainty.",
                "Completed transaction history is descriptive only and is not used to fill missing legality, deadline, or health evidence.",
            ],
        },
        "trajectory_methodology": trajectory["methodology"],
        "primary_trajectory": trajectory_by_team[primary_team_id],
        "partner_plans": plans,
    }
    json.dumps(result, allow_nan=False, sort_keys=True)
    return result


def _vulnerable_windows(projected, first_week, partner_id, trigger_index):
    windows = []
    projected_weeks = {item["week"] for item in projected}
    for row in projected:
        effective_week = row["week"] + 1
        trigger = trigger_index[(partner_id, row["week"])]
        if (
            effective_week <= first_week
            or effective_week not in projected_weeks
            or trigger.probability is None
        ):
            continue
        minimum_scenarios = max(
            _MIN_CONDITIONAL_SCENARIOS,
            ceil(
                trigger.total_scenario_count
                * _MIN_CONDITIONAL_SCENARIO_FRACTION
            ),
        )
        trigger_count = len(trigger.scenario_indexes)
        windows.append(
            {
                "result_week": row["week"],
                "effective_week": effective_week,
                "opponent_id": row["opponent_id"],
                "opponent_name": row["opponent_name"],
                "win_probability": row["win_probability"],
                "loss_probability": row["loss_probability"],
                "downward_slope_probability": row["downward_slope_probability"],
                "two_loss_streak_probability": row["two_loss_streak_probability"],
                "trigger_probability": trigger.probability,
                "trigger_scenario_count": trigger_count,
                "trigger_eligible_scenario_count": trigger.eligible_scenario_count,
                "conditional_minimum_scenario_count": minimum_scenarios,
                "conditional_trade_simulation_status": (
                    "ready"
                    if trigger_count >= minimum_scenarios
                    else "insufficient_trigger_scenarios"
                ),
                "playoff_probability_if_win": row["playoff_probability_if_win"],
                "playoff_probability_if_loss": row["playoff_probability_if_loss"],
                "playoff_sensitivity": row["playoff_sensitivity"],
                "pressure_percentile": row["pressure_percentile"],
                "trade_deadline_status": "unverified",
            }
        )
    return sorted(
        windows,
        key=lambda row: (
            -(row["pressure_percentile"] or 0.0),
            -(row["trigger_probability"] or 0.0),
            row["effective_week"],
        ),
    )


def _completed_timing_record(profile):
    timing = profile["timing"]
    rates = timing.get("rates", {})
    comparisons = timing.get("comparisons", {})
    return {
        "status": timing.get("status", profile["status"]),
        "profile_status": profile["status"],
        "analysis_as_of": profile["analysis_as_of"],
        "evidence": profile["evidence"],
        "coverage": profile["coverage"],
        "unavailable_reason": timing.get("reason")
        or (
            None
            if timing.get("status") == "descriptive"
            else profile["coverage"]["status"]
        ),
        "manager_acceptance_modeled": False,
        "use_for_personalization": profile["use_for_personalization"],
        "health_screen_status": profile["health_screen_status"],
        "interpretation": (
            "Completed-deal participation per elapsed scoring period, not "
            "the probability this offer will be accepted."
        ),
        "timestamp_bases": profile["timestamp_bases"],
        "unconditional": rates.get("unconditional"),
        "after_loss": rates.get("after_loss"),
        "after_nonloss": rates.get("after_nonloss"),
        "downward": rates.get("downward"),
        "non_downward": rates.get("non_downward"),
        "association": comparisons.get("after_loss_minus_nonloss"),
        "slope_association": comparisons.get("downward_minus_non_downward"),
        "projected_completed_deal_participation_proxy": None,
        "proxy_status": "not_personalized_without_week_aligned_health_evidence",
        "trade_deadline_status": "not_captured",
    }


def _current_injuries(history):
    if history is None:
        return (), "history_not_collected"
    as_of = history.bundle_captured_at
    captures = tuple(row for row in history.captures if row.captured_at <= as_of)
    latest = max(
        (row for row in captures if row.roster_complete),
        key=lambda row: (row.captured_at, row.capture_id),
        default=None,
    )
    if latest is None or not capture_is_fresh(latest, as_of):
        return (), "current_health_unavailable"
    injuries = latest_physical_injury_ids(captures, as_of)
    if any(
        player.injury_status not in RECOGNIZED_HEALTH_STATUSES
        for roster in latest.rosters
        for player in roster.players
    ):
        return injuries, "partial_or_unrecognized_statuses"
    return injuries, "complete_and_fresh"


def _near_term_direction(trajectory):
    projected = trajectory["projected"]
    if not projected:
        return trajectory["current_direction"]
    slope = projected[0]["median_record_slope"]
    if slope is None:
        return "insufficient_history"
    return record_slope_direction(slope)


def _bounded_config(bundle, scenario_limit):
    source = bundle.scenario_config
    return (
        source
        if source.scenario_count <= scenario_limit
        else CorrelatedScenarioConfig(
            scenario_limit,
            source.seed,
            source.loadings,
            source.player_score_floor,
        )
    )


def _validate_inputs(bundle, history, primary_team_id, scenario_limit):
    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    if history is not None and not isinstance(history, LeagueHistorySnapshot):
        raise ValueError("history must be a LeagueHistorySnapshot or null")
    if history is not None and (
        history.bundle_id != bundle.bundle_id or history.season != bundle.state.season
    ):
        raise ValueError("history does not match the selected weekly bundle")
    if primary_team_id not in {row.team_id for row in bundle.state.teams}:
        raise ValueError("primary_team_id is not in the selected bundle")
    if type(scenario_limit) is not int or scenario_limit < 1:
        raise ValueError("scenario_limit must be a positive integer")


def _season_complete(bundle, history, primary_team_id, names):
    profiles = build_completed_deal_timing_profiles(bundle.state, history)
    return {
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "history_revision": None if history is None else history.history_revision,
        "analysis_as_of": _iso(_analysis_as_of(bundle, history)),
        "status": "season_complete",
        "primary_team_id": primary_team_id,
        "primary_team_name": names[primary_team_id],
        "scenario_sampling": None,
        "history_coverage": _history_coverage(history, profiles),
        "evidence": _timing_evidence(bundle, history),
        "projection_lineage": projection_lineage_summary(bundle),
        "trade_deadline": {"status": "season_complete"},
        "trade_legality": {
            "status": "season_complete",
            "host_legality_verified": False,
        },
        "methodology": {"manager_acceptance_modeled": False},
        "primary_trajectory": None,
        "partner_plans": [],
    }


__all__ = ("build_trade_timing",)
