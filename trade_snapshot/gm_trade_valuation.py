"""Leakage-safe retrospective valuation of verified completed league trades.

Only a weekly engine captured strictly before ESPN's recorded proposal time may
value a completed trade.  Because ESPN does not expose the execution timestamp,
valuation is withheld when an intervening move makes ordering ambiguous.  This
keeps current ECR and current rosters out of historical conclusions.
"""

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from types import MappingProxyType

from ._league_history_evidence import (
    captured_transaction_evidence,
    transaction_executed_by,
)
from ._league_history_health import (
    NON_PHYSICAL_UNAVAILABLE_STATUSES,
    PHYSICAL_INJURY_STATUSES,
)
from ._gm_model_evidence import (
    GmModelEvidence,
    POWER_RESULT_STATUSES,
    build_gm_model_evidence,
    model_comparability_reasons,
)
from ._scenario_random import content_id
from .engine_bundle import EngineBundle
from .league_history import (
    HISTORY_CAPTURE_BINDING_TOLERANCE,
    HistoryTransaction,
    HistoryTransactionAssetKind,
    HistoryTransactionKind,
    LeagueHistorySnapshot,
)
from .scenario_config import CorrelatedScenarioConfig
from .trade_impact import prepare_season_baseline
from .trade_space import TeamRoster


_MAX_VALUATION_LAG = timedelta(days=8)
_MAX_PLAYOFF_SCENARIOS = 2_000
_MAX_HEALTH_COVERAGE_GAP = timedelta(days=8)


