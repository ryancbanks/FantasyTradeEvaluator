"""Strict NFL game context parsed from ESPN's weekly pro-team schedule view."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from types import MappingProxyType

from ._scenario_random import SAFE_INTEGER, content_id


_REGULAR_SEASON_WEEKS = tuple(range(1, 19))
_TOP_LEVEL_FIELDS = frozenset({"display", "settings"})
_SETTINGS_FIELDS = frozenset(
    {
        "defaultDraftPosition",
        "draftLobbyMinimumLeagueCount",
        "gameNotificationSettings",
        "gated",
        "playerOwnershipSettings",
        "proTeams",
        "readOnly",
        "statIdToOverridePosition",
        "teamActivityEnabled",
        "typeNames",
    }
)
_ACTIVE_TEAM_FIELDS = frozenset(
    {
        "abbrev",
        "byeWeek",
        "id",
        "location",
        "name",
        "proGamesByScoringPeriod",
        "teamPlayersByPosition",
        "universeId",
    }
)
_FREE_AGENT_FIELDS = _ACTIVE_TEAM_FIELDS.difference({"teamPlayersByPosition"})
_GAME_FIELDS = frozenset(
    {
        "awayProTeamId",
        "date",
        "homeProTeamId",
        "id",
        "scoringPeriodId",
        "startTimeTBD",
        "statsOfficial",
        "validForLocking",
    }
)


class NflTeamWeekStatus(str, Enum):
    SCHEDULED = "scheduled"
    BYE = "bye"


@dataclass(frozen=True, slots=True)
class NflTeamWeek:
    """One NFL team's verified game context, or explicit bye, for one week."""

    nfl_team_id: str
    week: int
    status: NflTeamWeekStatus
    nfl_game_id: str | None = None
    opponent_team_id: str | None = None
    is_home: bool | None = None
    source_game_id: str | None = None
    kickoff_at: datetime | None = None

    def __post_init__(self) -> None:
        _text("nfl_team_id", self.nfl_team_id)
        _integer("week", self.week, minimum=1, maximum=25)
        if not isinstance(self.status, NflTeamWeekStatus):
            raise ValueError("status must be an NflTeamWeekStatus")
        if self.status is NflTeamWeekStatus.BYE:
            if any(
                value is not None
                for value in (
                    self.nfl_game_id,
                    self.opponent_team_id,
                    self.is_home,
                    self.source_game_id,
                    self.kickoff_at,
                )
            ):
                raise ValueError("a bye cannot carry NFL game context")
            return
        _text("nfl_game_id", self.nfl_game_id)
        _text("opponent_team_id", self.opponent_team_id)
        if self.opponent_team_id == self.nfl_team_id:
            raise ValueError("opponent_team_id cannot equal nfl_team_id")
        if not isinstance(self.is_home, bool):
            raise ValueError("is_home must be a boolean for a scheduled game")
        if self.source_game_id is not None:
            _text("source_game_id", self.source_game_id)
        if self.kickoff_at is not None:
            _aware_time("kickoff_at", self.kickoff_at)

    @property
    def is_bye(self) -> bool:
        return self.status is NflTeamWeekStatus.BYE


