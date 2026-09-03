from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from typing import Iterator

from .trade_filters import (
    TRADE_FILTER_EXPRESSION_SEMANTICS_VERSION,
    TRADE_FILTER_SEMANTICS_VERSION,
    TradeFilterExpression,
    TradePackageExpression,
    TradePackageFilter,
)
from .trade_package_enumeration import TradePackagePool as _TradePackagePool


PlayerId = str


@dataclass(frozen=True)
class TeamRoster:
    """All owned players, with explicit IDs that do not consume active capacity."""

    team_id: str
    player_ids: tuple[PlayerId, ...]
    current_size: int
    roster_cap: int
    capacity_exempt_player_ids: frozenset[PlayerId] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.team_id, str) or not self.team_id:
            raise ValueError("team_id must be a non-empty string")
        player_ids = tuple(self.player_ids)
        if any(not isinstance(player_id, str) or not player_id for player_id in player_ids):
            raise ValueError("player_ids must be non-empty strings")
        unique_ids = set(player_ids)
        if len(unique_ids) != len(player_ids):
            raise ValueError("a team roster contains a duplicate player_id")
        _require_int("current_size", self.current_size, minimum=len(player_ids))
        if isinstance(self.capacity_exempt_player_ids, (str, bytes)):
            raise ValueError(
                "capacity_exempt_player_ids must be non-empty strings"
            )
        try:
            capacity_exempt = frozenset(self.capacity_exempt_player_ids)
        except TypeError:
            raise ValueError(
                "capacity_exempt_player_ids must be non-empty strings"
            ) from None
        if any(
            not isinstance(player_id, str) or not player_id
            for player_id in capacity_exempt
        ):
            raise ValueError("capacity_exempt_player_ids must be non-empty strings")
        if not capacity_exempt.issubset(unique_ids):
            raise ValueError("capacity-exempt players must be owned by the team")
        _require_int(
            "roster_cap",
            self.roster_cap,
            minimum=self.current_size - len(capacity_exempt),
        )
        object.__setattr__(self, "player_ids", player_ids)
        object.__setattr__(self, "capacity_exempt_player_ids", capacity_exempt)

    @property
    def active_size(self) -> int:
        """Return the number of owned players consuming ordinary roster capacity."""

        return self.current_size - len(self.capacity_exempt_player_ids)


