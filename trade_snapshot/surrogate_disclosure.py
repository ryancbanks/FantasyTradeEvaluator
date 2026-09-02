"""Auditable disclosure for an explicitly opted-in nonexact power formula."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from ._calibration_results import MINIMUM_EXACT_TRADE_COUNT
from ._scenario_random import content_id
from .formula_verification import REQUIRED_BALANCED_PACKAGE_SIZES
from .methodology_reuse import (
    FormulaAction,
    FormulaReuseDecision,
    MethodologyFingerprint,
)
from .strength import CalibrationStatus, StrengthModel
from .strength_calibration import CalibrationMetadata
from .strength_formula import StrengthFormula


SURROGATE_NOTICE = (
    "SURROGATE approximation: this formula did not reproduce FantasyPros exactly "
    "on blind holdout trades. No power result from this engine is exact. "
    "Observed balanced/no-adjustment shapes are labeled surrogate; unobserved, "
    "imbalanced, or add/drop shapes are labeled surrogate_extrapolated."
)
SURROGATE_QUALITY_GATE = (
    "converged_identifiable_training-exact_full-blind-design_v1"
)


@dataclass(frozen=True, slots=True)
class SurrogateDisclosure:
    """Content-addressed evidence for a knowingly published approximation.

    This is intentionally a separate type from ``MethodologyAttestation``.  An
    exact attestation can therefore retain its strict gates while a user may
    explicitly choose to run a fitted, measured approximation for the week.
    """

    weekly_snapshot_id: str
    strength_model_id: str
    formula_id: str
    formula_source_fit_id: str
    formula_trained_snapshot_id: str
    methodology_fingerprint: MethodologyFingerprint
    formula_decision: FormulaReuseDecision
    calibration_diagnostics: CalibrationMetadata
    held_out_trade_ids: tuple[str, ...]
    observed_balanced_package_sizes: tuple[int, ...]
    disclosure_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "weekly_snapshot_id",
            "strength_model_id",
            "formula_id",
            "formula_source_fit_id",
            "formula_trained_snapshot_id",
        ):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        if not isinstance(self.methodology_fingerprint, MethodologyFingerprint):
            raise ValueError(
                "methodology_fingerprint must be a MethodologyFingerprint"
            )
        if not isinstance(self.formula_decision, FormulaReuseDecision):
            raise ValueError("formula_decision must be a FormulaReuseDecision")
        if self.formula_decision.action is not FormulaAction.RECALIBRATE:
            raise ValueError("surrogate publication requires a fresh calibration")
        if (
            self.formula_decision.methodology_fingerprint_id
            != self.methodology_fingerprint.fingerprint_id
        ):
            raise ValueError(
                "formula decision does not match the methodology fingerprint"
            )
        calibration = self.calibration_diagnostics
        if not isinstance(calibration, CalibrationMetadata):
            raise ValueError("calibration_diagnostics must be CalibrationMetadata")
        if calibration.status is not CalibrationStatus.SURROGATE:
            raise ValueError("surrogate disclosure requires surrogate calibration")
        if calibration.held_out_trade_count < MINIMUM_EXACT_TRADE_COUNT:
            raise ValueError(
                "surrogate disclosure requires the full blind holdout budget"
            )
        fingerprint = self.methodology_fingerprint
        if (
            calibration.analyzer_bundle_sha256 != fingerprint.analyzer_bundle.sha256
            or calibration.response_schema_sha256
            != fingerprint.response_schema_sha256
        ):
            raise ValueError(
                "surrogate diagnostics do not match the methodology fingerprint"
            )
        if self.formula_trained_snapshot_id != self.weekly_snapshot_id:
            raise ValueError(
                "surrogate formula must be fitted on the published weekly snapshot"
            )
        holdouts = _ids(self.held_out_trade_ids)
        if len(holdouts) != calibration.held_out_trade_count:
            raise ValueError(
                "surrogate holdout IDs do not match the diagnostic holdout count"
            )
        sizes = _sizes(self.observed_balanced_package_sizes)
        missing = REQUIRED_BALANCED_PACKAGE_SIZES.difference(sizes)
        if missing:
            raise ValueError(
                "surrogate disclosure lacks required observed package sizes "
                + ", ".join(str(value) for value in sorted(missing))
            )
        object.__setattr__(self, "held_out_trade_ids", holdouts)
        object.__setattr__(self, "observed_balanced_package_sizes", sizes)
        object.__setattr__(
            self,
            "disclosure_id",
            content_id("surrogate-disclosure", self._content_record()),
        )

    @classmethod
    def from_refresh(
        cls,
        *,
        formula: StrengthFormula,
        strength_model: StrengthModel,
        methodology_fingerprint: MethodologyFingerprint,
        formula_decision: FormulaReuseDecision,
    ) -> "SurrogateDisclosure":
        if not isinstance(formula, StrengthFormula):
            raise ValueError("formula must be a StrengthFormula")
        if not isinstance(strength_model, StrengthModel):
            raise ValueError("strength_model must be a StrengthModel")
        if (
            strength_model.calibration != formula.calibration
            or strength_model.role_definitions != formula.role_definitions
            or strength_model.season != formula.season
            or strength_model.scoring_profile_id != formula.scoring_profile_id
        ):
            raise ValueError("strength model does not match the selected formula")
        if formula.role_definitions != methodology_fingerprint.role_definitions:
            raise ValueError("formula roles do not match the methodology fingerprint")
        return cls(
            weekly_snapshot_id=strength_model.snapshot_id,
            strength_model_id=strength_model.model_id,
            formula_id=formula.formula_id,
            formula_source_fit_id=formula.source_fit_id,
            formula_trained_snapshot_id=formula.trained_snapshot_id,
            methodology_fingerprint=methodology_fingerprint,
            formula_decision=formula_decision,
            calibration_diagnostics=formula.calibration,
            held_out_trade_ids=formula.held_out_trade_ids,
            observed_balanced_package_sizes=(
                formula.held_out_balanced_package_sizes
            ),
        )

    @property
    def current_evidence_id(self) -> str:
        return self.formula_source_fit_id

    @property
    def current_evidence_at(self) -> datetime:
        return self.calibration_diagnostics.captured_at

    @property
    def current_holdout_count(self) -> int:
        return len(self.held_out_trade_ids)

    @property
    def validated_balanced_package_sizes(self) -> tuple[int, ...]:
        """A surrogate has observations, but no package size is exact."""

        return ()

    def power_result_status(
        self,
        *,
        outgoing_count: int,
        incoming_count: int,
        has_roster_adjustment: bool,
    ) -> str:
        if (
            type(outgoing_count) is not int
            or outgoing_count < 1
            or type(incoming_count) is not int
            or incoming_count < 1
            or not isinstance(has_roster_adjustment, bool)
        ):
            raise ValueError("trade shape counts and adjustment flag are invalid")
        observed_shape = (
            not has_roster_adjustment
            and outgoing_count == incoming_count
            and outgoing_count in self.observed_balanced_package_sizes
        )
        return "surrogate" if observed_shape else "surrogate_extrapolated"

    def validate_bundle(self, *, snapshot_id: str, strength_model: StrengthModel) -> None:
        if not isinstance(strength_model, StrengthModel):
            raise ValueError("strength_model must be a StrengthModel")
        if self.weekly_snapshot_id != snapshot_id:
            raise ValueError("surrogate disclosure does not match league snapshot")
        if self.strength_model_id != strength_model.model_id:
            raise ValueError("surrogate disclosure does not match strength model")
        if self.calibration_diagnostics != strength_model.calibration:
            raise ValueError(
                "surrogate disclosure does not match strength calibration"
            )

    def _content_record(self) -> dict[str, object]:
        return {
            "calibration_diagnostics": self.calibration_diagnostics.to_record(),
            "formula_decision": self.formula_decision.to_record(),
            "formula_id": self.formula_id,
            "formula_source_fit_id": self.formula_source_fit_id,
            "formula_trained_snapshot_id": self.formula_trained_snapshot_id,
            "held_out_trade_ids": list(self.held_out_trade_ids),
            "methodology_fingerprint": self.methodology_fingerprint.to_record(),
            "observed_balanced_package_sizes": list(
                self.observed_balanced_package_sizes
            ),
            "publication_mode": "surrogate_opt_in_v1",
            "quality_gate": SURROGATE_QUALITY_GATE,
            "strength_model_id": self.strength_model_id,
            "weekly_snapshot_id": self.weekly_snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {
            **self._content_record(),
            "disclosure_id": self.disclosure_id,
            "schema_version": 1,
        }

    @classmethod
    def from_record(cls, record: object) -> "SurrogateDisclosure":
        content = {
            "calibration_diagnostics",
            "formula_decision",
            "formula_id",
            "formula_source_fit_id",
            "formula_trained_snapshot_id",
            "held_out_trade_ids",
            "methodology_fingerprint",
            "observed_balanced_package_sizes",
            "publication_mode",
            "quality_gate",
            "strength_model_id",
            "weekly_snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != content | {
            "disclosure_id",
            "schema_version",
        }:
            raise ValueError("surrogate disclosure record fields are invalid")
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["publication_mode"] != "surrogate_opt_in_v1"
            or record["quality_gate"] != SURROGATE_QUALITY_GATE
        ):
            raise ValueError("surrogate disclosure schema or mode is invalid")
        holdouts = record["held_out_trade_ids"]
        sizes = record["observed_balanced_package_sizes"]
        if not isinstance(holdouts, list) or not isinstance(sizes, list):
            raise ValueError("surrogate disclosure arrays are invalid")
        result = cls(
            weekly_snapshot_id=record["weekly_snapshot_id"],
            strength_model_id=record["strength_model_id"],
            formula_id=record["formula_id"],
            formula_source_fit_id=record["formula_source_fit_id"],
            formula_trained_snapshot_id=record["formula_trained_snapshot_id"],
            methodology_fingerprint=MethodologyFingerprint.from_record(
                record["methodology_fingerprint"]
            ),
            formula_decision=FormulaReuseDecision.from_record(
                record["formula_decision"]
            ),
            calibration_diagnostics=CalibrationMetadata.from_record(
                _mapping("calibration_diagnostics", record["calibration_diagnostics"])
            ),
            held_out_trade_ids=tuple(holdouts),
            observed_balanced_package_sizes=tuple(sizes),
        )
        if record["disclosure_id"] != result.disclosure_id:
            raise ValueError("surrogate disclosure content does not match disclosure_id")
        return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _ids(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("held_out_trade_ids must be a collection")
    try:
        rows = tuple(_text("held_out_trade_ids", row) for row in value)
    except TypeError:
        raise ValueError("held_out_trade_ids must be a collection") from None
    if not rows or len(set(rows)) != len(rows):
        raise ValueError("held_out_trade_ids must contain distinct identifiers")
    return tuple(sorted(rows))


def _sizes(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError("observed package sizes must be a collection")
    try:
        rows = tuple(value)
    except TypeError:
        raise ValueError("observed package sizes must be a collection") from None
    if any(type(row) is not int or row < 1 for row in rows):
        raise ValueError("observed package sizes must be positive integers")
    return tuple(sorted(set(rows)))


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


__all__ = (
    "SURROGATE_NOTICE",
    "SURROGATE_QUALITY_GATE",
    "SurrogateDisclosure",
)
