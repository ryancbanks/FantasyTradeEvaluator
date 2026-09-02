"""Design a small, diverse analyzer experiment set instead of enumerating trades."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

from ._calibration_experiment_design import (
    atomic_candidates,
    balanced_holdout_candidates,
)
from ._calibration_experiment_selection import (
    select_holdout_candidates,
    select_training_candidates,
)
from ._scenario_random import content_id
from .feature_engineering import StrengthFeatureSet
from .strength import RoleDefinition
from .trade_space import TeamRoster


__all__ = (
    "CalibrationExperiment",
    "CalibrationExperimentPlan",
    "CalibrationExperimentPurpose",
    "design_calibration_experiments",
)


class CalibrationExperimentPurpose(str, Enum):
    TRAINING = "training"
    HELD_OUT = "held_out"


@dataclass(frozen=True, slots=True)
class CalibrationExperiment:
    purpose: CalibrationExperimentPurpose
    team1_id: str
    team2_id: str
    team1_gives: tuple[str, ...]
    team2_gives: tuple[str, ...]
    design_signature: tuple[float, ...]
    experiment_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, CalibrationExperimentPurpose):
            raise ValueError("purpose must be a CalibrationExperimentPurpose")
        for name in ("team1_id", "team2_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if self.team1_id == self.team2_id:
            raise ValueError("calibration experiment teams must be different")
        left = _ids("team1_gives", self.team1_gives)
        right = _ids("team2_gives", self.team2_gives)
        if len(left) != len(right):
            raise ValueError("calibration experiments must keep roster sizes balanced")
        if set(left).intersection(right):
            raise ValueError("calibration experiment packages cannot overlap")
        signature = tuple(float(value) for value in self.design_signature)
        if (
            not signature
            or not all(isfinite(value) for value in signature)
            or not any(value != 0 for value in signature)
        ):
            raise ValueError("design_signature must contain a nonzero perturbation")
        object.__setattr__(self, "team1_gives", left)
        object.__setattr__(self, "team2_gives", right)
        object.__setattr__(self, "design_signature", signature)
        object.__setattr__(
            self,
            "experiment_id",
            content_id(
                "calibration-experiment",
                {
                    "purpose": self.purpose.value,
                    "team1_gives": list(left),
                    "team1_id": self.team1_id,
                    "team2_gives": list(right),
                    "team2_id": self.team2_id,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class CalibrationExperimentPlan:
    snapshot_id: str
    primary_team_id: str
    residual_feature_names: tuple[str, ...]
    role_feature_names: tuple[str, ...]
    role_ids: tuple[str, ...]
    experiments: tuple[CalibrationExperiment, ...]
    plan_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")
        if not isinstance(self.primary_team_id, str) or not self.primary_team_id:
            raise ValueError("primary_team_id must be a non-empty string")
        residual = _names("residual_feature_names", self.residual_feature_names)
        role_features = _names("role_feature_names", self.role_feature_names)
        role_ids = _names("role_ids", self.role_ids)
        rows = tuple(self.experiments)
        if not rows or any(not isinstance(row, CalibrationExperiment) for row in rows):
            raise ValueError("experiments must contain CalibrationExperiment values")
        if any(row.team1_id != self.primary_team_id for row in rows):
            raise ValueError("every experiment must use primary_team_id as team1")
        if len({row.experiment_id for row in rows}) != len(rows):
            raise ValueError("experiments contain a duplicate")
        signatures = tuple(row.design_signature for row in rows)
        if len(set(signatures)) != len(signatures):
            raise ValueError("experiments contain duplicate feature perturbations")
        width = len(residual) + len(role_ids) * len(role_features)
        if any(len(signature) != width for signature in signatures):
            raise ValueError("experiment design signature width is invalid")
        object.__setattr__(self, "residual_feature_names", residual)
        object.__setattr__(self, "role_feature_names", role_features)
        object.__setattr__(self, "role_ids", role_ids)
        object.__setattr__(self, "experiments", rows)
        object.__setattr__(
            self,
            "plan_id",
            content_id(
                "calibration-experiment-plan",
                {
                    "experiments": [
                        {
                            "experiment_id": row.experiment_id,
                            "design_signature": list(row.design_signature),
                        }
                        for row in rows
                    ],
                    "primary_team_id": self.primary_team_id,
                    "residual_feature_names": list(residual),
                    "role_feature_names": list(role_features),
                    "role_ids": list(role_ids),
                    "snapshot_id": self.snapshot_id,
                },
            ),
        )

    @property
    def training(self) -> tuple[CalibrationExperiment, ...]:
        return tuple(
            row for row in self.experiments
            if row.purpose is CalibrationExperimentPurpose.TRAINING
        )

    @property
    def held_out(self) -> tuple[CalibrationExperiment, ...]:
        return tuple(
            row for row in self.experiments
            if row.purpose is CalibrationExperimentPurpose.HELD_OUT
        )

    @property
    def held_out_balanced_package_sizes(self) -> tuple[int, ...]:
        """Balanced package sizes tested blindly by this immutable plan."""

        return tuple(sorted({len(row.team1_gives) for row in self.held_out}))


def design_calibration_experiments(
    features: StrengthFeatureSet,
    roles: Iterable[RoleDefinition],
    rosters: Iterable[TeamRoster],
    *,
    primary_team_id: str,
    residual_feature_names: Iterable[str],
    role_feature_names: Iterable[str],
    training_experiment_count: int = 250,
    held_out_experiment_count: int = 100,
) -> CalibrationExperimentPlan:
    """Fit on atomic swaps and blind-test representative balanced packages.

    The bounded blind pool spans every feasible balanced package size when the
    holdout budget permits.  It intentionally does not claim evidence for
    imbalanced packages, free-agent additions, or roster drops.
    """

    if not isinstance(features, StrengthFeatureSet):
        raise ValueError("features must be a StrengthFeatureSet")
    role_rows = tuple(roles)
    if not role_rows or any(not isinstance(row, RoleDefinition) for row in role_rows):
        raise ValueError("roles must contain RoleDefinition values")
    if len({row.role_id for row in role_rows}) != len(role_rows):
        raise ValueError("roles contain a duplicate role_id")
    roster_rows = tuple(rosters)
    by_team = {row.team_id: row for row in roster_rows}
    if len(by_team) != len(roster_rows) or primary_team_id not in by_team:
        raise ValueError("rosters must uniquely include primary_team_id")
    if len(roster_rows) < 2:
        raise ValueError("at least two complete team rosters are required")
    for row in roster_rows:
        if row.current_size != len(row.player_ids):
            raise ValueError("calibration planning requires complete team rosters")
    residual = _names("residual_feature_names", residual_feature_names)
    role_features = _names("role_feature_names", role_feature_names)
    available = set(features.feature_names)
    missing = set(residual).union(role_features).difference(available)
    if missing:
        raise ValueError(f"calibration feature set is missing {min(missing)!r}")
    for name, value in (
        ("training_experiment_count", training_experiment_count),
        ("held_out_experiment_count", held_out_experiment_count),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    feature_by_id = {row.player_id: row for row in features.player_features}
    owned = {player_id for roster in roster_rows for player_id in roster.player_ids}
    unknown = owned.difference(feature_by_id)
    if unknown:
        raise ValueError(f"calibration roster references unknown player {min(unknown)!r}")
    training_candidates = atomic_candidates(
        by_team,
        primary_team_id,
        feature_by_id,
        tuple(role_rows),
        residual,
        role_features,
    )
    counterparties = set(by_team).difference({primary_team_id})
    training = select_training_candidates(
        training_candidates, training_experiment_count
    )
    holdout_team_coverage = (
        tuple(sorted(counterparties))
        if held_out_experiment_count >= len(counterparties)
        else ()
    )
    holdouts = select_holdout_candidates(
        balanced_holdout_candidates(
            by_team,
            primary_team_id,
            feature_by_id,
            tuple(role_rows),
            residual,
            role_features,
        ),
        held_out_experiment_count,
        blocked_candidates=training,
        counterparties=holdout_team_coverage,
        by_team=by_team,
        primary_team_id=primary_team_id,
    )
    selected = (*training, *holdouts)
    covered = {row.team2_id for row in selected}
    if covered != counterparties:
        missing_team = min(counterparties.difference(covered))
        raise ValueError(f"calibration selection did not cover team {missing_team!r}")
    experiments = tuple(
        CalibrationExperiment(
            CalibrationExperimentPurpose.TRAINING
            if index < len(training)
            else CalibrationExperimentPurpose.HELD_OUT,
            primary_team_id,
            candidate.team2_id,
            candidate.team1_gives,
            candidate.team2_gives,
            candidate.signature,
        )
        for index, candidate in enumerate(selected)
    )
    return CalibrationExperimentPlan(
        features.snapshot_id,
        primary_team_id,
        residual,
        role_features,
        tuple(row.role_id for row in role_rows),
        experiments,
    )
def _ids(name, values):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    rows = tuple(values)
    if not rows or any(not isinstance(value, str) or not value for value in rows):
        raise ValueError(f"{name} must contain non-empty strings")
    if len(set(rows)) != len(rows):
        raise ValueError(f"{name} contains a duplicate")
    return tuple(sorted(rows))


def _names(name, values):
    rows = _ids(name, values)
    return tuple(sorted(rows))
