"""Thread-safe application service behind the localhost user interface."""

from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from uuid import uuid4

from ._app_support import (
    BUNDLE_ID_PATTERN,
    boolean,
    bundle_summary,
    search_result_record,
    string_array,
    three_way_search_result_record,
    workbook_sources,
)
from ._scenario_random import SAFE_INTEGER, content_id
from .dashboard import build_league_dashboard
from .draft_service import DraftLabService
from .engine_bundle import EngineBundle, load_engine_bundle, save_engine_bundle
from .gm_insights import build_gm_insights
from .job_retention import (
    ACTIVE_JOB_STATUSES,
    DEFAULT_TERMINAL_JOB_LIMIT,
    TERMINAL_JOB_STATUSES,
    has_active_jobs,
    prune_terminal_jobs,
)
from .league_history import LeagueHistoryStore
from .league_search import (
    LeagueSearchOutcome,
    LeagueSearchProgress,
    ResumableLeagueTradeSearch,
)
from .player_outlook import build_player_outlook
from .roster_adjustment import (
    MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY,
    PreparedRosterAdjuster,
)
from .scenario_config import CorrelatedScenarioConfig
from .search_runner import TradeSearchSettings
from .season import SeasonProjection
from .surrogate_disclosure import SURROGATE_QUALITY_GATE
from .three_way_search import (
    PreparedThreeWayTrade,
    ResumableThreeWayTradeSearch,
    ThreeWaySearchOutcome,
    ThreeWaySearchProgress,
)
from .three_way_trade import ThreeWayTradeSpace
from .three_way_workbook import ThreeWayExportProvenance, three_way_workbook_rows
from .three_way_xlsx import (
    export_three_way_trade_workbook,
    require_three_way_exportable_count,
)
from .trade_filters import (
    TradeFilterExpression,
    TradeFilterMode,
    TradePackageFilter,
    iter_trade_filter_leaves,
    parse_trade_filter,
)
from .trade_impact import PreparedSeasonBaseline, prepare_season_baseline
from .trade_timing import build_trade_timing
from .trade_space import TeamRoster, TradeConstraints, TradeSpace
from .weekly_collection import (
    LEAGUE_HISTORY_FILENAME,
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

_MAX_DASHBOARD_SCENARIOS = 10_000
_MAX_BUNDLE_CACHE_SIZE = 4
_MAX_DASHBOARD_CACHE_SIZE = 4
_MAX_BASELINE_CACHE_SIZE = 1
_MAX_PLAYER_OUTLOOK_CACHE_SIZE = 4
_MAX_RETAINED_SEARCH_JOBS = DEFAULT_TERMINAL_JOB_LIMIT
_MAX_GM_INSIGHTS_CACHE_SIZE = 4
_MAX_TRADE_TIMING_CACHE_SIZE = 6


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
    trade_format: str = "two_team"
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
        if not isinstance(self.trade_format, str) or self.trade_format not in {
            "two_team",
            "three_team",
        }:
            raise ValueError("trade_format must be two_team or three_team")
        if self.trade_format == "three_team":
            if len(counterparties) != 2:
                raise ValueError("three-team trades require exactly two partner teams")
            counterparties = tuple(sorted(counterparties))
        object.__setattr__(self, "counterparty_team_ids", counterparties)
        object.__setattr__(self, "request_id", content_id("app-search", self.to_record()))

    def to_record(self) -> dict[str, object]:
        record = {
            "bundle_id": self.bundle_id,
            "counterparty_team_ids": list(self.counterparty_team_ids),
            "primary_team_id": self.primary_team_id,
            "scenario_count": self.scenario_count,
            "seed": self.seed,
            "allow_surrogate_power": self.allow_surrogate_power,
            "settings": self.settings.to_record(),
            "trade_constraints": self.constraints.to_record(),
        }
        if self.trade_format == "three_team":
            record["trade_format"] = self.trade_format
        return record

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
        optional_keys = {
            "allow_surrogate_power",
            "outgoing_filter",
            "incoming_filter",
            "outgoing_filter_expression",
            "incoming_filter_expression",
            "trade_format",
        }
        if (
            not isinstance(payload, Mapping)
            or not keys <= set(payload)
            or not set(payload) <= keys | optional_keys
        ):
            raise ValueError("search request fields are invalid")
        trade_format = payload.get("trade_format", "two_team")
        if not isinstance(trade_format, str) or trade_format not in {
            "two_team",
            "three_team",
        }:
            raise ValueError("trade_format must be two_team or three_team")
        counterparties = string_array("counterparty_team_ids", payload["counterparty_team_ids"])
        locked = string_array("locked_player_ids", payload["locked_player_ids"])
        skip_small = boolean(
            "skip_fantasypros_small_trades",
            payload["skip_fantasypros_small_trades"],
        )
        if trade_format == "three_team" and skip_small:
            raise ValueError(
                "skip_fantasypros_small_trades must be false for three-team trades"
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
                outgoing_filter=_payload_filter(payload, "outgoing"),
                incoming_filter=_payload_filter(payload, "incoming"),
            ),
            settings=TradeSearchSettings(
                payload["minimum_power_delta"], payload["checkpoint_interval"]
            ),
            scenario_count=payload["scenario_count"],
            seed=payload["seed"],
            trade_format=trade_format,
            allow_surrogate_power=boolean(
                "allow_surrogate_power", payload.get("allow_surrogate_power", False)
            ),
        )


