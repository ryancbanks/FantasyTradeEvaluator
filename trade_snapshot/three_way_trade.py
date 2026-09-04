"""Exact, seekable enumeration of fully directed three-team trades."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Iterator

from .positions import normalize_player_position
from .roster_capacity import (
    reserve_counts_for,
    signature_record,
    solve_post_trade_capacity,
)
from .trade_filters import (
    TradeFilterExpression,
    TradeFilterMode,
    TradePackageExpression,
    TradePackageFilter,
    iter_trade_filter_leaves,
)
from .trade_filter_compiler import CompiledTradeFilter, compile_trade_filter
from .trade_space import TeamRoster, TradeConstraints


PlayerId = str
_Counts = tuple[int, int, int]
_ReserveSignature = tuple[int, ...]
_State = tuple[
    int,
    _Counts,
    _Counts,
    _ReserveSignature,
    _ReserveSignature,
    int,
    int,
]


@dataclass(frozen=True, slots=True)
class TradeTransfer:
    """One nonempty player package moving between two participant teams."""

    source_team_id: str
    destination_team_id: str
    player_ids: tuple[PlayerId, ...]

    def __post_init__(self) -> None:
        source = _team_id("source_team_id", self.source_team_id)
        destination = _team_id("destination_team_id", self.destination_team_id)
        if source == destination:
            raise ValueError("a trade transfer cannot return players to their source team")
        players = _player_ids("player_ids", self.player_ids)
        if not players:
            raise ValueError("a trade transfer must contain at least one player")
        object.__setattr__(self, "source_team_id", source)
        object.__setattr__(self, "destination_team_id", destination)
        object.__setattr__(self, "player_ids", players)


@dataclass(frozen=True, slots=True)
class ThreeWayTradeCandidate:
    """A canonical set of transfers in which all three teams send and receive."""

    participant_team_ids: tuple[str, str, str]
    transfers: tuple[TradeTransfer, ...]

    def __post_init__(self) -> None:
        participants = _participants(self.participant_team_ids)
        try:
            transfers = tuple(self.transfers)
        except TypeError:
            raise ValueError("transfers must contain TradeTransfer values") from None
        if not transfers or any(not isinstance(row, TradeTransfer) for row in transfers):
            raise ValueError("transfers must contain TradeTransfer values")
        participant_set = frozenset(participants)
        if any(
            row.source_team_id not in participant_set
            or row.destination_team_id not in participant_set
            for row in transfers
        ):
            raise ValueError("every transfer must be between participant teams")
        routes = tuple(
            (row.source_team_id, row.destination_team_id) for row in transfers
        )
        if len(set(routes)) != len(routes):
            raise ValueError("a candidate cannot repeat a directed transfer route")
        moved = tuple(player for row in transfers for player in row.player_ids)
        if len(set(moved)) != len(moved):
            raise ValueError("a player cannot be transferred more than once")
        sources = {row.source_team_id for row in transfers}
        destinations = {row.destination_team_id for row in transfers}
        if sources != participant_set or destinations != participant_set:
            raise ValueError("every participant must send and receive at least one player")
        rank = {team_id: index for index, team_id in enumerate(participants)}
        canonical = tuple(
            sorted(
                transfers,
                key=lambda row: (
                    rank[row.source_team_id],
                    rank[row.destination_team_id],
                ),
            )
        )
        object.__setattr__(self, "participant_team_ids", participants)
        object.__setattr__(self, "transfers", canonical)

    def outgoing_for(self, team_id: str) -> tuple[PlayerId, ...]:
        """Return this team's moved players in deterministic route order."""

        self._require_participant(team_id)
        return tuple(
            player
            for row in self.transfers
            if row.source_team_id == team_id
            for player in row.player_ids
        )

    def incoming_for(self, team_id: str) -> tuple[PlayerId, ...]:
        """Return arrivals ordered by source team, then source-roster order."""

        self._require_participant(team_id)
        return tuple(
            player
            for row in self.transfers
            if row.destination_team_id == team_id
            for player in row.player_ids
        )

    def _require_participant(self, team_id: str) -> None:
        if team_id not in self.participant_team_ids:
            raise KeyError(team_id)


