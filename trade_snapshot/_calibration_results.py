"""Configuration and audited outputs for the calibration solver."""

from dataclasses import dataclass, field
from math import fsum
from types import MappingProxyType
from typing import Mapping

from ._calibration_inputs import CalibrationCorpus
from .strength import CalibrationStatus, StrengthModel
from .strength_calibration import (
    _content_id,
    _finite_number as _finite,
    _nonempty_string,
    _normalized_names as _names,
)


CALIBRATION_SOLVER_ALGORITHM = "alternating-nonnegative-role-ridge-v3"
MINIMUM_EXACT_TRADE_COUNT = 100


@dataclass(frozen=True, slots=True)
class CalibrationFitConfig:
    residual_feature_names: tuple[str, ...]
    role_feature_names: tuple[str, ...]
    ridge_penalty: float = 1e-8
    max_outer_iterations: int = 40
    max_coordinate_iterations: int = 800
    convergence_tolerance: float = 1e-9
    minimum_exact_holdouts: int = MINIMUM_EXACT_TRADE_COUNT
    exact_raw_tolerance: float = 1e-6
    config_id: str = field(init=False)

    def __post_init__(self) -> None:
        residual = tuple(sorted(_names("residual_feature_names", self.residual_feature_names)))
        roles = tuple(sorted(_names("role_feature_names", self.role_feature_names)))
        ridge = _finite("ridge_penalty", self.ridge_penalty)
        tolerance = _finite("convergence_tolerance", self.convergence_tolerance)
        exact_tolerance = _finite("exact_raw_tolerance", self.exact_raw_tolerance)
        if ridge < 0 or tolerance <= 0 or not 0 < exact_tolerance <= 1e-6:
            raise ValueError(
                "ridge_penalty must be non-negative; tolerances must be positive, "
                "and exact_raw_tolerance cannot exceed 1e-6"
            )
        for name in (
            "max_outer_iterations",
            "max_coordinate_iterations",
            "minimum_exact_holdouts",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.minimum_exact_holdouts < MINIMUM_EXACT_TRADE_COUNT:
            raise ValueError(
                f"minimum_exact_holdouts cannot be below {MINIMUM_EXACT_TRADE_COUNT}"
            )
        object.__setattr__(self, "residual_feature_names", residual)
        object.__setattr__(self, "role_feature_names", roles)
        object.__setattr__(self, "ridge_penalty", ridge)
        object.__setattr__(self, "convergence_tolerance", tolerance)
        object.__setattr__(self, "exact_raw_tolerance", exact_tolerance)
        object.__setattr__(self, "config_id", _content_id("calibration-config", self._record()))

    def _record(self) -> dict[str, object]:
        return {
            "algorithm": CALIBRATION_SOLVER_ALGORITHM,
            "convergence_tolerance": self.convergence_tolerance,
            "exact_raw_tolerance": self.exact_raw_tolerance,
            "max_coordinate_iterations": self.max_coordinate_iterations,
            "max_outer_iterations": self.max_outer_iterations,
            "minimum_exact_holdouts": self.minimum_exact_holdouts,
            "residual_feature_names": list(self.residual_feature_names),
            "ridge_penalty": self.ridge_penalty,
            "role_feature_names": list(self.role_feature_names),
        }

    def to_record(self) -> dict[str, object]:
        return {**self._record(), "config_id": self.config_id, "schema_version": 1}


@dataclass(frozen=True, slots=True)
class CalibrationDiagnostics:
    converged: bool
    outer_iterations: int
    coefficient_count: int
    design_rank: int
    training_sample_count: int
    held_out_trade_count: int
    held_out_distinct_perturbation_count: int
    training_rmse: float
    training_max_absolute_error: float
    holdout_max_absolute_score_error: float | None
    holdout_max_delta_error: float | None
    holdout_display_match_rate: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.converged, bool):
            raise ValueError("converged must be a boolean")
        for name in (
            "outer_iterations",
            "coefficient_count",
            "design_rank",
            "training_sample_count",
            "held_out_trade_count",
            "held_out_distinct_perturbation_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.design_rank > self.coefficient_count:
            raise ValueError("design_rank cannot exceed coefficient_count")
        if self.held_out_distinct_perturbation_count > self.held_out_trade_count:
            raise ValueError(
                "held_out_distinct_perturbation_count cannot exceed held_out_trade_count"
            )
        rmse = _finite("training_rmse", self.training_rmse)
        training_max = _finite(
            "training_max_absolute_error", self.training_max_absolute_error
        )
        if rmse < 0 or training_max < 0:
            raise ValueError("training errors must be non-negative")
        score_error = self.holdout_max_absolute_score_error
        delta = self.holdout_max_delta_error
        rate = self.holdout_display_match_rate
        if self.held_out_trade_count == 0:
            if score_error is not None or delta is not None or rate is not None:
                raise ValueError("holdout metrics require held-out trades")
        else:
            score_error = _finite("holdout_max_absolute_score_error", score_error)
            delta = _finite("holdout_max_delta_error", delta)
            rate = _finite("holdout_display_match_rate", rate)
            if score_error < 0 or delta < 0 or not 0 <= rate <= 1:
                raise ValueError("holdout metrics are outside their supported range")
        object.__setattr__(self, "training_rmse", rmse)
        object.__setattr__(self, "training_max_absolute_error", training_max)
        object.__setattr__(self, "holdout_max_absolute_score_error", score_error)
        object.__setattr__(self, "holdout_max_delta_error", delta)
        object.__setattr__(self, "holdout_display_match_rate", rate)

    @property
    def identifiable(self) -> bool:
        return self.design_rank == self.coefficient_count

    def to_record(self) -> dict[str, object]:
        return {
            "coefficient_count": self.coefficient_count,
            "converged": self.converged,
            "design_rank": self.design_rank,
            "held_out_trade_count": self.held_out_trade_count,
            "held_out_distinct_perturbation_count": (
                self.held_out_distinct_perturbation_count
            ),
            "holdout_display_match_rate": self.holdout_display_match_rate,
            "holdout_max_absolute_score_error": self.holdout_max_absolute_score_error,
            "holdout_max_delta_error": self.holdout_max_delta_error,
            "outer_iterations": self.outer_iterations,
            "training_max_absolute_error": self.training_max_absolute_error,
            "training_rmse": self.training_rmse,
            "training_sample_count": self.training_sample_count,
        }


@dataclass(frozen=True, slots=True)
class FittedStrengthCalibration:
    model: StrengthModel
    corpus: CalibrationCorpus
    config: CalibrationFitConfig
    residual_weights: Mapping[str, float]
    role_weights: Mapping[str, Mapping[str, float]]
    diagnostics: CalibrationDiagnostics
    solver_algorithm: str = CALIBRATION_SOLVER_ALGORITHM
    corpus_id: str = field(init=False)
    fit_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model, StrengthModel):
            raise ValueError("model must be a StrengthModel")
        if not isinstance(self.corpus, CalibrationCorpus):
            raise ValueError("corpus must be a CalibrationCorpus")
        corpus_id = self.corpus.corpus_id
        if not isinstance(self.config, CalibrationFitConfig):
            raise ValueError("config must be a CalibrationFitConfig")
        if not isinstance(self.diagnostics, CalibrationDiagnostics):
            raise ValueError("diagnostics must be CalibrationDiagnostics")
        if self.solver_algorithm != CALIBRATION_SOLVER_ALGORITHM:
            raise ValueError("solver_algorithm is unsupported")
        residual = _freeze_weights("residual_weights", self.residual_weights)
        role_rows = {
            _nonempty_string("role_id", role): _freeze_weights("role weights", weights)
            for role, weights in self.role_weights.items()
        } if isinstance(self.role_weights, Mapping) else {}
        if set(role_rows) != {row.role_id for row in self.model.role_definitions}:
            raise ValueError("role_weights must exactly cover the model roles")
        if set(residual) != set(self.config.residual_feature_names) or any(
            set(weights) != set(self.config.role_feature_names)
            for weights in role_rows.values()
        ):
            raise ValueError("reported weight features do not match the fit config")
        if any(value < 0 for weights in role_rows.values() for value in weights.values()):
            raise ValueError("role weights must be non-negative")
        metadata = self.model.calibration
        if (
            metadata.held_out_trade_count != self.diagnostics.held_out_trade_count
            or metadata.max_absolute_score_error
            != self.diagnostics.holdout_max_absolute_score_error
            or metadata.display_match_rate != self.diagnostics.holdout_display_match_rate
        ):
            raise ValueError("model metadata and diagnostics disagree")
        if metadata.status is CalibrationStatus.EXACT and not (
            self.diagnostics.converged
            and self.diagnostics.identifiable
            and self.diagnostics.training_max_absolute_error <= self.config.exact_raw_tolerance
            and self.diagnostics.held_out_distinct_perturbation_count
            >= self.config.minimum_exact_holdouts
        ):
            raise ValueError("exact model lacks diverse, converged, identified evidence")
        _validate_model_coefficients(self.model, self.corpus, residual, role_rows)
        roles = MappingProxyType(dict(sorted(role_rows.items())))
        object.__setattr__(self, "corpus_id", corpus_id)
        object.__setattr__(self, "residual_weights", residual)
        object.__setattr__(self, "role_weights", roles)
        object.__setattr__(
            self,
            "fit_id",
            _content_id(
                "strength-fit",
                {
                    "config": self.config.to_record(),
                    "corpus_id": corpus_id,
                    "diagnostics": self.diagnostics.to_record(),
                    "model_id": self.model.model_id,
                    "residual_weights": dict(residual),
                    "role_weights": {
                        key: dict(value) for key, value in roles.items()
                    },
                    "solver_algorithm": self.solver_algorithm,
                },
            ),
        )


