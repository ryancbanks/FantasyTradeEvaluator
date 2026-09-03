"""Deterministic, roster-aware snake drafts driven by anonymous draft brains."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import json

from .draft_brain import DraftBrain
from .draft_config import DraftLeagueConfig, DraftStrategy
from .draft_features import candidate_feature_values, resolve_preseason_projection
from .draft_feasibility import (
    FeasibilityCache,
    completion_after_pick,
    completion_group_is_surplus,
    filled_count,
    position_limit,
    prepare_completion,
    validate_player_supply,
)
from .draft_history import DraftPlayerBoard, HistoricalSeason, PreseasonPlayer


@dataclass(frozen=True, slots=True)
class DraftPick:
    overall_pick: int
    round_number: int
    drafter_number: int
    player_id: str
    utility: float

    def to_record(self, names: dict[str, str] | None = None) -> dict[str, object]:
        return {
            "overall_pick": self.overall_pick,
            "round": self.round_number,
            "drafter_number": self.drafter_number,
            "drafter_name": f"Drafter #{self.drafter_number}",
            "player_id": self.player_id,
            "player_name": None if names is None else names[self.player_id],
            "utility": self.utility,
        }


@dataclass(frozen=True, slots=True)
class DraftResult:
    season: int
    config_id: str
    seed: int
    brain_ids: tuple[str, ...]
    strategies: tuple[DraftStrategy, ...]
    rosters: tuple[tuple[str, ...], ...]
    picks: tuple[DraftPick, ...]

    def to_record(self, season: HistoricalSeason) -> dict[str, object]:
        names = {player.player_id: player.display_name for player in season.players}
        return {
            "season": self.season,
            "config_id": self.config_id,
            "seed": self.seed,
            "teams": [
                {
                    "drafter_number": index + 1,
                    "name": f"Drafter #{index + 1}",
                    "brain_id": self.brain_ids[index],
                    "strategy": self.strategies[index].value,
                    "roster": [
                        {"player_id": player_id, "player_name": names[player_id]}
                        for player_id in roster
                    ],
                }
                for index, roster in enumerate(self.rosters)
            ],
            "picks": [row.to_record(names) for row in self.picks],
        }


@dataclass(frozen=True, slots=True)
class RankedDraftCandidate:
    player_id: str
    utility: float
    baseline_utility: float
    neural_adjustment: float
    starter_need: float
    position_count: int


@dataclass(slots=True)
class _SimulationCache:
    feasibility: FeasibilityCache
    projection_hints: dict[str, float] = field(default_factory=dict)


def simulate_snake_draft(
    season: HistoricalSeason,
    config: DraftLeagueConfig,
    brains: Sequence[DraftBrain],
    *,
    strategies: Sequence[DraftStrategy] | None = None,
    seed: int = 0,
    candidate_window: int = 0,
    should_cancel=lambda: False,
) -> DraftResult:
    """Complete one legal draft; actual weekly outcomes are never inspected."""

    _validate_inputs(season, config, brains, seed)
    if type(candidate_window) is not int or not 0 <= candidate_window <= 4096:
        raise ValueError("candidate_window must be an integer from zero through 4096")
    assigned = _strategy_assignment(config, strategies, seed)
    cache = _SimulationCache(FeasibilityCache(config.config_id))
    rosters: list[list[str]] = [[] for _ in range(config.team_count)]
    available = {player.player_id: player for player in season.players}
    required = config.team_count * config.roster_size
    if len(available) < required:
        raise ValueError(f"season needs at least {required} draftable players")
    validate_player_supply(
        tuple(available.values()), config, assigned, cache.feasibility
    )
    picks: list[DraftPick] = []

    for round_number in range(1, config.total_rounds + 1):
        order = range(config.team_count) if round_number % 2 else range(config.team_count - 1, -1, -1)
        for seat in order:
            if should_cancel():
                raise InterruptedError("draft simulation was cancelled")
            overall_pick = len(picks) + 1
            ranked = rank_draft_candidates(
                season,
                config,
                brains[seat],
                assigned[seat],
                roster_player_ids=rosters[seat],
                available_players=tuple(available.values()),
                round_number=round_number,
                overall_pick=overall_pick,
                drafter_number=seat + 1,
                seed=seed,
                candidate_window=candidate_window,
                all_roster_player_ids=tuple(tuple(row) for row in rosters),
                all_strategies=assigned,
                _simulation_cache=cache,
            )
            if not ranked:
                raise ValueError(
                    f"{assigned[seat].value} leaves Drafter #{seat + 1} without a legal "
                    f"pick in round {round_number}; adjust slots, limits, or strategy"
                )
            chosen = ranked[0]
            rosters[seat].append(chosen.player_id)
            del available[chosen.player_id]
            picks.append(DraftPick(
                overall_pick, round_number, seat + 1, chosen.player_id, chosen.utility
            ))

    if any(
        _filled_starter_count(roster, season, config, cache) < len(config.starting_slots)
        for roster in rosters
    ):
        raise AssertionError("draft completion invariant failed")
    return DraftResult(
        season.season,
        config.config_id,
        seed,
        tuple(brain.brain_id for brain in brains),
        assigned,
        tuple(tuple(roster) for roster in rosters),
        tuple(picks),
    )


def rank_draft_candidates(
    season: HistoricalSeason | DraftPlayerBoard,
    config: DraftLeagueConfig,
    brain: DraftBrain,
    strategy: DraftStrategy,
    *,
    roster_player_ids: Sequence[str],
    available_players: Sequence[PreseasonPlayer],
    round_number: int,
    overall_pick: int,
    drafter_number: int,
    seed: int = 0,
    candidate_window: int = 0,
    all_roster_player_ids: Sequence[Sequence[str]] | None = None,
    all_strategies: Sequence[DraftStrategy] | None = None,
    _simulation_cache: _SimulationCache | None = None,
) -> tuple[RankedDraftCandidate, ...]:
    """Rank currently legal choices for simulations and the manual assistant."""

    if brain.league_config_fingerprint != config.config_id:
        raise ValueError("draft brain is not compatible with this league configuration")
    if not isinstance(strategy, DraftStrategy):
        raise ValueError("strategy must be a DraftStrategy")
    players = {player.player_id: player for player in season.players}
    if any(player_id not in players for player_id in roster_player_ids):
        raise ValueError("roster contains a player outside the selected season")
    available = tuple(available_players)
    if len({player.player_id for player in available}) != len(available):
        raise ValueError("available players contain duplicates")
    roster = tuple(players[player_id] for player_id in roster_player_ids)
    cache = _simulation_cache or _SimulationCache(FeasibilityCache(config.config_id))
    if cache.feasibility.config_id != config.config_id:
        raise ValueError("simulation cache does not match the league configuration")
    roster_counts = Counter(player.position for player in roster)
    counts = Counter(player.position for player in available)
    picks_left_after = config.roster_size - len(roster) - 1
    next_pick = _picks_until_next(config.team_count, overall_pick, drafter_number)
    pool_can_fill = filled_count(
        (*roster, *available), config, cache.feasibility
    ) == len(config.starting_slots)
    legal = []
    local_completion: dict[tuple[str, ...], bool] = {}
    for player in available:
        if not all(
            strategy.allows(position, round_number, config.total_rounds)
            for position in player.eligible_positions
        ):
            continue
        if roster_counts[player.position] >= position_limit(config, player.position):
            continue
        completion = local_completion.get(player.eligible_positions)
        if completion is None:
            completion = _can_complete(
                (*roster, player), picks_left_after, pool_can_fill,
                config, cache.feasibility,
            )
            local_completion[player.eligible_positions] = completion
        if not completion:
            continue
        legal.append(player)
    local_legal = tuple(legal)
    if candidate_window:
        if type(candidate_window) is not int or not 1 <= candidate_window <= 4096:
            raise ValueError("candidate_window must be zero or an integer through 4096")
        legal = _shortlist(legal, candidate_window, config, cache)
    if all_roster_player_ids is not None or all_strategies is not None:
        all_rosters, strategies = _global_context(
            all_roster_player_ids, all_strategies, config, players,
            roster_player_ids, drafter_number,
        )
        completion_problem = prepare_completion(
            all_rosters, available, strategies, players, config, overall_pick,
            pending_team=drafter_number - 1, cache=cache.feasibility,
        )
        completion_by_signature: dict[tuple[str, tuple[str, ...]], bool] = {}

        def preserves_global_completion(player):
            signature = (player.position, player.eligible_positions)
            known = completion_by_signature.get(signature)
            if known is not None:
                return known
            feasible = (
                not completion_problem.blocked
                and completion_group_is_surplus(completion_problem, player)
            ) or completion_after_pick(completion_problem, player)
            completion_by_signature[signature] = feasible
            return feasible

        legal = [player for player in legal if preserves_global_completion(player)]
        if not legal and candidate_window and len(local_legal) > candidate_window:
            # The projection shortlist is a speed control, not permission to
            # declare a feasible draft impossible. Expand only on this rare path.
            legal = [
                player for player in local_legal
                if preserves_global_completion(player)
            ]
    ranked = []
    feature_templates = {}
    for player in legal:
        signature = (player.position, player.eligible_positions)
        template = feature_templates.get(signature)
        if template is None:
            complete = candidate_feature_values(
                player,
                config=config,
                round_number=round_number,
                overall_pick=overall_pick,
                roster_player_positions=tuple(row.position for row in roster),
                roster_player_eligibilities=tuple(row.eligible_positions for row in roster),
                available_position_counts=counts,
                picks_until_next=next_pick,
            )
            template = {
                name: value for name, value in complete.items()
                if not name.startswith("preseason.") and not name.startswith("bio.")
            }
            feature_templates[signature] = template
        features = dict(template)
        features.update(
            (f"preseason.{name}", value)
            for name, value in player.preseason_features.items()
        )
        features.update({
            "bio.bye_week": float(player.bye_week),
            "bio.experience_years": float(player.nfl_experience_years),
            "bio.rookie": float(player.rookie),
            "bio.first_year_on_team": float(player.first_year_on_team),
        })
        baseline, utility = brain.score_parts(
            {name: features.get(name) for name in brain.schema.names}
        )
        need = float(features.get("context.starter_need", 0.0) or 0.0)
        # The baseline is a regression ranker with minimal roster construction
        # discipline.  The neural residual is free to refine this policy.
        construction = 0.05 * abs(baseline) * need
        utility += construction
        ranked.append(RankedDraftCandidate(
            player.player_id,
            utility,
            baseline + construction,
            utility - baseline - construction,
            need,
            roster_counts[player.position],
        ))
    ranked.sort(key=lambda row: (-row.utility, _tie_break(seed, season.season, overall_pick, row.player_id)))
    return tuple(ranked)


def _validate_inputs(season, config, brains, seed) -> None:
    if not isinstance(season, HistoricalSeason) or not isinstance(config, DraftLeagueConfig):
        raise ValueError("season and config must use draft domain types")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if len(brains) != config.team_count or any(not isinstance(row, DraftBrain) for row in brains):
        raise ValueError("brains must contain exactly one DraftBrain per team")
    if any(row.league_config_fingerprint != config.config_id for row in brains):
        raise ValueError("every brain must match the league configuration")


def _strategy_assignment(config, strategies, seed) -> tuple[DraftStrategy, ...]:
    values = tuple(config.strategy_seats() if strategies is None else strategies)
    if len(values) != config.team_count or any(not isinstance(row, DraftStrategy) for row in values):
        raise ValueError("strategies must contain exactly one DraftStrategy per team")
    # Stable rotation exposes strategies to different draft slots without a mutable RNG.
    shift = seed % len(values)
    return values[shift:] + values[:shift]


def _global_context(
    all_rosters, all_strategies, config, players, current_roster, drafter_number
):
    if all_rosters is None or all_strategies is None:
        raise ValueError("global draft context requires every roster and strategy")
    try:
        rosters = tuple(tuple(row) for row in all_rosters)
        strategies = tuple(all_strategies)
    except TypeError:
        raise ValueError("global draft context is invalid") from None
    if len(rosters) != config.team_count or len(strategies) != config.team_count:
        raise ValueError("global draft context must contain every team")
    if any(not isinstance(row, DraftStrategy) for row in strategies):
        raise ValueError("global draft strategies are invalid")
    if tuple(current_roster) != rosters[drafter_number - 1]:
        raise ValueError("current roster does not match global draft context")
    flat = tuple(player_id for roster in rosters for player_id in roster)
    if len(set(flat)) != len(flat) or not set(flat).issubset(players):
        raise ValueError("global draft rosters contain invalid players")
    return rosters, strategies


def _can_complete(roster, picks_left, pool_can_fill, config, cache) -> bool:
    filled = filled_count(roster, config, cache)
    if len(config.starting_slots) - filled > picks_left:
        return False
    return pool_can_fill


def _filled_starter_count(roster_ids, season, config, cache) -> int:
    players = {row.player_id: row for row in season.players}
    return filled_count(
        tuple(players[player_id] for player_id in roster_ids),
        config,
        cache.feasibility,
    )


def _picks_until_next(team_count, overall_pick, drafter_number) -> int:
    round_number = (overall_pick - 1) // team_count + 1
    seat = drafter_number - 1
    if round_number % 2:
        return 2 * (team_count - seat) - 1
    return 2 * seat + 1


def _tie_break(seed, season, pick, player_id) -> str:
    payload = json.dumps([seed, season, pick, player_id], separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _shortlist(players, limit, config, cache):
    """Bound neural work while retaining leaders at every available position."""

    if len(players) <= limit:
        return players
    ordered = sorted(
        players,
        key=lambda row: (-_projection_hint(row, config, cache), row.player_id),
    )
    selected = ordered[:limit]
    # A global projection list can otherwise erase scarce TE/DST/K candidates.
    per_position = max(1, limit // 12)
    leaders = {
        player.player_id
        for position in {row.position for row in ordered}
        for player in [row for row in ordered if row.position == position][:per_position]
    }
    selected_ids = {row.player_id for row in selected}
    if not leaders.issubset(selected_ids):
        required = [row for row in ordered if row.player_id in leaders and row.player_id not in selected_ids]
        replaceable = [row for row in reversed(selected) if row.player_id not in leaders]
        for add, remove in zip(required, replaceable):
            selected.remove(remove)
            selected.append(add)
    return selected


def _projection_hint(player, config, cache):
    known = cache.projection_hints.get(player.player_id)
    if known is not None:
        return known
    for name in ("projected_fantasy_points", "projected_points"):
        value = resolve_preseason_projection(player, name)
        if value is not None:
            cache.projection_hints[player.player_id] = value
            return value
    weighted = 0.0
    for name, weight in config.scoring_weights.items():
        semantic_name = name if name.startswith("projected_stat.") else f"projected_stat.{name}"
        value = resolve_preseason_projection(player, semantic_name)
        if value is not None:
            weighted += value * weight
    for name in ("ecr_rank", "overall_rank", "rank", "adp"):
        value = resolve_preseason_projection(player, name)
        if value is not None:
            weighted += 1_000.0 / (1.0 + max(0.0, value))
    cache.projection_hints[player.player_id] = weighted
    return weighted


__all__ = (
    "DraftPick", "DraftResult", "RankedDraftCandidate", "rank_draft_candidates",
    "simulate_snake_draft",
)
