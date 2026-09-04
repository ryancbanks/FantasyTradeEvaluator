"""Bounded reuse of baseline team-week scores across roster candidates."""

from __future__ import annotations

from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .score_scenarios import PreparedScoreScenarios
from .season import ScoreScenario, TeamWeekScore

if TYPE_CHECKING:
    from .league_state import LeagueState


DEFAULT_MAX_CACHE_BYTES = 128 * 1024 * 1024
_DOUBLE_SIZE = array("d").itemsize

__all__ = (
    "DEFAULT_MAX_CACHE_BYTES",
    "ScenarioScoreCache",
    "ScenarioScoreCacheBuilder",
)


@dataclass(frozen=True, slots=True)
class _IdentifiedScoreScenarios:
    """A cached score stream carrying the candidate roster-run identity."""

    run_id: str
    rows: Iterable[ScoreScenario]

    def __iter__(self) -> Iterator[ScoreScenario]:
        return iter(self.rows)


class ScenarioScoreCacheBuilder:
    """Capture one baseline scenario stream without retaining object graphs."""

    __slots__ = (
        "_baseline",
        "_baseline_lineups",
        "_complete",
        "_estimated_byte_count",
        "_layout",
        "_max_bytes",
        "_scores",
        "_started",
    )

    def __init__(
        self,
        baseline: PreparedScoreScenarios,
        layout: tuple[tuple[str, int], ...],
        baseline_lineups: tuple[tuple[str, ...], ...],
        max_bytes: int,
        estimated_byte_count: int,
    ) -> None:
        self._baseline = baseline
        self._layout = layout
        self._baseline_lineups = baseline_lineups
        self._max_bytes = max_bytes
        self._estimated_byte_count = estimated_byte_count
        self._scores: array | None = array("d")
        self._started = False
        self._complete = False

    @classmethod
    def for_prepared(
        cls,
        prepared: PreparedScoreScenarios,
        *,
        max_bytes: int = DEFAULT_MAX_CACHE_BYTES,
    ) -> ScenarioScoreCacheBuilder | None:
        """Return a builder unless its packed double payload exceeds the ceiling."""

        _require_prepared(prepared)
        _require_max_bytes(max_bytes)
        layout = _score_layout(prepared)
        estimated = prepared.config.scenario_count * len(layout) * _DOUBLE_SIZE
        if estimated > max_bytes:
            return None
        lineups = tuple(
            _lineup_player_ids(prepared, team_id, week)
            for team_id, week in layout
        )
        return cls(prepared, layout, lineups, max_bytes, estimated)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def run_id(self) -> str:
        """Preserve the wrapped scenario stream's public identity."""

        return self._baseline.run_id

    @property
    def estimated_byte_count(self) -> int:
        return self._estimated_byte_count

    def __iter__(self) -> Iterator[ScoreScenario]:
        if self._started:
            raise RuntimeError("scenario score cache builder can only capture once")
        if self._scores is None:
            raise RuntimeError("scenario score cache builder is already finished")
        self._started = True
        return self._capture(iter(self._baseline))

    def _capture(self, scenarios: Iterator[ScoreScenario]) -> Iterator[ScoreScenario]:
        scores = self._scores
        if scores is None:  # Defensive: ownership transfers to the finished cache.
            raise RuntimeError("scenario score cache builder is already finished")
        expected_count = self._baseline.config.scenario_count
        captured = 0
        for scenario in scenarios:
            if captured >= expected_count:
                raise ValueError("baseline scenario stream contains too many scenarios")
            values = _validated_scores(
                scenario,
                self._baseline,
                self._layout,
                captured,
            )
            scores.extend(values)
            captured += 1
            yield scenario
        if captured != expected_count:
            raise ValueError("baseline scenario stream ended before scenario_count")
        self._complete = True

    def finish(self) -> ScenarioScoreCache:
        """Transfer the completed packed buffer into a reusable cache."""

        scores = self._scores
        if scores is None:
            raise RuntimeError("scenario score cache builder is already finished")
        if not self._complete:
            raise RuntimeError("scenario score cache capture did not complete")
        if len(scores) * _DOUBLE_SIZE != self._estimated_byte_count:
            raise RuntimeError("scenario score cache capture has an invalid size")
        self._scores = None
        return ScenarioScoreCache(
            baseline_state=self._baseline.state,
            config_id=self._baseline.config.config_id,
            draw_space_id=self._baseline.draw_space_id,
            layout=self._layout,
            baseline_lineups=self._baseline_lineups,
            scenario_count=self._baseline.config.scenario_count,
            scores=scores,
            max_bytes=self._max_bytes,
        )


