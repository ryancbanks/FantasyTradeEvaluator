"""Strict adapters for the public Draft Lab starter-corpus sources.

The adapters deliberately expose only typed, season-scoped facts.  They do not
perform identity guessing: the builder owns the one exact normalized join
between Fantasy Football Calculator ADP rows and nflverse roster rows.
"""

import csv
import gzip
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

STARTER_CORPUS_YEARS = (2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024, 2025)
STARTER_TRANSFORM_VERSION = 4
FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})
_TEAM_ALIASES = {
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "SL": "LAR",
    "STL": "LAR",
    "WSH": "WAS",
}
_POSITION_ALIASES = {"DEF": "DST", "D/ST": "DST", "PK": "K", "FB": "RB"}
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})
_STAT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_FFC_BYTES = 2 * 1024 * 1024
_MAX_FFC_PLAYERS = 5_000
_MAX_SCHEDULE_ROWS = 20_000
_MAX_ROSTER_ROWS = 500_000
_MAX_PLAYER_STAT_ROWS = 250_000
_MAX_TEAM_STAT_ROWS = 20_000
_MAX_DECOMPRESSED_BYTES = 192 * 1024 * 1024
# Buffalo-Cincinnati in Week 17 of 2022 was cancelled. Both clubs therefore
# have two schedule gaps; these are their independently scheduled bye weeks.
_BYE_WEEK_EXCEPTIONS = {(2022, "BUF"): 7, (2022, "CIN"): 10}


@dataclass(frozen=True, slots=True)
class FfcAdpPlayer:
    source_player_id: str
    display_name: str
    position: str
    team: str | None
    bye_week: int | None
    adp: float
    adp_standard_deviation: float | None
    best_rank: float | None
    worst_rank: float | None
    position_rank: int


@dataclass(frozen=True, slots=True)
class FfcAdpSnapshot:
    season: int
    source_start: str
    source_end: str
    source_as_of: str
    players: tuple[FfcAdpPlayer, ...]


@dataclass(frozen=True, slots=True)
class ScheduleSeason:
    season: int
    kickoff_at: str
    available_weeks: tuple[int, ...]
    bye_by_team: Mapping[str, int]
    points_allowed: Mapping[tuple[str, int], float]


@dataclass(frozen=True, slots=True)
class RosterPlayer:
    player_id: str
    display_name: str
    position: str
    team: str
    nfl_experience_years: int
    rookie: bool


@dataclass(frozen=True, slots=True)
class RosterSnapshot:
    players: tuple[RosterPlayer, ...]
    rejected_row_count: int
    excluded_status_counts: Mapping[str, int]


