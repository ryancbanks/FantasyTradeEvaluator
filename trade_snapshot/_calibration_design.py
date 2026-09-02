"""Assignment design matrix and bounded coordinate solver."""

from math import fsum, isfinite
from typing import Mapping, Sequence

from ._calibration_inputs import (
    CalibrationCorpus,
    CalibrationTradeObservation,
    PlayerFeatureVector,
    RosterPowerSample,
)
from ._calibration_results import CalibrationFitConfig
from .lineup import LineupPlayer, optimize_lineup
from .strength_calibration import PlayerStrength


class CoefficientLayout:
    def __init__(self, corpus: CalibrationCorpus, config: CalibrationFitConfig) -> None:
        self.roles = corpus.role_definitions
        self.residual_names = config.residual_feature_names
        self.role_names = config.role_feature_names
        self.size = len(self.residual_names) + len(self.roles) * len(self.role_names)

    def initial_coefficients(self) -> tuple[float, ...]:
        role_value = 1.0 / len(self.role_names)
        return (0.0,) * len(self.residual_names) + (role_value,) * (
            len(self.roles) * len(self.role_names)
        )

    def unpack(
        self, coefficients: Sequence[float]
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        residual = dict(zip(self.residual_names, coefficients[: len(self.residual_names)]))
        roles: dict[str, dict[str, float]] = {}
        offset = len(self.residual_names)
        for index, role in enumerate(self.roles):
            start = offset + index * len(self.role_names)
            roles[role.role_id] = dict(
                zip(self.role_names, coefficients[start : start + len(self.role_names)])
            )
        return residual, roles

    def design_row(
        self,
        roster: Sequence[str],
        features: Mapping[str, PlayerFeatureVector],
        coefficients: Sequence[float],
    ) -> tuple[tuple[float, ...], tuple[str | None, ...]]:
        _, role_weights = self.unpack(coefficients)
        players = []
        for player_id in roster:
            feature = features[player_id]
            weights = {
                role.role_id: dot(role_weights[role.role_id], feature.values)
                for role in self.roles
                if feature.eligible_positions.intersection(role.eligible_positions)
            }
            players.append(LineupPlayer(player_id, weights))
        assignment = optimize_lineup(
            tuple(role.role_id for role in self.roles), tuple(players)
        )
        values = [
            _safe_sum(features[player_id].values[name] for player_id in roster)
            for name in self.residual_names
        ]
        selected = {row.slot: row.player_id for row in assignment.assignments}
        for role in self.roles:
            player_id = selected[role.role_id]
            values.extend(
                0.0 if player_id is None else features[player_id].values[name]
                for name in self.role_names
            )
        return tuple(values), tuple(row.player_id for row in assignment.assignments)


def validate_feature_configuration(
    corpus: CalibrationCorpus, config: CalibrationFitConfig
) -> None:
    available = set(corpus.player_features[0].values)
    requested = set(config.residual_feature_names).union(config.role_feature_names)
    missing = requested.difference(available)
    if missing:
        raise ValueError(f"fit config references missing feature {min(missing)!r}")
    for row in corpus.player_features:
        for name in config.role_feature_names:
            if row.values[name] < 0:
                raise ValueError("role features must be non-negative")


def design_matrix(
    samples: Sequence[RosterPowerSample],
    features: Mapping[str, PlayerFeatureVector],
    layout: CoefficientLayout,
    coefficients: Sequence[float],
) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[str | None, ...], ...]]:
    rows = tuple(
        layout.design_row(sample.roster_player_ids, features, coefficients)
        for sample in samples
    )
    return tuple(row for row, _ in rows), tuple(signature for _, signature in rows)


def distinct_trade_perturbation_count(
    trades: Sequence[CalibrationTradeObservation],
    features: Mapping[str, PlayerFeatureVector],
    layout: CoefficientLayout,
    coefficients: Sequence[float],
) -> int:
    """Count nonzero bilateral design changes, independent of observed scores."""

    signatures = set()
    for trade in trades:
        team_changes = []
        for before, after in (
            (
                trade.team1_before_player_ids,
                trade.team1_after_player_ids,
            ),
            (
                trade.team2_before_player_ids,
                trade.team2_after_player_ids,
            ),
        ):
            before_row, _ = layout.design_row(before, features, coefficients)
            after_row, _ = layout.design_row(after, features, coefficients)
            delta = tuple(
                _canonical_delta(right - left)
                for left, right in zip(before_row, after_row)
            )
            team_changes.append(delta)
        signature = tuple(sorted(team_changes))
        if any(value != 0.0 for delta in signature for value in delta):
            signatures.add(signature)
    return len(signatures)


