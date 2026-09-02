"""Strict evidence required before reusing a strength formula for a new week."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from numbers import Real
from collections.abc import Mapping

from ._scenario_random import content_id


MINIMUM_WEEKLY_VERIFICATION_HOLDOUTS = 100
MAXIMUM_EXACT_VERIFICATION_ERROR = 1e-6
REQUIRED_BALANCED_PACKAGE_SIZES = frozenset({2, 3, 4})


@dataclass(frozen=True, slots=True)
class FormulaVerificationReport:
    """Audited ordinary-power holdouts for one formula and weekly snapshot."""

    formula_id: str
    methodology_fingerprint_id: str
    weekly_snapshot_id: str
    ordinary_power_holdout_ids: tuple[str, ...]
    balanced_package_sizes: tuple[int, ...]
    max_absolute_score_error: float
    max_absolute_delta_error: float
    display_match_rate: float
    verified_at: datetime
    verification_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("formula_id", "methodology_fingerprint_id", "weekly_snapshot_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if isinstance(self.ordinary_power_holdout_ids, (str, bytes)):
            raise ValueError(
                "ordinary_power_holdout_ids must be a collection of identifiers"
            )
        try:
            holdouts = tuple(self.ordinary_power_holdout_ids)
        except TypeError:
            raise ValueError(
                "ordinary_power_holdout_ids must be a collection of identifiers"
            ) from None
        if any(not isinstance(value, str) or not value.strip() for value in holdouts):
            raise ValueError(
                "ordinary_power_holdout_ids must contain non-empty strings"
            )
        holdouts = tuple(value.strip() for value in holdouts)
        if len(set(holdouts)) != len(holdouts):
            raise ValueError("ordinary_power_holdout_ids must be distinct")
        object.__setattr__(self, "ordinary_power_holdout_ids", holdouts)
        if isinstance(self.balanced_package_sizes, (str, bytes)):
            raise ValueError("balanced_package_sizes must be a collection of integers")
        try:
            sizes = tuple(self.balanced_package_sizes)
        except TypeError:
            raise ValueError(
                "balanced_package_sizes must be a collection of integers"
            ) from None
        if (
            not sizes
            or any(type(value) is not int or value < 1 for value in sizes)
            or len(set(sizes)) != len(sizes)
        ):
            raise ValueError(
                "balanced_package_sizes must contain distinct positive integers"
            )
        sizes = tuple(sorted(sizes))
        object.__setattr__(self, "balanced_package_sizes", sizes)
        for name in ("max_absolute_score_error", "max_absolute_delta_error"):
            value = _finite(name, getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        rate = _finite("display_match_rate", self.display_match_rate)
        if not 0 <= rate <= 1:
            raise ValueError("display_match_rate must be between zero and one")
        object.__setattr__(self, "display_match_rate", rate)
        if (
            not isinstance(self.verified_at, datetime)
            or self.verified_at.tzinfo is None
            or self.verified_at.utcoffset() is None
        ):
            raise ValueError("verified_at must be a timezone-aware datetime")
        verified_at = self.verified_at.astimezone(timezone.utc)
        object.__setattr__(self, "verified_at", verified_at)
        object.__setattr__(
            self,
            "verification_id",
            content_id("formula-verification", self._content_record()),
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "balanced_package_sizes": list(self.balanced_package_sizes),
            "display_match_rate": self.display_match_rate,
            "formula_id": self.formula_id,
            "max_absolute_delta_error": self.max_absolute_delta_error,
            "max_absolute_score_error": self.max_absolute_score_error,
            "methodology_fingerprint_id": self.methodology_fingerprint_id,
            "ordinary_power_holdout_ids": list(self.ordinary_power_holdout_ids),
            "verified_at": self.verified_at.isoformat(),
            "weekly_snapshot_id": self.weekly_snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {
            **self._content_record(),
            "schema_version": 1,
            "verification_id": self.verification_id,
        }

    @classmethod
    def from_record(cls, record: object) -> "FormulaVerificationReport":
        content = {
            "balanced_package_sizes",
            "display_match_rate",
            "formula_id",
            "max_absolute_delta_error",
            "max_absolute_score_error",
            "methodology_fingerprint_id",
            "ordinary_power_holdout_ids",
            "verified_at",
            "weekly_snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != content | {
            "schema_version",
            "verification_id",
        }:
            raise ValueError("formula verification record fields are invalid")
        if type(record["schema_version"]) is not int or record["schema_version"] != 1:
            raise ValueError("formula verification schema version is invalid")
        holdouts = record["ordinary_power_holdout_ids"]
        sizes = record["balanced_package_sizes"]
        if not isinstance(holdouts, list) or not isinstance(sizes, list):
            raise ValueError("formula verification arrays are invalid")
        try:
            verified_at = datetime.fromisoformat(record["verified_at"])
        except (TypeError, ValueError):
            raise ValueError("formula verification verified_at is invalid") from None
        result = cls(
            formula_id=record["formula_id"],
            methodology_fingerprint_id=record["methodology_fingerprint_id"],
            weekly_snapshot_id=record["weekly_snapshot_id"],
            ordinary_power_holdout_ids=tuple(holdouts),
            balanced_package_sizes=tuple(sizes),
            max_absolute_score_error=record["max_absolute_score_error"],
            max_absolute_delta_error=record["max_absolute_delta_error"],
            display_match_rate=record["display_match_rate"],
            verified_at=verified_at,
        )
        if record["verification_id"] != result.verification_id:
            raise ValueError("formula verification content does not match verification_id")
        return result

    def rejection_reasons(
        self,
        *,
        formula_id: str,
        methodology_fingerprint_id: str,
        weekly_snapshot_id: str,
    ) -> tuple[str, ...]:
        """Return every reason this report cannot authorize formula reuse."""

        reasons = []
        if self.formula_id != formula_id:
            reasons.append("weekly verification was produced for a different formula")
        if self.methodology_fingerprint_id != methodology_fingerprint_id:
            reasons.append(
                "weekly verification was produced for a different analyzer methodology"
            )
        if self.weekly_snapshot_id != weekly_snapshot_id:
            reasons.append("weekly verification was produced for a different snapshot")
        count = len(self.ordinary_power_holdout_ids)
        if count < MINIMUM_WEEKLY_VERIFICATION_HOLDOUTS:
            reasons.append(
                "weekly verification used "
                f"{count} distinct ordinary-power holdouts; "
                f"at least {MINIMUM_WEEKLY_VERIFICATION_HOLDOUTS} are required"
            )
        missing_sizes = REQUIRED_BALANCED_PACKAGE_SIZES.difference(
            self.balanced_package_sizes
        )
        if missing_sizes:
            reasons.append(
                "weekly verification did not cover balanced package sizes "
                + ", ".join(str(value) for value in sorted(missing_sizes))
            )
        if self.max_absolute_score_error > MAXIMUM_EXACT_VERIFICATION_ERROR:
            reasons.append("weekly verification raw score error exceeded 1e-6")
        if self.max_absolute_delta_error > MAXIMUM_EXACT_VERIFICATION_ERROR:
            reasons.append("weekly verification raw delta error exceeded 1e-6")
        if self.display_match_rate != 1.0:
            reasons.append("weekly verification did not match every displayed change")
        return tuple(reasons)


def verification_report_from_calibration_session(
    session: object,
    observations: Mapping[str, object],
    formula: object,
    *,
    weekly_snapshot_id: str,
    verified_at: datetime,
) -> FormulaVerificationReport:
    """Score fresh blind analyzer trades with frozen local coefficients.

    Imports stay local so the verification data type remains independent of the
    browser/calibration coordinator.  This function performs no network I/O;
    callers supply the bounded ordinary-power observations captured once during
    weekly refresh.
    """

    from ._calibration_validation import heldout_trade_metrics
    from .calibration_observations import prepare_calibration_evidence
    from .calibration_workflow import CalibrationSession
    from .strength_formula import StrengthFormula

    if not isinstance(session, CalibrationSession):
        raise ValueError("session must be a CalibrationSession")
    if not isinstance(observations, Mapping):
        raise ValueError("observations must be a mapping")
    if not isinstance(formula, StrengthFormula):
        raise ValueError("formula must be a StrengthFormula")
    if (
        not isinstance(weekly_snapshot_id, str)
        or not weekly_snapshot_id.strip()
        or weekly_snapshot_id.strip() != session.features.snapshot_id
    ):
        raise ValueError(
            "weekly_snapshot_id must match the calibration feature snapshot"
        )
    prepared = prepare_calibration_evidence(
        session.plan,
        observations,
        session.features,
        session.roles,
        session.rosters,
        team_provider_ids=session.team_provider_ids,
        player_provider_ids=session.player_provider_ids,
    )
    if prepared.bundle != session.fingerprint.analyzer_bundle:
        raise ValueError("captured analyzer bundle changed during verification")
    model = formula.build_model(session.features, session.rosters)
    metrics = heldout_trade_metrics(model, prepared.corpus.held_out_trades)
    if (
        metrics.max_absolute_score_error is None
        or metrics.max_delta_error is None
        or metrics.display_match_rate is None
    ):
        raise ValueError("formula verification requires blind holdout trades")
    return FormulaVerificationReport(
        formula_id=formula.formula_id,
        methodology_fingerprint_id=session.fingerprint.fingerprint_id,
        weekly_snapshot_id=weekly_snapshot_id.strip(),
        ordinary_power_holdout_ids=tuple(
            row.trade_id for row in prepared.corpus.held_out_trades
        ),
        balanced_package_sizes=session.plan.held_out_balanced_package_sizes,
        max_absolute_score_error=metrics.max_absolute_score_error,
        max_absolute_delta_error=metrics.max_delta_error,
        display_match_rate=metrics.display_match_rate,
        verified_at=verified_at,
    )


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


__all__ = (
    "FormulaVerificationReport",
    "MAXIMUM_EXACT_VERIFICATION_ERROR",
    "MINIMUM_WEEKLY_VERIFICATION_HOLDOUTS",
    "REQUIRED_BALANCED_PACKAGE_SIZES",
    "verification_report_from_calibration_session",
)