@dataclass(frozen=True, slots=True)
class _PackageRule:
    required_player_ids: frozenset[PlayerId]
    forbidden_player_ids: frozenset[PlayerId]
    coverage_by_player: Mapping[PlayerId, int]
    target_coverage: int
    compiled_filter: CompiledTradeFilter | None = None

    def permits(self, player_id: PlayerId, selected: bool) -> bool:
        if selected:
            return player_id not in self.forbidden_player_ids
        return player_id not in self.required_player_ids

    def matches(self, evidence: int) -> bool:
        if self.compiled_filter is not None:
            return self.compiled_filter.matches(evidence)
        return evidence & self.target_coverage == self.target_coverage


@dataclass(frozen=True, slots=True)
class _PlayerDecision:
    origin: int
    player_id: PlayerId
    reserve_slot: str | None
    destinations: tuple[int, ...]
    outgoing_coverage: int
    incoming_coverage: int


class ThreeWayTradeSpace:
    """Count and seek through every valid directed trade among three teams."""

    def __init__(
        self,
        rosters: Iterable[TeamRoster],
        constraints: TradeConstraints,
        *,
        eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None = None,
    ) -> None:
        rows = _rosters(rosters)
        if not isinstance(constraints, TradeConstraints):
            raise ValueError("constraints must be TradeConstraints")
        if constraints.excluded_size_pairs:
            raise ValueError(
                "excluded_size_pairs do not apply to three-way trade searches"
            )
        self.rosters = rows
        self.primary = rows[0]
        self.partners = rows[1:]
        self.participant_team_ids = tuple(row.team_id for row in rows)
        self.constraints = constraints
        self._reserve_kinds = tuple(
            sorted(
                {
                    kind
                    for roster in rows
                    for kind in (
                        *roster.reserve_slot_counts,
                        *roster.reserve_slot_by_player.values(),
                    )
                }
            )
        )
        self._reserve_kind_index = {
            kind: index for index, kind in enumerate(self._reserve_kinds)
        }
        self._current_reserve_counts = tuple(
            reserve_counts_for(row.player_ids, row.reserve_slot_by_player)
            for row in rows
        )

        primary_players = rows[0].player_ids
        partner_players = tuple(
            player for row in rows[1:] for player in row.player_ids
        )
        outgoing_rule = _compile_rule(
            "outgoing_filter",
            constraints.outgoing_filter,
            primary_players,
            eligible_positions_by_player,
        )
        incoming_rule = _compile_rule(
            "incoming_filter",
            constraints.incoming_filter,
            partner_players,
            eligible_positions_by_player,
        )
        self._outgoing_rule = outgoing_rule
        self._incoming_rule = incoming_rule
        self._players = _player_decisions(
            rows,
            constraints.locked_player_ids,
            outgoing_rule,
            incoming_rule,
        )
        self._remaining_moves, self._remaining_receivers = _remaining_capacities(
            self._players
        )
        self._outgoing_target = outgoing_rule.target_coverage
        self._incoming_target = incoming_rule.target_coverage
        self._memo: dict[_State, int] = {}
        empty_reserve_signature = (0,) * (len(rows) * len(self._reserve_kinds))
        self._initial_state: _State = (
            0,
            (0, 0, 0),
            (0, 0, 0),
            empty_reserve_signature,
            empty_reserve_signature,
            0,
            0,
        )
        self._candidate_count = self._count(self._initial_state)

    @property
    def candidate_count(self) -> int:
        """Return the exact Python-integer count without constructing candidates."""

        return self._candidate_count

    def enumeration_record(self) -> dict[str, object]:
        """Return the compiled, JSON-ready identity of index-to-candidate order."""

        player_decisions = [
            {
                "active": int(row.reserve_slot is None),
                "destinations": list(row.destinations),
                "incoming_coverage": row.incoming_coverage,
                "origin": row.origin,
                "outgoing_coverage": row.outgoing_coverage,
                "player_id": row.player_id,
            }
            for row in self._players
        ]
        record = {
            "incoming_target": self._incoming_target,
            "outgoing_target": self._outgoing_target,
            "player_decisions": player_decisions,
        }
        if self._reserve_kinds:
            record["reserve_slot_counts_by_team"] = [
                dict(row.reserve_slot_counts) for row in self.rosters
            ]
            for decision, player in zip(player_decisions, self._players):
                decision["reserve_slot"] = player.reserve_slot
        for name, value in (
            ("outgoing_filter_expression", self.constraints.outgoing_filter),
            ("incoming_filter_expression", self.constraints.incoming_filter),
        ):
            if isinstance(value, TradeFilterExpression):
                record[name] = value.to_record()
        return record

    def __iter__(self) -> Iterator[ThreeWayTradeCandidate]:
        return self.iter_from(0)

    def iter_from(self, start_index: int) -> Iterator[ThreeWayTradeCandidate]:
        """Seek by counted subtrees and iterate from an exact candidate index."""

        if (
            isinstance(start_index, bool)
            or not isinstance(start_index, int)
            or not 0 <= start_index <= self._candidate_count
        ):
            raise ValueError("start_index must be between zero and candidate_count")
        return self._iterate(self._initial_state, start_index, [])

    def _count(self, state: _State) -> int:
        cached = self._memo.get(state)
        if cached is not None:
            return cached
        if self._impossible(state):
            result = 0
        elif state[0] == len(self._players):
            result = int(self._complete(state))
        else:
            player = self._players[state[0]]
            result = sum(
                self._count(child)
                for destination in player.destinations
                if (child := self._advance(state, player, destination)) is not None
            )
        self._memo[state] = result
        return result

    def _iterate(
        self,
        state: _State,
        skip: int,
        destinations: list[int],
    ) -> Iterator[ThreeWayTradeCandidate]:
        if state[0] == len(self._players):
            if self._complete(state):
                if skip != 0:
                    raise AssertionError("candidate subtree seek did not terminate")
                yield self._candidate(destinations)
            return
        player = self._players[state[0]]
        remaining_skip = skip
        for destination in player.destinations:
            child = self._advance(state, player, destination)
            if child is None:
                continue
            child_count = self._count(child)
            if remaining_skip >= child_count:
                remaining_skip -= child_count
                continue
            destinations.append(destination)
            yield from self._iterate(child, remaining_skip, destinations)
            destinations.pop()
            remaining_skip = 0

    def _advance(
        self,
        state: _State,
        player: _PlayerDecision,
        destination: int,
    ) -> _State | None:
        (
            index,
            outgoing,
            incoming,
            outgoing_reserve,
            incoming_reserve,
            out_mask,
            in_mask,
        ) = state
        if destination == player.origin:
            return (
                index + 1,
                outgoing,
                incoming,
                outgoing_reserve,
                incoming_reserve,
                out_mask,
                in_mask,
            )
        rules = self.constraints
        if (
            outgoing[player.origin] >= rules.max_outgoing
            or incoming[destination] >= rules.max_incoming
            or (
                rules.max_total_players is not None
                and sum(outgoing) >= rules.max_total_players
            )
        ):
            return None
        if player.reserve_slot is not None:
            kind = self._reserve_kind_index[player.reserve_slot]
            outgoing_reserve = _increment_reserve_signature(
                outgoing_reserve, player.origin, kind, len(self._reserve_kinds)
            )
            incoming_reserve = _increment_reserve_signature(
                incoming_reserve, destination, kind, len(self._reserve_kinds)
            )
        return (
            index + 1,
            _increment(outgoing, player.origin, 1),
            _increment(incoming, destination, 1),
            outgoing_reserve,
            incoming_reserve,
            out_mask | (player.outgoing_coverage if player.origin == 0 else 0),
            in_mask | (player.incoming_coverage if destination == 0 else 0),
        )

    def _impossible(self, state: _State) -> bool:
        index, outgoing, incoming, _, _, _, _ = state
        rules = self.constraints
        remaining_moves = self._remaining_moves[index]
        remaining_receivers = self._remaining_receivers[index]
        if any(
            sent + remaining_moves[team] < rules.min_outgoing
            or received + remaining_receivers[team] < rules.min_incoming
            for team, (sent, received) in enumerate(zip(outgoing, incoming))
        ):
            return True
        if rules.max_total_players is not None:
            outgoing_deficit = sum(
                max(0, rules.min_outgoing - value) for value in outgoing
            )
            incoming_deficit = sum(
                max(0, rules.min_incoming - value) for value in incoming
            )
            if (
                sum(outgoing) + max(outgoing_deficit, incoming_deficit)
                > rules.max_total_players
            ):
                return True
        return False

    def _complete(self, state: _State) -> bool:
        (
            _,
            outgoing,
            incoming,
            outgoing_reserve,
            incoming_reserve,
            out_mask,
            in_mask,
        ) = state
        rules = self.constraints
        if any(
            not rules.min_outgoing <= sent <= rules.max_outgoing
            or not rules.min_incoming <= received <= rules.max_incoming
            for sent, received in zip(outgoing, incoming)
        ):
            return False
        if (
            rules.max_total_players is not None
            and sum(outgoing) > rules.max_total_players
        ):
            return False
        if rules.balanced_only and outgoing != incoming:
            return False
        if rules.max_imbalance is not None and any(
            abs(sent - received) > rules.max_imbalance
            for sent, received in zip(outgoing, incoming)
        ):
            return False
        capacity_plans = tuple(
            self._capacity_plan(
                team,
                outgoing[team],
                incoming[team],
                outgoing_reserve,
                incoming_reserve,
            )
            for team in range(len(self.rosters))
        )
        if rules.require_no_drops and any(
            row.required_cuts != 0 for row in capacity_plans
        ):
            return False
        if not rules.require_no_drops and any(
            not row.feasible for row in capacity_plans
        ):
            return False
        return self._outgoing_rule.matches(out_mask) and self._incoming_rule.matches(
            in_mask
        )

    def _capacity_plan(
        self,
        team: int,
        outgoing_size: int,
        incoming_size: int,
        outgoing_reserve: _ReserveSignature,
        incoming_reserve: _ReserveSignature,
    ):
        roster = self.rosters[team]
        start = team * len(self._reserve_kinds)
        stop = start + len(self._reserve_kinds)
        return solve_post_trade_capacity(
            active_cap=roster.roster_cap,
            current_size=roster.current_size,
            known_player_count=len(roster.player_ids),
            reserve_slot_counts=roster.reserve_slot_counts,
            current_reserve_counts=self._current_reserve_counts[team],
            outgoing_size=outgoing_size,
            outgoing_reserve_counts=signature_record(
                self._reserve_kinds, outgoing_reserve[start:stop]
            ),
            incoming_size=incoming_size,
            incoming_reserve_counts=signature_record(
                self._reserve_kinds, incoming_reserve[start:stop]
            ),
        )

    def _candidate(self, destinations: list[int]) -> ThreeWayTradeCandidate:
        routes: dict[tuple[int, int], list[PlayerId]] = {}
        for player, destination in zip(self._players, destinations):
            if destination != player.origin:
                routes.setdefault((player.origin, destination), []).append(
                    player.player_id
                )
        transfers = tuple(
            TradeTransfer(
                self.rosters[source].team_id,
                self.rosters[destination].team_id,
                tuple(routes[(source, destination)]),
            )
            for source in range(3)
            for destination in range(3)
            if (source, destination) in routes
        )
        return ThreeWayTradeCandidate(self.participant_team_ids, transfers)


