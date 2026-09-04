"""Exact package matching and counting for atomic and Boolean trade filters."""

from collections.abc import Iterable, Mapping
from itertools import combinations
from math import comb

from .trade_filter_compiler import (
    CompiledTradeFilter,
    compile_trade_filter,
    positions_for_filter,
)
from .trade_filters import (
    TradeFilterExpression,
    TradeFilterMode,
    TradePackageExpression,
    TradePackageFilter,
)


PlayerId = str


class TradePackagePool:
    """Count and lazily iterate matching packages from one ordered roster side."""

    def __init__(
        self,
        available_player_ids: tuple[PlayerId, ...],
        package_filter: TradePackageExpression | None,
        eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None,
        capacity_exempt_player_ids: frozenset[PlayerId],
    ) -> None:
        self._available_ids = available_player_ids
        self._legacy_pool = (
            _LegacyTradePackagePool(
                available_player_ids,
                package_filter,
                eligible_positions_by_player,
                capacity_exempt_player_ids,
            )
            if isinstance(package_filter, TradePackageFilter)
            else None
        )
        self._compiled_filter = (
            compile_trade_filter(
                package_filter,
                available_player_ids,
                eligible_positions_by_player,
            )
            if isinstance(package_filter, TradeFilterExpression)
            else None
        )
        self._capacity_exempt_ids = capacity_exempt_player_ids
        self._compiled_package_cache: dict[
            tuple[int, int], _ReplayablePackages
        ] = {}

    def count(self, package_size: int, *, minimum_active: int = 0) -> int:
        """Return an exact count without constructing package combinations."""

        _nonnegative_int("package_size", package_size)
        _nonnegative_int("minimum_active", minimum_active)
        if package_size > len(self._available_ids) or minimum_active > package_size:
            return 0
        if self._legacy_pool is not None:
            return self._legacy_pool.count(
                package_size, minimum_active=minimum_active
            )
        if self._compiled_filter is None:
            return _count_by_active(
                self._available_ids,
                self._capacity_exempt_ids,
                package_size,
                minimum_active,
            )
        states = {(0, 0, 0): 1}
        for player_id in self._available_ids:
            updated = dict(states)
            active_increment = int(player_id not in self._capacity_exempt_ids)
            evidence = self._compiled_filter.evidence_by_player[player_id]
            for (chosen, active, mask), count in states.items():
                if chosen >= package_size:
                    continue
                key = (
                    chosen + 1,
                    min(minimum_active, active + active_increment),
                    mask | evidence,
                )
                updated[key] = updated.get(key, 0) + count
            states = updated
        return sum(
            count
            for (chosen, active, mask), count in states.items()
            if chosen == package_size
            and active == minimum_active
            and self._compiled_filter.matches(mask)
        )

    def iter_packages(
        self, package_size: int, *, minimum_active: int = 0
    ) -> Iterable[tuple[PlayerId, ...]]:
        """Yield matching packages in deterministic roster-combination order."""

        _nonnegative_int("package_size", package_size)
        _nonnegative_int("minimum_active", minimum_active)
        if package_size > len(self._available_ids) or minimum_active > package_size:
            return
        if self._legacy_pool is not None:
            yield from self._legacy_pool.iter_packages(
                package_size, minimum_active=minimum_active
            )
            return
        if self._compiled_filter is not None:
            key = (package_size, minimum_active)
            packages = self._compiled_package_cache.get(key)
            if packages is None:
                packages = _ReplayablePackages(
                    self._iter_compiled_packages(package_size, minimum_active)
                )
                self._compiled_package_cache[key] = packages
            yield from packages
            return
        check_active = _needs_active_check(
            package_size, minimum_active, self._capacity_exempt_ids
        )
        for package in combinations(self._available_ids, package_size):
            if check_active and sum(
                player_id not in self._capacity_exempt_ids for player_id in package
            ) < minimum_active:
                continue
            yield package

    def _iter_compiled_packages(
        self, package_size: int, minimum_active: int
    ) -> Iterable[tuple[PlayerId, ...]]:
        if self._compiled_filter is None:
            raise AssertionError("a compiled package scan requires an expression")
        check_active = _needs_active_check(
            package_size, minimum_active, self._capacity_exempt_ids
        )
        for package in combinations(self._available_ids, package_size):
            if check_active and sum(
                player_id not in self._capacity_exempt_ids for player_id in package
            ) < minimum_active:
                continue
            evidence = self._compiled_filter.evidence_for(package)
            if self._compiled_filter.matches(evidence):
                yield package


class _ReplayablePackages:
    """Lazily scan once, then replay matching packages in the same order."""

    def __init__(self, source: Iterable[tuple[PlayerId, ...]]) -> None:
        self._source = iter(source)
        self._cache: list[tuple[PlayerId, ...]] = []
        self._complete = False

    def __iter__(self) -> Iterable[tuple[PlayerId, ...]]:
        index = 0
        while True:
            if index < len(self._cache):
                yield self._cache[index]
                index += 1
                continue
            if self._complete:
                return
            try:
                package = next(self._source)
            except StopIteration:
                self._complete = True
                return
            self._cache.append(package)
            index += 1
            yield package


class _LegacyTradePackagePool:
    """Preserve the pruned v1 package plan and its combinatorial count path."""

    def __init__(
        self,
        available_player_ids: tuple[PlayerId, ...],
        rule: TradePackageFilter,
        eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None,
        capacity_exempt_player_ids: frozenset[PlayerId],
    ) -> None:
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

    def count(self, package_size: int, *, minimum_active: int) -> int:
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
        self, package_size: int, *, minimum_active: int
    ) -> Iterable[tuple[PlayerId, ...]]:
        optional_count = package_size - len(self._required_ids)
        if (
            not self._possible
            or optional_count < 0
            or optional_count > len(self._optional_ids)
            or minimum_active > package_size
        ):
            return
        check_active = _needs_active_check(
            package_size, minimum_active, self._capacity_exempt_ids
        )
        for optional in combinations(self._optional_ids, optional_count):
            selected = self._required_set.union(optional)
            package = tuple(
                player_id for player_id in self._allowed_ids if player_id in selected
            )
            if check_active and sum(
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
    positions = positions_for_filter(tuple(allowed), eligible_positions_by_player)
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


def _needs_active_check(
    package_size: int,
    minimum_active: int,
    capacity_exempt_player_ids: frozenset[PlayerId],
) -> bool:
    """Return whether any package could contain too few active players."""

    return len(capacity_exempt_player_ids) > package_size - minimum_active


def _coverage(
    player_ids: Iterable[PlayerId], position_masks: Mapping[PlayerId, int]
) -> int:
    result = 0
    for player_id in player_ids:
        result |= position_masks.get(player_id, 0)
    return result


def _nonnegative_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = ("TradePackagePool",)