def coordinate_descent(
    matrix: Sequence[Sequence[float]],
    targets: Sequence[float],
    initial: Sequence[float],
    *,
    signed_count: int,
    ridge: float,
    max_iterations: int,
    tolerance: float,
) -> tuple[float, ...]:
    scales = tuple(
        max(abs(row[column]) for row in matrix)
        for column in range(len(initial))
    )
    scaled_matrix = tuple(
        tuple(
            0.0 if scales[column] == 0 else value / scales[column]
            for column, value in enumerate(row)
        )
        for row in matrix
    )
    coefficients = [
        value * scales[column] for column, value in enumerate(initial)
    ]
    predictions = [
        _safe_sum(x * beta for x, beta in zip(row, coefficients))
        for row in scaled_matrix
    ]
    try:
        for _ in range(max_iterations):
            largest_change = 0.0
            for column in range(len(coefficients)):
                old = coefficients[column]
                denominator = ridge + _safe_sum(
                    row[column] * row[column] for row in scaled_matrix
                )
                if denominator == 0:
                    new = 0.0
                else:
                    numerator = _safe_sum(
                        row[column] * (target - prediction + row[column] * old)
                        for row, target, prediction in zip(
                            scaled_matrix, targets, predictions
                        )
                    )
                    new = numerator / denominator
                    if column >= signed_count:
                        new = max(0.0, new)
                    if not isfinite(new):
                        raise ValueError("calibration coefficients became non-finite")
                delta = new - old
                if delta:
                    coefficients[column] = new
                    predictions = [
                        prediction + row[column] * delta
                        for row, prediction in zip(scaled_matrix, predictions)
                    ]
                    if any(not isfinite(value) for value in predictions):
                        raise ValueError("calibration predictions became non-finite")
                    largest_change = max(largest_change, abs(delta))
            if largest_change <= tolerance:
                break
    except OverflowError:
        raise ValueError("calibration numeric range overflowed") from None
    result = tuple(
        0.0 if scale == 0 else value / scale
        for value, scale in zip(coefficients, scales)
    )
    if any(not isfinite(value) for value in result):
        raise ValueError("unscaled calibration coefficients are non-finite")
    return result


def normalize_to_baseline_max(
    coefficients: Sequence[float],
    baselines: Mapping[str, Sequence[str]],
    features: Mapping[str, PlayerFeatureVector],
    layout: CoefficientLayout,
) -> tuple[float, ...]:
    scores = []
    for roster in baselines.values():
        row, _ = layout.design_row(roster, features, coefficients)
        scores.append(_safe_sum(value * weight for value, weight in zip(row, coefficients)))
    maximum = max(scores)
    if maximum <= 0:
        raise ValueError("fitted features do not produce positive baseline strength")
    normalized = tuple(value * (100.0 / maximum) for value in coefficients)
    if any(not isfinite(value) for value in normalized):
        raise ValueError("normalized calibration coefficients are non-finite")
    return normalized


def player_strength_rows(
    features: Mapping[str, PlayerFeatureVector],
    layout: CoefficientLayout,
    coefficients: Sequence[float],
) -> tuple[PlayerStrength, ...]:
    residual_weights, role_weights = layout.unpack(coefficients)
    return tuple(
        PlayerStrength(
            player_id,
            dot(residual_weights, feature.values),
            feature.eligible_positions,
            {
                role.role_id: dot(role_weights[role.role_id], feature.values)
                for role in layout.roles
                if feature.eligible_positions.intersection(role.eligible_positions)
            },
        )
        for player_id, feature in sorted(features.items())
    )


def matrix_rank(matrix: Sequence[Sequence[float]], *, tolerance: float = 1e-10) -> int:
    if not matrix:
        return 0
    column_count = len(matrix[0])
    scales = [max(abs(row[column]) for row in matrix) for column in range(column_count)]
    work = [
        [0.0 if scales[column] == 0 else row[column] / scales[column]
         for column in range(column_count)]
        for row in matrix
    ]
    rank = pivot_row = 0
    for column in range(column_count):
        pivot = max(range(pivot_row, len(work)), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= tolerance:
            continue
        work[pivot_row], work[pivot] = work[pivot], work[pivot_row]
        divisor = work[pivot_row][column]
        for row in range(pivot_row + 1, len(work)):
            factor = work[row][column] / divisor
            if factor:
                for child in range(column, column_count):
                    work[row][child] -= factor * work[pivot_row][child]
        rank += 1
        pivot_row += 1
        if pivot_row == len(work):
            break
    return rank


def dot(weights: Mapping[str, float], values: Mapping[str, float]) -> float:
    return _safe_sum(weight * values[name] for name, weight in weights.items())


def _safe_sum(values) -> float:
    try:
        result = fsum(values)
    except OverflowError:
        raise ValueError("calibration numeric range overflowed") from None
    if not isfinite(result):
        raise ValueError("calibration numeric result is non-finite")
    return result


def _canonical_delta(value: float) -> float:
    if not isfinite(value):
        raise ValueError("calibration perturbation became non-finite")
    return 0.0 if value == 0.0 else value