@dataclass(frozen=True)
class TradeConstraints:
    """Package-size and roster-cap rules for one local trade search."""

    min_outgoing: int = 1
    max_outgoing: int = 1
    min_incoming: int = 1
    max_incoming: int = 1
    max_total_players: int | None = None
    max_imbalance: int | None = None
    balanced_only: bool = False
    excluded_size_pairs: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    locked_player_ids: frozenset[PlayerId] = field(default_factory=frozenset)
    require_no_drops: bool = False
    outgoing_filter: TradePackageExpression | None = None
    incoming_filter: TradePackageExpression | None = None

    def __post_init__(self) -> None:
        _require_int("min_outgoing", self.min_outgoing, minimum=1)
        _require_int("max_outgoing", self.max_outgoing, minimum=self.min_outgoing)
        _require_int("min_incoming", self.min_incoming, minimum=1)
        _require_int("max_incoming", self.max_incoming, minimum=self.min_incoming)
        if self.max_total_players is not None:
            _require_int(
                "max_total_players",
                self.max_total_players,
                minimum=self.min_outgoing + self.min_incoming,
            )
        if self.max_imbalance is not None:
            _require_int("max_imbalance", self.max_imbalance, minimum=0)
        if not isinstance(self.balanced_only, bool):
            raise ValueError("balanced_only must be a boolean")
        if not isinstance(self.require_no_drops, bool):
            raise ValueError("require_no_drops must be a boolean")

        excluded = frozenset(self.excluded_size_pairs)
        for pair in excluded:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("excluded_size_pairs must contain (outgoing, incoming) pairs")
            _require_int("excluded outgoing size", pair[0], minimum=1)
            _require_int("excluded incoming size", pair[1], minimum=1)
        try:
            locked = frozenset(self.locked_player_ids)
        except TypeError:
            raise ValueError("locked_player_ids must be non-empty strings") from None
        if any(not isinstance(player_id, str) or not player_id for player_id in locked):
            raise ValueError("locked_player_ids must be non-empty strings")
        for name in ("outgoing_filter", "incoming_filter"):
            package_filter = getattr(self, name)
            if package_filter is not None and not isinstance(
                package_filter, (TradePackageFilter, TradeFilterExpression)
            ):
                raise ValueError(
                    f"{name} must be a TradePackageFilter, "
                    "TradeFilterExpression, or null"
                )
            if (
                isinstance(package_filter, TradePackageFilter)
                and not package_filter.active
            ):
                package_filter = None
            object.__setattr__(self, name, package_filter)
        object.__setattr__(self, "excluded_size_pairs", excluded)
        object.__setattr__(self, "locked_player_ids", locked)

    def to_record(self) -> dict[str, object]:
        """Return the canonical JSON-ready constraints and active filters."""

        record = {
            "balanced_only": self.balanced_only,
            "excluded_size_pairs": [
                list(pair) for pair in sorted(self.excluded_size_pairs)
            ],
            "locked_player_ids": sorted(self.locked_player_ids),
            "max_imbalance": self.max_imbalance,
            "max_incoming": self.max_incoming,
            "max_outgoing": self.max_outgoing,
            "max_total_players": self.max_total_players,
            "min_incoming": self.min_incoming,
            "min_outgoing": self.min_outgoing,
            "require_no_drops": self.require_no_drops,
        }
        active_filters = {
            name: package_filter.to_record()
            for name, package_filter in (
                ("outgoing_filter", self.outgoing_filter),
                ("incoming_filter", self.incoming_filter),
            )
            if package_filter is not None
        }
        if active_filters:
            record.update(active_filters)
            record["package_filter_semantics_version"] = (
                TRADE_FILTER_EXPRESSION_SEMANTICS_VERSION
                if any(
                    isinstance(package_filter, TradeFilterExpression)
                    for package_filter in (
                        self.outgoing_filter,
                        self.incoming_filter,
                    )
                )
                else TRADE_FILTER_SEMANTICS_VERSION
            )
        return record


@dataclass(frozen=True)
class TradeCandidate:
    outgoing_player_ids: tuple[PlayerId, ...]
    incoming_player_ids: tuple[PlayerId, ...]


