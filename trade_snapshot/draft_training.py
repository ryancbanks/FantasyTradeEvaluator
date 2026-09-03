"""Deterministic local neuroevolution over historical draft arenas."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from types import MappingProxyType

from ._scenario_random import SAFE_INTEGER, canonical_json, content_id
from .draft_brain import DraftBrain, crossover_and_mutate, initialize_genome
from .draft_config import DraftLeagueConfig, DraftStrategy
from .draft_features import build_baseline_brain, fit_feature_schema
from .draft_history import HistoricalCorpus
from .draft_history import ActualWeekStatus
from .draft_season import (
    HistoricalSeasonTrace,
    _prepare_scoring_context,
    simulate_historical_season,
)
from .draft_simulation import DraftResult, simulate_snake_draft


@dataclass(frozen=True, slots=True)
class EvolutionConfig:
    population_size: int
    generations: int
    appearances_per_generation: int
    elite_fraction: float
    mutation_rate: float
    mutation_magnitude: int
    candidate_window: int
    training_years: tuple[int, ...]
    seed: int
    config_id: str = field(init=False)

    def __post_init__(self) -> None:
        _integer("population_size", self.population_size, 2, 5_000)
        _integer("generations", self.generations, 1, 1_000)
        _integer("appearances_per_generation", self.appearances_per_generation, 1, 20)
        _rate("elite_fraction", self.elite_fraction, minimum=0.01, maximum=0.5)
        _rate("mutation_rate", self.mutation_rate, minimum=0.0, maximum=1.0)
        _integer("mutation_magnitude", self.mutation_magnitude, 0, 10_000_000)
        _integer("candidate_window", self.candidate_window, 0, 4_096)
        _integer("seed", self.seed, 0, SAFE_INTEGER)
        years = tuple(self.training_years)
        if not years or tuple(sorted(set(years))) != years:
            raise ValueError("training_years must be unique and increasing")
        object.__setattr__(self, "training_years", years)
        object.__setattr__(self, "config_id", content_id("draft_evolution", self._content()))

    def _content(self):
        return {
            "population_size": self.population_size,
            "generations": self.generations,
            "appearances_per_generation": self.appearances_per_generation,
            "elite_fraction": self.elite_fraction,
            "mutation_rate": self.mutation_rate,
            "mutation_magnitude": self.mutation_magnitude,
            "candidate_window": self.candidate_window,
            "training_years": list(self.training_years),
            "seed": self.seed,
        }

    def to_record(self):
        return {"kind": "draft_evolution_config", "schema_version": 1,
                **self._content(), "config_id": self.config_id}

    @classmethod
    def from_record(cls, record):
        content = {
            "population_size", "generations", "appearances_per_generation",
            "elite_fraction", "mutation_rate", "mutation_magnitude",
            "candidate_window", "training_years", "seed",
        }
        _record(record, content | {"kind", "schema_version", "config_id"}, "evolution config")
        if record["kind"] != "draft_evolution_config" or record["schema_version"] != 1:
            raise ValueError("evolution config kind or version is invalid")
        if not isinstance(record["training_years"], list):
            raise ValueError("training_years must be a JSON array")
        result = cls(*(record[name] for name in (
            "population_size", "generations", "appearances_per_generation",
            "elite_fraction", "mutation_rate", "mutation_magnitude",
            "candidate_window",
        )), tuple(record["training_years"]), record["seed"])
        if record["config_id"] != result.config_id:
            raise ValueError("evolution config content does not match config_id")
        return result


@dataclass(frozen=True, slots=True)
class GenomePerformance:
    fitness: float
    appearances: int
    championships: int
    playoffs: int
    mean_finish: float
    mean_points_percentile: float

    def __post_init__(self):
        for name in ("fitness", "mean_finish", "mean_points_percentile"):
            _finite(name, getattr(self, name))
        _integer("appearances", self.appearances, 1, SAFE_INTEGER)
        _integer("championships", self.championships, 0, self.appearances)
        _integer("playoffs", self.playoffs, 0, self.appearances)

    def to_record(self):
        return {
            "fitness": self.fitness, "appearances": self.appearances,
            "championships": self.championships, "playoffs": self.playoffs,
            "mean_finish": self.mean_finish,
            "mean_points_percentile": self.mean_points_percentile,
        }


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    generation: int
    champion_brain_id: str
    champion_fitness: float
    mean_fitness: float
    championship_rate: float
    playoff_rate: float
    arena_count: int
    strategy_leaders: Mapping[str, str]

    def __post_init__(self):
        _integer("generation", self.generation, 1, 1_000)
        _integer("arena_count", self.arena_count, 1, SAFE_INTEGER)
        for name in ("champion_fitness", "mean_fitness", "championship_rate", "playoff_rate"):
            _finite(name, getattr(self, name))
        if not isinstance(self.champion_brain_id, str) or not self.champion_brain_id.startswith("draft_brain_"):
            raise ValueError("champion_brain_id is invalid")
        if not isinstance(self.strategy_leaders, Mapping) or any(
            key not in {row.value for row in DraftStrategy}
            or not isinstance(value, str) or not value.startswith("draft_brain_")
            for key, value in self.strategy_leaders.items()
        ):
            raise ValueError("strategy_leaders is invalid")
        object.__setattr__(
            self, "strategy_leaders", MappingProxyType(dict(sorted(self.strategy_leaders.items())))
        )

    def to_record(self):
        return {
            "generation": self.generation,
            "champion_brain_id": self.champion_brain_id,
            "champion_fitness": self.champion_fitness,
            "mean_fitness": self.mean_fitness,
            "championship_rate": self.championship_rate,
            "playoff_rate": self.playoff_rate,
            "arena_count": self.arena_count,
            "strategy_leaders": dict(self.strategy_leaders),
        }


@dataclass(frozen=True, slots=True)
class TrainingCheckpoint:
    corpus_id: str
    league_config: DraftLeagueConfig
    evolution_config: EvolutionConfig
    generation_completed: int
    population: tuple[DraftBrain, ...]
    champion: DraftBrain
    champion_performance: GenomePerformance
    history: tuple[GenerationSummary, ...]
    showcase: Mapping[str, object]
    checkpoint_id: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.corpus_id, str) or not self.corpus_id.startswith("draft_corpus_"):
            raise ValueError("checkpoint corpus_id is invalid")
        if not isinstance(self.league_config, DraftLeagueConfig) or not isinstance(
            self.evolution_config, EvolutionConfig
        ):
            raise ValueError("checkpoint configurations are invalid")
        if type(self.generation_completed) is not int or not 1 <= self.generation_completed <= self.evolution_config.generations:
            raise ValueError("checkpoint generation is invalid")
        population = tuple(self.population)
        if len(population) != self.evolution_config.population_size:
            raise ValueError("checkpoint population size is invalid")
        if not isinstance(self.champion, DraftBrain) or any(
            not isinstance(row, DraftBrain)
            or row.league_config_fingerprint != self.league_config.config_id
            for row in (*population, self.champion)
        ):
            raise ValueError("checkpoint contains an incompatible brain")
        reference = population[0]
        if any(row.schema != reference.schema or row.baseline != reference.baseline for row in (*population, self.champion)):
            raise ValueError("checkpoint brains do not share one feature schema and baseline")
        if not isinstance(self.champion_performance, GenomePerformance):
            raise ValueError("checkpoint champion performance is invalid")
        history = tuple(self.history)
        if len(history) != self.generation_completed:
            raise ValueError("checkpoint history does not match its generation")
        if not isinstance(self.showcase, Mapping):
            raise ValueError("checkpoint showcase must be an object")
        object.__setattr__(self, "population", population)
        object.__setattr__(self, "history", history)
        detached = json.loads(canonical_json({"showcase": self.showcase}))["showcase"]
        object.__setattr__(self, "showcase", _freeze_json(detached))
        object.__setattr__(self, "checkpoint_id", content_id("draft_checkpoint", self._content()))

    def _content(self):
        reference = self.population[0]
        return {
            "corpus_id": self.corpus_id,
            "league_config": self.league_config.to_record(),
            "evolution_config": self.evolution_config.to_record(),
            "generation_completed": self.generation_completed,
            "feature_schema": reference.schema.to_record(),
            "regression_baseline": reference.baseline.to_record(),
            "league_config_fingerprint": reference.league_config_fingerprint,
            "population_genomes": [_genome_record(row) for row in self.population],
            "champion_genome": _genome_record(self.champion),
            "champion_performance": self.champion_performance.to_record(),
            "history": [row.to_record() for row in self.history],
            "showcase": _thaw_json(self.showcase),
        }

    def to_record(self):
        return {"kind": "draft_training_checkpoint", "schema_version": 2,
                **self._content(), "checkpoint_id": self.checkpoint_id}

    @classmethod
    def from_record(cls, record):
        if not isinstance(record, Mapping):
            raise ValueError("checkpoint record fields are invalid")
        if record.get("kind") != "draft_training_checkpoint":
            raise ValueError("checkpoint kind or version is invalid")
        if record.get("schema_version") == 1:
            return cls._from_legacy_record(record)
        if record.get("schema_version") != 2:
            raise ValueError("checkpoint kind or version is invalid")
        content = {
            "corpus_id", "league_config", "evolution_config", "generation_completed",
            "feature_schema", "regression_baseline", "league_config_fingerprint",
            "population_genomes", "champion_genome", "champion_performance",
            "history", "showcase",
        }
        _record(record, content | {"kind", "schema_version", "checkpoint_id"}, "checkpoint")
        for name in (
            "league_config", "evolution_config", "feature_schema",
            "regression_baseline", "champion_genome", "champion_performance", "showcase",
        ):
            if not isinstance(record[name], Mapping):
                raise ValueError(f"checkpoint {name} must be an object")
        if not isinstance(record["population_genomes"], list) or not isinstance(record["history"], list):
            raise ValueError("checkpoint population and history must be arrays")
        from .draft_brain import FeatureSchema, RegressionBaseline

        schema = FeatureSchema.from_record(record["feature_schema"])
        baseline = RegressionBaseline.from_record(record["regression_baseline"])
        fingerprint = record["league_config_fingerprint"]
        result = cls(
            record["corpus_id"], DraftLeagueConfig.from_record(record["league_config"]),
            EvolutionConfig.from_record(record["evolution_config"]),
            record["generation_completed"],
            tuple(
                _brain_from_genome(schema, baseline, fingerprint, row)
                for row in record["population_genomes"]
            ),
            _brain_from_genome(schema, baseline, fingerprint, record["champion_genome"]),
            _performance_from_record(record["champion_performance"]),
            tuple(_summary_from_record(row) for row in record["history"]),
            record["showcase"],
        )
        if record["checkpoint_id"] != result.checkpoint_id:
            raise ValueError("checkpoint content does not match checkpoint_id")
        return result

    @classmethod
    def _from_legacy_record(cls, record):
        content = {
            "corpus_id", "league_config", "evolution_config", "generation_completed",
            "population", "champion", "champion_performance", "history", "showcase",
        }
        _record(record, content | {"kind", "schema_version", "checkpoint_id"}, "checkpoint")
        legacy_content = {key: record[key] for key in content}
        if record["checkpoint_id"] != content_id("draft_checkpoint", legacy_content):
            raise ValueError("checkpoint content does not match checkpoint_id")
        for name in (
            "league_config", "evolution_config", "champion",
            "champion_performance", "showcase",
        ):
            if not isinstance(record[name], Mapping):
                raise ValueError(f"checkpoint {name} must be an object")
        if not isinstance(record["population"], list) or not isinstance(record["history"], list):
            raise ValueError("checkpoint population and history must be arrays")
        return cls(
            record["corpus_id"], DraftLeagueConfig.from_record(record["league_config"]),
            EvolutionConfig.from_record(record["evolution_config"]),
            record["generation_completed"],
            tuple(DraftBrain.from_record(row) for row in record["population"]),
            DraftBrain.from_record(record["champion"]),
            _performance_from_record(record["champion_performance"]),
            tuple(_summary_from_record(row) for row in record["history"]),
            record["showcase"],
        )


def training_estimate(corpus, config, evolution):
    seasons = _validate_training_inputs(corpus, config, evolution)
    competitive_leagues_per_season = (
        math.ceil(evolution.population_size / config.team_count)
        * evolution.appearances_per_generation
    )
    # Every competitive arena has a same-seat, same-seed all-baseline control.
    leagues_per_season = competitive_leagues_per_season * 2
    leagues_per_generation = leagues_per_season * len(evolution.training_years)
    picks = config.team_count * config.roster_size
    candidates = evolution.candidate_window or max(len(row.players) for row in seasons)
    schema = fit_feature_schema(corpus, config, evolution.training_years)
    network_parameters = schema.vector_size * 16 + 16 + 16 * 8 + 8 + 8 + 1
    estimated_checkpoint_bytes = evolution.population_size * (
        network_parameters * 10 + 320
    ) + schema.vector_size * 80 + 64_000
    estimated_population_memory_bytes = evolution.population_size * (
        network_parameters * 36 + 1_024
    )
    total_leagues = leagues_per_generation * evolution.generations
    ranked_picks = total_leagues * picks
    feasibility_groups = max(
        len({(row.position, row.eligible_positions) for row in season.players})
        for season in seasons
    )
    return {
        "training_season_count": len(evolution.training_years),
        "leagues_per_season": leagues_per_season,
        "leagues_per_generation": leagues_per_generation,
        "total_leagues": total_leagues,
        "brain_appearances": (
            evolution.population_size
            * evolution.appearances_per_generation
            * len(evolution.training_years)
            * evolution.generations
        ),
        "ranked_picks_estimate": ranked_picks,
        "candidate_scores_estimate": ranked_picks * candidates,
        "feasibility_checks_estimate": ranked_picks * min(candidates, feasibility_groups),
        "network_parameters": network_parameters,
        "estimated_checkpoint_bytes": estimated_checkpoint_bytes,
        "estimated_population_memory_bytes": estimated_population_memory_bytes,
        "size_notice": (
            "Large local batches can still take hours. Start with one generation, "
            "inspect its measured runtime, then resume the autosave for more."
            if ranked_picks * candidates >= 10_000_000
            else "This is a local work-unit estimate; runtime depends on this machine."
        ),
    }


def run_training_batch(
    corpus: HistoricalCorpus,
    config: DraftLeagueConfig,
    evolution: EvolutionConfig,
    *,
    resume: TrainingCheckpoint | None = None,
    on_generation: Callable[[TrainingCheckpoint], None] | None = None,
    on_arena: Callable[[int, int, int], None] | None = None,
    should_cancel: Callable[[], bool] = lambda: False,
) -> TrainingCheckpoint:
    """Run whole generations; every completed generation is resumable exactly."""

    seasons = _validate_training_inputs(corpus, config, evolution)
    baseline = build_baseline_brain(corpus, config, evolution.training_years)
    if resume is None:
        population = (baseline,) + tuple(
            initialize_genome(
                baseline.schema, baseline.baseline, config.config_id,
                seed=evolution.seed, genome_index=index,
            )
            for index in range(1, evolution.population_size)
        )
        generation_start, history = 1, ()
    else:
        _validate_resume(resume, corpus, config, evolution, baseline)
        population = resume.population
        generation_start = resume.generation_completed + 1
        history = resume.history
        if generation_start > evolution.generations:
            return resume

    latest = resume
    scoring_contexts = tuple(
        _prepare_scoring_context(season, config) for season in seasons
    )
    for generation in range(generation_start, evolution.generations + 1):
        performances, summary, showcase = _evaluate_population(
            population, baseline, scoring_contexts, config, evolution, generation,
            should_cancel, on_arena,
        )
        ranked = sorted(
            range(len(population)),
            key=lambda index: (-performances[index].fitness, population[index].brain_id),
        )
        winner = population[ranked[0]]
        winner_performance = performances[ranked[0]]
        next_population = _reproduce(
            population, ranked, summary, evolution, generation
        )
        latest = TrainingCheckpoint(
            corpus.corpus_id, config, evolution, generation, next_population,
            winner, winner_performance, (*history, summary), showcase,
        )
        population = next_population
        history = latest.history
        if on_generation is not None:
            on_generation(latest)
    assert latest is not None
    return latest


def _evaluate_population(
    population, baseline, scoring_contexts, config, evolution, generation,
    should_cancel, on_arena,
):
    totals = [[0.0, 0, 0, 0, 0.0, 0.0] for _ in population]
    strategy_scores = {strategy.value: {} for strategy in DraftStrategy}
    showcase = None
    arena_count = 0
    indices = list(range(len(population)))
    group_count = math.ceil(len(population) / config.team_count)
    rotation_step = _coprime_rotation_step(len(indices))
    expected_arenas = (
        group_count
        * evolution.appearances_per_generation
        * len(scoring_contexts)
        * 2
    )
    for appearance in range(evolution.appearances_per_generation):
        for season_index, scoring_context in enumerate(scoring_contexts):
            season = scoring_context.season
            # A step coprime to every population size visits distinct seats
            # instead of aliasing at sizes such as 31.
            exposure = (
                (generation - 1)
                * evolution.appearances_per_generation
                * len(scoring_contexts)
                + appearance * len(scoring_contexts)
                + season_index
            )
            offset = exposure * rotation_step % len(indices)
            ordered = indices[offset:] + indices[:offset]
            for group_index in range(group_count):
                if should_cancel():
                    raise InterruptedError("draft brain training was cancelled")
                members = ordered[
                    group_index * config.team_count:(group_index + 1) * config.team_count
                ]
                scored_count = len(members)
                # A partial final cohort borrows cyclic population members as
                # opponents. Borrowed seats are not scored twice; this avoids
                # giving only the remainder cohort easier baseline opponents.
                seats = members + [
                    ordered[index % len(ordered)]
                    for index in range(config.team_count - scored_count)
                ]
                arena_seed = (
                    evolution.seed
                    + generation * 1_000_003
                    + appearance * 10_007
                    + season_index * 101
                    + group_index
                )
                brains = tuple(population[index] for index in seats)
                draft = simulate_snake_draft(
                    season, config, brains, seed=arena_seed,
                    candidate_window=evolution.candidate_window, should_cancel=should_cancel,
                )
                trace = simulate_historical_season(
                    draft.rosters, season, config, _prepared=scoring_context
                )
                control_draft = simulate_snake_draft(
                    season, config, (baseline,) * config.team_count,
                    seed=arena_seed,
                    candidate_window=evolution.candidate_window,
                    should_cancel=should_cancel,
                )
                control_trace = simulate_historical_season(
                    control_draft.rosters, season, config,
                    _prepared=scoring_context,
                )
                arena_count += 2
                by_team = _arena_team_scores(trace, config)
                control_by_team = _arena_team_scores(control_trace, config)
                for seat, index in enumerate(seats[:scored_count]):
                    standing, points_pct, raw_fitness = by_team[
                        f"drafter-{seat + 1}"
                    ]
                    control_fitness = control_by_team[standing.team_id][2]
                    champion = int(standing.team_id == trace.champion_team_id)
                    fitness = raw_fitness - control_fitness
                    total = totals[index]
                    total[0] += fitness; total[1] += 1; total[2] += champion
                    total[3] += int(standing.made_playoffs)
                    total[4] += standing.finish_rank
                    total[5] += points_pct
                    strategy = draft.strategies[seat].value
                    strategy_scores[strategy].setdefault(index, []).append(fitness)
                    if showcase is None or fitness > showcase[0]:
                        showcase = (fitness, draft, trace, index, seat, season)
                if on_arena is not None:
                    on_arena(generation, arena_count, expected_arenas)
    performances = tuple(_aggregate_performance(row) for row in totals)
    ranked = sorted(range(len(population)), key=lambda i: (-performances[i].fitness, population[i].brain_id))
    champion_index = ranked[0]
    all_appearances = sum(row.appearances for row in performances)
    leaders = {}
    for strategy, by_index in strategy_scores.items():
        if by_index:
            winner = min(
                by_index,
                key=lambda index: (-sum(by_index[index]) / len(by_index[index]), population[index].brain_id),
            )
            leaders[strategy] = population[winner].brain_id
    summary = GenerationSummary(
        generation, population[champion_index].brain_id,
        performances[champion_index].fitness,
        sum(row.fitness for row in performances) / len(performances),
        sum(row.championships for row in performances) / all_appearances,
        sum(row.playoffs for row in performances) / all_appearances,
        arena_count, leaders,
    )
    assert showcase is not None
    return performances, summary, _showcase_record(*showcase[1:])


def _aggregate_performance(values):
    _, count, championships, playoffs, finish, points = values
    if not count:
        raise AssertionError("every genome must receive an arena appearance")
    return GenomePerformance(
        values[0] / count, count, championships, playoffs, finish / count, points / count
    )


def _arena_team_scores(trace, config):
    standings = {row.team_id: row for row in trace.standings}
    points_percentiles = _tie_aware_points_percentiles(trace.teams)
    scores = {}
    for team_id, standing in standings.items():
        finish_pct = (
            config.team_count - standing.finish_rank
        ) / max(1, config.team_count - 1)
        points_pct = points_percentiles[team_id]
        champion = int(team_id == trace.champion_team_id)
        fitness = (
            1_000 * champion
            + 250 * standing.made_playoffs
            + 100 * finish_pct
            + 50 * points_pct
        )
        scores[team_id] = standing, points_pct, fitness
    return scores


def _tie_aware_points_percentiles(teams):
    by_points = {}
    for team in teams:
        by_points.setdefault(team.points_for, []).append(team.team_id)
    denominator = max(1, len(teams) - 1)
    result = {}
    first_rank = 1
    for points in sorted(by_points, reverse=True):
        team_ids = by_points[points]
        last_rank = first_rank + len(team_ids) - 1
        midrank = (first_rank + last_rank) / 2
        percentile = (len(teams) - midrank) / denominator
        result.update((team_id, percentile) for team_id in team_ids)
        first_rank = last_rank + 1
    return result


def _coprime_rotation_step(size):
    step = max(1, size // 2)
    while math.gcd(step, size) != 1:
        step += 1
    return step


def _showcase_record(draft: DraftResult, trace: HistoricalSeasonTrace, genome_index, seat, season):
    return {
        "selected_drafter_number": seat + 1,
        "selected_genome_index": genome_index,
        "draft": draft.to_record(season),
        "season": trace.to_record(),
    }
def _reproduce(population, ranked, summary, evolution, generation):
    elite_count = max(1, math.ceil(len(population) * evolution.elite_fraction))
    required_ids = {summary.champion_brain_id, *summary.strategy_leaders.values()}
    retained = list(ranked[:elite_count])
    retained_ids = {population[index].brain_id for index in retained}
    for index in ranked:
        brain_id = population[index].brain_id
        if brain_id in required_ids and brain_id not in retained_ids:
            retained.append(index)
            retained_ids.add(brain_id)
    elites = tuple(population[index] for index in retained)
    result = list(elites)
    while len(result) < len(population):
        child_index = len(result)
        left = population[_tournament(ranked, evolution.seed, generation, child_index, 0)]
        right = population[_tournament(ranked, evolution.seed, generation, child_index, 1)]
        result.append(crossover_and_mutate(
            left, right, seed=evolution.seed, generation=generation,
            offspring_index=child_index, mutation_rate=evolution.mutation_rate,
            mutation_magnitude=evolution.mutation_magnitude,
        ))
    return tuple(result)


def _tournament(ranked, seed, generation, child, parent):
    choices = []
    for draw in range(3):
        payload = canonical_json({
            "domain": "draft-parent-tournament-v1",
            "seed": seed,
            "generation": generation,
            "child": child,
            "parent": parent,
            "draw": draw,
        }).encode("utf-8")
        choices.append(int.from_bytes(sha256(payload).digest()[:8], "big") % len(ranked))
    return ranked[min(choices)]


def _validate_training_inputs(corpus, config, evolution):
    if not isinstance(corpus, HistoricalCorpus) or not isinstance(config, DraftLeagueConfig) or not isinstance(evolution, EvolutionConfig):
        raise ValueError("training inputs use invalid domain types")
    by_year = {row.season: row for row in corpus.seasons}
    absent = set(evolution.training_years).difference(by_year)
    if absent:
        raise ValueError(f"training year {min(absent)} is unavailable")
    required = set((*config.regular_season_weeks, *config.playoff_weeks))
    seasons = tuple(by_year[year] for year in evolution.training_years)
    if any(not required.issubset(row.available_weeks) for row in seasons):
        raise ValueError("a training season lacks the configured week coverage")
    weighted_fields = {
        name for name, weight in config.scoring_weights.items() if weight != 0
    }
    for season in seasons:
        actual_fields = {
            name
            for player in season.players
            for week in player.actual_weeks
            if week.status is ActualWeekStatus.PLAYED
            for name in week.stats
        }
        missing = weighted_fields.difference(actual_fields)
        if missing:
            raise ValueError(
                f"season {season.season} actual stats lack scoring field {min(missing)!r}"
            )
    return seasons


def _validate_resume(resume, corpus, config, evolution, baseline):
    if not isinstance(resume, TrainingCheckpoint):
        raise ValueError("resume must be a TrainingCheckpoint")
    previous_evolution = resume.evolution_config._content()
    requested_evolution = evolution._content()
    previous_evolution.pop("generations")
    requested_evolution.pop("generations")
    if (
        resume.corpus_id != corpus.corpus_id
        or resume.league_config.config_id != config.config_id
        or previous_evolution != requested_evolution
        or evolution.generations < resume.generation_completed
    ):
        raise ValueError("checkpoint does not match this corpus or training configuration")
    if resume.population[0].schema != baseline.schema or resume.population[0].baseline != baseline.baseline:
        raise ValueError("checkpoint baseline does not match the rebuilt training baseline")


_GENOME_FIELDS = {
    "input_weights", "input_biases", "hidden_weights", "hidden_biases",
    "output_weights", "output_bias", "brain_id",
}


def _genome_record(brain):
    """Serialize only learned parameters; schema and baseline live once per checkpoint."""

    return {
        "input_weights": brain.input_weights,
        "input_biases": brain.input_biases,
        "hidden_weights": brain.hidden_weights,
        "hidden_biases": brain.hidden_biases,
        "output_weights": brain.output_weights,
        "output_bias": brain.output_bias,
        "brain_id": brain.brain_id,
    }


def _brain_from_genome(schema, baseline, fingerprint, record):
    _record(record, _GENOME_FIELDS, "checkpoint genome")
    brain = DraftBrain(
        schema, baseline, fingerprint,
        record["input_weights"], record["input_biases"],
        record["hidden_weights"], record["hidden_biases"],
        record["output_weights"], record["output_bias"],
    )
    if record["brain_id"] != brain.brain_id:
        raise ValueError("checkpoint genome does not match brain_id")
    return brain


def _performance_from_record(record):
    keys = {"fitness", "appearances", "championships", "playoffs", "mean_finish", "mean_points_percentile"}
    _record(record, keys, "genome performance")
    return GenomePerformance(*(record[name] for name in (
        "fitness", "appearances", "championships", "playoffs",
        "mean_finish", "mean_points_percentile",
    )))


def _summary_from_record(record):
    keys = {"generation", "champion_brain_id", "champion_fitness", "mean_fitness", "championship_rate", "playoff_rate", "arena_count", "strategy_leaders"}
    _record(record, keys, "generation summary")
    return GenerationSummary(*(record[name] for name in (
        "generation", "champion_brain_id", "champion_fitness", "mean_fitness",
        "championship_rate", "playoff_rate", "arena_count", "strategy_leaders"
    )))


def _record(value, keys, name):
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} record fields are invalid")


def _integer(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


def _rate(name, value, *, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _finite(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _freeze_json(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value):
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


__all__ = (
    "EvolutionConfig", "GenerationSummary", "GenomePerformance", "TrainingCheckpoint",
    "run_training_batch", "training_estimate",
)
