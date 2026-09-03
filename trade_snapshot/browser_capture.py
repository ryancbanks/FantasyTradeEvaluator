"""Sequential one-page orchestration for authorized weekly browser capture."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from time import monotonic
from typing import Protocol
from ._analyzer_types import BundleFingerprint
from ._capture_errors import (
    BrowserCaptureCancelled, BrowserCaptureDependencyError, BrowserCaptureError,
    BrowserCaptureTimeout, BrowserExtensionUpgradeRequired, YahooScoringError,
)
from ._capture_runtime import (
    ActionPacer, BrowserCaptureOptions, ECRCaptureData, LeagueCaptureData,
    ProjectionCaptureData,
    cancellation_check, capture_time, check, navigation_bindings as validate_navigation_bindings,
    navigation_url,
    remaining,
)
from ._capture_task_policy import yahoo_settings_url

from .capture_schema import (
    AnalyzerCapturePhase,
    AnalyzerResponseArtifact,
    CaptureArtifact,
    CaptureKind,
    CapturePlan,
    CaptureProvider,
    CaptureTask,
    ECRCaptureMethod,
    FantasyProsECRArtifact,
    FantasyProsECRTask,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    PageCaptureTask,
    validate_artifact_for_task,
)


__all__ = (
    "BrowserCaptureCancelled", "BrowserCaptureDependencyError",
    "BrowserCaptureError", "BrowserCaptureOptions", "BrowserCaptureTimeout",
    "BrowserExtensionUpgradeRequired",
    "BrowserCollectionSession", "BrowserCollector", "CaptureBackend", "CaptureSession", "ECRCaptureData",
    "LeagueCaptureData", "ProjectionCaptureData", "SignInGate", "YahooScoringError",
)


class CaptureSession(Protocol):
    """One persistent context exposing only its single retained page."""

    def begin_analyzer_response_capture(self, phase: AnalyzerCapturePhase) -> None: ...

    def navigate(
        self, url: str, timeout_ms: int, cancelled: Callable[[], bool]
    ) -> None: ...

    def finish_analyzer_response_capture(
        self, timeout_ms: int, cancelled: Callable[[], bool]
    ) -> Mapping[str, object]: ...

    def abort_analyzer_response_capture(self) -> None: ...

    def capture_analyzer_bundle(
        self, timeout_ms: int, cancelled: Callable[[], bool]
    ) -> BundleFingerprint: ...

    def activate_full_analysis(
        self, timeout_ms: int, cancelled: Callable[[], bool]
    ) -> None: ...

    def assert_page_provenance(
        self,
        task: CaptureTask,
        planned_url: str,
        timeout_ms: int,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def capture_visible_tables(
        self,
        task: PageCaptureTask,
        timeout_ms: int,
        action_delay_ms: int,
        cancelled: Callable[[], bool],
    ) -> ProjectionCaptureData: ...

    def capture_ecr_rankings(
        self,
        task: FantasyProsECRTask,
        timeout_ms: int,
        cancelled: Callable[[], bool],
    ) -> ECRCaptureData: ...

    def capture_league_sources(
        self,
        task: PageCaptureTask,
        timeout_ms: int,
        cancelled: Callable[[], bool],
    ) -> LeagueCaptureData: ...

    def read_authenticated_espn_json(
        self,
        season: int,
        league_id: str,
        timeout_ms: int,
        maximum_bytes: int,
        cancelled: Callable[[], bool],
    ) -> tuple[Mapping[str, object], Mapping[str, object]]: ...

    def read_yahoo_scoring(
        self,
        task: PageCaptureTask,
        settings_url: str,
        timeout_ms: int,
        cancelled: Callable[[], bool],
    ) -> str: ...

    def wait_for_events(self, timeout_ms: int) -> None: ...

    def close(self, timeout_ms: int) -> None: ...


class CaptureBackend(Protocol):
    def open(
        self, options: BrowserCaptureOptions, timeout_ms: int, cancelled: Callable[[], bool]
    ) -> CaptureSession: ...


class SignInGate(Protocol):
    """Nonblocking application-owned confirmation for a headed same-page sign-in."""

    def is_ready(self, task: CaptureTask) -> bool: ...


class BrowserCollector:
    """Collect a complete plan sequentially and return artifacts without writing."""

    def __init__(
        self,
        backend: CaptureBackend | None = None,
        *,
        clock: Callable[[], float] = monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._now = now

    def collect(
        self,
        plan: CapturePlan,
        options: BrowserCaptureOptions,
        *,
        cancellation: object | None = None,
        sign_in_gate: SignInGate | None = None,
        navigation_bindings: Mapping[str, str] | None = None,
    ) -> tuple[CaptureArtifact, ...]:
        self._validate_plan(plan)
        self._validate_options(options, sign_in_gate)
        bindings = validate_navigation_bindings(plan, navigation_bindings)
        with self.open_session(
            options, cancellation=cancellation, sign_in_gate=sign_in_gate
        ) as opened:
            return opened._collect_validated(plan, bindings)

    def open_session(
        self,
        options: BrowserCaptureOptions,
        *,
        cancellation: object | None = None,
        sign_in_gate: SignInGate | None = None,
    ) -> "BrowserCollectionSession":
        """Open one bounded context for multiple sequential capture phases."""

        self._validate_options(options, sign_in_gate)
        cancelled = cancellation_check(cancellation)
        deadline = (
            None if options.overall_timeout_ms is None
            else self._clock() + options.overall_timeout_ms / 1000
        )
        pacer = ActionPacer(options.action_delay_ms / 1000, self._clock)
        backend = self._backend or _default_backend()
        check(cancelled, deadline, self._clock)
        session = backend.open(
            options, remaining(options.navigation_timeout_ms, deadline, self._clock), cancelled
        )
        return BrowserCollectionSession(
            self,
            session,
            options,
            cancellation,
            cancelled,
            deadline,
            pacer,
            sign_in_gate,
        )

    @staticmethod
    def _validate_options(options, sign_in_gate) -> None:
        if not isinstance(options, BrowserCaptureOptions):
            raise ValueError("options must be BrowserCaptureOptions")
        if sign_in_gate is not None and not options.headed:
            raise ValueError("sign-in confirmation requires a headed browser")

    @staticmethod
    def _validate_plan(plan) -> None:
        if not isinstance(plan, CapturePlan):
            raise ValueError("plan must be a CapturePlan")
        if any(
            isinstance(task, FantasyProsECRTask)
            and task.capture_method is ECRCaptureMethod.OFFICIAL_API
            for task in plan.tasks
        ):
            raise BrowserCaptureError(
                "official API ECR tasks require the caller-supplied-key API path"
            )

    def _collect_opened(
        self,
        session,
        plan,
        options,
        pacer,
        cancelled,
        deadline,
        sign_in_gate,
        gated,
        bindings,
    ) -> tuple[CaptureArtifact, ...]:
        artifacts: list[CaptureArtifact] = []
        for task in plan.tasks:
            check(cancelled, deadline, self._clock)
            arguments = (
                session, task, options, pacer, cancelled, deadline,
                sign_in_gate, gated, bindings,
            )
            if task.kind is CaptureKind.ANALYZER_RESPONSE:
                artifact = self._capture_analyzer(*arguments)
            elif task.kind is CaptureKind.VISIBLE_TABLE:
                artifact = self._capture_tables(*arguments)
            elif task.kind is CaptureKind.ECR_RANKINGS:
                artifact = self._capture_ecr(*arguments)
            elif task.kind is CaptureKind.LEAGUE_SOURCE:
                artifact = self._capture_league(*arguments)
            else:
                raise BrowserCaptureError("capture task kind is not supported")
            validate_artifact_for_task(artifact, task)
            artifacts.append(artifact)
        return tuple(artifacts)

    def _capture_analyzer(
        self, session, task, options, pacer, cancelled, deadline, gate, gated, bindings
    ) -> AnalyzerResponseArtifact:
        if not isinstance(task, PageCaptureTask) or task.analyzer_phase is None:
            raise BrowserCaptureError("analyzer capture task is invalid")
        session.begin_analyzer_response_capture(task.analyzer_phase)
        try:
            self._navigate(session, task, options, pacer, cancelled, deadline, bindings)
            self._gate(session, task, options, pacer, cancelled, deadline, gate, gated, bindings)
            self._provenance(session, task, options, cancelled, deadline, bindings)
            bundle = session.capture_analyzer_bundle(
                remaining(options.capture_timeout_ms, deadline, self._clock), cancelled
            )
            if not isinstance(bundle, BundleFingerprint):
                raise BrowserCaptureError("analyzer bundle fingerprint was invalid")
            if task.analyzer_phase is AnalyzerCapturePhase.FULL_PLAYOFFS:
                pacer.before_action(cancelled, deadline, session.wait_for_events)
                session.activate_full_analysis(
                    remaining(options.capture_timeout_ms, deadline, self._clock), cancelled
                )
            body = session.finish_analyzer_response_capture(
                remaining(options.capture_timeout_ms, deadline, self._clock), cancelled,
            )
        finally:
            session.abort_analyzer_response_capture()
        try:
            return AnalyzerResponseArtifact(
                task.task_id, task.provider, task.season, task.week, task.kind,
                capture_time(self._now()), task.analyzer_phase,
                bundle.url, bundle.sha256, body,
            )
        except ValueError:
            raise BrowserCaptureError(
                "FantasyPros analyzer body failed capture-schema validation"
            ) from None

    def _capture_tables(
        self, session, task, options, pacer, cancelled, deadline, gate, gated, bindings
    ) -> GenericTableArtifact:
        if not isinstance(task, PageCaptureTask):
            raise BrowserCaptureError("visible-table capture task is invalid")
        self._navigate(session, task, options, pacer, cancelled, deadline, bindings)
        self._gate(session, task, options, pacer, cancelled, deadline, gate, gated, bindings)
        self._provenance(session, task, options, cancelled, deadline, bindings)
        pacer.before_action(cancelled, deadline, session.wait_for_events)
        data = session.capture_visible_tables(
            task,
            remaining(options.capture_timeout_ms, deadline, self._clock),
            options.action_delay_ms,
            cancelled,
        )
        check(cancelled, deadline, self._clock)
        if not isinstance(data, ProjectionCaptureData):
            raise BrowserCaptureError("projection extraction returned an invalid result")
        try:
            return GenericTableArtifact(
                task.task_id, task.provider, task.season, task.week, task.kind,
                capture_time(self._now()), task.projection.horizon,
                task.projection.scoring, task.projection.position_scope,
                data.source_period_text, data.segments_captured, True, data.tables,
            )
        except ValueError:
            raise BrowserCaptureError("visible tables failed capture-schema validation") from None

    def _capture_ecr(
        self, session, task, options, pacer, cancelled, deadline, gate, gated, bindings
    ) -> FantasyProsECRArtifact:
        if not isinstance(task, FantasyProsECRTask):
            raise BrowserCaptureError("FantasyPros ECR capture task is invalid")
        self._navigate(session, task, options, pacer, cancelled, deadline, bindings)
        self._gate(session, task, options, pacer, cancelled, deadline, gate, gated, bindings)
        self._provenance(session, task, options, cancelled, deadline, bindings)
        pacer.before_action(cancelled, deadline, session.wait_for_events)
        data = session.capture_ecr_rankings(
            task, remaining(options.capture_timeout_ms, deadline, self._clock), cancelled,
        )
        if not isinstance(data, ECRCaptureData):
            raise BrowserCaptureError("FantasyPros ECR extraction returned an invalid result")
        try:
            return FantasyProsECRArtifact.from_task(
                task, expert_ids=data.expert_ids, expert_count=data.expert_count,
                last_updated_text=data.last_updated_text,
                last_updated_at=data.last_updated_at,
                captured_at=capture_time(self._now()), rankings=data.rankings,
            )
        except ValueError:
            raise BrowserCaptureError("FantasyPros ECR data failed capture-schema validation") from None

    def _capture_league(
        self, session, task, options, pacer, cancelled, deadline, gate, gated, bindings
    ) -> FantasyProsLeagueArtifact:
        if not isinstance(task, PageCaptureTask):
            raise BrowserCaptureError("FantasyPros league-source task is invalid")
        self._navigate(session, task, options, pacer, cancelled, deadline, bindings)
        self._gate(session, task, options, pacer, cancelled, deadline, gate, gated, bindings)
        self._provenance(session, task, options, cancelled, deadline, bindings)
        pacer.before_action(cancelled, deadline, session.wait_for_events)
        bundle = session.capture_analyzer_bundle(
            remaining(options.capture_timeout_ms, deadline, self._clock), cancelled
        )
        if not isinstance(bundle, BundleFingerprint):
            raise BrowserCaptureError("analyzer bundle fingerprint was invalid")
        data = session.capture_league_sources(
            task, remaining(options.capture_timeout_ms, deadline, self._clock), cancelled,
        )
        if not isinstance(data, LeagueCaptureData):
            raise BrowserCaptureError("FantasyPros league extraction returned an invalid result")
        try:
            return FantasyProsLeagueArtifact(
                task.task_id, task.provider, task.season, task.week, task.kind,
                capture_time(self._now()), data.team_count, True,
                bundle.url, bundle.sha256, data.sources,
            )
        except ValueError:
            raise BrowserCaptureError(
                "FantasyPros league data failed capture-schema validation"
            ) from None

    def _navigate(self, session, task, options, pacer, cancelled, deadline, bindings) -> None:
        pacer.before_action(cancelled, deadline, session.wait_for_events)
        session.navigate(
            navigation_url(task, bindings),
            remaining(options.navigation_timeout_ms, deadline, self._clock),
            cancelled,
        )
        check(cancelled, deadline, self._clock)

    def _gate(
        self, session, task, options, pacer, cancelled, deadline, gate, gated, bindings
    ) -> None:
        if gate is None or task.provider in gated:
            return
        gate_deadline = self._clock() + (
            remaining(options.sign_in_timeout_ms, deadline, self._clock) / 1000
        )
        while True:
            check(cancelled, deadline, self._clock)
            try:
                ready = gate.is_ready(task)
            except BrowserCaptureError:
                raise
            except Exception:
                raise BrowserCaptureError("sign-in confirmation failed") from None
            if ready:
                break
            remaining_ms = int((gate_deadline - self._clock()) * 1000)
            if remaining_ms <= 0:
                raise BrowserCaptureTimeout("sign-in confirmation timed out")
            session.wait_for_events(min(50, remaining_ms))
        gated.add(task.provider)
        self._navigate(session, task, options, pacer, cancelled, deadline, bindings)

    def _provenance(self, session, task, options, cancelled, deadline, bindings) -> None:
        session.assert_page_provenance(
            task,
            navigation_url(task, bindings),
            remaining(options.capture_timeout_ms, deadline, self._clock),
            cancelled,
        )


class BrowserCollectionSession:
    """One owned browser worker reused across all phases of a weekly refresh."""

    def __init__(
        self, owner, session, options, cancellation_source, cancelled, deadline,
        pacer, sign_in_gate,
    ) -> None:
        self._owner = owner
        self._session = session
        self._options = options
        self._cancellation_source = cancellation_source
        self._cancelled = cancelled
        self._deadline = deadline
        self._pacer = pacer
        self._gate = sign_in_gate
        self._gated: set[CaptureProvider] = set()
        self._closed = False

    def __enter__(self) -> "BrowserCollectionSession":
        if self._closed:
            raise BrowserCaptureError("browser collection session is closed")
        return self

    def __exit__(self, exception_type, _exception, _traceback) -> bool:
        try:
            self.close()
        except Exception:
            if exception_type is None:
                raise
        return False

    def collect(
        self,
        plan: CapturePlan,
        options: BrowserCaptureOptions | None = None,
        *,
        cancellation: object | None = None,
        sign_in_gate: SignInGate | None = None,
        navigation_bindings: Mapping[str, str] | None = None,
    ) -> tuple[CaptureArtifact, ...]:
        self._require_open()
        self._owner._validate_plan(plan)
        if options is not None and options != self._options:
            raise ValueError("options must match the open browser session")
        if cancellation is not None and cancellation is not self._cancellation_source:
            raise ValueError("cancellation must match the open browser session")
        if sign_in_gate is not None and sign_in_gate is not self._gate:
            raise ValueError("sign_in_gate must match the open browser session")
        bindings = validate_navigation_bindings(plan, navigation_bindings)
        return self._collect_validated(plan, bindings)

    def _collect_validated(self, plan, bindings):
        self._require_open()
        return self._owner._collect_opened(
            self._session,
            plan,
            self._options,
            self._pacer,
            self._cancelled,
            self._deadline,
            self._gate,
            self._gated,
            bindings,
        )

    def read_authenticated_espn_json(
        self,
        task: PageCaptureTask,
        runtime_url: str,
        season: int,
        league_id: str,
        *,
        maximum_bytes: int = 32 * 1024 * 1024,
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        """Navigate/sign in, then return only the two validated JSON objects."""

        self._require_open()
        if (
            not isinstance(task, PageCaptureTask)
            or task.kind is not CaptureKind.VISIBLE_TABLE
            or task.provider is not CaptureProvider.ESPN
            or task.projection is None
            or task.url != "https://fantasy.espn.com/football/players/projections"
        ):
            raise ValueError("authenticated ESPN read requires an ESPN projection task")
        if self._gate is None:
            raise BrowserCaptureError(
                "authenticated ESPN read requires interactive sign-in confirmation"
            )
        if type(maximum_bytes) is not int or not 1024 <= maximum_bytes <= 64 * 1024 * 1024:
            raise ValueError("maximum_bytes must be from 1024 through 67108864")
        plan = CapturePlan((task,))
        bindings = validate_navigation_bindings(plan, {task.task_id: runtime_url})
        self._owner._navigate(
            self._session, task, self._options, self._pacer, self._cancelled,
            self._deadline, bindings,
        )
        self._owner._gate(
            self._session, task, self._options, self._pacer, self._cancelled,
            self._deadline, self._gate, self._gated, bindings,
        )
        self._owner._provenance(
            self._session,
            task,
            self._options,
            self._cancelled,
            self._deadline,
            bindings,
        )
        self._pacer.before_action(
            self._cancelled, self._deadline, self._session.wait_for_events
        )
        result = self._session.read_authenticated_espn_json(
            season,
            league_id,
            remaining(self._options.capture_timeout_ms, self._deadline, self._owner._clock),
            maximum_bytes,
            self._cancelled,
        )
        check(self._cancelled, self._deadline, self._owner._clock)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or any(not isinstance(value, Mapping) for value in result)
        ):
            raise BrowserCaptureError("authenticated ESPN read returned invalid data")
        return result

    def verify_yahoo_scoring(
        self, task: PageCaptureTask, runtime_url: str
    ) -> str:
        """Confirm that a bound Yahoo league uses the task's requested scoring."""

        self._require_open()
        if (
            not isinstance(task, PageCaptureTask)
            or task.kind is not CaptureKind.VISIBLE_TABLE
            or task.provider is not CaptureProvider.YAHOO
            or task.projection is None
            or task.url != "https://football.fantasysports.yahoo.com/f1/players"
            or task.projection.scoring not in {"STD", "HALF", "PPR"}
        ):
            raise ValueError("Yahoo scoring verification requires a Yahoo projection task")
        if self._gate is None:
            raise YahooScoringError(
                "Yahoo league scoring verification requires interactive sign-in confirmation."
            )
        try:
            settings_url = yahoo_settings_url(runtime_url, task.season)
        except ValueError:
            raise YahooScoringError(
                "Yahoo scoring verification requires the normalized numeric league "
                "Player List link."
            ) from None
        plan = CapturePlan((task,))
        bindings = validate_navigation_bindings(plan, {task.task_id: runtime_url})
        self._owner._navigate(
            self._session, task, self._options, self._pacer, self._cancelled,
            self._deadline, bindings,
        )
        self._owner._gate(
            self._session, task, self._options, self._pacer, self._cancelled,
            self._deadline, self._gate, self._gated, bindings,
        )
        self._owner._provenance(
            self._session, task, self._options, self._cancelled, self._deadline, bindings,
        )
        self._pacer.before_action(
            self._cancelled, self._deadline, self._session.wait_for_events
        )
        actual = self._session.read_yahoo_scoring(
            task,
            settings_url,
            remaining(self._options.capture_timeout_ms, self._deadline, self._owner._clock),
            self._cancelled,
        )
        check(self._cancelled, self._deadline, self._owner._clock)
        if actual not in {"STD", "HALF", "PPR"}:
            raise YahooScoringError("Yahoo league scoring verification returned invalid data.")
        expected = task.projection.scoring
        if actual != expected:
            labels = {"STD": "Standard", "HALF": "Half PPR", "PPR": "PPR"}
            raise YahooScoringError(
                f"Yahoo league scoring is {labels[actual]}, but this refresh is set to "
                f"{labels[expected]}. Change the refresh scoring or use a matching Yahoo league."
            )
        return actual

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._session.close(self._options.capture_timeout_ms)
        except Exception:
            raise BrowserCaptureError("browser session could not be closed") from None

    def _require_open(self) -> None:
        if self._closed:
            raise BrowserCaptureError("browser collection session is closed")


def _default_backend() -> CaptureBackend:
    from ._playwright_capture import PlaywrightCaptureBackend

    return PlaywrightCaptureBackend()
