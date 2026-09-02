"""Thread-safe application service behind the localhost user interface."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from ._scenario_random import SAFE_INTEGER, content_id
from ._app_support import (
    BUNDLE_ID_PATTERN,
    boolean,
    bundle_summary,
    search_result_record,
    string_array,
    workbook_sources,
)
from .engine_bundle import EngineBundle, load_engine_bundle, save_engine_bundle
from .league_search import LeagueSearchOutcome, LeagueSearchProgress, ResumableLeagueTradeSearch
from .scenario_config import CorrelatedScenarioConfig
from .search_runner import TradeSearchSettings
from .surrogate_disclosure import SURROGATE_QUALITY_GATE
from .trade_filters import TradeFilterMode, TradePackageFilter
from .trade_impact import PreparedSeasonBaseline, prepare_season_baseline
from .trade_space import TeamRoster, TradeConstraints, TradeSpace
from .weekly_collection import (
    WeeklyCollectionJobs,
    WeeklyCollectionRequest,
    WeeklyCollectionWorkflow,
)
from .workbook_model import (
    TradeWorkbookContext,
    team_outlook_rows,
    workbook_trade_rows,
)
from .xlsx_export import export_trade_workbook


@dataclass(frozen=True, slots=True)
class LocalSearchRequest:
    bundle_id: str
    primary_team_id: str
    counterparty_team_ids: tuple[str, ...]
    constraints: TradeConstraints
    settings: TradeSearchSettings
    scenario_count: int
    seed: int
    allow_surrogate_power: bool = False
    request_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("bundle_id", "primary_team_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        counterparties = tuple(self.counterparty_team_ids)
        if any(not isinstance(value, str) or not value for value in counterparties):
            raise ValueError("counterparty_team_ids must contain non-empty strings")
        if len(set(counterparties)) != len(counterparties):
            raise ValueError("counterparty_team_ids contains a duplicate")
        if not isinstance(self.constraints, TradeConstraints):
            raise ValueError("constraints must be TradeConstraints")
        if not isinstance(self.settings, TradeSearchSettings):
            raise ValueError("settings must be TradeSearchSettings")
        if type(self.scenario_count) is not int or not 1 <= self.scenario_count <= 1_000_000:
            raise ValueError("scenario_count must be between 1 and 1,000,000")
        if type(self.seed) is not int or not -SAFE_INTEGER <= self.seed <= SAFE_INTEGER:
            raise ValueError("seed is outside the portable integer range")
        if not isinstance(self.allow_surrogate_power, bool):
            raise ValueError("allow_surrogate_power must be a boolean")
        object.__setattr__(self, "counterparty_team_ids", counterparties)
        object.__setattr__(self, "request_id", content_id("app-search", self.to_record()))

    def to_record(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "counterparty_team_ids": list(self.counterparty_team_ids),
            "primary_team_id": self.primary_team_id,
            "scenario_count": self.scenario_count,
            "seed": self.seed,
            "allow_surrogate_power": self.allow_surrogate_power,
            "settings": self.settings.to_record(),
            "trade_constraints": self.constraints.to_record(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "LocalSearchRequest":
        keys = {
            "bundle_id",
            "primary_team_id",
            "counterparty_team_ids",
            "min_outgoing",
            "max_outgoing",
            "min_incoming",
            "max_incoming",
            "max_total_players",
            "max_imbalance",
            "balanced_only",
            "skip_fantasypros_small_trades",
            "locked_player_ids",
            "require_no_drops",
            "minimum_power_delta",
            "checkpoint_interval",
            "scenario_count",
            "seed",
        }
        if (
            not isinstance(payload, Mapping)
            or not keys <= set(payload)
            or not set(payload) <= keys | {
                "allow_surrogate_power",
                "outgoing_filter",
                "incoming_filter",
            }
        ):
            raise ValueError("search request fields are invalid")
        counterparties = string_array("counterparty_team_ids", payload["counterparty_team_ids"])
        locked = string_array("locked_player_ids", payload["locked_player_ids"])
        skip_small = boolean(
            "skip_fantasypros_small_trades",
            payload["skip_fantasypros_small_trades"],
        )
        excluded = (
            frozenset((left, right) for left in range(1, 4) for right in range(1, 4))
            if skip_small
            else frozenset()
        )
        return cls(
            bundle_id=payload["bundle_id"],
            primary_team_id=payload["primary_team_id"],
            counterparty_team_ids=counterparties,
            constraints=TradeConstraints(
                min_outgoing=payload["min_outgoing"],
                max_outgoing=payload["max_outgoing"],
                min_incoming=payload["min_incoming"],
                max_incoming=payload["max_incoming"],
                max_total_players=payload["max_total_players"],
                max_imbalance=payload["max_imbalance"],
                balanced_only=boolean("balanced_only", payload["balanced_only"]),
                excluded_size_pairs=excluded,
                locked_player_ids=frozenset(locked),
                require_no_drops=boolean("require_no_drops", payload["require_no_drops"]),
                outgoing_filter=_package_filter(
                    "outgoing_filter", payload.get("outgoing_filter")
                ),
                incoming_filter=_package_filter(
                    "incoming_filter", payload.get("incoming_filter")
                ),
            ),
            settings=TradeSearchSettings(
                payload["minimum_power_delta"], payload["checkpoint_interval"]
            ),
            scenario_count=payload["scenario_count"],
            seed=payload["seed"],
            allow_surrogate_power=boolean(
                "allow_surrogate_power", payload.get("allow_surrogate_power", False)
            ),
        )


@dataclass(slots=True)
class _SearchJob:
    job_id: str
    request: LocalSearchRequest
    status: str = "queued"
    progress: LeagueSearchProgress | None = None
    error: str | None = None
    outcome: LeagueSearchOutcome | None = None
    baseline: PreparedSeasonBaseline | None = None
    cancel: Event = field(default_factory=Event)


class LocalAppService:
    def __init__(
        self,
        data_directory: str | Path,
        *,
        weekly_collection_workflow: WeeklyCollectionWorkflow | None = None,
    ) -> None:
        self.data_directory = Path(data_directory).resolve()
        self.bundle_directory = self.data_directory / "bundles"
        self.search_directory = self.data_directory / "searches"
        self.export_directory = self.data_directory / "exports"
        for directory in (
            self.bundle_directory,
            self.search_directory,
            self.export_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._jobs: dict[str, _SearchJob] = {}
        self._collections = WeeklyCollectionJobs(
            self.data_directory,
            self.bundle_directory,
            weekly_collection_workflow,
        )

    def import_bundle(self, record: Mapping[str, object]) -> dict[str, object]:
        bundle = EngineBundle.from_record(record)
        path = self.bundle_directory / f"{bundle.bundle_id}.json"
        save_engine_bundle(bundle, path)
        return bundle_summary(bundle)

    def list_bundles(self) -> tuple[dict[str, object], ...]:
        summaries = []
        for path in sorted(self.bundle_directory.glob("*.json")):
            try:
                summaries.append(bundle_summary(load_engine_bundle(path)))
            except ValueError as error:
                summaries.append({"file": path.name, "status": "invalid", "error": str(error)})
        return tuple(summaries)

    def bundle_readiness(self) -> dict[str, object]:
        return self._bundle_readiness(self.list_bundles())

    def bundle_catalog(self) -> dict[str, object]:
        bundles = self.list_bundles()
        return {"bundles": bundles, "readiness": self._bundle_readiness(bundles)}

    def _bundle_readiness(self, bundles) -> dict[str, object]:
        ready_count = sum(row.get("status") == "ready" for row in bundles)
        exact_count = sum(
            row.get("status") == "ready" and row.get("power_engine_mode") == "exact"
            for row in bundles
        )
        surrogate_count = sum(
            row.get("status") == "ready"
            and row.get("power_engine_mode") == "surrogate"
            for row in bundles
        )
        invalid_count = len(bundles) - ready_count
        if ready_count:
            message = (
                f"{exact_count} exact-method and {surrogate_count} SURROGATE weekly "
                "engine(s) are ready. Surrogate use requires explicit acceptance."
            )
        elif invalid_count:
            message = (
                "Saved weekly data failed validation. Collect this week again or import "
                "a complete bundle."
            )
        elif self._collections.available:
            message = "Not ready yet. Collect this week or import a complete bundle."
        else:
            message = (
                "Not ready yet. Weekly collection is unavailable in this build; "
                "import a complete bundle."
            )
        return {
            "ready": ready_count > 0,
            "ready_bundle_count": ready_count,
            "exact_bundle_count": exact_count,
            "surrogate_bundle_count": surrogate_count,
            "invalid_bundle_count": invalid_count,
            "collection_available": self._collections.available,
            "message": message,
        }

    def start_weekly_collection(
        self, request: WeeklyCollectionRequest
    ) -> dict[str, object]:
        with self._lock:
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise RuntimeError("stop the local trade search before collecting a new week")
            return self._collections.start(request)

    def weekly_collection(self, job_id: str) -> dict[str, object]:
        return self._collections.job(job_id)

    def cancel_weekly_collection(self, job_id: str) -> dict[str, object]:
        return self._collections.cancel(job_id)

    def confirm_weekly_collection_sign_in(self, job_id: str) -> dict[str, object]:
        return self._collections.confirm_sign_in(job_id)

    def start_search(self, request: LocalSearchRequest) -> dict[str, object]:
        if not isinstance(request, LocalSearchRequest):
            raise ValueError("request must be a LocalSearchRequest")
        bundle = load_engine_bundle(self._bundle_path(request.bundle_id))
        _require_surrogate_consent(bundle, request)
        _search_scope(bundle, request)
        with self._lock:
            if self._collections.is_running:
                raise RuntimeError("weekly collection must finish before a trade search starts")
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise RuntimeError("another search is already running")
            job = _SearchJob(uuid4().hex, request)
            self._jobs[job.job_id] = job
            Thread(target=self._run_search, args=(job,), daemon=True).start()
            return self._job_record(job)

    def estimate_search(self, request: LocalSearchRequest) -> dict[str, object]:
        if not isinstance(request, LocalSearchRequest):
            raise ValueError("request must be a LocalSearchRequest")
        bundle = load_engine_bundle(self._bundle_path(request.bundle_id))
        _require_surrogate_consent(bundle, request)
        by_team, primary, selected = _search_scope(bundle, request)
        eligible_positions = {
            player_id: player.eligible_positions
            for player_id, player in bundle.strength_model.players.items()
        }
        pairs = tuple(
            {
                "counterparty_team_id": team_id,
                "candidate_count": TradeSpace(
                    primary,
                    by_team[team_id],
                    request.constraints,
                    eligible_positions_by_player=eligible_positions,
                ).candidate_count,
            }
            for team_id in selected
        )
        return {
            "pair_count": len(pairs),
            "candidate_count": sum(row["candidate_count"] for row in pairs),
            "pairs": pairs,
        }

    def job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._job_record(self._require_job(job_id))

    def cancel_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if job.status in {"queued", "running"}:
                job.cancel.set()
            return self._job_record(job)

    def job_results(self, job_id: str, *, limit: int = 500) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("result limit must be between 1 and 500")
        with self._lock:
            job = self._require_job(job_id)
            if (
                job.status != "complete"
                or job.outcome is None
                or job.baseline is None
            ):
                raise RuntimeError("search results are not ready")
            request, outcome, baseline = job.request, job.outcome, job.baseline
        bundle = load_engine_bundle(self._bundle_path(request.bundle_id))
        return search_result_record(outcome, bundle, baseline.season_projection, limit)

    def export_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if job.status != "complete" or job.outcome is None or job.baseline is None:
                raise RuntimeError("search must complete before it can be exported")
            request, outcome, baseline = job.request, job.outcome, job.baseline
        bundle = load_engine_bundle(self._bundle_path(request.bundle_id))
        team_names = {row.team_id: row.name for row in bundle.state.teams}
        rows = workbook_trade_rows(
            outcome,
            team_names,
            bundle.player_names,
            bundle.methodology_evidence,
        )
        methodology = bundle.methodology_evidence
        context = TradeWorkbookContext(
            snapshot_id=bundle.state.snapshot_id,
            strength_model_id=bundle.strength_model.model_id,
            scenario_run_id=baseline.scenarios.run_id,
            primary_team_id=request.primary_team_id,
            primary_team_name=team_names[request.primary_team_id],
            generated_at=datetime.now(timezone.utc),
            minimum_power_delta=request.settings.minimum_displayed_power_delta,
            scenario_count=request.scenario_count,
            power_engine_mode=bundle.methodology_mode,
            calibration_status=bundle.strength_model.calibration.status.value,
            methodology_evidence_kind=(
                "exact_attestation"
                if bundle.methodology_mode == "exact"
                else "surrogate_disclosure"
            ),
            methodology_record_id=(
                bundle.methodology_attestation.attestation_id
                if bundle.methodology_mode == "exact"
                else bundle.surrogate_disclosure.disclosure_id
            ),
            formula_id=methodology.formula_id,
            formula_source_fit_id=methodology.formula_source_fit_id,
            methodology_fingerprint_id=(
                methodology.methodology_fingerprint.fingerprint_id
            ),
            formula_action=methodology.formula_decision.action.value,
            methodology_current_evidence_id=methodology.current_evidence_id,
            methodology_quality_gate=(
                "exact_attestation_v1"
                if bundle.methodology_mode == "exact"
                else SURROGATE_QUALITY_GATE
            ),
            methodology_holdout_count=methodology.current_holdout_count,
            holdout_max_absolute_score_error=(
                methodology.calibration_diagnostics.max_absolute_score_error
            ),
            holdout_display_match_rate=(
                methodology.calibration_diagnostics.display_match_rate
            ),
            exact_balanced_package_sizes=(
                methodology.validated_balanced_package_sizes
            ),
            sources=workbook_sources(bundle),
        )
        filename = f"trade-results-{request.request_id}.xlsx"
        path = export_trade_workbook(
            self.export_directory / filename,
            context,
            rows,
            team_outlook_rows(bundle.state, baseline.season_projection),
        )
        return {"filename": path.name, "trade_count": len(rows)}

    def export_path(self, filename: str) -> Path:
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("invalid export filename")
        path = (self.export_directory / filename).resolve()
        if path.parent != self.export_directory or not path.is_file() or path.suffix != ".xlsx":
            raise FileNotFoundError(filename)
        return path

    def _run_search(self, job: _SearchJob) -> None:
        try:
            self._set_job(job, status="running")
            bundle = load_engine_bundle(self._bundle_path(job.request.bundle_id))
            _require_surrogate_consent(bundle, job.request)
            config = CorrelatedScenarioConfig(
                job.request.scenario_count,
                job.request.seed,
                bundle.scenario_config.loadings,
            )
            baseline = prepare_season_baseline(
                bundle.state,
                bundle.rosters,
                bundle.projections,
                bundle.eligibilities,
                config,
            )
            search = ResumableLeagueTradeSearch(
                bundle.rosters,
                job.request.primary_team_id,
                bundle.strength_model,
                baseline,
                job.request.constraints,
                job.request.settings,
                counterparty_team_ids=(job.request.counterparty_team_ids or None),
            )
            outcome = search.run(
                self.search_directory / job.request.request_id,
                on_progress=lambda progress: self._set_job(job, progress=progress),
                should_cancel=job.cancel.is_set,
            )
            status = "cancelled" if outcome.progress.cancelled else "complete"
            self._set_job(job, status=status, progress=outcome.progress, outcome=outcome, baseline=baseline)
        except Exception as error:
            self._set_job(job, status="failed", error=str(error))

    def _set_job(self, job, **changes) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)

    def _bundle_path(self, bundle_id: str) -> Path:
        if not isinstance(bundle_id, str) or not BUNDLE_ID_PATTERN.fullmatch(bundle_id):
            raise ValueError("bundle_id is invalid")
        path = self.bundle_directory / f"{bundle_id}.json"
        if not path.is_file():
            raise FileNotFoundError(bundle_id)
        return path

    def _require_job(self, job_id: str) -> _SearchJob:
        try:
            return self._jobs[job_id]
        except (KeyError, TypeError):
            raise KeyError("unknown search job") from None

    @staticmethod
    def _job_record(job: _SearchJob) -> dict[str, object]:
        progress = job.progress
        return {
            "job_id": job.job_id,
            "request_id": job.request.request_id,
            "status": job.status,
            "error": job.error,
            "progress": None
            if progress is None
            else {
                "pair_count": progress.pair_count,
                "completed_pair_count": progress.completed_pair_count,
                "current_counterparty_team_id": progress.current_counterparty_team_id,
                "examined_candidate_count": progress.examined_candidate_count,
                "total_candidate_count": progress.total_candidate_count,
                "qualified_trade_count": progress.qualified_trade_count,
                "mutual_playoff_gain_count": progress.mutual_playoff_gain_count,
                "completion_fraction": progress.completion_fraction,
                "cancelled": progress.cancelled,
            },
        }


def _require_surrogate_consent(
    bundle: EngineBundle, request: LocalSearchRequest
) -> None:
    if (
        bundle.methodology_mode == "surrogate"
        and not request.allow_surrogate_power
    ):
        raise ValueError(
            "This weekly engine is a SURROGATE approximation. Explicitly accept "
            "surrogate power before counting or running trades."
        )


def _package_filter(name: str, value: object) -> TradePackageFilter | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "player_ids",
        "player_mode",
        "positions",
        "position_mode",
    }:
        raise ValueError(f"{name} fields are invalid")
    player_ids = string_array(f"{name}.player_ids", value["player_ids"])
    positions = string_array(f"{name}.positions", value["positions"])
    if len(set(player_ids)) != len(player_ids):
        raise ValueError(f"{name}.player_ids contains a duplicate")
    if len(set(positions)) != len(positions):
        raise ValueError(f"{name}.positions contains a duplicate")
    for values_field, mode_field, selected, mode in (
        ("player_ids", "player_mode", player_ids, value["player_mode"]),
        ("positions", "position_mode", positions, value["position_mode"]),
    ):
        if bool(selected) != (mode is not None):
            raise ValueError(
                f"{name}.{mode_field} must be set exactly when "
                f"{name}.{values_field} has selections"
            )
    package_filter = TradePackageFilter(
        frozenset(player_ids),
        value["player_mode"],
        frozenset(positions),
        value["position_mode"],
    )
    return package_filter if package_filter.active else None


def _search_scope(
    bundle: EngineBundle, request: LocalSearchRequest
) -> tuple[dict[str, TeamRoster], TeamRoster, tuple[str, ...]]:
    by_team = {row.team_id: row for row in bundle.rosters}
    try:
        primary = by_team[request.primary_team_id]
    except KeyError:
        raise ValueError("primary_team_id is not present in the weekly bundle") from None
    selected = request.counterparty_team_ids or tuple(
        team.team_id
        for team in bundle.state.teams
        if team.team_id != request.primary_team_id
    )
    if request.primary_team_id in selected or not set(selected).issubset(by_team):
        raise ValueError("counterparty selection contains an invalid team")

    outgoing_filter = request.constraints.outgoing_filter
    if outgoing_filter is not None:
        invalid = outgoing_filter.player_ids.difference(primary.player_ids)
        if invalid:
            raise ValueError(
                "players you give must belong to the selected primary team"
            )

    incoming_filter = request.constraints.incoming_filter
    if incoming_filter is not None and incoming_filter.player_ids:
        owner_by_player = {
            player_id: team_id
            for team_id in selected
            for player_id in by_team[team_id].player_ids
        }
        invalid = incoming_filter.player_ids.difference(owner_by_player)
        if invalid:
            raise ValueError(
                "players you receive must belong to a selected other team"
            )
        if incoming_filter.player_mode in {
            TradeFilterMode.INCLUDE,
            TradeFilterMode.ONLY,
        } and len(
            {owner_by_player[player_id] for player_id in incoming_filter.player_ids}
        ) > 1:
            raise ValueError(
                "Players that must appear together need to be on the same other team."
            )
    return by_team, primary, selected
