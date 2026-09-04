"""Typed reserve-slot rules shared by trade counting and roster adjustment."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


SUPPORTED_RESERVE_SLOTS = frozenset({"IR", "ROOKIE_RESERVE"})


@dataclass(frozen=True, slots=True)
class PostTradeCapacityPlan:
    """The minimum legal cuts implied by one raw post-trade roster."""

    active_before_cuts: int
    required_cuts: int
    cuttable_active_reductions: int
    feasible: bool


def normalize_reserve_slot_counts(value: object) -> Mapping[str, int]:
    """Return an immutable, canonical reserve-kind-to-capacity mapping."""

    if not isinstance(value, Mapping):
        raise ValueError("reserve_slot_counts must be a mapping")
    result: dict[str, int] = {}
    for raw_kind, count in value.items():
        kind = _reserve_kind(raw_kind)
        if kind in result:
            raise ValueError("reserve_slot_counts contains a duplicate reserve kind")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("reserve slot capacity must be a positive integer")
        result[kind] = count
    return MappingProxyType(dict(sorted(result.items())))


def normalize_reserve_slot_by_player(
    value: object,
    *,
    owned_player_ids: Iterable[str] | None = None,
    reserve_slot_counts: Mapping[str, int] | None = None,
) -> Mapping[str, str]:
    """Return immutable current placements, optionally validating ownership/capacity."""

    if not isinstance(value, Mapping):
        raise ValueError("reserve_slot_by_player must be a mapping")
    result: dict[str, str] = {}
    for player_id, raw_kind in value.items():
        if not isinstance(player_id, str) or not player_id:
            raise ValueError("reserve_slot_by_player keys must be non-empty player IDs")
        result[player_id] = _reserve_kind(raw_kind)
    if owned_player_ids is not None:
        owned = frozenset(owned_player_ids)
        if not set(result).issubset(owned):
            raise ValueError("reserve-slot players must be owned by the team")
    if reserve_slot_counts is not None:
        counts = normalize_reserve_slot_counts(reserve_slot_counts)
        unknown = set(result.values()).difference(counts)
        if unknown:
            raise ValueError(
                f"reserve placement uses unconfigured slot {min(unknown)!r}"
            )
        occupancy = Counter(result.values())
        if any(occupancy[kind] > capacity for kind, capacity in counts.items()):
            raise ValueError("reserve occupancy exceeds its captured slot capacity")
    return MappingProxyType(dict(sorted(result.items())))


def reserve_counts_for(
    player_ids: Iterable[str], reserve_slot_by_player: Mapping[str, str]
) -> dict[str, int]:
    """Count the current typed placements among a player package."""

    result: Counter[str] = Counter()
    for player_id in player_ids:
        kind = reserve_slot_by_player.get(player_id)
        if kind is not None:
            result[kind] += 1
    return dict(result)


def solve_post_trade_capacity(
    *,
    active_cap: int,
    current_size: int,
    known_player_count: int,
    reserve_slot_counts: Mapping[str, int],
    current_reserve_counts: Mapping[str, int],
    outgoing_size: int,
    outgoing_reserve_counts: Mapping[str, int],
    incoming_size: int,
    incoming_reserve_counts: Mapping[str, int],
) -> PostTradeCapacityPlan:
    """Solve roster legality without player enumeration.

    Only a player's actual pre-trade reserve placement is transferable.  A
    transferred reserve player can occupy only the same reserve kind; overflow
    consumes ordinary active capacity.
    """

    capacities = dict(reserve_slot_counts)
    current = Counter(current_reserve_counts)
    outgoing = Counter(outgoing_reserve_counts)
    incoming = Counter(incoming_reserve_counts)
    retained = current - outgoing
    candidates = retained + incoming
    occupied = sum(
        min(count, capacities.get(kind, 0)) for kind, count in candidates.items()
    )
    total_after = current_size - outgoing_size + incoming_size
    active_before_cuts = total_after - occupied
    required_cuts = max(0, active_before_cuts - active_cap)

    known_active = known_player_count - sum(current.values())
    outgoing_active = outgoing_size - sum(outgoing.values())
    retained_known_active = known_active - outgoing_active
    reserve_overflow_reductions = sum(
        min(retained.get(kind, 0), max(0, count - capacities.get(kind, 0)))
        for kind, count in candidates.items()
    )
    cuttable = retained_known_active + reserve_overflow_reductions
    return PostTradeCapacityPlan(
        active_before_cuts=active_before_cuts,
        required_cuts=required_cuts,
        cuttable_active_reductions=cuttable,
        feasible=required_cuts <= cuttable,
    )


def assign_reserve_slots(
    reserve_candidates_by_player: Mapping[str, str],
    reserve_slot_counts: Mapping[str, int],
    player_order: Iterable[str],
) -> Mapping[str, str]:
    """Assign same-kind reserve candidates deterministically up to capacity."""

    remaining = dict(reserve_slot_counts)
    assigned: dict[str, str] = {}
    for player_id in player_order:
        kind = reserve_candidates_by_player.get(player_id)
        if kind is None or remaining.get(kind, 0) <= 0:
            continue
        assigned[player_id] = kind
        remaining[kind] -= 1
    return MappingProxyType(dict(sorted(assigned.items())))


def signature_record(
    reserve_kinds: tuple[str, ...], signature: tuple[int, ...]
) -> dict[str, int]:
    return {
        kind: count
        for kind, count in zip(reserve_kinds, signature, strict=True)
        if count
    }


def _reserve_kind(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reserve slot kind must be a non-empty string")
    kind = value.strip().upper().replace(" ", "_")
    if kind not in SUPPORTED_RESERVE_SLOTS:
        raise ValueError(f"unsupported reserve slot kind {value!r}")
    return kind


__all__ = (
    "PostTradeCapacityPlan",
    "SUPPORTED_RESERVE_SLOTS",
    "assign_reserve_slots",
    "normalize_reserve_slot_by_player",
    "normalize_reserve_slot_counts",
    "reserve_counts_for",
    "signature_record",
    "solve_post_trade_capacity",
)

