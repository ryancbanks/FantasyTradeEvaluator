"""Immutable coverage and limitation contract used by durable outputs."""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from numbers import Real

from ._data_readiness_time import aware_datetime as _aware


@dataclass(frozen=True, slots=True)
class DataReadinessSnapshot:
    """Small immutable coverage and limitation contract for durable outputs."""

    provider_cell_count: int
    direct_provider_cells: int
    ros_derived_provider_cells: int
    schedule_derived_availability_cells: int
    unavailable_provider_cells: int
    unattributed_provider_cells: int
    first_week_scheduled_games: int
    first_week_games_missing_kickoff: int
    source_capture_timestamp_count: int
    earliest_source_capture_at: datetime
    latest_source_capture_at: datetime
    scenario_player_score_floor: float | None
    fantasypros_comparison_team_count: int
    fantasypros_comparison_policy: str
    projection_source_count: int
    captured_projection_source_attempts: int
    not_published_projection_source_attempts: int
    unavailable_projection_source_attempts: int
    provider_total_projection_sources: int
    locally_recomputed_projection_sources: int
    base_format_only_projection_sources: int
    exact_host_rules_projection_sources: int
    projection_source_scoring_formats: tuple[str, ...]
    projection_source_provider_attempts: tuple[
        tuple[str, int, int, int], ...
    ]
    provider_status_observation_count: int
    provider_status_disagreement_scope_count: int
    latest_provider_status_observed_at: datetime | None
    power_score_status: str
    trade_search_status: str
    expected_standings_status: str
    playoff_model_status: str
    custom_scoring_limitation: str | None
    availability_limitation: str
    correlation_limitation: str
    marginal_uncertainty_limitation: str
    championship_proxy_limitation: str
    host_settlement_policy_limitation: str
    as_of_time_limitation: str | None
    ros_allocation_limitation: str | None

    def __post_init__(self) -> None:
        counts = (
            self.provider_cell_count,
            self.direct_provider_cells,
            self.ros_derived_provider_cells,
            self.schedule_derived_availability_cells,
            self.unavailable_provider_cells,
            self.unattributed_provider_cells,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError(
                "data-readiness coverage counts must be non-negative integers"
            )
        if self.provider_cell_count != sum(counts[1:]):
            raise ValueError(
                "data-readiness coverage categories must cover every provider cell"
            )
        if (
            type(self.first_week_scheduled_games) is not int
            or type(self.first_week_games_missing_kickoff) is not int
            or self.first_week_scheduled_games < 0
            or not (
                0
                <= self.first_week_games_missing_kickoff
                <= self.first_week_scheduled_games
            )
        ):
            raise ValueError("first-week kickoff coverage counts are invalid")
        if (
            type(self.source_capture_timestamp_count) is not int
            or self.source_capture_timestamp_count < 1
        ):
            raise ValueError("source_capture_timestamp_count must be positive")
        earliest = _aware(
            "earliest_source_capture_at", self.earliest_source_capture_at
        )
        latest = _aware("latest_source_capture_at", self.latest_source_capture_at)
        if earliest > latest:
            raise ValueError("source capture window is reversed")
        object.__setattr__(self, "earliest_source_capture_at", earliest)
        object.__setattr__(self, "latest_source_capture_at", latest)
        if self.scenario_player_score_floor is not None:
            if (
                isinstance(self.scenario_player_score_floor, bool)
                or not isinstance(self.scenario_player_score_floor, Real)
                or not isfinite(float(self.scenario_player_score_floor))
            ):
                raise ValueError(
                    "scenario_player_score_floor must be finite numeric data or None"
                )
            object.__setattr__(
                self,
                "scenario_player_score_floor",
                float(self.scenario_player_score_floor),
            )
        if (
            type(self.fantasypros_comparison_team_count) is not int
            or self.fantasypros_comparison_team_count < 1
        ):
            raise ValueError("fantasypros_comparison_team_count must be positive")
        source_counts = (
            self.projection_source_count,
            self.captured_projection_source_attempts,
            self.not_published_projection_source_attempts,
            self.unavailable_projection_source_attempts,
            self.provider_total_projection_sources,
            self.locally_recomputed_projection_sources,
            self.base_format_only_projection_sources,
            self.exact_host_rules_projection_sources,
        )
        if any(type(value) is not int or value < 0 for value in source_counts):
            raise ValueError("projection source coverage counts must be non-negative integers")
        if self.projection_source_count < 1:
            raise ValueError("projection_source_count must be positive")
        if self.projection_source_count != (
            self.provider_total_projection_sources
            + self.locally_recomputed_projection_sources
        ) or self.projection_source_count != (
            self.base_format_only_projection_sources
            + self.exact_host_rules_projection_sources
        ):
            raise ValueError("projection source policy counts must cover every source")
        formats = tuple(self.projection_source_scoring_formats)
        if not formats or any(
            not isinstance(value, str) or not value.strip() for value in formats
        ) or len(set(formats)) != len(formats):
            raise ValueError(
                "projection_source_scoring_formats must contain distinct text"
            )
        object.__setattr__(
            self,
            "projection_source_scoring_formats",
            tuple(sorted(value.strip() for value in formats)),
        )
        provider_attempts = tuple(self.projection_source_provider_attempts)
        if not provider_attempts:
            raise ValueError("projection_source_provider_attempts cannot be empty")
        providers = set()
        normalized_attempts = []
        for row in provider_attempts:
            if (
                not isinstance(row, tuple)
                or len(row) != 4
                or not isinstance(row[0], str)
                or not row[0].strip()
                or any(type(value) is not int or value < 0 for value in row[1:])
            ):
                raise ValueError("projection source provider attempt coverage is invalid")
            provider = row[0].strip()
            if provider in providers:
                raise ValueError("projection source provider attempt coverage is duplicated")
            providers.add(provider)
            normalized_attempts.append((provider, *row[1:]))
        object.__setattr__(
            self,
            "projection_source_provider_attempts",
            tuple(sorted(normalized_attempts)),
        )
        provider_totals = tuple(
            sum(row[index] for row in normalized_attempts)
            for index in range(1, 4)
        )
        if provider_totals != (
            self.captured_projection_source_attempts,
            self.not_published_projection_source_attempts,
            self.unavailable_projection_source_attempts,
        ):
            raise ValueError(
                "projection source provider attempts must reconcile to totals"
            )
        if (
            type(self.provider_status_observation_count) is not int
            or type(self.provider_status_disagreement_scope_count) is not int
            or self.provider_status_observation_count < 0
            or self.provider_status_disagreement_scope_count < 0
            or self.provider_status_disagreement_scope_count
            > self.provider_status_observation_count
        ):
            raise ValueError("provider status observation coverage is invalid")
        if self.latest_provider_status_observed_at is not None:
            object.__setattr__(
                self,
                "latest_provider_status_observed_at",
                _aware(
                    "latest_provider_status_observed_at",
                    self.latest_provider_status_observed_at,
                ),
            )
        if bool(self.latest_provider_status_observed_at) != bool(
            self.provider_status_observation_count
        ):
            raise ValueError(
                "provider status observation time must match retained coverage"
            )
        allowed_statuses = {
            "power_score_status": {
                "ready_with_holdout_validated_scope",
                "surrogate",
                "not_ready",
            },
            "trade_search_status": {"ready_with_limitations", "not_ready"},
            "expected_standings_status": {
                "ready_with_limitations",
                "not_ready",
            },
            "playoff_model_status": {
                "model_estimate_with_limitations",
                "not_ready",
            },
        }
        for name, allowed in allowed_statuses.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"{name} is invalid")
        for name in (
            "availability_limitation",
            "correlation_limitation",
            "marginal_uncertainty_limitation",
            "championship_proxy_limitation",
            "host_settlement_policy_limitation",
            "fantasypros_comparison_policy",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.custom_scoring_limitation is not None:
            if (
                not isinstance(self.custom_scoring_limitation, str)
                or not self.custom_scoring_limitation.strip()
            ):
                raise ValueError(
                    "custom_scoring_limitation must be non-empty text or None"
                )
            object.__setattr__(
                self,
                "custom_scoring_limitation",
                self.custom_scoring_limitation.strip(),
            )
        if self.as_of_time_limitation is not None:
            if (
                not isinstance(self.as_of_time_limitation, str)
                or not self.as_of_time_limitation.strip()
            ):
                raise ValueError(
                    "as_of_time_limitation must be a non-empty string or None"
                )
            object.__setattr__(
                self,
                "as_of_time_limitation",
                self.as_of_time_limitation.strip(),
            )
        if bool(self.as_of_time_limitation) != bool(
            self.first_week_games_missing_kickoff
        ):
            raise ValueError("as-of-time limitation must match missing kickoff coverage")
        if self.ros_allocation_limitation is not None:
            if (
                not isinstance(self.ros_allocation_limitation, str)
                or not self.ros_allocation_limitation.strip()
            ):
                raise ValueError(
                    "ros_allocation_limitation must be a non-empty string or None"
                )
            object.__setattr__(
                self,
                "ros_allocation_limitation",
                self.ros_allocation_limitation.strip(),
            )
        if bool(self.ros_allocation_limitation) != bool(
            self.ros_derived_provider_cells
        ):
            raise ValueError(
                "ROS-allocation limitation must match derived projection coverage"
            )