def canonical_team(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("NFL team must be non-empty text")
    raw = value.strip().upper()
    normalized = _TEAM_ALIASES.get(raw, raw)
    if not re.fullmatch(r"[A-Z]{2,3}", normalized):
        raise ValueError("NFL team is invalid")
    return normalized


def canonical_position(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("player position must be non-empty text")
    raw = value.strip().upper().replace(" ", "")
    return _POSITION_ALIASES.get(raw, raw)


def normalized_player_name(value: object) -> str:
    """Return a deterministic exact-match key, never a fuzzy similarity key."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("player name must be non-empty text")
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    tokens = re.findall(r"[a-z0-9]+", ascii_name.lower())
    while tokens and tokens[-1] in _NAME_SUFFIXES:
        tokens.pop()
    if not tokens:
        raise ValueError("player name has no portable characters")
    return "".join(tokens)


def load_ffc_adp(path: str | Path, season: int, kickoff_at: str) -> FfcAdpSnapshot:
    source = Path(path)
    if source.stat().st_size > _MAX_FFC_BYTES:
        raise ValueError("Fantasy Football Calculator ADP exceeds its size limit")
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not read Fantasy Football Calculator ADP: {error}"
        ) from None
    if not isinstance(payload, Mapping) or set(payload) != {
        "status",
        "meta",
        "players",
    }:
        raise ValueError("Fantasy Football Calculator response fields are invalid")
    if payload["status"] != "Success" or not isinstance(payload["meta"], Mapping):
        raise ValueError(
            "Fantasy Football Calculator did not return a successful snapshot"
        )
    meta = payload["meta"]
    required_meta = {
        "type",
        "teams",
        "rounds",
        "total_drafts",
        "start_date",
        "end_date",
    }
    if not required_meta.issubset(meta) or meta["type"] != "PPR" or meta["teams"] != 12:
        raise ValueError("Fantasy Football Calculator metadata is incompatible")
    source_start = _season_date("FFC start_date", meta["start_date"], season)
    source_end = _season_date("FFC end_date", meta["end_date"], season)
    if source_start > source_end:
        raise ValueError("Fantasy Football Calculator date window is reversed")
    source_as_of = f"{source_end.isoformat()}T23:59:59+00:00"
    if _timestamp(source_as_of) >= _timestamp(kickoff_at):
        raise ValueError("Fantasy Football Calculator ADP window is not preseason")
    raw_players = payload["players"]
    if (
        not isinstance(raw_players, list)
        or not 1 <= len(raw_players) <= _MAX_FFC_PLAYERS
    ):
        raise ValueError("Fantasy Football Calculator player array is invalid")
    parsed = []
    seen_ids = set()
    position_counts: defaultdict[str, int] = defaultdict(int)
    for raw in sorted(raw_players, key=_ffc_sort_key):
        if not isinstance(raw, Mapping):
            raise ValueError("Fantasy Football Calculator players must be objects")
        required = {
            "player_id",
            "name",
            "position",
            "team",
            "adp",
            "stdev",
            "high",
            "low",
            "bye",
        }
        if not required.issubset(raw):
            raise ValueError("Fantasy Football Calculator player fields are incomplete")
        source_id = _identifier("FFC player_id", raw["player_id"])
        if source_id in seen_ids:
            raise ValueError(
                "Fantasy Football Calculator contains duplicate player IDs"
            )
        seen_ids.add(source_id)
        position = canonical_position(raw["position"])
        if position not in FANTASY_POSITIONS | {"DST"}:
            continue
        team = None if raw["team"] is None else canonical_team(raw["team"])
        bye = _optional_integer("FFC bye", raw["bye"], 1, 25)
        adp = _number("FFC adp", raw["adp"], minimum=0, strictly_greater=True)
        deviation = _optional_number("FFC stdev", raw["stdev"], minimum=0)
        best = _optional_number(
            "FFC high", raw["high"], minimum=0, strictly_greater=True
        )
        worst = _optional_number(
            "FFC low", raw["low"], minimum=0, strictly_greater=True
        )
        position_counts[position] += 1
        parsed.append(
            FfcAdpPlayer(
                source_id,
                _text("FFC name", raw["name"]),
                position,
                team,
                bye,
                adp,
                deviation,
                best,
                worst,
                position_counts[position],
            )
        )
    if not parsed:
        raise ValueError("Fantasy Football Calculator has no supported players")
    return FfcAdpSnapshot(
        season,
        source_start.isoformat(),
        source_end.isoformat(),
        source_as_of,
        tuple(parsed),
    )


def load_schedules(
    path: str | Path, seasons: Iterable[int]
) -> dict[int, ScheduleSeason]:
    selected = frozenset(seasons)
    games: defaultdict[int, list[dict[str, object]]] = defaultdict(list)
    required = {
        "season",
        "game_type",
        "week",
        "gameday",
        "away_team",
        "home_team",
        "away_score",
        "home_score",
    }
    for row in _csv_rows(path, required, _MAX_SCHEDULE_ROWS):
        season = _csv_integer("schedule season", row["season"], 1900, 9999)
        if season not in selected or row["game_type"] != "REG":
            continue
        week = _csv_integer("schedule week", row["week"], 1, 25)
        day = _schedule_date("schedule gameday", row["gameday"], season)
        away = canonical_team(row["away_team"])
        home = canonical_team(row["home_team"])
        if away == home:
            raise ValueError("schedule game has the same home and away team")
        games[season].append(
            {
                "week": week,
                "day": day,
                "away": away,
                "home": home,
                "away_score": _csv_number("away_score", row["away_score"], minimum=0),
                "home_score": _csv_number("home_score", row["home_score"], minimum=0),
            }
        )
    result = {}
    for season in sorted(selected):
        rows = games.get(season, [])
        if not rows:
            raise ValueError(
                f"nflverse schedule has no regular-season games for {season}"
            )
        maximum_week = max(int(row["week"]) for row in rows)
        weeks = tuple(range(1, maximum_week + 1))
        teams = {str(row[key]) for row in rows for key in ("away", "home")}
        games_by_team: defaultdict[str, set[int]] = defaultdict(set)
        points_allowed = {}
        seen_games = set()
        for row in rows:
            week = int(row["week"])
            away, home = str(row["away"]), str(row["home"])
            game_key = week, away, home
            if game_key in seen_games:
                raise ValueError(f"nflverse schedule has a duplicate {season} game")
            seen_games.add(game_key)
            for team in (away, home):
                if week in games_by_team[team]:
                    raise ValueError(
                        f"nflverse schedule has two {team} games in week {week}"
                    )
                games_by_team[team].add(week)
            points_allowed[away, week] = float(row["home_score"])
            points_allowed[home, week] = float(row["away_score"])
        bye_by_team = {}
        expected = set(weeks)
        for team in teams:
            missing = sorted(expected.difference(games_by_team[team]))
            exception = _BYE_WEEK_EXCEPTIONS.get((season, team))
            if len(missing) == 1:
                bye_by_team[team] = missing[0]
            elif exception in missing:
                bye_by_team[team] = exception
            else:
                raise ValueError(
                    f"nflverse schedule cannot determine one bye for {team} in {season}"
                )
        kickoff_day = min(row["day"] for row in rows)
        kickoff = datetime.combine(
            kickoff_day, time(12), tzinfo=timezone.utc
        ).isoformat()
        result[season] = ScheduleSeason(
            season,
            kickoff,
            weeks,
            dict(sorted(bye_by_team.items())),
            dict(sorted(points_allowed.items())),
        )
    return result


def load_week_one_roster(path: str | Path, season: int) -> RosterSnapshot:
    required = {
        "season",
        "week",
        "game_type",
        "team",
        "position",
        "full_name",
        "gsis_id",
        "years_exp",
        "entry_year",
        "rookie_year",
        "status",
    }
    players: dict[str, RosterPlayer] = {}
    ambiguous = set()
    rejected = 0
    excluded_statuses = Counter()
    for row in _csv_rows(path, required, _MAX_ROSTER_ROWS):
        if (
            _csv_integer("roster season", row["season"], 1900, 9999) != season
            or row["game_type"] != "REG"
            or _csv_integer("roster week", row["week"], 1, 25) != 1
        ):
            continue
        if row["status"] in {"CUT", "DEV", "RET"}:
            excluded_statuses[row["status"]] += 1
            continue
        try:
            position = canonical_position(row["position"])
            if position not in FANTASY_POSITIONS:
                continue
            player_id = _text("roster gsis_id", row["gsis_id"])
            player = RosterPlayer(
                player_id,
                _text("roster full_name", row["full_name"]),
                position,
                canonical_team(row["team"]),
                _experience(row, season),
                _experience(row, season) == 0,
            )
        except ValueError:
            rejected += 1
            continue
        if player_id in ambiguous:
            continue
        previous = players.get(player_id)
        if previous is None:
            players[player_id] = player
        elif previous != player:
            rejected += 1
            del players[player_id]
            ambiguous.add(player_id)
    if not players:
        raise ValueError(
            f"nflverse Week 1 roster has no supported players for {season}"
        )
    return RosterSnapshot(
        tuple(sorted(players.values(), key=lambda row: row.player_id)),
        rejected,
        dict(sorted(excluded_statuses.items())),
    )


def load_previous_roster_teams(path: str | Path, season: int) -> Mapping[str, str]:
    required = {"season", "week", "game_type", "team", "gsis_id"}
    latest: dict[str, tuple[int, str]] = {}
    ambiguous = set()
    for row in _csv_rows(path, required, _MAX_ROSTER_ROWS):
        if _csv_integer("roster season", row["season"], 1900, 9999) != season:
            continue
        if row["game_type"] != "REG" or not row["gsis_id"].strip():
            continue
        week = _csv_integer("roster week", row["week"], 1, 25)
        player_id = row["gsis_id"].strip()
        team = canonical_team(row["team"])
        previous = latest.get(player_id)
        if previous is None or week > previous[0]:
            latest[player_id] = week, team
            ambiguous.discard(player_id)
        elif week == previous[0] and team != previous[1]:
            ambiguous.add(player_id)
    return {
        player_id: team
        for player_id, (_, team) in sorted(latest.items())
        if player_id not in ambiguous
    }


def load_player_week_stats(
    path: str | Path, season: int
) -> Mapping[str, Mapping[int, Mapping[str, float]]]:
    required = {"player_id", "season", "week", "season_type"}
    totals: defaultdict[tuple[str, int], defaultdict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in _csv_rows(path, required, _MAX_PLAYER_STAT_ROWS):
        if (
            _csv_integer("player stats season", row["season"], 1900, 9999) != season
            or row["season_type"] != "REG"
        ):
            continue
        # nflverse includes a small number of aggregate/unidentified rows. They
        # cannot be joined safely and are never assigned to a player.
        if not row["player_id"].strip():
            continue
        player_id = _text("player stats player_id", row["player_id"])
        week = _csv_integer("player stats week", row["week"], 1, 25)
        stats = _numeric_stats(row, required | _PLAYER_METADATA)
        _add_alias(stats, "interceptions", stats.get("passing_interceptions", 0.0))
        _add_alias(stats, "fumbles_lost", stats.get("fumbles_lost_total", 0.0))
        _add_alias(stats, "field_goals", stats.get("fg_made", 0.0))
        _add_alias(stats, "extra_points", stats.get("pat_made", 0.0))
        if not stats:
            stats["fantasy_points"] = 0.0
        for name, value in stats.items():
            totals[player_id, week][name].append(value)
    result: defaultdict[str, dict[int, Mapping[str, float]]] = defaultdict(dict)
    for (player_id, week), stats in sorted(totals.items()):
        result[player_id][week] = {
            name: math.fsum(values) for name, values in sorted(stats.items())
        }
    return {player_id: dict(weeks) for player_id, weeks in sorted(result.items())}


def load_team_week_stats(
    path: str | Path, season: int
) -> Mapping[tuple[str, int], Mapping[str, float]]:
    required = {"season", "week", "team", "season_type"}
    result = {}
    for row in _csv_rows(path, required, _MAX_TEAM_STAT_ROWS):
        if (
            _csv_integer("team stats season", row["season"], 1900, 9999) != season
            or row["season_type"] != "REG"
        ):
            continue
        team = canonical_team(row["team"])
        week = _csv_integer("team stats week", row["week"], 1, 25)
        key = team, week
        if key in result:
            raise ValueError(f"nflverse team stats duplicate {team} week {week}")
        stats = _numeric_stats(row, required | _TEAM_METADATA)
        result[key] = stats
    if not result:
        raise ValueError(f"nflverse team stats has no regular-season rows for {season}")
    return dict(sorted(result.items()))


_PLAYER_METADATA = {
    "player_name",
    "player_display_name",
    "position",
    "position_group",
    "headshot_url",
    "game_id",
    "team",
    "opponent_team",
    "fg_made_list",
    "fg_missed_list",
    "fg_blocked_list",
}
_TEAM_METADATA = {
    "game_id",
    "opponent_team",
    "fg_made_list",
    "fg_missed_list",
    "fg_blocked_list",
}


def _csv_rows(path: str | Path, required: set[str], maximum_rows: int):
    source = Path(path)
    binary = source.open("rb")
    text = None
    try:
        stream = (
            gzip.GzipFile(fileobj=binary) if source.name.endswith(".gz") else binary
        )
        text = _BoundedTextLines(stream, _MAX_DECOMPRESSED_BYTES)
        reader = csv.DictReader(text)
        if reader.fieldnames is None or len(reader.fieldnames) > 256:
            raise ValueError(f"{source.name} CSV header is invalid")
        if len(set(reader.fieldnames)) != len(
            reader.fieldnames
        ) or not required.issubset(reader.fieldnames):
            raise ValueError(f"{source.name} CSV fields are incompatible")
        for count, row in enumerate(reader, 1):
            if count > maximum_rows:
                raise ValueError(f"{source.name} exceeds its row limit")
            if None in row:
                raise ValueError(f"{source.name} contains an over-wide row")
            yield row
    except (OSError, UnicodeError, csv.Error, gzip.BadGzipFile) as error:
        raise ValueError(f"could not read {source.name}: {error}") from None
    finally:
        if text is not None:
            text.close()
        binary.close()


class _BoundedTextLines:
    def __init__(self, binary, maximum_bytes: int):
        import io

        self._text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
        self._maximum = maximum_bytes
        self._read = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self._text)
        self._read += len(line.encode("utf-8"))
        if self._read > self._maximum:
            raise ValueError("decompressed CSV exceeds its size limit")
        return line

    def close(self):
        self._text.close()


def _numeric_stats(row: Mapping[str, str], excluded: set[str]) -> dict[str, float]:
    stats = {}
    for name, raw in row.items():
        if name in excluded or not _STAT_NAME.fullmatch(name) or raw == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if not math.isfinite(value) or abs(value) > 1e12:
            raise ValueError(f"actual stat {name!r} is not a bounded finite number")
        if value != 0:
            stats[name] = value
    return stats


def _add_alias(stats: dict[str, float], name: str, value: float) -> None:
    # Canonical scoring aliases are present even when zero, proving coverage for
    # the built-in portable scoring preset without manufacturing points.
    stats[name] = float(value)


def _experience(row: Mapping[str, str], season: int) -> int:
    raw = row["years_exp"].strip()
    if raw:
        return _csv_integer("roster years_exp", raw, 0, 40)
    for name in ("rookie_year", "entry_year"):
        raw_year = row[name].strip()
        if raw_year:
            year = _csv_integer(f"roster {name}", raw_year, 1900, season)
            return min(40, season - year)
    raise ValueError("roster experience metadata is missing")


def _ffc_sort_key(row: object):
    if not isinstance(row, Mapping):
        return math.inf, ""
    adp = row.get("adp")
    return (
        float(adp)
        if isinstance(adp, (int, float)) and not isinstance(adp, bool)
        else math.inf,
        str(row.get("player_id", "")),
    )


def _season_date(name: str, value: object, season: int) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date") from None
    if parsed.year != season:
        raise ValueError(f"{name} must be in season {season}")
    return parsed


def _schedule_date(name: str, value: object, season: int) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date") from None
    if parsed.year not in {season, season + 1}:
        raise ValueError(f"{name} is outside NFL season {season}")
    return parsed


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _identifier(name: str, value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} is invalid")
    return _text(name, str(value))


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2_048:
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _number(
    name: str, value: object, *, minimum: float | None = None, strictly_greater=False
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or abs(result) > 1e12:
        raise ValueError(f"{name} must be a bounded finite number")
    if minimum is not None and (
        result <= minimum if strictly_greater else result < minimum
    ):
        raise ValueError(f"{name} is below its minimum")
    return result


def _optional_number(name: str, value: object, **kwargs) -> float | None:
    return None if value is None else _number(name, value, **kwargs)


def _optional_integer(
    name: str, value: object, minimum: int, maximum: int
) -> int | None:
    # FFC uses zero, as well as JSON null in older snapshots, for no known bye.
    if value is None or (type(value) is int and value == 0):
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in range")
    return value


def _csv_integer(name: str, value: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if str(parsed) != value.strip() or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is out of range")
    return parsed


def _csv_number(name: str, value: str, *, minimum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if (
        not math.isfinite(parsed)
        or abs(parsed) > 1e12
        or (minimum is not None and parsed < minimum)
    ):
        raise ValueError(f"{name} is invalid")
    return parsed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid JSON constant {value!r}")


__all__ = (
    "FANTASY_POSITIONS",
    "STARTER_CORPUS_YEARS",
    "STARTER_TRANSFORM_VERSION",
    "FfcAdpPlayer",
    "FfcAdpSnapshot",
    "RosterPlayer",
    "RosterSnapshot",
    "ScheduleSeason",
    "canonical_position",
    "canonical_team",
    "load_ffc_adp",
    "load_player_week_stats",
    "load_previous_roster_teams",
    "load_schedules",
    "load_team_week_stats",
    "load_week_one_roster",
    "normalized_player_name",
)
