"""Exact package matching and counting for atomic and Boolean trade filters."""

from collections.abc import Iterable, Mapping
from itertools import combinations

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
        reserve_slot_by_player: Mapping[PlayerId, str],
    ) -> None:
        self._available_ids = available_player_ids
        self._legacy_pool = (
            _LegacyTradePackagePool(
                available_player_ids,
                package_filter,
                eligible_positions_by_player,
                reserve_slot_by_player,
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
        self._reserve_slot_by_player = dict(reserve_slot_by_player)
        self._compiled_package_cache: dict[int, _ReplayablePackages] = {}

    def count_by_reserve_signature(
        self,
        package_size: int,
        reserve_kinds: tuple[str, ...],
    ) -> dict[tuple[int, ...], int]:
        """Count matching packages grouped by their typed reserve occupants."""

        _nonnegative_int("package_size", package_size)
        if len(set(reserve_kinds)) != len(reserve_kinds):
            raise ValueError("reserve_kinds cannot contain duplicates")
        if package_size > len(self._available_ids):
            return {}
        if self._legacy_pool is not None:
            return self._legacy_pool.count_by_reserve_signature(
                package_size, reserve_kinds
            )
        kind_index = {kind: index for index, kind in enumerate(reserve_kinds)}
        states = {(0, (0,) * len(reserve_kinds), 0): 1}
        for player_id in self._available_ids:
            updated = dict(states)
            reserve_kind = self._reserve_slot_by_player.get(player_id)
            evidence = (
                0
                if self._compiled_filter is None
                else self._compiled_filter.evidence_by_player[player_id]
            )
            for (chosen, signature, mask), count in states.items():
                if chosen >= package_size:
                    continue
                incremented = list(signature)
                if reserve_kind is not None:
                    incremented[kind_index[reserve_kind]] += 1
                key = (
                    chosen + 1,
                    tuple(incremented),
                    mask | evidence,
                )
                updated[key] = updated.get(key, 0) + count
            states = updated
        result: dict[tuple[int, ...], int] = {}
        for (chosen, signature, mask), count in states.items():
            if chosen != package_size:
                continue
            if self._compiled_filter is not None and not self._compiled_filter.matches(mask):
                continue
            result[signature] = result.get(signature, 0) + count
        return result

    def iter_packages(self, package_size: int) -> Iterable[tuple[PlayerId, ...]]:
        """Yield matching packages in deterministic roster-combination order."""

        _nonnegative_int("package_size", package_size)
        if package_size > len(self._available_ids):
            return
        if self._legacy_pool is not None:
            yield from self._legacy_pool.iter_packages(package_size)
            return
        if self._compiled_filter is not None:
            packages = self._compiled_package_cache.get(package_size)
            if packages is None:
                packages = _ReplayablePackages(
                    self._iter_compiled_packages(package_size)
                )
                self._compiled_package_cache[package_size] = packages
            yield from packages
            return
        yield from combinations(self._available_ids, package_size)

    def _iter_compiled_packages(
        self, package_size: int
    ) -> Iterable[tuple[PlayerId, ...]]:
        if self._compiled_filter is None:
            raise AssertionError("a compiled package scan requires an expression")
        for package in combinations(self._available_ids, package_size):
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
        reserve_slot_by_player: Mapping[PlayerId, str],
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
        self._reserve_slot_by_player = dict(reserve_slot_by_player)
        self._position_masks = masks
        self._target_position_mask = (
            sum(position_bits.values())
            if rule.position_mode is TradeFilterMode.INCLUDE
            else 0
        )
        self._required_position_mask = _coverage(required_ids, masks)
        self._possible = possible

    def count_by_reserve_signature(
        self,
        package_size: int,
        reserve_kinds: tuple[str, ...],
    ) -> dict[tuple[int, ...], int]:
        optional_count = package_size - len(self._required_ids)
        if (
            not self._possible
            or optional_count < 0
            or optional_count > len(self._optional_ids)
        ):
            return {}
        kind_index = {kind: index for index, kind in enumerate(reserve_kinds)}
        required_signature = [0] * len(reserve_kinds)
        for player_id in self._required_ids:
            kind = self._reserve_slot_by_player.get(player_id)
            if kind is not None:
                required_signature[kind_index[kind]] += 1
        states = {
            (0, tuple(required_signature), self._required_position_mask): 1
        }
        for player_id in self._optional_ids:
            updated = dict(states)
            reserve_kind = self._reserve_slot_by_player.get(player_id)
            coverage_increment = self._position_masks[player_id]
            for (chosen, signature, coverage), count in states.items():
                if chosen >= optional_count:
                    continue
                incremented = list(signature)
                if reserve_kind is not None:
                    incremented[kind_index[reserve_kind]] += 1
                key = (
                    chosen + 1,
                    tuple(incremented),
                    coverage | coverage_increment,
                )
                updated[key] = updated.get(key, 0) + count
            states = updated
        result: dict[tuple[int, ...], int] = {}
        for (chosen, signature, coverage), count in states.items():
            if chosen != optional_count:
                continue
            if coverage & self._target_position_mask != self._target_position_mask:
                continue
            result[signature] = result.get(signature, 0) + count
        return result

    def iter_packages(self, package_size: int) -> Iterable[tuple[PlayerId, ...]]:
        optional_count = package_size - len(self._required_ids)
        if (
            not self._possible
            or optional_count < 0
            or optional_count > len(self._optional_ids)
        ):
            return
        for optional in combinations(self._optional_ids, optional_count):
            selected = self._required_set.union(optional)
            package = tuple(
                player_id for player_id in self._allowed_ids if player_id in selected
            )
            if (
                _coverage(package, self._position_masks)
                & self._target_position_mask
                != self._target_position_mask
            ):
                continue
            yield package

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
