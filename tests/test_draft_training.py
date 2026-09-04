import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from tests.draft_fixtures import small_draft_config, small_historical_corpus
from trade_snapshot._scenario_random import content_id
from trade_snapshot.draft_config import score_raw_stats
from trade_snapshot.draft_history import ActualWeekStatus
from trade_snapshot.draft_season import _prepare_scoring_context
from trade_snapshot.draft_simulation import _new_simulation_cache
from trade_snapshot.draft_training import (
    EvolutionConfig,
    GenerationSummary,
    TrainingCheckpoint,
    _reproduce,
    run_training_batch,
    training_estimate,
    _tournament,
)


def evolution(generations=2):
    return EvolutionConfig(
        population_size=4,
        generations=generations,
        appearances_per_generation=1,
        elite_fraction=0.25,
        mutation_rate=0.1,
        mutation_magnitude=1_000,
        candidate_window=4,
        training_years=(2025,),
        seed=72,
    )


class DraftTrainingTests(unittest.TestCase):
    def test_season_scoring_is_prepared_once_across_generations_and_arenas(self):
        corpus = small_historical_corpus()
        settings = replace(
            evolution(2), appearances_per_generation=2
        )
        played_week_count = sum(
            week.status is ActualWeekStatus.PLAYED
            for player in corpus.seasons[0].players
            for week in player.actual_weeks
        )

        with (
            patch(
                "trade_snapshot.draft_training._prepare_scoring_context",
                wraps=_prepare_scoring_context,
            ) as prepare,
            patch(
                "trade_snapshot.draft_season.score_raw_stats",
                wraps=score_raw_stats,
            ) as score,
            patch(
                "trade_snapshot.draft_training._new_simulation_cache",
                wraps=_new_simulation_cache,
            ) as simulation_cache,
        ):
            run_training_batch(corpus, small_draft_config(), settings)

        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(simulation_cache.call_count, 1)
        self.assertEqual(score.call_count, played_week_count)

    def test_runs_evolution_retains_champion_showcase_and_round_trips(self):
        checkpoint = run_training_batch(
            small_historical_corpus(), small_draft_config(), evolution()
        )
        self.assertEqual(checkpoint.generation_completed, 2)
        self.assertEqual(len(checkpoint.population), 4)
        self.assertEqual(len(checkpoint.history), 2)
        self.assertEqual(checkpoint.showcase["season"]["champion_team_name"], "Drafter #1")
        self.assertEqual(len(checkpoint.showcase["draft"]["picks"]), 20)
        restored = TrainingCheckpoint.from_record(checkpoint.to_record())
        self.assertEqual(restored.checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(restored.champion.brain_id, checkpoint.champion.brain_id)

    def test_checkpoint_v2_shares_model_metadata_and_reads_v1(self):
        checkpoint = run_training_batch(
            small_historical_corpus(), small_draft_config(), evolution(1)
        )
        compact = checkpoint.to_record()
        self.assertEqual(compact["schema_version"], 2)
        self.assertNotIn("feature_schema", compact["population_genomes"][0])
        legacy_content = {
            "corpus_id": checkpoint.corpus_id,
            "league_config": checkpoint.league_config.to_record(),
            "evolution_config": checkpoint.evolution_config.to_record(),
            "generation_completed": checkpoint.generation_completed,
            "population": [row.to_record() for row in checkpoint.population],
            "champion": checkpoint.champion.to_record(),
            "champion_performance": checkpoint.champion_performance.to_record(),
            "history": [row.to_record() for row in checkpoint.history],
            "showcase": copy.deepcopy(compact["showcase"]),
        }
        legacy = {
            "kind": "draft_training_checkpoint", "schema_version": 1,
            **legacy_content,
            "checkpoint_id": content_id("draft_checkpoint", legacy_content),
        }
        restored = TrainingCheckpoint.from_record(legacy)
        self.assertEqual(restored.champion.brain_id, checkpoint.champion.brain_id)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(legacy)) * 0.9)

    def test_generation_checkpoint_resume_matches_uninterrupted_run(self):
        corpus, config, settings = small_historical_corpus(), small_draft_config(), evolution()
        uninterrupted = run_training_batch(corpus, config, settings)
        captured = []

        class StopAfterFirst(Exception):
            pass

        def stop(checkpoint):
            captured.append(checkpoint)
            if checkpoint.generation_completed == 1:
                raise StopAfterFirst

        with self.assertRaises(StopAfterFirst):
            run_training_batch(corpus, config, settings, on_generation=stop)
        resumed = run_training_batch(corpus, config, settings, resume=captured[0])
        self.assertEqual(resumed.checkpoint_id, uninterrupted.checkpoint_id)
        self.assertEqual(
            tuple(row.brain_id for row in resumed.population),
            tuple(row.brain_id for row in uninterrupted.population),
        )

    def test_completed_small_trial_can_extend_to_a_larger_target(self):
        corpus, config = small_historical_corpus(), small_draft_config()
        first = run_training_batch(corpus, config, evolution(1))
        extended = run_training_batch(corpus, config, evolution(2), resume=first)
        uninterrupted = run_training_batch(corpus, config, evolution(2))
        self.assertEqual(extended.checkpoint_id, uninterrupted.checkpoint_id)

    def test_work_estimate_and_cancellation_are_bounded(self):
        corpus, config, settings = small_historical_corpus(), small_draft_config(), evolution(3)
        estimate = training_estimate(corpus, config, settings)
        self.assertEqual(estimate["total_leagues"], 6)
        self.assertEqual(estimate["brain_appearances"], 12)
        with self.assertRaisesRegex(InterruptedError, "cancelled"):
            run_training_batch(corpus, config, settings, should_cancel=lambda: True)

    def test_every_genome_faces_every_selected_season(self):
        source = small_historical_corpus()
        season_2025 = source.seasons[0]
        season_2024 = replace(
            season_2025,
            season=2024,
            preseason_as_of="2024-08-01T00:00:00+00:00",
            season_kickoff_at="2024-09-01T00:00:00+00:00",
        )
        provenance = replace(
            source.provenance[0],
            preseason_source_as_of={
                2024: "2024-07-20T12:00:00+00:00",
                **source.provenance[0].preseason_source_as_of,
            },
        )
        corpus = replace(
            source,
            seasons=(season_2024, season_2025),
            provenance=(provenance,),
        )
        settings = replace(evolution(1), training_years=(2024, 2025))
        estimate = training_estimate(corpus, small_draft_config(), settings)
        self.assertEqual(estimate["training_season_count"], 2)
        self.assertEqual(estimate["leagues_per_generation"], 4)
        self.assertEqual(estimate["brain_appearances"], 8)
        checkpoint = run_training_batch(corpus, small_draft_config(), settings)
        self.assertEqual(checkpoint.history[0].arena_count, 4)
        self.assertEqual(checkpoint.champion_performance.appearances, 2)

    def test_equal_outcomes_do_not_reward_a_deterministic_draft_seat(self):
        corpus = small_historical_corpus()
        equal_players = tuple(
            replace(
                player,
                actual_weeks=tuple(
                    replace(week, stats={"points": 10.0})
                    for week in player.actual_weeks
                ),
            )
            for player in corpus.seasons[0].players
        )
        corpus = replace(
            corpus,
            seasons=(replace(corpus.seasons[0], players=equal_players),),
        )

        checkpoint = run_training_batch(
            corpus, small_draft_config(), evolution(1)
        )

        self.assertEqual(checkpoint.champion_performance.fitness, 0.0)
        self.assertEqual(checkpoint.history[0].champion_fitness, 0.0)
        self.assertEqual(checkpoint.history[0].mean_fitness, 0.0)
        self.assertEqual(
            checkpoint.champion_performance.mean_points_percentile, 0.5
        )
        self.assertEqual(checkpoint.history[0].arena_count, 2)

    def test_checkpoint_tampering_and_configuration_mismatch_are_rejected(self):
        corpus, config = small_historical_corpus(), small_draft_config()
        first = run_training_batch(corpus, config, evolution(1))
        record = copy.deepcopy(first.to_record())
        record["champion_performance"]["fitness"] += 1
        with self.assertRaisesRegex(ValueError, "checkpoint_id"):
            TrainingCheckpoint.from_record(record)
        incompatible = replace(evolution(2), candidate_window=3)
        with self.assertRaisesRegex(ValueError, "training configuration"):
            run_training_batch(corpus, config, incompatible, resume=first)

    def test_missing_actual_scoring_fields_fail_before_simulation(self):
        config = replace(
            small_draft_config(), scoring_weights={"missing_stat": 1.0}
        )
        with self.assertRaisesRegex(ValueError, "actual stats lack scoring field"):
            training_estimate(small_historical_corpus(), config, evolution(1))

    def test_parent_tournament_does_not_alias_population_sizes_divisible_by_97(self):
        ranked = list(range(194))
        selected = {
            _tournament(ranked, 7, 3, child, parent)
            for child in range(194)
            for parent in (0, 1)
        }
        self.assertGreater(len(selected), 30)

    def test_duplicate_required_elites_do_not_freeze_the_population(self):
        corpus, config = small_historical_corpus(), small_draft_config()
        checkpoint = run_training_batch(corpus, config, evolution(1))
        brain = checkpoint.champion
        population = (brain,) * 4
        summary = GenerationSummary(
            1, brain.brain_id, 1.0, 1.0, 0.0, 0.0, 1,
            {"none": brain.brain_id},
        )
        settings = replace(evolution(1), mutation_rate=1.0)
        children = _reproduce(population, list(range(4)), summary, settings, 1)
        self.assertEqual(sum(row.brain_id == brain.brain_id for row in children), 1)


if __name__ == "__main__":
    unittest.main()
