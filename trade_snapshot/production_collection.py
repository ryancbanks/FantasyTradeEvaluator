"""Production-only weekly source collection; trade evaluation remains offline."""

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from ._analyzer_types import BundleFingerprint
from ._production_source_policy import (
    espn_host_id,
    espn_projection_url,
    league_metadata,
    provider_id,
    response_schema_digest,
    runtime_bindings,
    validate_host_scoring,
)
from .browser_capture import (
    BrowserCaptureCancelled,
    BrowserCaptureDependencyError,
    BrowserCaptureError,
    BrowserCaptureOptions,
    BrowserExtensionUpgradeRequired,
    BrowserCollector,
    SignInGate,
    YahooScoringError,
)
from .capture_schema import (
    CaptureKind,
    CapturePlan,
    FantasyProsECRArtifact,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    PageCaptureTask,
    ProjectionTableSpec,
    validate_artifact_for_task,
)
from .collection_diagnostics import (
    save_fantasypros_league_capture,
    save_validation_failure,
)
from .engine_bundle import EngineBundle
from .espn_activity import espn_activity_capture
from .espn_free_read import (
    EspnFreeReadClient,
    EspnFreeReadError,
    EspnUnauthorizedError,
)
from .espn_league import espn_host_league_snapshot
from .history_ingest import canonicalize_espn_history
from .identity_io import load_identity_registry, save_identity_registry
from .independent_source_plan import build_independent_weekly_source_plan
from .independent_weekly_assembly import (
    IndependentWeeklyEngine,
    assemble_independent_weekly_engine,
)
from .nfl_schedule import parse_espn_pro_team_schedule
from .production_calibration import (
    BrowserCalibrationFactory,
    CalibrationCallbacks,
    CalibrationCaptureContext,
    InteractiveSignInGate,
)
from .projection_source_policy import select_projection_sources
from .player_profile_materialize import (
    PlayerProfileMaterializationError,
    materialize_player_profiles,
)
from .player_profiles import PlayerProfileSnapshot
from .public_player_cache import (
    PublicPlayerCacheError,
    load_public_player_week,
    save_public_player_week,
)
from .public_player_data import (
    DataAvailability,
    PublicPlayerDataCancelled,
    PublicPlayerDataError,
    PublicPlayerDataSnapshot,
    collect_public_player_data,
)
from .public_projection_collection import (
    assemble_with_public_fallback,
    capture_optional_public,
    split_optional_public,
)
from .calibration_workflow import CalibrationNotExact
from .source_plan import build_weekly_source_plan
from .weekly_assembly import AssembledWeeklyEvidence, assemble_weekly_refresh_evidence
from .weekly_collection import (
    WeeklyCollectionError,
    WeeklyCollectionPublication,
    WeeklyCollectionProgress,
    WeeklyCollectionRequest,
    WeeklyCollectionStage,
)
from .weekly_refresh import (
    CalibrationRequired,
    RefreshCancelled,
    RefreshProgress,
    RefreshStage,
    refresh_weekly_engine,
)


_TRADE_ANALYZER_URL = "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"
_IDENTITY_FILE = "identity-registry.json"


