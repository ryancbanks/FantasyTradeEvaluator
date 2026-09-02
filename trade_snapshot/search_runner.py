"""Resumable power-prefiltered trade search with paired playoff simulation."""

from collections.abc import Callable
from dataclasses import dataclass
from itertools import islice
from math import isfinite
from numbers import Real
from pathlib import Path

from .search import PreparedTradePair, TradePowerEvaluation
from .search_store import (
    QualifiedSearchResult,
    SearchResumeState,
    SearchRunDefinition,
    SearchStore,
)
from .trade_impact import PreparedSeasonBaseline
from .trade_space import TeamRoster, TradeSpace


SEARCH_ALGORITHM = "local-power-paired-playoffs-v2"


@dataclass(frozen=True, slots=True)
class TradeSearchSettings:
    minimum_displayed_power_delta: float = -5.0
    checkpoint_interval: int = 1000

    def __post_init__(self) -> None:
        threshold = _finite("minimum_displayed_power_delta", self.minimum_displayed_power_delta)
        if type(self.checkpoint_interval) is not int or self.checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be a positive integer")
        object.__setattr__(self, "minimum_displayed_power_delta", threshold)

    def to_record(self) -> dict[str, object]:
        return {
            "checkpoint_interval": self.checkpoint_interval,
            "minimum_displayed_power_delta": self.minimum_displayed_power_delta,
        }


@dataclass(frozen=True, slots=True)
class TradeSearchProgress:
    run_id: str
    next_candidate_index: int
    total_candidate_count: int
    power_qualified_count: int
    playoff_evaluated_count: int
    mutual_playoff_gain_count: int
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        for name in (
            "next_candidate_index",
            "total_candidate_count",
            "power_qualified_count",
            "playoff_evaluated_count",
            "mutual_playoff_gain_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.next_candidate_index > self.total_candidate_count:
            raise ValueError("next_candidate_index cannot exceed total_candidate_count")
        if self.playoff_evaluated_count > self.power_qualified_count:
            raise ValueError("playoff_evaluated_count cannot exceed power_qualified_count")
        if self.mutual_playoff_gain_count > self.playoff_evaluated_count:
            raise ValueError("mutual gain count cannot exceed playoff evaluation count")
        if not isinstance(self.cancelled, bool):
            raise ValueError("cancelled must be a boolean")

    @property
    def completion_fraction(self) -> float:
        if self.total_candidate_count == 0:
            return 1.0
        return self.next_candidate_index / self.total_candidate_count


@dataclass(frozen=True, slots=True)
class TradeSearchOutcome:
    progress: TradeSearchProgress
    results: tuple[QualifiedSearchResult, ...]

    @property
    def mutual_playoff_gains(self) -> tuple[QualifiedSearchResult, ...]:
        return tuple(row for row in self.results if _is_mutual_gain(row))


class ResumableTradeSearch:
    """One exact candidate order bound to immutable weekly calculation inputs."""

    def __init__(
        self,
        trade_space: TradeSpace,
        prepared_strength: PreparedTradePair,
        season_baseline: PreparedSeasonBaseline,
        settings: TradeSearchSettings | None = None,
    ) -> None:
        if not isinstance(trade_space, TradeSpace):
            raise ValueError("trade_space must be a TradeSpace")
        if not isinstance(prepared_strength, PreparedTradePair):
            raise ValueError("prepared_strength must be a PreparedTradePair")
        if not isinstance(season_baseline, PreparedSeasonBaseline):
            raise ValueError("season_baseline must be a PreparedSeasonBaseline")
        if settings is None:
            settings = TradeSearchSettings()
        if not isinstance(settings, TradeSearchSettings):
            raise ValueError("settings must be TradeSearchSettings")
        if not _same_roster(trade_space.primary, prepared_strength.primary) or not _same_roster(
            trade_space.counterparty, prepared_strength.counterparty
        ):
            raise ValueError("trade space and prepared strength teams do not match")
        if not trade_space.constraints.require_no_drops and prepared_strength.adjuster is None:
            raise ValueError("trades allowing roster drops require a prepared roster adjuster")
        baseline_by_team = {
            roster.team_id: roster for roster in season_baseline.scenarios.rosters
        }
        for roster in (prepared_strength.primary, prepared_strength.counterparty):
            baseline_roster = baseline_by_team.get(roster.team_id)
            if baseline_roster is None or not _same_roster(baseline_roster, roster):
                raise ValueError("strength roster does not match the season baseline")
        model_identity = (
            prepared_strength.model.snapshot_id,
            prepared_strength.model.season,
            prepared_strength.model.scoring_profile_id,
        )
        state_identity = (
            season_baseline.state.snapshot_id,
            season_baseline.state.season,
            season_baseline.state.scoring_profile_id,
        )
        if model_identity != state_identity:
            raise ValueError("strength model and season baseline engine identity do not match")
        self.trade_space = trade_space
        self.prepared_strength = prepared_strength
        self.season_baseline = season_baseline
        self.settings = settings
        self.run_definition = SearchRunDefinition(
            snapshot_id=season_baseline.state.snapshot_id,
            strength_model_id=prepared_strength.model.model_id,
            primary_team_id=prepared_strength.primary.team_id,
            counterparty_team_id=prepared_strength.counterparty.team_id,
            trade_constraint_record={
                "algorithm": SEARCH_ALGORITHM,
                "candidate_order": {
                    "counterparty_capacity_exempt_player_ids": sorted(
                        trade_space.counterparty.capacity_exempt_player_ids
                    ),
                    "counterparty_player_ids": list(trade_space.counterparty.player_ids),
                    "primary_capacity_exempt_player_ids": sorted(
                        trade_space.primary.capacity_exempt_player_ids
                    ),
                    "primary_player_ids": list(trade_space.primary.player_ids),
                },
                "scenario_run_id": season_baseline.scenarios.run_id,
                "roster_adjustment_id": (
                    None
                    if prepared_strength.adjuster is None
                    else prepared_strength.adjuster.adjustment_id
                ),
                "season_projection_options": {
                    "score_decimal_places": season_baseline.score_decimal_places,
                    "tiebreak_random_seed": season_baseline.tiebreak_random_seed,
                },
                "settings": settings.to_record(),
                "trade_constraints": trade_space.constraints.to_record(),
            },
            total_candidate_count=trade_space.candidate_count,
        )

    def run(
        self,
        database_path: str | Path,
        *,
        on_progress: Callable[[TradeSearchProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TradeSearchOutcome:
        if on_progress is not None and not callable(on_progress):
            raise ValueError("on_progress must be callable")
        if should_cancel is not None and not callable(should_cancel):
            raise ValueError("should_cancel must be callable")
        with SearchStore(database_path, self.run_definition) as store:
            state = store.resume()
            next_index = state.next_candidate_index
            saved_results = list(state.qualified_results)
            cancelled = False
            for index, candidate in enumerate(
                islice(iter(self.trade_space), next_index, None), next_index
            ):
                if should_cancel is not None and should_cancel():
                    store.checkpoint(index)
                    next_index = index
                    cancelled = True
                    break
                power = self.prepared_strength.evaluate(candidate, candidate_index=index)
                next_index = index + 1
                qualified = self._power_qualifies(power)
                if qualified:
                    saved = self._simulate_candidate(power)
                    store.upsert_qualified_result(saved, next_candidate_index=next_index)
                    saved_results.append(saved)
                elif next_index % self.settings.checkpoint_interval == 0:
                    store.checkpoint(next_index)
                if on_progress is not None and (
                    qualified
                    or next_index % self.settings.checkpoint_interval == 0
                ):
                    on_progress(
                        self._progress_values(
                            next_index, tuple(saved_results), cancelled=False
                        )
                    )
            else:
                store.checkpoint(self.trade_space.candidate_count)
                next_index = self.trade_space.candidate_count
            final_state = store.resume()
            if final_state.next_candidate_index != next_index:
                raise AssertionError("search checkpoint did not advance to the expected index")
            progress = self._progress(final_state, cancelled=cancelled)
            if on_progress is not None:
                on_progress(progress)
            return TradeSearchOutcome(progress, final_state.qualified_results)

    def _power_qualifies(self, result: TradePowerEvaluation) -> bool:
        threshold = self.settings.minimum_displayed_power_delta
        return (
            result.primary_display_delta >= threshold
            and result.counterparty_display_delta >= threshold
        )

    def _simulate_candidate(
        self, power: TradePowerEvaluation
    ) -> QualifiedSearchResult:
        after_rosters = _after_rosters(
            self.season_baseline.scenarios.rosters,
            power.roster_adjustment,
        )
        impact = self.season_baseline.project(after_rosters)
        primary = impact.for_team(self.prepared_strength.primary.team_id)
        counterparty = impact.for_team(self.prepared_strength.counterparty.team_id)
        return QualifiedSearchResult(
            candidate_index=power.candidate_index,
            outgoing_player_ids=power.candidate.outgoing_player_ids,
            incoming_player_ids=power.candidate.incoming_player_ids,
            primary_raw_power_delta=power.primary_raw_delta,
            primary_display_power_delta=power.primary_display_delta,
            counterparty_raw_power_delta=power.counterparty_raw_delta,
            counterparty_display_power_delta=power.counterparty_display_delta,
            primary_added_player_ids=power.roster_adjustment.primary.added_player_ids,
            primary_dropped_player_ids=power.roster_adjustment.primary.dropped_player_ids,
            counterparty_added_player_ids=(
                power.roster_adjustment.counterparty.added_player_ids
            ),
            counterparty_dropped_player_ids=(
                power.roster_adjustment.counterparty.dropped_player_ids
            ),
            primary_playoff_before=primary.before.playoff_probability * 100,
            primary_playoff_after=primary.after.playoff_probability * 100,
            counterparty_playoff_before=counterparty.before.playoff_probability * 100,
            counterparty_playoff_after=counterparty.after.playoff_probability * 100,
        )

    def _progress(
        self, state: SearchResumeState, *, cancelled: bool
    ) -> TradeSearchProgress:
        return self._progress_values(
            state.next_candidate_index,
            state.qualified_results,
            cancelled=cancelled,
        )

    def _progress_values(
        self,
        next_candidate_index: int,
        results: tuple[QualifiedSearchResult, ...],
        *,
        cancelled: bool,
    ) -> TradeSearchProgress:
        return TradeSearchProgress(
            run_id=self.run_definition.run_id,
            next_candidate_index=next_candidate_index,
            total_candidate_count=self.run_definition.total_candidate_count,
            power_qualified_count=len(results),
            playoff_evaluated_count=sum(row.primary_playoff_before is not None for row in results),
            mutual_playoff_gain_count=sum(_is_mutual_gain(row) for row in results),
            cancelled=cancelled,
        )


def _after_rosters(
    rosters: tuple[TeamRoster, ...],
    adjustment,
) -> tuple[TeamRoster, ...]:
    changed = {
        adjustment.primary.roster.team_id: adjustment.primary.roster,
        adjustment.counterparty.roster.team_id: adjustment.counterparty.roster,
    }
    result = []
    for roster in rosters:
        result.append(changed.get(roster.team_id, roster))
    return tuple(result)


def _is_mutual_gain(result: QualifiedSearchResult) -> bool:
    primary_before = result.primary_playoff_before
    primary_after = result.primary_playoff_after
    counterparty_before = result.counterparty_playoff_before
    counterparty_after = result.counterparty_playoff_after
    if (
        primary_before is None
        or primary_after is None
        or counterparty_before is None
        or counterparty_after is None
    ):
        return False
    return primary_after > primary_before and counterparty_after > counterparty_before


def _same_roster(left: TeamRoster, right: TeamRoster) -> bool:
    return (
        left.team_id == right.team_id
        and frozenset(left.player_ids) == frozenset(right.player_ids)
        and left.current_size == right.current_size
        and left.roster_cap == right.roster_cap
        and left.capacity_exempt_player_ids == right.capacity_exempt_player_ids
    )


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result
