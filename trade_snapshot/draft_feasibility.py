"""Draft-specific completion proofs, roster caps, and strategy preflight."""

from collections import Counter
from dataclasses import dataclass, field

from .draft_config import DraftLeagueConfig, DraftStrategy
from .draft_history import PreseasonPlayer
from .draft_matching import maximum_group_slot_fill
from .positions import CANONICAL_PLAYER_POSITIONS


@dataclass(slots=True)
class FeasibilityCache:
    config_id: str
    filled_counts: dict[tuple[tuple[str, ...], ...], int] = field(default_factory=dict)
    strategy_permissions: dict[tuple[DraftStrategy, str, int], bool] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class CompletionProblem:
    config: DraftLeagueConfig
    slots: tuple[tuple[int, tuple[str, ...]], ...]
    roster_players: tuple[tuple[PreseasonPlayer, ...], ...]
    future_rounds: tuple[tuple[int, ...], ...]
    owned_groups: Counter
    available_groups: Counter
    available_keys: dict[tuple[str, tuple[str, ...]], tuple | None]
    position_remaining: dict[tuple[int, str], int]
    position_capacities: dict[tuple[int, str], int]
    cache: FeasibilityCache
    pending_team: int | None
    blocked: bool

    @property
    def target(self) -> int:
        return len(self.slots)


def validate_player_supply(players, config, strategies, cache=None) -> None:
    """Reject a draft pool that cannot complete every configured lineup."""

    cache = cache or FeasibilityCache(config.config_id)
    if cache.config_id != config.config_id:
        raise ValueError("feasibility cache does not match the league configuration")
    if filled_count(players, config, cache) < len(config.starting_slots):
        raise ValueError("player pool cannot fill the configured starting lineup")
    _validate_strategy_round_capacity(config, strategies, cache)
    empty_rosters = tuple(() for _ in range(config.team_count))
    if _global_completion_capacity(
        empty_rosters,
        players,
        strategies,
        {player.player_id: player for player in players},
        config,
        cache,
    ) < config.team_count * config.roster_size:
        raise ValueError(
            "player pool, position limits, and strategies cannot fill "
            "every team's complete configured roster"
        )


def prepare_completion(
    rosters,
    available,
    strategies,
    players,
    config,
    overall_pick,
    *,
    pending_team,
    cache,
):
    bench_eligibility = tuple(sorted(CANONICAL_PLAYER_POSITIONS))
    roster_eligibilities = (
        *(config.slot_eligibility[slot] for slot in config.starting_slots),
        *((bench_eligibility,) * config.bench_slots),
    )
    slots = tuple(
        (team, allowed)
        for team in range(config.team_count)
        for allowed in roster_eligibilities
    )
    future_rounds = tuple(
        tuple(
            (pick - 1) // config.team_count + 1
            for pick in range(overall_pick + 1, config.team_count * config.roster_size + 1)
            if _drafter_number(pick, config.team_count) == team + 1
        )
        for team in range(config.team_count)
    )
    roster_players = tuple(
        tuple(players[player_id] for player_id in roster) for roster in rosters
    )
    blocked = False
    position_remaining = {}
    position_capacities = {}
    positions = {player.position for player in available}
    for team, roster in enumerate(roster_players):
        counts = Counter(player.position for player in roster)
        blocked |= len(roster) > config.roster_size
        for position in positions | set(counts):
            remaining = position_limit(config, position) - counts[position]
            allowed_rounds = sum(
                _strategy_allows(strategies[team], position, round_number, config, cache)
                for round_number in future_rounds[team]
            )
            position_remaining[team, position] = remaining
            position_capacities[team, position] = min(remaining, allowed_rounds)
            blocked |= remaining < 0
        if team != pending_team:
            missing = len(config.starting_slots) - filled_count(roster, config, cache)
            blocked |= missing > len(future_rounds[team])

    owned_groups = Counter()
    for team, roster in enumerate(roster_players):
        for player in roster:
            edges = _owned_slot_edges(player, team, slots)
            if edges:
                owned_groups[edges] += 1
    available_groups = Counter()
    available_keys = {}
    for player in available:
        signature = player.position, player.eligible_positions
        if signature not in available_keys:
            available_keys[signature] = _available_group_key(
                player, strategies, future_rounds, config, cache
            )
        key = available_keys[signature]
        if key is not None:
            available_groups[key] += 1
    return CompletionProblem(
        config,
        slots,
        roster_players,
        future_rounds,
        owned_groups,
        available_groups,
        available_keys,
        position_remaining,
        position_capacities,
        cache,
        pending_team,
        blocked,
    )


