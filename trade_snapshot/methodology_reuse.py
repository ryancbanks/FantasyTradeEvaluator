"""Check static compatibility before mandatory weekly formula verification."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import re

from ._analyzer_types import BundleFingerprint
from ._scenario_random import content_id
from .methodology import PowerMethodology
from .strength import CalibrationStatus, RoleDefinition
from .strength_formula import StrengthFormula


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FormulaAction(str, Enum):
    REUSE = "reuse"
    RECALIBRATE = "recalibrate"


@dataclass(frozen=True, slots=True)
class MethodologyFingerprint:
    """Observable inputs that distinguish one analyzer scoring method."""

    analyzer_bundle: BundleFingerprint
    response_schema_sha256: str
    power_methodology: PowerMethodology
    role_definitions: tuple[RoleDefinition, ...]
    fingerprint_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.analyzer_bundle, BundleFingerprint):
            raise ValueError("analyzer_bundle must be a BundleFingerprint")
        schema = self.response_schema_sha256
        if not isinstance(schema, str) or not _SHA256.fullmatch(schema.casefold()):
            raise ValueError("response_schema_sha256 must contain 64 hexadecimal characters")
        if not isinstance(self.power_methodology, PowerMethodology):
            raise ValueError("power_methodology must be a PowerMethodology")
        roles = tuple(self.role_definitions)
        if not roles or any(not isinstance(row, RoleDefinition) for row in roles):
            raise ValueError("role_definitions must contain RoleDefinition values")
        if len({row.role_id for row in roles}) != len(roles):
            raise ValueError("role_definitions contain a duplicate role_id")
        roles = tuple(sorted(roles, key=lambda row: row.role_id))
        object.__setattr__(self, "response_schema_sha256", schema.casefold())
        object.__setattr__(self, "role_definitions", roles)
        object.__setattr__(
            self,
            "fingerprint_id",
            content_id(
                "analyzer-methodology",
                {
                    "analyzer_bundle_sha256": self.analyzer_bundle.sha256,
                    "power_methodology_id": self.power_methodology.methodology_id,
                    "response_schema_sha256": schema.casefold(),
                    "role_definitions": [row.to_record() for row in roles],
                },
            ),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "analyzer_bundle": {
                "url": self.analyzer_bundle.url,
                "sha256": self.analyzer_bundle.sha256,
            },
            "fingerprint_id": self.fingerprint_id,
            "power_methodology": {
                "methodology_id": self.power_methodology.methodology_id,
                "residual_feature_names": list(
                    self.power_methodology.residual_feature_names
                ),
                "role_feature_names": list(
                    self.power_methodology.role_feature_names
                ),
            },
            "response_schema_sha256": self.response_schema_sha256,
            "role_definitions": [row.to_record() for row in self.role_definitions],
            "schema_version": 1,
        }

    @classmethod
    def from_record(cls, record: object) -> "MethodologyFingerprint":
        expected = {
            "analyzer_bundle",
            "fingerprint_id",
            "power_methodology",
            "response_schema_sha256",
            "role_definitions",
            "schema_version",
        }
        if not isinstance(record, Mapping) or set(record) != expected:
            raise ValueError("methodology fingerprint record fields are invalid")
        if type(record["schema_version"]) is not int or record["schema_version"] != 1:
            raise ValueError("methodology fingerprint schema version is invalid")
        bundle = record["analyzer_bundle"]
        if not isinstance(bundle, Mapping) or set(bundle) != {"url", "sha256"}:
            raise ValueError("methodology analyzer bundle record is invalid")
        methodology = record["power_methodology"]
        if not isinstance(methodology, Mapping) or set(methodology) != {
            "methodology_id",
            "residual_feature_names",
            "role_feature_names",
        }:
            raise ValueError("power methodology record is invalid")
        residual = methodology["residual_feature_names"]
        roles = methodology["role_feature_names"]
        definitions = record["role_definitions"]
        if not isinstance(residual, list) or not isinstance(roles, list):
            raise ValueError("power methodology features must be JSON arrays")
        if not isinstance(definitions, list):
            raise ValueError("methodology role definitions must be a JSON array")
        result = cls(
            BundleFingerprint(bundle["url"], bundle["sha256"]),
            record["response_schema_sha256"],
            PowerMethodology(tuple(residual), tuple(roles)),
            tuple(RoleDefinition.from_record(row) for row in definitions),
        )
        if methodology["methodology_id"] != result.power_methodology.methodology_id:
            raise ValueError("power methodology content does not match methodology_id")
        if record["fingerprint_id"] != result.fingerprint_id:
            raise ValueError("methodology fingerprint content does not match fingerprint_id")
        return result


@dataclass(frozen=True, slots=True)
class FormulaReuseDecision:
    action: FormulaAction
    reasons: tuple[str, ...]
    methodology_fingerprint_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.action, FormulaAction):
            raise ValueError("action must be a FormulaAction")
        reasons = tuple(self.reasons)
        if any(not isinstance(row, str) or not row for row in reasons):
            raise ValueError("reasons must contain non-empty strings")
        if self.action is FormulaAction.REUSE and reasons:
            raise ValueError("a reusable formula cannot have incompatibility reasons")
        if self.action is FormulaAction.RECALIBRATE and not reasons:
            raise ValueError("recalibration requires at least one reason")
        if not isinstance(self.methodology_fingerprint_id, str) or not self.methodology_fingerprint_id:
            raise ValueError("methodology_fingerprint_id must be a non-empty string")
        object.__setattr__(self, "reasons", reasons)

    def to_record(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "methodology_fingerprint_id": self.methodology_fingerprint_id,
            "reasons": list(self.reasons),
            "schema_version": 1,
        }

    @classmethod
    def from_record(cls, record: object) -> "FormulaReuseDecision":
        if not isinstance(record, Mapping) or set(record) != {
            "action",
            "methodology_fingerprint_id",
            "reasons",
            "schema_version",
        }:
            raise ValueError("formula reuse decision record fields are invalid")
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or not isinstance(record["reasons"], list)
        ):
            raise ValueError("formula reuse decision record is invalid")
        try:
            action = FormulaAction(record["action"])
        except (TypeError, ValueError):
            raise ValueError("formula reuse decision action is invalid") from None
        return cls(
            action,
            tuple(record["reasons"]),
            record["methodology_fingerprint_id"],
        )


def decide_formula_reuse(
    formula: StrengthFormula | None,
    fingerprint: MethodologyFingerprint,
    *,
    season: int,
    scoring_profile_id: str,
) -> FormulaReuseDecision:
    """Mark an exact static match eligible for the weekly verification gate."""

    if not isinstance(fingerprint, MethodologyFingerprint):
        raise ValueError("fingerprint must be a MethodologyFingerprint")
    if type(season) is not int or season < 2012:
        raise ValueError("season must be an integer of at least 2012")
    if not isinstance(scoring_profile_id, str) or not scoring_profile_id.strip():
        raise ValueError("scoring_profile_id must be a non-empty string")
    reasons = []
    if formula is None:
        reasons.append("no saved formula")
    elif not isinstance(formula, StrengthFormula):
        raise ValueError("formula must be a StrengthFormula or None")
    else:
        calibration = formula.calibration
        if calibration.status is not CalibrationStatus.EXACT:
            reasons.append("saved formula is not exact on blind holdouts")
        reasons.extend(
            formula_static_incompatibility_reasons(
                formula,
                fingerprint,
                season=season,
                scoring_profile_id=scoring_profile_id,
            )
        )
    action = FormulaAction.RECALIBRATE if reasons else FormulaAction.REUSE
    return FormulaReuseDecision(action, tuple(reasons), fingerprint.fingerprint_id)


def formula_static_incompatibility_reasons(
    formula: StrengthFormula,
    fingerprint: MethodologyFingerprint,
    *,
    season: int,
    scoring_profile_id: str,
) -> tuple[str, ...]:
    """Check identity and feature compatibility without making an exactness claim."""

    if not isinstance(formula, StrengthFormula):
        raise ValueError("formula must be a StrengthFormula")
    if not isinstance(fingerprint, MethodologyFingerprint):
        raise ValueError("fingerprint must be a MethodologyFingerprint")
    if type(season) is not int or season < 2012:
        raise ValueError("season must be an integer of at least 2012")
    if not isinstance(scoring_profile_id, str) or not scoring_profile_id.strip():
        raise ValueError("scoring_profile_id must be a non-empty string")
    calibration = formula.calibration
    reasons = []
    if formula.season != season:
        reasons.append("season changed")
    if formula.scoring_profile_id != scoring_profile_id.strip():
        reasons.append("league scoring profile changed")
    if calibration.analyzer_bundle_sha256 != fingerprint.analyzer_bundle.sha256:
        reasons.append("FantasyPros analyzer bundle content changed")
    if calibration.response_schema_sha256 != fingerprint.response_schema_sha256:
        reasons.append("FantasyPros analyzer response schema changed")
    if (
        tuple(sorted(formula.role_definitions, key=lambda row: row.role_id))
        != fingerprint.role_definitions
    ):
        reasons.append("league starter or depth roles changed")
    expected_residual = set(fingerprint.power_methodology.residual_feature_names)
    if set(formula.residual_weights) != expected_residual:
        reasons.append("FantasyPros power feature policy changed")
    expected_role = set(fingerprint.power_methodology.role_feature_names)
    if any(set(weights) != expected_role for weights in formula.role_weights.values()):
        reasons.append("FantasyPros role feature policy changed")
    return tuple(reasons)


__all__ = (
    "FormulaAction",
    "FormulaReuseDecision",
    "MethodologyFingerprint",
    "decide_formula_reuse",
    "formula_static_incompatibility_reasons",
)
