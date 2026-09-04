"""Paired regression-baseline evaluation for evolved draft brains."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
import json
import math

from .draft_brain import DraftBrain
from .draft_config import DraftLeagueConfig
from .draft_history import HistoricalCorpus
from .draft_season import _prepare_scoring_context, simulate_historical_season
from .draft_simulation import _new_simulation_cache, simulate_snake_draft


@dataclass(frozen=True, slots=True)
class DraftBenchmarkResult:
    trial_count: int
    wins: int
    ties: int
    losses: int
    mean_points_delta: float
    mean_points_percentile_delta: float
    mean_finish_improvement: float
    playoff_rate_delta: float
    championship_rate_delta: float
    percentile_delta_interval_95: tuple[float, float]
    verdict: str
    evaluation_seasons: tuple[int, ...]
    interval_basis: str = "season_clustered"

    def to_record(self) -> dict[str, object]:
        return {
            "trial_count": self.trial_count,
            "wins": self.wins,
            "ties": self.ties,
            "losses": self.losses,
            "mean_points_delta": self.mean_points_delta,
            "mean_points_percentile_delta": self.mean_points_percentile_delta,
            "mean_finish_improvement": self.mean_finish_improvement,
            "playoff_rate_delta": self.playoff_rate_delta,
            "championship_rate_delta": self.championship_rate_delta,
            "percentile_delta_interval_95": list(self.percentile_delta_interval_95),
            "verdict": self.verdict,
            "evaluation_seasons": list(self.evaluation_seasons),
            "interval_basis": self.interval_basis,
        }


def compare_to_regression_baseline(
    brain: DraftBrain,
    corpus: HistoricalCorpus,
    config: DraftLeagueConfig,
    *,
    trials: int = 100,
    evaluation_years: Iterable[int] | None = None,
    seed: int = 904_221,
    candidate_window: int = 32,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] = lambda: False,
) -> DraftBenchmarkResult:
    """Compare one focal drafter under paired seats, opponents, seasons, and seeds."""

    if not isinstance(brain, DraftBrain) or not isinstance(corpus, HistoricalCorpus) or not isinstance(config, DraftLeagueConfig):
        raise ValueError("benchmark inputs use invalid domain types")
    if brain.league_config_fingerprint != config.config_id:
        raise ValueError("draft brain is not compatible with this league configuration")
    if type(trials) is not int or not 1 <= trials <= 1_000:
        raise ValueError("trials must be an integer from 1 through 1000")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if type(candidate_window) is not int or not 0 <= candidate_window <= 4096:
        raise ValueError("candidate_window is invalid")
    seasons = _select_seasons(corpus, evaluation_years)
    required = set((*config.regular_season_weeks, *config.playoff_weeks))
    if any(not required.issubset(season.available_weeks) for season in seasons):
        raise ValueError("an evaluation season lacks the configured week coverage")
    baseline = DraftBrain.zero_residual(brain.schema, brain.baseline, config.config_id)
    deltas: list[float] = []
    point_deltas: list[float] = []
    finish_deltas: list[float] = []
    playoff_deltas: list[int] = []
    championship_deltas: list[int] = []
    deltas_by_season: dict[int, list[float]] = {
        season.season: [] for season in seasons
    }
    scoring_contexts = {}
    simulation_caches = {}
    wins = ties = losses = 0

    for trial in range(trials):
        if should_cancel():
            raise InterruptedError("draft model benchmark was cancelled")
        season_index, seat = _trial_cell(trial, len(seasons), config.team_count)
        season = seasons[season_index]
        scoring_context = scoring_contexts.get(season_index)
        if scoring_context is None:
            scoring_context = _prepare_scoring_context(season, config)
            scoring_contexts[season_index] = scoring_context
        simulation_cache = simulation_caches.get(season_index)
        if simulation_cache is None:
            simulation_cache = _new_simulation_cache(season, config)
            simulation_caches[season_index] = simulation_cache
        trial_seed = seed + trial * 7_919
        opponents = _paired_opponents(
            brain, baseline, config.team_count, seat, seed, trial, season.season
        )
        reference_brains = list(opponents)
        reference_brains[seat] = baseline
        reference_draft = simulate_snake_draft(
            season, config, tuple(reference_brains),
            seed=trial_seed, candidate_window=candidate_window, should_cancel=should_cancel,
            _simulation_cache=simulation_cache,
        )
        candidate_brains = list(opponents)
        candidate_brains[seat] = brain
        candidate_draft = simulate_snake_draft(
            season, config, tuple(candidate_brains),
            seed=trial_seed, candidate_window=candidate_window, should_cancel=should_cancel,
            _simulation_cache=simulation_cache,
        )
        reference = simulate_historical_season(
            reference_draft.rosters, season, config, _prepared=scoring_context
        )
        candidate = simulate_historical_season(
            candidate_draft.rosters, season, config, _prepared=scoring_context
        )
        team_id = f"drafter-{seat + 1}"
        old = next(row for row in reference.standings if row.team_id == team_id)
        new = next(row for row in candidate.standings if row.team_id == team_id)
        old_pct = _points_percentile(reference, team_id)
        new_pct = _points_percentile(candidate, team_id)
        delta = new_pct - old_pct
        deltas.append(delta)
        deltas_by_season[season.season].append(delta)
        point_deltas.append(new.points_for - old.points_for)
        finish_deltas.append(old.finish_rank - new.finish_rank)
        playoff_deltas.append(int(new.made_playoffs) - int(old.made_playoffs))
        championship_deltas.append(
            int(candidate.champion_team_id == team_id) - int(reference.champion_team_id == team_id)
        )
        if delta > 1e-12:
            wins += 1
        elif delta < -1e-12:
            losses += 1
        else:
            ties += 1
        if on_progress is not None:
            on_progress(trial + 1, trials)

    season_means = tuple(
        math.fsum(values) / len(values)
        for _, values in sorted(deltas_by_season.items())
        if values
    )
    clustered_mean = math.fsum(season_means) / len(season_means)
    interval = _mean_interval(season_means, clustered_mean)
    verdict = (
        "inconclusive"
        if len(season_means) < 2
        else "improved"
        if interval[0] > 0
        else "worse"
        if interval[1] < 0
        else "inconclusive"
    )
    return DraftBenchmarkResult(
        trials, wins, ties, losses,
        math.fsum(point_deltas) / trials,
        clustered_mean,
        math.fsum(finish_deltas) / trials,
        math.fsum(playoff_deltas) / trials,
        math.fsum(championship_deltas) / trials,
        interval,
        verdict,
        tuple(season.season for season in seasons),
    )


def _trial_cell(trial, season_count, team_count):
    """Balance both margins and cover every season×seat cell before repeats."""

    season_index = trial % season_count
    cycle = trial // season_count
    seat = (cycle + season_index) % team_count
    return season_index, seat


def _paired_opponents(brain, baseline, team_count, focal_seat, seed, trial, season):
    result = []
    for seat in range(team_count):
        if seat == focal_seat:
            result.append(baseline)
            continue
        payload = json.dumps(
            ["draft-benchmark-opponent-v1", seed, trial, season, focal_seat, seat],
            separators=(",", ":"),
        ).encode("utf-8")
        result.append(brain if sha256(payload).digest()[0] & 1 else baseline)
    return tuple(result)


def _points_percentile(trace, team_id):
    order = sorted(trace.standings, key=lambda row: (-row.points_for, row.team_id))
    rank = next(index for index, row in enumerate(order, 1) if row.team_id == team_id)
    return (len(order) - rank) / max(1, len(order) - 1)


def _mean_interval(values, mean):
    if len(values) < 2:
        return (mean, mean)
    variance = math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    critical = _T95[min(len(values) - 1, len(_T95) - 1)]
    margin = critical * math.sqrt(variance / len(values))
    return (mean - margin, mean + margin)


_T95 = (
    0.0, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365,
    2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
    2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069,
    2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)


def _select_seasons(corpus, years):
    by_year = {row.season: row for row in corpus.seasons}
    if years is None:
        return tuple(by_year[year] for year in sorted(by_year))
    try:
        requested = tuple(years)
    except TypeError:
        raise ValueError("evaluation_years must contain years") from None
    if not requested or any(type(year) is not int for year in requested):
        raise ValueError("evaluation_years must contain integer years")
    absent = set(requested).difference(by_year)
    if absent:
        raise ValueError(f"evaluation year {min(absent)} is unavailable")
    return tuple(by_year[year] for year in sorted(set(requested)))


__all__ = ("DraftBenchmarkResult", "compare_to_regression_baseline")
