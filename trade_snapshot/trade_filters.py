"""Side-specific player and position rules for local trade packages."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from math import comb
from typing import Iterator

from .positions import normalize_player_position


PlayerId = str
TRADE_FILTER_SEMANTICS_VERSION = 1


class TradeFilterMode(str, Enum):
    """How one selected player or position set constrains a trade package."""

    INCLUDE = "include"
    ONLY = "only"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True)
class TradePackageFilter:
    """Independent player and position rules for one side of a trade.

    Active player and position dimensions are combined with AND.  Player
    ``include`` requires every selected ID, ``only`` requires exactly the
    selected ID set, and ``exclude`` forbids every selected ID.  Position
    ``include`` requires every selected position to be represented, ``only``
    treats the selected positions as an allowed set for every player, and
    ``exclude`` forbids players eligible at any selected position.

    A dimension with no selected values or no mode is disabled.  A player with
    multiple canonical positions matches a position rule when the two sets
    intersect; one such player can represent multiple requested positions.
    """

    player_ids: frozenset[PlayerId] = field(default_factory=frozenset)
    player_mode: TradeFilterMode | str | None = None
    positions: frozenset[str] = field(default_factory=frozenset)
    position_mode: TradeFilterMode | str | None = None

    def __post_init__(self) -> None:
        player_ids = _string_set("player_ids", self.player_ids)
        positions = _position_set(self.positions)
        player_mode = _mode("player_mode", self.player_mode) if player_ids else None
        position_mode = (
            _mode("position_mode", self.position_mode) if positions else None
        )
        if player_mode is None:
            player_ids = frozenset()
        if position_mode is None:
            positions = frozenset()
        object.__setattr__(self, "player_ids", player_ids)
        object.__setattr__(self, "player_mode", player_mode)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "position_mode", position_mode)

    @property
    def active(self) -> bool:
        return self.player_mode is not None or self.position_mode is not None

    def to_record(self) -> dict[str, object]:
        """Return the canonical JSON-ready representation used in run identities."""

        return {
            "player_ids": sorted(self.player_ids),
            "player_mode": (
                None if self.player_mode is None else self.player_mode.value
            ),
            "positions": sorted(self.positions),
            "position_mode": (
                None if self.position_mode is None else self.position_mode.value
            ),
        }


class TradePackagePool:
    """Count and lazily iterate valid packages from one ordered roster side."""

    def __init__(
        self,
        available_player_ids: tuple[PlayerId, ...],
        package_filter: TradePackageFilter | None,
        eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None,
        capacity_exempt_player_ids: frozenset[PlayerId],
    ) -> None:
        rule = package_filter or TradePackageFilter()
        allowed, required, possible = _apply_player_rule(
            available_player_ids, rule
        )
        allowed, positions_by_player, possible = _apply_position_rule(
            allowed,
            required,
            possible,
            rule,
            eligible_positions_by_player,
        )

        position_bits = {
            position: 1 << index
            for index, position in enumerate(sorted(rule.positions))
        }
        masks = {
            player_id: sum(
                bit
                for position, bit in position_bits.items()
                if position in positions_by_player.get(player_id, ())
            )
            for player_id in allowed
        }
        required_ids = tuple(
            player_id for player_id in allowed if player_id in required
        )
        required_set = frozenset(required_ids)
        self._allowed_ids = tuple(allowed)
        self._required_ids = required_ids
        self._required_set = required_set
        self._optional_ids = tuple(
            player_id for player_id in allowed if player_id not in required_set
        )
        self._capacity_exempt_ids = capacity_exempt_player_ids
        self._position_masks = masks
        self._target_position_mask = (
            sum(position_bits.values())
            if rule.position_mode is TradeFilterMode.INCLUDE
            else 0
        )
        self._required_position_mask = _coverage(required_ids, masks)
        self._possible = possible

    def count(self, package_size: int, *, minimum_active: int = 0) -> int:
        """Return an exact count without constructing package combinations."""

        _nonnegative_int("package_size", package_size)
        _nonnegative_int("minimum_active", minimum_active)
        optional_count = package_size - len(self._required_ids)
        if (
            not self._possible
            or optional_count < 0
            or optional_count > len(self._optional_ids)
            or minimum_active > package_size
        ):
            return 0
        required_active = sum(
            player_id not in self._capacity_exempt_ids
            for player_id in self._required_ids
        )
        needed_active = max(0, minimum_active - required_active)
        missing_positions = (
            self._target_position_mask & ~self._required_position_mask
        )
        if not missing_positions:
            return _count_by_active(
                self._optional_ids,
                self._capacity_exempt_ids,
                optional_count,
                needed_active,
            )
        return self._count_with_position_coverage(
            optional_count, needed_active, missing_positions
        )

    def iter_packages(
        self, package_size: int, *, minimum_active: int = 0
    ) -> Iterator[tuple[PlayerId, ...]]:
        """Yield matching packages in the roster's deterministic combination order."""

        _nonnegative_int("package_size", package_size)
        _nonnegative_int("minimum_active", minimum_active)
        optional_count = package_size - len(self._required_ids)
        if (
            not self._possible
            or optional_count < 0
            or optional_count > len(self._optional_ids)
            or minimum_active > package_size
        ):
            return
        for optional in combinations(self._optional_ids, optional_count):
            selected = self._required_set.union(optional)
            package = tuple(
                player_id for player_id in self._allowed_ids if player_id in selected
            )
            if sum(
                player_id not in self._capacity_exempt_ids for player_id in package
            ) < minimum_active:
                continue
            if (
                _coverage(package, self._position_masks)
                & self._target_position_mask
                != self._target_position_mask
            ):
                continue
            yield package

    def _count_with_position_coverage(
        self,
        optional_count: int,
        needed_active: int,
        missing_positions: int,
    ) -> int:
        states = {(0, 0, 0): 1}
        for player_id in self._optional_ids:
            updated = dict(states)
            active_increment = int(player_id not in self._capacity_exempt_ids)
            coverage_increment = self._position_masks[player_id] & missing_positions
            for (chosen, active, coverage), count in states.items():
                if chosen >= optional_count:
                    continue
                key = (
                    chosen + 1,
                    min(needed_active, active + active_increment),
                    coverage | coverage_increment,
                )
                updated[key] = updated.get(key, 0) + count
            states = updated
        return states.get((optional_count, needed_active, missing_positions), 0)


