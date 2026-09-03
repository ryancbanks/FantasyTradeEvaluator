"""Capability gates derived from the evidence retained in an engine bundle."""

from ._data_readiness_policy import (
    _AVAILABILITY_LIMITATION,
    _CHAMPIONSHIP_PROXY_LIMITATION,
    _FANTASYPROS_BENCHMARK_POLICY,
    _ROS_ALLOCATION_LIMITATION,
    _SCORING_LIMITATION,
)
from ._season_ranking import (
    UnsupportedTiebreakerError,
    validate_tiebreaker_inputs,
)
from .ecr import EcrPeriod
from .feature_engineering import projection_availability_requirements
from .nfl_schedule import NflTeamWeekStatus, validate_complete_regular_season
from .projections import ProjectionStatus


def _capability_decisions(
    bundle,
    *,
    projections_by_player,
    full_horizon_provider_availability,
    available_full_horizon_ensembles,
    provider_status_coverage,
    simulation_limitations,
    holdout_validated_power,
):
    """Gate each calculation on the evidence it actually consumes."""

    state = bundle.state
    team_ids = {row.team_id for row in state.teams}
    player_ids = set(projections_by_player)
    weeks = set(state.remaining_regular_season_weeks)
    expected_projection_keys = {
        (player_id, week) for player_id in player_ids for week in weeks
    }
    actual_projection_keys = {
        (row.canonical_player_id, row.week) for row in bundle.projections
    }
    projection_grid_complete = (
        bool(expected_projection_keys)
        and actual_projection_keys == expected_projection_keys
        and all(
            row.status in {ProjectionStatus.OBSERVED, ProjectionStatus.BYE}
            for row in bundle.projections
        )
    )
    ecr_periods = {row.period for row in bundle.ecr_snapshots}
    ecr_pair_complete = ecr_periods == {
        EcrPeriod.WEEKLY,
        EcrPeriod.REST_OF_SEASON,
    }
    formula_identity_matches = (
        bundle.strength_formula.scoring_profile_id
        == bundle.scoring_profile.scoring_profile_id
        == state.scoring_profile_id
        and bundle.strength_formula.formula_id
        == bundle.methodology_evidence.formula_id
        and bundle.strength_model.snapshot_id == state.snapshot_id
        and bundle.strength_model.scoring_profile_id == state.scoring_profile_id
        and bundle.strength_model.season == state.season
    )
    required_features = set(bundle.strength_formula.residual_weights)
    required_features.update(
        name
        for weights in bundle.strength_formula.role_weights.values()
        for name in weights
    )
    required_ecr_periods = set()
    if any(name.startswith("ecr_weekly_") for name in required_features):
        required_ecr_periods.add(EcrPeriod.WEEKLY)
    if any(name.startswith("ecr_ros_") for name in required_features):
        required_ecr_periods.add(EcrPeriod.REST_OF_SEASON)
    required_ecr_complete = required_ecr_periods.issubset(ecr_periods)
    availability = projection_availability_requirements(
        required_features,
        tuple(row.provider for row in bundle.ensemble_config.provider_weights),
    )
    first_week = state.first_remaining_week
    first_week_rows = tuple(
        row for row in bundle.projections if row.week == first_week
    )
    current_provider_complete = all(
        all(
            next(
                (
                    observation.status
                    for observation in row.provider_observations
                    if observation.provider == provider
                ),
                None,
            )
            in {ProjectionStatus.OBSERVED, ProjectionStatus.BYE}
            for row in first_week_rows
        )
        for provider in availability.current_providers
    )
    full_provider_complete = all(
        full_horizon_provider_availability[provider]["unavailable_players"] == 0
        for provider in availability.full_ros_providers
    )
    current_ensemble_complete = not availability.ensemble_current or (
        len(first_week_rows) == len(player_ids)
        and all(
            row.status in {ProjectionStatus.OBSERVED, ProjectionStatus.BYE}
            for row in first_week_rows
        )
    )
    full_ensemble_complete = (
        not availability.ensemble_full_ros
        or available_full_horizon_ensembles == len(player_ids)
    )
    required_projection_evidence_complete = all(
        (
            current_provider_complete,
            full_provider_complete,
            current_ensemble_complete,
            full_ensemble_complete,
        )
    )
    formula_uses_projection = any(
        (
            availability.current_providers,
            availability.full_ros_providers,
            availability.ensemble_current,
            availability.ensemble_full_ros,
        )
    )
    schedule_complete = _schedule_evidence_complete(bundle)
    roster_identity_complete = (
        {row.team_id for row in bundle.rosters} == team_ids
        and set(bundle.strength_model.players) == player_ids
        and {row.canonical_player_id for row in bundle.eligibilities}
        == player_ids
    )
    projection_scoring_bound = (
        bundle.projection_source_manifest.evaluation_scoring_profile_id
        == bundle.scoring_profile.scoring_profile_id
        and all(
            row.scoring_profile_id == bundle.scoring_profile.scoring_profile_id
            for row in (*bundle.projections, *bundle.projection_evidence)
        )
    )
    standings_complete = {row.team_id for row in state.standings} == team_ids
    matchup_schedule_complete = _fantasy_matchup_schedule_complete(bundle)
    try:
        validate_tiebreaker_inputs(state)
    except UnsupportedTiebreakerError as error:
        tiebreaker_inputs_ready = False
        tiebreaker_input_requirement = str(error)
    else:
        tiebreaker_inputs_ready = True
        tiebreaker_input_requirement = (
            "captured tiebreak inputs required by the league rules"
        )
    waiver_pool_complete = (
        bundle.waiver_pool.snapshot_id == state.snapshot_id
        and bundle.waiver_pool.minimum_pool_size
        >= state.roster_rules.roster_cap
    )

    power_missing = _missing_labels(
        (required_ecr_complete, "ECR horizons required by the strength formula"),
        (formula_identity_matches, "compatible strength formula and methodology evidence"),
        (roster_identity_complete, "complete roster, eligibility, and strength identities"),
        (required_projection_evidence_complete, "projection horizons required by the strength formula"),
        (
            not formula_uses_projection or projection_scoring_bound,
            "formula projection evidence bound to the selected scoring profile",
        ),
        (
            not formula_uses_projection or schedule_complete,
            "NFL schedule required by formula projection features",
        ),
    )
    power_status = (
        "not_ready"
        if power_missing
        else "ready_with_holdout_validated_scope"
        if holdout_validated_power
        else "surrogate"
    )
    expected_standings_missing = _missing_labels(
        (projection_grid_complete, "complete remaining regular-season projection grid"),
        (projection_scoring_bound, "projection evidence bound to the selected scoring profile"),
        (schedule_complete, "complete NFL schedule and player-team binding"),
        (roster_identity_complete, "complete rosters and player eligibility"),
        (standings_complete, "one current standing for every league team"),
        (matchup_schedule_complete, "one remaining matchup for every team and week"),
        (tiebreaker_inputs_ready, tiebreaker_input_requirement),
        (bundle.scenario_config.scenario_count > 0, "valid scenario configuration"),
    )
    expected_standings_status = (
        "not_ready" if expected_standings_missing else "ready_with_limitations"
    )
    playoff_missing = list(expected_standings_missing)
    playoff_missing.extend(
        _missing_labels(
            (
                0 < state.playoff_rules.qualifier_count <= len(team_ids),
                "valid playoff qualifier and seeding rules",
            ),
        )
    )
    playoff_status = (
        "not_ready"
        if playoff_missing
        else "model_estimate_with_limitations"
    )
    trade_missing = _missing_labels(
        (not power_missing, "ready power-score evidence"),
        (not expected_standings_missing, "ready expected-standings evidence"),
        (not playoff_missing, "ready playoff-model evidence"),
        (waiver_pool_complete, "bundle-bound waiver replacement pool"),
    )
    trade_status = "not_ready" if trade_missing else "ready_with_limitations"
    lab_missing = _missing_labels(
        (projection_grid_complete, "complete remaining regular-season projection grid"),
        (bool(bundle.projection_evidence), "retained provider projection evidence"),
        (ecr_pair_complete, "weekly and rest-of-season ECR evidence"),
        (roster_identity_complete, "complete ownership and eligibility evidence"),
    )
    benchmark_complete = (
        {row.team_id for row in bundle.fantasypros_benchmark.teams} == team_ids
        and bundle.fantasypros_benchmark.snapshot_id == state.snapshot_id
    )
    common_evidence = {
        "projection_grid_complete": projection_grid_complete,
        "projection_scoring_profile_bound": projection_scoring_bound,
        "nfl_schedule_complete": schedule_complete,
    }
    power_limitations = (
        [
            "Representative blind holdouts validate the listed balanced, "
            "no-adjustment package shapes; this is not exhaustive proof of every "
            "combination, and every other shape is labeled extrapolated."
        ]
        if holdout_validated_power
        else ["The current power formula is a disclosed local surrogate."]
    )
    if formula_uses_projection and _SCORING_LIMITATION in simulation_limitations:
        power_limitations.append(_SCORING_LIMITATION)
    return {
        "fantasypros_style_power": {
            "status": power_status,
            "uses": [
                "strength_model",
                "rosters",
                "eligibilities",
                *(
                    ["formula_required_ECR_horizons"]
                    if required_ecr_periods
                    else []
                ),
                *(
                    ["formula_required_projection_horizons"]
                    if formula_uses_projection
                    else []
                ),
            ],
            "evidence": {
                **common_evidence,
                "weekly_and_ros_ecr_complete": ecr_pair_complete,
                "required_ecr_periods": sorted(
                    row.value for row in required_ecr_periods
                ),
                "required_ecr_evidence_complete": required_ecr_complete,
                "formula_identity_matches": formula_identity_matches,
                "required_projection_features": sorted(required_features),
                "required_current_providers": sorted(availability.current_providers),
                "required_full_ros_providers": sorted(availability.full_ros_providers),
                "required_projection_evidence_complete": (
                    required_projection_evidence_complete
                ),
                "formula_uses_projection_features": formula_uses_projection,
                "formula_projection_scoring_profile_bound": (
                    not formula_uses_projection or projection_scoring_bound
                ),
            },
            "holdout_validated_scope": (
                {
                    "balanced_package_sizes": list(
                        bundle.methodology_attestation.validated_balanced_package_sizes
                    ),
                    "roster_adjustments": False,
                }
                if holdout_validated_power
                else None
            ),
            "limitations": power_limitations,
            "missing": power_missing,
        },
        "expected_standings": {
            "status": expected_standings_status,
            "uses": [
                "current_standings",
                "remaining_matchups",
                "regular_season_projection_grid",
                "rank_and_seed_rules",
                "scenario_config",
            ],
            "evidence": {
                **common_evidence,
                "standings_complete": standings_complete,
                "remaining_matchup_schedule_complete": matchup_schedule_complete,
                "tiebreaker_inputs_ready": tiebreaker_inputs_ready,
                "roster_and_eligibility_identity_complete": roster_identity_complete,
            },
            "limitations": list(simulation_limitations),
            "missing": expected_standings_missing,
        },
        "playoff_model_estimates": {
            "status": playoff_status,
            "uses": [
                "expected_standings_scenarios",
                "playoff_qualification_rules",
                "rank_and_seed_rules",
            ],
            "evidence": {
                "expected_standings_ready": not expected_standings_missing,
                "qualifier_rules_present": (
                    0 < state.playoff_rules.qualifier_count <= len(team_ids)
                ),
            },
            "limitations": list(simulation_limitations),
            "missing": playoff_missing,
        },
        "trade_search": {
            "status": trade_status,
            "uses": [
                "power_score",
                "rosters",
                "waiver_pool",
                "expected_standings",
                "playoff_model_estimates",
            ],
            "evidence": {
                "power_score_ready": not power_missing,
                "expected_standings_ready": not expected_standings_missing,
                "playoff_model_ready": not playoff_missing,
                "waiver_pool_complete": waiver_pool_complete,
            },
            "limitations": list(simulation_limitations),
            "missing": trade_missing,
        },
        "player_lab": {
            "status": "not_ready" if lab_missing else "ready_with_limitations",
            "uses": [
                "weekly_ensemble",
                "full_horizon_ensemble",
                "provider_evidence",
                "ECR",
                "ownership",
                "eligibility",
            ],
            "evidence": {
                **common_evidence,
                "provider_projection_evidence_retained": bool(bundle.projection_evidence),
                "weekly_and_ros_ecr_complete": ecr_pair_complete,
                "provider_status_observation_count": provider_status_coverage[
                    "observation_count"
                ],
                "provider_status_disagreement_scope_count": (
                    provider_status_coverage["disagreement_scope_count"]
                ),
                "provider_status_is_observation_only": True,
            },
            "limitations": [
                *(
                    [_SCORING_LIMITATION]
                    if _SCORING_LIMITATION in simulation_limitations
                    else []
                ),
                _AVAILABILITY_LIMITATION,
                *(
                    [
                        limitation
                        for limitation in simulation_limitations
                        if limitation == _ROS_ALLOCATION_LIMITATION
                    ]
                ),
                "A provider publication time is shown only when the provider discloses it.",
            ],
            "missing": lab_missing,
        },
        "team_outlook_and_exports": {
            "status": (
                "not_ready"
                if expected_standings_missing or playoff_missing
                else "ready_with_limitations"
            ),
            "uses": [
                "current_standings",
                "projected_record_and_points",
                "rank_distribution",
                "seed_distribution",
                "playoff_model_estimate",
            ],
            "limitations": [
                *simulation_limitations,
                _CHAMPIONSHIP_PROXY_LIMITATION,
            ],
            "missing": sorted(set(expected_standings_missing + playoff_missing)),
        },
        "fantasypros_comparison_benchmark": {
            "status": "comparison_only" if benchmark_complete else "not_ready",
            "uses": [
                "captured_projected_standings",
                "captured_playoff_probability",
                "captured_championship_probability",
            ],
            "evidence": {
                "league_team_coverage_complete": benchmark_complete,
                "team_count": len(bundle.fantasypros_benchmark.teams),
            },
            "limitations": [_FANTASYPROS_BENCHMARK_POLICY],
            "missing": (
                [] if benchmark_complete else ["benchmark coverage for every league team"]
            ),
        },
        "exact_championship_simulation": {
            "status": "not_ready",
            "uses": [],
            "missing": [
                "materialized playoff-week ensemble projections",
                "typed fantasy playoff bracket, bye, tie, and home-bonus rules",
            ],
            "available_fallback": "strength_weighted_playoff_field_proxy",
        },
    }


def _missing_labels(*checks):
    return [label for ready, label in checks if not ready]


def _fantasy_matchup_schedule_complete(bundle):
    state = bundle.state
    expected = {
        (week, team.team_id)
        for week in state.remaining_regular_season_weeks
        for team in state.teams
    }
    actual = {
        (row.week, team_id)
        for row in state.remaining_matchups
        for team_id in (row.team1_id, row.team2_id)
    }
    return actual == expected


def _schedule_evidence_complete(bundle):
    try:
        validate_complete_regular_season(bundle.nfl_schedule)
        for projection in bundle.projections:
            scheduled = bundle.nfl_schedule.team_week(
                projection.nfl_team_id,
                projection.week,
            )
            if scheduled.status is NflTeamWeekStatus.BYE:
                if projection.status is not ProjectionStatus.BYE:
                    return False
            elif (
                projection.status is not ProjectionStatus.OBSERVED
                or projection.nfl_game_id != scheduled.nfl_game_id
                or projection.opponent_team_id != scheduled.opponent_team_id
                or projection.is_home is not scheduled.is_home
            ):
                return False
    except (KeyError, ValueError):
        return False
    return True

