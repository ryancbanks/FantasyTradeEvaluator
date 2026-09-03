"""Strict deserialization for cached public player-data snapshots."""

from collections.abc import Mapping
from datetime import datetime
import json

from .public_player_data import (
    DataAvailability,
    PlayerInjuryReport,
    PlayerWeekStats,
    PublicDataProvenance,
    PublicPlayerDataSnapshot,
    PublicPlayerIdCrosswalk,
    SeasonInjuryReports,
    SeasonPlayerStats,
    SleeperPlayerMetadata,
    SleeperPlayerTrend,
)


_SNAPSHOT_FIELDS = {
    "kind", "schema_version", "season", "captured_at", "current_stats",
    "previous_stats", "injury_history", "sleeper_players", "trends",
    "id_crosswalk", "provenance", "data_id",
}


def snapshot_from_record(record) -> PublicPlayerDataSnapshot:
    row = _record("public player-data snapshot", record, _SNAPSHOT_FIELDS)
    if (
        row["kind"] != "public_player_data"
        or type(row["schema_version"]) is not int
        or row["schema_version"] != 2
    ):
        raise ValueError("public player-data kind or schema version is invalid")
    snapshot = PublicPlayerDataSnapshot(
        season=row["season"],
        captured_at=_time("captured_at", row["captured_at"]),
        current_stats=_season_stats(row["current_stats"]),
        previous_stats=_season_stats(row["previous_stats"]),
        injury_history=tuple(
            _injury_season(value)
            for value in _records("injury_history", row["injury_history"])
        ),
        sleeper_players=tuple(
            _sleeper_player(value)
            for value in _records("sleeper_players", row["sleeper_players"])
        ),
        trends=tuple(
            _trend(value) for value in _records("trends", row["trends"])
        ),
        id_crosswalk=tuple(
            _crosswalk(value)
            for value in _records("id_crosswalk", row["id_crosswalk"])
        ),
        provenance=tuple(
            _provenance(value)
            for value in _records("provenance", row["provenance"])
        ),
    )
    if row["data_id"] != snapshot.data_id:
        raise ValueError("public player-data content does not match data_id")
    return snapshot


def snapshot_from_json(payload) -> PublicPlayerDataSnapshot:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ValueError("public player-data JSON was not UTF-8") from None
    if not isinstance(payload, str):
        raise ValueError("public player-data JSON must be text or bytes")
    try:
        record = json.loads(
            payload,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, ValueError):
        raise ValueError("public player-data JSON was invalid or non-unique") from None
    return snapshot_from_record(record)


def _provenance(value):
    row = _record(
        "public provenance",
        value,
        {
            "provider", "dataset", "requested_url", "availability",
            "captured_at", "source_updated_at", "etag", "content_sha256",
            "byte_count",
        },
    )
    return PublicDataProvenance(
        provider=row["provider"],
        dataset=row["dataset"],
        requested_url=row["requested_url"],
        availability=_availability(row["availability"]),
        captured_at=_time("captured_at", row["captured_at"]),
        source_updated_at=_optional_time(
            "source_updated_at", row["source_updated_at"]
        ),
        etag=row["etag"],
        content_sha256=row["content_sha256"],
        byte_count=row["byte_count"],
    )


def _season_stats(value):
    row = _record(
        "season player stats", value, {"season", "availability", "rows"}
    )
    return SeasonPlayerStats(
        row["season"],
        _availability(row["availability"]),
        tuple(_week_stats(item) for item in _records("stats rows", row["rows"])),
    )


def _week_stats(value):
    fields = {
        "gsis_id", "display_name", "position", "season", "week", "game_id",
        "nfl_team_id", "opponent_team_id", "headshot_url",
        "fantasy_points_standard", "fantasy_points_ppr", "stat_values",
    }
    row = _record("player week stats", value, fields)
    stats = _mapping("stat_values", row["stat_values"])
    return PlayerWeekStats(
        row["gsis_id"], row["display_name"], row["position"], row["season"],
        row["week"], row["game_id"], row["nfl_team_id"],
        row["opponent_team_id"], row["headshot_url"],
        row["fantasy_points_standard"], row["fantasy_points_ppr"],
        tuple(stats.items()),
    )


def _injury_season(value):
    row = _record(
        "season injury reports", value, {"season", "availability", "rows"}
    )
    return SeasonInjuryReports(
        row["season"],
        _availability(row["availability"]),
        tuple(
            _injury_report(item)
            for item in _records("injury rows", row["rows"])
        ),
    )


def _injury_report(value):
    fields = {
        "gsis_id", "display_name", "position", "season", "week",
        "nfl_team_id", "report_primary_injury", "report_secondary_injury",
        "report_status", "practice_primary_injury",
        "practice_secondary_injury", "practice_status", "source_modified_at",
    }
    row = _record("player injury report", value, fields)
    return PlayerInjuryReport(
        row["gsis_id"], row["display_name"], row["position"], row["season"],
        row["week"], row["nfl_team_id"], row["report_primary_injury"],
        row["report_secondary_injury"], row["report_status"],
        row["practice_primary_injury"], row["practice_secondary_injury"],
        row["practice_status"],
        _optional_time("source_modified_at", row["source_modified_at"]),
    )


def _sleeper_player(value):
    fields = {
        "sleeper_player_id", "gsis_id", "espn_id", "display_name", "position",
        "fantasy_positions", "nfl_team_id", "active", "status",
        "injury_status", "injury_body_part", "practice_participation",
        "depth_chart_position", "depth_chart_order", "years_experience",
        "jersey_number", "news_updated_ms",
    }
    row = _record("Sleeper player", value, fields)
    return SleeperPlayerMetadata(
        row["sleeper_player_id"], row["gsis_id"], row["espn_id"],
        row["display_name"], row["position"],
        tuple(_array("fantasy_positions", row["fantasy_positions"])),
        row["nfl_team_id"], row["active"], row["status"],
        row["injury_status"], row["injury_body_part"],
        row["practice_participation"], row["depth_chart_position"],
        row["depth_chart_order"], row["years_experience"],
        row["jersey_number"], row["news_updated_ms"],
    )


def _trend(value):
    row = _record(
        "Sleeper trend", value, {"sleeper_player_id", "adds", "drops"}
    )
    return SleeperPlayerTrend(row["sleeper_player_id"], row["adds"], row["drops"])


def _crosswalk(value):
    row = _record(
        "player ID crosswalk", value, {"gsis_id", "espn_id", "sleeper_id"}
    )
    return PublicPlayerIdCrosswalk(
        row["gsis_id"], row["espn_id"], row["sleeper_id"]
    )


def _availability(value):
    try:
        return DataAvailability(value)
    except (TypeError, ValueError):
        raise ValueError("public data availability is invalid") from None


def _record(name, value, fields):
    row = _mapping(name, value)
    if set(row) != set(fields):
        raise ValueError(f"{name} has missing or unknown fields")
    return row


def _mapping(name, value):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _records(name, value):
    rows = _array(name, value)
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError(f"{name} must contain records")
    return rows


def _array(name, value):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _time(name, value):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be ISO-8601 text")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be ISO-8601 text") from None


def _optional_time(name, value):
    return None if value is None else _time(name, value)


def _reject_constant(value):
    raise ValueError(f"unsupported JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = ("snapshot_from_json", "snapshot_from_record")
