"""Side-specific player and position rules for local trade packages."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Iterator, TypeAlias

from .positions import normalize_player_position


PlayerId = str
TRADE_FILTER_SEMANTICS_VERSION = 1
TRADE_FILTER_EXPRESSION_SEMANTICS_VERSION = 2
MAX_TRADE_FILTER_EXPRESSION_DEPTH = 16
MAX_TRADE_FILTER_EXPRESSION_NODES = 128


class TradeFilterMode(str, Enum):
    """How one selected player or position set constrains a trade package."""

    INCLUDE = "include"
    ONLY = "only"
    EXCLUDE = "exclude"


class TradeFilterOperator(str, Enum):
    """Boolean operation joining package-filter operands."""

    AND = "and"
    OR = "or"
    XOR = "xor"
    NOT = "not"


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


@dataclass(frozen=True, slots=True)
class TradeFilterExpression:
    """A recursive Boolean expression over active package-filter leaves.

    ``xor`` means exactly one immediate operand matches.  ``and``, ``or``,
    and ``xor`` are commutative, so their operands are stored in canonical
    record order.  Duplicate operands and explicit nesting remain significant.
    """

    operator: TradeFilterOperator | str
    operands: tuple["TradePackageExpression", ...]
    _depth: int = field(init=False, repr=False, compare=False)
    _node_count: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            operator = TradeFilterOperator(self.operator)
        except (TypeError, ValueError):
            raise ValueError("operator must be and, or, xor, or not") from None
        if isinstance(self.operands, (str, bytes)):
            raise ValueError("operands must contain trade package filters")
        try:
            operands = tuple(self.operands)
        except TypeError:
            raise ValueError("operands must contain trade package filters") from None
        if any(
            not isinstance(row, (TradePackageFilter, TradeFilterExpression))
            for row in operands
        ):
            raise ValueError("operands must contain trade package filters")
        if any(
            isinstance(row, TradePackageFilter) and not row.active
            for row in operands
        ):
            raise ValueError("expression leaves must contain an active package rule")
        expected = 1 if operator is TradeFilterOperator.NOT else 2
        if len(operands) < expected or (
            operator is TradeFilterOperator.NOT and len(operands) != 1
        ):
            requirement = "exactly one" if expected == 1 else "at least two"
            raise ValueError(f"{operator.value} requires {requirement} operand(s)")
        if operator is not TradeFilterOperator.NOT:
            operands = tuple(sorted(operands, key=_canonical_operand_key))
        depth = 1 + max(
            row._depth if isinstance(row, TradeFilterExpression) else 0
            for row in operands
        )
        node_count = 1 + sum(
            row._node_count if isinstance(row, TradeFilterExpression) else 1
            for row in operands
        )
        if depth > MAX_TRADE_FILTER_EXPRESSION_DEPTH:
            raise ValueError(
                "trade filter expression exceeds the maximum nesting depth of "
                f"{MAX_TRADE_FILTER_EXPRESSION_DEPTH}"
            )
        if node_count > MAX_TRADE_FILTER_EXPRESSION_NODES:
            raise ValueError(
                "trade filter expression exceeds the maximum node count of "
                f"{MAX_TRADE_FILTER_EXPRESSION_NODES}"
            )
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "operands", operands)
        object.__setattr__(self, "_depth", depth)
        object.__setattr__(self, "_node_count", node_count)

    def to_record(self) -> dict[str, object]:
        return {
            "operator": self.operator.value,
            "operands": [row.to_record() for row in self.operands],
        }


TradePackageExpression: TypeAlias = TradePackageFilter | TradeFilterExpression


def iter_trade_filter_leaves(
    value: TradePackageExpression,
) -> Iterator[TradePackageFilter]:
    """Yield atomic rules in canonical depth-first order."""

    if isinstance(value, TradePackageFilter):
        yield value
        return
    for operand in value.operands:
        yield from iter_trade_filter_leaves(operand)


def parse_trade_filter(
    name: str, value: object
) -> TradePackageExpression | None:
    """Parse one strict JSON filter record, including recursive expressions."""

    return _parse_trade_filter(
        name, value, nested=False, depth=0, node_count=[0]
    )


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


def _parse_trade_filter(
    name: str,
    value: object,
    *,
    nested: bool,
    depth: int,
    node_count: list[int],
) -> TradePackageExpression | None:
    if value is None and not nested:
        return None
    if depth > MAX_TRADE_FILTER_EXPRESSION_DEPTH:
        raise ValueError(
            "trade filter expression exceeds the maximum nesting depth of "
            f"{MAX_TRADE_FILTER_EXPRESSION_DEPTH}"
        )
    node_count[0] += 1
    if node_count[0] > MAX_TRADE_FILTER_EXPRESSION_NODES:
        raise ValueError(
            "trade filter expression exceeds the maximum node count of "
            f"{MAX_TRADE_FILTER_EXPRESSION_NODES}"
        )
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    fields = set(value)
    if fields == {"operator", "operands"}:
        raw_operands = value["operands"]
        if not isinstance(raw_operands, list):
            raise ValueError(f"{name}.operands must be a JSON array")
        operands = tuple(
            _parse_trade_filter(
                f"{name}.operands[{index}]",
                row,
                nested=True,
                depth=depth + 1,
                node_count=node_count,
            )
            for index, row in enumerate(raw_operands)
        )
        return TradeFilterExpression(value["operator"], operands)
    if fields != {"player_ids", "player_mode", "positions", "position_mode"}:
        raise ValueError(f"{name} fields are invalid")
    player_ids = _json_string_array(f"{name}.player_ids", value["player_ids"])
    positions = _json_string_array(f"{name}.positions", value["positions"])
    if len(set(player_ids)) != len(player_ids):
        raise ValueError(f"{name}.player_ids contains a duplicate")
    if len(set(positions)) != len(positions):
        raise ValueError(f"{name}.positions contains a duplicate")
    for values_field, mode_field, selected, mode in (
        ("player_ids", "player_mode", player_ids, value["player_mode"]),
        ("positions", "position_mode", positions, value["position_mode"]),
    ):
        if bool(selected) != (mode is not None):
            raise ValueError(
                f"{name}.{mode_field} must be set exactly when "
                f"{name}.{values_field} has selections"
            )
    package_filter = TradePackageFilter(
        frozenset(player_ids),
        value["player_mode"],
        frozenset(positions),
        value["position_mode"],
    )
    return package_filter if nested or package_filter.active else None


def _json_string_array(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row for row in value
    ):
        raise ValueError(f"{name} must be a JSON array of non-empty strings")
    return tuple(value)


def _canonical_operand_key(value: TradePackageExpression) -> str:
    return json.dumps(value.to_record(), sort_keys=True, separators=(",", ":"))


__all__ = (
    "MAX_TRADE_FILTER_EXPRESSION_DEPTH",
    "MAX_TRADE_FILTER_EXPRESSION_NODES",
    "TRADE_FILTER_EXPRESSION_SEMANTICS_VERSION",
    "TRADE_FILTER_SEMANTICS_VERSION",
    "TradeFilterExpression",
    "TradeFilterMode",
    "TradeFilterOperator",
    "TradePackageExpression",
    "TradePackageFilter",
    "iter_trade_filter_leaves",
    "parse_trade_filter",
)
