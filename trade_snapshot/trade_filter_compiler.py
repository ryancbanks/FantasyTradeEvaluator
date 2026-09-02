"""Compile recursive trade filters into exact per-player evidence."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .positions import normalize_player_position
from .trade_filters import (
    TradeFilterMode,
    TradeFilterOperator,
    TradePackageExpression,
    TradePackageFilter,
)


PlayerId = str


@dataclass(frozen=True, slots=True)
class _CompiledLeaf:
    required_evidence: int
    forbidden_evidence: int

    def matches(self, evidence: int) -> bool:
        return (
            evidence & self.required_evidence == self.required_evidence
            and not evidence & self.forbidden_evidence
        )


@dataclass(frozen=True, slots=True)
class CompiledTradeFilter:
    """A filter compiled to monotone package-evidence bits and a Boolean formula."""

    evidence_by_player: Mapping[PlayerId, int]
    leaves: tuple[_CompiledLeaf, ...]
    formula: object

    def evidence_for(self, player_ids: Iterable[PlayerId]) -> int:
        evidence = 0
        for player_id in player_ids:
            evidence |= self.evidence_by_player.get(player_id, 0)
        return evidence

    def matches(self, evidence: int) -> bool:
        values = tuple(row.matches(evidence) for row in self.leaves)
        return _evaluate_formula(self.formula, values)


def compile_trade_filter(
    value: TradePackageExpression | None,
    universe: tuple[PlayerId, ...],
    eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None,
) -> CompiledTradeFilter | None:
    """Compile final-package predicates without changing their truth semantics."""

    if value is None:
        return None
    leaves: list[TradePackageFilter] = []
    leaf_indexes: dict[TradePackageFilter, int] = {}
    formula = _compile_formula(value, leaves, leaf_indexes)
    evidence_by_player = {player_id: 0 for player_id in universe}
    available = frozenset(universe)
    conditions = []
    next_bit = 0

    for rule in leaves:
        required = 0
        forbidden = 0

        def add_evidence(player_ids: Iterable[PlayerId], bit: int) -> None:
            for player_id in player_ids:
                if player_id in evidence_by_player:
                    evidence_by_player[player_id] |= bit

        def new_bit() -> int:
            nonlocal next_bit
            result = 1 << next_bit
            next_bit += 1
            return result

        if rule.player_mode in {TradeFilterMode.INCLUDE, TradeFilterMode.ONLY}:
            for player_id in sorted(rule.player_ids):
                bit = new_bit()
                required |= bit
                add_evidence((player_id,), bit)

        player_forbidden: frozenset[PlayerId] = frozenset()
        if rule.player_mode is TradeFilterMode.EXCLUDE:
            player_forbidden = rule.player_ids.intersection(available)
        elif rule.player_mode is TradeFilterMode.ONLY:
            player_forbidden = available.difference(rule.player_ids)
        if player_forbidden:
            bit = new_bit()
            forbidden |= bit
            add_evidence(player_forbidden, bit)

        missing_required = (
            rule.player_mode in {TradeFilterMode.INCLUDE, TradeFilterMode.ONLY}
            and not rule.player_ids.issubset(available)
        )
        if rule.position_mode is not None and not missing_required:
            position_universe = tuple(
                player_id for player_id in universe if player_id not in player_forbidden
            )
            positions = positions_for_filter(
                position_universe, eligible_positions_by_player
            )
            if rule.position_mode is TradeFilterMode.INCLUDE:
                for position in sorted(rule.positions):
                    bit = new_bit()
                    required |= bit
                    add_evidence(
                        (
                            player_id
                            for player_id in position_universe
                            if position in positions[player_id]
                        ),
                        bit,
                    )
            else:
                matching = frozenset(
                    player_id
                    for player_id in position_universe
                    if positions[player_id].intersection(rule.positions)
                )
                position_forbidden = tuple(
                    player_id
                    for player_id in position_universe
                    if (
                        player_id not in matching
                        if rule.position_mode is TradeFilterMode.ONLY
                        else player_id in matching
                    )
                )
                if position_forbidden:
                    bit = new_bit()
                    forbidden |= bit
                    add_evidence(position_forbidden, bit)
        conditions.append(_CompiledLeaf(required, forbidden))

    return CompiledTradeFilter(evidence_by_player, tuple(conditions), formula)


def positions_for_filter(
    player_ids: tuple[PlayerId, ...],
    values: Mapping[PlayerId, Iterable[str]] | None,
) -> dict[PlayerId, frozenset[str]]:
    """Validate and normalize position evidence needed by a package filter."""

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


def _compile_formula(
    value: TradePackageExpression,
    leaves: list[TradePackageFilter],
    leaf_indexes: dict[TradePackageFilter, int],
) -> object:
    if isinstance(value, TradePackageFilter):
        index = leaf_indexes.get(value)
        if index is None:
            index = len(leaves)
            leaf_indexes[value] = index
            leaves.append(value)
        return index
    return (
        value.operator,
        tuple(
            _compile_formula(operand, leaves, leaf_indexes)
            for operand in value.operands
        ),
    )


def _evaluate_formula(formula: object, values: tuple[bool, ...]) -> bool:
    if isinstance(formula, int):
        return values[formula]
    operator, operands = formula
    results = tuple(_evaluate_formula(row, values) for row in operands)
    if operator is TradeFilterOperator.AND:
        return all(results)
    if operator is TradeFilterOperator.OR:
        return any(results)
    if operator is TradeFilterOperator.XOR:
        return sum(results) == 1
    return not results[0]


__all__ = (
    "CompiledTradeFilter",
    "compile_trade_filter",
    "positions_for_filter",
)
