"""Compact catalog and exact-player response views for Player Lab."""

from collections.abc import Mapping
import json


_CATALOG_PROFILE_FIELDS = frozenset(
    {"status", "depth_chart", "market_trend", "performance_trend"}
)
_CATALOG_BURDEN_FIELDS = frozenset(
    {"status", "burden_index", "burden_tier"}
)
_CATALOG_PLAYER_FIELDS = frozenset(
    {
        "player_id",
        "name",
        "position",
        "eligible_slots",
        "nfl_team_id",
        "owner",
        "availability",
        "weekly_ecr",
        "rest_of_season_ecr",
        "remaining_projected_points",
        "average_weekly_points",
        "average_provider_disagreement",
        "provider_complete_week_count",
        "all_direct_week_count",
        "total_week_count",
        "projection_overall_rank",
        "projection_position_rank",
        "overall_rank",
        "overall_rank_basis",
        "profile",
        # Retain pre-v2 list fields when a legacy adapter supplies them directly.
        "depth_chart_position",
        "depth_chart_order",
        "market_trend",
        "performance_trend",
        "historical_availability",
        "availability_risk",
    }
)
_CATALOG_TREND_FIELDS = frozenset(
    {"status", "direction", "change", "adds", "drops", "net_adds"}
)
_CATALOG_ECR_FIELDS = frozenset({"rank", "position_rank"})


def build_player_outlook_catalog(outlook: Mapping[str, object]) -> dict[str, object]:
    """Return the bounded Player Lab list view without per-player evidence rows."""

    players = outlook.get("players")
    if not isinstance(players, list):
        raise ValueError("player outlook players must be a list")
    result = {key: value for key, value in outlook.items() if key != "players"}
    result["view"] = "catalog"
    result["players"] = [_catalog_player_record(row) for row in players]
    _require_strict_json(result, "player outlook catalog")
    return result


def select_player_outlook_detail(
    outlook: Mapping[str, object], player_id: str
) -> dict[str, object]:
    """Select one exact canonical player ID from a full cached outlook."""

    if not isinstance(player_id, str) or not player_id:
        raise ValueError("player_id must be a non-empty string")
    players = outlook.get("players")
    if not isinstance(players, list):
        raise ValueError("player outlook players must be a list")
    player = next(
        (
            row
            for row in players
            if isinstance(row, Mapping) and row.get("player_id") == player_id
        ),
        None,
    )
    if player is None:
        raise KeyError(player_id)
    result = {
        "schema_version": outlook.get("schema_version"),
        "bundle_id": outlook.get("bundle_id"),
        "snapshot_id": outlook.get("snapshot_id"),
        "scoring_mode": outlook.get("scoring_mode"),
        "view": "player_detail",
        "player": player,
    }
    _require_strict_json(result, "player outlook detail")
    return result


def _catalog_player_record(value):
    if not isinstance(value, Mapping):
        raise ValueError("player outlook contains a non-object player")
    result = {
        key: item
        for key, item in value.items()
        if key in _CATALOG_PLAYER_FIELDS
    }
    for field in ("weekly_ecr", "rest_of_season_ecr"):
        if isinstance(result.get(field), Mapping):
            result[field] = {
                key: item
                for key, item in result[field].items()
                if key in _CATALOG_ECR_FIELDS
            }
    profile = value.get("profile")
    if isinstance(profile, Mapping):
        compact_profile = {
            field: item
            for field, item in profile.items()
            if field in _CATALOG_PROFILE_FIELDS
        }
        for field in ("market_trend", "performance_trend"):
            if isinstance(compact_profile.get(field), Mapping):
                compact_profile[field] = {
                    key: item
                    for key, item in compact_profile[field].items()
                    if key in _CATALOG_TREND_FIELDS
                }
        history = profile.get("historical_availability")
        if isinstance(history, Mapping):
            compact_profile["historical_availability"] = (
                _catalog_availability_record(history)
            )
        result["profile"] = compact_profile
    for field in ("market_trend", "performance_trend"):
        if isinstance(result.get(field), Mapping):
            result[field] = {
                key: item
                for key, item in result[field].items()
                if key in _CATALOG_TREND_FIELDS
            }
    legacy_history = result.get("historical_availability")
    if isinstance(legacy_history, Mapping):
        result["historical_availability"] = _catalog_availability_record(
            legacy_history
        )
    legacy_risk = result.get("availability_risk")
    if isinstance(legacy_risk, Mapping):
        result["availability_risk"] = _catalog_availability_record(legacy_risk)
    return result


def _catalog_availability_record(history):
    """Compact availability data and normalize the pre-release risk field names."""

    result = {
        field: item
        for field, item in history.items()
        if field in _CATALOG_BURDEN_FIELDS
    }
    if "burden_index" not in result and "risk_score" in history:
        result["burden_index"] = history["risk_score"]
    if "burden_tier" not in result and "risk_tier" in history:
        result["burden_tier"] = history["risk_tier"]
    return result


def _require_strict_json(value, label):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AssertionError(f"{label} must contain strict JSON data") from error


__all__ = (
    "build_player_outlook_catalog",
    "select_player_outlook_detail",
)
