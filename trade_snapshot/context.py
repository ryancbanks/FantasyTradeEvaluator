"""Fail-closed identity and completeness boundary for local calculations."""

from collections import Counter
from dataclasses import dataclass
from math import isclose
from typing import Hashable

from .league_state import LeagueState
from .projections import ProjectionStatus, WeeklyProjection
from .scoring import ScoringProfile
from .strength import RoleKind, StrengthModel
from .trade_space import TeamRoster


@dataclass(frozen=True)
class ProjectionProviderPolicy:
    """Providers and minimum usable coverage required by one ensemble run."""

    required_providers: tuple[str, ...]
    minimum_observed_providers: int = 1

    def __post_init__(self) -> None:
        providers = tuple(self.required_providers)
        if not providers or any(
            not isinstance(provider, str) or not provider.strip()
            for provider in providers
        ):
            raise ValueError("required_providers must contain non-empty strings")
        if len(set(providers)) != len(providers):
            raise ValueError("required_providers cannot contain duplicates")
        if (
            isinstance(self.minimum_observed_providers, bool)
            or not isinstance(self.minimum_observed_providers, int)
            or not 1 <= self.minimum_observed_providers <= len(providers)
        ):
            raise ValueError(
                "minimum_observed_providers must be between 1 and the provider count"
            )
        object.__setattr__(self, "required_providers", providers)


@dataclass(frozen=True)
class EngineContext:
    """Inputs proven to describe one league, snapshot, season, and scoring profile."""

    scoring_profile: ScoringProfile
    league_state: LeagueState
    team_rosters: tuple[TeamRoster, ...]
    computation_player_ids: frozenset[str]
    projection_policy: ProjectionProviderPolicy
    weekly_projections: tuple[WeeklyProjection, ...]
    strength_model: StrengthModel

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_rosters", tuple(self.team_rosters))
        try:
            domain = frozenset(self.computation_player_ids)
        except TypeError:
            raise ValueError("computation_player_ids must be hashable") from None
        object.__setattr__(self, "computation_player_ids", domain)
        object.__setattr__(self, "weekly_projections", tuple(self.weekly_projections))
        validate_engine_context(self)