def _rosters(values: Iterable[TeamRoster]) -> tuple[TeamRoster, TeamRoster, TeamRoster]:
    if isinstance(values, (str, bytes)):
        raise ValueError("rosters must contain exactly three TeamRoster values")
    try:
        supplied = tuple(values)
    except TypeError:
        raise ValueError("rosters must contain exactly three TeamRoster values") from None
    if len(supplied) != 3 or any(not isinstance(row, TeamRoster) for row in supplied):
        raise ValueError("rosters must contain exactly three TeamRoster values")
    rows = (supplied[0], *sorted(supplied[1:], key=lambda row: row.team_id))
    if len({row.team_id for row in rows}) != 3:
        raise ValueError("three-way trade teams must have distinct team_id values")
    owned: set[PlayerId] = set()
    for row in rows:
        overlap = owned.intersection(row.player_ids)
        if overlap:
            raise ValueError(f"player {min(overlap)!r} cannot be owned by multiple teams")
        owned.update(row.player_ids)
    return rows


def _compile_rule(
    name: str,
    rule: TradePackageExpression | None,
    universe: tuple[PlayerId, ...],
    eligible_positions_by_player: Mapping[PlayerId, Iterable[str]] | None,
) -> _PackageRule:
    if rule is None:
        return _PackageRule(frozenset(), frozenset(), {}, 0)
    if isinstance(rule, TradeFilterExpression):
        available = frozenset(universe)
        selected_ids = frozenset(
            player_id
            for leaf in iter_trade_filter_leaves(rule)
            for player_id in leaf.player_ids
        )
        invalid = selected_ids.difference(available)
        if invalid:
            owner = "primary roster" if name == "outgoing_filter" else "partner rosters"
            raise ValueError(f"{name} players must belong to the selected {owner}")
        compiled = compile_trade_filter(
            rule, universe, eligible_positions_by_player
        )
        if compiled is None:
            raise AssertionError("an active expression did not compile")
        return _PackageRule(
            frozenset(),
            frozenset(),
            compiled.evidence_by_player,
            0,
            compiled,
        )
    if not isinstance(rule, TradePackageFilter):
        raise ValueError(
            f"{name} must be a TradePackageFilter, TradeFilterExpression, or null"
        )
    available = frozenset(universe)
    invalid = rule.player_ids.difference(available)
    if invalid:
        owner = "primary roster" if name == "outgoing_filter" else "partner rosters"
        raise ValueError(f"{name} players must belong to the selected {owner}")

    required = (
        rule.player_ids
        if rule.player_mode in {TradeFilterMode.INCLUDE, TradeFilterMode.ONLY}
        else frozenset()
    )
    forbidden = (
        rule.player_ids
        if rule.player_mode is TradeFilterMode.EXCLUDE
        else frozenset()
    )
    if rule.player_mode is TradeFilterMode.ONLY:
        forbidden = available.difference(rule.player_ids)

    coverage: dict[PlayerId, int] = {}
    target = 0
    if rule.position_mode is not None:
        positions = _position_evidence(universe, eligible_positions_by_player)
        bits = {
            position: 1 << index
            for index, position in enumerate(sorted(rule.positions))
        }
        target = sum(bits.values()) if rule.position_mode is TradeFilterMode.INCLUDE else 0
        coverage = {
            player_id: sum(
                bit
                for position, bit in bits.items()
                if position in positions[player_id]
            )
            for player_id in universe
        }
        matching = frozenset(
            player_id for player_id, mask in coverage.items() if mask
        )
        if rule.position_mode is TradeFilterMode.ONLY:
            forbidden = forbidden.union(available.difference(matching))
        elif rule.position_mode is TradeFilterMode.EXCLUDE:
            forbidden = forbidden.union(matching)
    return _PackageRule(required, forbidden, coverage, target)


