"""Build the feature-oriented readiness report for one engine bundle."""

from collections import defaultdict

from ._current_projection_coverage import build_current_player_projection_coverage
from ._data_readiness_bound_inputs import bound_inputs as _bound_inputs
from ._data_readiness_capabilities import _capability_decisions
from ._data_readiness_evidence import (
    _correlation_limitation,
    _covers_scheduled_playoff_weeks,
    _first_week_games,
    _projection_source_coverage,
    _provider_status_coverage,
    _source_capture_times,
)
from ._data_readiness_policy import (
    _AS_OF_TIME_LIMITATION,
    _AVAILABILITY_LIMITATION,
    _HOST_SETTLEMENT_POLICY_LIMITATION,
    _INDEPENDENT_HOST_SETTLEMENT_POLICY_LIMITATION,
    _INDEPENDENT_SCHEDULE_PROVENANCE_LIMITATION,
    _MARGINAL_UNCERTAINTY_LIMITATION,
    _ROS_ALLOCATION_LIMITATION,
    _SCORING_LIMITATION,
)
from ._data_readiness_time import timestamp_text as _timestamp
from .engine_bundle import EngineBundle
from .nfl_schedule import NflTeamWeekStatus
from .projection_lineage import ProjectionLineageIndex
from .projections import ProjectionStatus, WeeklyProjectionOrigin
from .remaining_projection import summarize_remaining_projection


