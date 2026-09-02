"""Portable calibrated coefficients that can score a new weekly feature set."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import json
from math import fsum, isfinite
from numbers import Real
import os
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from ._calibration_results import (
    CALIBRATION_SOLVER_ALGORITHM,
    FittedStrengthCalibration,
)
from ._strength_formula_evidence import (
    fitted_formula_evidence,
    normalize_formula_evidence,
)
from .feature_engineering import StrengthFeatureSet
from .methodology import _validate_fantasypros_power_features
from .strength import StrengthModel
from .strength_calibration import (
    CalibrationMetadata,
    PlayerStrength,
    RoleDefinition,
    _content_id,
)
from .trade_space import TeamRoster


@dataclass(frozen=True, slots=True)
class StrengthFormula:
    """A validated strength methodology, independent of weekly player values."""

    source_fit_id: str
    trained_snapshot_id: str
    season: int
    scoring_profile_id: str
    role_definitions: tuple[RoleDefinition, ...]
    residual_weights: Mapping[str, float]
    role_weights: Mapping[str, Mapping[str, float]]
    calibration: CalibrationMetadata
    held_out_trade_ids: tuple[str, ...] = ()
    held_out_balanced_package_sizes: tuple[int, ...] = ()
    solver_algorithm: str = CALIBRATION_SOLVER_ALGORITHM
    formula_id: str = field(init=False)

    def __post_init__(self) -> None:
        source = _text("source_fit_id", self.source_fit_id)
        snapshot = _text("trained_snapshot_id", self.trained_snapshot_id)
        profile = _text("scoring_profile_id", self.scoring_profile_id)
        if type(self.season) is not int or self.season < 2012:
            raise ValueError("season must be an integer of at least 2012")
        roles = tuple(self.role_definitions)
        if not roles or any(not isinstance(row, RoleDefinition) for row in roles):
            raise ValueError("role_definitions must contain RoleDefinition values")
        role_ids = tuple(row.role_id for row in roles)
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("role_definitions contain a duplicate role_id")
        residual = _weights("residual_weights", self.residual_weights)
        role_weights = _role_weights(self.role_weights, role_ids)
        _validate_fantasypros_power_features(
            residual,
            *(weights for weights in role_weights.values()),
        )
        if not isinstance(self.calibration, CalibrationMetadata):
            raise ValueError("calibration must be CalibrationMetadata")
        heldout_ids, balanced_sizes = normalize_formula_evidence(
            self.calibration,
            self.held_out_trade_ids,
            self.held_out_balanced_package_sizes,
        )
        if self.solver_algorithm != CALIBRATION_SOLVER_ALGORITHM:
            raise ValueError("solver_algorithm is unsupported")
        object.__setattr__(self, "source_fit_id", source)
        object.__setattr__(self, "trained_snapshot_id", snapshot)
        object.__setattr__(self, "scoring_profile_id", profile)
        object.__setattr__(self, "role_definitions", roles)
        object.__setattr__(self, "residual_weights", residual)
        object.__setattr__(self, "role_weights", role_weights)
        object.__setattr__(self, "held_out_trade_ids", heldout_ids)
        object.__setattr__(
            self, "held_out_balanced_package_sizes", balanced_sizes
        )
        object.__setattr__(self, "formula_id", _content_id("strength-formula", self._content_record()))

    @classmethod
    def from_fitted(cls, fitted: FittedStrengthCalibration) -> "StrengthFormula":
        if not isinstance(fitted, FittedStrengthCalibration):
            raise ValueError("fitted must be a FittedStrengthCalibration")
        heldout_ids, balanced_sizes = fitted_formula_evidence(fitted)
        return cls(
            source_fit_id=fitted.fit_id,
            trained_snapshot_id=fitted.model.snapshot_id,
            season=fitted.model.season,
            scoring_profile_id=fitted.model.scoring_profile_id,
            role_definitions=fitted.model.role_definitions,
            residual_weights=fitted.residual_weights,
            role_weights=fitted.role_weights,
            calibration=fitted.model.calibration,
            held_out_trade_ids=heldout_ids,
            held_out_balanced_package_sizes=balanced_sizes,
            solver_algorithm=fitted.solver_algorithm,
        )

    def build_model(
        self,
        features: StrengthFeatureSet,
        baseline_rosters: Iterable[TeamRoster],
    ) -> StrengthModel:
        """Apply the frozen methodology to new weekly evidence entirely locally."""

        if not isinstance(features, StrengthFeatureSet):
            raise ValueError("features must be a StrengthFeatureSet")
        if (
            features.season != self.season
            or features.scoring_profile_id != self.scoring_profile_id
        ):
            raise ValueError("weekly features do not match the formula season and scoring profile")
        required = set(self.residual_weights)
        required.update(
            name for weights in self.role_weights.values() for name in weights
        )
        if not required.issubset(features.feature_names):
            missing = min(required.difference(features.feature_names))
            raise ValueError(f"weekly features are missing formula input {missing!r}")
        players = tuple(self._player_strength(row) for row in features.player_features)
        rosters = _baseline_rosters(baseline_rosters, {row.player_id for row in players})
        provisional = StrengthModel(
            self.role_definitions,
            players,
            1.0,
            snapshot_id=features.snapshot_id,
            season=features.season,
            scoring_profile_id=features.scoring_profile_id,
            calibration=self.calibration,
        )
        denominator = max(
            provisional.score_roster(row.player_ids).absolute_score for row in rosters
        )
        if not isfinite(denominator) or denominator <= 0:
            raise ValueError("weekly baseline league strength must be positive")
        return StrengthModel(
            self.role_definitions,
            players,
            denominator,
            snapshot_id=features.snapshot_id,
            season=features.season,
            scoring_profile_id=features.scoring_profile_id,
            calibration=self.calibration,
        )

    def _player_strength(self, row) -> PlayerStrength:
        residual = fsum(
            weight * row.values[name] for name, weight in self.residual_weights.items()
        )
        assignments = {
            role.role_id: fsum(
                weight * row.values[name]
                for name, weight in self.role_weights[role.role_id].items()
            )
            for role in self.role_definitions
            if row.eligible_positions.intersection(role.eligible_positions)
        }
        return PlayerStrength(
            row.player_id,
            residual,
            row.eligible_positions,
            assignments,
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "calibration": self.calibration.to_record(),
            "held_out_balanced_package_sizes": list(
                self.held_out_balanced_package_sizes
            ),
            "held_out_trade_ids": list(self.held_out_trade_ids),
            "residual_weights": dict(self.residual_weights),
            "role_definitions": [row.to_record() for row in self.role_definitions],
            "role_weights": {
                role_id: dict(weights) for role_id, weights in self.role_weights.items()
            },
            "scoring_profile_id": self.scoring_profile_id,
            "season": self.season,
            "solver_algorithm": self.solver_algorithm,
            "source_fit_id": self.source_fit_id,
            "trained_snapshot_id": self.trained_snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "fantasy_trade_strength_formula",
            "schema_version": 2,
            **self._content_record(),
            "formula_id": self.formula_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "StrengthFormula":
        content_keys = {
            "calibration",
            "held_out_balanced_package_sizes",
            "held_out_trade_ids",
            "residual_weights",
            "role_definitions",
            "role_weights",
            "scoring_profile_id",
            "season",
            "solver_algorithm",
            "source_fit_id",
            "trained_snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != content_keys | {
            "kind", "schema_version", "formula_id"
        }:
            raise ValueError("strength formula record fields are invalid")
        if (
            record["kind"] != "fantasy_trade_strength_formula"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 2
        ):
            raise ValueError("strength formula kind or schema version is invalid")
        raw_roles = record["role_definitions"]
        if not isinstance(raw_roles, list):
            raise ValueError("role_definitions must be a JSON array")
        calibration = record["calibration"]
        if not isinstance(calibration, Mapping):
            raise ValueError("calibration must be a JSON object")
        formula = cls(
            source_fit_id=record["source_fit_id"],
            trained_snapshot_id=record["trained_snapshot_id"],
            season=record["season"],
            scoring_profile_id=record["scoring_profile_id"],
            role_definitions=tuple(RoleDefinition.from_record(row) for row in raw_roles),
            residual_weights=record["residual_weights"],
            role_weights=record["role_weights"],
            calibration=CalibrationMetadata.from_record(calibration),
            held_out_trade_ids=record["held_out_trade_ids"],
            held_out_balanced_package_sizes=record[
                "held_out_balanced_package_sizes"
            ],
            solver_algorithm=record["solver_algorithm"],
        )
        if record["formula_id"] != formula.formula_id:
            raise ValueError("strength formula content does not match formula_id")
        return formula


def save_strength_formula(
    formula: StrengthFormula, path: str | os.PathLike[str]
) -> Path:
    if not isinstance(formula, StrengthFormula):
        raise ValueError("formula must be a StrengthFormula")
    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise ValueError("strength formula path must end in .json")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.json")
    try:
        temporary.write_text(
            json.dumps(
                formula.to_record(), sort_keys=True, separators=(",", ":"), allow_nan=False
            ),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def load_strength_formula(path: str | os.PathLike[str]) -> StrengthFormula:
    try:
        record = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=lambda value: (_raise_constant(value)),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read strength formula: {error}") from None
    if not isinstance(record, Mapping):
        raise ValueError("strength formula file must contain a JSON object")
    return StrengthFormula.from_record(record)


def _role_weights(value, role_ids):
    if not isinstance(value, Mapping) or set(value) != set(role_ids):
        raise ValueError("role_weights must exactly cover role_definitions")
    normalized = {
        role_id: _weights(f"role_weights[{role_id!r}]", value[role_id], nonnegative=True)
        for role_id in role_ids
    }
    feature_sets = {tuple(weights) for weights in normalized.values()}
    if len(feature_sets) != 1:
        raise ValueError("every role must use the same role feature names")
    return MappingProxyType(normalized)


def _weights(name, value, *, nonnegative=False):
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result = {}
    for raw_key, raw_value in value.items():
        key = _text("feature name", raw_key)
        if isinstance(raw_value, bool) or not isinstance(raw_value, Real):
            raise ValueError(f"{name} values must be finite numbers")
        number = float(raw_value)
        if not isfinite(number) or (nonnegative and number < 0):
            raise ValueError(f"{name} values must be finite and permitted by the formula")
        result[key] = number
    return MappingProxyType(dict(sorted(result.items())))


def _baseline_rosters(values, player_ids):
    rows = tuple(values)
    if len(rows) < 2 or any(not isinstance(row, TeamRoster) for row in rows):
        raise ValueError("baseline_rosters must contain at least two TeamRoster values")
    if len({row.team_id for row in rows}) != len(rows):
        raise ValueError("baseline_rosters contain a duplicate team")
    owned = set()
    for row in rows:
        if row.current_size != len(row.player_ids):
            raise ValueError("baseline_rosters must contain complete current rosters")
        unknown = set(row.player_ids).difference(player_ids)
        if unknown:
            raise ValueError(f"baseline roster references unknown player {min(unknown)!r}")
        overlap = owned.intersection(row.player_ids)
        if overlap:
            raise ValueError(f"baseline rosters share player {min(overlap)!r}")
        owned.update(row.player_ids)
    return rows


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _raise_constant(value):
    raise ValueError(f"strength formula contains non-finite JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"strength formula contains duplicate JSON key {key!r}")
        result[key] = value
    return result