def _positions_for(
    player_ids: tuple[PlayerId, ...],
    values: Mapping[PlayerId, Iterable[str]] | None,
) -> dict[PlayerId, frozenset[str]]:
    if not isinstance(values, Mapping):
        raise ValueError(
            "eligible_positions_by_player is required for an active position filter"
        )
    result = {}
    for player_id in player_ids:
        if player_id not in values:
            raise ValueError(
                f"eligible_positions_by_player is missing player {player_id!r}"
            )
        positions = values[player_id]
        if isinstance(positions, (str, bytes)):
            raise ValueError(
                "eligible_positions_by_player values must be position iterables"
            )
        try:
            normalized = frozenset(
                normalize_player_position(position) for position in positions
            )
        except TypeError:
            raise ValueError(
                "eligible_positions_by_player values must be position iterables"
            ) from None
        if not normalized:
            raise ValueError(
                "eligible_positions_by_player values cannot be empty"
            )
        result[player_id] = normalized
    return result


def _apply_player_rule(
    available_player_ids: tuple[PlayerId, ...], rule: TradePackageFilter
) -> tuple[list[PlayerId], frozenset[PlayerId], bool]:
    allowed = list(available_player_ids)
    available = frozenset(allowed)
    required: frozenset[PlayerId] = frozenset()
    if rule.player_mode is TradeFilterMode.INCLUDE:
        required = rule.player_ids
        return allowed, required, required.issubset(available)
    if rule.player_mode is TradeFilterMode.ONLY:
        required = rule.player_ids
        allowed = [player_id for player_id in allowed if player_id in required]
        return allowed, required, required.issubset(available)
    if rule.player_mode is TradeFilterMode.EXCLUDE:
        allowed = [
            player_id for player_id in allowed if player_id not in rule.player_ids
        ]
    return allowed, required, True


def _apply_position_rule(
    allowed: list[PlayerId],
    required: frozenset[PlayerId],
    possible: bool,
    rule: TradePackageFilter,
    eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None,
) -> tuple[list[PlayerId], dict[PlayerId, frozenset[str]], bool]:
    if rule.position_mode is None or not possible:
        return allowed, {}, possible
    positions = _positions_for(tuple(allowed), eligible_positions_by_player)
    if rule.position_mode is TradeFilterMode.ONLY:
        allowed = [
            player_id
            for player_id in allowed
            if positions[player_id].intersection(rule.positions)
        ]
    elif rule.position_mode is TradeFilterMode.EXCLUDE:
        allowed = [
            player_id
            for player_id in allowed
            if not positions[player_id].intersection(rule.positions)
        ]
    return allowed, positions, required.issubset(allowed)


def _count_by_active(
    player_ids: tuple[PlayerId, ...],
    capacity_exempt_player_ids: frozenset[PlayerId],
    package_size: int,
    minimum_active: int,
) -> int:
    exempt_count = sum(
        player_id in capacity_exempt_player_ids for player_id in player_ids
    )
    active_count = len(player_ids) - exempt_count
    lower = max(0, package_size - exempt_count, minimum_active)
    upper = min(package_size, active_count)
    return sum(
        comb(active_count, active)
        * comb(exempt_count, package_size - active)
        for active in range(lower, upper + 1)
    )


def _coverage(
    player_ids: Iterable[PlayerId], position_masks: Mapping[PlayerId, int]
) -> int:
    result = 0
    for player_id in player_ids:
        result |= position_masks.get(player_id, 0)
    return result


def _string_set(name: str, values: object) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must contain non-empty strings")
    try:
        result = frozenset(values)
    except TypeError:
        raise ValueError(f"{name} must contain non-empty strings") from None
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


def _position_set(values: object) -> frozenset[str]:
    positions = _string_set("positions", values)
    return frozenset(
        normalize_player_position(position, require_supported=True)
        for position in positions
    )


def _mode(name: str, value: object) -> TradeFilterMode | None:
    if value is None:
        return None
    try:
        return TradeFilterMode(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be include, only, exclude, or null") from None


def _nonnegative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = (
    "TRADE_FILTER_SEMANTICS_VERSION",
    "TradeFilterMode",
    "TradePackageFilter",
)
