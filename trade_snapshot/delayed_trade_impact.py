"""Paired season projections for a roster change that starts in a future week."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ._scenario_random import content_id
from .score_scenarios import prepare_score_scenarios
from .season import (
    ScoreScenario,
    SeasonProjection,
    TeamWeekScore,
    project_remaining_season,
)
from .trade_impact import (
    PairedSeasonProjection,
    PreparedSeasonBaseline,
    TeamSeasonChange,
)
from .trade_space import TeamRoster


@dataclass(frozen=True, slots=True, init=False)
class PreparedDelayedBaseline:
    """One trusted materialization reused across many delayed roster changes."""

    baseline: PreparedSeasonBaseline
    before_scenarios: tuple[ScoreScenario, ...]
    _conditioned_before_cache: dict[
        tuple[int, ...], tuple[SeasonProjection, str]
    ] = field(init=False, compare=False, repr=False)

    def __init__(self, baseline: PreparedSeasonBaseline) -> None:
        if not isinstance(baseline, PreparedSeasonBaseline):
            raise ValueError("baseline must be a PreparedSeasonBaseline")
        scenarios = tuple(baseline.scenarios)
        _validate_before_scenarios(baseline, scenarios)
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "before_scenarios", scenarios)
        object.__setattr__(self, "_conditioned_before_cache", {})

    def roster_change(
        self,
        after_rosters: Iterable[TeamRoster],
        affected_team_ids: Iterable[str],
    ) -> "PreparedDelayedRosterChange":
        return PreparedDelayedRosterChange(
            self, after_rosters, affected_team_ids
        )

    def _conditioned_before(
        self, scenario_indexes: tuple[int, ...]
    ) -> tuple[SeasonProjection, str]:
        cached = self._conditioned_before_cache.get(scenario_indexes)
        if cached is not None:
            return cached
        projection = project_remaining_season(
            self.baseline.state,
            (self.before_scenarios[index] for index in scenario_indexes),
            score_decimal_places=self.baseline.score_decimal_places,
            random_seed=self.baseline.tiebreak_random_seed,
        )
        run_id = content_id(
            "conditioned-scenario-run",
            {
                "baseline_run_id": self.baseline.scenarios.run_id,
                "scenario_indexes": scenario_indexes,
            },
        )
        result = projection, run_id
        self._conditioned_before_cache[scenario_indexes] = result
        return result


@dataclass(frozen=True, slots=True, init=False)
class PreparedDelayedRosterChange:
    """A bounded roster change whose correlated scores can be spliced by week."""

    baseline: PreparedSeasonBaseline
    after_rosters: tuple[TeamRoster, ...]
    affected_team_ids: tuple[str, ...]
    before_scenarios: tuple[ScoreScenario, ...]
    after_team_scores: tuple[tuple[TeamWeekScore, ...], ...]
    after_run_id: str
    _prepared_baseline: PreparedDelayedBaseline = field(
        init=False, compare=False, repr=False
    )

    def __init__(
        self,
        prepared_baseline: PreparedDelayedBaseline,
        after_rosters: Iterable[TeamRoster],
        affected_team_ids: Iterable[str],
    ) -> None:
        if not isinstance(prepared_baseline, PreparedDelayedBaseline):
            raise ValueError(
                "prepared_baseline must be a PreparedDelayedBaseline"
            )
        baseline = prepared_baseline.baseline
        after_rows = tuple(after_rosters)
        affected = _affected_ids(baseline, affected_team_ids)
        _validate_roster_changes(baseline, after_rows, affected)
        after = prepare_score_scenarios(
            baseline.state,
            after_rows,
            baseline.scenarios.projections,
            baseline.scenarios.eligibilities,
            baseline.scenarios.config,
        )
        if after.draw_space_id != baseline.scenarios.draw_space_id:
            raise ValueError(
                "delayed roster change could not establish common random draws"
            )
        after_scores = tuple(
            after.team_scores(affected, index)
            for index in range(after.config.scenario_count)
        )
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "after_rosters", after.rosters)
        object.__setattr__(self, "affected_team_ids", affected)
        object.__setattr__(
            self, "before_scenarios", prepared_baseline.before_scenarios
        )
        object.__setattr__(self, "after_team_scores", after_scores)
        object.__setattr__(self, "after_run_id", after.run_id)
        object.__setattr__(self, "_prepared_baseline", prepared_baseline)

    def project(self, effective_week: int) -> PairedSeasonProjection:
        if (
            type(effective_week) is not int
            or effective_week
            not in self.baseline.state.remaining_regular_season_weeks
        ):
            raise ValueError("effective_week must be a remaining regular-season week")
        return self._project_many((effective_week,), None)[effective_week]

    def project_conditioned(
        self,
        effective_week: int,
        scenario_indexes: Iterable[int],
    ) -> PairedSeasonProjection:
        """Project within one pre-trade scenario subset selected without leakage."""

        return self.project_conditioned_many(
            (effective_week,), scenario_indexes
        )[effective_week]

    def project_many(
        self, effective_weeks: Iterable[int]
    ) -> Mapping[int, PairedSeasonProjection]:
        weeks = self._effective_weeks(effective_weeks)
        return self._project_many(weeks, None)

    def project_conditioned_many(
        self,
        effective_weeks: Iterable[int],
        scenario_indexes: Iterable[int],
    ) -> Mapping[int, PairedSeasonProjection]:
        """Project several effective weeks within the same pre-trade trigger paths."""

        weeks = self._effective_weeks(effective_weeks)
        indexes = self._scenario_indexes(scenario_indexes)
        return self._project_many(weeks, indexes)

    def _project_many(self, effective_weeks, scenario_indexes):
        if scenario_indexes is None:
            before_projection = self.baseline.season_projection
            before_run_id = self.baseline.scenarios.run_id
        else:
            before_projection, before_run_id = (
                self._prepared_baseline._conditioned_before(scenario_indexes)
            )
        return {
            week: self._paired_projection(
                week,
                scenario_indexes,
                before_projection,
                before_run_id,
            )
            for week in effective_weeks
        }

    def _paired_projection(
        self,
        effective_week,
        scenario_indexes,
        before_projection,
        before_run_id,
    ):
        after_projection = project_remaining_season(
            self.baseline.state,
            self._spliced_scenarios(effective_week, scenario_indexes),
            score_decimal_places=self.baseline.score_decimal_places,
            random_seed=self.baseline.tiebreak_random_seed,
        )
        before_by_team = {
            row.team_id: row for row in before_projection.teams
        }
        after_by_team = {row.team_id: row for row in after_projection.teams}
        changes = tuple(
            TeamSeasonChange(
                team.team_id,
                before_by_team[team.team_id],
                after_by_team[team.team_id],
            )
            for team in self.baseline.state.teams
        )
        delayed_run_id = content_id(
            "delayed-scenario-run",
            {
                "after_run_id": self.after_run_id,
                "before_run_id": before_run_id,
                "effective_week": effective_week,
            },
        )
        return PairedSeasonProjection(
            before=before_projection,
            after=after_projection,
            changes=changes,
            before_scenario_run_id=before_run_id,
            after_scenario_run_id=delayed_run_id,
            draw_space_id=self.baseline.scenarios.draw_space_id,
        )

    def _effective_weeks(self, effective_weeks):
        if isinstance(effective_weeks, (str, bytes)):
            raise ValueError("effective_weeks must be an iterable of weeks")
        try:
            weeks = tuple(effective_weeks)
        except TypeError:
            raise ValueError("effective_weeks must be an iterable of weeks") from None
        if not weeks or len(set(weeks)) != len(weeks):
            raise ValueError("effective_weeks must contain unique weeks")
        remaining = self.baseline.state.remaining_regular_season_weeks
        if any(type(week) is not int or week not in remaining for week in weeks):
            raise ValueError(
                "effective_weeks must contain remaining regular-season weeks"
            )
        return tuple(sorted(weeks))

    def _scenario_indexes(self, scenario_indexes):
        if isinstance(scenario_indexes, (str, bytes)):
            raise ValueError("scenario_indexes must be an iterable of indexes")
        try:
            indexes = tuple(scenario_indexes)
        except TypeError:
            raise ValueError(
                "scenario_indexes must be an iterable of indexes"
            ) from None
        if (
            not indexes
            or len(set(indexes)) != len(indexes)
            or any(type(index) is not int for index in indexes)
        ):
            raise ValueError("scenario_indexes must contain unique integer indexes")
        if min(indexes) < 0 or max(indexes) >= len(self.before_scenarios):
            raise ValueError("scenario_indexes contains an index outside the baseline run")
        return tuple(sorted(indexes))

    def _spliced_scenarios(self, effective_week, scenario_indexes=None):
        affected = frozenset(self.affected_team_ids)
        selected = (
            range(len(self.before_scenarios))
            if scenario_indexes is None
            else scenario_indexes
        )
        for index in selected:
            before = self.before_scenarios[index]
            after_rows = self.after_team_scores[index]
            replacements = {
                (row.team_id, row.week): row
                for row in after_rows
                if row.week >= effective_week
            }
            scores = tuple(
                replacements.get((row.team_id, row.week), row)
                if row.team_id in affected and row.week >= effective_week
                else row
                for row in before.scores
            )
            yield ScoreScenario(
                before.scenario_id,
                before.snapshot_id,
                before.scoring_profile_id,
                scores,
            )


def prepare_delayed_roster_change(
    baseline: PreparedSeasonBaseline,
    after_rosters: Iterable[TeamRoster],
    affected_team_ids: Iterable[str],
) -> PreparedDelayedRosterChange:
    """Prepare one delayed roster change from a trusted baseline materialization."""

    return prepare_delayed_baseline(baseline).roster_change(
        after_rosters, affected_team_ids
    )


def prepare_delayed_baseline(
    baseline: PreparedSeasonBaseline,
) -> PreparedDelayedBaseline:
    """Materialize a baseline once so callers never inject pre-trade scenarios."""

    return PreparedDelayedBaseline(baseline)


def _affected_ids(baseline, values):
    if isinstance(values, (str, bytes)):
        raise ValueError("affected_team_ids must be an iterable of team IDs")
    try:
        result = tuple(sorted(values))
    except TypeError:
        raise ValueError("affected_team_ids must be an iterable of team IDs") from None
    if len(result) < 1 or any(not isinstance(value, str) or not value for value in result):
        raise ValueError("affected_team_ids must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError("affected_team_ids contains a duplicate")
    known = {row.team_id for row in baseline.state.teams}
    unknown = set(result).difference(known)
    if unknown:
        raise ValueError(f"affected team {min(unknown)!r} is outside the league")
    return result


def _validate_roster_changes(baseline, after_rows, affected):
    before = {row.team_id: row for row in baseline.scenarios.rosters}
    after = {row.team_id: row for row in after_rows}
    if set(after) != set(before) or len(after_rows) != len(after):
        raise ValueError("after_rosters must contain exactly one row for every league team")
    changed = {
        team_id
        for team_id in before
        if before[team_id] != after[team_id]
    }
    if not changed or not changed.issubset(affected):
        raise ValueError("affected_team_ids must cover every and at least one changed roster")
    if set(affected).difference(changed):
        raise ValueError("affected_team_ids cannot include an unchanged roster")


def _validate_before_scenarios(baseline, scenarios):
    if len(scenarios) != baseline.scenarios.config.scenario_count:
        raise ValueError("before_scenarios must contain the complete baseline run")
    expected_ids = tuple(
        f"{baseline.scenarios.draw_space_id}:{index}"
        for index in range(len(scenarios))
    )
    if tuple(row.scenario_id for row in scenarios) != expected_ids:
        raise ValueError("before_scenarios do not match the baseline run order")
    if any(
        row.snapshot_id != baseline.state.snapshot_id
        or row.scoring_profile_id != baseline.state.scoring_profile_id
        for row in scenarios
    ):
        raise ValueError("before_scenarios do not match the baseline league")


__all__ = (
    "PreparedDelayedBaseline",
    "PreparedDelayedRosterChange",
    "prepare_delayed_baseline",
    "prepare_delayed_roster_change",
)
