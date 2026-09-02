"""Prepared power evaluation and resumable execution for three-team trades."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .analyzer_contract import PowerRankingChange
from .roster_adjustment import PreparedRosterAdjuster, TeamRosterAdjustment
from .search_runner import TradeSearchSettings
from .strength import RosterStrength, StrengthModel
from .three_way_search_records import (
    ThreeWayQualifiedResult,
    ThreeWaySearchProgress,
    ThreeWaySearchRunDefinition,
    ThreeWayTeamResult,
    _nonnegative_integer,
)
from .three_way_search_store import (
    THREE_WAY_DATABASE_SCHEMA_VERSION,
    ThreeWayResumeState,
    ThreeWaySearchRunMismatchError,
    ThreeWaySearchStore,
    ThreeWaySearchStoreError,
    read_three_way_results,
)
from .three_way_trade import ThreeWayTradeCandidate, ThreeWayTradeSpace, TradeTransfer
from .trade_impact import PreparedSeasonBaseline
from .trade_space import TeamRoster


THREE_WAY_SEARCH_ALGORITHM = "local-three-way-power-paired-playoffs-v1"


@dataclass(frozen=True, slots=True)
class ThreeWayPowerEvaluation:
    candidate_index: int
    candidate: ThreeWayTradeCandidate
    changes: tuple[PowerRankingChange, ...]
    adjustments: tuple[TeamRosterAdjustment, ...]

    def for_team(
        self, team_id: str
    ) -> tuple[PowerRankingChange, TeamRosterAdjustment]:
        for change, adjustment in zip(self.changes, self.adjustments):
            if change.team_id == team_id:
                return change, adjustment
        raise KeyError(team_id)


@dataclass(frozen=True, slots=True, init=False)
class PreparedThreeWayTrade:
    """Three complete rosters with their baseline power cached once."""

    model: StrengthModel
    rosters: tuple[TeamRoster, TeamRoster, TeamRoster]
    before_strengths: tuple[RosterStrength, RosterStrength, RosterStrength]
    adjuster: PreparedRosterAdjuster | None

    def __init__(
        self,
        model: StrengthModel,
        rosters: Iterable[TeamRoster],
        adjuster: PreparedRosterAdjuster | None = None,
    ) -> None:
        if not isinstance(model, StrengthModel):
            raise ValueError("model must be a StrengthModel")
        rows = tuple(rosters)
        _validate_three_rosters(rows, model)
        if adjuster is not None and not isinstance(adjuster, PreparedRosterAdjuster):
            raise ValueError("adjuster must be a PreparedRosterAdjuster or None")
        if adjuster is not None and adjuster.model != model:
            raise ValueError("adjuster and prepared trade must use the same model")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "rosters", rows)
        object.__setattr__(
            self,
            "before_strengths",
            tuple(model.score_roster(row.player_ids) for row in rows),
        )
        object.__setattr__(self, "adjuster", adjuster)

    def evaluate(
        self,
        candidate: ThreeWayTradeCandidate,
        *,
        candidate_index: int,
    ) -> ThreeWayPowerEvaluation:
        index = _nonnegative_integer("candidate_index", candidate_index)
        if not isinstance(candidate, ThreeWayTradeCandidate):
            raise ValueError("candidate must be a ThreeWayTradeCandidate")
        expected_ids = tuple(row.team_id for row in self.rosters)
        if tuple(candidate.participant_team_ids) != expected_ids:
            raise ValueError("candidate participants do not match the prepared rosters")
        _validate_transfer_ownership(candidate, self.rosters)
        changes = tuple(
            (
                roster,
                candidate.outgoing_for(roster.team_id),
                candidate.incoming_for(roster.team_id),
            )
            for roster in self.rosters
        )
        adjustments = (
            self.adjuster.adjust_teams(changes)
            if self.adjuster is not None
            else _pure_adjustments(changes, self.model)
        )
        power_changes = tuple(
            PowerRankingChange(
                roster.team_id,
                before.power_score,
                self.model.score_roster(adjustment.roster.player_ids).power_score,
            )
            for roster, before, adjustment in zip(
                self.rosters, self.before_strengths, adjustments
            )
        )
        return ThreeWayPowerEvaluation(
            index, candidate, power_changes, adjustments
        )


@dataclass(frozen=True, slots=True)
class ThreeWaySearchOutcome:
    progress: ThreeWaySearchProgress
    database_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.progress, ThreeWaySearchProgress):
            raise ValueError("progress must be ThreeWaySearchProgress")
        object.__setattr__(self, "database_path", Path(self.database_path).resolve())

    def results(
        self, limit: int | None = None
    ) -> tuple[ThreeWayQualifiedResult, ...]:
        return read_three_way_results(
            self.database_path,
            limit,
            expected_run_id=self.progress.run_id,
        )


class ResumableThreeWayTradeSearch:
    """Power-filter one exact three-team space, then run paired playoffs."""

    def __init__(
        self,
        space: ThreeWayTradeSpace,
        prepared: PreparedThreeWayTrade,
        baseline: PreparedSeasonBaseline,
        settings: TradeSearchSettings | None = None,
    ) -> None:
        if not isinstance(space, ThreeWayTradeSpace):
            raise ValueError("space must be a ThreeWayTradeSpace")
        if not isinstance(prepared, PreparedThreeWayTrade):
            raise ValueError("prepared must be a PreparedThreeWayTrade")
        if not isinstance(baseline, PreparedSeasonBaseline):
            raise ValueError("baseline must be a PreparedSeasonBaseline")
        if settings is None:
            settings = TradeSearchSettings()
        if not isinstance(settings, TradeSearchSettings):
            raise ValueError("settings must be TradeSearchSettings")
        if not all(
            _same_roster(left, right)
            for left, right in zip(space.rosters, prepared.rosters)
        ):
            raise ValueError("trade space and prepared rosters do not match")
        if space.constraints.require_no_drops and prepared.adjuster is not None:
            raise ValueError("no-drop trades require pure simultaneous roster changes")
        if not space.constraints.require_no_drops and prepared.adjuster is None:
            raise ValueError("trades allowing roster drops require a prepared adjuster")
        baseline_by_team = {row.team_id: row for row in baseline.scenarios.rosters}
        if any(
            roster.team_id not in baseline_by_team
            or not _same_roster(roster, baseline_by_team[roster.team_id])
            for roster in prepared.rosters
        ):
            raise ValueError("prepared rosters do not match the season baseline")
        if _model_identity(prepared.model) != _state_identity(baseline):
            raise ValueError("strength model and season baseline identities do not match")
        self.space = space
        self.prepared = prepared
        self.baseline = baseline
        self.settings = settings
        self.run_definition = _run_definition(space, prepared, baseline, settings)

    def run(
        self,
        database_path: str | Path,
        *,
        on_progress: Callable[[ThreeWaySearchProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ThreeWaySearchOutcome:
        if on_progress is not None and not callable(on_progress):
            raise ValueError("on_progress must be callable")
        if should_cancel is not None and not callable(should_cancel):
            raise ValueError("should_cancel must be callable")
        with ThreeWaySearchStore(database_path, self.run_definition) as store:
            path = store.path
            state = store.resume()
            next_index = state.next_candidate_index
            qualified_count = state.qualified_result_count
            gain_count = state.all_playoff_gain_count
            cancelled = False
            for index, candidate in enumerate(
                self.space.iter_from(next_index), start=next_index
            ):
                if should_cancel is not None and should_cancel():
                    store.checkpoint(index)
                    next_index, cancelled = index, True
                    break
                power = self.prepared.evaluate(candidate, candidate_index=index)
                next_index = index + 1
                qualified = self._power_qualifies(power)
                if qualified:
                    result = self._simulate_candidate(power)
                    store.upsert_qualified_result(
                        result, next_candidate_index=next_index
                    )
                    qualified_count += 1
                    gain_count += int(result.all_teams_gain)
                elif next_index % self.settings.checkpoint_interval == 0:
                    store.checkpoint(next_index)
                if on_progress is not None and (
                    qualified or next_index % self.settings.checkpoint_interval == 0
                ):
                    on_progress(
                        self._progress(next_index, qualified_count, gain_count, False)
                    )
            else:
                store.checkpoint(self.space.candidate_count)
                next_index = self.space.candidate_count
            final = store.resume()
            if final.next_candidate_index != next_index:
                raise AssertionError("search checkpoint did not advance as expected")
            progress = self._progress(
                next_index,
                final.qualified_result_count,
                final.all_playoff_gain_count,
                cancelled,
            )
            if on_progress is not None:
                on_progress(progress)
        return ThreeWaySearchOutcome(progress, path)

    def _power_qualifies(self, result: ThreeWayPowerEvaluation) -> bool:
        threshold = self.settings.minimum_displayed_power_delta
        return all(row.display_delta >= threshold for row in result.changes)

    def _simulate_candidate(
        self, power: ThreeWayPowerEvaluation
    ) -> ThreeWayQualifiedResult:
        adjusted = {row.roster.team_id: row.roster for row in power.adjustments}
        after_rosters = tuple(
            adjusted.get(row.team_id, row) for row in self.baseline.scenarios.rosters
        )
        projection = self.baseline.project(after_rosters)
        results = tuple(
            _team_result(power, projection, change, adjustment)
            for change, adjustment in zip(power.changes, power.adjustments)
        )
        return ThreeWayQualifiedResult(
            power.candidate_index, power.candidate.transfers, results
        )

    def _progress(self, next_index, qualified, gains, cancelled):
        return ThreeWaySearchProgress(
            self.run_definition.run_id,
            next_index,
            self.space.candidate_count,
            qualified,
            qualified,
            gains,
            cancelled,
        )


def _team_result(power, projection, change, adjustment):
    season = projection.for_team(change.team_id)
    return ThreeWayTeamResult(
        change.team_id,
        power.candidate.outgoing_for(change.team_id),
        power.candidate.incoming_for(change.team_id),
        adjustment.added_player_ids,
        adjustment.dropped_player_ids,
        change.raw_after - change.raw_before,
        change.display_delta,
        season.before.playoff_probability * 100,
        season.after.playoff_probability * 100,
    )


def _run_definition(space, prepared, baseline, settings):
    return ThreeWaySearchRunDefinition(
        snapshot_id=baseline.state.snapshot_id,
        strength_model_id=prepared.model.model_id,
        participant_team_ids=space.participant_team_ids,
        trade_constraint_record={
            "algorithm": THREE_WAY_SEARCH_ALGORITHM,
            "candidate_order": {
                "compiled_enumeration": space.enumeration_record(),
                "rosters": [_roster_record(row) for row in space.rosters],
            },
            "roster_adjustment_id": (
                None if prepared.adjuster is None else prepared.adjuster.adjustment_id
            ),
            "scenario_run_id": baseline.scenarios.run_id,
            "season_projection_options": {
                "score_decimal_places": baseline.score_decimal_places,
                "tiebreak_random_seed": baseline.tiebreak_random_seed,
            },
            "settings": settings.to_record(),
            "trade_constraints": space.constraints.to_record(),
        },
        total_candidate_count=space.candidate_count,
    )


def _pure_adjustments(changes, model):
    adjustments = []
    for before, outgoing, incoming in changes:
        outgoing_set = set(outgoing)
        players = tuple(
            player_id
            for player_id in before.player_ids
            if player_id not in outgoing_set
        ) + tuple(incoming)
        if not set(players).issubset(model.players):
            raise ValueError("trade contains a player absent from the strength model")
        exempt = before.capacity_exempt_player_ids.difference(outgoing_set)
        adjustments.append(
            TeamRosterAdjustment(
                TeamRoster(
                    before.team_id, players, len(players), before.roster_cap, exempt
                )
            )
        )
    return tuple(adjustments)


def _validate_three_rosters(rows, model):
    if len(rows) != 3 or any(not isinstance(row, TeamRoster) for row in rows):
        raise ValueError("rosters must contain exactly three TeamRoster values")
    if len({row.team_id for row in rows}) != 3:
        raise ValueError("rosters contain a duplicate team")
    owned = set()
    for row in rows:
        if row.current_size != len(row.player_ids):
            raise ValueError("prepared rosters must contain full current player lists")
        if not set(row.player_ids).issubset(model.players):
            raise ValueError("prepared roster contains a player absent from the model")
        if owned.intersection(row.player_ids):
            raise ValueError("prepared rosters cannot share a player")
        owned.update(row.player_ids)


def _validate_transfer_ownership(candidate, rosters):
    owners = {
        player_id: roster.team_id
        for roster in rosters
        for player_id in roster.player_ids
    }
    for transfer in candidate.transfers:
        if any(
            owners.get(player_id) != transfer.source_team_id
            for player_id in transfer.player_ids
        ):
            raise ValueError("a transfer contains a player not owned by its source team")


def _same_roster(left, right):
    return (
        left.team_id == right.team_id
        and frozenset(left.player_ids) == frozenset(right.player_ids)
        and left.current_size == right.current_size
        and left.roster_cap == right.roster_cap
        and left.capacity_exempt_player_ids == right.capacity_exempt_player_ids
    )


def _roster_record(roster):
    return {
        "active_size": roster.active_size,
        "capacity_exempt_player_ids": sorted(roster.capacity_exempt_player_ids),
        "current_size": roster.current_size,
        "player_ids": list(roster.player_ids),
        "roster_cap": roster.roster_cap,
        "team_id": roster.team_id,
    }


def _model_identity(model):
    return model.snapshot_id, model.season, model.scoring_profile_id


def _state_identity(baseline):
    state = baseline.state
    return state.snapshot_id, state.season, state.scoring_profile_id


__all__ = (
    "PreparedThreeWayTrade",
    "ResumableThreeWayTradeSearch",
    "THREE_WAY_DATABASE_SCHEMA_VERSION",
    "THREE_WAY_SEARCH_ALGORITHM",
    "ThreeWayPowerEvaluation",
    "ThreeWayQualifiedResult",
    "ThreeWayResumeState",
    "ThreeWaySearchOutcome",
    "ThreeWaySearchProgress",
    "ThreeWaySearchRunDefinition",
    "ThreeWaySearchRunMismatchError",
    "ThreeWaySearchStore",
    "ThreeWaySearchStoreError",
    "ThreeWayTeamResult",
    "TradeTransfer",
)
