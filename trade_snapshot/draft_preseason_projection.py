"""Leak-free starter projections derived only from the preceding NFL season."""

from collections.abc import Mapping
from math import fsum, isfinite

from .draft_config import score_raw_stats


PROJECTION_METHOD = "prior_season_per_game_carry_forward_v1"

# These are season-total counting stats rather than rates or realized fantasy
# scores.  Keeping the list explicit prevents a newly added nflverse outcome
# column from silently becoming a pre-draft model input.
PROJECTED_STAT_NAMES = (
    "attempts",
    "carries",
    "completions",
    "dst_fumble_recoveries",
    "dst_interceptions",
    "dst_points_allowed_0",
    "dst_points_allowed_1_6",
    "dst_points_allowed_7_13",
    "dst_points_allowed_14_20",
    "dst_points_allowed_21_27",
    "dst_points_allowed_28_34",
    "dst_points_allowed_35_plus",
    "dst_sacks",
    "dst_safeties",
    "dst_touchdowns",
    "dst_unclassified_recovery_touchdowns",
    "extra_points",
    "field_goals",
    "fumbles_lost",
    "interceptions",
    "passing_2pt_conversions",
    "passing_tds",
    "passing_yards",
    "receiving_2pt_conversions",
    "receiving_tds",
    "receiving_yards",
    "receptions",
    "rushing_2pt_conversions",
    "rushing_tds",
    "rushing_yards",
    "special_teams_tds",
    "targets",
)
PRESEASON_PROJECTION_FEATURE_NAMES = (
    "projected_fantasy_points",
    "projected_fantasy_points_per_game",
    "projected_games",
    *(f"projected_stat.{name}" for name in PROJECTED_STAT_NAMES),
)

# The point total is an additional portable PPR signal.  The raw projected
# stats remain separate so a custom league's regression can learn its own
# scoring relationship instead of treating this total as universal truth.
_PORTABLE_PPR_WEIGHTS = {
    "passing_yards": 0.04,
    "passing_tds": 4.0,
    "interceptions": -2.0,
    "rushing_yards": 0.1,
    "rushing_tds": 6.0,
    "receiving_yards": 0.1,
    "receiving_tds": 6.0,
    "receptions": 1.0,
    "fumbles_lost": -2.0,
    "field_goals": 3.0,
    "extra_points": 1.0,
    "dst_sacks": 1.0,
    "dst_interceptions": 2.0,
    "dst_fumble_recoveries": 2.0,
    "dst_touchdowns": 6.0,
    "dst_safeties": 2.0,
    "dst_points_allowed_0": 10.0,
    "dst_points_allowed_1_6": 7.0,
    "dst_points_allowed_7_13": 4.0,
    "dst_points_allowed_14_20": 1.0,
    "dst_points_allowed_28_34": -1.0,
    "dst_points_allowed_35_plus": -4.0,
}


def build_preseason_projection(
    prior_weeks: Mapping[int, Mapping[str, float]],
    *,
    projected_games: int,
) -> dict[str, float | None]:
    """Create a deterministic preseason baseline without target-season data.

    The method carries the preceding regular season's per-game counting stats
    over the current season's scheduled game count.  Missing prior experience
    remains explicit rather than being filled with a player's future results.
    """

    if type(projected_games) is not int or not 1 <= projected_games <= 25:
        raise ValueError("projected_games must be an integer from 1 through 25")
    features: dict[str, float | None] = {
        name: None for name in PRESEASON_PROJECTION_FEATURE_NAMES
    }
    features["projected_games"] = float(projected_games)
    if not isinstance(prior_weeks, Mapping) or not prior_weeks:
        return features

    weeks = tuple(prior_weeks.values())
    if any(not isinstance(row, Mapping) for row in weeks):
        raise ValueError("prior_weeks must map weeks to stat mappings")
    scale = projected_games / len(weeks)
    projected_stats = {
        name: _rounded(fsum(float(row.get(name, 0.0)) for row in weeks) * scale)
        for name in PROJECTED_STAT_NAMES
    }
    for name, value in projected_stats.items():
        features[f"projected_stat.{name}"] = value
    total = _rounded(score_raw_stats(projected_stats, _PORTABLE_PPR_WEIGHTS))
    features["projected_fantasy_points"] = total
    features["projected_fantasy_points_per_game"] = _rounded(
        total / projected_games
    )
    return features


def _rounded(value: float) -> float:
    result = round(float(value), 8)
    if not isfinite(result):
        raise ValueError("starter projection is not finite")
    return 0.0 if result == 0 else result


__all__ = (
    "PRESEASON_PROJECTION_FEATURE_NAMES",
    "PROJECTED_STAT_NAMES",
    "PROJECTION_METHOD",
    "build_preseason_projection",
)
