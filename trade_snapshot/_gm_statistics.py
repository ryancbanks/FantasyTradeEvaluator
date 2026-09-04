"""Small deterministic statistics used by General Manager Insights."""

from math import sqrt
from statistics import fmean, median


def percentile(value: float, population) -> float | None:
    values = sorted(float(row) for row in population)
    if not values:
        return None
    if len(values) == 1:
        return 0.5
    less = sum(row < value for row in values)
    equal = sum(row == value for row in values)
    return (less + (equal - 1) / 2) / (len(values) - 1)


def wilson_interval(successes: int, trials: int, z: float = 1.96):
    if trials < 1 or not 0 <= successes <= trials:
        return None
    proportion = successes / trials
    denominator = 1 + z * z / trials
    center = (proportion + z * z / (2 * trials)) / denominator
    radius = z * sqrt(
        (proportion * (1 - proportion) + z * z / (4 * trials)) / trials
    ) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def poisson_rate_interval(count: int, exposure: int, scale: float = 10.0):
    if count < 0 or exposure < 1:
        return None
    rate = count / exposure * scale
    if count == 0:
        return 0.0, 3.0 / exposure * scale
    radius = 1.96 * sqrt(count) / exposure * scale
    return max(0.0, rate - radius), rate + radius


def predictive_active_probability(active_weeks: int, observed_weeks: int, horizon: int = 2):
    """Jeffreys-posterior chance of activity in at least one future week."""

    if observed_weeks < 1 or not 0 <= active_weeks <= observed_weeks or horizon < 1:
        return None
    alpha = active_weeks + 0.5
    beta = observed_weeks - active_weeks + 0.5
    probability_no_activity = 1.0
    for offset in range(horizon):
        probability_no_activity *= (beta + offset) / (alpha + beta + offset)
    return 1 - probability_no_activity


def summarize(values):
    rows = tuple(float(value) for value in values)
    if not rows:
        return None, None
    return fmean(rows), median(rows)


def partial_pool(values, league_values, prior_strength: float = 3.0):
    """Transparent normal shrinkage so tiny samples cannot create hard labels."""

    rows = tuple(float(value) for value in values)
    league = tuple(float(value) for value in league_values)
    if not rows:
        return None
    center = fmean(league) if league else 0.0
    if len(league) > 1:
        league_variance = sum((value - center) ** 2 for value in league) / (len(league) - 1)
    else:
        league_variance = 0.25
    scale = max(0.25, sqrt(max(league_variance, 0.0)))
    estimate = (sum(rows) + prior_strength * center) / (len(rows) + prior_strength)
    standard_error = scale / sqrt(len(rows) + prior_strength)
    return {
        "estimate": estimate,
        "interval_80": (
            estimate - 1.281551565545 * standard_error,
            estimate + 1.281551565545 * standard_error,
        ),
        "interval_90": (
            estimate - 1.644853626951 * standard_error,
            estimate + 1.644853626951 * standard_error,
        ),
        "interval_95": (
            estimate - 1.95996398454 * standard_error,
            estimate + 1.95996398454 * standard_error,
        ),
        "prior_mean": center,
        "prior_scale": scale,
        "prior_strength": prior_strength,
    }


def gini(values) -> float | None:
    rows = sorted(max(0.0, float(value)) for value in values)
    if not rows or sum(rows) == 0:
        return None
    count = len(rows)
    weighted = sum(index * value for index, value in enumerate(rows, 1))
    return (2 * weighted) / (count * sum(rows)) - (count + 1) / count


__all__ = (
    "gini",
    "partial_pool",
    "percentile",
    "poisson_rate_interval",
    "predictive_active_probability",
    "summarize",
    "wilson_interval",
)
