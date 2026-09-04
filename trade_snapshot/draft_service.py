"""Application service for local Draft Lab jobs, files, and assistant sessions."""

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from .draft_assistant import (
    AssistantDraftBinding,
    DraftAssistantSession,
    assistant_board_coverage,
    assistant_status,
    bind_assistant_draft,
    create_assistant_session,
    drafter_for_pick,
    reconcile_assistant_picks,
    record_assistant_pick,
    undo_assistant_pick,
)
from .draft_benchmark import compare_to_regression_baseline
from .draft_config import (
    DraftLeagueConfig,
    DraftStrategy,
    config_from_engine_bundle,
)
from .draft_corpus_install import CorpusInstallCancelled, DraftCorpusInstaller
from .draft_espn_live import (
    EspnDraftObservation,
    EspnDraftSyncError,
    EspnPublicDraftAdapter,
)
from .draft_persistence import DraftFileStore, DraftModelArtifact
from .draft_training import (
    EvolutionConfig,
    TrainingCheckpoint,
    run_training_batch,
    training_estimate,
)
from .engine_bundle import load_engine_bundle
from .job_retention import (
    ACTIVE_JOB_STATUSES,
    DEFAULT_TERMINAL_JOB_LIMIT,
    TERMINAL_JOB_STATUSES,
    has_active_jobs,
    prune_terminal_jobs,
)


_MAX_RETAINED_TERMINAL_JOBS = DEFAULT_TERMINAL_JOB_LIMIT
_MAX_ASSISTANT_SESSION_BYTES = 16 * 1024 * 1024
_MAX_CACHED_ASSETS_PER_KIND = 2
_MAX_CACHED_LEAGUE_PRESETS = 64


@dataclass(slots=True)
class _DraftJob:
    job_id: str
    kind: str
    status: str = "queued"
    progress: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    result: dict[str, object] | None = None
    cancel: Event = field(default_factory=Event)