class ProductionWeeklyCollectionWorkflow:
    """Collect one verified week, then hand an offline bundle to the job publisher."""

    def __init__(
        self,
        *,
        sign_in_gate: SignInGate,
        calibration_factory: Callable[
            [AssembledWeeklyEvidence, str, CalibrationCaptureContext], CalibrationCallbacks
        ],
        collector: BrowserCollector | None = None,
        espn_reader: Callable | None = None,
        host_adapter: Callable = espn_host_league_snapshot,
        schedule_adapter: Callable = parse_espn_pro_team_schedule,
        plan_builder: Callable = build_weekly_source_plan,
        assembler: Callable = assemble_weekly_refresh_evidence,
        independent_plan_builder: Callable = build_independent_weekly_source_plan,
        independent_assembler: Callable = assemble_independent_weekly_engine,
        public_player_reader: Callable = collect_public_player_data,
        profile_builder: Callable = materialize_player_profiles,
        refresher: Callable = refresh_weekly_engine,
        activity_adapter: Callable = espn_activity_capture,
        history_adapter: Callable = canonicalize_espn_history,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        dependencies = (
            calibration_factory,
            host_adapter,
            schedule_adapter,
            plan_builder,
            assembler,
            independent_plan_builder,
            independent_assembler,
            public_player_reader,
            profile_builder,
            refresher,
            activity_adapter,
            history_adapter,
            now,
        )
        if not callable(getattr(sign_in_gate, "is_ready", None)):
            raise ValueError("sign_in_gate must provide interactive readiness confirmation")
        if any(not callable(value) for value in dependencies):
            raise ValueError("production collection dependencies must be callable")
        if collector is not None and not callable(getattr(collector, "collect", None)):
            raise ValueError("collector must provide collect()")
        self._gate = sign_in_gate
        self._calibration_factory = calibration_factory
        self._collector = collector or BrowserCollector()
        self._espn_reader = espn_reader or EspnFreeReadClient()
        self._host_adapter = host_adapter
        self._schedule_adapter = schedule_adapter
        self._plan_builder = plan_builder
        self._assembler = assembler
        self._independent_plan_builder = independent_plan_builder
        self._independent_assembler = independent_assembler
        self._public_player_reader = public_player_reader
        self._profile_builder = profile_builder
        self._refresher = refresher
        self._activity_adapter = activity_adapter
        self._history_adapter = history_adapter
        self._now = now

    @property
    def sign_in_gate(self) -> SignInGate:
        return self._gate

    def __call__(
        self,
        request: WeeklyCollectionRequest,
        *,
        data_directory: Path,
        progress: Callable[[WeeklyCollectionProgress], None],
        cancelled: Callable[[], bool],
    ) -> WeeklyCollectionPublication:
        if not isinstance(request, WeeklyCollectionRequest):
            raise ValueError("request must be a WeeklyCollectionRequest")
        if request.yahoo_projection_league_url is None:
            raise WeeklyCollectionError(
                "A numeric Yahoo fantasy-football league link is required for Yahoo "
                "projections."
            )
        if not callable(progress) or not callable(cancelled):
            raise ValueError("progress and cancelled must be callable")
        root = Path(data_directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        validation = _ValidationContext("preparing weekly collection")
        reset_gate = getattr(self._gate, "reset", None)
        if callable(reset_gate):
            reset_gate()
        token = _CancellationToken(cancelled)
        options = BrowserCaptureOptions(
            root / "browser-profile",
            headed=True,
            navigation_timeout_ms=45_000,
            capture_timeout_ms=45_000,
            sign_in_timeout_ms=300_000,
            overall_timeout_ms=3_600_000,
            action_delay_ms=200,
        )
        try:
            open_session = getattr(self._collector, "open_session", None)
            browser_context = (
                open_session(options, cancellation=token, sign_in_gate=self._gate)
                if callable(open_session)
                else nullcontext(self._collector)
            )
            with browser_context as collector:
                return self._collect(
                    request,
                    root,
                    options,
                    token,
                    progress,
                    cancelled,
                    collector,
                    validation,
                )
        except WeeklyCollectionError:
            raise
        except (BrowserCaptureCancelled, PublicPlayerDataCancelled, RefreshCancelled):
            raise WeeklyCollectionError("Weekly collection was cancelled.") from None
        except EspnFreeReadError as error:
            raise WeeklyCollectionError(str(error)) from None
        except YahooScoringError as error:
            raise WeeklyCollectionError(str(error)) from None
        except BrowserExtensionUpgradeRequired as error:
            raise WeeklyCollectionError(str(error)) from None
        except BrowserCaptureDependencyError:
            raise WeeklyCollectionError(
                "Connect the Fantasy Trade Evaluator browser extension, then collect "
                "the week again. No weekly bundle was published."
            ) from None
        except BrowserCaptureError as error:
            raise WeeklyCollectionError(
                f"{str(error).rstrip('.')}. No weekly bundle was published."
            ) from None
        except CalibrationRequired:
            raise WeeklyCollectionError(
                "The exact FantasyPros formula could not be calibrated for this week."
            ) from None
        except CalibrationNotExact as error:
            if error.surrogate_eligible and not request.allow_surrogate_power:
                message = (
                    "The healthy fitted formula missed exact blind replication, so no "
                    "bundle was published. Select the explicit SURROGATE option to "
                    "publish the measured approximation."
                )
            else:
                message = (
                    "Calibration did not meet the exact or healthy SURROGATE "
                    "publication gate. No weekly bundle was published."
                )
            raise WeeklyCollectionError(message) from None
        except ValueError as error:
            league_capture = (
                save_fantasypros_league_capture(root, validation.league)
                if validation.league is not None
                else None
            )
            diagnostic_id = save_validation_failure(
                root,
                stage=validation.stage,
                error=error,
                captured_at=self._now(),
                league_capture_available=league_capture is not None,
            )
            diagnostic = (
                f" Local diagnostic {diagnostic_id} was saved."
                if diagnostic_id is not None
                else ""
            )
            raise WeeklyCollectionError(
                "Weekly source data did not pass strict validation during "
                f"{validation.stage}. No weekly bundle was published.{diagnostic}"
            ) from None
        finally:
            if callable(reset_gate):
                reset_gate()

    def _collect(
        self,
        request,
        root,
        options,
        token,
        progress,
        cancelled,
        collector,
        validation,
    ):
        league = None
        metadata = {}
        team_count = None
        if request.use_fantasypros:
            validation.stage = "FantasyPros league capture"
            _emit(
                progress,
                WeeklyCollectionStage.COLLECTING_LEAGUE,
                .05,
                "Reading FantasyPros through your connected browser extension",
            )
            league_task = PageCaptureTask(
                "fantasypros",
                request.season,
                request.week,
                "league_source",
                _TRADE_ANALYZER_URL,
            )
            league_rows = collector.collect(
                CapturePlan((league_task,)),
                options,
                cancellation=token,
                sign_in_gate=self._gate,
            )
            validation.stage = "FantasyPros league artifact verification"
            league = _league_artifact(league_rows, league_task)
            validation.league = league
            team_count = _discovered_team_count(
                league, request.expected_team_count
            )
            _emit(
                progress,
                WeeklyCollectionStage.COLLECTING_LEAGUE,
                .15,
                f"Found {team_count} teams and complete FantasyPros rosters",
            )
            validation.stage = "FantasyPros and ESPN league linkage"
            metadata = league_metadata(league)
        else:
            validation.stage = "independent ESPN league setup"
            _emit(
                progress,
                WeeklyCollectionStage.COLLECTING_LEAGUE,
                .05,
                "FantasyPros is off; starting from your ESPN league",
            )
        host_id = espn_host_id(metadata, request.host_league_url)

        validation.stage = "ESPN league and schedule collection"
        _emit(progress, WeeklyCollectionStage.COLLECTING_ESPN, .25,
              "Reading the league schedule, standings, rosters, and NFL team schedule")
        _check_cancelled(cancelled)
        try:
            league_payload, pro_team_payload = self._espn_reader(
                request.season, host_id, cancelled
            )
        except EspnUnauthorizedError:
            _emit(
                progress,
                WeeklyCollectionStage.COLLECTING_ESPN,
                .3,
                "ESPN requires confirmation in the signed-in browser before collection continues",
            )
            league_payload, pro_team_payload = self._authenticated_espn_read(
                collector, request, host_id
            )
        captured_at = self._now()
        activity = self._activity_adapter(
            league_payload,
            captured_at=captured_at,
        )
        validation.stage = "ESPN league and schedule normalization"
        if team_count is None:
            team_count = _espn_team_count(
                league_payload, request.expected_team_count
            )
        host = self._host_adapter(
            league_payload,
            pro_team_payload,
            captured_at=captured_at,
            expected_team_count=team_count,
        )
        _validate_host(host, request, host_id, team_count)
        validate_host_scoring(host, request.scoring)
        nfl_schedule = self._schedule_adapter(
            pro_team_payload, season=request.season, captured_at=captured_at
        )
        if not request.use_fantasypros:
            _emit(
                progress,
                WeeklyCollectionStage.COLLECTING_LEAGUE,
                .33,
                f"Found {team_count} teams and complete ESPN rosters",
            )

        validation.stage = "weekly source-plan construction"
        plan = self._remaining_plan(request, host)
        yahoo_task = _yahoo_projection_task(plan, request)
        verifier = getattr(collector, "verify_yahoo_scoring", None)
        if not callable(verifier):
            raise BrowserCaptureError(
                "Yahoo scoring verification requires the persistent browser collector"
            )
        providers = "FantasyPros" if request.use_fantasypros else "ESPN"
        if request.use_broad_consensus:
            providers = "independent ESPN and public projection publishers"
        _emit(
            progress,
            (
                WeeklyCollectionStage.COLLECTING_FANTASYPROS
                if request.use_fantasypros
                else WeeklyCollectionStage.COLLECTING_ESPN
            ),
            .36,
            f"Preparing visible {providers} projections",
        )
        _emit(
            progress,
            WeeklyCollectionStage.COLLECTING_YAHOO,
            .38,
            "Checking the selected Yahoo league's reception scoring",
        )
        if verifier(yahoo_task, request.yahoo_projection_league_url) != request.scoring:
            raise ValueError("Yahoo scoring verification returned the wrong profile")
        validation.stage = "required projection and ECR capture"
        required_plan, public_plan = split_optional_public(plan)
        bindings = runtime_bindings(
            required_plan, host_id, request.yahoo_projection_league_url
        )
        rows = collector.collect(
            required_plan,
            options,
            cancellation=token,
            sign_in_gate=self._gate,
            navigation_bindings=bindings,
        )
        validation.stage = "optional public projection capture"
        public_rows, public_tasks = capture_optional_public(
            collector,
            public_plan,
            options=options,
            token=token,
            gate=self._gate,
            cancelled=cancelled,
            progress=progress,
        )
        rows = (*rows, *public_rows)
        captured_plan = CapturePlan((*required_plan.tasks, *public_tasks))
        validation.stage = "projection and ECR artifact verification"
        projections, ecr = _source_artifacts(
            rows,
            captured_plan,
            request.scoring,
            require_ecr=request.use_fantasypros,
        )
        if request.use_broad_consensus:
            completion_stage = WeeklyCollectionStage.COLLECTING_PUBLIC
        elif request.use_fantasypros:
            completion_stage = WeeklyCollectionStage.COLLECTING_FANTASYPROS
        else:
            completion_stage = WeeklyCollectionStage.COLLECTING_ESPN
        _emit(
            progress,
            completion_stage,
            .65,
            "Projection page capture is complete; validating provider evidence locally",
        )

        public_player_data = self._collect_public_player_data(
            request, root, cancelled, progress
        )

        previous = _previous_identities(root / _IDENTITY_FILE)
        validation.stage = "cross-source identity and weekly evidence assembly"
        _emit(progress, WeeklyCollectionStage.NORMALIZING, .7,
              "Matching player identities and validating complete weekly evidence")
        if not request.use_fantasypros:
            validation.stage = "independent weekly model assembly"
            _emit(
                progress,
                WeeklyCollectionStage.BUILDING,
                .82,
                "Building the transparent local power and playoff engine",
            )
            independent, projections = assemble_with_public_fallback(
                projections,
                lambda candidate, consensus: self._independent_assembler(
                    host_snapshot=host,
                    projection_artifacts=candidate,
                    nfl_schedule=nfl_schedule,
                    scoring=request.scoring,
                    expected_team_count=team_count,
                    previous_identities=previous,
                    broad_consensus=consensus,
                ),
                broad_consensus=request.use_broad_consensus,
            )
            if not isinstance(independent, IndependentWeeklyEngine):
                raise ValueError(
                    "independent_assembler must return IndependentWeeklyEngine"
                )
            _emit(
                progress,
                WeeklyCollectionStage.BUILDING,
                .9,
                "Using forecast inputs from "
                + ", ".join(
                    select_projection_sources(
                        {row.provider.value for row in projections},
                        broad_consensus=request.use_broad_consensus,
                        fantasypros_available=False,
                    ).providers
                ),
            )
            _emit(
                progress,
                WeeklyCollectionStage.BUILDING,
                .95,
                "Independent weekly engine is complete",
            )
            bundle = self._attach_player_profiles(
                independent.bundle,
                independent.identities,
                independent.player_lab_projections,
                public_player_data,
                progress,
            )
            save_identity_registry(independent.identities, root / _IDENTITY_FILE)
            history_capture, history_binding = self._history_adapter(
                activity,
                independent,
                bundle,
                bundle_captured_at=self._now(),
            )
            return WeeklyCollectionPublication(
                bundle,
                history_capture,
                history_binding,
            )

        if league is None:
            raise ValueError("FantasyPros collection did not produce a league artifact")
        assembled, projections = assemble_with_public_fallback(
            projections,
            lambda candidate, consensus: self._assembler(
                host_snapshot=host,
                fantasypros_league=league,
                projection_artifacts=candidate,
                ecr_artifacts=ecr,
                nfl_schedule=nfl_schedule,
                analyzer_bundle=BundleFingerprint(
                    league.bundle_url, league.bundle_sha256
                ),
                response_schema_sha256=response_schema_digest(),
                scoring=request.scoring,
                expected_team_count=team_count,
                previous_identities=previous,
                broad_consensus=consensus,
            ),
            broad_consensus=request.use_broad_consensus,
        )
        if not isinstance(assembled, AssembledWeeklyEvidence):
            raise ValueError("assembler must return AssembledWeeklyEvidence")
        forecast_providers = select_projection_sources(
            {row.provider.value for row in projections},
            broad_consensus=request.use_broad_consensus,
            fantasypros_available=True,
        ).providers
        _emit(
            progress,
            WeeklyCollectionStage.NORMALIZING,
            .8,
            "Using forecast inputs from " + ", ".join(forecast_providers),
        )
        validation.stage = "FantasyPros power calibration setup"
        primary_team = _primary_team(assembled, metadata)
        capture_context = CalibrationCaptureContext(
            collector, options, self._gate, token,
            request.season, request.week, self._now,
            allow_surrogate_power=request.allow_surrogate_power,
        )
        callbacks = self._calibration_factory(assembled, primary_team, capture_context)
        if not isinstance(callbacks, CalibrationCallbacks):
            raise ValueError("calibration_factory must return CalibrationCallbacks")
        validation.stage = "weekly model calibration and build"
        with TemporaryDirectory(prefix=".weekly-refresh-", dir=root) as staging:
            result = self._refresher(
                assembled.evidence,
                formula_path=root / "methodology" / "strength-formula.json",
                bundle_directory=Path(staging),
                calibrate=callbacks.calibrate,
                verify_reuse=callbacks.verify_reuse,
                allow_surrogate_power=request.allow_surrogate_power,
                progress=lambda value: _refresh_progress(progress, value),
                cancelled=cancelled,
            )
        _check_cancelled(cancelled)
        bundle = getattr(result, "bundle", None)
        if not isinstance(bundle, EngineBundle):
            raise ValueError("refresher must return a weekly refresh result")
        bundle = self._attach_player_profiles(
            bundle,
            assembled.identities,
            assembled.player_lab_projections,
            public_player_data,
            progress,
        )
        history_capture, history_binding = self._history_adapter(
            activity,
            assembled,
            bundle,
            bundle_captured_at=self._now(),
        )
        publication = WeeklyCollectionPublication(
            bundle,
            history_capture,
            history_binding,
        )
        save_identity_registry(assembled.identities, root / _IDENTITY_FILE)
        return publication

    def _collect_public_player_data(self, request, root, cancelled, progress):
        _emit(
            progress,
            WeeklyCollectionStage.COLLECTING_PUBLIC,
            .66,
            "Collecting public player history, depth charts, injuries, and trends",
        )
        _check_cancelled(cancelled)
        if not request.refresh_public_player_data:
            try:
                cached = load_public_player_week(root, request.season, request.week)
            except PublicPlayerCacheError:
                cached = None
                _emit(
                    progress,
                    WeeklyCollectionStage.COLLECTING_PUBLIC,
                    .67,
                    "The local Player Lab cache was invalid; collecting a fresh copy",
                )
            if cached is not None and _public_profile_cacheable(cached):
                _emit(
                    progress,
                    WeeklyCollectionStage.COLLECTING_PUBLIC,
                    .69,
                    "Reusing public Player Lab data already collected for this NFL week; "
                    + _public_profile_source_summary(cached),
                )
                return cached
            if cached is not None:
                _emit(
                    progress,
                    WeeklyCollectionStage.COLLECTING_PUBLIC,
                    .67,
                    "The cached Player Lab snapshot was incomplete; retrying its public sources",
                )
        try:
            public_data = self._public_player_reader(
                request.season,
                as_of_week=request.week,
                cancelled=cancelled,
                clock=self._now,
            )
        except PublicPlayerDataCancelled:
            raise
        except PublicPlayerDataError:
            _emit(
                progress,
                WeeklyCollectionStage.COLLECTING_PUBLIC,
                .69,
                "Player history is unavailable; projections and trade analysis will still be published",
            )
            return None
        if not isinstance(public_data, PublicPlayerDataSnapshot):
            raise ValueError(
                "public_player_reader must return a PublicPlayerDataSnapshot"
            )
        if not _public_profile_cacheable(public_data):
            cache_message = (
                "Public player profiles are ready with incomplete source coverage; "
                "unavailable sources will retry automatically on the next scan"
            )
        else:
            try:
                save_public_player_week(
                    root, request.season, request.week, public_data
                )
            except PublicPlayerCacheError:
                cache_message = (
                    "Public player profiles are ready, but this week's local reuse cache "
                    "could not be saved"
                )
            else:
                cache_message = (
                    "Public player profiles are ready and cached for this NFL week; "
                    + _public_profile_source_summary(public_data)
                )
        _emit(
            progress,
            WeeklyCollectionStage.COLLECTING_PUBLIC,
            .69,
            cache_message,
        )
        return public_data

    def _attach_player_profiles(
        self, bundle, identities, player_lab_projections, public_data, progress
    ):
        projected_bundle = replace(
            bundle, player_lab_projections=player_lab_projections
        )
        if public_data is None:
            return projected_bundle
        try:
            profiles = self._profile_builder(
                league_snapshot_id=projected_bundle.state.snapshot_id,
                as_of_week=projected_bundle.state.first_remaining_week,
                identities=identities,
                public_data=public_data,
            )
            if not isinstance(profiles, PlayerProfileSnapshot):
                raise ValueError("profile_builder must return a PlayerProfileSnapshot")
            enriched = replace(
                projected_bundle,
                player_profiles=profiles,
            )
        except (PlayerProfileMaterializationError, ValueError):
            _emit(
                progress,
                WeeklyCollectionStage.BUILDING,
                .98,
                "Public profile identities could not be matched safely; trade analysis remains ready",
            )
            return projected_bundle
        _emit(
            progress,
            WeeklyCollectionStage.BUILDING,
            .98,
            f"Player Lab retained {len(profiles.players)} public player profiles",
        )
        return enriched

    @staticmethod
    def _authenticated_espn_read(collector, request, host_id):
        reader = getattr(collector, "read_authenticated_espn_json", None)
        if not callable(reader):
            raise BrowserCaptureError(
                "authenticated ESPN reads require the persistent browser collector"
            )
        task = PageCaptureTask(
            "espn",
            request.season,
            request.week,
            "visible_table",
            "https://fantasy.espn.com/football/players/projections",
            projection=ProjectionTableSpec("weekly", request.scoring, ("ALL",)),
        )
        return reader(
            task,
            espn_projection_url(host_id),
            request.season,
            host_id,
        )

    def _remaining_plan(self, request, host):
        regular_end = host.playoff_rules.regular_season_end_week
        if request.week > regular_end:
            raise ValueError("weekly collection requires remaining regular-season games")
        builder = (
            self._plan_builder
            if request.use_fantasypros
            else self._independent_plan_builder
        )
        complete = builder(
            season=request.season,
            as_of_week=request.week,
            remaining_weeks=range(request.week, regular_end + 1),
            scoring=request.scoring,
            player_positions=(player.position for player in host.players),
            include_future_weekly=request.include_future_weekly,
            broad_consensus=request.use_broad_consensus,
        )
        if not isinstance(complete, CapturePlan):
            raise ValueError("plan_builder must return a CapturePlan")
        return CapturePlan(
            task
            for task in complete.tasks
            if task.kind is not CaptureKind.LEAGUE_SOURCE
        )


@dataclass(frozen=True, slots=True)
class _CancellationToken:
    check: Callable[[], bool]

    def is_set(self) -> bool:
        return _cancelled_value(self.check)


@dataclass(slots=True)
class _ValidationContext:
    stage: str
    league: FantasyProsLeagueArtifact | None = None


def _league_artifact(rows, task):
    if not isinstance(rows, tuple) or len(rows) != 1:
        raise ValueError("league capture must return exactly one artifact")
    league = rows[0]
    if not isinstance(league, FantasyProsLeagueArtifact):
        raise ValueError("league capture returned the wrong artifact type")
    validate_artifact_for_task(league, task)
    return league


def _discovered_team_count(league, expected):
    team_count = getattr(league, "team_count", None)
    if type(team_count) is not int or not 2 <= team_count <= 32:
        raise WeeklyCollectionError(
            "The signed-in FantasyPros league has an unsupported number of teams."
        )
    if expected is not None and expected != team_count:
        raise WeeklyCollectionError(
            f"The signed-in FantasyPros league has {team_count} teams, not the "
            f"{expected} provided by this collection request."
        )
    return team_count


def _espn_team_count(payload, expected):
    if not isinstance(payload, Mapping):
        raise ValueError("ESPN league payload must be an object")
    teams = payload.get("teams")
    if not isinstance(teams, list) or not 2 <= len(teams) <= 32:
        raise WeeklyCollectionError(
            "The ESPN league has an unsupported or incomplete team list."
        )
    team_count = len(teams)
    if expected is not None and expected != team_count:
        raise WeeklyCollectionError(
            f"The ESPN league has {team_count} teams, not the {expected} provided "
            "by this collection request."
        )
    return team_count


def _validate_host(host, request, host_id, team_count):
    if (
        getattr(host, "source_provider", "").casefold() != "espn"
        or getattr(host, "source_league_id", None) != host_id
        or getattr(host, "season", None) != request.season
        or getattr(host, "first_remaining_week", None) != request.week
        or getattr(host, "expected_team_count", None) != team_count
    ):
        raise ValueError("ESPN host snapshot does not match the capture request")


def _source_artifacts(rows, plan, scoring, *, require_ecr=True):
    if not isinstance(require_ecr, bool):
        raise ValueError("require_ecr must be a boolean")
    try:
        artifacts = tuple(rows)
    except TypeError:
        raise ValueError("browser collector returned invalid artifacts") from None
    by_id = {getattr(row, "task_id", None): row for row in artifacts}
    if len(by_id) != len(artifacts) or set(by_id) != {task.task_id for task in plan.tasks}:
        raise ValueError("browser collector did not return exact plan coverage")
    projections, ecr = [], []
    for task in plan.tasks:
        artifact = by_id[task.task_id]
        validate_artifact_for_task(artifact, task)
        if getattr(artifact, "scoring", None) != scoring:
            raise ValueError("captured projection scoring does not match the request")
        if isinstance(artifact, GenericTableArtifact):
            projections.append(artifact)
        elif isinstance(artifact, FantasyProsECRArtifact):
            ecr.append(artifact)
        else:
            raise ValueError("remaining source plan returned an unexpected artifact")
    if not projections or (require_ecr and not ecr) or (not require_ecr and ecr):
        raise ValueError("remaining source capture is incomplete")
    return tuple(projections), tuple(ecr)


def _yahoo_projection_task(plan, request):
    tasks = tuple(
        task
        for task in plan.tasks
        if (
            isinstance(task, PageCaptureTask)
            and task.kind is CaptureKind.VISIBLE_TABLE
            and task.provider.value == "yahoo"
            and task.projection is not None
        )
    )
    if not tasks:
        raise ValueError("remaining source plan has no Yahoo projection task")
    if any(
        task.season != request.season
        or task.projection.scoring != request.scoring
        for task in tasks
    ):
        raise ValueError("Yahoo projection tasks do not match the collection request")
    current_week = tuple(
        task
        for task in tasks
        if task.week == request.week
        and task.projection.horizon.value == "weekly"
    )
    if not current_week:
        raise ValueError(
            "remaining source plan has no current-week Yahoo projection task"
        )
    return min(
        current_week,
        key=lambda task: (task.projection.position_scope, task.task_id),
    )


def _primary_team(assembled, metadata):
    if not isinstance(assembled, AssembledWeeklyEvidence):
        raise ValueError("assembler must return AssembledWeeklyEvidence")
    provider_team_id = metadata.get("team_id")
    provider_id("FantasyPros primary team_id", provider_team_id)
    matches = [
        canonical for canonical, current in assembled.fantasypros_team_ids.items()
        if current == provider_team_id
    ]
    if len(matches) != 1:
        raise ValueError("FantasyPros primary team cannot be mapped exactly")
    return matches[0]


def _previous_identities(path):
    return load_identity_registry(path) if path.exists() else None


def _refresh_progress(callback, value):
    if not isinstance(value, RefreshProgress):
        raise ValueError("refresh progress is invalid")
    building = value.stage in {
        RefreshStage.BUILDING_ENGINE, RefreshStage.SAVING, RefreshStage.COMPLETE,
    }
    stage = WeeklyCollectionStage.BUILDING if building else WeeklyCollectionStage.CALIBRATING
    fraction = .72 + min(value.fraction, 1) * .25
    _emit(callback, stage, fraction, value.message)


def _public_profile_source_summary(snapshot):
    statuses = [row.availability.value for row in snapshot.provenance]
    observed = statuses.count("observed")
    unavailable = statuses.count("unavailable")
    unpublished = statuses.count("not_published")
    total = len(statuses)
    if observed == total:
        return f"all {total} sources were captured"
    details = [f"{observed} of {total} sources were captured"]
    if unpublished:
        details.append(f"{unpublished} not yet published")
    if unavailable:
        details.append(f"{unavailable} unavailable")
    return "; ".join(details)


def _public_profile_cacheable(snapshot):
    return all(
        row.availability is not DataAvailability.UNAVAILABLE
        for row in snapshot.provenance
    )


def create_production_weekly_collection_workflow(
    extension_bridge=None,
) -> ProductionWeeklyCollectionWorkflow:
    """Construct the refresh workflow used by the installed local application."""

    from ._extension_capture import ExtensionCaptureBackend
    from .extension_bridge import ExtensionCommandBridge

    bridge = extension_bridge or ExtensionCommandBridge()
    collector = BrowserCollector(ExtensionCaptureBackend(bridge))
    gate = InteractiveSignInGate()
    return ProductionWeeklyCollectionWorkflow(
        sign_in_gate=gate,
        calibration_factory=BrowserCalibrationFactory(),
        collector=collector,
    )


def _emit(callback, stage, fraction, message):
    callback(WeeklyCollectionProgress(stage, fraction, message))


def _check_cancelled(check):
    if _cancelled_value(check):
        raise WeeklyCollectionError("Weekly collection was cancelled.")


def _cancelled_value(check):
    if not callable(check):
        raise ValueError("cancelled must be callable")
    value = check()
    if not isinstance(value, bool):
        raise ValueError("cancelled must return a boolean")
    return value


__all__ = (
    "CalibrationCallbacks", "EspnFreeReadClient", "InteractiveSignInGate",
    "ProductionWeeklyCollectionWorkflow", "create_production_weekly_collection_workflow",
)
