"""Killable RPC boundary around synchronous Playwright operations."""

import json
import multiprocessing
import time

from .browser_capture import (
    BrowserCaptureCancelled,
    BrowserCaptureDependencyError,
    BrowserCaptureError,
    BrowserCaptureOptions,
    BrowserCaptureTimeout,
    ECRCaptureData,
    LeagueCaptureData,
    ProjectionCaptureData,
    ProjectionNotPublished,
    YahooScoringError,
)
from .capture_schema import ECRRankingRow, LeagueSource, VisibleTable


_POLL_SECONDS = 0.05


def open_worker_session(options: BrowserCaptureOptions, timeout_ms: int, cancelled):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=_worker_main, args=(child, options))
    process.start()
    child.close()
    proxy = _WorkerSession(parent, process)
    try:
        response = proxy._receive(timeout_ms, cancelled, "browser startup")
    except BaseException:
        proxy._terminate()
        raise
    if response != ("ready", None):
        proxy._raise_response(response)
    return proxy


class _WorkerSession:
    def __init__(self, connection, process) -> None:
        self._connection = connection
        self._process = process
        self._closed = False

    def begin_analyzer_response_capture(self, phase) -> None:
        self._call("begin_analyzer_response_capture", (phase,), 5000, lambda: False)

    def navigate(self, url, timeout_ms, cancelled) -> None:
        self._call("navigate", (url, timeout_ms), timeout_ms, cancelled)

    def finish_analyzer_response_capture(self, timeout_ms, cancelled):
        return self._call(
            "finish_analyzer_response_capture", (timeout_ms,), timeout_ms, cancelled
        )

    def abort_analyzer_response_capture(self) -> None:
        if not self._closed:
            self._call("abort_analyzer_response_capture", (), 5000, lambda: False)

    def capture_analyzer_bundle(self, timeout_ms, cancelled):
        return self._call("capture_analyzer_bundle", (timeout_ms,), timeout_ms, cancelled)

    def activate_full_analysis(self, timeout_ms, cancelled) -> None:
        self._call("activate_full_analysis", (timeout_ms,), timeout_ms, cancelled)

    def assert_page_provenance(self, task, planned_url, timeout_ms, cancelled) -> None:
        self._call(
            "assert_page_provenance",
            (task, planned_url, timeout_ms),
            timeout_ms,
            cancelled,
        )

    def capture_visible_tables(self, task, timeout_ms, action_delay_ms, cancelled):
        return self._call(
            "capture_visible_tables", (task, timeout_ms, action_delay_ms),
            timeout_ms, cancelled,
        )

    def capture_ecr_rankings(self, task, timeout_ms, cancelled):
        return self._call("capture_ecr_rankings", (task, timeout_ms), timeout_ms, cancelled)

    def capture_league_sources(self, task, timeout_ms, cancelled):
        return self._call("capture_league_sources", (task, timeout_ms), timeout_ms, cancelled)

    def read_authenticated_espn_json(
        self, season, league_id, timeout_ms, maximum_bytes, cancelled
    ):
        return self._call(
            "read_authenticated_espn_json",
            (season, league_id, timeout_ms, maximum_bytes),
            timeout_ms,
            cancelled,
        )

    def read_yahoo_scoring(self, task, settings_url, timeout_ms, cancelled):
        return self._call(
            "read_yahoo_scoring", (task, settings_url, timeout_ms), timeout_ms, cancelled
        )

    def wait_for_events(self, timeout_ms: int) -> None:
        self._call("wait_for_events", (timeout_ms,), timeout_ms + 1000, lambda: False)

    def close(self, timeout_ms: int = 5000) -> None:
        if self._closed:
            return
        try:
            self._call("close", (timeout_ms,), timeout_ms, lambda: False)
        finally:
            self._closed = True
            self._terminate(graceful=True)

    def _call(self, operation, arguments, timeout_ms, cancelled):
        if self._closed or not self._process.is_alive():
            raise BrowserCaptureError("browser worker is not running")
        try:
            self._connection.send((operation, arguments))
        except (BrokenPipeError, EOFError, OSError):
            self._terminate()
            raise BrowserCaptureError("browser worker connection failed") from None
        response = self._receive(timeout_ms, cancelled, operation)
        if isinstance(response, tuple) and len(response) == 2 and response[0] == "ok":
            return _decode_result(operation, response[1])
        self._raise_response(response)

    def _receive(self, timeout_ms, cancelled, label):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if cancelled():
                self._terminate()
                raise BrowserCaptureCancelled("browser capture was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate()
                raise BrowserCaptureTimeout(f"{label} exceeded its hard deadline")
            if self._connection.poll(min(_POLL_SECONDS, remaining)):
                try:
                    return self._connection.recv()
                except (EOFError, OSError):
                    self._terminate()
                    raise BrowserCaptureError("browser worker exited unexpectedly") from None
            if not self._process.is_alive():
                self._terminate()
                raise BrowserCaptureError("browser worker exited unexpectedly")

    def _raise_response(self, response) -> None:
        if not isinstance(response, tuple) or len(response) != 3 or response[0] != "error":
            self._terminate()
            raise BrowserCaptureError("browser worker returned invalid error data")
        kind, message = response[1], response[2]
        errors = {
            "cancelled": BrowserCaptureCancelled,
            "dependency": BrowserCaptureDependencyError,
            "timeout": BrowserCaptureTimeout,
            "not_published": ProjectionNotPublished,
            "capture": BrowserCaptureError,
            "yahoo_scoring": YahooScoringError,
        }
        if kind not in {"capture", "not_published", "yahoo_scoring"}:
            self._terminate(graceful=True)
        raise errors.get(kind, BrowserCaptureError)(message)

    def _terminate(self, *, graceful: bool = False) -> None:
        try:
            if graceful and self._process.is_alive():
                self._process.join(0.5)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(0.5)
            if self._process.is_alive() and hasattr(self._process, "kill"):
                self._process.kill()
                self._process.join(0.5)
        finally:
            try:
                self._connection.close()
            except OSError:
                pass


def _worker_main(connection, options) -> None:
    session = None
    try:
        from ._playwright_backend import open_local_session

        session = open_local_session(options)
        connection.send(("ready", None))
        while True:
            operation, arguments = connection.recv()
            try:
                if operation == "close":
                    session.close(*arguments)
                    connection.send(("ok", None))
                    return
                method = getattr(session, operation, None)
                if method is None or operation.startswith("_"):
                    raise BrowserCaptureError("browser worker operation was not allowed")
                if operation in {
                    "navigate", "finish_analyzer_response_capture", "activate_full_analysis",
                    "capture_analyzer_bundle",
                    "assert_page_provenance", "capture_visible_tables", "capture_ecr_rankings",
                    "capture_league_sources", "read_authenticated_espn_json",
                    "read_yahoo_scoring",
                }:
                    result = method(*arguments, lambda: False)
                else:
                    result = method(*arguments)
                connection.send(("ok", _encode_result(operation, result)))
            except (BrowserCaptureError, YahooScoringError) as error:
                kind = "dependency" if isinstance(error, BrowserCaptureDependencyError) else (
                    "cancelled" if isinstance(error, BrowserCaptureCancelled) else
                    "timeout" if isinstance(error, BrowserCaptureTimeout) else
                    "not_published" if isinstance(error, ProjectionNotPublished) else
                    "yahoo_scoring" if isinstance(error, YahooScoringError) else
                    "capture"
                )
                connection.send(("error", kind, str(error)))
    except EOFError:
        return
    except BaseException as error:
        kind = "dependency" if isinstance(error, BrowserCaptureDependencyError) else (
            "cancelled" if isinstance(error, BrowserCaptureCancelled) else
            "timeout" if isinstance(error, BrowserCaptureTimeout) else "capture"
        )
        if isinstance(error, YahooScoringError):
            kind = "yahoo_scoring"
        message = str(error) if isinstance(error, BrowserCaptureError) else "browser worker failed"
        try:
            connection.send(("error", kind, message))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if session is not None:
            try:
                session.close(2000)
            except Exception:
                pass
        connection.close()


def _encode_result(operation: str, result: object) -> object:
    """Convert frozen capture values to a strictly typed, pickle-safe wire record."""

    if operation == "capture_ecr_rankings":
        if not isinstance(result, ECRCaptureData):
            raise BrowserCaptureError("ECR worker result was invalid")
        return {
            "expert_count": result.expert_count,
            "expert_ids": list(result.expert_ids),
            "source_scoring": result.source_scoring,
            "source_details": result.source_details.to_record(),
            "last_updated_at": result.last_updated_at,
            "last_updated_text": result.last_updated_text,
            "rankings": [row.to_record() for row in result.rankings],
        }
    if operation == "capture_league_sources":
        if not isinstance(result, LeagueCaptureData):
            raise BrowserCaptureError("league worker result was invalid")
        return {
            "sources": [row.to_record() for row in result.sources],
            "team_count": result.team_count,
        }
    if operation == "capture_visible_tables":
        if not isinstance(result, ProjectionCaptureData):
            raise BrowserCaptureError("projection worker result was invalid")
        return {
            "segments_captured": result.segments_captured,
            "source_period_text": result.source_period_text,
            "tables": [row.to_record() for row in result.tables],
        }
    if operation == "read_authenticated_espn_json":
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or any(not isinstance(value, dict) for value in result)
        ):
            raise BrowserCaptureError("authenticated ESPN worker result was invalid")
        try:
            encoded = json.dumps(
                result, allow_nan=False, ensure_ascii=False, separators=(",", ":")
            )
            value = json.loads(encoded)
        except (TypeError, ValueError, RecursionError):
            raise BrowserCaptureError(
                "authenticated ESPN worker result was invalid"
            ) from None
        if len(encoded.encode("utf-8")) > 128 * 1024 * 1024:
            raise BrowserCaptureError(
                "authenticated ESPN worker result exceeded the size limit"
            )
        return value
    if operation == "read_yahoo_scoring":
        if result not in {"STD", "HALF", "PPR"}:
            raise BrowserCaptureError("Yahoo scoring worker result was invalid")
        return result
    return result


