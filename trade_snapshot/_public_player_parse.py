"""Strict parsers for bulk public NFL player datasets.

This module is intentionally private.  The public immutable records live in
``public_player_data``; imports are delayed there so this parsing boundary can
construct those records without creating an import cycle at module load time.
"""

from collections.abc import Callable, Mapping
import csv
from datetime import datetime, timezone
import gzip
from io import BytesIO, StringIO
import json
from math import isfinite
import re

from ._public_player_http import (
    DownloadedPublicData,
    PublicPlayerDataCancelled,
    PublicPlayerDataError,
)
from .public_player_data import (
    PlayerInjuryReport,
    PlayerWeekStats,
    PublicPlayerIdCrosswalk,
    PublicPlayerDataLimits,
    SleeperPlayerMetadata,
    _NFL_TEAMS,
    _REQUIRED_STATS_HEADERS,
    _STAT_FIELDS,
    _TEAM_ALIASES,
)


_INJURY_HEADERS = frozenset(
    {
        "season",
        "game_type",
        "team",
        "week",
        "gsis_id",
        "position",
        "full_name",
        "first_name",
        "last_name",
        "report_primary_injury",
        "report_secondary_injury",
        "report_status",
        "practice_primary_injury",
        "practice_secondary_injury",
        "practice_status",
    }
)
_REPORT_STATUSES = {
    "out": "out",
    "doubtful": "doubtful",
    "questionable": "questionable",
    "probable": "probable",
    "note": "note",
}
_PRACTICE_STATUSES = {
    "did not participate in practice": "did_not_participate",
    "limited participation in practice": "limited",
    "full participation in practice": "full",
    "note": "note",
}
_LEGACY_NFL_TEAMS = frozenset({"OAK", "SD", "STL"})
_HISTORICAL_TEAM_ALIASES = {"OAK": "LV", "SD": "LAC", "STL": "LAR"}
_CROSSWALK_HEADERS = frozenset({"gsis_id", "espn_id", "sleeper_id"})


def parse_stats_csv(
    data: bytes,
    season: int,
    maximum: int,
    cancelled: Callable[[], bool],
) -> tuple[PlayerWeekStats, ...]:
    reader = _csv_reader(data, "nflverse player stats")
    headers = reader.fieldnames
    if (
        headers is None
        or len(set(headers)) != len(headers)
        or not _REQUIRED_STATS_HEADERS <= set(headers)
    ):
        raise PublicPlayerDataError("nflverse player-stat columns changed")
    rows = []
    for index, row in enumerate(_csv_rows(reader, "nflverse player stats"), start=1):
        _check_row(index, maximum, cancelled, "nflverse player stats")
        _check_width(row, "nflverse player-stat")
        row_season = _csv_int("season", row["season"], 1999, 2200)
        if row_season != season:
            raise PublicPlayerDataError("nflverse player stats contained the wrong season")
        if row["season_type"] != "REG":
            continue
        if isinstance(row["player_id"], str) and not row["player_id"].strip():
            if any(
                not isinstance(row[field], str) or row[field].strip()
                for field in ("player_display_name", "position")
            ):
                raise PublicPlayerDataError(
                    "nflverse player stats contained a partial player identity"
                )
            # nflverse retains a small number of anonymous team events (for
            # example, uncredited safeties).  They are real team statistics but
            # cannot be joined to a player profile without guessing an identity.
            continue
        stat_values = tuple(
            (name, value)
            for name in _STAT_FIELDS
            if (value := _csv_float(name, row[name], optional=True)) not in {None, 0.0}
        )
        try:
            rows.append(
                PlayerWeekStats(
                    _csv_text("player_id", row["player_id"], 64),
                    _csv_text("player_display_name", row["player_display_name"], 160),
                    _csv_text("position", row["position"], 16).upper(),
                    row_season,
                    _csv_int("week", row["week"], 1, 25),
                    _csv_text("game_id", row["game_id"], 64),
                    _normalized_historical_team(row["team"]),
                    _normalized_historical_team(row["opponent_team"]),
                    _optional_headshot(row["headshot_url"]),
                    _csv_float("fantasy_points", row["fantasy_points"], optional=True),
                    _csv_float(
                        "fantasy_points_ppr", row["fantasy_points_ppr"], optional=True
                    ),
                    stat_values,
                )
            )
        except ValueError as error:
            raise PublicPlayerDataError(
                f"invalid nflverse player-stat row: {error}"
            ) from None
    return tuple(rows)