class DraftLabService:
    def __init__(
        self,
        data_directory: str | Path,
        bundle_directory: str | Path,
        *,
        heavy_work_guard=lambda: None,
        activity_lock=None,
        espn_draft_adapter=None,
        corpus_installer=None,
    ) -> None:
        self.data_directory = Path(data_directory).resolve()
        self.bundle_directory = Path(bundle_directory).resolve()
        self.store = DraftFileStore(self.data_directory)
        self.session_directory = self.data_directory / "draft-assistant-sessions"
        self.session_directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._recommendation_lock = RLock()
        self._jobs: dict[str, _DraftJob] = {}
        self._pending_terminal_job_id: str | None = None
        self._corpus_cache = OrderedDict()
        self._model_cache = OrderedDict()
        self._board_cache = OrderedDict()
        self._league_preset_cache = OrderedDict()
        self._heavy_work_guard = heavy_work_guard
        self._activity_lock = activity_lock or RLock()
        self._espn_draft_adapter = (
            EspnPublicDraftAdapter()
            if espn_draft_adapter is None
            else espn_draft_adapter
        )
        if not callable(getattr(self._espn_draft_adapter, "poll", None)):
            raise ValueError("espn_draft_adapter must provide poll()")
        self._corpus_installer = (
            DraftCorpusInstaller(self.data_directory, store=self.store)
            if corpus_installer is None
            else corpus_installer
        )
        if not all(callable(getattr(self._corpus_installer, name, None)) for name in (
            "install", "catalog", "recoverable_state",
        )):
            raise ValueError("corpus_installer must provide install(), catalog(), and recoverable_state()")

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return has_active_jobs(self._jobs)

    def active_job(self) -> dict[str, object] | None:
        """Return the sole resumable Draft Lab job, if one exists."""

        with self._lock:
            active = tuple(
                job
                for job in self._jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            )
            if len(active) > 1:
                raise RuntimeError("multiple Draft Lab jobs are active")
            return self._job_record(active[0]) if active else None

    def recoverable_job(self) -> dict[str, object] | None:
        """Return active work or the latest terminal job not yet surfaced by the UI."""

        with self._lock:
            active = self.active_job()
            if active is not None:
                return active
            pending_id = self._pending_terminal_job_id
            pending = self._jobs.get(pending_id) if pending_id is not None else None
            if pending is not None and pending.status in TERMINAL_JOB_STATUSES:
                return self._job_record(pending)
            self._pending_terminal_job_id = None
            return None

    def acknowledge_job_activity(self, job_id: str) -> dict[str, object]:
        """Mark one terminal job as surfaced without clearing a newer result."""

        with self._lock:
            job = self._require_job(job_id)
            if job.status not in TERMINAL_JOB_STATUSES:
                raise RuntimeError("active Draft Lab activity cannot be acknowledged")
            acknowledged = self._pending_terminal_job_id == job_id
            if acknowledged:
                self._pending_terminal_job_id = None
            return {"job_id": job_id, "acknowledged": acknowledged}

    def catalog(self) -> dict[str, object]:
        return {
            "corpora": self.store.list_corpora(),
            "boards": self.store.list_boards(),
            "models": self.store.list_models(),
            "checkpoints": self.store.list_checkpoints(),
            "assistant_sessions": self._session_catalog(),
            "league_presets": self._league_presets(),
            "starter_corpus_installs": self._corpus_installer.catalog(),
            "starter_corpus_install_state": self._corpus_installer.recoverable_state(),
            "supported_training_seasons": [
                2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024, 2025
            ],
            "year_notice": (
                "2015 is usable only when the imported pack includes a genuine 2015 "
                "preseason snapshot. 2020 is intentionally excluded."
            ),
            "live_sync": {
                "status": "available",
                "provider": "espn",
                "access": "public",
                "polling": "on_demand",
                "message": (
                    "ESPN snake drafts can be polled from the public read endpoint. "
                    "The current board must map ESPN player IDs. Private leagues and "
                    "credentialed reads are not supported."
                ),
            },
        }

    def import_corpus(self, record):
        return self.store.import_corpus(record)

    def import_board(self, record):
        return self.store.import_board(record)

    def import_model(self, record):
        return self.store.import_model(record)

    def start_corpus_install(self, payload: Mapping[str, object] | None = None):
        """Start or resume the bounded public starter-corpus installation."""

        payload = {} if payload is None else payload
        _exact_keys("starter corpus install", payload, set())
        with self._activity_lock:
            self._heavy_work_guard()
            with self._lock:
                if self.is_busy:
                    raise RuntimeError("another Draft Lab background job is running")
                job = _DraftJob(uuid4().hex, "corpus_install")
                job.progress = {"phase": "manifest"}
                self._pending_terminal_job_id = None
                self._jobs[job.job_id] = job
        Thread(
            target=self._run_corpus_install,
            args=(job,),
            name=f"draft-corpus-install-{job.job_id[:8]}",
            daemon=True,
        ).start()
        return self._job_record(job)

    def estimate_training(self, payload: Mapping[str, object]) -> dict[str, object]:
        corpus, config, evolution, _ = self._training_inputs(payload)
        return training_estimate(corpus, config, evolution)

    def start_training(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._activity_lock:
            self._heavy_work_guard()
            with self._lock:
                if self.is_busy:
                    raise RuntimeError("another Draft Lab background job is running")
            corpus, config, evolution, resume = self._training_inputs(payload)
            with self._recommendation_lock:
                with self._lock:
                    if self.is_busy:
                        raise RuntimeError(
                            "another Draft Lab background job is running"
                        )
                    job = _DraftJob(uuid4().hex, "training")
                    job.progress = {
                        "generation": 0, "generation_count": evolution.generations
                    }
                    self._pending_terminal_job_id = None
                    self._jobs[job.job_id] = job
        Thread(
            target=self._run_training,
            args=(job, corpus, config, evolution, resume),
            name=f"draft-training-{job.job_id[:8]}",
            daemon=True,
        ).start()
        return self._job_record(job)

    def resume_training(
        self, checkpoint_job_id: str, generations: int | None = None
    ) -> dict[str, object]:
        with self._activity_lock:
            self._heavy_work_guard()
            with self._lock:
                if self.is_busy:
                    raise RuntimeError("another Draft Lab background job is running")
            checkpoint = TrainingCheckpoint.from_record(
                self.store.load_checkpoint(checkpoint_job_id)
            )
            evolution = checkpoint.evolution_config
            if generations is not None:
                if type(generations) is not int or not 1 <= generations <= 1_000:
                    raise ValueError("resume generations must be an integer from 1 through 1000")
                if generations < checkpoint.generation_completed:
                    raise ValueError("resume generations cannot precede the saved generation")
                evolution = replace(evolution, generations=generations)
            return self.start_training({
                "corpus_id": checkpoint.corpus_id,
                "league_config": checkpoint.league_config.to_record(),
                "evolution_config": evolution.to_record(),
                "resume_checkpoint_job_id": checkpoint_job_id,
            })

    def start_benchmark(self, payload: Mapping[str, object]) -> dict[str, object]:
        _exact_keys(
            "benchmark", payload,
            {"model_id", "trials", "seed", "candidate_window", "evaluation_years"},
        )
        years = payload["evaluation_years"]
        if not isinstance(years, list):
            raise ValueError("evaluation_years must be a JSON array")
        options = {
            "trials": payload["trials"], "seed": payload["seed"],
            "candidate_window": payload["candidate_window"],
            "evaluation_years": tuple(years),
        }
        with self._activity_lock:
            self._heavy_work_guard()
            with self._lock:
                if self.is_busy:
                    raise RuntimeError("another Draft Lab background job is running")
            model = self._load_model(payload["model_id"])
            corpus = self._load_corpus(model.corpus_id)
            with self._recommendation_lock:
                with self._lock:
                    if self.is_busy:
                        raise RuntimeError(
                            "another Draft Lab background job is running"
                        )
                    job = _DraftJob(uuid4().hex, "benchmark")
                    job.progress = {"trial": 0, "trial_count": options["trials"]}
                    self._pending_terminal_job_id = None
                    self._jobs[job.job_id] = job
        Thread(
            target=self._run_benchmark, args=(job, model, corpus, options),
            name=f"draft-benchmark-{job.job_id[:8]}", daemon=True,
        ).start()
        return self._job_record(job)

    def job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._job_record(self._require_job(job_id))

    def cancel_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if job.status in {"queued", "running"}:
                job.cancel.set()
            return self._job_record(job)

    def job_result(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if job.status != "complete" or job.result is None:
                raise RuntimeError("Draft Lab job result is not ready")
            return job.result

    def create_assistant(self, payload: Mapping[str, object]) -> dict[str, object]:
        _exact_keys(
            "assistant", payload,
            {"model_id", "board_id", "user_drafter_number", "strategy"},
        )
        model = self._load_model(payload["model_id"])
        board = self._load_board(payload["board_id"])
        try:
            strategy = DraftStrategy(payload["strategy"])
        except (TypeError, ValueError):
            raise ValueError("assistant strategy is invalid") from None
        with self._activity_lock:
            with self._recommendation_lock:
                session = create_assistant_session(
                    model, board, user_drafter_number=payload["user_drafter_number"],
                    strategy=strategy,
                )
                with self._lock:
                    self._ensure_recommendations_available(session, model)
                result = assistant_status(session, model, board)
                with self._lock:
                    self._save_session(session)
                return result

    def assistant(self, session_id: str) -> dict[str, object]:
        with self._activity_lock:
            with self._recommendation_lock:
                with self._lock:
                    session, model, board = self._assistant_inputs(session_id)
                    self._ensure_recommendations_available(session, model)
                return assistant_status(session, model, board)

    def assistant_players(self, session_id: str) -> dict[str, object]:
        with self._lock:
            session, _, board = self._assistant_inputs(session_id)
            drafted = {row.player_id for row in session.picks}
        return {
            "players": [
                {
                    "player_id": row.player_id, "name": row.display_name,
                    "position": row.position, "drafted": row.player_id in drafted,
                }
                for row in sorted(
                    board.players,
                    key=lambda row: (row.position, row.display_name.casefold()),
                )
            ]
        }

    def record_pick(self, session_id: str, payload: Mapping[str, object]):
        _exact_keys("assistant pick", payload, {"player_id", "drafter_number"})
        with self._activity_lock:
            with self._recommendation_lock:
                with self._lock:
                    session, model, board = self._assistant_inputs(session_id)
                session = record_assistant_pick(
                    session, model, board, player_id=payload["player_id"],
                    drafter_number=payload["drafter_number"],
                )
                with self._lock:
                    self._ensure_recommendations_available(session, model)
                result = assistant_status(session, model, board)
                with self._lock:
                    self._save_session(session)
                return result

    def undo_pick(self, session_id: str):
        with self._activity_lock:
            with self._recommendation_lock:
                with self._lock:
                    session, model, board = self._assistant_inputs(session_id)
                session = undo_assistant_pick(session)
                with self._lock:
                    self._ensure_recommendations_available(session, model)
                result = assistant_status(session, model, board)
                with self._lock:
                    self._save_session(session)
                return result

    def sync_espn_draft(
        self, session_id: str, payload: Mapping[str, object]
    ) -> dict[str, object]:
        _exact_keys("ESPN public draft sync", payload, {"league_id", "season"})
        with self._lock:
            _, model, board = self._assistant_inputs(session_id)
        observation = self._espn_draft_adapter.poll(
            league_id=payload["league_id"],
            season=payload["season"],
            board=board,
            team_count=model.league_config.team_count,
            roster_size=model.league_config.roster_size,
        )
        if not isinstance(observation, EspnDraftObservation):
            raise EspnDraftSyncError("ESPN draft adapter returned an invalid observation")
        if (
            observation.league_id != payload["league_id"]
            or observation.season != payload["season"]
        ):
            raise EspnDraftSyncError(
                "ESPN draft adapter returned different source coordinates"
            )

        with self._activity_lock:
            with self._recommendation_lock:
                with self._lock:
                    current, model, board = self._assistant_inputs(session_id)
                try:
                    binding = AssistantDraftBinding(
                        "espn", observation.league_id, observation.season,
                        observation.team_order,
                    )
                    bound = bind_assistant_draft(current, binding)
                except ValueError as error:
                    raise EspnDraftSyncError(str(error)) from None
                if len(observation.assistant_picks) < len(current.picks):
                    raise EspnDraftSyncError(
                        "ESPN currently exposes fewer picks than this assistant session; "
                        "automatic rollback is intentionally disabled"
                    )
                updated = reconcile_assistant_picks(
                    bound, model, board, observation.assistant_picks
                )
                appended = len(updated.picks) - len(current.picks)
                with self._lock:
                    self._ensure_recommendations_available(updated, model)
                result = assistant_status(updated, model, board)
                result["live_sync"] = observation.live_sync_record(appended)
                if updated != current:
                    with self._lock:
                        self._save_session(updated)
                return result

    def model_path(self, model_id: str) -> Path:
        return self.store.model_path(model_id)

    def promote_checkpoint(self, checkpoint_job_id: str) -> dict[str, object]:
        """Make an autosaved champion usable without running another generation."""

        with self._lock:
            checkpoint = TrainingCheckpoint.from_record(
                self.store.load_checkpoint(checkpoint_job_id)
            )
            performance = checkpoint.champion_performance
            expected_metrics = {
                "fitness": performance.fitness,
                "championship_rate": (
                    performance.championships / performance.appearances
                ),
                "playoff_rate": performance.playoffs / performance.appearances,
                "mean_finish": performance.mean_finish,
            }
            expected_seasons = list(checkpoint.evolution_config.training_years)
            for summary in self.store.list_models():
                if (
                    summary.get("status") != "invalid"
                    and summary.get("brain_id") == checkpoint.champion.brain_id
                    and summary.get("corpus_id") == checkpoint.corpus_id
                    and summary.get("config_id") == checkpoint.league_config.config_id
                    and summary.get("trained_seasons") == expected_seasons
                    and summary.get("generation") == checkpoint.generation_completed
                    and summary.get("metrics") == expected_metrics
                ):
                    return summary
            artifact = _checkpoint_model_artifact(checkpoint)
            self.store.save_model(artifact)
            self._remember_asset(self._model_cache, artifact.model_id, artifact)
            return artifact.summary()

    def _run_corpus_install(self, job):
        try:
            self._set_job(job, status="running")
            receipt = self._corpus_installer.install(
                should_cancel=job.cancel.is_set,
                on_progress=lambda progress: self._set_job(
                    job, progress=dict(progress)
                ),
            )
            corpus_id = receipt["corpus_id"]
            with self._lock:
                self._corpus_cache.pop(corpus_id, None)
            self._set_job(
                job,
                status="complete",
                result={"install": receipt, "corpus": receipt["summary"]},
            )
        except CorpusInstallCancelled:
            self._set_job(
                job,
                status="cancelled",
                error="Starter corpus installation paused safely and can be resumed.",
            )
        except Exception as error:
            self._set_job(job, status="failed", error=str(error))

    def _run_training(self, job, corpus, config, evolution, resume):
        try:
            self._set_job(job, status="running")
            started_at = monotonic()
            initial_generation = 0 if resume is None else resume.generation_completed

            def arena_progress(generation, arena, arena_count):
                elapsed = monotonic() - started_at
                completed_here = (
                    (generation - 1 - initial_generation) * arena_count + arena
                )
                rate = elapsed / max(1, completed_here)
                total_remaining = (
                    evolution.generations * arena_count
                    - ((generation - 1) * arena_count + arena)
                )
                self._set_job(job, progress={
                    "generation": generation - 1,
                    "generation_count": evolution.generations,
                    "current_generation": generation,
                    "arena": arena,
                    "arena_count": arena_count,
                    "estimated_remaining_seconds": rate * total_remaining,
                    "autosaved": generation - 1 > 0,
                })

            def saved(checkpoint):
                self.store.save_checkpoint(job.job_id, checkpoint.to_record())
                summary = checkpoint.history[-1]
                completed_here = checkpoint.generation_completed - initial_generation
                elapsed = monotonic() - started_at
                per_generation = elapsed / max(1, completed_here)
                self._set_job(job, progress={
                    "generation": checkpoint.generation_completed,
                    "generation_count": evolution.generations,
                    "champion_brain_id": checkpoint.champion.brain_id,
                    "champion_fitness": checkpoint.champion_performance.fitness,
                    "mean_fitness": summary.mean_fitness,
                    "autosaved": True,
                    "elapsed_seconds": elapsed,
                    "seconds_per_generation": per_generation,
                    "estimated_remaining_seconds": per_generation * (
                        evolution.generations - checkpoint.generation_completed
                    ),
                })

            checkpoint = run_training_batch(
                corpus, config, evolution, resume=resume, on_generation=saved,
                on_arena=arena_progress,
                should_cancel=job.cancel.is_set,
            )
            artifact = _checkpoint_model_artifact(checkpoint)
            self.store.save_model(artifact)
            self._remember_asset(self._model_cache, artifact.model_id, artifact)
            self._set_job(job, status="complete", result={
                "model": artifact.summary(),
                "showcase": checkpoint.to_record()["showcase"],
                "history": [row.to_record() for row in checkpoint.history],
                "checkpoint_id": checkpoint.checkpoint_id,
            })
        except InterruptedError:
            self._set_job(job, status="cancelled", error="Stopped safely after the last autosaved generation.")
        except Exception as error:
            self._set_job(job, status="failed", error=str(error))

    def _run_benchmark(self, job, model, corpus, options):
        try:
            self._set_job(job, status="running")
            result = compare_to_regression_baseline(
                model.brain, corpus, model.league_config, **options,
                should_cancel=job.cancel.is_set,
                on_progress=lambda done, total: self._set_job(
                    job, progress={"trial": done, "trial_count": total}
                ),
            )
            record = result.to_record()
            evaluated = set(result.evaluation_seasons)
            trained = set(model.trained_seasons)
            record["evaluation_scope"] = (
                "holdout" if evaluated.isdisjoint(trained)
                else "mixed" if evaluated.difference(trained)
                else "training_years"
            )
            record["scope_notice"] = (
                "This is an out-of-sample historical check."
                if record["evaluation_scope"] == "holdout"
                else "At least one selected season was used to fit or evolve this model; "
                "treat the result as an in-sample regression check."
            )
            self._set_job(job, status="complete", result=record)
        except InterruptedError:
            self._set_job(job, status="cancelled", error="Benchmark stopped safely.")
        except Exception as error:
            self._set_job(job, status="failed", error=str(error))

    def _training_inputs(self, payload):
        allowed = {"corpus_id", "league_config", "evolution_config", "resume_checkpoint_job_id"}
        if not isinstance(payload, Mapping) or not set(payload).issubset(allowed) or not {
            "corpus_id", "league_config", "evolution_config"
        }.issubset(payload):
            raise ValueError("training request fields are invalid")
        corpus = self._load_corpus(payload["corpus_id"])
        config = _league_config(payload["league_config"])
        evolution = _evolution_config(payload["evolution_config"])
        resume_id = payload.get("resume_checkpoint_job_id")
        resume = None if resume_id is None else TrainingCheckpoint.from_record(
            self.store.load_checkpoint(resume_id)
        )
        return corpus, config, evolution, resume

    def _league_presets(self):
        standard = DraftLeagueConfig.standard_ppr()
        records = [{
            "preset_id": "standard-ppr",
            "source": "built_in",
            "config": standard.to_record(),
            "compatibility_notice": (
                "All displayed built-in rules are represented directly."
            ),
        }]
        for path in sorted(self.bundle_directory.glob("*.json")):
            records.append(self._league_preset(path))
        return records

    def _league_preset(self, path):
        try:
            metadata = path.stat()
        except OSError as error:
            return self._unsupported_league_preset(path, error)
        signature = metadata.st_mtime_ns, metadata.st_size
        cache_key = path.name
        with self._lock:
            cached = self._league_preset_cache.get(cache_key)
            if cached is not None and cached[0] == signature:
                self._league_preset_cache.move_to_end(cache_key)
                return deepcopy(cached[1])
        try:
            bundle = load_engine_bundle(path)
            config = config_from_engine_bundle(bundle)
            record = {
                "preset_id": bundle.bundle_id,
                "source": "synced_league",
                "season": bundle.state.season,
                "week": bundle.state.first_remaining_week,
                "config": config.to_record(),
                "compatibility_notice": (
                    "Team count, roster size, starter slots, regular-season end, "
                    "playoff field, weeks, and linear scoring were imported. "
                    "Review division berths, standings tiebreakers, and playoff "
                    "reseeding; Draft Lab v1 does not model those three host rules."
                ),
            }
        except ValueError as error:
            record = self._unsupported_league_preset(path, error)
        with self._lock:
            self._league_preset_cache[cache_key] = signature, record
            self._league_preset_cache.move_to_end(cache_key)
            while len(self._league_preset_cache) > _MAX_CACHED_LEAGUE_PRESETS:
                self._league_preset_cache.popitem(last=False)
        return deepcopy(record)

    @staticmethod
    def _unsupported_league_preset(path, error):
        return {
            "preset_id": path.stem,
            "source": "synced_league",
            "status": "unsupported",
            "compatibility_notice": (
                f"This synced league needs manual review: {error}"
            ),
        }

    def _assistant_inputs(self, session_id):
        session = self._load_session(session_id)
        return session, self._load_model(session.model_id), self._load_board(session.board_id)

    def _session_catalog(self):
        records = []
        coverages = {}
        for path in sorted(self.session_directory.glob("*.json")):
            try:
                session = self._load_session(path.stem)
                model = self._load_model(session.model_id)
                board = self._load_board(session.board_id)
                coverage_key = (
                    session.model_id,
                    session.board_id,
                    session.user_drafter_number,
                    session.strategy,
                )
                coverage = coverages.get(coverage_key)
                if coverage is None:
                    coverage = assistant_board_coverage(
                        model,
                        board,
                        user_drafter_number=session.user_drafter_number,
                        strategy=session.strategy,
                    )
                    coverages[coverage_key] = coverage
                records.append({
                    "session_id": session.session_id,
                    "model_id": session.model_id,
                    "board_id": session.board_id,
                    "user_drafter_number": session.user_drafter_number,
                    "strategy": session.strategy.value,
                    "pick_count": len(session.picks),
                    "board_coverage": deepcopy(coverage),
                    "draft_binding": (
                        None if session.draft_binding is None
                        else session.draft_binding.to_record()
                    ),
                })
            except (OSError, ValueError):
                records.append({"status": "invalid", "file": path.name})
        return tuple(records)

    def _session_path(self, session_id):
        if not isinstance(session_id, str) or len(session_id) != 32 or any(
            char not in "0123456789abcdef" for char in session_id
        ):
            raise ValueError("assistant session_id is invalid")
        return self.session_directory / f"{session_id}.json"

    def _save_session(self, session):
        path = self._session_path(session.session_id)
        temporary = path.with_name(f".{path.stem}.{uuid4().hex}.tmp.json")
        try:
            temporary.write_text(json.dumps(session.to_record(), sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_session(self, session_id):
        path = self._session_path(session_id)
        try:
            if path.stat().st_size > _MAX_ASSISTANT_SESSION_BYTES:
                raise ValueError("assistant session exceeds its size limit")
            return DraftAssistantSession.from_record(json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant {value!r}")
                ),
            ))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"could not read assistant session: {error}") from None

    def _load_corpus(self, corpus_id):
        return self._load_asset(self._corpus_cache, corpus_id, self.store.load_corpus)

    def _load_model(self, model_id):
        return self._load_asset(self._model_cache, model_id, self.store.load_model)

    def _load_board(self, board_id):
        return self._load_asset(self._board_cache, board_id, self.store.load_board)

    def _load_asset(self, cache, identifier, loader):
        if not isinstance(identifier, str):
            return loader(identifier)
        with self._lock:
            cached = cache.get(identifier)
            if cached is not None:
                cache.move_to_end(identifier)
                return cached
        return self._remember_asset(cache, identifier, loader(identifier))

    def _remember_asset(self, cache, identifier, value):
        with self._lock:
            cache[identifier] = value
            cache.move_to_end(identifier)
            while len(cache) > _MAX_CACHED_ASSETS_PER_KIND:
                cache.popitem(last=False)
        return value

    def _ensure_recommendations_available(self, session, model):
        total_picks = model.league_config.team_count * model.league_config.roster_size
        next_pick = len(session.picks) + 1
        recommendations_due = (
            next_pick <= total_picks
            and drafter_for_pick(
                next_pick, model.league_config.team_count
            ) == session.user_drafter_number
        )
        if recommendations_due:
            self._heavy_work_guard()
            if self.is_busy:
                raise RuntimeError(
                    "Draft assistant recommendations are unavailable while a Draft "
                    "Lab background job is running"
                )

    def _require_job(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32:
            raise ValueError("Draft Lab job ID is invalid")
        try:
            return self._jobs[job_id]
        except KeyError:
            raise FileNotFoundError(job_id) from None

    def _set_job(self, job, **changes):
        with self._lock:
            was_terminal = job.status in TERMINAL_JOB_STATUSES
            for name, value in changes.items():
                setattr(job, name, value)
            if job.status in TERMINAL_JOB_STATUSES:
                if not was_terminal:
                    self._pending_terminal_job_id = job.job_id
                prune_terminal_jobs(self._jobs, _MAX_RETAINED_TERMINAL_JOBS)

    @staticmethod
    def _job_record(job):
        return {
            "job_id": job.job_id, "kind": job.kind, "status": job.status,
            "progress": dict(job.progress), "error": job.error,
            "result_ready": job.result is not None,
        }


def _checkpoint_model_artifact(checkpoint: TrainingCheckpoint) -> DraftModelArtifact:
    performance = checkpoint.champion_performance
    return DraftModelArtifact(
        checkpoint.champion,
        checkpoint.league_config,
        checkpoint.corpus_id,
        checkpoint.evolution_config.training_years,
        checkpoint.generation_completed,
        {
            "fitness": performance.fitness,
            "championship_rate": performance.championships / performance.appearances,
            "playoff_rate": performance.playoffs / performance.appearances,
            "mean_finish": performance.mean_finish,
        },
        datetime.now(timezone.utc).isoformat(),
    )


def _league_config(value):
    if not isinstance(value, Mapping):
        raise ValueError("league_config must be an object")
    if value.get("kind") == "draft_league_config":
        return DraftLeagueConfig.from_record(value)
    keys = {
        "name", "team_count", "starting_slots", "bench_slots", "slot_eligibility",
        "position_limits", "scoring_weights", "regular_season_weeks",
        "playoff_team_count", "playoff_weeks", "strategy_counts",
    }
    _exact_keys("league_config", value, keys)
    try:
        strategies = {DraftStrategy(key): count for key, count in value["strategy_counts"].items()}
        return DraftLeagueConfig(
            value["name"], value["team_count"], tuple(value["starting_slots"]),
            value["bench_slots"], {key: tuple(rows) for key, rows in value["slot_eligibility"].items()},
            value["position_limits"], value["scoring_weights"],
            tuple(value["regular_season_weeks"]), value["playoff_team_count"],
            tuple(value["playoff_weeks"]), strategies,
        )
    except (AttributeError, TypeError):
        raise ValueError("league_config nested fields are invalid") from None


def _evolution_config(value):
    if not isinstance(value, Mapping):
        raise ValueError("evolution_config must be an object")
    if value.get("kind") == "draft_evolution_config":
        return EvolutionConfig.from_record(value)
    keys = {
        "population_size", "generations", "appearances_per_generation",
        "elite_fraction", "mutation_rate", "mutation_magnitude", "candidate_window",
        "training_years", "seed",
    }
    _exact_keys("evolution_config", value, keys)
    try:
        return EvolutionConfig(
            *(value[name] for name in (
                "population_size", "generations", "appearances_per_generation",
                "elite_fraction", "mutation_rate", "mutation_magnitude", "candidate_window",
            )), tuple(value["training_years"]), value["seed"],
        )
    except TypeError:
        raise ValueError("evolution_config nested fields are invalid") from None


def _exact_keys(name, value, keys):
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} request fields are invalid")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


__all__ = ("DraftLabService",)