@dataclass(frozen=True, slots=True)
class HistoricalTeamOutcome:
    team_id: str
    power_delta: float
    relative_power_edge: float
    playoff_probability_delta: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.team_id, str) or not self.team_id:
            raise ValueError("team_id must be a non-empty string")
        for name in ("power_delta", "relative_power_edge"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be finite")
            if not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        playoff_delta = self.playoff_probability_delta
        if playoff_delta is not None and (
            isinstance(playoff_delta, bool)
            or not isinstance(playoff_delta, (int, float))
            or not isfinite(float(playoff_delta))
            or not -1 <= playoff_delta <= 1
        ):
            raise ValueError(
                "playoff_probability_delta must be between -1 and 1 or null"
            )


@dataclass(frozen=True, slots=True)
class HistoricalPlayoffEvidence:
    """Content-addressed paired-simulation evidence for a playoff delta."""

    scenario_count: int
    scenario_config_id: str
    player_score_floor: float | None
    projection_set_id: str
    impact_id: str
    before_scenario_run_id: str
    after_scenario_run_id: str
    draw_space_id: str

    def __post_init__(self) -> None:
        if type(self.scenario_count) is not int or self.scenario_count < 1:
            raise ValueError("scenario_count must be a positive integer")
        for name in (
            "scenario_config_id",
            "projection_set_id",
            "impact_id",
            "before_scenario_run_id",
            "after_scenario_run_id",
            "draw_space_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        floor = self.player_score_floor
        if floor is not None and (
            isinstance(floor, bool)
            or not isinstance(floor, (int, float))
            or not isfinite(float(floor))
        ):
            raise ValueError("player_score_floor must be finite numeric data or null")
        if floor is not None:
            object.__setattr__(self, "player_score_floor", float(floor))

    def to_record(self) -> dict[str, object]:
        return {
            "scenario_count": self.scenario_count,
            "scenario_config_id": self.scenario_config_id,
            "player_score_floor": self.player_score_floor,
            "projection_set_id": self.projection_set_id,
            "impact_id": self.impact_id,
            "before_scenario_run_id": self.before_scenario_run_id,
            "after_scenario_run_id": self.after_scenario_run_id,
            "draw_space_id": self.draw_space_id,
        }


@dataclass(frozen=True, slots=True)
class CurrentTeamRevaluation:
    """The same historical team/package scored by the selected current model."""

    team_id: str
    power_delta: float
    relative_power_edge: float
    relative_power_edge_drift: float

    def __post_init__(self) -> None:
        if not isinstance(self.team_id, str) or not self.team_id:
            raise ValueError("team_id must be a non-empty string")
        for name in (
            "power_delta",
            "relative_power_edge",
            "relative_power_edge_drift",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class CurrentTradeRevaluation:
    """A hindsight revaluation, kept distinct from the at-time outcome."""

    bundle_id: str
    bundle_captured_at: datetime
    methodology_status: str
    model_evidence: GmModelEvidence
    model_comparability_reasons: tuple[str, ...]
    foresight_eligible: bool
    foresight_ineligibility_reasons: tuple[str, ...]
    outcomes: tuple[CurrentTeamRevaluation, ...]

    def __post_init__(self) -> None:
        for name in ("bundle_id", "methodology_status"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        captured_at = _aware("bundle_captured_at", self.bundle_captured_at)
        if self.methodology_status not in POWER_RESULT_STATUSES:
            raise ValueError("methodology_status is invalid")
        if not isinstance(self.model_evidence, GmModelEvidence):
            raise ValueError("model_evidence must be GmModelEvidence")
        if (
            self.model_evidence.bundle_id != self.bundle_id
            or self.model_evidence.methodology_status != self.methodology_status
        ):
            raise ValueError("model_evidence does not match current revaluation")
        model_reasons = tuple(sorted(set(self.model_comparability_reasons)))
        if any(not isinstance(reason, str) or not reason for reason in model_reasons):
            raise ValueError("model comparability reasons must be non-empty strings")
        if not isinstance(self.foresight_eligible, bool):
            raise ValueError("foresight_eligible must be a boolean")
        reasons = tuple(sorted(set(self.foresight_ineligibility_reasons)))
        if any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ValueError("foresight ineligibility reasons must be non-empty strings")
        if self.foresight_eligible == bool(reasons):
            raise ValueError(
                "foresight eligibility must be true exactly when no reasons exist"
            )
        if not set(model_reasons).issubset(reasons):
            raise ValueError(
                "model comparability reasons must also be foresight reasons"
            )
        outcomes = tuple(self.outcomes)
        if any(not isinstance(row, CurrentTeamRevaluation) for row in outcomes):
            raise ValueError("outcomes must contain CurrentTeamRevaluation values")
        if len(outcomes) != 2 or len({row.team_id for row in outcomes}) != 2:
            raise ValueError("a current revaluation must contain two team outcomes")
        if abs(sum(row.relative_power_edge for row in outcomes)) > 1e-9:
            raise ValueError("current relative power edges must sum to zero")
        if abs(sum(row.relative_power_edge_drift for row in outcomes)) > 1e-9:
            raise ValueError("relative power edge drifts must sum to zero")
        object.__setattr__(self, "bundle_captured_at", captured_at)
        object.__setattr__(self, "model_comparability_reasons", model_reasons)
        object.__setattr__(self, "foresight_ineligibility_reasons", reasons)
        object.__setattr__(
            self, "outcomes", tuple(sorted(outcomes, key=lambda row: row.team_id))
        )


@dataclass(frozen=True, slots=True)
class HistoricalTradeValuation:
    transaction_id: str
    proposal_at: datetime
    analysis_as_of: datetime
    source_bundle_id: str
    source_bundle_captured_at: datetime
    valuation_lag_hours: float
    methodology_status: str
    source_model_evidence: GmModelEvidence
    playoff_evidence: HistoricalPlayoffEvidence | None
    playoff_unavailable_reason: str | None
    outcomes: tuple[HistoricalTeamOutcome, ...]
    current_revaluation: CurrentTradeRevaluation | None
    current_revaluation_unavailable_reason: str | None

    def __post_init__(self) -> None:
        for name in ("transaction_id", "source_bundle_id", "methodology_status"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        proposal_at = _aware("proposal_at", self.proposal_at)
        analysis_as_of = _aware("analysis_as_of", self.analysis_as_of)
        source_at = _aware(
            "source_bundle_captured_at", self.source_bundle_captured_at
        )
        if analysis_as_of < proposal_at:
            raise ValueError("analysis_as_of cannot predate the transaction")
        if not 0 < self.valuation_lag_hours <= _MAX_VALUATION_LAG.total_seconds() / 3600:
            raise ValueError("valuation_lag_hours is outside the contemporaneous window")
        expected_lag = (proposal_at - source_at).total_seconds() / 3600
        if abs(expected_lag - self.valuation_lag_hours) > 1e-9:
            raise ValueError("valuation_lag_hours does not match its evidence timestamps")
        if self.methodology_status not in POWER_RESULT_STATUSES:
            raise ValueError("methodology_status is invalid")
        if not isinstance(self.source_model_evidence, GmModelEvidence):
            raise ValueError("source_model_evidence must be GmModelEvidence")
        if (
            self.source_model_evidence.bundle_id != self.source_bundle_id
            or self.source_model_evidence.methodology_status
            != self.methodology_status
        ):
            raise ValueError("source_model_evidence does not match valuation")
        if self.playoff_evidence is not None and not isinstance(
            self.playoff_evidence, HistoricalPlayoffEvidence
        ):
            raise ValueError(
                "playoff_evidence must be HistoricalPlayoffEvidence or null"
            )
        playoff_reason = self.playoff_unavailable_reason
        if playoff_reason is not None and (
            not isinstance(playoff_reason, str) or not playoff_reason
        ):
            raise ValueError("playoff_unavailable_reason must be non-empty or null")
        if (self.playoff_evidence is None) != (playoff_reason is not None):
            raise ValueError(
                "playoff availability must match its unavailable reason"
            )
        outcomes = tuple(self.outcomes)
        if any(not isinstance(row, HistoricalTeamOutcome) for row in outcomes):
            raise ValueError("outcomes must contain HistoricalTeamOutcome values")
        if len(outcomes) != 2 or len({row.team_id for row in outcomes}) != 2:
            raise ValueError("a valued trade must contain exactly two team outcomes")
        playoff_values_available = all(
            row.playoff_probability_delta is not None for row in outcomes
        )
        if playoff_values_available != (self.playoff_evidence is not None):
            raise ValueError(
                "playoff outcome availability must match the scenario evidence"
            )
        if abs(sum(row.relative_power_edge for row in outcomes)) > 1e-9:
            raise ValueError("relative power edges must sum to zero")
        current = self.current_revaluation
        reason = self.current_revaluation_unavailable_reason
        if (current is None) == (reason is None):
            raise ValueError(
                "current revaluation and its unavailable reason are mutually exclusive"
            )
        if current is not None:
            if not isinstance(current, CurrentTradeRevaluation):
                raise ValueError("current_revaluation has an invalid type")
            at_time = {row.team_id: row for row in outcomes}
            if {row.team_id for row in current.outcomes} != set(at_time):
                raise ValueError("then/current revaluation team identities differ")
            for row in current.outcomes:
                expected = row.relative_power_edge - at_time[row.team_id].relative_power_edge
                if abs(row.relative_power_edge_drift - expected) > 1e-9:
                    raise ValueError(
                        "relative power edge drift does not match then/current values"
                    )
        elif not isinstance(reason, str) or not reason:
            raise ValueError("current revaluation unavailable reason must be non-empty")
        object.__setattr__(self, "proposal_at", proposal_at)
        object.__setattr__(self, "analysis_as_of", analysis_as_of)
        object.__setattr__(self, "source_bundle_captured_at", source_at)
        object.__setattr__(
            self, "outcomes", tuple(sorted(outcomes, key=lambda row: row.team_id))
        )

    @property
    def playoff_scenario_count(self) -> int | None:
        return (
            None
            if self.playoff_evidence is None
            else self.playoff_evidence.scenario_count
        )


@dataclass(frozen=True, slots=True)
class HistoricalValuationResult:
    valuations: tuple[HistoricalTradeValuation, ...]
    unvalued_reasons: Mapping[str, int]
    unvalued_transactions: Mapping[str, str]
    analysis_as_of: datetime
    history_revision: str
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        rows = tuple(self.valuations)
        if any(not isinstance(row, HistoricalTradeValuation) for row in rows):
            raise ValueError("valuations must contain HistoricalTradeValuation values")
        rows = tuple(
            sorted(rows, key=lambda row: (row.proposal_at, row.transaction_id))
        )
        if len({row.transaction_id for row in rows}) != len(rows):
            raise ValueError("valuations contain duplicate transaction IDs")
        reasons = dict(sorted(self.unvalued_reasons.items()))
        if any(
            not isinstance(name, str)
            or not name
            or type(count) is not int
            or count < 1
            for name, count in reasons.items()
        ):
            raise ValueError("unvalued reasons are invalid")
        unvalued = dict(sorted(self.unvalued_transactions.items()))
        if any(
            not isinstance(transaction_id, str)
            or not transaction_id
            or not isinstance(reason, str)
            or not reason
            for transaction_id, reason in unvalued.items()
        ):
            raise ValueError("unvalued transaction reasons are invalid")
        if set(unvalued).intersection(row.transaction_id for row in rows):
            raise ValueError("a transaction cannot be both valued and unvalued")
        if Counter(unvalued.values()) != Counter(reasons):
            raise ValueError("unvalued transaction reasons do not match reason counts")
        analysis_as_of = _aware("analysis_as_of", self.analysis_as_of)
        if any(row.analysis_as_of != analysis_as_of for row in rows):
            raise ValueError("valuation analysis cutoffs are inconsistent")
        if not isinstance(self.history_revision, str) or not self.history_revision:
            raise ValueError("history_revision must be non-empty text")
        object.__setattr__(self, "valuations", rows)
        object.__setattr__(self, "unvalued_reasons", MappingProxyType(reasons))
        object.__setattr__(
            self, "unvalued_transactions", MappingProxyType(unvalued)
        )
        object.__setattr__(self, "analysis_as_of", analysis_as_of)
        object.__setattr__(
            self,
            "evidence_id",
            content_id(
                "historical-valuation-evidence",
                {
                    "analysis_as_of": analysis_as_of.isoformat(),
                    "history_revision": self.history_revision,
                    "valued_trades": [
                        {
                            "transaction_id": row.transaction_id,
                            "source_model_evidence_id": (
                                row.source_model_evidence.evidence_id
                            ),
                            "playoff_impact_id": (
                                None
                                if row.playoff_evidence is None
                                else row.playoff_evidence.impact_id
                            ),
                            "current_model_evidence_id": (
                                None
                                if row.current_revaluation is None
                                else row.current_revaluation.model_evidence.evidence_id
                            ),
                        }
                        for row in rows
                    ],
                    "unvalued_transactions": unvalued,
                },
            ),
        )


def value_historical_trades(
    history: LeagueHistorySnapshot,
    *,
    as_of: datetime,
    bundle_loader: Callable[[str], EngineBundle],
    current_bundle: EngineBundle | None = None,
) -> HistoricalValuationResult:
    """Value trades at the time, then optionally revalue the same context now.

    The selected current model is never a fallback for missing at-time evidence.
    It is used only after a strictly prior bundle reconstructs the pre-trade
    rosters and packages.
    """

    if not isinstance(history, LeagueHistorySnapshot):
        raise ValueError("history must be a LeagueHistorySnapshot")
    cutoff = _aware("as_of", as_of)
    if not callable(bundle_loader):
        raise ValueError("bundle_loader must be callable")
    if current_bundle is not None and (
        not isinstance(current_bundle, EngineBundle)
        or current_bundle.bundle_id != history.bundle_id
        or current_bundle.state.season != history.season
    ):
        raise ValueError("current_bundle must be the selected history bundle")
    if current_bundle is not None and cutoff != history.bundle_captured_at:
        raise ValueError(
            "as_of must equal the selected current bundle capture time"
        )
    captures = tuple(row for row in history.captures if row.captured_at <= cutoff)
    transactions, first_observed_at = captured_transaction_evidence(captures)
    transactions = tuple(row for row in transactions if row.recorded_at <= cutoff)
    trades = tuple(
        row for row in transactions if row.kind is HistoryTransactionKind.TRADE
    )
    bindings = tuple(
        row for row in history.bundle_bindings if row.captured_at <= cutoff
    )
    loaded: dict[str, EngineBundle | None] = {}
    valuations = []
    reasons: Counter[str] = Counter()
    unvalued_transactions: dict[str, str] = {}

    def reject(trade: HistoryTransaction, reason: str) -> None:
        reasons[reason] += 1
        unvalued_transactions[trade.transaction_id] = reason

    for trade in trades:
        if any(
            asset.asset_kind is not HistoryTransactionAssetKind.PLAYER
            or asset.canonical_player_id is None
            for asset in trade.assets
        ):
            reject(trade, "trade_contains_unsupported_or_unresolved_asset")
            continue
        binding = _prior_binding(bindings, trade.recorded_at)
        if binding is None:
            reject(trade, "no_strictly_prior_weekly_model")
            continue
        if trade.recorded_at - binding.captured_at > _MAX_VALUATION_LAG:
            reject(trade, "prior_weekly_model_is_stale")
            continue
        if not _has_complete_transaction_window(
            captures,
            binding.captured_at,
            trade,
            first_observed_at,
        ):
            reject(trade, "transaction_history_is_incomplete")
            continue
        if binding.bundle_id not in loaded:
            try:
                candidate = bundle_loader(binding.bundle_id)
            except (FileNotFoundError, KeyError, ValueError):
                candidate = None
            if not isinstance(candidate, EngineBundle) or (
                candidate.bundle_id != binding.bundle_id
            ):
                candidate = None
            loaded[binding.bundle_id] = candidate
        bundle = loaded[binding.bundle_id]
        if bundle is None or bundle.state.season != history.season:
            reject(trade, "prior_weekly_model_is_unavailable")
            continue
        try:
            valuation = _value_one(
                trade,
                binding.captured_at,
                bundle,
                transactions,
                analysis_as_of=cutoff,
                current_bundle=current_bundle,
                current_bundle_captured_at=history.bundle_captured_at,
                history_captures=captures,
                first_observed_at=first_observed_at,
            )
        except ValueError:
            reject(trade, "roster_or_player_evidence_is_incomplete")
            continue
        valuations.append(valuation)

    return HistoricalValuationResult(
        valuations=tuple(valuations),
        unvalued_reasons=reasons,
        unvalued_transactions=unvalued_transactions,
        analysis_as_of=cutoff,
        history_revision=history.history_revision,
    )


def _prior_binding(bindings, transaction_at):
    eligible = [row for row in bindings if row.captured_at < transaction_at]
    return max(eligible, key=lambda row: (row.captured_at, row.bundle_id), default=None)


def _has_complete_transaction_window(
    captures,
    binding_at,
    trade,
    first_observed_at,
):
    executed_by = transaction_executed_by(trade, first_observed_at)
    if executed_by is None:
        return False
    return any(
        capture.transaction_history_complete
        and capture.coverage_start <= binding_at
        and capture.coverage_end >= executed_by
        and any(
            row.transaction_id == trade.transaction_id
            for row in capture.transactions
        )
        for capture in captures
    )


def _value_one(
    trade,
    binding_at,
    bundle,
    transactions,
    *,
    analysis_as_of,
    current_bundle,
    current_bundle_captured_at,
    history_captures,
    first_observed_at,
):
    participants, outgoing = _trade_packages(trade)
    owners, exempt, playoff_context_known = _rosters_at(
        bundle,
        transactions,
        first_observed_at,
        binding_at,
        trade,
        participants,
    )
    first, second = participants
    if not set(outgoing[first]).issubset(owners[first]) or not set(
        outgoing[second]
    ).issubset(owners[second]):
        raise ValueError("trade players do not match the reconstructed rosters")
    result = bundle.strength_model.evaluate_trade(
        primary_roster=owners[first],
        counterparty_roster=owners[second],
        outgoing_player_ids=outgoing[first],
        incoming_player_ids=outgoing[second],
    )
    power_deltas, relative = _power_values(result, first, second)
    source_model_evidence = build_gm_model_evidence(
        bundle,
        outgoing_count=len(outgoing[first]),
        incoming_count=len(outgoing[second]),
    )
    playoff, playoff_evidence, playoff_unavailable = (
        _playoff_deltas(bundle, owners, exempt, trade, participants)
        if playoff_context_known
        else ({}, None, "intervening_league_move_order_is_ambiguous")
    )
    at_time_outcomes = tuple(
        HistoricalTeamOutcome(
            team_id,
            power_deltas[team_id],
            relative[team_id],
            playoff.get(team_id),
        )
        for team_id in participants
    )
    current, current_unavailable = _current_revaluation(
        source_bundle=bundle,
        source_model_evidence=source_model_evidence,
        current_bundle=current_bundle,
        current_bundle_captured_at=current_bundle_captured_at,
        source_bundle_captured_at=binding_at,
        owners=owners,
        outgoing=outgoing,
        participants=participants,
        at_time_outcomes=at_time_outcomes,
        history_captures=history_captures,
        traded_player_ids=frozenset(
            player_id for package in outgoing.values() for player_id in package
        ),
    )
    return HistoricalTradeValuation(
        transaction_id=trade.transaction_id,
        proposal_at=trade.recorded_at,
        analysis_as_of=analysis_as_of,
        source_bundle_id=bundle.bundle_id,
        source_bundle_captured_at=binding_at,
        valuation_lag_hours=(trade.recorded_at - binding_at).total_seconds() / 3600,
        methodology_status=source_model_evidence.methodology_status,
        source_model_evidence=source_model_evidence,
        playoff_evidence=playoff_evidence,
        playoff_unavailable_reason=playoff_unavailable,
        outcomes=at_time_outcomes,
        current_revaluation=current,
        current_revaluation_unavailable_reason=current_unavailable,
    )


def _current_revaluation(
    *,
    source_bundle,
    source_model_evidence,
    current_bundle,
    current_bundle_captured_at,
    source_bundle_captured_at,
    owners,
    outgoing,
    participants,
    at_time_outcomes,
    history_captures,
    traded_player_ids,
):
    if current_bundle is None:
        return None, "selected_current_bundle_not_supplied"
    if (
        current_bundle.state.scoring_profile_id
        != source_bundle.state.scoring_profile_id
    ):
        return None, "current_scoring_profile_is_not_comparable"
    historical_players = {
        player_id
        for team_id in participants
        for player_id in owners[team_id]
    }
    if not historical_players.issubset(current_bundle.strength_model.players):
        return None, "current_model_is_missing_historical_roster_players"
    first, second = participants
    try:
        result = current_bundle.strength_model.evaluate_trade(
            primary_roster=owners[first],
            counterparty_roster=owners[second],
            outgoing_player_ids=outgoing[first],
            incoming_player_ids=outgoing[second],
        )
    except ValueError:
        return None, "current_model_cannot_score_historical_roster_context"
    power_deltas, relative = _power_values(result, first, second)
    current_model_evidence = build_gm_model_evidence(
        current_bundle,
        outgoing_count=len(outgoing[first]),
        incoming_count=len(outgoing[second]),
    )
    at_time = {row.team_id: row for row in at_time_outcomes}
    eligibility_reasons = []
    comparison_reasons = model_comparability_reasons(
        source_model_evidence, current_model_evidence
    )
    eligibility_reasons.extend(comparison_reasons)
    eligibility_reasons.extend(
        _health_eligibility_reasons(
            history_captures,
            traded_player_ids,
            source_bundle_captured_at,
            current_bundle_captured_at,
        )
    )
    return (
        CurrentTradeRevaluation(
            bundle_id=current_bundle.bundle_id,
            bundle_captured_at=current_bundle_captured_at,
            methodology_status=current_model_evidence.methodology_status,
            model_evidence=current_model_evidence,
            model_comparability_reasons=comparison_reasons,
            foresight_eligible=not eligibility_reasons,
            foresight_ineligibility_reasons=tuple(eligibility_reasons),
            outcomes=tuple(
                CurrentTeamRevaluation(
                    team_id,
                    power_deltas[team_id],
                    relative[team_id],
                    relative[team_id] - at_time[team_id].relative_power_edge,
                )
                for team_id in participants
            ),
        ),
        None,
    )


def _health_eligibility_reasons(
    captures,
    traded_player_ids,
    source_bundle_captured_at,
    current_bundle_captured_at,
):
    """Return conservative reasons that prohibit a foresight interpretation."""

    if current_bundle_captured_at < source_bundle_captured_at:
        return ("current_bundle_predates_source_bundle",)
    ordered = tuple(sorted(captures, key=lambda row: (row.captured_at, row.capture_id)))
    source_capture = _boundary_capture(ordered, source_bundle_captured_at)
    current_capture = _boundary_capture(ordered, current_bundle_captured_at)
    reasons = []
    if source_capture is None:
        reasons.append("source_health_capture_missing")
    if current_capture is None:
        reasons.append("current_health_capture_missing")
    if reasons:
        return tuple(reasons)
    if not source_capture.roster_complete:
        reasons.append("source_health_roster_capture_incomplete")
    if not current_capture.roster_complete:
        reasons.append("current_health_roster_capture_incomplete")
    window = tuple(
        row
        for row in ordered
        if source_capture.captured_at <= row.captured_at <= current_capture.captured_at
    )
    if any(not row.roster_complete for row in window):
        reasons.append("intermediate_health_roster_capture_incomplete")
    complete = tuple(row for row in window if row.roster_complete)
    coverage_times = tuple(
        sorted(
            {
                source_bundle_captured_at,
                current_bundle_captured_at,
                *(row.captured_at for row in complete),
            }
        )
    )
    if any(
        later - earlier > _MAX_HEALTH_COVERAGE_GAP
        for earlier, later in zip(coverage_times, coverage_times[1:])
    ):
        reasons.append("health_capture_gap_exceeds_eight_days")

    for capture in complete:
        observations = {
            player.canonical_player_id: player.injury_status
            for roster in capture.rosters
            for player in roster.players
        }
        for player_id in traded_player_ids:
            status = observations.get(player_id)
            if status is None:
                reasons.append("traded_player_health_status_unknown")
            elif status in PHYSICAL_INJURY_STATUSES:
                reasons.append("physical_injury_status_observed")
            elif status in NON_PHYSICAL_UNAVAILABLE_STATUSES:
                reasons.append("non_physical_unavailability_observed")
            elif status != "ACTIVE":
                reasons.append("unrecognized_health_status_observed")
    return tuple(sorted(set(reasons)))


def _boundary_capture(captures, binding_at):
    eligible = [row for row in captures if row.captured_at <= binding_at]
    capture = max(
        eligible,
        key=lambda row: (row.captured_at, row.capture_id),
        default=None,
    )
    if (
        capture is None
        or binding_at - capture.captured_at
        > HISTORY_CAPTURE_BINDING_TOLERANCE
    ):
        return None
    return capture


def _power_values(result, first, second):
    power_deltas = {
        first: result.primary.power_delta,
        second: result.counterparty.power_delta,
    }
    relative = {
        first: power_deltas[first] - power_deltas[second],
        second: power_deltas[second] - power_deltas[first],
    }
    return power_deltas, relative


def _rosters_at(
    bundle,
    transactions,
    first_observed_at,
    binding_at,
    trade,
    participants,
):
    owners = {row.team_id: list(row.player_ids) for row in bundle.rosters}
    if not set(participants).issubset(owners):
        raise ValueError("the prior weekly model is missing a trade participant")
    exempt = {
        row.team_id: set(row.capacity_exempt_player_ids) for row in bundle.rosters
    }
    trade_executed_by = transaction_executed_by(trade, first_observed_at)
    if trade_executed_by is None:
        raise ValueError("trade has no capture observation bound")
    possible_intervening = tuple(
        row
        for row in transactions
        if row.transaction_id != trade.transaction_id
        and (
            executed_by := transaction_executed_by(row, first_observed_at)
        ) is not None
        and binding_at < executed_by
        and row.recorded_at <= trade_executed_by
    )
    participant_roster_ambiguous = any(
        set(row.participant_team_ids).intersection(participants)
        for row in possible_intervening
    )
    if participant_roster_ambiguous:
        raise ValueError("ESPN does not expose enough timing to order intervening moves")
    return owners, exempt, not possible_intervening


def _apply_event(owners, exempt, event: HistoryTransaction) -> None:
    removals = []
    additions = []
    for asset in event.assets:
        if asset.canonical_player_id is None:
            if asset.from_team_id in owners or asset.to_team_id in owners:
                raise ValueError("an intervening roster move has an unresolved player")
            continue
        if asset.from_team_id in owners:
            removals.append((asset.from_team_id, asset.canonical_player_id))
        if asset.to_team_id in owners:
            additions.append((asset.to_team_id, asset.canonical_player_id))
    for team_id, player_id in removals:
        if player_id not in owners[team_id]:
            raise ValueError("intervening transaction conflicts with roster ownership")
        owners[team_id].remove(player_id)
        exempt[team_id].discard(player_id)
    for team_id, player_id in additions:
        if any(player_id in roster for roster in owners.values()):
            raise ValueError("intervening transaction duplicates player ownership")
        owners[team_id].append(player_id)


def _trade_packages(trade):
    participants = sorted(trade.participant_team_ids)
    if len(participants) != 2:
        raise ValueError("historical valuation supports two-team trades")
    packages = {team_id: [] for team_id in participants}
    for asset in trade.assets:
        player_id = asset.canonical_player_id
        if (
            asset.asset_kind is not HistoryTransactionAssetKind.PLAYER
            or player_id is None
            or asset.from_team_id not in packages
            or asset.to_team_id not in packages
            or asset.from_team_id == asset.to_team_id
        ):
            raise ValueError("trade contains an unresolved or non-player asset")
        packages[asset.from_team_id].append(player_id)
    if any(not package for package in packages.values()):
        raise ValueError("both teams must send at least one player")
    return tuple(participants), {key: tuple(value) for key, value in packages.items()}


def _playoff_deltas(bundle, owners, exempt, trade, participants):
    roster_templates = {row.team_id: row for row in bundle.rosters}
    try:
        before = _team_rosters(roster_templates, owners, exempt)
        after_owners = {team_id: list(players) for team_id, players in owners.items()}
        after_exempt = {team_id: set(players) for team_id, players in exempt.items()}
        _apply_event(after_owners, after_exempt, trade)
        after = _team_rosters(roster_templates, after_owners, after_exempt)
        config = bundle.scenario_config
        if config.scenario_count > _MAX_PLAYOFF_SCENARIOS:
            config = CorrelatedScenarioConfig(
                _MAX_PLAYOFF_SCENARIOS,
                config.seed,
                config.loadings,
                config.player_score_floor,
            )
        baseline = prepare_season_baseline(
            bundle.state,
            before,
            bundle.projections,
            bundle.eligibilities,
            config,
        )
        paired = baseline.project(after)
    except ValueError:
        return {}, None, "playoff_simulation_inputs_are_incomplete"
    return (
        {
            team_id: paired.for_team(team_id).playoff_probability_delta
            for team_id in participants
        },
        HistoricalPlayoffEvidence(
            scenario_count=paired.before.scenario_count,
            scenario_config_id=baseline.scenarios.config.config_id,
            player_score_floor=baseline.scenarios.config.player_score_floor,
            projection_set_id=baseline.scenarios.projection_set_id,
            impact_id=paired.impact_id,
            before_scenario_run_id=paired.before_scenario_run_id,
            after_scenario_run_id=paired.after_scenario_run_id,
            draw_space_id=paired.draw_space_id,
        ),
        None,
    )


def _team_rosters(templates, owners, exempt):
    rows = []
    for team_id in sorted(owners):
        template = templates[team_id]
        players = tuple(owners[team_id])
        capacity_exempt = frozenset(exempt[team_id]).intersection(players)
        rows.append(
            TeamRoster(
                team_id,
                players,
                len(players),
                template.roster_cap,
                capacity_exempt,
            )
        )
    return tuple(rows)


def _aware(name, value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone aware")
    return value.astimezone(timezone.utc)


__all__ = (
    "CurrentTeamRevaluation",
    "CurrentTradeRevaluation",
    "HistoricalTeamOutcome",
    "HistoricalPlayoffEvidence",
    "HistoricalTradeValuation",
    "HistoricalValuationResult",
    "PHYSICAL_INJURY_STATUSES",
    "value_historical_trades",
)
