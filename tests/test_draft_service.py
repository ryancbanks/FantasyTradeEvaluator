from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import trade_snapshot.draft_service as draft_service_module
from tests.draft_fixtures import small_draft_config, small_historical_corpus
from tests.test_engine_bundle import engine_bundle
from trade_snapshot.draft_assistant import (
    DraftAssistantSession,
    assistant_board_coverage,
)
from trade_snapshot.draft_config import DraftStrategy
from trade_snapshot.draft_espn_live import EspnDraftObservation, EspnDraftSyncError
from trade_snapshot.draft_features import build_baseline_brain
from trade_snapshot.draft_history import DraftPlayerBoard
from trade_snapshot.draft_persistence import DraftModelArtifact
from trade_snapshot.draft_service import (
    DraftLabService,
    _DraftJob,
    _MAX_RETAINED_TERMINAL_JOBS,
)
from trade_snapshot.draft_training import EvolutionConfig, run_training_batch
from trade_snapshot.engine_bundle import save_engine_bundle


class _EspnDraftAdapterStub:
    def __init__(self):
        self.observation = None
        self.calls = []

    def poll(self, **kwargs):
        self.calls.append(kwargs)
        if self.observation is None:
            raise AssertionError("test observation was not configured")
        return self.observation


class DraftLabServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.espn = _EspnDraftAdapterStub()
        self.service = DraftLabService(
            self.directory.name,
            self.directory.name + "/bundles",
            espn_draft_adapter=self.espn,
        )
        self.corpus = small_historical_corpus()
        self.config = small_draft_config()
        self.service.import_corpus(self.corpus.to_record())

    def tearDown(self):
        self.directory.cleanup()

    def wait(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.service.job(job_id)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("Draft Lab job did not finish")

    def test_active_catalog_tracks_one_draft_job_and_rejects_corrupt_ownership(self):
        active = _DraftJob("a" * 32, "training", status="running")
        self.service._jobs[active.job_id] = active
        self.assertEqual(self.service.active_job(), self.service.job(active.job_id))

        second = _DraftJob("b" * 32, "benchmark", status="queued")
        self.service._jobs[second.job_id] = second
        with self.assertRaisesRegex(RuntimeError, "multiple Draft Lab"):
            self.service.active_job()

        active.status = "complete"
        second.status = "cancelled"
        self.assertIsNone(self.service.active_job())

    def test_latest_terminal_job_is_recoverable_until_the_ui_acknowledges_it(self):
        first = _DraftJob("a" * 32, "training", status="running")
        self.service._jobs[first.job_id] = first
        self.service._set_job(first, status="complete", result={"model": {}})
        self.assertEqual(self.service.recoverable_job()["job_id"], first.job_id)

        second = _DraftJob("b" * 32, "benchmark", status="running")
        self.service._jobs[second.job_id] = second
        self.service._set_job(second, status="failed", error="test failure")
        self.assertFalse(
            self.service.acknowledge_job_activity(first.job_id)["acknowledged"]
        )
        self.assertEqual(self.service.recoverable_job()["job_id"], second.job_id)
        self.assertTrue(
            self.service.acknowledge_job_activity(second.job_id)["acknowledged"]
        )
        self.assertIsNone(self.service.recoverable_job())
        self.assertFalse(
            self.service.acknowledge_job_activity(second.job_id)["acknowledged"]
        )

    def test_catalog_estimate_background_training_autosave_and_model(self):
        evolution = EvolutionConfig(4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 2)
        payload = {
            "corpus_id": self.corpus.corpus_id,
            "league_config": self.config.to_record(),
            "evolution_config": evolution.to_record(),
        }
        self.assertEqual(self.service.estimate_training(payload)["total_leagues"], 2)
        job = self.service.start_training(payload)
        complete = self.wait(job["job_id"])
        self.assertEqual(complete["status"], "complete")
        self.assertTrue(complete["progress"]["autosaved"])
        result = self.service.job_result(job["job_id"])
        model_id = result["model"]["model_id"]
        self.assertTrue(self.service.model_path(model_id).is_file())
        catalog = self.service.catalog()
        self.assertEqual(catalog["models"][0]["model_id"], model_id)
        self.assertEqual(catalog["corpora"][0]["seasons"], [2025])
        self.assertEqual(catalog["checkpoints"][0]["checkpoint_job_id"], job["job_id"])
        self.assertIn("standard-ppr", {row["preset_id"] for row in catalog["league_presets"]})
        resumed = self.service.resume_training(job["job_id"], generations=2)
        self.assertEqual(self.wait(resumed["job_id"])["status"], "complete")
        self.assertEqual(
            self.service.job_result(resumed["job_id"])["model"]["generation"], 2
        )

    def test_terminal_job_results_are_bounded_without_evicting_active_work(self):
        active = _DraftJob("f" * 32, "training", status="running")
        self.service._jobs[active.job_id] = active
        for index in range(_MAX_RETAINED_TERMINAL_JOBS + 4):
            job = _DraftJob(f"{index:032x}", "benchmark")
            self.service._jobs[job.job_id] = job
            self.service._set_job(
                job,
                status="complete",
                result={"trial": index},
            )

        terminal = [
            job
            for job in self.service._jobs.values()
            if job.status == "complete"
        ]
        self.assertEqual(len(terminal), _MAX_RETAINED_TERMINAL_JOBS)
        self.assertIs(self.service._jobs[active.job_id], active)
        self.assertEqual(
            self.service.job(f"{_MAX_RETAINED_TERMINAL_JOBS + 3:032x}")["status"],
            "complete",
        )
        with self.assertRaises(FileNotFoundError):
            self.service.job("0" * 32)

    def test_rejects_oversized_assistant_session_before_reading_it(self):
        session_id = "e" * 32
        self.service._session_path(session_id).write_text("{}", encoding="utf-8")
        with patch("trade_snapshot.draft_service._MAX_ASSISTANT_SESSION_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.service._load_session(session_id)

    def test_autosaved_champion_can_be_promoted_without_more_training(self):
        evolution = EvolutionConfig(4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 2)
        checkpoint = run_training_batch(self.corpus, self.config, evolution)
        checkpoint_job_id = "a" * 32
        self.service.store.save_checkpoint(checkpoint_job_id, checkpoint.to_record())
        performance = checkpoint.champion_performance
        exact_metrics = {
            "fitness": performance.fitness,
            "championship_rate": performance.championships / performance.appearances,
            "playoff_rate": performance.playoffs / performance.appearances,
            "mean_finish": performance.mean_finish,
        }
        for seasons, metrics, created_at in (
            ((2025,), {**exact_metrics, "fitness": exact_metrics["fitness"] + 1},
             "2026-09-02T11:00:00+00:00"),
            ((2024,), exact_metrics, "2026-09-02T11:30:00+00:00"),
        ):
            self.service.store.save_model(DraftModelArtifact(
                checkpoint.champion, checkpoint.league_config, checkpoint.corpus_id,
                seasons, checkpoint.generation_completed, metrics, created_at,
            ))

        promoted = self.service.promote_checkpoint(checkpoint_job_id)
        repeated = self.service.promote_checkpoint(checkpoint_job_id)

        self.assertEqual(promoted, repeated)
        self.assertEqual(promoted["brain_id"], checkpoint.champion.brain_id)
        self.assertEqual(promoted["generation"], checkpoint.generation_completed)
        self.assertEqual(promoted["trained_seasons"], [2025])
        self.assertEqual(promoted["metrics"], exact_metrics)
        self.assertEqual(len(self.service.catalog()["models"]), 3)

    def test_concurrent_checkpoint_promotion_creates_one_model(self):
        evolution = EvolutionConfig(4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 2)
        checkpoint = run_training_batch(self.corpus, self.config, evolution)
        checkpoint_job_id = "b" * 32
        self.service.store.save_checkpoint(checkpoint_job_id, checkpoint.to_record())
        original_list_models = self.service.store.list_models
        first_scan_started = Event()
        release_first_scan = Event()

        def delayed_list_models():
            snapshot = original_list_models()
            if not first_scan_started.is_set():
                first_scan_started.set()
                release_first_scan.wait(2)
            return snapshot

        self.service.store.list_models = delayed_list_models
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(
                    self.service.promote_checkpoint, checkpoint_job_id
                )
                self.assertTrue(first_scan_started.wait(2))
                second_started = Event()

                def second_promotion():
                    second_started.set()
                    return self.service.promote_checkpoint(checkpoint_job_id)

                second = workers.submit(second_promotion)
                self.assertTrue(second_started.wait(2))
                time.sleep(0.05)
                release_first_scan.set()
                promoted = first.result(timeout=2)
                repeated = second.result(timeout=2)
        finally:
            release_first_scan.set()
            self.service.store.list_models = original_list_models

        self.assertEqual(promoted, repeated)
        self.assertEqual(len(self.service.store.list_models()), 1)

    def test_infeasible_current_board_does_not_leave_a_saved_room(self):
        evolution = EvolutionConfig(4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 2)
        job = self.service.start_training({
            "corpus_id": self.corpus.corpus_id,
            "league_config": self.config.to_record(),
            "evolution_config": evolution.to_record(),
        })
        self.assertEqual(self.wait(job["job_id"])["status"], "complete")
        model_id = self.service.job_result(job["job_id"])["model"]["model_id"]
        board = DraftPlayerBoard(
            2026,
            "2026-08-01T00:00:00+00:00",
            "2026-09-01T00:00:00+00:00",
            tuple(
                replace(
                    player,
                    player_id=f"all-qb-{player.player_id}",
                    position="QB",
                    eligible_positions=("QB",),
                    actual_weeks=(),
                )
                for player in self.corpus.seasons[0].players
            ),
        )
        self.service.import_board(board.to_record())

        with self.assertRaisesRegex(ValueError, "fill"):
            self.service.create_assistant({
                "model_id": model_id,
                "board_id": board.board_id,
                "user_drafter_number": 2,
                "strategy": "none",
            })

        self.assertEqual(self.service.catalog()["assistant_sessions"], ())

    def test_catalog_reuses_shared_assets_and_equivalent_coverage(self):
        brain = build_baseline_brain(self.corpus, self.config, (2025,))
        model = DraftModelArtifact(
            brain,
            self.config,
            self.corpus.corpus_id,
            (2025,),
            0,
            {"fitness": 0.0},
            "2026-09-02T12:00:00+00:00",
        )
        self.service.store.save_model(model)
        board = DraftPlayerBoard(
            2026,
            "2026-08-01T00:00:00+00:00",
            "2026-09-01T00:00:00+00:00",
            tuple(
                replace(player, player_id=f"catalog-{player.player_id}", actual_weeks=())
                for player in self.corpus.seasons[0].players
            ),
        )
        self.service.import_board(board.to_record())
        for index, drafter_number in enumerate((1, 1, 2)):
            self.service._save_session(DraftAssistantSession(
                f"{index:032x}",
                model.model_id,
                board.board_id,
                drafter_number,
                DraftStrategy.NONE,
            ))

        with (
            patch.object(
                self.service.store,
                "load_model",
                wraps=self.service.store.load_model,
            ) as load_model,
            patch.object(
                self.service.store,
                "load_board",
                wraps=self.service.store.load_board,
            ) as load_board,
            patch(
                "trade_snapshot.draft_service.assistant_board_coverage",
                wraps=assistant_board_coverage,
            ) as coverage,
        ):
            sessions = self.service.catalog()["assistant_sessions"]
            repeated = self.service.catalog()["assistant_sessions"]

        self.assertEqual(load_model.call_count, 1)
        self.assertEqual(load_board.call_count, 1)
        self.assertEqual(coverage.call_count, 4)
        self.assertEqual(repeated, sessions)
        self.assertEqual(sessions[0]["board_coverage"], sessions[1]["board_coverage"])
        self.assertIsNot(
            sessions[0]["board_coverage"], sessions[1]["board_coverage"]
        )
        self.assertIsNot(
            sessions[0]["board_coverage"]["feasibility"],
            sessions[1]["board_coverage"]["feasibility"],
        )

    def test_asset_cache_is_bounded_and_least_recently_used(self):
        loaded = []

        def load(identifier):
            loaded.append(identifier)
            return object()

        with patch("trade_snapshot.draft_service._MAX_CACHED_ASSETS_PER_KIND", 2):
            cache = self.service._model_cache
            first = self.service._load_asset(cache, "first", load)
            self.service._load_asset(cache, "second", load)
            self.assertIs(self.service._load_asset(cache, "first", load), first)
            self.service._load_asset(cache, "third", load)
            self.service._load_asset(cache, "second", load)

        self.assertEqual(loaded, ["first", "second", "third", "second"])
        self.assertEqual(tuple(cache), ("third", "second"))

    def test_synced_league_presets_do_not_reload_unchanged_bundles(self):
        bundle = engine_bundle()
        save_engine_bundle(
            bundle,
            self.service.bundle_directory / f"{bundle.bundle_id}.json",
        )
        with patch(
            "trade_snapshot.draft_service.load_engine_bundle",
            wraps=draft_service_module.load_engine_bundle,
        ) as load:
            first = self.service.catalog()["league_presets"]
            second = self.service.catalog()["league_presets"]

        self.assertEqual(load.call_count, 1)
        self.assertEqual(first, second)
        synced = next(row for row in first if row["source"] == "synced_league")
        self.assertEqual(synced["preset_id"], bundle.bundle_id)

    def test_manual_assistant_and_benchmark_jobs(self):
        evolution = EvolutionConfig(4, 1, 1, 0.25, 0.1, 1_000, 4, (2025,), 3)
        job = self.service.start_training({
            "corpus_id": self.corpus.corpus_id,
            "league_config": self.config.to_record(),
            "evolution_config": evolution.to_record(),
        })
        self.assertEqual(self.wait(job["job_id"])["status"], "complete")
        model_id = self.service.job_result(job["job_id"])["model"]["model_id"]
        season = self.corpus.seasons[0]
        board = DraftPlayerBoard(
            2026, "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00",
            tuple(replace(row, player_id=f"current-{row.player_id}", actual_weeks=()) for row in season.players),
        )
        self.service.import_board(board.to_record())
        assistant = self.service.create_assistant({
            "model_id": model_id, "board_id": board.board_id,
            "user_drafter_number": 1, "strategy": "none",
        })
        self.assertTrue(assistant["your_turn"])
        player_id = assistant["recommendations"][0]["player_id"]
        assistant = self.service.record_pick(
            assistant["session_id"], {"player_id": player_id, "drafter_number": 1}
        )
        self.assertEqual(len(assistant["picks"]), 1)
        self.assertEqual(self.service.catalog()["assistant_sessions"][0]["pick_count"], 1)
        self.assertEqual(len(self.service.assistant_players(assistant["session_id"])["players"]), 24)
        self.assertEqual(len(self.service.undo_pick(assistant["session_id"])["picks"]), 0)

        busy_job_id = "c" * 32
        self.service._jobs[busy_job_id] = SimpleNamespace(status="running")
        try:
            with self.assertRaisesRegex(RuntimeError, "recommendations are unavailable"):
                self.service.assistant(assistant["session_id"])
        finally:
            del self.service._jobs[busy_job_id]

        self.espn.observation = EspnDraftObservation(
            "123", 2026, ("10", "20", "30", "40"), (), False, True
        )
        initially_synced = self.service.sync_espn_draft(
            assistant["session_id"], {"league_id": "123", "season": 2026}
        )
        self.assertEqual(initially_synced["picks"], [])
        self.assertEqual(initially_synced["draft_binding"]["league_id"], "123")

        self.espn.observation = EspnDraftObservation(
            "123", 2026, ("10", "20", "30", "40"),
            ((1, player_id),), False, True,
        )
        synced = self.service.sync_espn_draft(
            assistant["session_id"], {"league_id": "123", "season": 2026}
        )
        self.assertEqual(len(synced["picks"]), 1)
        self.assertEqual(synced["live_sync"]["appended_pick_count"], 1)
        self.assertEqual(synced["draft_binding"], {
            "provider": "espn", "league_id": "123", "season": 2026,
            "team_order": ["10", "20", "30", "40"],
        })
        repeated = self.service.sync_espn_draft(
            assistant["session_id"], {"league_id": "123", "season": 2026}
        )
        self.assertEqual(repeated["live_sync"]["appended_pick_count"], 0)
        saved_room = self.service.catalog()["assistant_sessions"][0]
        self.assertEqual(saved_room["pick_count"], 1)
        self.assertEqual(saved_room["board_coverage"], synced["board_coverage"])
        self.assertEqual(saved_room["draft_binding"], synced["draft_binding"])
        self.assertEqual(self.espn.calls[-1]["team_count"], 4)
        self.assertEqual(self.espn.calls[-1]["roster_size"], 5)

        self.espn.observation = EspnDraftObservation(
            "123", 2026, ("10", "20", "30", "40"), (), False, True
        )
        with self.assertRaisesRegex(EspnDraftSyncError, "fewer picks"):
            self.service.sync_espn_draft(
                assistant["session_id"], {"league_id": "123", "season": 2026}
            )

        self.espn.observation = EspnDraftObservation(
            "456", 2026, ("10", "20", "30", "40"),
            ((1, player_id),), False, True,
        )
        with self.assertRaisesRegex(EspnDraftSyncError, "different public draft"):
            self.service.sync_espn_draft(
                assistant["session_id"], {"league_id": "456", "season": 2026}
            )

        self.espn.observation = EspnDraftObservation(
            "123", 2027, ("10", "20", "30", "40"),
            ((1, player_id),), False, True,
        )
        with self.assertRaisesRegex(EspnDraftSyncError, "different public draft"):
            self.service.sync_espn_draft(
                assistant["session_id"], {"league_id": "123", "season": 2027}
            )

        self.espn.observation = EspnDraftObservation(
            "123", 2026, ("40", "20", "30", "10"),
            ((1, player_id),), False, True,
        )
        with self.assertRaisesRegex(EspnDraftSyncError, "different public draft"):
            self.service.sync_espn_draft(
                assistant["session_id"], {"league_id": "123", "season": 2026}
            )
        with self.assertRaisesRegex(ValueError, "fields"):
            self.service.sync_espn_draft(
                assistant["session_id"],
                {"league_id": "123", "season": 2026, "cookie": "forbidden"},
            )

        benchmark = self.service.start_benchmark({
            "model_id": model_id, "trials": 2, "seed": 5,
            "candidate_window": 4, "evaluation_years": [2025],
        })
        self.assertEqual(self.wait(benchmark["job_id"])["status"], "complete")
        self.assertEqual(self.service.job_result(benchmark["job_id"])["trial_count"], 2)


if __name__ == "__main__":
    unittest.main()