def parse_injury_csv(
    data: bytes,
    season: int,
    maximum: int,
    cancelled: Callable[[], bool],
) -> tuple[PlayerInjuryReport, ...]:
    """Parse documented weekly reports, retaining the latest published update."""

    reader = _csv_reader(data, "nflverse injuries")
    headers = reader.fieldnames
    if (
        headers is None
        or len(set(headers)) != len(headers)
        or not _INJURY_HEADERS <= set(headers)
    ):
        raise PublicPlayerDataError("nflverse injury columns changed")
    has_modified = "date_modified" in headers
    latest: dict[tuple[str, int], PlayerInjuryReport] = {}
    for index, row in enumerate(_csv_rows(reader, "nflverse injuries"), start=1):
        _check_row(index, maximum, cancelled, "nflverse injuries")
        _check_width(row, "nflverse injury")
        row_season = _csv_int("season", row["season"], 1999, 2200)
        if row_season != season:
            raise PublicPlayerDataError("nflverse injuries contained the wrong season")
        if _normalized_text(row["game_type"], 16) != "REG":
            continue
        modified = _optional_iso_time(row.get("date_modified")) if has_modified else None
        try:
            report = PlayerInjuryReport(
                _csv_text("gsis_id", row["gsis_id"], 64),
                _injury_player_name(row),
                (
                    position.upper()
                    if (position := _optional_normalized_text(row["position"], 16))
                    else None
                ),
                row_season,
                _csv_int("week", row["week"], 1, 25),
                _normalized_historical_team(row["team"]),
                _optional_normalized_text(row["report_primary_injury"], 256),
                _optional_normalized_text(row["report_secondary_injury"], 256),
                _status(row["report_status"], _REPORT_STATUSES, "report_status"),
                _optional_normalized_text(row["practice_primary_injury"], 256),
                _optional_normalized_text(row["practice_secondary_injury"], 256),
                _status(row["practice_status"], _PRACTICE_STATUSES, "practice_status"),
                modified,
            )
        except ValueError as error:
            raise PublicPlayerDataError(f"invalid nflverse injury row: {error}") from None
        key = (report.gsis_id, report.week)
        prior = latest.get(key)
        if prior is None:
            latest[key] = report
        elif prior == report:
            continue
        elif prior.source_modified_at is None or report.source_modified_at is None:
            raise PublicPlayerDataError(
                "nflverse injuries repeated a player/week without update timestamps"
            )
        elif report.source_modified_at > prior.source_modified_at:
            latest[key] = report
        elif report.source_modified_at == prior.source_modified_at:
            raise PublicPlayerDataError(
                "nflverse injuries conflicted at the same update timestamp"
            )
    return tuple(latest.values())