class ScenarioScoreCache:
    """Packed baseline scores plus selective candidate-score recomputation."""

    __slots__ = (
        "_baseline_lineups",
        "_baseline_state",
        "_config_id",
        "_draw_space_id",
        "_layout",
        "_max_bytes",
        "_scenario_count",
        "_scores",
    )

    def __init__(
        self,
        *,
        baseline_state: LeagueState,
        config_id: str,
        draw_space_id: str,
        layout: tuple[tuple[str, int], ...],
        baseline_lineups: tuple[tuple[str, ...], ...],
        scenario_count: int,
        scores: array,
        max_bytes: int,
    ) -> None:
        expected_values = scenario_count * len(layout)
        if scores.typecode != "d" or len(scores) != expected_values:
            raise ValueError("scores must be a complete packed double buffer")
        self._baseline_state = baseline_state
        self._config_id = config_id
        self._draw_space_id = draw_space_id
        self._layout = layout
        self._baseline_lineups = baseline_lineups
        self._scenario_count = scenario_count
        self._scores = scores
        self._max_bytes = max_bytes

    @property
    def scenario_count(self) -> int:
        return self._scenario_count

    @property
    def cached_byte_count(self) -> int:
        return len(self._scores) * _DOUBLE_SIZE

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def recomputed_cell_count(self, prepared: PreparedScoreScenarios) -> int:
        """Return exact candidate team-week score evaluations for a full run."""

        changed = self._reuse_mask(prepared).count(False)
        return changed * self._scenario_count

    def prepare_projection(
        self,
        prepared: PreparedScoreScenarios,
    ) -> tuple[int, Iterable[ScoreScenario]]:
        """Validate one candidate once and return its work count and score stream."""

        reused = self._reuse_mask(prepared)
        rows = self._iter_scenarios(prepared, reused, 0, self._scenario_count)
        return (
            reused.count(False) * self._scenario_count,
            _IdentifiedScoreScenarios(prepared.run_id, rows),
        )

    def iter_scenarios(
        self,
        prepared: PreparedScoreScenarios,
        start: int = 0,
        stop: int | None = None,
    ) -> Iterator[ScoreScenario]:
        """Yield candidate scores, reusing every unchanged baseline team-week cell."""

        end = self._scenario_count if stop is None else stop
        _validate_slice(start, end, self._scenario_count)
        reused = self._reuse_mask(prepared)
        return self._iter_scenarios(prepared, reused, start, end)

    def _reuse_mask(
        self, prepared: PreparedScoreScenarios
    ) -> tuple[bool, ...]:
        candidate_lineups = self._validated_candidate_lineups(prepared)
        return tuple(
            candidate == baseline
            for candidate, baseline in zip(
                candidate_lineups, self._baseline_lineups, strict=True
            )
        )

    def _iter_scenarios(
        self,
        prepared: PreparedScoreScenarios,
        reused: tuple[bool, ...],
        start: int,
        end: int,
    ) -> Iterator[ScoreScenario]:
        cell_count = len(self._layout)
        for scenario_index in range(start, end):
            offset = scenario_index * cell_count
            draw_cache: dict[tuple[str, tuple[object, ...]], float] = {}
            rows = []
            for cell_index, ((team_id, week), use_baseline) in enumerate(
                zip(self._layout, reused, strict=True)
            ):
                score = (
                    self._scores[offset + cell_index]
                    if use_baseline
                    else prepared._team_week_score(
                        team_id, week, scenario_index, draw_cache
                    )
                )
                rows.append(TeamWeekScore(team_id, week, score))
            yield ScoreScenario(
                scenario_id=f"{prepared.draw_space_id}:{scenario_index}",
                snapshot_id=prepared.state.snapshot_id,
                scoring_profile_id=prepared.state.scoring_profile_id,
                scores=tuple(rows),
            )

    def _validated_candidate_lineups(
        self, prepared: PreparedScoreScenarios
    ) -> tuple[tuple[str, ...], ...]:
        _require_prepared(prepared)
        if prepared.state != self._baseline_state:
            raise ValueError("scenario score cache does not match league state")
        if prepared.config.config_id != self._config_id:
            raise ValueError("scenario score cache does not match scenario config")
        if prepared.draw_space_id != self._draw_space_id:
            raise ValueError("scenario score cache does not match player draw space")
        if _score_layout(prepared) != self._layout:
            raise ValueError("scenario score cache does not match team-week layout")
        return tuple(
            _lineup_player_ids(prepared, team_id, week)
            for team_id, week in self._layout
        )


def _require_prepared(prepared: PreparedScoreScenarios) -> None:
    if not isinstance(prepared, PreparedScoreScenarios):
        raise ValueError("prepared must be PreparedScoreScenarios")


def _require_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")


def _score_layout(prepared: PreparedScoreScenarios) -> tuple[tuple[str, int], ...]:
    team_ids = tuple(sorted(team.team_id for team in prepared.state.teams))
    return tuple(
        (team_id, week)
        for week in prepared.state.remaining_regular_season_weeks
        for team_id in team_ids
    )


def _lineup_player_ids(
    prepared: PreparedScoreScenarios, team_id: str, week: int
) -> tuple[str, ...]:
    try:
        values = prepared._lineup_player_ids(team_id, week)
    except AttributeError:
        raise ValueError(
            "prepared scenarios do not support lineup-aware score reuse"
        ) from None
    return tuple(values)


def _validated_scores(
    scenario: ScoreScenario,
    baseline: PreparedScoreScenarios,
    layout: tuple[tuple[str, int], ...],
    scenario_index: int,
) -> tuple[float, ...]:
    if not isinstance(scenario, ScoreScenario):
        raise ValueError("baseline stream must contain ScoreScenario values")
    if (
        scenario.scenario_id != f"{baseline.draw_space_id}:{scenario_index}"
        or scenario.snapshot_id != baseline.state.snapshot_id
        or scenario.scoring_profile_id != baseline.state.scoring_profile_id
    ):
        raise ValueError("baseline scenario identity does not match prepared scenarios")
    if tuple((row.team_id, row.week) for row in scenario.scores) != layout:
        raise ValueError("baseline scenario scores do not match team-week layout")
    return tuple(float(row.score) for row in scenario.scores)


def _validate_slice(start: int, stop: int, scenario_count: int) -> None:
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError("start must be a nonnegative integer")
    if isinstance(stop, bool) or not isinstance(stop, int) or stop < start:
        raise ValueError("stop must be an integer greater than or equal to start")
    if stop > scenario_count:
        raise ValueError("stop cannot exceed scenario_count")
