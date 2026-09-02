"""Public feature-calibration API with strict held-out trade validation."""

from datetime import datetime

from ._analyzer_types import BundleFingerprint
from ._calibration_inputs import (
    CalibrationCorpus,
    CalibrationTradeObservation,
    PlayerFeatureVector,
    RosterPowerSample,
)
from ._calibration_results import (
    CALIBRATION_SOLVER_ALGORITHM,
    MINIMUM_EXACT_TRADE_COUNT,
    CalibrationDiagnostics,
    CalibrationFitConfig,
    FittedStrengthCalibration,
)


__all__ = (
    "CALIBRATION_SOLVER_ALGORITHM",
    "CalibrationCorpus",
    "CalibrationDiagnostics",
    "CalibrationFitConfig",
    "CalibrationTradeObservation",
    "FittedStrengthCalibration",
    "MINIMUM_EXACT_TRADE_COUNT",
    "PlayerFeatureVector",
    "RosterPowerSample",
    "fit_strength_surrogate",
)


def fit_strength_surrogate(
    corpus: CalibrationCorpus,
    config: CalibrationFitConfig,
    *,
    bundle: BundleFingerprint,
    response_schema_sha256: str,
    captured_at: datetime,
) -> FittedStrengthCalibration:
    """Fit role/depth features and validate on atomic, unseen trades."""

    from ._calibration_solver import fit_strength_surrogate as solve

    return solve(
        corpus,
        config,
        bundle=bundle,
        response_schema_sha256=response_schema_sha256,
        captured_at=captured_at,
    )
