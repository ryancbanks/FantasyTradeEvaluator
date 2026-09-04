"""Construct the compact readiness snapshot embedded in durable exports."""

from ._data_readiness_evidence import (
    _correlation_limitation,
    _source_capture_times,
)
from ._data_readiness_model import DataReadinessSnapshot
from ._data_readiness_policy import (
    _AS_OF_TIME_LIMITATION,
    _AVAILABILITY_LIMITATION,
    _CHAMPIONSHIP_PROXY_LIMITATION,
    _FANTASYPROS_BENCHMARK_POLICY,
    _HOST_SETTLEMENT_POLICY_LIMITATION,
    _INDEPENDENT_FANTASYPROS_BENCHMARK_POLICY,
    _INDEPENDENT_HOST_SETTLEMENT_POLICY_LIMITATION,
    _MARGINAL_UNCERTAINTY_LIMITATION,
    _ROS_ALLOCATION_LIMITATION,
    _SCORING_LIMITATION,
)
from ._data_readiness_report import build_bundle_data_readiness
from ._data_readiness_time import parse_timestamp_text as _parse_timestamp
from .engine_bundle import EngineBundle


def build_data_readiness_snapshot(bundle: EngineBundle) -> DataReadinessSnapshot:
    """Freeze the coverage and limitations needed by exported results."""

    report = build_bundle_data_readiness(bundle)
    coverage = report["coverage"]
    capabilities = report["capabilities"]
    capture_times = _source_capture_times(bundle)
    independent_loadings = (
        bundle.scenario_config.loadings.league == 0
        and bundle.scenario_config.loadings.game == 0
        and bundle.scenario_config.loadings.nfl_team == 0
    )
    return DataReadinessSnapshot(
        provider_cell_count=report["calculation_domain"]["provider_cell_count"],
        direct_provider_cells=coverage["direct_provider_cells"],
        ros_derived_provider_cells=coverage["ros_derived_provider_cells"],
        schedule_derived_availability_cells=coverage[
            "schedule_derived_availability_cells"
        ],
        unavailable_provider_cells=coverage["unavailable_provider_cells"],
        unattributed_provider_cells=coverage["unattributed_provider_cells"],
        first_week_scheduled_games=coverage["first_week_scheduled_games"],
        first_week_games_missing_kickoff=coverage[
            "first_week_games_missing_kickoff"
        ],
        source_capture_timestamp_count=len(capture_times),
        earliest_source_capture_at=min(capture_times),
        latest_source_capture_at=max(capture_times),
        scenario_player_score_floor=bundle.scenario_config.player_score_floor,
        fantasypros_comparison_team_count=coverage[
            "fantasypros_comparison_team_count"
        ],
        fantasypros_comparison_policy=(
            _INDEPENDENT_FANTASYPROS_BENCHMARK_POLICY
            if bundle.methodology_mode == "independent"
            else _FANTASYPROS_BENCHMARK_POLICY
        ),
        projection_source_count=coverage["projection_sources"]["source_count"],
        captured_projection_source_attempts=coverage["projection_sources"][
            "captured_attempts"
        ],
        not_published_projection_source_attempts=coverage["projection_sources"][
            "not_published_attempts"
        ],
        unavailable_projection_source_attempts=coverage["projection_sources"][
            "unavailable_attempts"
        ],
        provider_total_projection_sources=coverage["projection_sources"][
            "provider_total_sources"
        ],
        locally_recomputed_projection_sources=coverage["projection_sources"][
            "locally_recomputed_sources"
        ],
        base_format_only_projection_sources=coverage["projection_sources"][
            "base_format_only_sources"
        ],
        exact_host_rules_projection_sources=coverage["projection_sources"][
            "exact_host_rules_sources"
        ],
        projection_source_scoring_formats=tuple(
            coverage["projection_sources"]["source_scoring_formats"]
        ),
        projection_source_provider_attempts=tuple(
            (
                provider,
                values["captured_attempts"],
                values["not_published_attempts"],
                values["unavailable_attempts"],
            )
            for provider, values in coverage["projection_sources"][
                "providers"
            ].items()
            if any(
                values[name]
                for name in (
                    "captured_attempts",
                    "not_published_attempts",
                    "unavailable_attempts",
                )
            )
        ),
        provider_status_observation_count=coverage[
            "provider_status_observations"
        ]["observation_count"],
        provider_status_disagreement_scope_count=coverage[
            "provider_status_observations"
        ]["disagreement_scope_count"],
        latest_provider_status_observed_at=(
            None
            if coverage["provider_status_observations"]["latest_observed_at"]
            is None
            else _parse_timestamp(
                coverage["provider_status_observations"]["latest_observed_at"]
            )
        ),
        power_score_status=capabilities["fantasypros_style_power"]["status"],
        trade_search_status=capabilities["trade_search"]["status"],
        expected_standings_status=capabilities["expected_standings"]["status"],
        playoff_model_status=capabilities["playoff_model_estimates"]["status"],
        custom_scoring_limitation=(
            _SCORING_LIMITATION
            if bundle.projection_source_manifest is None
            or coverage["projection_sources"]["base_format_only_sources"]
            or coverage["projection_sources"]["provider_total_sources"]
            else None
        ),
        availability_limitation=_AVAILABILITY_LIMITATION,
        correlation_limitation=_correlation_limitation(independent_loadings),
        marginal_uncertainty_limitation=_MARGINAL_UNCERTAINTY_LIMITATION,
        championship_proxy_limitation=_CHAMPIONSHIP_PROXY_LIMITATION,
        host_settlement_policy_limitation=(
            _INDEPENDENT_HOST_SETTLEMENT_POLICY_LIMITATION
            if bundle.methodology_mode == "independent"
            else _HOST_SETTLEMENT_POLICY_LIMITATION
        ),
        as_of_time_limitation=(
            _AS_OF_TIME_LIMITATION
            if coverage["first_week_games_missing_kickoff"]
            else None
        ),
        ros_allocation_limitation=(
            _ROS_ALLOCATION_LIMITATION
            if coverage["ros_derived_provider_cells"]
            else None
        ),
    )
