"""Strict provider-neutral evidence for one host fantasy league."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from numbers import Real

from ._league_source_validation import validate_host_league_snapshot
from .league_state import PlayoffRules, RosterRules
from .positions import (
    CANONICAL_PLAYER_POSITIONS,
    normalize_lineup_slot,
    normalize_player_position,
)
from .roster_capacity import normalize_reserve_slot_by_player
from .scoring import ScoringProfile


_ALLOWED_LINEUP_SLOTS = CANONICAL_PLAYER_POSITIONS | {
    "FLEX",
    "RB_WR",
    "WR_TE",
    "SFLX",
    "OP",
    "UTIL",
}


@dataclass(frozen=True, slots=True)
class ProviderPlayerId:
    provider: str
    player_id: str

    def __post_init__(self) -> None:
        _text("provider", self.provider)
        _text("player_id", self.player_id)


@dataclass(frozen=True, slots=True)
class ProviderTeamId:
    provider: str
    team_id: str

    def __post_init__(self) -> None:
        _text("provider", self.provider)
        _text("team_id", self.team_id)


@dataclass(frozen=True, slots=True)
class SourceLeagueTeam:
    source_team_id: str
    name: str
    provider_ids: tuple[ProviderTeamId, ...]
    division_id: str | None = None

    def __post_init__(self) -> None:
        _text("source_team_id", self.source_team_id)
        _text("team name", self.name)
        references = _typed_tuple("provider_ids", self.provider_ids, ProviderTeamId)
        _one_id_per_provider("team", references)
        if self.division_id is not None:
            _text("division_id", self.division_id)
        object.__setattr__(self, "provider_ids", references)


@dataclass(frozen=True, slots=True)
class SourceLeaguePlayer:
    source_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    eligible_slots: tuple[str, ...]
    provider_ids: tuple[ProviderPlayerId, ...]

    def __post_init__(self) -> None:
        _text("source_player_id", self.source_player_id)
        _text("display_name", self.display_name)
        _text("nfl_team_id", self.nfl_team_id)
        position = normalize_player_position(self.position, require_supported=True)
        slots = _normalized_slots(self.eligible_slots)
        references = _typed_tuple("provider_ids", self.provider_ids, ProviderPlayerId)
        _one_id_per_provider("player", references)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "eligible_slots", slots)
        object.__setattr__(self, "provider_ids", references)


@dataclass(frozen=True, slots=True)
class SourceTeamRoster:
    source_team_id: str
    source_player_ids: tuple[str, ...]
    reserve_slot_by_player: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text("source_team_id", self.source_team_id)
        try:
            player_ids = tuple(self.source_player_ids)
        except TypeError:
            raise ValueError("source_player_ids must be an iterable") from None
        if not player_ids or any(not _is_text(value) for value in player_ids):
            raise ValueError("source_player_ids must contain non-empty strings")
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("source roster contains a duplicate player ID")
        reserve_slots = normalize_reserve_slot_by_player(
            self.reserve_slot_by_player,
            owned_player_ids=player_ids,
        )
        object.__setattr__(self, "source_player_ids", player_ids)
        object.__setattr__(self, "reserve_slot_by_player", reserve_slots)

    @property
    def capacity_exempt_source_player_ids(self) -> frozenset[str]:
        """Legacy read view of players occupying any typed reserve slot."""

        return frozenset(self.reserve_slot_by_player)

    def __hash__(self) -> int:
        return hash(
            (
                self.source_team_id,
                self.source_player_ids,
                tuple(self.reserve_slot_by_player.items()),
            )
        )


@dataclass(frozen=True, slots=True)
class SourceTeamStanding:
    source_team_id: str
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float

    def __post_init__(self) -> None:
        _text("source_team_id", self.source_team_id)
        for name in ("wins", "losses", "ties"):
            _integer(name, getattr(self, name), minimum=0)
        _number("points_for", self.points_for)
        _number("points_against", self.points_against)


@dataclass(frozen=True, slots=True)
class SourceMatchup:
    week: int
    source_team1_id: str
    source_team2_id: str
    team1_score_adjustment: float = 0.0

    def __post_init__(self) -> None:
        _integer("week", self.week, minimum=1)
        _text("source_team1_id", self.source_team1_id)
        _text("source_team2_id", self.source_team2_id)
        _number("team1_score_adjustment", self.team1_score_adjustment)


@dataclass(frozen=True, slots=True)
class SourceCompletedMatchup:
    week: int
    source_team1_id: str
    source_team2_id: str
    team1_score: float
    team2_score: float

    def __post_init__(self) -> None:
        _integer("week", self.week, minimum=1)
        _text("source_team1_id", self.source_team1_id)
        _text("source_team2_id", self.source_team2_id)
        _number("team1_score", self.team1_score)
        _number("team2_score", self.team2_score)


@dataclass(frozen=True, slots=True)
class VerifiedHostLeagueSnapshot:
    """Complete host evidence, before canonical player/team IDs are applied."""

    snapshot_id: str
    captured_at: datetime
    source_provider: str
    source_league_id: str
    season: int
    scoring_profile: ScoringProfile
    first_remaining_week: int
    expected_team_count: int
    teams: tuple[SourceLeagueTeam, ...]
    players: tuple[SourceLeaguePlayer, ...]
    rosters: tuple[SourceTeamRoster, ...]
    standings: tuple[SourceTeamStanding, ...]
    remaining_matchups: tuple[SourceMatchup, ...]
    roster_rules: RosterRules
    playoff_rules: PlayoffRules
    completed_matchups: tuple[SourceCompletedMatchup, ...] | None = None

    def __post_init__(self) -> None:
        _text("snapshot_id", self.snapshot_id)
        _aware_datetime("captured_at", self.captured_at)
        _text("source_provider", self.source_provider)
        _text("source_league_id", self.source_league_id)
        _integer("season", self.season, minimum=2012)
        _integer("first_remaining_week", self.first_remaining_week, minimum=1)
        _integer("expected_team_count", self.expected_team_count, minimum=2)
        for name, item_type in (
            ("teams", SourceLeagueTeam),
            ("players", SourceLeaguePlayer),
            ("rosters", SourceTeamRoster),
            ("standings", SourceTeamStanding),
            ("remaining_matchups", SourceMatchup),
        ):
            object.__setattr__(self, name, _typed_tuple(name, getattr(self, name), item_type))
        if self.completed_matchups is not None:
            object.__setattr__(
                self,
                "completed_matchups",
                _typed_tuple(
                    "completed_matchups", self.completed_matchups, SourceCompletedMatchup
                ),
            )
        if not isinstance(self.roster_rules, RosterRules):
            raise ValueError("roster_rules must be RosterRules")
        if not isinstance(self.playoff_rules, PlayoffRules):
            raise ValueError("playoff_rules must be PlayoffRules")
        if not isinstance(self.scoring_profile, ScoringProfile):
            raise ValueError("scoring_profile must preserve exact captured scoring settings")
        lineup_slots = tuple(
            normalize_lineup_slot(slot)
            for slot in self.roster_rules.starting_lineup_slots
        )
        if (
            lineup_slots != self.roster_rules.starting_lineup_slots
            or any(slot not in _ALLOWED_LINEUP_SLOTS for slot in lineup_slots)
        ):
            raise ValueError("starting lineup slots must use canonical supported names")
        validate_host_league_snapshot(self)


def _one_id_per_provider(entity, references) -> None:
    if not references:
        raise ValueError(f"provider_ids must contain at least one {entity} ID")
    providers = [row.provider for row in references]
    if len(set(providers)) != len(providers):
        raise ValueError(f"a {entity} can have only one ID per provider")


def _normalized_slots(values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("eligible_slots must be an iterable")
    try:
        slots = tuple(normalize_lineup_slot(value) for value in values)
    except TypeError:
        raise ValueError("eligible_slots must be an iterable") from None
    if not slots or len(set(slots)) != len(slots):
        raise ValueError("eligible_slots must be non-empty and cannot contain duplicates")
    if any(slot not in _ALLOWED_LINEUP_SLOTS for slot in slots):
        raise ValueError("eligible_slots contain an unsupported lineup slot")
    return slots


def _typed_tuple(name, values, item_type):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of {item_type.__name__}")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(not isinstance(row, item_type) for row in rows):
        raise ValueError(f"{name} must contain only {item_type.__name__} values")
    return rows


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text(name, value) -> None:
    if not _is_text(value):
        raise ValueError(f"{name} must be a non-empty string")


def _integer(name, value, *, minimum) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")


def _number(name, value) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _aware_datetime(name, value) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")


__all__ = (
    "ProviderPlayerId",
    "ProviderTeamId",
    "SourceCompletedMatchup",
    "SourceLeaguePlayer",
    "SourceLeagueTeam",
    "SourceMatchup",
    "SourceTeamRoster",
    "SourceTeamStanding",
    "VerifiedHostLeagueSnapshot",
    "validate_host_league_snapshot",
)