def validate_engine_context(context: EngineContext) -> None:
    """Reject mixed identities, partial rosters, and incomplete projection grids."""

    if not isinstance(context.scoring_profile, ScoringProfile):
        raise ValueError("scoring_profile must be a ScoringProfile")
    if not isinstance(context.league_state, LeagueState):
        raise ValueError("league_state must be a LeagueState")
    if not isinstance(context.strength_model, StrengthModel):
        raise ValueError("strength_model must be a StrengthModel")
    if not isinstance(context.projection_policy, ProjectionProviderPolicy):
        raise ValueError("projection_policy must be a ProjectionProviderPolicy")

    state = context.league_state
    identity = (state.snapshot_id, state.season, state.scoring_profile_id)
    if context.scoring_profile.scoring_profile_id != state.scoring_profile_id:
        raise ValueError("league state does not match the content-addressed scoring profile")
    model_identity = (
        context.strength_model.snapshot_id,
        context.strength_model.season,
        context.strength_model.scoring_profile_id,
    )
    if model_identity != identity:
        raise ValueError("strength model identity does not match league state")

    roster_by_team: dict[Hashable, TeamRoster] = {}
    for roster in context.team_rosters:
        if not isinstance(roster, TeamRoster):
            raise ValueError("team_rosters must contain only TeamRoster rows")
        if roster.team_id in roster_by_team:
            raise ValueError("team_rosters contain a duplicate team_id")
        roster_by_team[roster.team_id] = roster

    state_team_ids = {team.team_id for team in state.teams}
    if set(roster_by_team) != state_team_ids:
        raise ValueError("team_rosters must contain exactly one full roster for every team")

    owner_by_player: dict[Hashable, Hashable] = {}
    for roster in context.team_rosters:
        if roster.current_size != len(roster.player_ids):
            raise ValueError("EngineContext requires full rosters, not search-pool subsets")
        if roster.roster_cap != state.roster_rules.roster_cap:
            raise ValueError("team roster cap does not match league roster rules")
        for player_id in roster.player_ids:
            previous_owner = owner_by_player.get(player_id)
            if previous_owner is not None:
                raise ValueError("a player_id cannot be owned by more than one team")
            owner_by_player[player_id] = roster.team_id

    domain = context.computation_player_ids
    if not domain or any(
        not isinstance(player_id, str) or not player_id
        for player_id in domain
    ):
        raise ValueError("computation_player_ids must contain non-empty strings")
    missing_owned = set(owner_by_player).difference(domain)
    if missing_owned:
        missing = _first_stable(missing_owned)
        raise ValueError(
            f"computation domain omits rostered player_id {missing!r}"
        )

    missing_calibrations = set(domain).difference(context.strength_model.players)
    if missing_calibrations:
        missing = _first_stable(missing_calibrations)
        raise ValueError(f"missing strength calibration for computation player_id {missing!r}")

    _validate_role_schema(context)
    pretrade_max = max(
        context.strength_model.score_roster(roster.player_ids).absolute_score
        for roster in context.team_rosters
    )
    if not isclose(
        pretrade_max,
        context.strength_model.normalization_denominator,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise ValueError(
            "strength normalization denominator does not equal the pre-trade league maximum"
        )

    projection_keys: set[tuple[str, str, int]] = set()
    canonical_keys: set[tuple[str, str, int]] = set()
    row_by_grid_key: dict[tuple[str, str, int], WeeklyProjection] = {}
    remaining_weeks = set(state.remaining_regular_season_weeks)
    providers = set(context.projection_policy.required_providers)
    for projection in context.weekly_projections:
        if not isinstance(projection, WeeklyProjection):
            raise ValueError("weekly_projections must contain only WeeklyProjection rows")
        projection_identity = (
            projection.snapshot_id,
            projection.season,
            projection.scoring_profile_id,
        )
        if projection_identity != identity:
            raise ValueError("weekly projection identity does not match league state")
        if projection.provider not in providers:
            raise ValueError("weekly projection provider is outside the declared provider policy")
        if projection.week not in remaining_weeks:
            raise ValueError("weekly projection is outside the remaining regular-season window")
        if projection.canonical_player_id is None:
            raise ValueError("computation projections cannot contain unresolved players")
        if projection.canonical_player_id not in domain:
            raise ValueError("weekly projection player is outside the computation domain")
        projection_key = (
            projection.provider,
            projection.provider_player_id,
            projection.week,
        )
        if projection_key in projection_keys:
            raise ValueError("weekly_projections contain a duplicate provider player/week row")
        projection_keys.add(projection_key)
        canonical_key = (
            projection.provider,
            projection.canonical_player_id,
            projection.week,
        )
        if canonical_key in canonical_keys:
            raise ValueError("weekly_projections map two provider IDs to one player/week")
        canonical_keys.add(canonical_key)
        row_by_grid_key[canonical_key] = projection

    expected_grid = {
        (provider, player_id, week)
        for provider in context.projection_policy.required_providers
        for player_id in domain
        for week in state.remaining_regular_season_weeks
    }
    missing_grid = expected_grid.difference(row_by_grid_key)
    if missing_grid:
        provider, player_id, week = min(
            missing_grid,
            key=lambda value: (value[0], _stable_key(value[1]), value[2]),
        )
        raise ValueError(
            f"missing {provider!r} projection row for player_id {player_id!r}, week {week}"
        )

    for player_id in domain:
        for week in state.remaining_regular_season_weeks:
            rows = tuple(
                row_by_grid_key[(provider, player_id, week)]
                for provider in context.projection_policy.required_providers
            )
            bye_count = sum(row.status is ProjectionStatus.BYE for row in rows)
            if bye_count:
                if bye_count != len(rows):
                    raise ValueError("providers disagree on whether a player-week is a bye")
                continue
            observed_count = sum(
                row.status is ProjectionStatus.OBSERVED for row in rows
            )
            if observed_count < context.projection_policy.minimum_observed_providers:
                raise ValueError(
                    f"player_id {player_id!r}, week {week} has too few observed providers"
                )


def _validate_role_schema(context: EngineContext) -> None:
    expected = Counter(context.league_state.roster_rules.starting_lineup_slots)
    actual = Counter(
        role.source_slot
        for role in context.strength_model.role_definitions
        if role.kind is RoleKind.STARTER
    )
    if actual != expected:
        raise ValueError("strength starter roles do not match league starting-lineup slots")


def _first_stable(values: set[Hashable]) -> Hashable:
    return min(values, key=_stable_key)


def _stable_key(value: Hashable) -> tuple[str, str]:
    return type(value).__qualname__, repr(value)