def _position_evidence(
    player_ids: tuple[PlayerId, ...],
    values: Mapping[PlayerId, Iterable[str]] | None,
) -> dict[PlayerId, frozenset[str]]:
    if not isinstance(values, Mapping):
        raise ValueError(
            "eligible_positions_by_player is required for active position filters"
        )
    result = {}
    for player_id in player_ids:
        if player_id not in values:
            raise ValueError(
                f"eligible_positions_by_player is missing player {player_id!r}"
            )
        raw = values[player_id]
        if isinstance(raw, (str, bytes)):
            raise ValueError(
                "eligible_positions_by_player values must be position iterables"
            )
        try:
            normalized = frozenset(normalize_player_position(value) for value in raw)
        except (TypeError, ValueError):
            raise ValueError(
                "eligible_positions_by_player values must be nonempty position iterables"
            ) from None
        if not normalized:
            raise ValueError(
                "eligible_positions_by_player values must be nonempty position iterables"
            )
        result[player_id] = normalized
    return result


def _player_decisions(
    rosters: tuple[TeamRoster, TeamRoster, TeamRoster],
    locked_player_ids: frozenset[PlayerId],
    outgoing_rule: _PackageRule,
    incoming_rule: _PackageRule,
) -> tuple[_PlayerDecision, ...]:
    result = []
    for origin, roster in enumerate(rosters):
        for player_id in roster.player_ids:
            choices = (origin, *(index for index in range(3) if index != origin))
            if player_id in locked_player_ids:
                choices = (origin,)
            choices = tuple(
                destination
                for destination in choices
                if (
                    origin != 0
                    or outgoing_rule.permits(player_id, destination != origin)
                )
                and (
                    origin == 0
                    or incoming_rule.permits(player_id, destination == 0)
                )
            )
            result.append(
                _PlayerDecision(
                    origin,
                    player_id,
                    roster.reserve_slot_by_player.get(player_id),
                    choices,
                    outgoing_rule.coverage_by_player.get(player_id, 0),
                    incoming_rule.coverage_by_player.get(player_id, 0),
                )
            )
    return tuple(result)


