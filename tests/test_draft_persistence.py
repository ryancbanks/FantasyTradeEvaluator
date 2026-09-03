import copy
from tempfile import TemporaryDirectory
import unittest

from tests.test_draft_brain import components
from tests.test_draft_history import historical_corpus
from trade_snapshot.draft_brain import DraftBrain
from trade_snapshot.draft_config import DraftLeagueConfig
from trade_snapshot.draft_persistence import DraftFileStore, DraftModelArtifact


class DraftPersistenceTests(unittest.TestCase):
    def test_corpus_and_model_are_atomic_strict_and_exportable(self):
        corpus = historical_corpus()
        config = DraftLeagueConfig.standard_ppr(team_count=2)
        schema, source_baseline = components()
        baseline = type(source_baseline)(
            schema.feature_schema_id,
            source_baseline.coefficients,
            source_baseline.intercept,
        )
        brain = DraftBrain.zero_residual(schema, baseline, config.config_id)
        artifact = DraftModelArtifact(
            brain, config, corpus.corpus_id, (2025,), 3,
            {"fitness": 1.25}, "2026-09-02T12:00:00+00:00",
        )
        with TemporaryDirectory() as directory:
            store = DraftFileStore(directory)
            self.assertEqual(store.import_corpus(corpus.to_record())["corpus_id"], corpus.corpus_id)
            self.assertEqual(store.load_corpus(corpus.corpus_id), corpus)
            self.assertEqual(store.list_corpora()[0]["corpus_id"], corpus.corpus_id)
            path = store.save_model(artifact)
            self.assertTrue(path.name.endswith(".draftbrain.json"))
            self.assertEqual(store.load_model(artifact.model_id), artifact)
            self.assertEqual(store.list_models()[0]["brain_id"], brain.brain_id)
            self.assertEqual(store.model_path(artifact.model_id), path.resolve())

    def test_tampered_artifacts_and_unsafe_checkpoint_names_are_rejected(self):
        corpus = historical_corpus()
        config = DraftLeagueConfig.standard_ppr(team_count=2)
        schema, baseline = components()
        brain = DraftBrain.zero_residual(schema, baseline, config.config_id)
        artifact = DraftModelArtifact(
            brain, config, corpus.corpus_id, (2025,), 1, {"fitness": 1},
            "2026-09-02T12:00:00Z",
        )
        changed = copy.deepcopy(artifact.to_record())
        changed["generation"] = 2
        with self.assertRaisesRegex(ValueError, "model_id"):
            DraftModelArtifact.from_record(changed)
        with TemporaryDirectory() as directory:
            store = DraftFileStore(directory)
            with self.assertRaisesRegex(ValueError, "job ID"):
                store.save_checkpoint("../escape", {"safe": True})
            job_id = "a" * 32
            store.save_checkpoint(job_id, {"safe": True})
            self.assertEqual(store.load_checkpoint(job_id), {"safe": True})

    def test_training_checkpoint_has_a_small_discoverable_catalog_row(self):
        from tests.draft_fixtures import small_draft_config, small_historical_corpus
        from trade_snapshot.draft_training import EvolutionConfig, run_training_batch

        corpus = small_historical_corpus()
        settings = EvolutionConfig(4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 2)
        checkpoint = run_training_batch(corpus, small_draft_config(), settings)
        with TemporaryDirectory() as directory:
            store = DraftFileStore(directory)
            job_id = "b" * 32
            store.save_checkpoint(job_id, checkpoint.to_record())
            rows = store.list_checkpoints()
            self.assertEqual(rows[0]["checkpoint_job_id"], job_id)
            self.assertEqual(rows[0]["checkpoint_id"], checkpoint.checkpoint_id)
            self.assertEqual(rows[0]["generation_completed"], 1)
            self.assertNotIn("population", rows[0])

    def test_catalog_rejects_dangling_summary_sidecars(self):
        corpus = historical_corpus()
        with TemporaryDirectory() as directory:
            store = DraftFileStore(directory)
            store.import_corpus(corpus.to_record())
            (
                store.corpus_directory
                / f"{corpus.corpus_id}.draftcorpus.json"
            ).unlink()
            row = store.list_corpora()[0]

        self.assertEqual(row["status"], "invalid")
        self.assertIn("no matching saved asset", row["error"])


if __name__ == "__main__":
    unittest.main()
