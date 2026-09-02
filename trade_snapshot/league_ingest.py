"""Canonicalize one verified host-league snapshot for local computation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ._league_ingest_validation import validate_normalized_inputs
from .identity import IdentityRegistry, PlayerIdentity
from .identity_match import ProviderPlayerRecord
from .league_source import VerifiedHostLeagueSnapshot
from .league_state import (
    CompletedFantasyMatchup,
    FantasyMatchup,
    LeagueState,
    LeagueTeam,
    TeamStanding,
)
from .positions import normalize_player_position
from .scenario_config import PlayerEligibility
from .scoring import ScoringProfile
from .trade_space import TeamRoster


@dataclass(frozen=True, slots=True)
class CanonicalPlayerProviderId:
    canonical_player_id: str
    provider: str
    provider_player_id: str

    def __post_init__(self) -> None:
        _text("canonical_player_id", self.canonical_player_id)
        _text("provider", self.provider)
        _text("provider_player_id", self.provider_player_id)


@dataclass(frozen=True, slots=True)
class CanonicalTeamProviderId:
    canonical_team_id: str
    provider: str
    provider_team_id: str

    def __post_init__(self) -> None:
        _text("canonical_team_id", self.canonical_team_id)
        _text("provider", self.provider)
        _text("provider_team_id", self.provider_team_id)


@dataclass(frozen=True, slots=True)
class NormalizedLeagueInputs:
    """Complete immutable league inputs and their exact provider-ID audit trail."""

    source_provider: str
    source_league_id: str
    captured_at: datetime
    scoring_profile: ScoringProfile
    league_state: LeagueState
    rosters: tuple[TeamRoster, ...]
    eligibilities: tuple[PlayerEligibility, ...]
    player_identities: tuple[PlayerIdentity, ...]
    player_provider_ids: tuple[CanonicalPlayerProviderId, ...]
    team_provider_ids: tuple[CanonicalTeamProviderId, ...]
    completed_history_available: bool

    def __post_init__(self) -> None:
        _text("source_provider", self.source_provider)
        _text("source_league_id", self.source_league_id)
        if (
            not isinstance(self.captured_at, datetime)
            or self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() is None
        ):
            raise ValueError("captured_at must be a timezone-aware datetime")
        if not isinstance(self.scoring_profile, ScoringProfile):
            raise ValueError("scoring_profile must be ScoringProfile")
        if not isinstance(self.league_state, LeagueState):
            raise ValueError("league_state must be LeagueState")
        if self.league_state.scoring_profile_id != self.scoring_profile.scoring_profile_id:
            raise ValueError("league_state does not match the captured scoring profile")
        for name, item_type in (
            ("rosters", TeamRoster),
            ("eligibilities", PlayerEligibility),
            ("player_identities", PlayerIdentity),
            ("player_provider_ids", CanonicalPlayerProviderId),
            ("team_provider_ids", CanonicalTeamProviderId),
        ):
            object.__setattr__(self, name, _typed_tuple(name, getattr(self, name), item_type))
        if not isinstance(self.completed_history_available, bool):
            raise ValueError("completed_history_available must be a boolean")
        validate_normalized_inputs(self)

    def player_ids_for(self, provider: str) -> dict[str, str]:
        _text("provider", provider)
        return {
            row.canonical_player_id: row.provider_player_id
            for row in self.player_provider_ids
            if row.provider == provider
        }

    def team_ids_for(self, provider: str) -> dict[str, str]:
        _text("provider", provider)
        return {
            row.canonical_team_id: row.provider_team_id
            for row in self.team_provider_ids
            if row.provider == provider
        }


class HostLeagueArtifactAdapter(Protocol):
    """The sole seam between a volatile capture schema and verified host evidence."""

    def to_host_league_snapshot(
        self, artifact: object, *, expected_team_count: int
    ) -> VerifiedHostLeagueSnapshot: ...


def host_player_records(
    snapshot: VerifiedHostLeagueSnapshot,
) -> tuple[ProviderPlayerRecord, ...]:
    """Expose exact host metadata for the existing identity reconciler."""

    if not isinstance(snapshot, VerifiedHostLeagueSnapshot):
        raise ValueError("snapshot must be VerifiedHostLeagueSnapshot")
    return tuple(
        ProviderPlayerRecord(
            reference.provider,
            reference.player_id,
            player.display_name,
            player.position,
            player.nfl_team_id,
        )
        for player in snapshot.players
        for reference in player.provider_ids
    )


def ingest_host_league_artifact(
    artifact: object,
    *,
    adapter: HostLeagueArtifactAdapter,
    identities: IdentityRegistry,
    expected_team_count: int,
) -> NormalizedLeagueInputs:
    """Adapt once, verify the requested league size, then canonicalize."""

    if (
        isinstance(expected_team_count, bool)
        or not isinstance(expected_team_count, int)
        or expected_team_count < 2
    ):
        raise ValueError("expected_team_count must be an integer of at least 2")
    snapshot = adapter.to_host_league_snapshot(
        artifact, expected_team_count=expected_team_count
    )
    if not isinstance(snapshot, VerifiedHostLeagueSnapshot):
        raise ValueError("host league adapter returned an invalid snapshot type")
    if snapshot.expected_team_count != expected_team_count:
        raise ValueError("host league adapter did not honor expected_team_count")
    return normalize_host_league_snapshot(snapshot, identities)


def normalize_host_league_snapshot(
    snapshot: VerifiedHostLeagueSnapshot,
    identities: IdentityRegistry,
) -> NormalizedLeagueInputs:
    """Resolve every rostered source player through verified exact provider IDs."""

    if not isinstance(snapshot, VerifiedHostLeagueSnapshot):
        raise ValueError("snapshot must be VerifiedHostLeagueSnapshot")
    if not isinstance(identities, IdentityRegistry):
        raise ValueError("identities must be IdentityRegistry")

    source_players = {row.source_player_id: row for row in snapshot.players}
    rostered_ids = {
        player_id for roster in snapshot.rosters for player_id in roster.source_player_ids
    }
    resolved = _resolve_rostered_players(snapshot, identities, source_players, rostered_ids)
    team_ids = {
        row.source_team_id: _canonical_team_id(snapshot.source_provider, row.source_team_id)
        for row in snapshot.teams
    }
    division_ids = {
        row.division_id: _canonical_division_id(snapshot.source_provider, row.division_id)
        for row in snapshot.teams
        if row.division_id is not None
    }

    state = _normalized_league_state(snapshot, team_ids, division_ids)
    rosters = tuple(
        TeamRoster(
            team_ids[row.source_team_id],
            tuple(resolved[player_id].canonical_player_id for player_id in row.source_player_ids),
            len(row.source_player_ids),
            snapshot.roster_rules.roster_cap,
            frozenset(
                resolved[player_id].canonical_player_id
                for player_id in row.capacity_exempt_source_player_ids
            ),
        )
        for row in snapshot.rosters
    )
    eligibilities = tuple(
        PlayerEligibility(
            resolved[player_id].canonical_player_id,
            source_players[player_id].eligible_slots,
        )
        for player_id in sorted(rostered_ids)
    )
    players = tuple(
        sorted(
            {resolved[player_id] for player_id in rostered_ids},
            key=lambda row: row.canonical_player_id,
        )
    )
    return NormalizedLeagueInputs(
        source_provider=snapshot.source_provider,
        source_league_id=snapshot.source_league_id,
        captured_at=snapshot.captured_at,
        scoring_profile=snapshot.scoring_profile,
        league_state=state,
        rosters=rosters,
        eligibilities=eligibilities,
        player_identities=players,
        player_provider_ids=_player_provider_mappings(players),
        team_provider_ids=tuple(
            CanonicalTeamProviderId(
                team_ids[team.source_team_id], reference.provider, reference.team_id
            )
            for team in snapshot.teams
            for reference in team.provider_ids
        ),
        completed_history_available=snapshot.completed_matchups is not None,
    )


def _normalized_league_state(snapshot, team_ids, division_ids) -> LeagueState:
    return LeagueState(
        snapshot_id=snapshot.snapshot_id,
        season=snapshot.season,
        scoring_profile_id=snapshot.scoring_profile.scoring_profile_id,
        first_remaining_week=snapshot.first_remaining_week,
        teams=tuple(
            LeagueTeam(
                team_ids[row.source_team_id],
                row.name,
                division_ids.get(row.division_id),
            )
            for row in snapshot.teams
        ),
        standings=tuple(
            TeamStanding(
                team_ids[row.source_team_id], row.wins, row.losses, row.ties,
                row.points_for, row.points_against,
            )
            for row in snapshot.standings
        ),
        remaining_matchups=tuple(
            FantasyMatchup(
                row.week,
                team_ids[row.source_team1_id],
                team_ids[row.source_team2_id],
                row.team1_score_adjustment,
            )
            for row in snapshot.remaining_matchups
        ),
        completed_matchups=tuple(
            CompletedFantasyMatchup(
                row.week,
                team_ids[row.source_team1_id],
                team_ids[row.source_team2_id],
                row.team1_score,
                row.team2_score,
            )
            for row in (snapshot.completed_matchups or ())
        ),
        roster_rules=snapshot.roster_rules,
        playoff_rules=snapshot.playoff_rules,
    )


def _resolve_rostered_players(snapshot, identities, players, rostered_ids):
    resolved = {}
    canonical_ids = set()
    for source_id in sorted(rostered_ids):
        source = players[source_id]
        identity = identities.lookup(snapshot.source_provider, source_id)
        if identity is None:
            unresolved = identities.unresolved_for(snapshot.source_provider, source_id)
            detail = f": {unresolved.reason}" if unresolved is not None else ""
            raise ValueError(f"rostered source player {source_id!r} is not exactly resolved{detail}")
        if normalize_player_position(identity.position) != source.position:
            raise ValueError("resolved player position conflicts with host metadata")
        for reference in source.provider_ids:
            linked = identities.lookup(reference.provider, reference.player_id)
            if linked is None or linked.canonical_player_id != identity.canonical_player_id:
                raise ValueError("host player provider IDs are not verified by the identity registry")
        if identity.canonical_player_id in canonical_ids:
            raise ValueError("two rostered source IDs resolve to one canonical player")
        canonical_ids.add(identity.canonical_player_id)
        resolved[source_id] = PlayerIdentity(
            identity.canonical_player_id,
            source.display_name,
            source.position,
            source.nfl_team_id,
            identity.provider_references,
        )
    return resolved


def _player_provider_mappings(players):
    return tuple(
        CanonicalPlayerProviderId(
            player.canonical_player_id,
            reference.provider,
            reference.provider_player_id,
        )
        for player in players
        for reference in player.provider_references
    )


def _canonical_team_id(provider: str, source_team_id: str) -> str:
    return f"{provider}:team:{source_team_id}"


def _canonical_division_id(provider: str, division_id: str) -> str:
    return f"{provider}:division:{division_id}"


def _typed_tuple(name, values, item_type):
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(not isinstance(row, item_type) for row in rows):
        raise ValueError(f"{name} must contain only {item_type.__name__} values")
    return rows


def _text(name, value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


__all__ = (
    "CanonicalPlayerProviderId",
    "CanonicalTeamProviderId",
    "HostLeagueArtifactAdapter",
    "NormalizedLeagueInputs",
    "host_player_records",
    "ingest_host_league_artifact",
    "normalize_host_league_snapshot",
)