def _remaining_capacities(players: tuple[_PlayerDecision, ...]):
    moves = [(0, 0, 0)] * (len(players) + 1)
    receivers = [(0, 0, 0)] * (len(players) + 1)
    for index in range(len(players) - 1, -1, -1):
        player = players[index]
        move = list(moves[index + 1])
        receive = list(receivers[index + 1])
        if any(destination != player.origin for destination in player.destinations):
            move[player.origin] += 1
        for destination in set(player.destinations).difference({player.origin}):
            receive[destination] += 1
        moves[index] = tuple(move)
        receivers[index] = tuple(receive)
    return tuple(moves), tuple(receivers)


def _participants(values: object) -> tuple[str, str, str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("participant_team_ids must contain exactly three team IDs")
    try:
        result = tuple(_team_id("participant team ID", value) for value in values)
    except TypeError:
        raise ValueError("participant_team_ids must contain exactly three team IDs") from None
    if len(result) != 3 or len(set(result)) != 3:
        raise ValueError("participant_team_ids must contain exactly three distinct team IDs")
    if result[1:] != tuple(sorted(result[1:])):
        raise ValueError("partner team IDs must be in canonical order")
    return result


def _increment(values: _Counts, index: int, amount: int) -> _Counts:
    result = list(values)
    result[index] += amount
    return tuple(result)


def _increment_reserve_signature(
    values: _ReserveSignature,
    team: int,
    kind: int,
    kind_count: int,
) -> _ReserveSignature:
    result = list(values)
    result[team * kind_count + kind] += 1
    return tuple(result)


def _team_id(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _player_ids(name: str, values: object) -> tuple[PlayerId, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must contain non-empty player IDs")
    try:
        result = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must contain non-empty player IDs") from None
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain non-empty player IDs")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} cannot contain a duplicate player ID")
    return result


__all__ = (
    "ThreeWayTradeCandidate",
    "ThreeWayTradeSpace",
    "TradeTransfer",
)