@dataclass(frozen=True, slots=True)
class NflSchedule:
    """Immutable provider-neutral team/week schedule evidence."""

    season: int
    captured_at: datetime
    source_provider: str
    team_weeks: tuple[NflTeamWeek, ...]
    schedule_id: str = field(init=False)
    _by_team_week: Mapping[tuple[str, int], NflTeamWeek] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        _integer("season", self.season, minimum=2012, maximum=9999)
        _aware_time("captured_at", self.captured_at)
        _text("source_provider", self.source_provider)
        rows = _typed_rows("team_weeks", self.team_weeks, NflTeamWeek)
        index: dict[tuple[str, int], NflTeamWeek] = {}
        game_rows: dict[tuple[int, str], list[NflTeamWeek]] = defaultdict(list)
        weeks_by_team: dict[str, set[int]] = defaultdict(set)
        for row in rows:
            key = row.nfl_team_id, row.week
            if key in index:
                raise ValueError("NFL schedule contains a duplicate team/week")
            index[key] = row
            weeks_by_team[row.nfl_team_id].add(row.week)
            if row.status is NflTeamWeekStatus.SCHEDULED:
                game_rows[(row.week, row.nfl_game_id)].append(row)
        week_sets = {tuple(sorted(value)) for value in weeks_by_team.values()}
        if len(week_sets) != 1:
            raise ValueError("NFL schedule must cover the same weeks for every team")
        teams = set(weeks_by_team)
        source_games = {}
        for game_key, appearances in game_rows.items():
            if len(appearances) != 2:
                raise ValueError("each scheduled NFL game must have two team appearances")
            first, second = appearances
            if (
                first.opponent_team_id not in teams
                or second.opponent_team_id not in teams
                or first.nfl_team_id != second.opponent_team_id
                or second.nfl_team_id != first.opponent_team_id
                or {first.is_home, second.is_home} != {True, False}
                or first.source_game_id != second.source_game_id
                or first.kickoff_at != second.kickoff_at
            ):
                raise ValueError("scheduled NFL game appearances conflict")
            if first.source_game_id is not None:
                previous = source_games.setdefault(first.source_game_id, game_key)
                if previous != game_key:
                    raise ValueError("one source game ID describes conflicting games")
        ordered = tuple(sorted(rows, key=lambda row: (row.nfl_team_id, row.week)))
        record = {
            "season": self.season,
            "captured_at": self.captured_at.isoformat(timespec="microseconds"),
            "source_provider": self.source_provider,
            "team_weeks": [_team_week_record(row) for row in ordered],
        }
        object.__setattr__(self, "team_weeks", ordered)
        object.__setattr__(self, "_by_team_week", MappingProxyType(index))
        object.__setattr__(self, "schedule_id", content_id("nfl-schedule", record))

    def team_week(self, nfl_team_id: str, week: int) -> NflTeamWeek:
        """Return verified context or fail if the schedule does not cover the cell."""

        _text("nfl_team_id", nfl_team_id)
        _integer("week", week, minimum=1, maximum=25)
        try:
            return self._by_team_week[(nfl_team_id, week)]
        except KeyError:
            raise ValueError(
                f"NFL schedule lacks team/week {(nfl_team_id, week)!r}"
            ) from None


@dataclass(frozen=True, slots=True)
class _EspnGame:
    source_game_id: int
    week: int
    away_team_id: int
    home_team_id: int
    kickoff_milliseconds: int
    start_time_tbd: bool
    stats_official: bool
    valid_for_locking: bool


def parse_espn_pro_team_schedule(
    payload: Mapping[str, object],
    *,
    season: int,
    captured_at: datetime,
) -> NflSchedule:
    """Parse the official ``proTeamSchedules_wl`` payload without guessing fields."""

    _integer("season", season, minimum=2012, maximum=9999)
    _aware_time("captured_at", captured_at)
    _fields("ESPN pro-team payload", payload, _TOP_LEVEL_FIELDS)
    if type(payload["display"]) is not bool:
        raise ValueError("ESPN pro-team display must be a boolean")
    settings = _mapping("ESPN pro-team settings", payload["settings"])
    _fields("ESPN pro-team settings", settings, _SETTINGS_FIELDS)
    raw_teams = settings["proTeams"]
    if not isinstance(raw_teams, list):
        raise ValueError("ESPN proTeams must be a list")

    provider_teams: dict[int, str] = {}
    raw_schedules: dict[int, Mapping[str, object]] = {}
    bye_weeks: dict[int, int] = {}
    saw_free_agent = False
    for raw_team in raw_teams:
        team = _mapping("ESPN pro team", raw_team)
        provider_id = _json_integer("ESPN pro-team id", team.get("id"), minimum=0)
        expected_fields = _FREE_AGENT_FIELDS if provider_id == 0 else _ACTIVE_TEAM_FIELDS
        _fields("ESPN pro team", team, expected_fields)
        abbreviation = _espn_team_id(team["abbrev"])
        schedule = _mapping("ESPN pro-team schedule", team["proGamesByScoringPeriod"])
        bye_week = _json_integer("ESPN pro-team byeWeek", team["byeWeek"], minimum=0)
        _json_integer("ESPN pro-team universeId", team["universeId"], minimum=0)
        if provider_id == 0:
            if saw_free_agent or abbreviation != "FA" or bye_week != 0 or schedule:
                raise ValueError("ESPN free-agent pro-team row is invalid")
            saw_free_agent = True
            continue
        if provider_id in provider_teams or abbreviation in provider_teams.values():
            raise ValueError("ESPN proTeams contains a duplicate team")
        if abbreviation == "FA":
            raise ValueError("an active ESPN pro team cannot use FA")
        _integer("ESPN pro-team byeWeek", bye_week, minimum=1, maximum=18)
        _text("ESPN pro-team location", team["location"])
        _text("ESPN pro-team name", team["name"])
        if not isinstance(team["teamPlayersByPosition"], Mapping):
            raise ValueError("ESPN teamPlayersByPosition must be a mapping")
        provider_teams[provider_id] = abbreviation
        raw_schedules[provider_id] = schedule
        bye_weeks[provider_id] = bye_week
    if not saw_free_agent or len(provider_teams) != 32 or len(raw_teams) != 33:
        raise ValueError("ESPN proTeams must contain 32 NFL teams and the FA row")

    appearances: dict[int, list[tuple[int, _EspnGame]]] = defaultdict(list)
    for provider_id, raw_schedule in raw_schedules.items():
        bye_week = bye_weeks[provider_id]
        expected_keys = {
            str(week) for week in _REGULAR_SEASON_WEEKS if week != bye_week
        }
        if set(raw_schedule) != expected_keys:
            raise ValueError("ESPN pro-team schedule does not match its explicit bye")
        for week_text, games in raw_schedule.items():
            if not isinstance(games, list) or len(games) != 1:
                raise ValueError("each ESPN team/week must contain exactly one game")
            week = int(week_text)
            game = _espn_game(games[0], expected_week=week)
            if provider_id not in (game.away_team_id, game.home_team_id):
                raise ValueError("ESPN pro-team schedule lists a game for the wrong team")
            appearances[game.source_game_id].append((provider_id, game))

    normalized_games: dict[int, _EspnGame] = {}
    matchups: set[tuple[int, frozenset[int]]] = set()
    for source_game_id, rows in appearances.items():
        games = {game for _, game in rows}
        owners = {owner for owner, _ in rows}
        if len(rows) != 2 or len(games) != 1:
            raise ValueError("each ESPN game must have two identical team appearances")
        game = next(iter(games))
        if owners != {game.away_team_id, game.home_team_id}:
            raise ValueError("ESPN game appearances do not match home and away teams")
        if game.away_team_id == game.home_team_id or not owners.issubset(provider_teams):
            raise ValueError("ESPN game references invalid NFL teams")
        matchup = game.week, frozenset(owners)
        if matchup in matchups:
            raise ValueError("ESPN schedule contains a duplicate weekly matchup")
        matchups.add(matchup)
        normalized_games[source_game_id] = game

    team_weeks = []
    for provider_id, nfl_team_id in provider_teams.items():
        by_week = {
            game.week: game
            for game in normalized_games.values()
            if provider_id in (game.away_team_id, game.home_team_id)
        }
        if len(by_week) != 17 or set(by_week) != set(_REGULAR_SEASON_WEEKS).difference(
            {bye_weeks[provider_id]}
        ):
            raise ValueError("ESPN game set does not prove one game or bye per team/week")
        for week in _REGULAR_SEASON_WEEKS:
            game = by_week.get(week)
            if game is None:
                team_weeks.append(
                    NflTeamWeek(nfl_team_id, week, NflTeamWeekStatus.BYE)
                )
                continue
            opponent_id = (
                game.away_team_id
                if provider_id == game.home_team_id
                else game.home_team_id
            )
            opponent = provider_teams[opponent_id]
            team_weeks.append(
                NflTeamWeek(
                    nfl_team_id,
                    week,
                    NflTeamWeekStatus.SCHEDULED,
                    canonical_nfl_game_id(season, week, nfl_team_id, opponent),
                    opponent,
                    provider_id == game.home_team_id,
                    str(game.source_game_id),
                    _kickoff(game.kickoff_milliseconds, season),
                )
            )
    return NflSchedule(
        season=season,
        captured_at=captured_at.astimezone(timezone.utc),
        source_provider="espn",
        team_weeks=tuple(team_weeks),
    )


