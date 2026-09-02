from dataclasses import dataclass
from math import fsum, isfinite
from numbers import Real
from types import MappingProxyType
from typing import Hashable, Mapping, Sequence


@dataclass(frozen=True)
class LineupPlayer:
    """A player and the weight earned in each eligible lineup slot."""

    player_id: Hashable
    slot_weights: Mapping[str, float]

    def __post_init__(self) -> None:
        try:
            hash(self.player_id)
        except TypeError:
            raise ValueError("player_id must be hashable") from None
        if self.player_id is None:
            raise ValueError("player_id cannot be None")
        if not isinstance(self.slot_weights, Mapping):
            raise ValueError("slot_weights must be a mapping")

        normalized_weights: dict[str, float] = {}
        for slot, weight in self.slot_weights.items():
            _require_slot_name(slot)
            if isinstance(weight, bool) or not isinstance(weight, Real):
                raise ValueError("every slot weight must be a finite number")
            try:
                normalized_weight = float(weight)
            except (OverflowError, TypeError, ValueError):
                raise ValueError("every slot weight must be a finite number") from None
            if not isfinite(normalized_weight):
                raise ValueError("every slot weight must be a finite number")
            normalized_weights[slot] = normalized_weight
        object.__setattr__(
            self,
            "slot_weights",
            MappingProxyType(normalized_weights),
        )


@dataclass(frozen=True)
class LineupAssignment:
    """The selected player, if any, for one distinct lineup position."""

    slot_index: int
    slot: str
    player_id: Hashable | None
    weight: float


@dataclass(frozen=True)
class LineupResult:
    assignments: tuple[LineupAssignment, ...]
    total_weight: float


@dataclass(frozen=True)
class _State:
    assignments: tuple[int | None, ...]
    total_weight: float


def optimize_lineup(
    slots: Sequence[str],
    players: Sequence[LineupPlayer],
) -> LineupResult:
    """Return the exact maximum-weight legal lineup.

    Input player order is the stable priority for otherwise equal solutions;
    earlier players are assigned to earlier lineup positions first.
    """

    lineup_slots = _normalize_slots(slots)
    lineup_players = _normalize_players(players)
    player_count = len(lineup_players)
    empty_assignments: tuple[int | None, ...] = (None,) * len(lineup_slots)
    states = {0: _State(empty_assignments, 0.0)}

    for player_index, player in enumerate(lineup_players):
        next_states = dict(states)  # Leaving the player on the bench is always legal.
        for occupied_mask, state in states.items():
            considered_slots: set[str] = set()
            for slot_index, slot in enumerate(lineup_slots):
                if occupied_mask & (1 << slot_index) or slot in considered_slots:
                    continue
                # Equal-named positions are interchangeable; always using the
                # first open one removes symmetric states while preserving the
                # deterministic slot-order tie-break.
                considered_slots.add(slot)
                weight = player.slot_weights.get(slot)
                if weight is None or weight < 0:
                    continue
                assignments = list(state.assignments)
                assignments[slot_index] = player_index
                candidate_assignments = tuple(assignments)
                candidate = _State(
                    candidate_assignments,
                    _assignment_total(candidate_assignments, lineup_slots, lineup_players),
                )
                candidate_mask = occupied_mask | (1 << slot_index)
                incumbent = next_states.get(candidate_mask)
                if incumbent is None or _is_better(candidate, incumbent, player_count):
                    next_states[candidate_mask] = candidate
        states = next_states

    best = _State(empty_assignments, 0.0)
    for state in states.values():
        if _is_better(state, best, player_count):
            best = state

    assignments = tuple(
        _public_assignment(index, slot, player_index, lineup_players)
        for index, (slot, player_index) in enumerate(zip(lineup_slots, best.assignments))
    )
    return LineupResult(assignments, best.total_weight)


def _normalize_slots(slots: Sequence[str]) -> tuple[str, ...]:
    if isinstance(slots, (str, bytes)):
        raise ValueError("slots must be a sequence of slot names")
    try:
        normalized = tuple(slots)
    except TypeError:
        raise ValueError("slots must be a sequence of slot names") from None
    for slot in normalized:
        _require_slot_name(slot)
    return normalized


def _normalize_players(players: Sequence[LineupPlayer]) -> tuple[LineupPlayer, ...]:
    if isinstance(players, (str, bytes)):
        raise ValueError("players must be a sequence of LineupPlayer values")
    try:
        normalized = tuple(players)
    except TypeError:
        raise ValueError("players must be a sequence of LineupPlayer values") from None

    seen_ids: set[Hashable] = set()
    for player in normalized:
        if not isinstance(player, LineupPlayer):
            raise ValueError("players must contain only LineupPlayer values")
        if player.player_id in seen_ids:
            raise ValueError("players contain a duplicate player_id")
        seen_ids.add(player.player_id)
    return normalized


def _assignment_total(
    assignments: tuple[int | None, ...],
    slots: tuple[str, ...],
    players: tuple[LineupPlayer, ...],
) -> float:
    try:
        total = fsum(
            players[player_index].slot_weights[slot]
            for slot, player_index in zip(slots, assignments)
            if player_index is not None
        )
    except OverflowError:
        raise ValueError("lineup weights produce a non-finite total") from None
    if not isfinite(total):
        raise ValueError("lineup weights produce a non-finite total")
    return total


def _is_better(candidate: _State, incumbent: _State, player_count: int) -> bool:
    if candidate.total_weight != incumbent.total_weight:
        return candidate.total_weight > incumbent.total_weight
    return _tie_key(candidate.assignments, player_count) < _tie_key(
        incumbent.assignments,
        player_count,
    )


def _tie_key(assignments: tuple[int | None, ...], player_count: int) -> tuple[int, ...]:
    return tuple(
        player_count if player_index is None else player_index
        for player_index in assignments
    )


def _public_assignment(
    slot_index: int,
    slot: str,
    player_index: int | None,
    players: tuple[LineupPlayer, ...],
) -> LineupAssignment:
    if player_index is None:
        return LineupAssignment(slot_index, slot, None, 0.0)
    player = players[player_index]
    return LineupAssignment(
        slot_index,
        slot,
        player.player_id,
        player.slot_weights[slot],
    )


def _require_slot_name(slot: object) -> None:
    if not isinstance(slot, str) or not slot:
        raise ValueError("slot names must be non-empty strings")
