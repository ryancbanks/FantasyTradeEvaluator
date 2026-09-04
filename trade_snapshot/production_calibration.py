"""Interactive sign-in and exact browser calibration for production refreshes."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from .browser_capture import BrowserCaptureOptions, BrowserCollector, SignInGate
from .calibration_capture import (
    build_calibration_capture_batch,
    observations_from_calibration_artifacts,
)
from .calibration_workflow import (
    complete_calibration_session,
    prepare_weekly_calibration_session,
)
from .capture_schema import CaptureProvider, CaptureTask
from .formula_verification import (
    FormulaVerificationReport,
    verification_report_from_calibration_session,
)
from .methodology_reuse import MethodologyFingerprint
from .strength_formula import StrengthFormula
from .weekly_assembly import AssembledWeeklyEvidence
from .weekly_refresh import WeeklyRefreshEvidence


@dataclass(frozen=True, slots=True)
class CalibrationCallbacks:
    """Lazy exact-calibration operations bound to one assembled weekly capture."""

    calibrate: Callable[[WeeklyRefreshEvidence, MethodologyFingerprint], StrengthFormula]
    verify_reuse: Callable[
        [WeeklyRefreshEvidence, StrengthFormula, MethodologyFingerprint],
        FormulaVerificationReport,
    ]

    def __post_init__(self) -> None:
        if not callable(self.calibrate) or not callable(self.verify_reuse):
            raise ValueError("calibration callbacks must be callable")


@dataclass(frozen=True, slots=True)
class CalibrationCaptureContext:
    collector: BrowserCollector
    options: BrowserCaptureOptions
    sign_in_gate: SignInGate
    cancellation: object
    season: int
    week: int
    now: Callable[[], datetime]
    allow_surrogate_power: bool = False

    def __post_init__(self) -> None:
        if not callable(getattr(self.collector, "collect", None)):
            raise ValueError("collector must provide collect()")
        if not isinstance(self.options, BrowserCaptureOptions):
            raise ValueError("options must be BrowserCaptureOptions")
        if not callable(getattr(self.sign_in_gate, "is_ready", None)):
            raise ValueError("sign_in_gate must provide is_ready()")
        if not callable(getattr(self.cancellation, "is_set", None)):
            raise ValueError("cancellation must provide is_set()")
        if type(self.season) is not int or not 2012 <= self.season <= 9999:
            raise ValueError("season is invalid")
        if type(self.week) is not int or not 1 <= self.week <= 25:
            raise ValueError("week is invalid")
        if not callable(self.now):
            raise ValueError("now must be callable")
        if not isinstance(self.allow_surrogate_power, bool):
            raise ValueError("allow_surrogate_power must be a boolean")


class InteractiveSignInGate:
    """Thread-safe UI handshake; it stores provider names, never credentials."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._transition_lock = RLock()
        self._notifying_wait_state = False
        self._pending: CaptureProvider | None = None
        self._confirmed: set[CaptureProvider] = set()
        self._wait_state_listeners: list[Callable[[bool], None]] = []

    def subscribe_wait_state(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Notify a listener when a provider wait starts or ends."""

        if not callable(listener):
            raise ValueError("wait-state listener must be callable")
        with self._lock:
            self._wait_state_listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                for index, candidate in enumerate(self._wait_state_listeners):
                    if candidate is listener:
                        del self._wait_state_listeners[index]
                        break

        return unsubscribe

    def reset(self) -> None:
        with self._transition_lock:
            self._require_outside_listener()
            with self._lock:
                stopped_waiting = self._pending is not None
                self._pending = None
                self._confirmed.clear()
            if stopped_waiting:
                self._notify_wait_state(False)

    def is_ready(self, task: CaptureTask) -> bool:
        provider = getattr(task, "provider", None)
        if not isinstance(provider, CaptureProvider):
            raise ValueError("sign-in task provider is invalid")
        with self._transition_lock:
            self._require_outside_listener()
            with self._lock:
                if provider in self._confirmed:
                    return True
                started_waiting = self._pending is None
                if started_waiting:
                    self._pending = provider
                elif self._pending is not provider:
                    raise ValueError("another provider sign-in is already pending")
            if started_waiting:
                self._notify_wait_state(True)
            return False

    def confirm(self, provider: str | CaptureProvider | None = None) -> str:
        with self._transition_lock:
            self._require_outside_listener()
            with self._lock:
                if self._pending is None:
                    raise ValueError("no provider sign-in is waiting for confirmation")
                expected = self._pending
                if provider is not None:
                    try:
                        supplied = (
                            provider
                            if isinstance(provider, CaptureProvider)
                            else CaptureProvider(provider)
                        )
                    except (TypeError, ValueError):
                        raise ValueError(
                            "provider sign-in confirmation is invalid"
                        ) from None
                    if supplied is not expected:
                        raise ValueError("provider sign-in confirmation does not match")
                self._confirmed.add(expected)
                self._pending = None
            self._notify_wait_state(False)
            return expected.value

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "pending_provider": None if self._pending is None else self._pending.value,
                "confirmed_providers": [row.value for row in sorted(
                    self._confirmed, key=lambda value: value.value
                )],
            }

    def _notify_wait_state(self, waiting: bool) -> None:
        with self._lock:
            listeners = tuple(self._wait_state_listeners)
        self._notifying_wait_state = True
        try:
            for listener in listeners:
                try:
                    listener(waiting)
                except Exception:
                    # Timing/telemetry observers must never break the sign-in
                    # state transition they are observing.
                    continue
        finally:
            self._notifying_wait_state = False

    def _require_outside_listener(self) -> None:
        if self._notifying_wait_state:
            raise RuntimeError("sign-in state cannot change from a wait-state listener")


class BrowserCalibrationFactory:
    """Create lazy initial-calibration and weekly-verification browser callbacks."""

    def __call__(
        self,
        assembled: AssembledWeeklyEvidence,
        primary_team_id: str,
        context: CalibrationCaptureContext,
    ) -> CalibrationCallbacks:
        if not isinstance(assembled, AssembledWeeklyEvidence):
            raise ValueError("assembled must be AssembledWeeklyEvidence")
        if not isinstance(primary_team_id, str) or not primary_team_id:
            raise ValueError("primary_team_id must be non-empty")
        if not isinstance(context, CalibrationCaptureContext):
            raise ValueError("context must be CalibrationCaptureContext")

        def calibrate(evidence, fingerprint):
            _same_evidence(assembled, evidence, fingerprint)
            session, observations = self._capture(
                assembled, primary_team_id, context, evidence, 250, 100
            )
            return complete_calibration_session(
                session,
                observations,
                captured_at=context.now(),
                allow_surrogate_power=context.allow_surrogate_power,
            )

        def verify_reuse(evidence, formula, fingerprint):
            _same_evidence(assembled, evidence, fingerprint)
            if not isinstance(formula, StrengthFormula):
                raise ValueError("formula must be StrengthFormula")
            session, observations = self._capture(
                assembled, primary_team_id, context, evidence, 1, 100
            )
            return verification_report_from_calibration_session(
                session,
                observations,
                formula,
                weekly_snapshot_id=evidence.state.snapshot_id,
                verified_at=context.now(),
            )

        return CalibrationCallbacks(calibrate, verify_reuse)

    @staticmethod
    def _capture(
        assembled,
        primary_team_id,
        context,
        evidence,
        training_count,
        heldout_count,
    ):
        session = prepare_weekly_calibration_session(
            evidence,
            primary_team_id=primary_team_id,
            team_provider_ids=assembled.fantasypros_team_ids,
            player_provider_ids=assembled.fantasypros_player_ids,
            training_experiment_count=training_count,
            held_out_experiment_count=heldout_count,
        )
        batch = build_calibration_capture_batch(
            session, season=context.season, week=context.week
        )
        artifacts = context.collector.collect(
            batch.plan,
            context.options,
            cancellation=context.cancellation,
            sign_in_gate=context.sign_in_gate,
        )
        observations = observations_from_calibration_artifacts(
            batch, artifacts, bundle=evidence.analyzer_bundle
        )
        return session, observations


def _same_evidence(assembled, evidence, fingerprint):
    if evidence is not assembled.evidence:
        raise ValueError("calibration callback received different weekly evidence")
    if not isinstance(fingerprint, MethodologyFingerprint):
        raise ValueError("fingerprint must be MethodologyFingerprint")
    if fingerprint != evidence.methodology_fingerprint:
        raise ValueError("calibration callback received a different methodology")


__all__ = (
    "BrowserCalibrationFactory",
    "CalibrationCallbacks",
    "CalibrationCaptureContext",
    "InteractiveSignInGate",
)
