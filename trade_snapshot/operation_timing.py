"""Thread-safe active-time progress and ETA tracking for background operations.

The tracker deliberately knows nothing about collection or search jobs.  Callers
name phases, publish exact unit counts when they have them, and choose when an
operation is paused because it is waiting for a person rather than computing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import math
from statistics import median
from threading import RLock
import time
from typing import Final, Literal, TypedDict


SCHEMA_VERSION: Final = 1
JS_SAFE_INTEGER: Final = 9_007_199_254_740_991

OperationStatus = Literal["queued", "running", "complete", "cancelled", "failed"]
OperationActivity = Literal["idle", "active", "paused", "terminal"]
EtaConfidence = Literal["low", "medium", "high"]

_TERMINAL_STATUSES: Final = frozenset({"complete", "cancelled", "failed"})


class ProgressRecord(TypedDict):
    determinate: bool
    fraction: float | None
    completed_units: int | None
    completed_units_text: str | None
    total_units: int | None
    total_units_text: str | None


class EtaRecord(TypedDict):
    low_seconds: int
    likely_seconds: int
    high_seconds: int
    confidence: EtaConfidence
    basis: Literal["observed_phase_throughput"]
    sample_count: int


class TimingSnapshot(TypedDict):
    schema_version: int
    status: OperationStatus
    activity: OperationActivity
    phase: str | None
    elapsed_seconds: float
    cancel_requested: bool
    progress: ProgressRecord
    eta: EtaRecord | None


class OperationTiming:
    """Measure one operation's active runtime and evidence-backed remaining time.

    A new tracker is queued and idle.  ``start`` begins active time.  ``pause``
    excludes a user-controlled wait (for example, sign-in) from elapsed time and
    throughput samples; ``resume`` restarts both clocks.  Every ``begin_phase``
    call intentionally discards the prior phase's rate evidence because unlike
    units must not be used to predict each other.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        minimum_rate_samples: int = 3,
        minimum_sample_seconds: float = 1.0,
        rate_window_size: int = 12,
        ewma_alpha: float = 0.35,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        if type(minimum_rate_samples) is not int or minimum_rate_samples < 2:
            raise ValueError("minimum_rate_samples must be an integer of at least two")
        if type(rate_window_size) is not int or rate_window_size < minimum_rate_samples:
            raise ValueError(
                "rate_window_size must be an integer no smaller than minimum_rate_samples"
            )
        if (
            isinstance(minimum_sample_seconds, bool)
            or not isinstance(minimum_sample_seconds, (int, float))
            or not math.isfinite(float(minimum_sample_seconds))
            or minimum_sample_seconds < 0
        ):
            raise ValueError("minimum_sample_seconds must be a finite non-negative number")
        if (
            isinstance(ewma_alpha, bool)
            or not isinstance(ewma_alpha, (int, float))
            or not math.isfinite(float(ewma_alpha))
            or not 0 < ewma_alpha <= 1
        ):
            raise ValueError("ewma_alpha must be greater than zero and at most one")

        self._clock = clock
        self._minimum_rate_samples = minimum_rate_samples
        self._minimum_sample_seconds = float(minimum_sample_seconds)
        self._rate_window_size = rate_window_size
        self._ewma_alpha = float(ewma_alpha)
        self._lock = RLock()

        self._last_clock = self._read_clock_value()
        self._status: OperationStatus = "queued"
        self._activity: OperationActivity = "idle"
        self._cancel_requested = False
        self._active_seconds = 0.0
        self._active_started_at: float | None = None

        self._phase: str | None = None
        self._phase_active_seconds = 0.0
        self._phase_started_at: float | None = None
        self._completed_units: int | None = None
        self._total_units: int | None = None
        self._last_observation: tuple[float, int] | None = None
        self._rates: deque[tuple[float, float]] = deque(maxlen=rate_window_size)

    def start(
        self,
        phase: str,
        *,
        completed_units: int | None = None,
        total_units: int | None = None,
    ) -> TimingSnapshot:
        """Start the operation, optionally with its first determinate observation."""

        with self._lock:
            if self._status != "queued":
                raise RuntimeError("only a queued operation can be started")
            # Validate before changing status so a rejected start remains retryable.
            _phase_name(phase)
            _validate_unit_pair(completed_units, total_units)
            now = self._now()
            self._status = "running"
            self._activity = "active"
            self._active_started_at = now
            self._begin_phase(now, phase, completed_units, total_units)
            return self._snapshot(now)

    def begin_phase(
        self,
        phase: str,
        *,
        completed_units: int | None = None,
        total_units: int | None = None,
    ) -> TimingSnapshot:
        """Begin a distinct phase and discard the previous phase's rate evidence."""

        with self._lock:
            self._require_running()
            now = self._now()
            self._begin_phase(now, phase, completed_units, total_units)
            return self._snapshot(now)

    def observe(self, completed_units: int, total_units: int) -> TimingSnapshot:
        """Publish exact monotonically increasing work units for the current phase."""

        with self._lock:
            self._require_running()
            if self._activity != "active":
                raise RuntimeError("determinate progress cannot be observed while paused")
            now = self._now()
            self._observe(now, completed_units, total_units)
            return self._snapshot(now)

    def pause(self) -> TimingSnapshot:
        """Pause active time while the operation waits for user action."""

        with self._lock:
            self._require_running()
            now = self._now()
            if self._activity == "active":
                self._stop_active_clocks(now)
                self._activity = "paused"
            return self._snapshot(now)

    def resume(self) -> TimingSnapshot:
        """Resume active time after a user-controlled wait."""

        with self._lock:
            self._require_running()
            now = self._now()
            if self._activity == "paused":
                self._active_started_at = now
                self._phase_started_at = now
                self._activity = "active"
            return self._snapshot(now)

    def request_cancel(self) -> TimingSnapshot:
        """Record a cancellation request without claiming the worker has stopped."""

        with self._lock:
            now = self._now()
            if self._status not in _TERMINAL_STATUSES:
                self._cancel_requested = True
            return self._snapshot(now)

    def finish(self) -> TimingSnapshot:
        """Freeze timing with a successful terminal status."""

        return self._finish_as("complete")

    def cancel(self) -> TimingSnapshot:
        """Freeze timing with a cancelled terminal status."""

        with self._lock:
            if self._status not in _TERMINAL_STATUSES:
                self._cancel_requested = True
            return self._finish_as_locked("cancelled")

    def fail(self) -> TimingSnapshot:
        """Freeze timing with a failed terminal status."""

        return self._finish_as("failed")

    def snapshot(self) -> TimingSnapshot:
        """Return a JSON-ready, versioned view of the current timing state."""

        with self._lock:
            now = self._now()
            return self._snapshot(now)

    def _finish_as(self, status: OperationStatus) -> TimingSnapshot:
        with self._lock:
            return self._finish_as_locked(status)

    def _finish_as_locked(self, status: OperationStatus) -> TimingSnapshot:
        now = self._now()
        if self._status not in _TERMINAL_STATUSES:
            if self._activity == "active":
                self._stop_active_clocks(now)
            self._status = status
            self._activity = "terminal"
        return self._snapshot(now)

    def _begin_phase(
        self,
        now: float,
        phase: str,
        completed_units: int | None,
        total_units: int | None,
    ) -> None:
        normalized_phase = _phase_name(phase)
        _validate_unit_pair(completed_units, total_units)
        self._phase = normalized_phase
        self._phase_active_seconds = 0.0
        self._phase_started_at = now if self._activity == "active" else None
        self._completed_units = None
        self._total_units = None
        self._last_observation = None
        self._rates.clear()
        if completed_units is not None and total_units is not None:
            self._observe(now, completed_units, total_units)

    def _observe(self, now: float, completed_units: int, total_units: int) -> None:
        _validate_units(completed_units, total_units)
        if self._total_units is not None and total_units != self._total_units:
            raise ValueError("total_units cannot change within a phase")
        if self._completed_units is not None and completed_units < self._completed_units:
            raise ValueError("completed_units cannot move backwards within a phase")

        phase_seconds = self._phase_elapsed(now)
        if self._last_observation is not None:
            prior_seconds, prior_completed = self._last_observation
            if completed_units != prior_completed:
                interval_seconds = phase_seconds - prior_seconds
                if interval_seconds > 0:
                    try:
                        rate = (completed_units - prior_completed) / interval_seconds
                    except OverflowError:
                        # Counts stay exact even if their throughput cannot be
                        # represented as a finite float.  Withhold ETA instead.
                        rate = math.inf
                    if math.isfinite(rate) and rate > 0:
                        self._rates.append((rate, interval_seconds))
                self._last_observation = (phase_seconds, completed_units)
        else:
            self._last_observation = (phase_seconds, completed_units)
        self._completed_units = completed_units
        self._total_units = total_units

    def _snapshot(self, now: float) -> TimingSnapshot:
        completed = self._completed_units
        total = self._total_units
        determinate = completed is not None and total is not None
        progress: ProgressRecord = {
            "determinate": determinate,
            "fraction": _fraction(completed, total),
            "completed_units": _safe_json_integer(completed),
            "completed_units_text": None if completed is None else str(completed),
            "total_units": _safe_json_integer(total),
            "total_units_text": None if total is None else str(total),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self._status,
            "activity": self._activity,
            "phase": self._phase,
            "elapsed_seconds": round(self._elapsed(now), 3),
            "cancel_requested": self._cancel_requested,
            "progress": progress,
            "eta": self._eta(),
        }

    def _eta(self) -> EtaRecord | None:
        if self._status != "running" or self._activity != "active":
            return None
        completed = self._completed_units
        total = self._total_units
        if completed is None or total is None or completed >= total:
            return None
        rates = list(self._rates)
        sample_seconds = sum(duration for _, duration in rates)
        if (
            len(rates) < self._minimum_rate_samples
            or sample_seconds < self._minimum_sample_seconds
        ):
            return None

        clipped = _robust_rates([rate for rate, _ in rates])
        likely_rate = clipped[0]
        for rate in clipped[1:]:
            likely_rate = (
                self._ewma_alpha * rate
                + (1 - self._ewma_alpha) * likely_rate
            )
        slow_rate = min(_quantile(clipped, 0.2), likely_rate)
        fast_rate = max(_quantile(clipped, 0.8), likely_rate)
        remaining = total - completed
        estimates = (
            remaining / fast_rate,
            remaining / likely_rate,
            remaining / slow_rate,
        )
        if not all(math.isfinite(value) and value >= 0 for value in estimates):
            return None
        low, likely, high = (max(0, int(round(value))) for value in estimates)
        likely = max(low, likely)
        high = max(likely, high)
        dispersion = (
            0.0
            if likely_rate == 0
            else (fast_rate - slow_rate) / likely_rate
        )
        confidence: EtaConfidence
        if len(rates) >= 8 and sample_seconds >= 10 and dispersion <= 0.25:
            confidence = "high"
        elif len(rates) >= 5 and sample_seconds >= 4 and dispersion <= 0.5:
            confidence = "medium"
        else:
            confidence = "low"
        return {
            "low_seconds": low,
            "likely_seconds": likely,
            "high_seconds": high,
            "confidence": confidence,
            "basis": "observed_phase_throughput",
            "sample_count": len(rates),
        }

    def _elapsed(self, now: float) -> float:
        active = self._active_seconds
        if self._active_started_at is not None:
            active += now - self._active_started_at
        return max(0.0, active)

    def _phase_elapsed(self, now: float) -> float:
        active = self._phase_active_seconds
        if self._phase_started_at is not None:
            active += now - self._phase_started_at
        return max(0.0, active)

    def _stop_active_clocks(self, now: float) -> None:
        if self._active_started_at is not None:
            self._active_seconds += now - self._active_started_at
            self._active_started_at = None
        if self._phase_started_at is not None:
            self._phase_active_seconds += now - self._phase_started_at
            self._phase_started_at = None

    def _require_running(self) -> None:
        if self._status != "running":
            raise RuntimeError("operation must be running")

    def _now(self) -> float:
        now = self._read_clock_value()
        if now < self._last_clock:
            raise RuntimeError("clock moved backwards")
        self._last_clock = now
        return now

    def _read_clock_value(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock must return a finite number")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("clock must return a finite number")
        return converted


def _phase_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("phase must be a non-empty string")
    return value.strip()


def _validate_unit_pair(completed: int | None, total: int | None) -> None:
    if (completed is None) != (total is None):
        raise ValueError("completed_units and total_units must be provided together")
    if completed is not None and total is not None:
        _validate_units(completed, total)


def _validate_units(completed: int, total: int) -> None:
    if type(completed) is not int or completed < 0:
        raise ValueError("completed_units must be a non-negative integer")
    if type(total) is not int or total < 0:
        raise ValueError("total_units must be a non-negative integer")
    if completed > total:
        raise ValueError("completed_units cannot exceed total_units")


def _safe_json_integer(value: int | None) -> int | None:
    if value is None or value > JS_SAFE_INTEGER:
        return None
    return value


def _fraction(completed: int | None, total: int | None) -> float | None:
    if completed is None or total is None:
        return None
    if total == 0:
        return 1.0
    return completed / total


def _robust_rates(values: list[float]) -> list[float]:
    center = median(values)
    deviations = [abs(value - center) for value in values]
    mad = median(deviations)
    if mad == 0:
        return [center for _ in values]
    radius = 3 * 1.4826 * mad
    lower = max(center - radius, center * 0.05, 1e-300)
    upper = center + radius
    return [min(upper, max(lower, value)) for value in values]


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight
