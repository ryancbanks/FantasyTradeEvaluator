from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import math
from numbers import Real

from .roster_capacity import normalize_reserve_slot_counts


TeamId = str


class Tiebreaker(str, Enum):
    """Supported ordering rules for teams with the same record."""

    WIN_PERCENTAGE = "win_percentage"
    POINTS_FOR = "points_for"
    HEAD_TO_HEAD = "head_to_head"
    DIVISION_RECORD = "division_record"
    POINTS_AGAINST = "points_against"
    RANDOM_DRAW = "random_draw"


class HeadToHeadPolicy(str, Enum):
    """Explicit multi-team handling for head-to-head ranking.

    The balanced-group policy requires every pair in the tied group to have met
    the same positive number of times, ranks their combined head-to-head winning
    percentages, and then proceeds to the next rule without restarting the order.
    """

    BALANCED_GROUP_WIN_PERCENTAGE = "balanced_group_win_percentage"


@dataclass(frozen=True)
class LeagueTeam:
    team_id: TeamId
    name: str
    division_id: str | None = None

    def __post_init__(self) -> None:
        _require_id("team_id", self.team_id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("team name must be a non-empty string")
        if self.division_id is not None:
            _require_id("division_id", self.division_id)


@dataclass(frozen=True)
class TeamStanding:
    team_id: TeamId
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float

    def __post_init__(self) -> None:
        _require_id("standing team_id", self.team_id)
        _require_int("wins", self.wins, minimum=0)
        _require_int("losses", self.losses, minimum=0)
        _require_int("ties", self.ties, minimum=0)
        _require_finite_number("points_for", self.points_for)
        _require_finite_number("points_against", self.points_against)


@dataclass
class _CompletedRecord:
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: list[Real] = field(default_factory=list)
    points_against: list[Real] = field(default_factory=list)


@dataclass(frozen=True)
class FantasyMatchup:
    week: int
    team1_id: TeamId
    team2_id: TeamId
    team1_score_adjustment: float = 0.0

    def __post_init__(self) -> None:
        _require_int("matchup week", self.week, minimum=1)
        _require_id("matchup team1_id", self.team1_id)
        _require_id("matchup team2_id", self.team2_id)
        _require_finite_number(
            "matchup team1_score_adjustment", self.team1_score_adjustment
        )


@dataclass(frozen=True)
class CompletedFantasyMatchup:
    """One immutable final result from an elapsed fantasy-league week."""

    week: int
    team1_id: TeamId
    team2_id: TeamId
    team1_score: float
    team2_score: float

    def __post_init__(self) -> None:
        _require_int("completed matchup week", self.week, minimum=1)
        _require_id("completed matchup team1_id", self.team1_id)
        _require_id("completed matchup team2_id", self.team2_id)
        _require_finite_number("completed matchup team1_score", self.team1_score)
        _require_finite_number("completed matchup team2_score", self.team2_score)


@dataclass(frozen=True)
class RosterRules:
    """League-wide active, reserve, and ordered starting-lineup slots."""

    roster_cap: int
    starting_lineup_slots: tuple[str, ...]
    reserve_slot_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_int("roster_cap", self.roster_cap, minimum=1)
        slots = tuple(self.starting_lineup_slots)
        if not slots or any(not isinstance(slot, str) or not slot.strip() for slot in slots):
            raise ValueError("starting_lineup_slots must contain non-empty strings")
        if len(slots) > self.roster_cap:
            raise ValueError("starting lineup size cannot exceed roster_cap")
        object.__setattr__(self, "starting_lineup_slots", slots)
        object.__setattr__(
            self,
            "reserve_slot_counts",
            normalize_reserve_slot_counts(self.reserve_slot_counts),
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.roster_cap,
                self.starting_lineup_slots,
                tuple(self.reserve_slot_counts.items()),
            )
        )