def parse_sleeper_players(
    payload: DownloadedPublicData,
    limits: PublicPlayerDataLimits,
    cancelled: Callable[[], bool],
) -> tuple[SleeperPlayerMetadata, ...]:
    root = _json_object(payload.body, "Sleeper active players")
    if len(root) > limits.max_sleeper_players:
        raise PublicPlayerDataError("Sleeper active players exceeded the row limit")
    result = []
    for index, (source_id, value) in enumerate(root.items(), start=1):
        if index % 256 == 0:
            _raise_if_cancelled(cancelled)
        if not isinstance(value, Mapping):
            raise PublicPlayerDataError("Sleeper player row was not an object")
        try:
            player_id = _identifier(value.get("player_id"), "player_id")
            if source_id != player_id:
                raise ValueError("player_id did not match its map key")
            synthetic_defense = _source_team_or_none(source_id)
            is_defense = (
                synthetic_defense in _NFL_TEAMS
                and not _optional_normalized_text(value.get("full_name"), 160)
            )
            name = f"{synthetic_defense} D/ST" if is_defense else _player_name(value)
            raw_position = _optional_normalized_text(value.get("position"), 16)
            position = "DEF" if is_defense else (raw_position.upper() if raw_position else None)
            fantasy_positions = _string_tuple(value.get("fantasy_positions"), 8)
            if is_defense and not fantasy_positions:
                fantasy_positions = ("DEF",)
            team = _normalized_team_or_none(value.get("team"))
            team_abbr = _normalized_team_or_none(value.get("team_abbr"))
            if team is not None and team_abbr is not None and team != team_abbr:
                raise ValueError("team and team_abbr conflicted")
            if is_defense:
                team = synthetic_defense
            result.append(
                SleeperPlayerMetadata(
                    player_id,
                    _optional_identifier(value.get("gsis_id"), "gsis_id"),
                    _optional_identifier(value.get("espn_id"), "espn_id"),
                    name,
                    position,
                    fantasy_positions,
                    team or team_abbr,
                    _required_bool(value.get("active"), "active"),
                    _optional_normalized_text(value.get("status"), 256),
                    _optional_normalized_text(value.get("injury_status"), 256),
                    _optional_normalized_text(value.get("injury_body_part"), 256),
                    _optional_normalized_text(value.get("practice_participation"), 256),
                    _optional_normalized_text(value.get("depth_chart_position"), 256),
                    _optional_int(value.get("depth_chart_order"), "depth_chart_order", 100),
                    _plausible_years_experience(value.get("years_exp")),
                    _optional_int(value.get("number"), "number", 999),
                    _optional_int(value.get("news_updated"), "news_updated", 10**16),
                )
            )
        except ValueError as error:
            raise PublicPlayerDataError(f"invalid Sleeper player row: {error}") from None
    return tuple(result)


def parse_sleeper_trends(
    payload: DownloadedPublicData,
    limits: PublicPlayerDataLimits,
    cancelled: Callable[[], bool],
) -> dict[str, int]:
    root = _json_value(payload.body, "Sleeper trends")
    if not isinstance(root, list) or len(root) > limits.max_trend_rows:
        raise PublicPlayerDataError("Sleeper trends had an invalid row count")
    result = {}
    for index, row in enumerate(root, start=1):
        if index % 256 == 0:
            _raise_if_cancelled(cancelled)
        if not isinstance(row, Mapping) or not {"player_id", "count"} <= set(row):
            raise PublicPlayerDataError("Sleeper trend row changed shape")
        try:
            player_id = _identifier(row["player_id"], "player_id")
            count = _integerish("count", row["count"], 0, 10**12)
        except ValueError as error:
            raise PublicPlayerDataError(f"invalid Sleeper trend row: {error}") from None
        if player_id in result:
            raise PublicPlayerDataError("Sleeper trends repeated a player ID")
        result[player_id] = count
    return result


