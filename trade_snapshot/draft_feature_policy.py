"""Versioned names for trustworthy pre-draft numeric model inputs."""

import re


# This value is serialized and content-addressed. Changing feature semantics
# requires a new policy version so saved corpora and boards fail closed.
PRESEASON_FEATURE_POLICY_VERSION = 1

_FEATURE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,52}$")
_PROVIDERS = frozenset({"ensemble", "espn", "fantasypros", "yahoo"})
_EXACT_FEATURES = frozenset(
    {
        "adp",
        "adp_position",
        "adp_rank",
        "adp_standard_deviation",
        "auction_value",
        "average_draft_position",
        "average_rank",
        "avg_rank",
        "best_rank",
        "consensus_rank",
        "ecr",
        "ecr_average_rank",
        "ecr_best_rank",
        "ecr_expert_count",
        "ecr_position_rank",
        "ecr_rank",
        "ecr_rank_stddev",
        "ecr_worst_rank",
        "expert_count",
        "overall_rank",
        "position_rank",
        "positional_rank",
        "projected_ceiling",
        "projected_fantasy_points",
        "projected_floor",
        "projected_games",
        "projected_games_played",
        "projected_fantasy_points_per_game",
        "projected_points",
        "projected_points_per_game",
        "projected_value",
        "projected_vorp",
        "rank",
        "rank_ave",
        "rank_average",
        "rank_max",
        "rank_min",
        "rank_std",
        "rank_stddev",
        "rank_stdev",
        "worst_rank",
    }
)
_IDENTITY_TOKENS = frozenset(
    {
        "club", "id", "identifier", "identity", "name", "nflteam", "player",
        "team",
    }
)
_OUTCOME_TOKENS = frozenset(
    {
        "accuracy",
        "actual",
        "champion",
        "earned",
        "error",
        "final",
        "finalized",
        "finish",
        "future",
        "hindsight",
        "label",
        "loss",
        "losses",
        "observed",
        "outcome",
        "playoff",
        "postseason",
        "realized",
        "residual",
        "result",
        "results",
        "score",
        "scores",
        "scored",
        "seasonend",
        "truth",
        "win",
        "winner",
        "wins",
        "yearend",
    }
)


def validate_preseason_feature_name(name: object) -> str:
    """Validate one v1 feature label as explicitly pre-draft information.

    The policy is intentionally opt-in rather than a blacklist. Totals, ranks,
    and projection columns must say what they are; custom linear-scoring stats
    use ``projected_stat.<stat-name>``. Provider-specific copies may add one of
    the supported source namespaces, for example
    ``espn.projected_stat.espn_stat_3``. This keeps arbitrary imported columns
    from silently becoming model inputs while retaining captured raw stats.
    """

    if not isinstance(name, str) or not _FEATURE_NAME.fullmatch(name):
        raise ValueError(
            "preseason feature names must be lowercase portable identifiers"
        )
    tokens = set(re.split(r"[_.-]", name.replace("nfl_team", "nflteam")))
    if tokens.intersection(_IDENTITY_TOKENS):
        raise ValueError(f"identity preseason feature {name!r} is forbidden")
    if (
        tokens.intersection(_OUTCOME_TOKENS)
        or "year_end" in name
        or "season_end" in name
    ):
        raise ValueError(
            f"outcome-derived preseason feature {name!r} is forbidden"
        )

    parts = name.split(".")
    if parts[0] in _PROVIDERS:
        parts = parts[1:]
    if not parts or any(not part for part in parts):
        raise ValueError(_policy_message(name))
    if parts[0] == "projected_stat":
        if len(parts) >= 2:
            return name
        raise ValueError(_policy_message(name))
    if len(parts) == 1 and parts[0] in _EXACT_FEATURES:
        return name
    raise ValueError(_policy_message(name))


def _policy_message(name: str) -> str:
    return (
        f"preseason feature {name!r} is not allowed by feature policy "
        f"v{PRESEASON_FEATURE_POLICY_VERSION}; use a documented total/rank field "
        "or projected_stat.<stat-name>"
    )


__all__ = (
    "PRESEASON_FEATURE_POLICY_VERSION",
    "validate_preseason_feature_name",
)
