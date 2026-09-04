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
from .bundle_summary_cache import (
    BundleSummaryCacheError,
    load_cached_bundle_summary,
    save_bundle_with_summary,
    save_cached_bundle_summary,
)
from .dashboard import build_league_dashboard
from .data_readiness import (
    build_bundle_data_readiness,
    build_data_readiness_snapshot,
)
from .draft_service import DraftLabService
from .engine_bundle import (
    ENGINE_BUNDLE_SCHEMA_VERSION,
    EngineBundle,
    UnsupportedEngineBundleSchema,
    load_engine_bundle,
)
from .gm_insights import build_gm_insights
from .history_readiness import build_history_data_readiness
from .job_retention import (
    ACTIVE_JOB_STATUSES,
    DEFAULT_TERMINAL_JOB_LIMIT,
    TERMINAL_JOB_STATUSES,
    has_active_jobs,
    prune_terminal_jobs,
)
from .league_history import LeagueHistoryStore, LeagueHistoryStoreError
from .league_search import (
    LeagueSearchOutcome,
    LeagueSearchProgress,
    ResumableLeagueTradeSearch,
)
from .league_workspace import UNASSIGNED_PROFILE_ID, LeagueWorkspaceService
from .operation_timing import OperationTiming
from .player_outlook import build_player_outlook
from .player_outlook_detail import build_player_outlook_detail_from_bundle
from .player_outlook_lazy import (
    build_player_outlook_catalog_from_bundle,
    build_player_outlook_read_context,
)
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
from .trade_space import TeamRoster, TradeConstraints, TradeSpace
from .trade_timing import build_trade_timing, trade_timing_scenario_config
from .weekly_collection import (
    LEAGUE_HISTORY_FILENAME,
    WeeklyCollectionJobs,
    WeeklyCollectionRequest,
    WeeklyCollectionWorkflow,
    load_weekly_history_attempt,
)
from .workbook_model import (
    TradeWorkbookContext,
    TwoTeamExportProvenance,
    team_outlook_rows,
    workbook_trade_rows,
)
from .xlsx_export import export_trade_workbook

_MAX_DASHBOARD_SCENARIOS = 10_000
_MAX_BUNDLE_CACHE_SIZE = 4
_MAX_DASHBOARD_CACHE_SIZE = 4
_MAX_BASELINE_CACHE_SIZE = 1
_MAX_RETAINED_SEARCH_JOBS = DEFAULT_TERMINAL_JOB_LIMIT
_MAX_GM_INSIGHTS_CACHE_SIZE = 4
_MAX_TRADE_TIMING_CACHE_SIZE = 6
_INDEPENDENT_NFL_SCHEDULE_ARTIFACT = (
    "not-retained:independent-nfl-schedule-artifact"
)
_INDEPENDENT_ENSEMBLE_CONFIG_ARTIFACT = (
    "not-retained:independent-ensemble-config-artifact"
)
_INDEPENDENT_FORMULA_SOURCE_FIT = (
    "not-applicable:independent-power-policy-has-no-source-fit"
)


