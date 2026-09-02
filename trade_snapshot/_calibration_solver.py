"""Coordinator for deterministic feature-based strength calibration."""

from datetime import datetime
from math import fsum, sqrt

from ._analyzer_types import BundleFingerprint
from ._calibration_design import (
    CoefficientLayout,
    coordinate_descent,
    design_matrix,
    distinct_trade_perturbation_count,
    matrix_rank,
    normalize_to_baseline_max,
    player_strength_rows,
    validate_feature_configuration,
)
from ._calibration_inputs import CalibrationCorpus
from ._calibration_results import (
    CalibrationDiagnostics,
    CalibrationFitConfig,
    FittedStrengthCalibration,
)
from ._calibration_validation import (
    calibration_status,
    heldout_trade_metrics,
    training_errors,
)
from .strength import StrengthModel
from .strength_calibration import CalibrationMetadata


def fit_strength_surrogate(
    corpus: CalibrationCorpus,
    config: CalibrationFitConfig,
    *,
    bundle: BundleFingerprint,
    response_schema_sha256: str,
    captured_at: datetime,
) -> FittedStrengthCalibration:
    if not isinstance(corpus, CalibrationCorpus):
        raise ValueError("corpus must be a CalibrationCorpus")
    if not isinstance(config, CalibrationFitConfig):
        raise ValueError("config must be a CalibrationFitConfig")
    if not isinstance(bundle, BundleFingerprint):
        raise ValueError("bundle must be a BundleFingerprint")
    validate_feature_configuration(corpus, config)
    provisional_metadata = CalibrationMetadata(
        analyzer_bundle_url=bundle.url,
        analyzer_bundle_sha256=bundle.sha256,
        response_schema_sha256=response_schema_sha256,
        captured_at=captured_at,
    )

    features = {row.player_id: row for row in corpus.player_features}
    layout = CoefficientLayout(corpus, config)
    coefficients = layout.initial_coefficients()
    converged = False
    iterations = 0
    targets = tuple(row.raw_power_score for row in corpus.samples)
    for iterations in range(1, config.max_outer_iterations + 1):
        matrix, signature = design_matrix(
            corpus.samples, features, layout, coefficients
        )
        updated = coordinate_descent(
            matrix,
            targets,
            coefficients,
            signed_count=len(config.residual_feature_names),
            ridge=config.ridge_penalty,
            max_iterations=config.max_coordinate_iterations,
            tolerance=config.convergence_tolerance,
        )
        updated = normalize_to_baseline_max(
            updated, corpus.baseline_rosters, features, layout
        )
        max_change = max(abs(left - right) for left, right in zip(coefficients, updated))
        _, updated_signature = design_matrix(corpus.samples, features, layout, updated)
        coefficients = updated
        if updated_signature == signature and max_change <= config.convergence_tolerance:
            converged = True
            break

    final_matrix, _ = design_matrix(corpus.samples, features, layout, coefficients)
    design_rank = matrix_rank(final_matrix)
    identifiable = design_rank == layout.size
    if config.ridge_penalty == 0 and not identifiable:
        raise ValueError(
            "unregularized calibration design is rank deficient; add independent "
            "training trades, remove redundant features, or use a ridge penalty"
        )
    player_rows = player_strength_rows(features, layout, coefficients)
    provisional = _model(corpus, player_rows, 1.0, provisional_metadata)
    denominator = max(
        provisional.score_roster(roster).absolute_score
        for roster in corpus.baseline_rosters.values()
    )
    if denominator <= 0:
        raise ValueError("fitted baseline strength must be positive")
    provisional = _model(corpus, player_rows, denominator, provisional_metadata)
    errors = training_errors(provisional, corpus.samples)
    training_max = max(abs(value) for value in errors)
    holdout = heldout_trade_metrics(provisional, corpus.held_out_trades)
    distinct_perturbations = distinct_trade_perturbation_count(
        corpus.held_out_trades, features, layout, coefficients
    )
    status = calibration_status(
        config,
        converged=converged,
        identifiable=identifiable,
        training_max_error=training_max,
        trades=corpus.held_out_trades,
        distinct_perturbation_count=distinct_perturbations,
        holdout=holdout,
    )
    metadata = CalibrationMetadata(
        analyzer_bundle_url=bundle.url,
        analyzer_bundle_sha256=bundle.sha256,
        response_schema_sha256=response_schema_sha256,
        captured_at=captured_at,
        status=status,
        held_out_trade_count=len(corpus.held_out_trades),
        max_absolute_score_error=holdout.max_absolute_score_error,
        display_match_rate=holdout.display_match_rate,
    )
    model = _model(corpus, player_rows, denominator, metadata)
    residual, roles = layout.unpack(coefficients)
    diagnostics = CalibrationDiagnostics(
        converged=converged,
        outer_iterations=iterations,
        coefficient_count=layout.size,
        design_rank=design_rank,
        training_sample_count=len(corpus.samples),
        held_out_trade_count=len(corpus.held_out_trades),
        held_out_distinct_perturbation_count=distinct_perturbations,
        training_rmse=sqrt(fsum(value * value for value in errors) / len(errors)),
        training_max_absolute_error=training_max,
        holdout_max_absolute_score_error=holdout.max_absolute_score_error,
        holdout_max_delta_error=holdout.max_delta_error,
        holdout_display_match_rate=holdout.display_match_rate,
    )
    return FittedStrengthCalibration(
        model=model,
        corpus=corpus,
        config=config,
        residual_weights=residual,
        role_weights=roles,
        diagnostics=diagnostics,
    )


def _model(corpus, players, denominator, metadata) -> StrengthModel:
    return StrengthModel(
        corpus.role_definitions,
        players,
        denominator,
        snapshot_id=corpus.snapshot_id,
        season=corpus.season,
        scoring_profile_id=corpus.scoring_profile_id,
        calibration=metadata,
    )