@dataclass(frozen=True)
class PlayoffRules:
    qualifier_count: int
    regular_season_end_week: int
    playoff_weeks: tuple[int, ...]
    reseed_each_round: bool
    division_winner_qualifier_count: int
    tiebreaker_order: tuple[Tiebreaker, ...]
    head_to_head_policy: HeadToHeadPolicy | None = None

    def __post_init__(self) -> None:
        _require_int("qualifier_count", self.qualifier_count, minimum=1)
        _require_int("regular_season_end_week", self.regular_season_end_week, minimum=1)
        _require_int(
            "division_winner_qualifier_count",
            self.division_winner_qualifier_count,
            minimum=0,
        )
        if self.division_winner_qualifier_count > self.qualifier_count:
            raise ValueError("division winner berths cannot exceed qualifier_count")
        if not isinstance(self.reseed_each_round, bool):
            raise ValueError("reseed_each_round must be a boolean")

        weeks = tuple(self.playoff_weeks)
        if not weeks:
            raise ValueError("playoff_weeks cannot be empty")
        for week in weeks:
            _require_int("playoff week", week, minimum=self.regular_season_end_week + 1)
        if any(left >= right for left, right in zip(weeks, weeks[1:])):
            raise ValueError("playoff_weeks must be strictly increasing")

        tiebreakers = tuple(self.tiebreaker_order)
        if not tiebreakers or any(not isinstance(rule, Tiebreaker) for rule in tiebreakers):
            raise ValueError("tiebreaker_order must contain supported Tiebreaker values")
        if len(set(tiebreakers)) != len(tiebreakers):
            raise ValueError("tiebreaker_order cannot contain duplicates")
        if self.head_to_head_policy is not None and not isinstance(
            self.head_to_head_policy, HeadToHeadPolicy
        ):
            raise ValueError("head_to_head_policy must be a HeadToHeadPolicy")

        object.__setattr__(self, "playoff_weeks", weeks)
        object.__setattr__(self, "tiebreaker_order", tiebreakers)


@dataclass(frozen=True)
class LeagueState:
    """Validated inputs for future record and playoff projection."""

    snapshot_id: str
    season: int
    scoring_profile_id: str
    first_remaining_week: int
    teams: tuple[LeagueTeam, ...]
    standings: tuple[TeamStanding, ...]
    remaining_matchups: tuple[FantasyMatchup, ...]
    roster_rules: RosterRules
    playoff_rules: PlayoffRules
    completed_matchups: tuple[CompletedFantasyMatchup, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "teams", tuple(self.teams))
        object.__setattr__(self, "standings", tuple(self.standings))
        object.__setattr__(self, "remaining_matchups", tuple(self.remaining_matchups))
        object.__setattr__(self, "completed_matchups", tuple(self.completed_matchups))
        validate_league_state(self)

    @property
    def remaining_regular_season_weeks(self) -> tuple[int, ...]:
        return tuple(
            range(self.first_remaining_week, self.playoff_rules.regular_season_end_week + 1)
        )

    @property
    def completed_history_is_complete(self) -> bool:
        """Whether every team has one result in every elapsed league week."""

        expected = {
            (week, team.team_id)
            for week in range(1, self.first_remaining_week)
            for team in self.teams
        }
        actual = {
            (matchup.week, team_id)
            for matchup in self.completed_matchups
            for team_id in (matchup.team1_id, matchup.team2_id)
        }
        return actual == expected

    @property
    def completed_history_matches_standings(self) -> bool:
        """Whether completed results reproduce current standings totals."""

        records = _records_from_completed(self)
        return all(
            _completed_record_matches(standing, records[standing.team_id])
            for standing in self.standings
        )

    @property
    def completed_history_is_usable(self) -> bool:
        """Whether exact history-based tiebreak calculations are safe."""

        return (
            self.completed_history_is_complete
            and self.completed_history_matches_standings
        )


def validate_league_state(state: LeagueState) -> None:
    """Reject incomplete or contradictory schedule and standings inputs."""

    _require_nonempty_string("snapshot_id", state.snapshot_id)
    _require_int("season", state.season, minimum=2012)
    _require_nonempty_string("scoring_profile_id", state.scoring_profile_id)
    _require_int("first_remaining_week", state.first_remaining_week, minimum=1)
    final_week = state.playoff_rules.regular_season_end_week
    if state.first_remaining_week > final_week + 1:
        raise ValueError("first_remaining_week cannot be after the regular season")

    team_ids = _unique_ids("team_id", (team.team_id for team in state.teams))
    if len(team_ids) < 2:
        raise ValueError("league state must contain at least two teams")

    standing_ids = _unique_ids(
        "standing team_id", (standing.team_id for standing in state.standings)
    )
    if standing_ids != team_ids:
        raise ValueError("standings must contain exactly one row for every league team")

    if state.playoff_rules.qualifier_count > len(team_ids):
        raise ValueError("playoff qualifier_count cannot exceed the number of teams")
    _validate_divisions(state)
    _validate_schedule(state, team_ids)
    _validate_completed_matchups(state, team_ids)


