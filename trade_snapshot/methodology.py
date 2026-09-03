"""Auditable defaults separating FantasyPros power from playoff projections."""

from dataclasses import dataclass, field

from ._scenario_random import content_id
from .calibration_fit import CalibrationFitConfig
from .ensemble import EnsembleConfig, ProviderWeight


__all__ = (
    "DEFAULT_POWER_METHODOLOGY",
    "PowerMethodology",
    "default_projection_ensemble",
)


@dataclass(frozen=True, slots=True)
class PowerMethodology:
    """The small FantasyPros-only feature policy used to fit roster power."""

    residual_feature_names: tuple[str, ...]
    role_feature_names: tuple[str, ...]
    methodology_id: str = field(init=False)

    def __post_init__(self) -> None:
        residual = _names("residual_feature_names", self.residual_feature_names)
        roles = _names("role_feature_names", self.role_feature_names)
        _validate_fantasypros_power_features(residual, roles)
        object.__setattr__(self, "residual_feature_names", residual)
        object.__setattr__(self, "role_feature_names", roles)
        object.__setattr__(
            self,
            "methodology_id",
            content_id(
                "power-methodology",
                {
                    "residual_feature_names": list(residual),
                    "role_feature_names": list(roles),
                    "source_boundary": "fantasypros_ros_ecr_only_v3",
                },
            ),
        )

    def fit_config(self) -> CalibrationFitConfig:
        return CalibrationFitConfig(
            residual_feature_names=self.residual_feature_names,
            role_feature_names=self.role_feature_names,
            ridge_penalty=1e-8,
        )


def default_projection_ensemble() -> EnsembleConfig:
    """Equal-source baseline; disagreement becomes predictive uncertainty."""

    return EnsembleConfig(
        provider_weights=tuple(
            ProviderWeight(provider, 1.0)
            for provider in ("fantasypros", "espn", "yahoo")
        ),
        minimum_observed_sources=2,
        position_stddev_floors={
            "QB": 3.0,
            "RB": 4.0,
            "WR": 4.0,
            "TE": 3.0,
            "K": 2.0,
            "DST": 3.0,
            "DL": 3.0,
            "LB": 3.0,
            "DB": 3.0,
            "IDP": 3.0,
        },
    )


def _names(name, values):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    rows = tuple(values)
    if not rows or any(not isinstance(value, str) or not value for value in rows):
        raise ValueError(f"{name} must contain non-empty strings")
    if len(set(rows)) != len(rows):
        raise ValueError(f"{name} contains a duplicate")
    return tuple(sorted(rows))


def _validate_fantasypros_power_features(*feature_groups) -> None:
    """Keep every projection input to roster power on the FantasyPros side."""

    if any(
        name.startswith("projection_")
        and not name.startswith("projection_fantasypros_")
        for group in feature_groups
        for name in group
    ):
        raise ValueError(
            "FantasyPros-style power can use only FantasyPros projection features"
        )


DEFAULT_POWER_METHODOLOGY = PowerMethodology(
    residual_feature_names=(
        "presence",
        "ecr_ros_log_strength",
    ),
    role_feature_names=(
        "ecr_ros_inverse_rank",
        "ecr_ros_log_strength",
    ),
)
