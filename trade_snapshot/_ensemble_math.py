"""Numerically stable weighted statistics for provider ensembles."""

import math

from ._ensemble_validation import is_finite_number
from .projections import ProjectionStatus


def weighted_metrics(observations, floor: float) -> tuple[float, float, float]:
    observed = tuple(
        item for item in observations if item.status is ProjectionStatus.OBSERVED
    )
    try:
        weight_scale = max(item.weight for item in observed)
        scaled_weights = tuple(item.weight / weight_scale for item in observed)
        total_scaled_weight = math.fsum(scaled_weights)
        normalized_weights = tuple(
            weight / total_scaled_weight for weight in scaled_weights
        )
        point_scale = max(abs(item.projected_fantasy_points) for item in observed)
        if point_scale:
            scaled_points = tuple(
                item.projected_fantasy_points / point_scale for item in observed
            )
            scaled_mean = math.fsum(
                weight * points
                for weight, points in zip(normalized_weights, scaled_points)
            )
            lower_point, upper_point = min(scaled_points), max(scaled_points)
            scaled_mean = min(upper_point, max(lower_point, scaled_mean))
            mean = scaled_mean * point_scale
            scaled_variance = math.fsum(
                weight * (points - scaled_mean) ** 2
                for weight, points in zip(normalized_weights, scaled_points)
            )
            variance_bound = (upper_point - lower_point) ** 2 / 4
            scaled_variance = min(variance_bound, max(0.0, scaled_variance))
            disagreement = math.sqrt(scaled_variance) * point_scale
        else:
            mean = disagreement = 0.0
        predictive = math.hypot(floor, disagreement)
    except (OverflowError, ValueError, ZeroDivisionError):
        raise ValueError("ensemble calculation produced a nonfinite result") from None
    if not all(is_finite_number(value) for value in (mean, disagreement, predictive)):
        raise ValueError("ensemble calculation produced a nonfinite result")
    return mean, disagreement, predictive