def build_bundle_data_readiness(
    bundle: EngineBundle,
    *,
    player_projection_positions=None,
) -> dict[str, object]:
    """Describe usable, inferred, and missing data without changing readiness gates."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    players = {row.canonical_player_id for row in bundle.projections}
    weeks = bundle.state.remaining_regular_season_weeks
    ensemble_cells = len(bundle.projections)
    provider_cells = [
        (projection, observation)
        for projection in bundle.projections
        for observation in projection.provider_observations
    ]
    lineage = ProjectionLineageIndex(
        bundle.projections,
        bundle.projection_evidence,
    )
    direct = derived = schedule_derived = unavailable = unattributed = 0
    for projection, observation in provider_cells:
        source = lineage.lineage_for(projection, observation)
        if observation.status is ProjectionStatus.OBSERVED:
            if source.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED:
                direct += 1
            elif source.origin is WeeklyProjectionOrigin.DERIVED_REST_OF_SEASON:
                derived += 1
            else:
                unattributed += 1
        elif observation.status is ProjectionStatus.BYE:
            if source.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED:
                direct += 1
            else:
                schedule_derived += 1
        else:
            unavailable += 1

    projections_by_player = defaultdict(list)
    nfl_team_by_player = {}
    for projection in bundle.projections:
        projections_by_player[projection.canonical_player_id].append(projection)
        nfl_team_by_player[projection.canonical_player_id] = projection.nfl_team_id
    configured_providers = (
        tuple(row.provider for row in bundle.ensemble_config.provider_weights)
        if bundle.ensemble_config is not None
        else tuple(
            sorted(
                {
                    observation.provider
                    for projection in bundle.projections
                    for observation in projection.provider_observations
                }
            )
        )
    )
    full_horizon_provider_availability = {
        provider: {"available_players": 0, "unavailable_players": 0}
        for provider in configured_providers
    }
    available_full_horizon_ensembles = 0
    for player_id, player_projections in sorted(projections_by_player.items()):
        nfl_team_id = nfl_team_by_player[player_id]
        if bundle.nfl_schedule is None:
            scope = tuple(
                row.week
                for row in player_projections
                if row.status is ProjectionStatus.OBSERVED
            )
        else:
            scope = tuple(
                row.week
                for row in bundle.nfl_schedule.team_weeks
                if row.nfl_team_id == nfl_team_id
                and row.week >= bundle.state.first_remaining_week
                and row.status is NflTeamWeekStatus.SCHEDULED
            )
        summary = summarize_remaining_projection(
            player_projections,
            lineage,
            applicable_weeks=scope,
        )
        assert summary is not None
        available_full_horizon_ensembles += int(
            summary.projected_fantasy_points is not None
        )
        for observation in summary.provider_observations:
            status_key = (
                "available_players"
                if observation.status is ProjectionStatus.OBSERVED
                else "unavailable_players"
            )
            full_horizon_provider_availability.setdefault(
                observation.provider,
                {"available_players": 0, "unavailable_players": 0},
            )[status_key] += 1

    full_horizon_provider_cells = sum(
        sum(provider.values())
        for provider in full_horizon_provider_availability.values()
    )
    available_full_horizon_provider_cells = sum(
        provider["available_players"]
        for provider in full_horizon_provider_availability.values()
    )

    observed_evidence = tuple(
        row
        for row in bundle.projection_evidence
        if row.status is ProjectionStatus.OBSERVED
    )
    provider_status_coverage = _provider_status_coverage(
        bundle.projection_evidence
    )
    disclosed_publication_times = sum(
        row.source_published_at is not None for row in bundle.projection_evidence
    )
    playoff_weeks = set(bundle.state.playoff_rules.playoff_weeks)
    ros_rows = tuple(
        row
        for row in lineage.remaining_season_rows()
        if row.canonical_player_id in players
        and row.status is ProjectionStatus.OBSERVED
    )
    postseason_ros_rows = sum(
        _covers_scheduled_playoff_weeks(
            row,
            nfl_team_by_player[row.canonical_player_id],
            bundle,
            playoff_weeks,
        )
        for row in ros_rows
    )
    capture_times = _source_capture_times(bundle)
    earliest_capture_at = min(capture_times)
    latest_capture_at = max(capture_times)
    independent_loadings = (
        bundle.scenario_config.loadings.league == 0
        and bundle.scenario_config.loadings.game == 0
        and bundle.scenario_config.loadings.nfl_team == 0
    )
    first_week_games = _first_week_games(bundle)
    missing_first_week_kickoffs = sum(
        getattr(row, "kickoff_at", None) is None for row in first_week_games
    )
    as_of_time_limitation = (
        _AS_OF_TIME_LIMITATION if missing_first_week_kickoffs else None
    )
    correlation_limitation = _correlation_limitation(independent_loadings)
    ros_allocation_limitation = _ROS_ALLOCATION_LIMITATION if derived else None
    holdout_validated_power = bundle.methodology_mode == "holdout_validated"
    projection_source_coverage = _projection_source_coverage(bundle)
    exact_projection_scoring = (
        bundle.projection_source_manifest is not None
        and projection_source_coverage["base_format_only_sources"] == 0
        and projection_source_coverage["provider_total_sources"] == 0
    )
    simulation_limitations = [
        *([] if exact_projection_scoring else [_SCORING_LIMITATION]),
        _AVAILABILITY_LIMITATION,
        correlation_limitation,
        _MARGINAL_UNCERTAINTY_LIMITATION,
        *([as_of_time_limitation] if as_of_time_limitation else []),
        *([ros_allocation_limitation] if ros_allocation_limitation else []),
        *(
            [_INDEPENDENT_SCHEDULE_PROVENANCE_LIMITATION]
            if bundle.nfl_schedule is None
            else []
        ),
        (
            _INDEPENDENT_HOST_SETTLEMENT_POLICY_LIMITATION
            if bundle.methodology_mode == "independent"
            else _HOST_SETTLEMENT_POLICY_LIMITATION
        ),
    ]

    capabilities = _capability_decisions(
        bundle,
        projections_by_player=projections_by_player,
        full_horizon_provider_availability=full_horizon_provider_availability,
        available_full_horizon_ensembles=available_full_horizon_ensembles,
        provider_status_coverage=provider_status_coverage,
        simulation_limitations=simulation_limitations,
        holdout_validated_power=holdout_validated_power,
    )
    core_ready = all(
        capabilities[name]["status"] != "not_ready"
        for name in (
            "fantasypros_style_power",
            "trade_search",
            "expected_standings",
            "playoff_model_estimates",
        )
    )

    return {
        "schema_version": 5,
        "status": (
            "ready_with_known_limitations" if core_ready else "not_ready"
        ),
        "bound_inputs": _bound_inputs(bundle),
        "calculation_domain": {
            "player_count": len(players),
            "remaining_regular_season_weeks": list(weeks),
            "ensemble_player_week_count": ensemble_cells,
            "provider_cell_count": len(provider_cells),
        },
        "coverage": {
            "direct_provider_cells": direct,
            "ros_derived_provider_cells": derived,
            "schedule_derived_availability_cells": schedule_derived,
            "unavailable_provider_cells": unavailable,
            "unattributed_provider_cells": unattributed,
            "first_week_scheduled_games": len(first_week_games),
            "first_week_games_missing_kickoff": missing_first_week_kickoffs,
            "observed_projection_evidence_rows": len(observed_evidence),
            "observed_rows_with_raw_stats": sum(
                bool(row.raw_projected_stats) for row in observed_evidence
            ),
            "rows_with_source_publication_time": disclosed_publication_times,
            "ros_rows": len(ros_rows),
            "ros_rows_covering_all_fantasy_playoff_weeks": postseason_ros_rows,
            "full_horizon_provider_cells": full_horizon_provider_cells,
            "available_full_horizon_provider_cells": (
                available_full_horizon_provider_cells
            ),
            "unavailable_full_horizon_provider_cells": (
                full_horizon_provider_cells
                - available_full_horizon_provider_cells
            ),
            "full_horizon_provider_availability": (
                full_horizon_provider_availability
            ),
            "available_full_horizon_ensemble_players": (
                available_full_horizon_ensembles
            ),
            "unavailable_full_horizon_ensemble_players": (
                len(projections_by_player) - available_full_horizon_ensembles
            ),
            "source_capture_timestamp_count": len(capture_times),
            "earliest_capture_at": _timestamp(earliest_capture_at),
            "latest_capture_at": _timestamp(latest_capture_at),
            "capture_window_seconds": int(
                (latest_capture_at - earliest_capture_at).total_seconds()
            ),
            "fantasypros_comparison_team_count": (
                0
                if bundle.fantasypros_benchmark is None
                else len(bundle.fantasypros_benchmark.teams)
            ),
            "projection_sources": projection_source_coverage,
            "provider_status_observations": provider_status_coverage,
            "current_player_projection_audit": (
                build_current_player_projection_coverage(
                    bundle,
                    positions=player_projection_positions,
                )
            ),
        },
        "capabilities": capabilities,
        "missing_data_plan": [
            {
                "data": "host_trade_legality",
                "strategy": (
                    "Capture a timestamped host transaction-policy sidecar once per "
                    "refresh, including the deadline/window, processing rules, player "
                    "locks, undroppable flags, and pending transactions. Bind it to the "
                    "league, season, host snapshot, and analysis time."
                ),
            },
            {
                "data": "exact_projection_scoring_compatibility",
                "strategy": (
                    "Retain complete provider stat components and recompute points with "
                    "the captured host ScoringProfile; reject unsupported custom rules."
                ),
            },
            {
                "data": "player_week_availability",
                "strategy": (
                    "Join retained provider status observations with public NFL injury "
                    "reports and historical appearances in a timestamped, bundle-bound "
                    "availability sidecar; calibrate probabilities instead of treating "
                    "a provider label as a certain outcome."
                ),
            },
            {
                "data": "postseason_schedule_and_bracket",
                "strategy": (
                    "Use the persisted NFL schedule, capture exact host bracket rules, "
                    "then materialize and simulate the playoff weeks."
                ),
            },
            {
                "data": "calibrated_outcome_correlation",
                "strategy": (
                    "Store timestamped forecasts and actual results by player/game/week, "
                    "fit versioned factor loadings locally, and retain the fit evidence."
                ),
            },
            {
                "data": "history_profiles_and_draft",
                "strategy": (
                    "Keep append-only league history, public player profiles, and draft "
                    "observations as separately versioned datasets bound to league and bundle IDs."
                ),
            },
        ],
    }