def _freeze_weights(name: str, values: Mapping[str, float]) -> Mapping[str, float]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError(f"{name} must be a non-empty mapping")
    return MappingProxyType(
        dict(
            sorted(
                (
                    _nonempty_string("feature name", key),
                    _finite("feature weight", value),
                )
                for key, value in values.items()
            )
        )
    )


def _validate_model_coefficients(model, corpus, residual, role_rows) -> None:
    if (
        model.snapshot_id != corpus.snapshot_id
        or model.season != corpus.season
        or model.scoring_profile_id != corpus.scoring_profile_id
        or model.role_definitions != corpus.role_definitions
    ):
        raise ValueError("model identity or roles do not match the calibration corpus")
    features = {row.player_id: row for row in corpus.player_features}
    if set(model.players) != set(features):
        raise ValueError("model players do not exactly cover calibration features")
    for player_id, feature in features.items():
        player = model.players[player_id]
        expected_residual = fsum(
            residual[name] * feature.values[name] for name in residual
        )
        expected_roles = {
            role.role_id: fsum(
                role_rows[role.role_id][name] * feature.values[name]
                for name in role_rows[role.role_id]
            )
            for role in corpus.role_definitions
            if feature.eligible_positions.intersection(role.eligible_positions)
        }
        if (
            player.eligible_positions != feature.eligible_positions
            or player.residual_score != expected_residual
            or dict(player.assignment_score_by_role) != expected_roles
        ):
            raise ValueError("reported weights do not reproduce the strength model")
    baseline_max = max(
        model.score_roster(roster).absolute_score
        for roster in corpus.baseline_rosters.values()
    )
    if model.normalization_denominator != baseline_max:
        raise ValueError("model denominator does not match the baseline league maximum")