def _decode_result(operation: str, value: object) -> object:
    """Rebuild validated immutable values after the process boundary."""

    try:
        if operation == "capture_ecr_rankings":
            if not isinstance(value, dict) or set(value) != {
                "expert_count",
                "expert_ids",
                "source_scoring",
                "source_details",
                "last_updated_at",
                "last_updated_text",
                "rankings",
            } or not isinstance(value["expert_ids"], list) or not isinstance(
                value["rankings"], list
            ) or not isinstance(value["source_details"], dict):
                raise ValueError
            from .ecr_source import EcrSourceDetails
            return ECRCaptureData(
                tuple(value["expert_ids"]),
                value["expert_count"],
                value["source_scoring"],
                value["last_updated_text"],
                value["last_updated_at"],
                EcrSourceDetails.from_record(value["source_details"]),
                tuple(ECRRankingRow.from_record(row) for row in value["rankings"]),
            )
        if operation == "capture_league_sources":
            if not isinstance(value, dict) or set(value) != {
                "sources",
                "team_count",
            } or not isinstance(value["sources"], list):
                raise ValueError
            return LeagueCaptureData(
                value["team_count"],
                tuple(LeagueSource.from_record(row) for row in value["sources"]),
            )
        if operation == "capture_visible_tables":
            if not isinstance(value, dict) or set(value) != {
                "segments_captured",
                "source_period_text",
                "tables",
            } or not isinstance(value["tables"], list):
                raise ValueError
            return ProjectionCaptureData(
                tuple(VisibleTable.from_record(row) for row in value["tables"]),
                value["source_period_text"],
                value["segments_captured"],
            )
        if operation == "read_authenticated_espn_json":
            if (
                not isinstance(value, list)
                or len(value) != 2
                or any(not isinstance(row, dict) for row in value)
            ):
                raise ValueError
            encoded = json.dumps(
                value, allow_nan=False, ensure_ascii=False, separators=(",", ":")
            )
            if len(encoded.encode("utf-8")) > 128 * 1024 * 1024:
                raise ValueError
            return tuple(json.loads(encoded))
        if operation == "read_yahoo_scoring":
            if value not in {"STD", "HALF", "PPR"}:
                raise ValueError
            return value
    except (KeyError, TypeError, ValueError):
        raise BrowserCaptureError(
            f"browser worker returned invalid {operation} data"
        ) from None
    return value


__all__ = ("open_worker_session",)