def canonical_nfl_game_id(
    season: int,
    week: int,
    team: str,
    opponent: str,
) -> str:
    """Return the provider-independent game identity used by projection rows."""

    _integer("season", season, minimum=2012, maximum=9999)
    _integer("week", week, minimum=1, maximum=25)
    left, right = sorted((_canonical_team_id(team), _canonical_team_id(opponent)))
    if left == right:
        raise ValueError("an NFL game requires two different teams")
    return f"{season}-W{week:02d}-{left}-{right}"


def _espn_game(value: object, *, expected_week: int) -> _EspnGame:
    row = _mapping("ESPN pro game", value)
    _fields("ESPN pro game", row, _GAME_FIELDS)
    game = _EspnGame(
        _json_integer("ESPN game id", row["id"], minimum=1),
        _json_integer("ESPN game scoringPeriodId", row["scoringPeriodId"], minimum=1),
        _json_integer("ESPN awayProTeamId", row["awayProTeamId"], minimum=1),
        _json_integer("ESPN homeProTeamId", row["homeProTeamId"], minimum=1),
        _json_integer("ESPN game date", row["date"], minimum=1),
        _boolean("ESPN game startTimeTBD", row["startTimeTBD"]),
        _boolean("ESPN game statsOfficial", row["statsOfficial"]),
        _boolean("ESPN game validForLocking", row["validForLocking"]),
    )
    if game.week != expected_week or game.week not in _REGULAR_SEASON_WEEKS:
        raise ValueError("ESPN game scoring period conflicts with its schedule week")
    return game


def _kickoff(milliseconds: int, season: int) -> datetime:
    try:
        result = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("ESPN game date is outside the supported timestamp range") from None
    if result.year not in {season, season + 1}:
        raise ValueError("ESPN game date does not belong to the requested NFL season")
    return result


def _team_week_record(row: NflTeamWeek) -> dict[str, object]:
    return {
        "nfl_team_id": row.nfl_team_id,
        "week": row.week,
        "status": row.status.value,
        "nfl_game_id": row.nfl_game_id,
        "opponent_team_id": row.opponent_team_id,
        "is_home": row.is_home,
        "source_game_id": row.source_game_id,
        "kickoff_at": (
            row.kickoff_at.isoformat(timespec="microseconds")
            if row.kickoff_at is not None
            else None
        ),
    }


def _canonical_team_id(value: object) -> str:
    _text("NFL team ID", value)
    normalized = value.strip().upper()
    return {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"}.get(
        normalized, normalized
    )


def _espn_team_id(value: object) -> str:
    normalized = _canonical_team_id(value)
    if not re.fullmatch(r"[A-Z0-9]{2,4}", normalized):
        raise ValueError("ESPN pro-team abbreviation is invalid")
    return normalized


def _fields(name: str, value: object, expected: frozenset[str]) -> None:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise ValueError(f"{name} has missing or unknown fields")


def _mapping(name: str, value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be a string-keyed mapping")
    return value


def _json_integer(name: str, value: object, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= SAFE_INTEGER:
        raise ValueError(f"{name} must be a JSON-safe integer of at least {minimum}")
    return value


def _integer(name: str, value: object, *, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


def _boolean(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _aware_time(name: str, value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _typed_rows(name: str, values: Iterable[object], expected_type: type) -> tuple:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if not rows or any(not isinstance(row, expected_type) for row in rows):
        raise ValueError(f"{name} must contain {expected_type.__name__} values")
    return rows


__all__ = (
    "NflSchedule",
    "NflTeamWeek",
    "NflTeamWeekStatus",
    "canonical_nfl_game_id",
    "parse_espn_pro_team_schedule",
)