def completion_after_pick(problem, player) -> bool:
    if problem.blocked or problem.pending_team is None:
        return False
    team = problem.pending_team
    if not candidate_preserves_starter_deadline(
        problem.roster_players[team],
        player,
        len(problem.future_rounds[team]),
        problem.config,
        problem.cache,
    ):
        return False
    capacity_key = team, player.position
    remaining_capacity = problem.position_remaining.get(capacity_key, 0)
    if remaining_capacity <= 0:
        return False
    if _completion_group_is_surplus(problem, player):
        return True

    available_groups = problem.available_groups.copy()
    available_key = problem.available_keys[player.position, player.eligible_positions]
    if available_key is not None:
        available_groups[available_key] -= 1
        if not available_groups[available_key]:
            del available_groups[available_key]
    owned_groups = problem.owned_groups.copy()
    edges = _owned_slot_edges(player, team, problem.slots)
    if edges:
        owned_groups[edges] += 1
    capacities = problem.position_capacities.copy()
    capacities[capacity_key] = min(
        remaining_capacity - 1,
        problem.position_capacities.get(capacity_key, 0),
    )
    return maximum_group_slot_fill(
        owned_groups, available_groups, problem.slots, capacities
    ) == problem.target


def completion_group_is_surplus(problem, player) -> bool:
    """Prove a pick safe when identical players cover every possible use."""

    if problem.blocked or problem.pending_team is None:
        return False
    team = problem.pending_team
    if not candidate_preserves_starter_deadline(
        problem.roster_players[team],
        player,
        len(problem.future_rounds[team]),
        problem.config,
        problem.cache,
    ):
        return False
    if problem.position_remaining.get((team, player.position), 0) <= 0:
        return False
    return _completion_group_is_surplus(problem, player)


def _completion_group_is_surplus(problem, player) -> bool:
    key = problem.available_keys[player.position, player.eligible_positions]
    if key is None:
        return False
    primary, eligible, round_limits = key
    total_demand = 0
    for team, round_limit in enumerate(round_limits):
        compatible_slots = sum(
            slot_team == team and bool(set(eligible).intersection(allowed))
            for slot_team, allowed in problem.slots
        )
        if team == problem.pending_team:
            raw_remaining = problem.position_remaining.get((team, primary), 0)
            future_capacity = min(
                max(0, raw_remaining - 1),
                problem.position_capacities.get((team, primary), 0),
            )
            total_demand += min(compatible_slots, 1 + future_capacity)
        else:
            total_demand += min(
                compatible_slots,
                round_limit,
                problem.position_capacities.get((team, primary), 0),
            )
    return problem.available_groups[key] >= total_demand


def candidate_preserves_starter_deadline(
    roster, player, picks_left, config, cache, *, current_filled=None
) -> bool:
    """Check the local starter deadline without repeating the whole-pool proof."""

    starter_count = len(config.starting_slots)
    if picks_left >= starter_count:
        return True
    if current_filled is None:
        filled_after = filled_count((*roster, player), config, cache)
        return starter_count - filled_after <= picks_left
    missing = starter_count - current_filled
    if missing <= picks_left:
        return True
    return (
        missing == picks_left + 1
        and filled_count((*roster, player), config, cache) > current_filled
    )