@dataclass(slots=True)
class _SearchJob:
    job_id: str
    request: LocalSearchRequest
    status: str = "queued"
    progress: LeagueSearchProgress | ThreeWaySearchProgress | None = None
    error: str | None = None
    outcome: LeagueSearchOutcome | ThreeWaySearchOutcome | None = None
    context: "_CompletedSearchContext | None" = None
    cancel: Event = field(default_factory=Event)


@dataclass(frozen=True, slots=True)
class _CompletedSearchContext:
    season_projection: SeasonProjection
    scenario_run_id: str


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
        self._baseline_build_lock = Lock()
        self._jobs: dict[str, _SearchJob] = {}
        self._bundle_cache: OrderedDict[str, EngineBundle] = OrderedDict()
        self._dashboard_cache: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._dashboard_futures: dict[str, Future[dict[str, object]]] = {}
        self._baseline_cache: OrderedDict[
            tuple[str, str], PreparedSeasonBaseline
        ] = OrderedDict()
        self._baseline_futures: dict[
            tuple[str, str], Future[PreparedSeasonBaseline]
        ] = {}
        self._pending_terminal_search_id: str | None = None
        self._export_in_progress = False
        self._player_outlook_cache: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._player_outlook_futures: dict[str, Future[dict[str, object]]] = {}
        self._gm_insights_cache: OrderedDict[
            tuple[str, str | None], dict[str, object]
        ] = OrderedDict()
        self._gm_insights_futures: dict[str, Future[dict[str, object]]] = {}
        self._trade_timing_cache: OrderedDict[
            tuple[str, str | None, str], dict[str, object]
        ] = OrderedDict()
        self._trade_timing_futures: dict[
            tuple[str, str], Future[dict[str, object]]
        ] = {}
        self._history_store: LeagueHistoryStore | None = None
        self._collections = WeeklyCollectionJobs(
            self.data_directory,
            self.bundle_directory,
            weekly_collection_workflow,
        )
        self.draft_lab = DraftLabService(
            self.data_directory,
            self.bundle_directory,
            heavy_work_guard=self._require_draft_capacity,
            activity_lock=self._lock,
        )

    @property
    def is_busy(self) -> bool:
        """Whether background work should keep the local process alive."""

        with self._lock:
            return bool(
                self._export_in_progress
                or self._dashboard_futures
                or self._player_outlook_futures
                or self._gm_insights_futures
                or self._trade_timing_futures
                or self._baseline_futures
                or self._collections.is_running
                or has_active_jobs(self._jobs)
                or self.draft_lab.is_busy
            )

    def active_search_job(self) -> dict[str, object] | None:
        """Return the sole resumable trade-search job, if one exists."""

        with self._lock:
            active = tuple(
                job
                for job in self._jobs.values()
                if job.status in ACTIVE_JOB_STATUSES
            )
            if len(active) > 1:
                raise RuntimeError("multiple trade-search jobs are active")
            return self._job_record(active[0]) if active else None

    def active_job_catalog(self) -> dict[str, object]:
        """Return current work or the latest unsurfaced terminal search."""

        with self._lock:
            search = self.active_search_job()
            if search is None and self._pending_terminal_search_id is not None:
                pending = self._jobs.get(self._pending_terminal_search_id)
                if pending is not None and pending.status in TERMINAL_JOB_STATUSES:
                    search = self._job_record(pending)
                else:
                    self._pending_terminal_search_id = None
            return {
                "search": search,
                "weekly_collection": self._collections.recoverable_job(),
                "draft": self.draft_lab.recoverable_job(),
            }

    def acknowledge_search_activity(self, job_id: str) -> dict[str, object]:
        """Mark one terminal search as surfaced without clearing a newer result."""

        with self._lock:
            job = self._require_job(job_id)
            if job.status not in TERMINAL_JOB_STATUSES:
                raise RuntimeError("active search activity cannot be acknowledged")
            acknowledged = self._pending_terminal_search_id == job_id
            if acknowledged:
                self._pending_terminal_search_id = None
            return {"job_id": job_id, "acknowledged": acknowledged}

    def _require_draft_capacity(self) -> None:
        with self._lock:
            if (
                self._collections.is_running
                or has_active_jobs(self._jobs)
                or self._dashboard_futures
                or self._player_outlook_futures
                or self._gm_insights_futures
                or self._trade_timing_futures
                or self._export_in_progress
            ):
                raise RuntimeError(
                    "weekly collection, trade search, export, league dashboard, Player Lab, General Manager Insights, or Trade Timing must finish before Draft Lab starts"
                )

    def import_bundle(self, record: Mapping[str, object]) -> dict[str, object]:
        bundle = EngineBundle.from_record(record)
        path = self.bundle_directory / f"{bundle.bundle_id}.json"
        save_engine_bundle(bundle, path)
        self._remember_bundle(bundle)
        return bundle_summary(bundle)

    def list_bundles(self) -> tuple[dict[str, object], ...]:
        summaries = []
        for path in sorted(self.bundle_directory.glob("*.json")):
            try:
                bundle = (
                    self._load_bundle(path.stem)
                    if BUNDLE_ID_PATTERN.fullmatch(path.stem)
                    else load_engine_bundle(path)
                )
                summaries.append(bundle_summary(bundle))
            except ValueError as error:
                summaries.append({"file": path.name, "status": "invalid", "error": str(error)})
        return tuple(summaries)

    def bundle_readiness(self) -> dict[str, object]:
        return self._bundle_readiness(self.list_bundles())

    def bundle_catalog(self) -> dict[str, object]:
        bundles = self.list_bundles()
        return {"bundles": bundles, "readiness": self._bundle_readiness(bundles)}

    def league_dashboard(self, bundle_id: str) -> dict[str, object]:
        """Return one deterministic league outlook, cached by immutable bundle ID."""

        with self._lock:
            cached = self._dashboard_cache.get(bundle_id)
            if cached is not None:
                self._dashboard_cache.move_to_end(bundle_id)
                return cached
            future = self._dashboard_futures.get(bundle_id)
            owns_calculation = future is None
            if owns_calculation:
                if self.draft_lab.is_busy:
                    raise RuntimeError(
                        "Draft Lab training or benchmark must finish before building a league dashboard"
                    )
                if (
                    self._export_in_progress
                    or self._collections.is_running
                    or self._gm_insights_futures
                    or self._trade_timing_futures
                    or has_active_jobs(self._jobs)
                ):
                    raise RuntimeError(
                        "weekly collection, trade search, export, General Manager Insights, or Trade Timing must finish before building a league dashboard"
                    )
                if self._player_outlook_futures:
                    raise RuntimeError(
                        "Player Lab calculation must finish before building a league dashboard"
                    )
                future = Future()
                self._dashboard_futures[bundle_id] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            bundle = self._load_bundle(bundle_id)
            source_config = bundle.scenario_config
            dashboard_config = (
                source_config
                if source_config.scenario_count <= _MAX_DASHBOARD_SCENARIOS
                else CorrelatedScenarioConfig(
                    _MAX_DASHBOARD_SCENARIOS,
                    source_config.seed,
                    source_config.loadings,
                )
            )
            baseline = self._season_baseline(bundle_id, bundle, dashboard_config)
            dashboard = build_league_dashboard(
                bundle,
                baseline.season_projection,
                baseline.scenarios,
                baseline.iter_baseline_scenarios(),
            )
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            with self._lock:
                self._dashboard_cache[bundle_id] = dashboard
                self._dashboard_cache.move_to_end(bundle_id)
                while len(self._dashboard_cache) > _MAX_DASHBOARD_CACHE_SIZE:
                    self._dashboard_cache.popitem(last=False)
            future.set_result(dashboard)
            return dashboard
        finally:
            with self._lock:
                if self._dashboard_futures.get(bundle_id) is future:
                    del self._dashboard_futures[bundle_id]

    def player_outlook(self, bundle_id: str) -> dict[str, object]:
        """Return one coalesced, bounded-cache player outlook by immutable bundle ID."""

        with self._lock:
            cached = self._player_outlook_cache.get(bundle_id)
            if cached is not None:
                self._player_outlook_cache.move_to_end(bundle_id)
                return cached
            future = self._player_outlook_futures.get(bundle_id)
            owns_calculation = future is None
            if owns_calculation:
                if self.draft_lab.is_busy:
                    raise RuntimeError(
                        "Draft Lab training or benchmark must finish before opening Player Lab"
                    )
                if (
                    self._export_in_progress
                    or self._collections.is_running
                    or self._dashboard_futures
                    or self._gm_insights_futures
                    or self._trade_timing_futures
                    or has_active_jobs(self._jobs)
                ):
                    raise RuntimeError(
                        "weekly collection, trade search, export, league dashboard, General Manager Insights, or Trade Timing must finish before opening Player Lab"
                    )
                future = Future()
                self._player_outlook_futures[bundle_id] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            outlook = build_player_outlook(self._load_bundle(bundle_id))
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            with self._lock:
                self._player_outlook_cache[bundle_id] = outlook
                self._player_outlook_cache.move_to_end(bundle_id)
                while len(self._player_outlook_cache) > _MAX_PLAYER_OUTLOOK_CACHE_SIZE:
                    self._player_outlook_cache.popitem(last=False)
            future.set_result(outlook)
            return outlook
        finally:
            with self._lock:
                if self._player_outlook_futures.get(bundle_id) is future:
                    del self._player_outlook_futures[bundle_id]

    def gm_insights(self, bundle_id: str) -> dict[str, object]:
        """Return evidence-backed team tendencies for one bound league history."""

        self._bundle_path(bundle_id)
        with self._lock:
            future = self._gm_insights_futures.get(bundle_id)
            owns_calculation = future is None
            if owns_calculation:
                if self.draft_lab.is_busy:
                    raise RuntimeError(
                        "Draft Lab training or benchmark must finish before General Manager Insights starts"
                    )
                if (
                    self._export_in_progress
                    or self._collections.is_running
                    or self._dashboard_futures
                    or self._player_outlook_futures
                    or self._trade_timing_futures
                    or has_active_jobs(self._jobs)
                ):
                    raise RuntimeError(
                        "weekly collection, trade search, export, league dashboard, Player Lab, or Trade Timing must finish before General Manager Insights starts"
                    )
                if self._gm_insights_futures:
                    raise RuntimeError(
                        "another General Manager Insights calculation is already running"
                    )
                future = Future()
                self._gm_insights_futures[bundle_id] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            bundle = self._load_bundle(bundle_id)
            history = self._league_history_store().snapshot_for_bundle(bundle_id)
            revision = None if history is None else history.history_revision
            cache_key = bundle_id, revision
            with self._lock:
                cached = self._gm_insights_cache.get(cache_key)
                if cached is not None:
                    self._gm_insights_cache.move_to_end(cache_key)
            if cached is not None:
                future.set_result(cached)
                return cached
            insights = build_gm_insights(
                bundle,
                history,
                bundle_loader=self._load_bundle,
            )
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            with self._lock:
                self._gm_insights_cache[cache_key] = insights
                self._gm_insights_cache.move_to_end(cache_key)
                while len(self._gm_insights_cache) > _MAX_GM_INSIGHTS_CACHE_SIZE:
                    self._gm_insights_cache.popitem(last=False)
            future.set_result(insights)
            return insights
        finally:
            with self._lock:
                if self._gm_insights_futures.get(bundle_id) is future:
                    del self._gm_insights_futures[bundle_id]

    def trade_timing(
        self, bundle_id: str, primary_team_id: str
    ) -> dict[str, object]:
        """Return one coalesced, history-aware trade-timing preview."""

        self._bundle_path(bundle_id)
        request_key = bundle_id, primary_team_id
        with self._lock:
            future = self._trade_timing_futures.get(request_key)
            owns_calculation = future is None
            if owns_calculation:
                if self.draft_lab.is_busy:
                    raise RuntimeError(
                        "Draft Lab training or benchmark must finish before Trade Timing starts"
                    )
                if (
                    self._export_in_progress
                    or self._collections.is_running
                    or self._dashboard_futures
                    or self._player_outlook_futures
                    or self._gm_insights_futures
                    or has_active_jobs(self._jobs)
                ):
                    raise RuntimeError(
                        "weekly collection, trade search, export, league dashboard, Player Lab, or General Manager Insights must finish before Trade Timing starts"
                    )
                if self._trade_timing_futures:
                    raise RuntimeError(
                        "another Trade Timing calculation is already running"
                    )
                future = Future()
                self._trade_timing_futures[request_key] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            bundle = self._load_bundle(bundle_id)
            history = self._league_history_store().snapshot_for_bundle(bundle_id)
            revision = None if history is None else history.history_revision
            cache_key = bundle_id, revision, primary_team_id
            with self._lock:
                cached = self._trade_timing_cache.get(cache_key)
                if cached is not None:
                    self._trade_timing_cache.move_to_end(cache_key)
            if cached is not None:
                future.set_result(cached)
                return cached
            timing = build_trade_timing(bundle, history, primary_team_id)
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            with self._lock:
                self._trade_timing_cache[cache_key] = timing
                self._trade_timing_cache.move_to_end(cache_key)
                while len(self._trade_timing_cache) > _MAX_TRADE_TIMING_CACHE_SIZE:
                    self._trade_timing_cache.popitem(last=False)
            future.set_result(timing)
            return timing
        finally:
            with self._lock:
                if self._trade_timing_futures.get(request_key) is future:
                    del self._trade_timing_futures[request_key]

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
        independent_count = sum(
            row.get("status") == "ready"
            and row.get("power_engine_mode") == "independent"
            for row in bundles
        )
        invalid_count = len(bundles) - ready_count
        if ready_count:
            message = (
                f"{exact_count} exact-method, {surrogate_count} SURROGATE, and "
                f"{independent_count} independent weekly engine(s) are ready. "
                "Surrogate use requires explicit acceptance."
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
            "independent_bundle_count": independent_count,
            "invalid_bundle_count": invalid_count,
            "collection_available": self._collections.available,
            "message": message,
        }

    def start_weekly_collection(
        self, request: WeeklyCollectionRequest
    ) -> dict[str, object]:
        with self._lock:
            if self.draft_lab.is_busy:
                raise RuntimeError("Draft Lab training or benchmark must finish before collecting")
            if self._export_in_progress:
                raise RuntimeError("trade export must finish before collecting")
            if self._dashboard_futures or self._player_outlook_futures:
                raise RuntimeError(
                    "league dashboard or Player Lab calculation must finish before collecting"
                )
            if self._gm_insights_futures:
                raise RuntimeError(
                    "General Manager Insights calculation must finish before collecting"
                )
            if self._trade_timing_futures:
                raise RuntimeError(
                    "Trade Timing calculation must finish before collecting"
                )
            if has_active_jobs(self._jobs):
                raise RuntimeError("stop the local trade search before collecting a new week")
            return self._collections.start(request)

    def weekly_collection(self, job_id: str) -> dict[str, object]:
        return self._collections.job(job_id)

    def cancel_weekly_collection(self, job_id: str) -> dict[str, object]:
        return self._collections.cancel(job_id)

    def acknowledge_weekly_collection_activity(
        self, job_id: str
    ) -> dict[str, object]:
        return self._collections.acknowledge_activity(job_id)

    def confirm_weekly_collection_sign_in(self, job_id: str) -> dict[str, object]:
        return self._collections.confirm_sign_in(job_id)

    def start_search(self, request: LocalSearchRequest) -> dict[str, object]:
        if not isinstance(request, LocalSearchRequest):
            raise ValueError("request must be a LocalSearchRequest")
        bundle = self._load_bundle(request.bundle_id)
        _require_surrogate_consent(bundle, request)
        _search_scope(bundle, request)
        with self._lock:
            if self.draft_lab.is_busy:
                raise RuntimeError("Draft Lab training or benchmark must finish before a trade search")
            if self._dashboard_futures:
                raise RuntimeError(
                    "league dashboard calculation must finish before a trade search"
                )
            if self._player_outlook_futures:
                raise RuntimeError("Player Lab calculation must finish before a trade search")
            if self._gm_insights_futures:
                raise RuntimeError(
                    "General Manager Insights calculation must finish before a trade search"
                )
            if self._trade_timing_futures:
                raise RuntimeError(
                    "Trade Timing calculation must finish before a trade search"
                )
            if self._export_in_progress:
                raise RuntimeError("trade export must finish before another trade search")
            if self._collections.is_running:
                raise RuntimeError("weekly collection must finish before a trade search starts")
            if has_active_jobs(self._jobs):
                raise RuntimeError("another search is already running")
            job = _SearchJob(uuid4().hex, request)
            self._pending_terminal_search_id = None
            self._jobs[job.job_id] = job
            Thread(target=self._run_search, args=(job,), daemon=True).start()
            return self._job_record(job)

    def estimate_search(self, request: LocalSearchRequest) -> dict[str, object]:
        if not isinstance(request, LocalSearchRequest):
            raise ValueError("request must be a LocalSearchRequest")
        bundle = self._load_bundle(request.bundle_id)
        _require_surrogate_consent(bundle, request)
        by_team, primary, selected = _search_scope(bundle, request)
        eligible_positions = {
            player_id: player.eligible_positions
            for player_id, player in bundle.strength_model.players.items()
        }
        if request.trade_format == "three_team":
            space = ThreeWayTradeSpace(
                (
                    primary,
                    *(by_team[team_id] for team_id in selected),
                ),
                request.constraints,
                eligible_positions_by_player=eligible_positions,
            )
            count = space.candidate_count
            return {
                "trade_format": "three_team",
                "agreement_count": 1,
                "candidate_count": count if count <= SAFE_INTEGER else None,
                "candidate_count_text": str(count),
                "participant_team_ids": [
                    request.primary_team_id,
                    *request.counterparty_team_ids,
                ],
                "free_agent_allocation_policy": (
                    None
                    if request.constraints.require_no_drops
                    else MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY
                ),
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
        count = sum(row["candidate_count"] for row in pairs)
        return {
            "trade_format": "two_team",
            "pair_count": len(pairs),
            "candidate_count": count,
            "candidate_count_text": str(count),
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
                or job.context is None
            ):
                raise RuntimeError("search results are not ready")
            request, outcome, context = job.request, job.outcome, job.context
        bundle = self._load_bundle(request.bundle_id)
        if request.trade_format == "three_team":
            return three_way_search_result_record(
                outcome,
                bundle,
                context.season_projection,
                limit,
                free_agent_allocation_policy=(
                    None
                    if request.constraints.require_no_drops
                    else MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY
                ),
            )
        return search_result_record(outcome, bundle, context.season_projection, limit)

    def export_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if job.status != "complete" or job.outcome is None or job.context is None:
                raise RuntimeError("search must complete before it can be exported")
            if self.draft_lab.is_busy:
                raise RuntimeError(
                    "Draft Lab training or benchmark must finish before trade export"
                )
            if self._collections.is_running or has_active_jobs(self._jobs):
                raise RuntimeError(
                    "weekly collection or active trade search must finish before export"
                )
            if self._dashboard_futures or self._player_outlook_futures:
                raise RuntimeError(
                    "league dashboard or Player Lab calculation must finish before export"
                )
            if self._gm_insights_futures:
                raise RuntimeError(
                    "General Manager Insights calculation must finish before trade export"
                )
            if self._trade_timing_futures:
                raise RuntimeError(
                    "Trade Timing calculation must finish before trade export"
                )
            if self._export_in_progress:
                raise RuntimeError("another trade export is already running")
            request, outcome, context = job.request, job.outcome, job.context
            self._export_in_progress = True
        try:
            return self._export_completed_job(request, outcome, context)
        finally:
            with self._lock:
                self._export_in_progress = False

    def _export_completed_job(self, request, outcome, context) -> dict[str, object]:
        bundle = self._load_bundle(request.bundle_id)
        team_names = {row.team_id: row.name for row in bundle.state.teams}
        if request.trade_format == "three_team":
            require_three_way_exportable_count(
                outcome.progress.power_qualified_count
            )
            rows = three_way_workbook_rows(
                outcome.results(),
                team_names,
                bundle.player_names,
                bundle.methodology_mode,
            )
            filename = f"three-team-trade-results-{request.request_id}.xlsx"
            path = export_three_way_trade_workbook(
                self.export_directory / filename,
                _workbook_context(bundle, context.scenario_run_id, request),
                ThreeWayExportProvenance.from_records(
                    request_id=request.request_id,
                    search_run_id=outcome.progress.run_id,
                    participant_team_ids=(
                        request.primary_team_id,
                        *request.counterparty_team_ids,
                    ),
                    participant_team_names=tuple(
                        team_names[team_id]
                        for team_id in (
                            request.primary_team_id,
                            *request.counterparty_team_ids,
                        )
                    ),
                    total_candidate_count=outcome.progress.total_candidate_count,
                    seed=request.seed,
                    trade_constraint_record=request.constraints.to_record(),
                    power_settings_record=request.settings.to_record(),
                    free_agent_allocation_policy=(
                        None
                        if request.constraints.require_no_drops
                        else MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY
                    ),
                ),
                rows,
                team_outlook_rows(bundle.state, context.season_projection),
            )
            return {"filename": path.name, "trade_count": len(rows)}
        rows = workbook_trade_rows(
            outcome,
            team_names,
            bundle.player_names,
            bundle.methodology_evidence,
        )
        filename = f"trade-results-{request.request_id}.xlsx"
        path = export_trade_workbook(
            self.export_directory / filename,
            _workbook_context(bundle, context.scenario_run_id, request),
            rows,
            team_outlook_rows(bundle.state, context.season_projection),
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
            bundle = self._load_bundle(job.request.bundle_id)
            _require_surrogate_consent(bundle, job.request)
            config = CorrelatedScenarioConfig(
                job.request.scenario_count,
                job.request.seed,
                bundle.scenario_config.loadings,
            )
            baseline = self._season_baseline(job.request.bundle_id, bundle, config)
            if job.request.trade_format == "three_team":
                by_team, primary, selected = _search_scope(bundle, job.request)
                space = ThreeWayTradeSpace(
                    (primary, *(by_team[team_id] for team_id in selected)),
                    job.request.constraints,
                    eligible_positions_by_player={
                        player_id: player.eligible_positions
                        for player_id, player in bundle.strength_model.players.items()
                    },
                )
                adjuster = (
                    None
                    if job.request.constraints.require_no_drops
                    else PreparedRosterAdjuster(bundle.strength_model, bundle.rosters)
                )
                prepared = PreparedThreeWayTrade(
                    bundle.strength_model,
                    space.rosters,
                    adjuster,
                )
                search = ResumableThreeWayTradeSearch(
                    space,
                    prepared,
                    baseline,
                    job.request.settings,
                )
                run_directory = self.search_directory / job.request.request_id
                run_directory.mkdir(parents=True, exist_ok=True)
                outcome = search.run(
                    run_directory / "three-team.sqlite3",
                    on_progress=lambda progress: self._set_job(job, progress=progress),
                    should_cancel=job.cancel.is_set,
                )
            else:
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
            self._set_job(
                job,
                status=status,
                progress=outcome.progress,
                outcome=outcome if status == "complete" else None,
                context=(
                    _CompletedSearchContext(
                        baseline.season_projection,
                        baseline.scenarios.run_id,
                    )
                    if status == "complete"
                    else None
                ),
            )
        except Exception as error:
            self._set_job(job, status="failed", error=str(error))

    def _set_job(self, job, **changes) -> None:
        with self._lock:
            was_terminal = job.status in TERMINAL_JOB_STATUSES
            for name, value in changes.items():
                setattr(job, name, value)
            if job.status in TERMINAL_JOB_STATUSES:
                if not was_terminal:
                    self._pending_terminal_search_id = job.job_id
                prune_terminal_jobs(self._jobs, _MAX_RETAINED_SEARCH_JOBS)

    def _season_baseline(
        self,
        bundle_id: str,
        bundle: EngineBundle,
        config: CorrelatedScenarioConfig,
    ) -> PreparedSeasonBaseline:
        """Build each immutable scenario baseline once and retain only the newest."""

        key = (bundle_id, config.config_id)
        with self._lock:
            cached = self._baseline_cache.get(key)
            if cached is not None:
                self._baseline_cache.move_to_end(key)
                return cached
            future = self._baseline_futures.get(key)
            owns_calculation = future is None
            if owns_calculation:
                future = Future()
                self._baseline_futures[key] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            with self._baseline_build_lock:
                baseline = prepare_season_baseline(
                    bundle.state,
                    bundle.rosters,
                    bundle.projections,
                    bundle.eligibilities,
                    config,
                )
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            with self._lock:
                self._baseline_cache[key] = baseline
                self._baseline_cache.move_to_end(key)
                while len(self._baseline_cache) > _MAX_BASELINE_CACHE_SIZE:
                    self._baseline_cache.popitem(last=False)
            future.set_result(baseline)
            return baseline
        finally:
            with self._lock:
                if self._baseline_futures.get(key) is future:
                    del self._baseline_futures[key]

    def _remember_bundle(
        self, bundle: EngineBundle, bundle_id: str | None = None
    ) -> EngineBundle:
        cache_key = bundle_id or bundle.bundle_id
        with self._lock:
            self._bundle_cache[cache_key] = bundle
            self._bundle_cache.move_to_end(cache_key)
            while len(self._bundle_cache) > _MAX_BUNDLE_CACHE_SIZE:
                self._bundle_cache.popitem(last=False)
        return bundle

    def _load_bundle(self, bundle_id: str) -> EngineBundle:
        with self._lock:
            cached = self._bundle_cache.get(bundle_id)
            if cached is not None:
                self._bundle_cache.move_to_end(bundle_id)
                return cached
        return self._remember_bundle(
            load_engine_bundle(self._bundle_path(bundle_id)), bundle_id
        )

    def _league_history_store(self) -> LeagueHistoryStore:
        """Open optional league history only when a history-aware view needs it."""

        with self._lock:
            if self._history_store is None:
                self._history_store = LeagueHistoryStore(
                    self.data_directory / LEAGUE_HISTORY_FILENAME
                )
            return self._history_store

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
        if isinstance(progress, ThreeWaySearchProgress):
            progress_record = {
                "pair_count": 1,
                "completed_pair_count": int(
                    not progress.cancelled
                    and progress.next_candidate_index
                    == progress.total_candidate_count
                ),
                "current_counterparty_team_id": None,
                "examined_candidate_count": (
                    progress.next_candidate_index
                    if progress.next_candidate_index <= SAFE_INTEGER
                    else None
                ),
                "examined_candidate_count_text": str(progress.next_candidate_index),
                "total_candidate_count": (
                    progress.total_candidate_count
                    if progress.total_candidate_count <= SAFE_INTEGER
                    else None
                ),
                "total_candidate_count_text": str(progress.total_candidate_count),
                "qualified_trade_count": (
                    progress.power_qualified_count
                    if progress.power_qualified_count <= SAFE_INTEGER
                    else None
                ),
                "qualified_trade_count_text": str(progress.power_qualified_count),
                "mutual_playoff_gain_count": (
                    progress.all_playoff_gain_count
                    if progress.all_playoff_gain_count <= SAFE_INTEGER
                    else None
                ),
                "mutual_playoff_gain_count_text": str(
                    progress.all_playoff_gain_count
                ),
                "completion_fraction": progress.completion_fraction,
                "cancelled": progress.cancelled,
            }
        elif progress is not None:
            progress_record = {
                "pair_count": progress.pair_count,
                "completed_pair_count": progress.completed_pair_count,
                "current_counterparty_team_id": progress.current_counterparty_team_id,
                "examined_candidate_count": progress.examined_candidate_count,
                "examined_candidate_count_text": str(
                    progress.examined_candidate_count
                ),
                "total_candidate_count": progress.total_candidate_count,
                "total_candidate_count_text": str(progress.total_candidate_count),
                "qualified_trade_count": progress.qualified_trade_count,
                "qualified_trade_count_text": str(progress.qualified_trade_count),
                "mutual_playoff_gain_count": progress.mutual_playoff_gain_count,
                "mutual_playoff_gain_count_text": str(
                    progress.mutual_playoff_gain_count
                ),
                "completion_fraction": progress.completion_fraction,
                "cancelled": progress.cancelled,
            }
        else:
            progress_record = None
        return {
            "job_id": job.job_id,
            "request_id": job.request.request_id,
            "status": job.status,
            "error": job.error,
            "trade_format": job.request.trade_format,
            "request": {
                "bundle_id": job.request.bundle_id,
                "primary_team_id": job.request.primary_team_id,
                "counterparty_team_ids": list(job.request.counterparty_team_ids),
            },
            "progress": progress_record,
        }


def _workbook_context(
    bundle: EngineBundle,
    scenario_run_id: str,
    request: LocalSearchRequest,
) -> TradeWorkbookContext:
    methodology = bundle.methodology_evidence
    independent = bundle.methodology_mode == "independent"
    team_names = {row.team_id: row.name for row in bundle.state.teams}
    return TradeWorkbookContext(
        snapshot_id=bundle.state.snapshot_id,
        strength_model_id=bundle.strength_model.model_id,
        scenario_run_id=scenario_run_id,
        primary_team_id=request.primary_team_id,
        primary_team_name=team_names[request.primary_team_id],
        generated_at=datetime.now(timezone.utc),
        minimum_power_delta=request.settings.minimum_displayed_power_delta,
        scenario_count=request.scenario_count,
        power_engine_mode=bundle.methodology_mode,
        calibration_status=(
            "independent"
            if independent
            else bundle.strength_model.calibration.status.value
        ),
        methodology_evidence_kind=(
            "exact_attestation"
            if bundle.methodology_mode == "exact"
            else "independent_disclosure"
            if independent
            else "surrogate_disclosure"
        ),
        methodology_record_id=(
            bundle.methodology_attestation.attestation_id
            if bundle.methodology_mode == "exact"
            else bundle.independent_power_disclosure.disclosure_id
            if independent
            else bundle.surrogate_disclosure.disclosure_id
        ),
        formula_id=(methodology.policy_id if independent else methodology.formula_id),
        formula_source_fit_id=(
            methodology.disclosure_id
            if independent
            else methodology.formula_source_fit_id
        ),
        methodology_fingerprint_id=(
            methodology.policy_id
            if independent
            else methodology.methodology_fingerprint.fingerprint_id
        ),
        formula_action=(
            "independent_policy"
            if independent
            else methodology.formula_decision.action.value
        ),
        methodology_current_evidence_id=methodology.current_evidence_id,
        methodology_quality_gate=(
            "exact_attestation_v1"
            if bundle.methodology_mode == "exact"
            else "transparent_independent_v1"
            if independent
            else SURROGATE_QUALITY_GATE
        ),
        methodology_holdout_count=methodology.current_holdout_count,
        holdout_max_absolute_score_error=(
            None
            if independent
            else methodology.calibration_diagnostics.max_absolute_score_error
        ),
        holdout_display_match_rate=(
            None
            if independent
            else methodology.calibration_diagnostics.display_match_rate
        ),
        exact_balanced_package_sizes=methodology.validated_balanced_package_sizes,
        sources=workbook_sources(bundle),
    )


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


def _payload_filter(
    payload: Mapping[str, object], side: str
) -> TradePackageFilter | TradeFilterExpression | None:
    legacy_name = f"{side}_filter"
    expression_name = f"{side}_filter_expression"
    if legacy_name in payload and expression_name in payload:
        raise ValueError(
            f"{legacy_name} and {expression_name} cannot both be provided"
        )
    if expression_name in payload:
        value = parse_trade_filter(expression_name, payload[expression_name])
        if not isinstance(value, TradeFilterExpression):
            raise ValueError(f"{expression_name} must be an expression")
        return value
    value = parse_trade_filter(legacy_name, payload.get(legacy_name))
    if isinstance(value, TradeFilterExpression):
        raise ValueError(f"{legacy_name} must be a legacy package filter")
    return value


def _filter_player_ids(
    value: TradePackageFilter | TradeFilterExpression | None,
) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(
        player_id
        for leaf in iter_trade_filter_leaves(value)
        for player_id in leaf.player_ids
    )


def _search_scope(
    bundle: EngineBundle, request: LocalSearchRequest
) -> tuple[dict[str, TeamRoster], TeamRoster, tuple[str, ...]]:
    by_team = {row.team_id: row for row in bundle.rosters}
    try:
        primary = by_team[request.primary_team_id]
    except KeyError:
        raise ValueError("primary_team_id is not present in the weekly bundle") from None
    selected = (
        request.counterparty_team_ids
        if request.trade_format == "three_team"
        else request.counterparty_team_ids
        or tuple(
            team.team_id
            for team in bundle.state.teams
            if team.team_id != request.primary_team_id
        )
    )
    if request.trade_format == "three_team" and len(selected) != 2:
        raise ValueError("three-team trades require exactly two partner teams")
    if request.primary_team_id in selected or not set(selected).issubset(by_team):
        raise ValueError("counterparty selection contains an invalid team")

    outgoing_filter = request.constraints.outgoing_filter
    if outgoing_filter is not None:
        invalid = _filter_player_ids(outgoing_filter).difference(primary.player_ids)
        if invalid:
            raise ValueError(
                "players you give must belong to the selected primary team"
            )

    incoming_filter = request.constraints.incoming_filter
    incoming_player_ids = _filter_player_ids(incoming_filter)
    if incoming_player_ids:
        owner_by_player = {
            player_id: team_id
            for team_id in selected
            for player_id in by_team[team_id].player_ids
        }
        invalid = incoming_player_ids.difference(owner_by_player)
        if invalid:
            raise ValueError(
                "players you receive must belong to a selected other team"
            )
        if request.trade_format == "two_team" and incoming_filter is not None:
            for leaf in iter_trade_filter_leaves(incoming_filter):
                if (
                    leaf.player_mode
                    in {TradeFilterMode.INCLUDE, TradeFilterMode.ONLY}
                    and len(
                        {
                            owner_by_player[player_id]
                            for player_id in leaf.player_ids
                        }
                    )
                    > 1
                ):
                    raise ValueError(
                        "Players that must appear together need to be on the "
                        "same other team."
                    )
    return by_team, primary, selected