# A full public-catalog outlook can be tens of MiB; one hot bundle avoids
# multiplying that memory cost while still coalescing every concurrent request.
_MAX_PLAYER_OUTLOOK_CACHE_SIZE = 1
_MAX_PLAYER_OUTLOOK_CATALOG_CACHE_SIZE = 2
_MAX_PLAYER_OUTLOOK_DETAIL_CACHE_SIZE = 16
_MAX_PLAYER_OUTLOOK_READ_CACHE_SIZE = 2


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
    timing: OperationTiming = field(default_factory=OperationTiming)


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
        self._league_workspaces = LeagueWorkspaceService(
            self.data_directory,
            self.bundle_directory,
        )
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
        self._gm_insights_cache: OrderedDict[tuple[object, ...], dict[str, object]] = (
            OrderedDict()
        )
        self._gm_insights_futures: dict[
            tuple[object, ...], Future[dict[str, object]]
        ] = {}
        self._trade_timing_cache: OrderedDict[
            tuple[object, ...], dict[str, object]
        ] = OrderedDict()
        self._trade_timing_futures: dict[
            tuple[object, ...], Future[dict[str, object]]
        ] = {}
        self._history_stores: dict[Path, LeagueHistoryStore] = {}
        # Kept as a compatibility view of the most recently opened store.
        self._history_store: LeagueHistoryStore | None = None
        self._player_outlook_catalog_cache: OrderedDict[
            str, dict[str, object]
        ] = OrderedDict()
        self._player_outlook_catalog_futures: dict[
            str, Future[dict[str, object]]
        ] = {}
        self._player_outlook_detail_cache: OrderedDict[
            tuple[str, str], dict[str, object]
        ] = OrderedDict()
        self._player_outlook_detail_futures: dict[
            tuple[str, str], Future[dict[str, object]]
        ] = {}
        self._player_outlook_read_cache: OrderedDict[
            str, tuple[EngineBundle, Mapping[str, object]]
        ] = OrderedDict()
        self._player_outlook_read_futures: dict[
            str, Future[tuple[EngineBundle, Mapping[str, object]]]
        ] = {}
        self._player_outlook_build_lock = Lock()
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
                or self._player_lab_is_busy()
                or self._gm_insights_futures
                or self._trade_timing_futures
                or self._baseline_futures
                or self._collections.is_running
                or has_active_jobs(self._jobs)
                or self.draft_lab.is_busy
            )

    def _player_lab_is_busy(self) -> bool:
        return bool(
            self._player_outlook_futures
            or self._player_outlook_catalog_futures
            or self._player_outlook_detail_futures
            or self._player_outlook_read_futures
        )

    def _require_player_lab_capacity(self) -> None:
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
                "weekly collection, trade search, export, league dashboard, General "
                "Manager Insights, or Trade Timing must finish before opening Player Lab"
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
                or self._player_lab_is_busy()
                or self._gm_insights_futures
                or self._trade_timing_futures
                or self._export_in_progress
            ):
                raise RuntimeError(
                    "weekly collection, trade search, export, league dashboard, Player Lab, General Manager Insights, or Trade Timing must finish before Draft Lab starts"
                )

    def import_bundle(
        self,
        record: Mapping[str, object],
        *,
        league_profile_id: str | None = None,
    ) -> dict[str, object]:
        summary = self._league_workspaces.import_bundle(
            record,
            profile_id=league_profile_id,
        )
        self._load_bundle(summary["bundle_id"])
        if league_profile_id is not None:
            self._invalidate_history_views(summary["bundle_id"])
        return summary

    def bundle(self, bundle_id: str) -> dict[str, object]:
        return bundle_summary(self._load_bundle(bundle_id))

    def league_profiles(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, object]:
        record = self._league_workspaces.profiles(
            include_archived=include_archived,
            limit=limit,
            cursor=cursor,
        )
        record["collection_available"] = self._collections.available
        return record

    def create_league_profile(
        self, payload: Mapping[str, object]
    ) -> dict[str, object]:
        return self._league_workspaces.create_profile(payload)

    def update_league_profile(
        self,
        profile_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        return self._league_workspaces.update_profile(profile_id, payload)

    def archive_league_profile(self, profile_id: str) -> dict[str, object]:
        return self._league_workspaces.archive_profile(profile_id)

    def restore_league_profile(self, profile_id: str) -> dict[str, object]:
        return self._league_workspaces.restore_profile(profile_id)

    def save_league_team(
        self,
        profile_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(payload, Mapping) or set(payload) != {
            "bundle_id",
            "team_id",
        }:
            raise ValueError("saved league team fields are invalid")
        return self._league_workspaces.save_my_team(
            profile_id,
            bundle_id=payload["bundle_id"],
            team_id=payload["team_id"],
        )

    def assign_bundle_to_league(
        self,
        profile_id: str,
        bundle_id: str,
    ) -> dict[str, object]:
        association = self._league_workspaces.assign_bundle(profile_id, bundle_id)
        self._invalidate_history_views(bundle_id)
        return association

    def _invalidate_history_views(self, bundle_id: str) -> None:
        with self._lock:
            for key in tuple(self._gm_insights_cache):
                if key[0] == bundle_id:
                    del self._gm_insights_cache[key]
            for key in tuple(self._trade_timing_cache):
                if key[0] == bundle_id:
                    del self._trade_timing_cache[key]

    def list_bundles(self) -> tuple[dict[str, object], ...]:
        summaries = OrderedDict()
        for path in sorted(self.bundle_directory.glob("*.json")):
            try:
                try:
                    summary = load_cached_bundle_summary(path)
                except BundleSummaryCacheError:
                    summary = None
                if summary is None:
                    bundle = load_engine_bundle(path)
                    if path.stem != bundle.bundle_id:
                        path = self._canonicalize_migrated_bundle(path, bundle)
                    summary = bundle_summary(bundle)
                    try:
                        save_cached_bundle_summary(bundle, path)
                    except BundleSummaryCacheError:
                        pass
                summaries.setdefault(summary["bundle_id"], summary)
            except UnsupportedEngineBundleSchema as error:
                summaries.setdefault(
                    f"unsupported:{path.name}",
                    {
                        "file": path.name,
                        "status": (
                            "legacy_requires_rescan"
                            if error.schema_version < ENGINE_BUNDLE_SCHEMA_VERSION
                            else "requires_app_update"
                        ),
                        "schema_version": error.schema_version,
                        "error": str(error),
                    },
                )
            except ValueError as error:
                summaries.setdefault(
                    f"invalid:{path.name}",
                    {"file": path.name, "status": "invalid", "error": str(error)},
                )
        return tuple(summaries.values())

    def bundle_readiness(self) -> dict[str, object]:
        return self._bundle_readiness(self.list_bundles())

    def bundle_catalog(self) -> dict[str, object]:
        bundles = self.list_bundles()
        return {"bundles": bundles, "readiness": self._bundle_readiness(bundles)}

    def league_bundle_catalog(self, profile_id: str) -> dict[str, object]:
        bundles = self._league_workspaces.bundle_rows(profile_id)
        readiness = self._bundle_readiness(bundles)
        if profile_id == UNASSIGNED_PROFILE_ID:
            readiness["collection_available"] = False
            if not readiness["ready"]:
                readiness["message"] = (
                    "No unassigned weekly data remains. Choose a league workspace or "
                    "import a portable weekly bundle."
                )
        else:
            profile = self._league_workspaces.profile(profile_id)
            base_collection_available = bool(
                self._collections.available
                and not profile["archived"]
                and profile["yahoo_league_id"] is not None
            )
            readiness["collection_available"] = base_collection_available
            readiness["fantasypros_collection_available"] = (
                base_collection_available
            )
            readiness["independent_collection_available"] = bool(
                base_collection_available
                and profile["espn_league_id"] is not None
            )
            if not readiness["ready"] and profile["archived"]:
                readiness["message"] = (
                    "This league is archived. Restore it before collecting a new week."
                )
            elif not readiness["ready"] and profile["yahoo_league_id"] is None:
                readiness["message"] = (
                    "Add this league's Yahoo connection before collecting a week."
                )
            elif not readiness["ready"] and profile["espn_league_id"] is None:
                readiness["message"] = (
                    "FantasyPros-assisted collection is available. Add this league's "
                    "ESPN connection only if you also want independent collection."
                )
        return {"bundles": bundles, "readiness": readiness}

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
                if self._player_lab_is_busy():
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
            _require_bundle_capability(
                bundle,
                "team_outlook_and_exports",
                "league dashboard",
            )
            source_config = bundle.scenario_config
            dashboard_config = (
                source_config
                if source_config.scenario_count <= _MAX_DASHBOARD_SCENARIOS
                else CorrelatedScenarioConfig(
                    _MAX_DASHBOARD_SCENARIOS,
                    source_config.seed,
                    source_config.loadings,
                    source_config.player_score_floor,
                )
            )
            baseline = self._season_baseline(bundle_id, bundle, dashboard_config)
            dashboard = dict(build_league_dashboard(
                bundle,
                baseline.season_projection,
                baseline.scenarios,
                baseline.iter_baseline_scenarios(),
            ))
            dashboard["data_readiness"] = build_bundle_data_readiness(bundle)[
                "capabilities"
            ]["team_outlook_and_exports"]
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
                self._require_player_lab_capacity()
                future = Future()
                self._player_outlook_futures[bundle_id] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            with self._player_outlook_build_lock:
                bundle = self._load_bundle(bundle_id)
                outlook = dict(build_player_outlook(bundle))
            outlook["data_readiness"] = build_bundle_data_readiness(bundle)[
                "capabilities"
            ]["player_lab"]
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

        bundle = self._load_bundle(bundle_id)
        history, history_store_status, history_attempt = self._history_snapshot(bundle)
        revision = None if history is None else history.history_revision
        cache_key = (
            bundle_id,
            revision,
            history_store_status,
            self._history_attempt_revision(history_attempt),
            self._loadable_history_engine_revision(history),
        )
        with self._lock:
            cached = self._gm_insights_cache.get(cache_key)
            if cached is not None:
                self._gm_insights_cache.move_to_end(cache_key)
                return cached
            future = self._gm_insights_futures.get(cache_key)
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
                    or self._player_lab_is_busy()
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
                self._gm_insights_futures[cache_key] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            insights = dict(build_gm_insights(
                bundle,
                history,
                bundle_loader=self._load_bundle,
            ))
            insights["data_readiness"] = build_history_data_readiness(
                bundle,
                history,
                store_status=history_store_status,
                collection_attempt=history_attempt,
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
                if self._gm_insights_futures.get(cache_key) is future:
                    del self._gm_insights_futures[cache_key]

    def trade_timing(
        self, bundle_id: str, primary_team_id: str
    ) -> dict[str, object]:
        """Return one coalesced, history-aware trade-timing preview."""

        bundle = self._load_bundle(bundle_id)
        history, history_store_status, history_attempt = self._history_snapshot(bundle)
        revision = None if history is None else history.history_revision
        timing_config = trade_timing_scenario_config(bundle)
        cache_key = (
            bundle_id,
            timing_config.config_id,
            revision,
            history_store_status,
            self._history_attempt_revision(history_attempt),
            primary_team_id,
        )
        with self._lock:
            cached = self._trade_timing_cache.get(cache_key)
            if cached is not None:
                self._trade_timing_cache.move_to_end(cache_key)
                return cached
            future = self._trade_timing_futures.get(cache_key)
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
                    or self._player_lab_is_busy()
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
                self._trade_timing_futures[cache_key] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            baseline = (
                None
                if not bundle.state.remaining_regular_season_weeks
                else self._season_baseline(bundle_id, bundle, timing_config)
            )
            timing = dict(
                build_trade_timing(
                    bundle,
                    history,
                    primary_team_id,
                    prepared_baseline=baseline,
                )
            )
            timing["data_readiness"] = build_history_data_readiness(
                bundle,
                history,
                store_status=history_store_status,
                collection_attempt=history_attempt,
            )
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
                if self._trade_timing_futures.get(cache_key) is future:
                    del self._trade_timing_futures[cache_key]

    def _player_outlook_read_model(
        self, bundle_id: str
    ) -> tuple[EngineBundle, Mapping[str, object]]:
        """Coalesce one bounded parsed-bundle/index cache for catalog and detail."""

        with self._lock:
            cached = self._player_outlook_read_cache.get(bundle_id)
            if cached is not None:
                self._player_outlook_read_cache.move_to_end(bundle_id)
                return cached
            future = self._player_outlook_read_futures.get(bundle_id)
            owns_calculation = future is None
            if owns_calculation:
                future = Future()
                self._player_outlook_read_futures[bundle_id] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            with self._player_outlook_build_lock:
                bundle = self._load_bundle(bundle_id)
                context = build_player_outlook_read_context(bundle)
                read_model = (bundle, context)
                with self._lock:
                    self._player_outlook_read_cache[bundle_id] = read_model
                    self._player_outlook_read_cache.move_to_end(bundle_id)
                    while (
                        len(self._player_outlook_read_cache)
                        > _MAX_PLAYER_OUTLOOK_READ_CACHE_SIZE
                    ):
                        self._player_outlook_read_cache.popitem(last=False)
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            future.set_result(read_model)
            return read_model
        finally:
            with self._lock:
                if self._player_outlook_read_futures.get(bundle_id) is future:
                    del self._player_outlook_read_futures[bundle_id]

    def player_outlook_catalog(self, bundle_id: str) -> dict[str, object]:
        """Build and cache list/filter fields without creating full-player detail."""

        with self._lock:
            cached = self._player_outlook_catalog_cache.get(bundle_id)
            if cached is not None:
                self._player_outlook_catalog_cache.move_to_end(bundle_id)
                return cached
            future = self._player_outlook_catalog_futures.get(bundle_id)
            owns_calculation = future is None
            if owns_calculation:
                self._require_player_lab_capacity()
                future = Future()
                self._player_outlook_catalog_futures[bundle_id] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            bundle, context = self._player_outlook_read_model(bundle_id)
            with self._player_outlook_build_lock:
                catalog = dict(build_player_outlook_catalog_from_bundle(
                    bundle,
                    context=context,
                ))
                catalog["data_readiness"] = build_bundle_data_readiness(bundle)[
                    "capabilities"
                ]["player_lab"]
                with self._lock:
                    self._player_outlook_catalog_cache[bundle_id] = catalog
                    self._player_outlook_catalog_cache.move_to_end(bundle_id)
                    while (
                        len(self._player_outlook_catalog_cache)
                        > _MAX_PLAYER_OUTLOOK_CATALOG_CACHE_SIZE
                    ):
                        self._player_outlook_catalog_cache.popitem(last=False)
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            future.set_result(catalog)
            return catalog
        finally:
            with self._lock:
                if self._player_outlook_catalog_futures.get(bundle_id) is future:
                    del self._player_outlook_catalog_futures[bundle_id]

    def player_outlook_detail(
        self, bundle_id: str, player_id: str
    ) -> dict[str, object]:
        """Return full evidence for one exact canonical player ID."""

        if not isinstance(player_id, str) or not player_id:
            raise ValueError("player_id must be a non-empty string")
        cache_key = (bundle_id, player_id)
        with self._lock:
            cached = self._player_outlook_detail_cache.get(cache_key)
            if cached is not None:
                self._player_outlook_detail_cache.move_to_end(cache_key)
                return cached
            future = self._player_outlook_detail_futures.get(cache_key)
            owns_calculation = future is None
            if owns_calculation:
                self._require_player_lab_capacity()
                future = Future()
                self._player_outlook_detail_futures[cache_key] = future
        assert future is not None
        if not owns_calculation:
            return future.result()

        try:
            catalog = self.player_outlook_catalog(bundle_id)
            bundle, context = self._player_outlook_read_model(bundle_id)
            with self._player_outlook_build_lock:
                detail = dict(build_player_outlook_detail_from_bundle(
                    bundle,
                    player_id,
                    catalog=catalog,
                    context=context,
                ))
                detail["data_readiness"] = catalog["data_readiness"]
                with self._lock:
                    self._player_outlook_detail_cache[cache_key] = detail
                    self._player_outlook_detail_cache.move_to_end(cache_key)
                    while (
                        len(self._player_outlook_detail_cache)
                        > _MAX_PLAYER_OUTLOOK_DETAIL_CACHE_SIZE
                    ):
                        self._player_outlook_detail_cache.popitem(last=False)
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            future.set_result(detail)
            return detail
        finally:
            with self._lock:
                if self._player_outlook_detail_futures.get(cache_key) is future:
                    del self._player_outlook_detail_futures[cache_key]

    def _bundle_readiness(self, bundles) -> dict[str, object]:
        ready_count = sum(row.get("status") == "ready" for row in bundles)
        validated_count = sum(
            row.get("status") == "ready"
            and row.get("power_engine_mode") == "holdout_validated"
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
        legacy_count = sum(
            row.get("status") == "legacy_requires_rescan" for row in bundles
        )
        update_count = sum(
            row.get("status") == "requires_app_update" for row in bundles
        )
        invalid_count = sum(row.get("status") == "invalid" for row in bundles)
        if ready_count:
            message_parts = [
                f"{validated_count} holdout-validated, {surrogate_count} SURROGATE, "
                f"and {independent_count} independent weekly engine(s) are ready. "
                "Surrogate use requires explicit acceptance."
            ]
        elif legacy_count or update_count or invalid_count:
            message_parts = ["No compatible weekly engine is ready."]
        elif self._collections.available:
            message_parts = [
                "Not ready yet. Collect this week or import a complete bundle."
            ]
        else:
            message_parts = [
                "Not ready yet. Weekly collection is unavailable in this build; "
                "import a complete bundle."
            ]
        if legacy_count:
            message_parts.append(
                f"Older-format saved weekly bundles: {legacy_count}. Scan the league "
                "again to rebuild them with complete schedule and model evidence."
            )
        if update_count:
            message_parts.append(
                f"Saved weekly bundles requiring a newer application: {update_count}. "
                "Update the app before using them."
            )
        if invalid_count:
            message_parts.append(
                f"Saved weekly bundles that failed validation: {invalid_count}. "
                "Collect those weeks again or import complete bundles."
            )
        return {
            "ready": ready_count > 0,
            "ready_bundle_count": ready_count,
            "holdout_validated_bundle_count": validated_count,
            "surrogate_bundle_count": surrogate_count,
            "independent_bundle_count": independent_count,
            "legacy_bundle_count": legacy_count,
            "requires_app_update_bundle_count": update_count,
            "invalid_bundle_count": invalid_count,
            "collection_available": self._collections.available,
            "message": " ".join(message_parts),
        }

    def _history_snapshot(self, bundle: EngineBundle):
        """Load an optional sidecar without making core bundle use depend on it."""

        attempt = None
        try:
            data_directory = self._league_workspaces.data_directory_for_bundle(
                bundle.bundle_id
            )
            attempt = load_weekly_history_attempt(
                data_directory,
                bundle.bundle_id,
            )
            history = self._league_history_store(
                bundle.bundle_id,
                data_directory=data_directory,
            ).snapshot_for_bundle(bundle.bundle_id)
        except (KeyError, LeagueHistoryStoreError, OSError, RuntimeError, ValueError):
            return None, "unavailable", attempt
        if history is not None and (
            history.league_key != bundle.source_manifest.league_binding_id
            or history.season != bundle.state.season
            or history.bundle_id != bundle.bundle_id
        ):
            return None, "unavailable", attempt
        return history, "available", attempt

    @staticmethod
    def _history_attempt_revision(attempt) -> str | None:
        if attempt is None:
            return None
        return content_id("history-attempt", attempt.to_record())

    def _loadable_history_engine_revision(self, history) -> str | None:
        """Invalidate GM caches when a formerly missing prior engine appears."""

        if history is None:
            return None
        available = tuple(
            sorted(
                binding.bundle_id
                for binding in history.bundle_bindings
                if BUNDLE_ID_PATTERN.fullmatch(binding.bundle_id)
                and (
                    self.bundle_directory / f"{binding.bundle_id}.json"
                ).is_file()
            )
        )
        return content_id("history-engine-set", {"bundle_ids": list(available)})

    def start_weekly_collection(
        self, request: WeeklyCollectionRequest
    ) -> dict[str, object]:
        with self._lock:
            self._require_collection_capacity_locked()
            return self._collections.start(request)

    def start_profile_weekly_collection(
        self,
        profile_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        plan = self._league_workspaces.collection_plan(profile_id, payload)

        def associate_published(bundle: EngineBundle) -> None:
            self._league_workspaces.associate_bundle(
                profile_id,
                bundle,
                expected_espn_league_id=plan.espn_league_id,
            )
            self._remember_bundle(bundle)
            self._invalidate_history_views(bundle.bundle_id)

        with self._lock:
            self._require_collection_capacity_locked()
            return self._collections.start(
                plan.request,
                data_directory=plan.workspace,
                on_published=associate_published,
            )

    def _require_collection_capacity_locked(self) -> None:
        if self.draft_lab.is_busy:
            raise RuntimeError(
                "Draft Lab training or benchmark must finish before collecting"
            )
        if self._export_in_progress:
            raise RuntimeError("trade export must finish before collecting")
        if self._dashboard_futures or self._player_lab_is_busy():
            raise RuntimeError(
                "league dashboard or Player Lab calculation must finish before collecting"
            )
        if self._gm_insights_futures:
            raise RuntimeError(
                "General Manager Insights calculation must finish before collecting"
            )
        if self._trade_timing_futures:
            raise RuntimeError("Trade Timing calculation must finish before collecting")
        if has_active_jobs(self._jobs):
            raise RuntimeError("stop the local trade search before collecting a new week")

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
        _require_bundle_capability(bundle, "trade_search", "trade search")
        _search_scope(bundle, request)
        with self._lock:
            if self.draft_lab.is_busy:
                raise RuntimeError("Draft Lab training or benchmark must finish before a trade search")
            if self._dashboard_futures:
                raise RuntimeError(
                    "league dashboard calculation must finish before a trade search"
                )
            if self._player_lab_is_busy():
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
        readiness = build_bundle_data_readiness(bundle)["capabilities"][
            "trade_search"
        ]
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
                "free_agent_allocation_policy": MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY,
                "data_readiness": readiness,
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
            "data_readiness": readiness,
        }

    def job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._job_record(self._require_job(job_id))

    def cancel_job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require_job(job_id)
            if job.status in {"queued", "running"}:
                job.cancel.set()
                job.timing.request_cancel()
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
            result = three_way_search_result_record(
                outcome,
                bundle,
                context.season_projection,
                limit,
                free_agent_allocation_policy=MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY,
            )
        else:
            result = search_result_record(
                outcome, bundle, context.season_projection, limit
            )
        search_run_ids = (
            [outcome.progress.run_id]
            if request.trade_format == "three_team"
            else [row.search.progress.run_id for row in outcome.pairs]
        )
        return {
            **result,
            "bundle_id": request.bundle_id,
            "request_id": request.request_id,
            "search_run_id": (
                search_run_ids[0] if len(search_run_ids) == 1 else None
            ),
            "search_run_ids": search_run_ids,
            "search_request": request.to_record(),
            "data_readiness": build_bundle_data_readiness(bundle)["capabilities"][
                "trade_search"
            ],
        }

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
            if self._dashboard_futures or self._player_lab_is_busy():
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
                    bundle_id=bundle.bundle_id,
                    waiver_pool_id=bundle.waiver_pool.waiver_pool_id,
                    request_id=request.request_id,
                    request_record=request.to_record(),
                    search_run_definition=outcome.run_definition,
                    participant_team_names=tuple(
                        team_names[team_id]
                        for team_id in outcome.run_definition.participant_team_ids
                    ),
                    completed_candidate_count=outcome.progress.next_candidate_index,
                    free_agent_allocation_policy=MULTI_TEAM_FREE_AGENT_ALLOCATION_POLICY,
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
            TwoTeamExportProvenance.from_outcome(
                bundle_id=bundle.bundle_id,
                waiver_pool_id=bundle.waiver_pool.waiver_pool_id,
                request_id=request.request_id,
                request_record=request.to_record(),
                outcome=outcome,
            ),
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
            job.timing.start("preparing_season_simulation")
            self._set_job(job, status="running")
            bundle = self._load_bundle(job.request.bundle_id)
            _require_surrogate_consent(bundle, job.request)
            _require_bundle_capability(bundle, "trade_search", "trade search")
            config = CorrelatedScenarioConfig(
                job.request.scenario_count,
                job.request.seed,
                bundle.scenario_config.loadings,
                bundle.scenario_config.player_score_floor,
            )
            baseline = self._season_baseline(job.request.bundle_id, bundle, config)
            if job.cancel.is_set():
                job.timing.cancel()
                self._set_job(job, status="cancelled")
                return
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
                adjuster = PreparedRosterAdjuster(
                    bundle.strength_model,
                    bundle.rosters,
                    forbid_drops=job.request.constraints.require_no_drops,
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
                job.timing.begin_phase(
                    "searching_trade_combinations",
                    completed_units=0,
                    total_units=space.candidate_count,
                )
                run_directory = self.search_directory / job.request.request_id
                run_directory.mkdir(parents=True, exist_ok=True)
                outcome = search.run(
                    run_directory / "three-team.sqlite3",
                    on_progress=lambda progress: self._search_progress(job, progress),
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
                job.timing.begin_phase(
                    "searching_trade_combinations",
                    completed_units=0,
                    total_units=sum(
                        runner.run_definition.total_candidate_count
                        for _, runner in search.runners
                    ),
                )
                outcome = search.run(
                    self.search_directory / job.request.request_id,
                    on_progress=lambda progress: self._search_progress(job, progress),
                    should_cancel=job.cancel.is_set,
                )
            status = "cancelled" if outcome.progress.cancelled else "complete"
            if status == "cancelled":
                job.timing.cancel()
            else:
                job.timing.finish()
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
            job.timing.fail()
            self._set_job(job, status="failed", error=str(error))

    def _search_progress(
        self,
        job: _SearchJob,
        progress: LeagueSearchProgress | ThreeWaySearchProgress,
    ) -> None:
        completed = (
            progress.next_candidate_index
            if isinstance(progress, ThreeWaySearchProgress)
            else progress.examined_candidate_count
        )
        total = progress.total_candidate_count
        job.timing.observe(completed, total)
        self._set_job(job, progress=progress)

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

    def _league_history_store(
        self,
        bundle_id: str,
        *,
        data_directory: Path | None = None,
    ) -> LeagueHistoryStore:
        """Open optional league history only when a history-aware view needs it."""

        if data_directory is None:
            data_directory = self._league_workspaces.data_directory_for_bundle(
                bundle_id
            )
        history_path = (
            data_directory / LEAGUE_HISTORY_FILENAME
        )
        with self._lock:
            store = self._history_stores.get(history_path)
            if store is None:
                store = LeagueHistoryStore(history_path)
                self._history_stores[history_path] = store
            self._history_store = store
            return store

    def _canonicalize_migrated_bundle(
        self, legacy_path: Path, bundle: EngineBundle
    ) -> Path:
        """Persist a migrated bundle under its new content ID before retiring legacy."""

        source = legacy_path.resolve()
        if source.parent != self.bundle_directory or source.suffix.casefold() != ".json":
            raise ValueError("legacy bundle path is outside the bundle directory")
        target = self.bundle_directory / f"{bundle.bundle_id}.json"
        if target.exists():
            existing = load_engine_bundle(target)
            if existing.to_record() != bundle.to_record():
                raise ValueError("canonical migrated bundle conflicts with saved content")
            try:
                save_cached_bundle_summary(existing, target)
            except BundleSummaryCacheError:
                pass
        else:
            save_bundle_with_summary(bundle, target)
        if source != target:
            source.unlink()
            legacy_summary = (
                self.bundle_directory / ".summaries" / f"{source.stem}.json"
            )
            try:
                legacy_summary.unlink(missing_ok=True)
            except OSError:
                pass
        return target

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
            "bundle_id": job.request.bundle_id,
            "status": job.status,
            "error": job.error,
            "trade_format": job.request.trade_format,
            "request": {
                "bundle_id": job.request.bundle_id,
                "primary_team_id": job.request.primary_team_id,
                "counterparty_team_ids": list(job.request.counterparty_team_ids),
            },
            "search_request": job.request.to_record(),
            "progress": progress_record,
            "operation": job.timing.snapshot(),
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
        bundle_id=bundle.bundle_id,
        waiver_pool_id=bundle.waiver_pool.waiver_pool_id,
        snapshot_id=bundle.state.snapshot_id,
        scoring_profile_id=bundle.scoring_profile.scoring_profile_id,
        nfl_schedule_id=(
            _INDEPENDENT_NFL_SCHEDULE_ARTIFACT
            if independent
            else bundle.nfl_schedule.schedule_id
        ),
        ensemble_config_id=(
            _INDEPENDENT_ENSEMBLE_CONFIG_ARTIFACT
            if independent
            else bundle.ensemble_config.config_id
        ),
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
            else "holdout_validated"
            if bundle.methodology_mode == "holdout_validated"
            else "surrogate"
        ),
        methodology_evidence_kind=(
            "independent_disclosure"
            if independent
            else "blind_holdout_attestation"
            if bundle.methodology_mode == "holdout_validated"
            else "surrogate_disclosure"
        ),
        methodology_record_id=(
            bundle.independent_power_disclosure.disclosure_id
            if independent
            else bundle.methodology_attestation.attestation_id
            if bundle.methodology_mode == "holdout_validated"
            else bundle.surrogate_disclosure.disclosure_id
        ),
        formula_id=(methodology.policy_id if independent else methodology.formula_id),
        formula_source_fit_id=(
            _INDEPENDENT_FORMULA_SOURCE_FIT
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
            "transparent_independent_v1"
            if independent
            else "blind_holdout_validation_v1"
            if bundle.methodology_mode == "holdout_validated"
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
        holdout_validated_balanced_package_sizes=(
            methodology.validated_balanced_package_sizes
        ),
        data_readiness=build_data_readiness_snapshot(bundle),
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


def _require_bundle_capability(
    bundle: EngineBundle,
    capability_name: str,
    feature_name: str,
) -> None:
    capability = build_bundle_data_readiness(bundle)["capabilities"].get(
        capability_name
    )
    if not isinstance(capability, Mapping):
        raise ValueError(f"{feature_name} readiness is unavailable")
    if capability.get("status") != "not_ready":
        return
    missing = capability.get("missing")
    details = (
        "; ".join(str(value) for value in missing)
        if isinstance(missing, list) and missing
        else "required bundle evidence"
    )
    raise ValueError(
        f"{feature_name} is not ready for this weekly bundle: {details}"
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