def parse_player_id_crosswalk(
    payload: DownloadedPublicData,
    limits: PublicPlayerDataLimits,
    cancelled: Callable[[], bool],
) -> tuple[PublicPlayerIdCrosswalk, ...]:
    """Retain only exact multi-provider ID links from the bounded CSV."""

    reader = _csv_reader(payload.body, "DynastyProcess player IDs")
    headers = reader.fieldnames
    if (
        headers is None
        or len(set(headers)) != len(headers)
        or not _CROSSWALK_HEADERS <= set(headers)
    ):
        raise PublicPlayerDataError("DynastyProcess player-ID columns changed")
    result = {}
    for index, row in enumerate(_csv_rows(reader, "DynastyProcess player IDs"), start=1):
        _check_row(
            index, limits.max_crosswalk_rows, cancelled, "DynastyProcess player IDs"
        )
        _check_width(row, "DynastyProcess player-ID")
        try:
            values = tuple(
                _crosswalk_id(row[name])
                for name in ("gsis_id", "espn_id", "sleeper_id")
            )
            if sum(value is not None for value in values) < 2:
                continue
            link = PublicPlayerIdCrosswalk(*values)
        except ValueError as error:
            raise PublicPlayerDataError(
                f"invalid DynastyProcess player-ID row: {error}"
            ) from None
        result.setdefault(link.key, link)
    return tuple(result.values())


def gunzip_limited(
    data: bytes,
    maximum: int,
    cancelled: Callable[[], bool],
    label: str,
) -> bytes:
    result = bytearray()
    try:
        with gzip.GzipFile(fileobj=BytesIO(data), mode="rb") as source:
            while True:
                _raise_if_cancelled(cancelled)
                chunk = source.read(min(64 * 1024, maximum - len(result) + 1))
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > maximum:
                    raise PublicPlayerDataError(f"{label} exceeded the decoded size limit")
    except PublicPlayerDataError:
        raise
    except (gzip.BadGzipFile, EOFError, OSError):
        raise PublicPlayerDataError(f"{label} was not valid gzip") from None
    return bytes(result)


def _csv_reader(data: bytes, label: str) -> csv.DictReader:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise PublicPlayerDataError(f"{label} was not valid UTF-8") from None
    reader = csv.DictReader(StringIO(text, newline=""), strict=True)
    try:
        reader.fieldnames
    except csv.Error:
        raise PublicPlayerDataError(f"{label} was not valid CSV") from None
    return reader


def _csv_rows(reader, label):
    while True:
        try:
            yield next(reader)
        except StopIteration:
            return
        except csv.Error:
            raise PublicPlayerDataError(f"{label} was not valid CSV") from None


def _check_row(index, maximum, cancelled, label):
    if index > maximum:
        raise PublicPlayerDataError(f"{label} exceeded the row limit")
    if index % 256 == 0:
        _raise_if_cancelled(cancelled)


def _check_width(row, label):
    if row.get(None) is not None:
        raise PublicPlayerDataError(f"{label} row width changed")


def _raise_if_cancelled(cancelled):
    if cancelled():
        raise PublicPlayerDataCancelled("public player-data collection was cancelled")


