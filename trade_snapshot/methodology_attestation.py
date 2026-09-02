"""Content-addressed proof for the FantasyPros power method in one engine."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from ._calibration_results import MINIMUM_EXACT_TRADE_COUNT
from ._scenario_random import content_id
from .formula_verification import (
    FormulaVerificationReport,
    REQUIRED_BALANCED_PACKAGE_SIZES,
)
from .methodology_reuse import (
    FormulaAction,
    FormulaReuseDecision,
    MethodologyFingerprint,
)
from .strength import CalibrationStatus, StrengthModel
from .strength_calibration import CalibrationMetadata
from .strength_formula import StrengthFormula


_TRADE_SCOPE = "balanced_packages_only_v1"


@dataclass(frozen=True, slots=True)
class MethodologyAttestation:
    """Immutable formula, analyzer-contract, and blind-evidence provenance."""

    weekly_snapshot_id: str
    strength_model_id: str
    formula_id: str
    formula_source_fit_id: str
    formula_trained_snapshot_id: str
    methodology_fingerprint: MethodologyFingerprint
    formula_decision: FormulaReuseDecision
    calibration_diagnostics: CalibrationMetadata
    calibration_holdout_ids: tuple[str, ...]
    calibration_balanced_package_sizes: tuple[int, ...]
    reuse_verification: FormulaVerificationReport | None
    attestation_id: str = field(init=False)

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
        if calibration.status is not CalibrationStatus.EXACT:
            raise ValueError("methodology attestation requires exact calibration")
        if calibration.held_out_trade_count < MINIMUM_EXACT_TRADE_COUNT:
            raise ValueError(
                "methodology attestation lacks the minimum calibration holdouts"
            )
        fingerprint = self.methodology_fingerprint
        if (
            calibration.analyzer_bundle_sha256 != fingerprint.analyzer_bundle.sha256
            or calibration.response_schema_sha256
            != fingerprint.response_schema_sha256
        ):
            raise ValueError(
                "calibration diagnostics do not match the methodology fingerprint"
            )
        holdouts = _ids("calibration_holdout_ids", self.calibration_holdout_ids)
        if len(holdouts) != calibration.held_out_trade_count:
            raise ValueError(
                "calibration holdout IDs do not match the diagnostic holdout count"
            )
        calibration_sizes = _sizes(
            "calibration_balanced_package_sizes",
            self.calibration_balanced_package_sizes,
        )
        missing = REQUIRED_BALANCED_PACKAGE_SIZES.difference(calibration_sizes)
        if missing:
            raise ValueError(
                "calibration did not cover required balanced package sizes "
                + ", ".join(str(value) for value in sorted(missing))
            )
        object.__setattr__(self, "calibration_holdout_ids", holdouts)
        object.__setattr__(
            self, "calibration_balanced_package_sizes", calibration_sizes
        )
        verification = self.reuse_verification
        if self.formula_decision.action is FormulaAction.REUSE:
            if not isinstance(verification, FormulaVerificationReport):
                raise ValueError("formula reuse requires current verification evidence")
            reasons = verification.rejection_reasons(
                formula_id=self.formula_id,
                methodology_fingerprint_id=fingerprint.fingerprint_id,
                weekly_snapshot_id=self.weekly_snapshot_id,
            )
            if reasons:
                raise ValueError(
                    "formula reuse verification is not exact: " + "; ".join(reasons)
                )
        else:
            if verification is not None:
                raise ValueError(
                    "a recalibrated formula cannot use a rejected reuse report as evidence"
                )
            if self.formula_trained_snapshot_id != self.weekly_snapshot_id:
                raise ValueError(
                    "recalibrated formula was not trained on the weekly snapshot"
                )
        object.__setattr__(
            self,
            "attestation_id",
            content_id("methodology-attestation", self._content_record()),
        )

    @classmethod
    def from_refresh(
        cls,
        *,
        formula: StrengthFormula,
        strength_model: StrengthModel,
        methodology_fingerprint: MethodologyFingerprint,
        formula_decision: FormulaReuseDecision,
        reuse_verification: FormulaVerificationReport | None,
    ) -> "MethodologyAttestation":
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
            calibration_holdout_ids=formula.held_out_trade_ids,
            calibration_balanced_package_sizes=(
                formula.held_out_balanced_package_sizes
            ),
            reuse_verification=(
                reuse_verification
                if formula_decision.action is FormulaAction.REUSE
                else None
            ),
        )

    @property
    def current_evidence_id(self) -> str:
        if self.reuse_verification is not None:
            return self.reuse_verification.verification_id
        return self.formula_source_fit_id

    @property
    def current_evidence_at(self) -> datetime:
        if self.reuse_verification is not None:
            return self.reuse_verification.verified_at
        return self.calibration_diagnostics.captured_at

    @property
    def validated_balanced_package_sizes(self) -> tuple[int, ...]:
        sizes = set(self.calibration_balanced_package_sizes)
        if self.reuse_verification is not None:
            sizes.intersection_update(self.reuse_verification.balanced_package_sizes)
        return tuple(sorted(sizes))

    @property
    def current_holdout_count(self) -> int:
        if self.reuse_verification is not None:
            return len(self.reuse_verification.ordinary_power_holdout_ids)
        return len(self.calibration_holdout_ids)

    def power_result_status(
        self,
        *,
        outgoing_count: int,
        incoming_count: int,
        has_roster_adjustment: bool,
    ) -> str:
        """Label one trade exact only inside its blind-held-out shape scope."""

        if (
            type(outgoing_count) is not int
            or outgoing_count < 1
            or type(incoming_count) is not int
            or incoming_count < 1
            or not isinstance(has_roster_adjustment, bool)
        ):
            raise ValueError("trade shape counts and adjustment flag are invalid")
        exact = (
            not has_roster_adjustment
            and outgoing_count == incoming_count
            and outgoing_count in self.validated_balanced_package_sizes
        )
        return "exact" if exact else "extrapolated"

    def validate_bundle(self, *, snapshot_id: str, strength_model: StrengthModel) -> None:
        """Fail closed when the persisted proof is detached from its engine."""

        if not isinstance(strength_model, StrengthModel):
            raise ValueError("strength_model must be a StrengthModel")
        if self.weekly_snapshot_id != snapshot_id:
            raise ValueError("methodology attestation does not match league snapshot")
        if self.strength_model_id != strength_model.model_id:
            raise ValueError("methodology attestation does not match strength model")
        if self.calibration_diagnostics != strength_model.calibration:
            raise ValueError(
                "methodology attestation does not match strength calibration"
            )

    def _content_record(self) -> dict[str, object]:
        return {
            "calibration_balanced_package_sizes": list(
                self.calibration_balanced_package_sizes
            ),
            "calibration_diagnostics": self.calibration_diagnostics.to_record(),
            "calibration_holdout_ids": list(self.calibration_holdout_ids),
            "formula_decision": self.formula_decision.to_record(),
            "formula_id": self.formula_id,
            "formula_source_fit_id": self.formula_source_fit_id,
            "formula_trained_snapshot_id": self.formula_trained_snapshot_id,
            "methodology_fingerprint": self.methodology_fingerprint.to_record(),
            "reuse_verification": (
                None
                if self.reuse_verification is None
                else self.reuse_verification.to_record()
            ),
            "strength_model_id": self.strength_model_id,
            "trade_scope": _TRADE_SCOPE,
            "weekly_snapshot_id": self.weekly_snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {
            **self._content_record(),
            "attestation_id": self.attestation_id,
            "schema_version": 1,
        }

    @classmethod
    def from_record(cls, record: object) -> "MethodologyAttestation":
        content = {
            "calibration_balanced_package_sizes",
            "calibration_diagnostics",
            "calibration_holdout_ids",
            "formula_decision",
            "formula_id",
            "formula_source_fit_id",
            "formula_trained_snapshot_id",
            "methodology_fingerprint",
            "reuse_verification",
            "strength_model_id",
            "trade_scope",
            "weekly_snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != content | {
            "attestation_id",
            "schema_version",
        }:
            raise ValueError("methodology attestation record fields are invalid")
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["trade_scope"] != _TRADE_SCOPE
        ):
            raise ValueError("methodology attestation schema or trade scope is invalid")
        holdouts = record["calibration_holdout_ids"]
        sizes = record["calibration_balanced_package_sizes"]
        if not isinstance(holdouts, list) or not isinstance(sizes, list):
            raise ValueError("methodology attestation arrays are invalid")
        verification = record["reuse_verification"]
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
            calibration_holdout_ids=tuple(holdouts),
            calibration_balanced_package_sizes=tuple(sizes),
            reuse_verification=(
                None
                if verification is None
                else FormulaVerificationReport.from_record(verification)
            ),
        )
        if record["attestation_id"] != result.attestation_id:
            raise ValueError("methodology attestation content does not match attestation_id")
        return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _ids(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a collection of identifiers")
    try:
        rows = tuple(_text(name, row) for row in value)
    except TypeError:
        raise ValueError(f"{name} must be a collection of identifiers") from None
    if not rows or len(set(rows)) != len(rows):
        raise ValueError(f"{name} must contain distinct identifiers")
    return rows


def _sizes(name: str, value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a collection of integers")
    try:
        rows = tuple(value)
    except TypeError:
        raise ValueError(f"{name} must be a collection of integers") from None
    if (
        not rows
        or any(type(row) is not int or row < 1 for row in rows)
        or len(set(rows)) != len(rows)
    ):
        raise ValueError(f"{name} must contain distinct positive integers")
    return tuple(sorted(rows))


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


__all__ = ("MethodologyAttestation",)