def filled_count(players, config, cache) -> int:
    key = tuple(sorted(player.eligible_positions for player in players))
    known = cache.filled_counts.get(key)
    if known is not None:
        return known
    slots = tuple((0, config.slot_eligibility[slot]) for slot in config.starting_slots)
    groups = Counter(
        tuple(
            index
            for index, (_, allowed) in enumerate(slots)
            if set(player.eligible_positions).intersection(allowed)
        )
        for player in players
    )
    groups.pop((), None)
    result = maximum_group_slot_fill(groups, Counter(), slots, {})
    cache.filled_counts[key] = result
    return result


def position_limit(config, position) -> int:
    explicit = config.position_limits.get(position)
    return config.roster_size if explicit is None else explicit


def _global_completion_capacity(rosters, available, strategies, players, config, cache):
    problem = prepare_completion(
        rosters,
        available,
        strategies,
        players,
        config,
        0,
        pending_team=None,
        cache=cache,
    )
    if problem.blocked:
        return 0
    return maximum_group_slot_fill(
        problem.owned_groups,
        problem.available_groups,
        problem.slots,
        problem.position_capacities,
    )


def _available_group_key(player, strategies, future_rounds, config, cache):
    round_limits = []
    for team, rounds in enumerate(future_rounds):
        primary_rounds = tuple(
            round_number
            for round_number in rounds
            if _strategy_allows(
                strategies[team], player.position, round_number, config, cache
            )
        )
        player_rounds = tuple(
            round_number
            for round_number in rounds
            if all(
                _strategy_allows(
                    strategies[team], position, round_number, config, cache
                )
                for position in player.eligible_positions
            )
        )
        # Differently gated secondary eligibility creates another shared round
        # constraint. Ignore that rare group rather than claim a false proof.
        round_limits.append(len(player_rounds) if player_rounds == primary_rounds else 0)
    limits = tuple(round_limits)
    return (player.position, player.eligible_positions, limits) if any(limits) else None


def _owned_slot_edges(player, team, slots):
    eligible = set(player.eligible_positions)
    return tuple(
        index
        for index, (slot_team, allowed) in enumerate(slots)
        if slot_team == team and eligible.intersection(allowed)
    )


def _validate_strategy_round_capacity(config, strategies, cache):
    restricted = {
        DraftStrategy.STREAMING_QB: "QB",
        DraftStrategy.STREAMING_TE: "TE",
        DraftStrategy.STREAMING_DST: "DST",
        DraftStrategy.LATE_ROUND_QB: "QB",
    }
    for strategy in dict.fromkeys(strategies):
        position = restricted.get(strategy)
        if position is None:
            continue
        allowed = sum(
            _strategy_allows(strategy, position, round_number, config, cache)
            for round_number in range(1, config.total_rounds + 1)
        )
        required = _minimum_required_primary(config, position)
        if required > allowed:
            raise ValueError(
                f"{strategy.value} has only {allowed} eligible draft rounds but "
                f"the starting lineup requires at least {required} {position} picks"
            )


def _minimum_required_primary(config, restricted):
    slots = tuple(config.slot_eligibility[slot] for slot in config.starting_slots)
    positions = tuple(
        sorted({position for allowed in slots for position in allowed} - {restricted})
    )
    groups = Counter()
    for position in positions:
        edges = tuple(index for index, allowed in enumerate(slots) if position in allowed)
        if edges:
            groups[edges] += position_limit(config, position)
    encoded_slots = tuple((0, allowed) for allowed in slots)
    return len(slots) - maximum_group_slot_fill(
        groups, Counter(), encoded_slots, {}
    )


def _strategy_allows(strategy, position, round_number, config, cache):
    key = strategy, position, round_number
    if key not in cache.strategy_permissions:
        cache.strategy_permissions[key] = strategy.allows(
            position, round_number, config.total_rounds
        )
    return cache.strategy_permissions[key]


def _drafter_number(overall_pick, team_count):
    round_index, offset = divmod(overall_pick - 1, team_count)
    return offset + 1 if round_index % 2 == 0 else team_count - offset


__all__ = (
    "CompletionProblem",
    "FeasibilityCache",
    "completion_after_pick",
    "completion_group_is_surplus",
    "filled_count",
    "position_limit",
    "prepare_completion",
    "validate_player_supply",
)
