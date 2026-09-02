"""Fast, exact strength prefiltering for one prepared pair of teams."""

from dataclasses import dataclass, field
from itertools import islice
from math import isfinite
from numbers import Real
from typing import Iterator

from .analyzer_contract import PowerRankingChange
from .roster_adjustment import (
    PreparedRosterAdjuster,
    TradeRosterAdjustment,
    unchanged_trade_adjustment,
)
from .strength import RosterStrength, StrengthModel
from .trade_space import TeamRoster, TradeCandidate, TradeSpace


@dataclass(frozen=True, slots=True)
class TradePowerEvaluation:
    """One candidate's local power result in its original search-space position."""

    candidate_index: int
    candidate: TradeCandidate
    primary: PowerRankingChange
    counterparty: PowerRankingChange
    roster_adjustment: TradeRosterAdjustment

    @property
    def primary_raw_delta(self) -> float:
        return self.primary.raw_after - self.primary.raw_before

    @property
    def primary_display_delta(self) -> float:
        return self.primary.display_delta

    @property
    def counterparty_raw_delta(self) -> float:
        return self.counterparty.raw_after - self.counterparty.raw_before

    @property
    def counterparty_display_delta(self) -> float:
        return self.counterparty.display_delta


@dataclass(frozen=True, slots=True, init=False)
class PreparedTradePair:
    """Two full rosters with their expensive pre-trade scores cached once."""

    model: StrengthModel
    primary: TeamRoster
    counterparty: TeamRoster
    primary_before: RosterStrength
    counterparty_before: RosterStrength
    _primary_ids: frozenset[str] = field(repr=False, compare=False)
    _counterparty_ids: frozenset[str] = field(repr=False, compare=False)
    adjuster: PreparedRosterAdjuster | None = field(repr=False, compare=False)

    def __init__(
        self,
        model: StrengthModel,
        primary: TeamRoster,
        counterparty: TeamRoster,
        adjuster: PreparedRosterAdjuster | None = None,
    ) -> None:
        if not isinstance(model, StrengthModel):
            raise ValueError("model must be a StrengthModel")
        if not isinstance(primary, TeamRoster) or not isinstance(counterparty, TeamRoster):
            raise ValueError("primary and counterparty must be TeamRoster values")
        if primary.team_id == counterparty.team_id:
            raise ValueError("prepared trade teams must have different team_id values")
        if primary.current_size != len(primary.player_ids):
            raise ValueError("primary must contain its full current roster")
        if counterparty.current_size != len(counterparty.player_ids):
            raise ValueError("counterparty must contain its full current roster")

        primary_ids = frozenset(primary.player_ids)
        counterparty_ids = frozenset(counterparty.player_ids)
        if primary_ids.intersection(counterparty_ids):
            raise ValueError("a player_id cannot be owned by both prepared teams")
        if adjuster is not None and not isinstance(adjuster, PreparedRosterAdjuster):
            raise ValueError("adjuster must be a PreparedRosterAdjuster or None")
        if adjuster is not None and adjuster.model != model:
            raise ValueError("adjuster and prepared pair must use the same strength model")

        # These are the only two baseline scores for the lifetime of this object.
        primary_before = model.score_roster(primary.player_ids)
        counterparty_before = model.score_roster(counterparty.player_ids)

        object.__setattr__(self, "model", model)
        object.__setattr__(self, "primary", primary)
        object.__setattr__(self, "counterparty", counterparty)
        object.__setattr__(self, "primary_before", primary_before)
        object.__setattr__(self, "counterparty_before", counterparty_before)
        object.__setattr__(self, "_primary_ids", primary_ids)
        object.__setattr__(self, "_counterparty_ids", counterparty_ids)
        object.__setattr__(self, "adjuster", adjuster)

    def evaluate(
        self,
        candidate: TradeCandidate,
        *,
        candidate_index: int,
    ) -> TradePowerEvaluation:
        """Score only the two post-trade rosters for one validated candidate."""

        index = _candidate_index(candidate_index)
        outgoing, incoming = self._validated_packages(candidate)
        outgoing_set = set(outgoing)
        incoming_set = set(incoming)
        normalized_candidate = TradeCandidate(outgoing, incoming)

        adjustment = (
            self.adjuster.adjust_trade(self.primary, self.counterparty, normalized_candidate)
            if self.adjuster is not None
            else unchanged_trade_adjustment(
                self.model, self.primary, self.counterparty, normalized_candidate
            )
        )
        primary_after_ids = adjustment.primary.roster.player_ids
        counterparty_after_ids = adjustment.counterparty.roster.player_ids

        primary_after = self.model.score_roster(primary_after_ids)
        counterparty_after = self.model.score_roster(counterparty_after_ids)
        return TradePowerEvaluation(
            candidate_index=index,
            candidate=normalized_candidate,
            primary=PowerRankingChange(
                self.primary.team_id,
                self.primary_before.power_score,
                primary_after.power_score,
            ),
            counterparty=PowerRankingChange(
                self.counterparty.team_id,
                self.counterparty_before.power_score,
                counterparty_after.power_score,
            ),
            roster_adjustment=adjustment,
        )

    def prefilter(
        self,
        trade_space: TradeSpace,
        *,
        minimum_displayed_power_delta: float = -5.0,
        start_candidate_index: int = 0,
    ) -> "TradeStrengthPrefilter":
        """Create a lazy, order-preserving strength filter for this pair."""

        return TradeStrengthPrefilter(
            self,
            trade_space,
            minimum_displayed_power_delta=minimum_displayed_power_delta,
            start_candidate_index=start_candidate_index,
        )

    def _validated_packages(
        self,
        candidate: TradeCandidate,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not isinstance(candidate, TradeCandidate):
            raise ValueError("candidate must be a TradeCandidate")
        outgoing = _package("outgoing package", candidate.outgoing_player_ids)
        incoming = _package("incoming package", candidate.incoming_player_ids)
        if not set(outgoing).issubset(self._primary_ids):
            raise ValueError("outgoing package contains a player not on the primary roster")
        if not set(incoming).issubset(self._counterparty_ids):
            raise ValueError(
                "incoming package contains a player not on the counterparty roster"
            )
        return outgoing, incoming


class TradeStrengthPrefilter(Iterator[TradePowerEvaluation]):
    """Single-use lazy results stream with live progress counters."""

    __slots__ = (
        "_prepared",
        "_candidates",
        "_minimum",
        "_start",
        "_examined",
        "_qualified",
    )

    def __init__(
        self,
        prepared: PreparedTradePair,
        trade_space: TradeSpace,
        *,
        minimum_displayed_power_delta: float = -5.0,
        start_candidate_index: int = 0,
    ) -> None:
        if not isinstance(prepared, PreparedTradePair):
            raise ValueError("prepared must be a PreparedTradePair")
        if not isinstance(trade_space, TradeSpace):
            raise ValueError("trade_space must be a TradeSpace")
        if (
            trade_space.primary != prepared.primary
            or trade_space.counterparty != prepared.counterparty
        ):
            raise ValueError("trade_space rosters must match the prepared trade pair")
        minimum = _finite_threshold(minimum_displayed_power_delta)
        start = _candidate_index(start_candidate_index)
        if start > trade_space.candidate_count:
            raise ValueError("start_candidate_index exceeds the trade-space count")

        self._prepared = prepared
        self._candidates = enumerate(islice(iter(trade_space), start, None), start)
        self._minimum = minimum
        self._start = start
        self._examined = 0
        self._qualified = 0

    @property
    def minimum_displayed_power_delta(self) -> float:
        return self._minimum

    @property
    def start_candidate_index(self) -> int:
        return self._start

    @property
    def examined_count(self) -> int:
        return self._examined

    @property
    def qualified_count(self) -> int:
        return self._qualified

    def __iter__(self) -> "TradeStrengthPrefilter":
        return self

    def __next__(self) -> TradePowerEvaluation:
        while True:
            candidate_index, candidate = next(self._candidates)
            self._examined += 1
            result = self._prepared.evaluate(
                candidate,
                candidate_index=candidate_index,
            )
            if (
                result.primary_display_delta >= self._minimum
                and result.counterparty_display_delta >= self._minimum
            ):
                self._qualified += 1
                return result


def _candidate_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("candidate_index must be a non-negative integer")
    return value


def _package(name: str, values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection of player IDs")
    try:
        normalized = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be a collection of player IDs") from None
    if not normalized:
        raise ValueError("both trade packages must contain at least one player")
    if any(not isinstance(player_id, str) or not player_id for player_id in normalized):
        raise ValueError(f"{name} must contain non-empty string player IDs")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} contains a duplicate player_id")
    return normalized


def _finite_threshold(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("minimum_displayed_power_delta must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("minimum_displayed_power_delta must be a finite number") from None
    if not isfinite(normalized):
        raise ValueError("minimum_displayed_power_delta must be a finite number")
    return normalized