def _json_value(data, label):
    try:
        text = data.decode("utf-8", errors="strict")
        return json.loads(text, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise PublicPlayerDataError(f"{label} was not strict JSON") from None


def _json_object(data, label):
    value = _json_value(data, label)
    if not isinstance(value, dict):
        raise PublicPlayerDataError(f"{label} was not a JSON object")
    return value


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _player_name(value):
    full = _optional_normalized_text(value.get("full_name"), 160)
    if full:
        return full
    parts = tuple(
        part
        for key in ("first_name", "last_name")
        if (part := _optional_normalized_text(value.get(key), 80))
    )
    if not parts:
        raise ValueError("player name was missing")
    return " ".join(parts)


def _injury_player_name(row):
    full = _optional_normalized_text(row["full_name"], 160)
    if full:
        return full
    parts = tuple(
        part
        for key in ("first_name", "last_name")
        if (part := _optional_normalized_text(row[key], 80))
    )
    if not parts:
        raise ValueError("player name was missing")
    return " ".join(parts)


def _string_tuple(value, maximum_length):
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("fantasy_positions must be a bounded list")
    result = tuple(
        _normalized_text(item, maximum_length).upper() for item in value
    )
    if len(set(result)) != len(result):
        raise ValueError("fantasy_positions contains duplicates")
    return result


def _optional_identifier(value, name):
    if value is None or value == "":
        return None
    return _identifier(value, name)


def _crosswalk_id(value):
    if not isinstance(value, str):
        raise ValueError("crosswalk ID must be text")
    if not value.strip() or value.strip().upper() == "NA":
        return None
    return _normalized_text(value, 64)


def _identifier(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a string or integer ID")
    return _normalized_text(str(value), 64)


def _optional_int(value, name, maximum):
    if value is None or value == "":
        return None
    return _integerish(name, value, 0, maximum)


def _plausible_years_experience(value):
    result = _optional_int(value, "years_exp", 200)
    # Sleeper currently publishes one known value of 122.  It is syntactically
    # valid source data but not credible NFL experience, so expose it as unknown
    # instead of poisoning or aborting the otherwise useful bulk snapshot.
    return result if result is None or result <= 50 else None


def _required_bool(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _integerish(name, value, minimum, maximum):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _csv_int(name, value, minimum, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise PublicPlayerDataError(f"nflverse {name} was not an integer")
    return _integerish(name, value, minimum, maximum)


def _csv_float(name, value, *, optional):
    if value == "" and optional:
        return None
    if not isinstance(value, str):
        raise PublicPlayerDataError(f"nflverse {name} was not numeric")
    try:
        number = float(value)
    except ValueError:
        raise PublicPlayerDataError(f"nflverse {name} was not numeric") from None
    if not isfinite(number):
        raise PublicPlayerDataError(f"nflverse {name} was not finite")
    return number


def _csv_text(name, value, maximum):
    try:
        return _normalized_text(value, maximum)
    except ValueError as error:
        raise PublicPlayerDataError(f"invalid nflverse {name}: {error}") from None


def _normalized_historical_team(value):
    if not isinstance(value, str):
        raise PublicPlayerDataError("nflverse team was missing")
    normalized = value.strip().upper()
    normalized = _HISTORICAL_TEAM_ALIASES.get(normalized, normalized)
    try:
        result = _normalized_team_or_none(normalized)
    except ValueError as error:
        raise PublicPlayerDataError(f"invalid nflverse team: {error}") from None
    if result is None:
        raise PublicPlayerDataError("nflverse team was missing")
    return result


def _normalized_team_or_none(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("NFL team must be text or null")
    result = _TEAM_ALIASES.get(value.strip().upper(), value.strip().upper())
    if result in _LEGACY_NFL_TEAMS:
        return None
    if result not in _NFL_TEAMS:
        raise ValueError("NFL team was unsupported")
    return result


def _source_team_or_none(value):
    if not isinstance(value, str):
        return None
    result = _TEAM_ALIASES.get(value.strip().upper(), value.strip().upper())
    return result if result in _NFL_TEAMS else None


def _optional_headshot(value):
    if not isinstance(value, str) or not value.strip():
        return None
    from .public_player_data import _headshot_url

    try:
        _headshot_url(value)
    except ValueError:
        return None
    return value


def _status(value, choices, name):
    text = _optional_normalized_text(value, 80)
    if text is None:
        return None
    result = choices.get(text.casefold())
    if result is None:
        raise ValueError(f"{name} contained an unsupported value")
    return result


def _optional_normalized_text(value, maximum):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _normalized_text(value, maximum)


def _normalized_text(value, maximum):
    if not isinstance(value, str):
        raise ValueError("text value must be a string")
    result = " ".join(value.split())
    if not result or len(result) > maximum:
        raise ValueError(f"text must contain 1 through {maximum} characters")
    return result


def _optional_iso_time(value):
    text = _optional_normalized_text(value, 80)
    if text is None:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("date_modified was not ISO-8601") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("date_modified lacked a timezone")
    return result.astimezone(timezone.utc)


__all__ = (
    "gunzip_limited",
    "parse_injury_csv",
    "parse_player_id_crosswalk",
    "parse_sleeper_players",
    "parse_sleeper_trends",
    "parse_stats_csv",
)