def _validate_divisions(state: LeagueState) -> None:
    berth_count = state.playoff_rules.division_winner_qualifier_count
    if berth_count == 0:
        return
    division_ids = tuple(team.division_id for team in state.teams)
    if any(division_id is None for division_id in division_ids):
        raise ValueError("every team needs a division_id when division winners qualify")
    if berth_count > len(set(division_ids)):
        raise ValueError("division winner berths cannot exceed the number of divisions")


def _validate_schedule(state: LeagueState, team_ids: set[TeamId]) -> None:
    weeks = set(state.remaining_regular_season_weeks)
    appearances = {week: {team_id: 0 for team_id in team_ids} for week in weeks}
    seen_matchups: set[tuple[int, frozenset[TeamId]]] = set()

    for matchup in state.remaining_matchups:
        if matchup.week not in weeks:
            raise ValueError(
                f"matchup week {matchup.week} is outside the remaining regular season"
            )
        if matchup.team1_id == matchup.team2_id:
            raise ValueError(f"team cannot play a self matchup in week {matchup.week}")
        if matchup.team1_id not in team_ids or matchup.team2_id not in team_ids:
            raise ValueError(f"matchup in week {matchup.week} contains an unknown team")

        key = (matchup.week, frozenset((matchup.team1_id, matchup.team2_id)))
        if key in seen_matchups:
            raise ValueError(f"duplicate matchup for week {matchup.week}")
        seen_matchups.add(key)
        appearances[matchup.week][matchup.team1_id] += 1
        appearances[matchup.week][matchup.team2_id] += 1

    for week in sorted(weeks):
        if any(count != 1 for count in appearances[week].values()):
            raise ValueError(f"every league team must have exactly one matchup in week {week}")


def _validate_completed_matchups(state: LeagueState, team_ids: set[TeamId]) -> None:
    appearances: set[tuple[int, TeamId]] = set()
    seen_matchups: set[tuple[int, frozenset[TeamId]]] = set()
    for matchup in state.completed_matchups:
        if not isinstance(matchup, CompletedFantasyMatchup):
            raise ValueError("completed_matchups must contain CompletedFantasyMatchup values")
        if matchup.week >= state.first_remaining_week:
            raise ValueError("completed matchup week must be before first_remaining_week")
        if matchup.team1_id == matchup.team2_id:
            raise ValueError(f"team cannot play a self completed matchup in week {matchup.week}")
        if matchup.team1_id not in team_ids or matchup.team2_id not in team_ids:
            raise ValueError(f"completed matchup in week {matchup.week} contains an unknown team")
        key = (matchup.week, frozenset((matchup.team1_id, matchup.team2_id)))
        if key in seen_matchups:
            raise ValueError(f"duplicate completed matchup for week {matchup.week}")
        seen_matchups.add(key)
        for team_id in (matchup.team1_id, matchup.team2_id):
            appearance = (matchup.week, team_id)
            if appearance in appearances:
                raise ValueError(f"team appears twice in completed matchup week {matchup.week}")
            appearances.add(appearance)


def _records_from_completed(state: LeagueState) -> dict[TeamId, _CompletedRecord]:
    records = {team.team_id: _CompletedRecord() for team in state.teams}
    for matchup in state.completed_matchups:
        left = records[matchup.team1_id]
        right = records[matchup.team2_id]
        left.points_for.append(matchup.team1_score)
        left.points_against.append(matchup.team2_score)
        right.points_for.append(matchup.team2_score)
        right.points_against.append(matchup.team1_score)
        if matchup.team1_score > matchup.team2_score:
            left.wins += 1
            right.losses += 1
        elif matchup.team2_score > matchup.team1_score:
            right.wins += 1
            left.losses += 1
        else:
            left.ties += 1
            right.ties += 1
    return records


def _completed_record_matches(
    standing: TeamStanding, record: _CompletedRecord
) -> bool:
    try:
        points_for = math.fsum(record.points_for)
        points_against = math.fsum(record.points_against)
    except OverflowError:
        return False
    return (
        standing.wins == record.wins
        and standing.losses == record.losses
        and standing.ties == record.ties
        and math.isclose(standing.points_for, points_for, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(
            standing.points_against,
            points_against,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )


def _unique_ids(name: str, values) -> set[TeamId]:
    identifiers = tuple(values)
    unique = set(identifiers)
    if len(unique) != len(identifiers):
        raise ValueError(f"{name} values must be unique")
    return unique


def _require_id(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_nonempty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_int(name: str, value: object, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _require_finite_number(name: str, value: object) -> None:
    try:
        finite = math.isfinite(value)
    except (TypeError, OverflowError):
        finite = False
    if isinstance(value, bool) or not isinstance(value, Real) or not finite:
        raise ValueError(f"{name} must be a finite number")