class TradeSpace:
    """Count and lazily iterate the valid package pairs between two teams."""

    def __init__(
        self,
        primary: TeamRoster,
        counterparty: TeamRoster,
        constraints: TradeConstraints,
        *,
        eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None = None,
    ) -> None:
        if primary.team_id == counterparty.team_id:
            raise ValueError("trade teams must have different team_id values")
        shared = set(primary.player_ids).intersection(counterparty.player_ids)
        if shared:
            raise ValueError("a player_id cannot be owned by both teams")

        self.primary = primary
        self.counterparty = counterparty
        self.constraints = constraints
        self._outgoing_ids = tuple(
            player_id
            for player_id in primary.player_ids
            if player_id not in constraints.locked_player_ids
        )
        self._incoming_ids = tuple(
            player_id
            for player_id in counterparty.player_ids
            if player_id not in constraints.locked_player_ids
        )
        self._outgoing_pool = _TradePackagePool(
            self._outgoing_ids,
            constraints.outgoing_filter,
            eligible_positions_by_player,
            primary.capacity_exempt_player_ids,
        )
        self._incoming_pool = _TradePackagePool(
            self._incoming_ids,
            constraints.incoming_filter,
            eligible_positions_by_player,
            counterparty.capacity_exempt_player_ids,
        )
        counted_pairs = tuple(
            (*pair, *self._package_counts(*pair)) for pair in self._iter_size_pairs()
        )
        self._size_pairs = tuple(
            row for row in counted_pairs if row[2] and row[3]
        )
        self._candidate_count = sum(
            outgoing_count * incoming_count
            for _, _, outgoing_count, incoming_count in self._size_pairs
        )

    @property
    def candidate_count(self) -> int:
        """Return the exact count without constructing any candidate packages."""

        return self._candidate_count

    def __iter__(self) -> Iterator[TradeCandidate]:
        return self.iter_from(0)

    def iter_from(self, start_candidate_index: int) -> Iterator[TradeCandidate]:
        """Iterate from one exact candidate index without replaying prior pairs."""

        if (
            isinstance(start_candidate_index, bool)
            or not isinstance(start_candidate_index, int)
            or not 0 <= start_candidate_index <= self._candidate_count
        ):
            raise ValueError(
                "start_candidate_index must be between zero and candidate_count"
            )
        return self._iterate_from(start_candidate_index)

    def _iterate_from(self, start_candidate_index: int) -> Iterator[TradeCandidate]:
        remaining = start_candidate_index
        for (
            outgoing_size,
            incoming_size,
            outgoing_count,
            incoming_count,
        ) in self._size_pairs:
            pair_count = outgoing_count * incoming_count
            if remaining >= pair_count:
                remaining -= pair_count
                continue
            primary_minimum = self._minimum_active_outgoing(
                self.primary, incoming_size
            )
            counterparty_minimum = self._minimum_active_outgoing(
                self.counterparty, outgoing_size
            )
            outgoing_skip, incoming_skip = divmod(remaining, incoming_count)
            remaining = 0
            outgoing_packages = self._outgoing_pool.iter_packages(
                outgoing_size, minimum_active=primary_minimum
            )
            for outgoing in islice(outgoing_packages, outgoing_skip, None):
                incoming_packages = self._incoming_pool.iter_packages(
                    incoming_size, minimum_active=counterparty_minimum
                )
                if incoming_skip:
                    incoming_packages = islice(
                        incoming_packages, incoming_skip, None
                    )
                    incoming_skip = 0
                for incoming in incoming_packages:
                    yield TradeCandidate(outgoing, incoming)

    def _iter_size_pairs(self) -> Iterator[tuple[int, int]]:
        rules = self.constraints
        outgoing_limit = min(rules.max_outgoing, len(self._outgoing_ids))
        incoming_limit = min(rules.max_incoming, len(self._incoming_ids))
        for outgoing_size in range(rules.min_outgoing, outgoing_limit + 1):
            for incoming_size in range(rules.min_incoming, incoming_limit + 1):
                pair = (outgoing_size, incoming_size)
                if pair in rules.excluded_size_pairs:
                    continue
                if rules.balanced_only and outgoing_size != incoming_size:
                    continue
                if (
                    rules.max_total_players is not None
                    and outgoing_size + incoming_size > rules.max_total_players
                ):
                    continue
                if (
                    rules.max_imbalance is not None
                    and abs(outgoing_size - incoming_size) > rules.max_imbalance
                ):
                    continue
                yield pair

    def _package_counts(
        self, outgoing_size: int, incoming_size: int
    ) -> tuple[int, int]:
        outgoing_count = self._outgoing_pool.count(
            outgoing_size,
            minimum_active=self._minimum_active_outgoing(
                self.primary, incoming_size
            ),
        )
        incoming_count = self._incoming_pool.count(
            incoming_size,
            minimum_active=self._minimum_active_outgoing(
                self.counterparty, outgoing_size
            ),
        )
        return outgoing_count, incoming_count

    def _minimum_active_outgoing(
        self, roster: TeamRoster, received_size: int
    ) -> int:
        if not self.constraints.require_no_drops:
            return 0
        return max(
            0,
            roster.active_size + received_size - roster.roster_cap,
        )


def _require_int(name: str, value: object, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
