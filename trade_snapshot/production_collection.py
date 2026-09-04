"""Production-only weekly source collection; trade evaluation remains offline."""

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
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
    BrowserCaptureTimeout,
    BrowserCollector,
    ProjectionNotPublished,
    SignInGate,
    YahooScoringError,
    YahooScoringMismatch,
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
from .engine_bundle import EngineBundle
from .ensemble import EnsembleConfig
from .espn_activity import espn_activity_capture
from .espn_free_read import (
    EspnFreeReadClient,
    EspnFreeReadError,
    EspnUnauthorizedError,
)
from .espn_league import espn_host_league_snapshot
from .history_ingest import canonicalize_espn_history
from .identity_io import load_identity_registry, save_identity_registry
from .league_binding import get_or_create_league_binding
from .nfl_schedule import NFL_REGULAR_SEASON_WEEKS, parse_espn_pro_team_schedule
from .methodology import default_projection_ensemble
from .projection_source import (
    ProjectionAttemptReason,
    ProjectionAttemptStatus,
    ProjectionSourceAttempt,
)
from .raw_capture_archive import (
    archive_private_league_capture,
    archive_public_captures,
)
from .production_calibration import (
    BrowserCalibrationFactory,
    CalibrationCallbacks,
    CalibrationCaptureContext,
    InteractiveSignInGate,
)
from .calibration_workflow import CalibrationNotExact
from .source_plan import build_weekly_source_plan
from .waiver_pool import required_waiver_positions
from .weekly_assembly import AssembledWeeklyEvidence, assemble_weekly_refresh_evidence
from .weekly_collection import (
    WeeklyCollectionError,
    WeeklyCollectionPublication,
    WeeklyCollectionProgress,
    WeeklyCollectionRequest,
    WeeklyCollectionStage,
    WeeklyHistoryAttempt,
    WeeklyHistoryReason,
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
_LEAGUE_BINDINGS_FILE = "league-bindings.json"


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
        refresher: Callable = refresh_weekly_engine,
        activity_adapter: Callable = espn_activity_capture,
        history_adapter: Callable = canonicalize_espn_history,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        dependencies = (
            calibration_factory, host_adapter, schedule_adapter,
            plan_builder, assembler, refresher, activity_adapter, history_adapter, now,
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
                    request, root, options, token, progress, cancelled, collector
                )
        except WeeklyCollectionError:
            raise
        except (BrowserCaptureCancelled, RefreshCancelled):
            raise WeeklyCollectionError("Weekly collection was cancelled.") from None
        except EspnFreeReadError as error:
            raise WeeklyCollectionError(str(error)) from None
        except YahooScoringError as error:
            raise WeeklyCollectionError(str(error)) from None
        except BrowserCaptureDependencyError:
            raise WeeklyCollectionError(
                "Connect the Fantasy Trade Evaluator browser extension, then collect "
                "the week again. No weekly bundle was published."
            ) from None
        except BrowserCaptureError:
            raise WeeklyCollectionError(
                "A signed-in source page could not be captured. No weekly bundle was published."
            ) from None
        except CalibrationRequired:
            raise WeeklyCollectionError(
                "A FantasyPros-style formula could not be fitted and blind-holdout "
                "validated for this week."
            ) from None
        except CalibrationNotExact as error:
            if error.surrogate_eligible and not request.allow_surrogate_power:
                message = (
                    "The fitted formula did not pass the blind-holdout validation gate, "
                    "so no bundle was published. Select the explicit SURROGATE option "
                    "to publish the measured approximation."
                )
            else:
                message = (
                    "Calibration met neither the blind-holdout validation gate nor the "
                    "healthy SURROGATE publication gate. No weekly bundle was published."
                )
            raise WeeklyCollectionError(message) from None
        except ValueError:
            raise WeeklyCollectionError(
                "Weekly source data did not pass strict validation. No weekly bundle was published."
            ) from None
        finally:
            if callable(reset_gate):
                reset_gate()

    def _collect(self, request, root, options, token, progress, cancelled, collector):
        _emit(progress, WeeklyCollectionStage.COLLECTING_LEAGUE, .05,
              "Reading FantasyPros through your connected browser extension")
        league_task = PageCaptureTask(
            "fantasypros", request.season, request.week, "league_source", _TRADE_ANALYZER_URL
        )
        league_rows = collector.collect(
            CapturePlan((league_task,)), options, cancellation=token, sign_in_gate=self._gate
        )
        league = _league_artifact(league_rows, league_task)
        team_count = _discovered_team_count(league, request.expected_team_count)
        _emit(
            progress,
            WeeklyCollectionStage.COLLECTING_LEAGUE,
            .15,
            f"Found {team_count} teams and complete FantasyPros rosters",
        )
        metadata = league_metadata(league)
        host_id = espn_host_id(metadata, request.host_league_url)

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
        try:
            activity = self._activity_adapter(
                league_payload,
                captured_at=captured_at,
            )
        except ValueError:
            activity = None
            history_attempt = WeeklyHistoryAttempt.unavailable(
                WeeklyHistoryReason.ACTIVITY_SCHEMA_UNSUPPORTED,
                captured_at,
            )
        except Exception:
            activity = None
            history_attempt = WeeklyHistoryAttempt.unavailable(
                WeeklyHistoryReason.ACTIVITY_UNAVAILABLE,
                captured_at,
            )
        else:
            history_attempt = None
        host = self._host_adapter(
            league_payload,
            pro_team_payload,
            captured_at=captured_at,
            expected_team_count=team_count,
        )
        _validate_host(host, request, host_id, team_count)
        validate_host_scoring(host, request.scoring)
        league_binding_id = get_or_create_league_binding(
            root / _LEAGUE_BINDINGS_FILE,
            host.source_provider,
            host.source_league_id,
        )
        archive_private_league_capture(
            root,
            league_binding_id,
            (league_task, league),
        )
        nfl_schedule = self._schedule_adapter(
            pro_team_payload, season=request.season, captured_at=captured_at
        )

        plan = self._remaining_plan(request, host)
        yahoo_task = _yahoo_projection_task(plan, request)
        verifier = getattr(collector, "verify_yahoo_scoring", None)
        if not callable(verifier):
            raise BrowserCaptureError(
                "Yahoo scoring verification requires the persistent browser collector"
            )
        _emit(
            progress,
            WeeklyCollectionStage.COLLECTING_FANTASYPROS,
            .36,
            "Preparing current ECR, every remaining FantasyPros week, and "
            "available ESPN and Yahoo projections",
        )
        _emit(
            progress,
            WeeklyCollectionStage.COLLECTING_YAHOO,
            .38,
            "Checking the selected Yahoo league's reception scoring",
        )
        unavailable_providers = {}
        try:
            yahoo_scoring = verifier(
                yahoo_task, request.yahoo_projection_league_url
            )
        except YahooScoringMismatch:
            raise
        except (BrowserCaptureCancelled, BrowserCaptureDependencyError):
            raise
        except BrowserCaptureError:
            unavailable_providers["yahoo"] = (
                ProjectionAttemptReason.PROVIDER_PAGE_UNAVAILABLE,
                self._now(),
            )
        else:
            if yahoo_scoring != request.scoring:
                raise YahooScoringMismatch(
                    "Yahoo scoring verification returned the wrong profile."
                )
        bindings = runtime_bindings(
            plan, host_id, request.yahoo_projection_league_url
        )
        rows, projection_attempts = _collect_remaining_sources(
            collector,
            plan,
            options,
            token,
            self._gate,
            bindings,
            first_remaining_week=request.week,
            attempt_clock=self._now,
            unavailable_providers=unavailable_providers,
        )
        projections, ecr = _source_artifacts(
            rows, plan, request.scoring, projection_attempts
        )
        archive_public_captures(
            root,
            _capture_pairs(plan, (*projections, *ecr)),
        )
        ensemble_config = _available_projection_ensemble(projections)
        _emit(progress, WeeklyCollectionStage.COLLECTING_YAHOO, .65,
              "Projection page attempts completed; unavailable pages were recorded explicitly")

        previous = _previous_identities(root / _IDENTITY_FILE)
        _emit(progress, WeeklyCollectionStage.NORMALIZING, .7,
              "Matching player identities and validating complete weekly evidence")
        assembled = self._assembler(
            host_snapshot=host,
            fantasypros_league=league,
            projection_artifacts=projections,
            ecr_artifacts=ecr,
            nfl_schedule=nfl_schedule,
            analyzer_bundle=BundleFingerprint(league.bundle_url, league.bundle_sha256),
            response_schema_sha256=response_schema_digest(),
            scoring=request.scoring,
            expected_team_count=team_count,
            previous_identities=previous,
            league_binding_id=league_binding_id,
            ensemble_config=ensemble_config,
            projection_source_attempts=projection_attempts,
        )
        primary_team = _primary_team(assembled, metadata)
        capture_context = CalibrationCaptureContext(
            collector, options, self._gate, token,
            request.season, request.week, self._now,
            allow_surrogate_power=request.allow_surrogate_power,
        )
        callbacks = self._calibration_factory(assembled, primary_team, capture_context)
        if not isinstance(callbacks, CalibrationCallbacks):
            raise ValueError("calibration_factory must return CalibrationCallbacks")
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
        if activity is None:
            history_capture = history_binding = None
        else:
            try:
                history_capture, history_binding = self._history_adapter(
                    activity,
                    assembled,
                    bundle,
                    bundle_captured_at=self._now(),
                )
            except ValueError:
                history_capture = history_binding = None
                history_attempt = _failed_history_attempt(
                    activity,
                    captured_at,
                    WeeklyHistoryReason.CANONICALIZATION_FAILED,
                )
            except Exception:
                history_capture = history_binding = None
                history_attempt = _failed_history_attempt(
                    activity,
                    captured_at,
                    WeeklyHistoryReason.HISTORY_PROCESSING_UNAVAILABLE,
                )
        publication = WeeklyCollectionPublication(
            bundle,
            history_capture,
            history_binding,
            history_attempt,
        )
        save_identity_registry(assembled.identities, root / _IDENTITY_FILE)
        return publication

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
        rostered_positions = tuple(player.position for player in host.players)
        lineup_positions = required_waiver_positions(
            host.roster_rules.starting_lineup_slots,
        )
        complete = self._plan_builder(
            season=request.season,
            as_of_week=request.week,
            remaining_weeks=(
                week
                for week in NFL_REGULAR_SEASON_WEEKS
                if week >= request.week
            ),
            scoring=request.scoring,
            player_positions=(*rostered_positions, *lineup_positions),
            include_future_weekly=request.include_future_weekly,
        )
        if not isinstance(complete, CapturePlan):
            raise ValueError("plan_builder must return a CapturePlan")
        return CapturePlan(
            task for task in complete.tasks if task.kind is not CaptureKind.LEAGUE_SOURCE
        )


@dataclass(frozen=True, slots=True)
class _CancellationToken:
    check: Callable[[], bool]

    def is_set(self) -> bool:
        return _cancelled_value(self.check)


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


def _validate_host(host, request, host_id, team_count):
    if (
        getattr(host, "source_provider", "").casefold() != "espn"
        or getattr(host, "source_league_id", None) != host_id
        or getattr(host, "season", None) != request.season
        or getattr(host, "first_remaining_week", None) != request.week
        or getattr(host, "expected_team_count", None) != team_count
    ):
        raise ValueError("ESPN host snapshot does not match the capture request")


def _collect_remaining_sources(
    collector,
    plan,
    options,
    token,
    sign_in_gate,
    bindings,
    *,
    first_remaining_week,
    attempt_clock,
    unavailable_providers=None,
):
    artifacts = []
    attempts = []
    provider_failures = dict(unavailable_providers or {})
    if not set(provider_failures) <= {"espn", "yahoo"}:
        raise ValueError("only optional projection providers may be unavailable")
    for task in plan.tasks:
        provider_failure = provider_failures.get(task.provider.value)
        if provider_failure is not None:
            if not _optional_projection_task(task):
                raise ValueError("only optional projection providers may be unavailable")
            reason, attempted_at = provider_failure
            attempts.append(_projection_attempt(
                task,
                ProjectionAttemptStatus.UNAVAILABLE,
                reason,
                attempted_at=attempted_at,
            ))
            continue
        task_plan = CapturePlan((task,))
        task_bindings = (
            {task.task_id: bindings[task.task_id]}
            if task.task_id in bindings
            else None
        )
        try:
            captured = collector.collect(
                task_plan,
                options,
                cancellation=token,
                sign_in_gate=sign_in_gate,
                navigation_bindings=task_bindings,
            )
        except ProjectionNotPublished:
            if not _skippable_unpublished_task(task, first_remaining_week):
                raise
            attempts.append(_projection_attempt(
                task,
                ProjectionAttemptStatus.NOT_PUBLISHED,
                ProjectionAttemptReason.SOURCE_NOT_PUBLISHED,
                attempted_at=attempt_clock(),
            ))
            continue
        except (BrowserCaptureCancelled, BrowserCaptureDependencyError):
            raise
        except BrowserCaptureTimeout:
            if not _optional_projection_task(task):
                raise
            attempts.append(_projection_attempt(
                task,
                ProjectionAttemptStatus.UNAVAILABLE,
                ProjectionAttemptReason.PROVIDER_PAGE_UNAVAILABLE,
                attempted_at=attempt_clock(),
            ))
            continue
        except BrowserCaptureError:
            if not _optional_projection_task(task):
                raise
            attempts.append(_projection_attempt(
                task,
                ProjectionAttemptStatus.UNAVAILABLE,
                ProjectionAttemptReason.PROVIDER_LAYOUT_UNSUPPORTED,
                attempted_at=attempt_clock(),
            ))
            continue
        if not isinstance(captured, tuple) or len(captured) != 1:
            raise ValueError("one source task must return exactly one artifact")
        artifact = captured[0]
        artifacts.append(artifact)
        if _projection_task(task):
            attempts.append(_projection_attempt(
                task,
                ProjectionAttemptStatus.CAPTURED,
                ProjectionAttemptReason.CAPTURED,
                attempted_at=datetime.fromisoformat(
                    artifact.captured_at.replace("Z", "+00:00")
                ),
                artifact=artifact,
            ))
    return tuple(artifacts), tuple(attempts)


def _source_artifacts(rows, plan, scoring, projection_attempts):
    try:
        artifacts = tuple(rows)
    except TypeError:
        raise ValueError("browser collector returned invalid artifacts") from None
    try:
        attempts = tuple(projection_attempts)
    except TypeError:
        raise ValueError("projection attempts were invalid") from None
    if any(not isinstance(row, ProjectionSourceAttempt) for row in attempts):
        raise ValueError("projection attempts were invalid")
    projection_tasks = {
        task.task_id: task for task in plan.tasks if _projection_task(task)
    }
    if {row.task_id for row in attempts} != set(projection_tasks) or len(attempts) != len(
        projection_tasks
    ):
        raise ValueError("projection attempts did not cover the exact projection plan")
    if any(
        not _attempt_matches_task(row, projection_tasks[row.task_id])
        for row in attempts
    ):
        raise ValueError("projection attempts did not match their requested dimensions")
    captured_projection_ids = {
        row.task_id
        for row in attempts
        if row.status is ProjectionAttemptStatus.CAPTURED
    }
    expected_artifact_ids = captured_projection_ids | {
        task.task_id for task in plan.tasks if not _projection_task(task)
    }
    by_id = {getattr(row, "task_id", None): row for row in artifacts}
    if len(by_id) != len(artifacts) or set(by_id) != expected_artifact_ids:
        raise ValueError("browser collector did not return exact plan coverage")
    projections, ecr = [], []
    for task in plan.tasks:
        if task.task_id not in by_id:
            continue
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
    if not projections or not ecr:
        raise ValueError("remaining source capture is incomplete")
    return tuple(projections), tuple(ecr)


def _capture_pairs(plan, artifacts):
    """Reattach validated artifacts to their capture tasks for local archival."""

    artifacts = tuple(artifacts)
    by_task = {row.task_id: row for row in artifacts}
    if len(by_task) != len(artifacts):
        raise ValueError("raw capture archive input repeats a capture task")
    tasks = {task.task_id: task for task in plan.tasks}
    if not set(by_task).issubset(tasks):
        raise ValueError("raw capture archive input is outside the capture plan")
    return tuple(
        (tasks[task_id], artifact)
        for task_id, artifact in sorted(by_task.items())
    )


def _projection_task(task):
    return (
        isinstance(task, PageCaptureTask)
        and task.kind is CaptureKind.VISIBLE_TABLE
        and task.projection is not None
    )


def _optional_projection_task(task):
    return _projection_task(task) and task.provider.value in {"espn", "yahoo"}


def _skippable_unpublished_task(task, first_remaining_week):
    return (
        _projection_task(task)
        and task.provider.value == "fantasypros"
        and task.projection.horizon.value == "weekly"
        and task.week > first_remaining_week
    )


def _projection_attempt(task, status, reason, *, attempted_at, artifact=None):
    if not _projection_task(task):
        raise ValueError("projection attempt requires a projection task")
    return ProjectionSourceAttempt(
        task_id=task.task_id,
        provider=task.provider,
        season=task.season,
        week=task.week,
        horizon=task.projection.horizon,
        scoring=task.projection.scoring,
        position_scope=task.projection.position_scope,
        attempted_at=attempted_at,
        status=status,
        reason_code=reason,
        artifact_id=None if artifact is None else artifact.artifact_id,
    )


def _attempt_matches_task(attempt, task):
    return (
        attempt.provider is task.provider
        and attempt.season == task.season
        and attempt.week == task.week
        and attempt.horizon is task.projection.horizon
        and attempt.scoring == task.projection.scoring
        and attempt.position_scope == task.projection.position_scope
    )


def _available_projection_ensemble(projections):
    providers = {row.provider.value for row in projections}
    baseline = default_projection_ensemble()
    retained = tuple(
        row for row in baseline.provider_weights if row.provider in providers
    )
    if len(retained) < baseline.minimum_observed_sources:
        raise WeeklyCollectionError(
            "At least two projection providers must publish usable data before this "
            "week can be calculated. No weekly bundle was published."
        )
    return EnsembleConfig(
        provider_weights=retained,
        minimum_observed_sources=baseline.minimum_observed_sources,
        position_stddev_floors=baseline.position_stddev_floors,
    )


def _yahoo_projection_task(plan, request):
    tasks = tuple(
        task for task in plan.tasks
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
        task for task in tasks
        if task.week == request.week and task.projection.horizon.value == "weekly"
    )
    if not current_week:
        raise ValueError("remaining source plan has no current-week Yahoo projection task")
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


def _failed_history_attempt(activity, attempted_at, reason):
    return WeeklyHistoryAttempt(
        status="unavailable",
        reason_code=reason,
        attempted_at=attempted_at,
        source_provider="espn",
        returned_transaction_count=getattr(
            activity, "returned_transaction_count", None
        ),
        normalized_transaction_count=None,
        transaction_limit=getattr(activity, "transaction_limit", None),
        transactions_complete=getattr(
            activity, "transactions_complete", None
        ),
    )


def _refresh_progress(callback, value):
    if not isinstance(value, RefreshProgress):
        raise ValueError("refresh progress is invalid")
    building = value.stage in {
        RefreshStage.BUILDING_ENGINE, RefreshStage.SAVING, RefreshStage.COMPLETE,
    }
    stage = WeeklyCollectionStage.BUILDING if building else WeeklyCollectionStage.CALIBRATING
    fraction = .72 + min(value.fraction, 1) * .25
    _emit(callback, stage, fraction, value.message)


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
