"""Chrome/Edge extension adapter for the strict browser-capture protocol."""

from collections.abc import Callable, Mapping
import time
from urllib.parse import urlsplit

from ._analyzer_bundle import fingerprint_analyzer_bundle_url
from ._capture_errors import (
    BrowserCaptureCancelled,
    BrowserCaptureDependencyError,
    BrowserCaptureError,
    BrowserCaptureTimeout,
    ProjectionNotPublished,
    YahooScoringError,
)
from ._capture_task_policy import (
    page_path_matches_task,
    validate_yahoo_settings_url,
    yahoo_settings_path_matches,
)
from ._ecr_page import ecr_capture_data
from ._league_page import league_capture_data
from ._projection_configure import projection_request
from ._projection_tables import projection_capture
from .browser_capture import BrowserCaptureOptions, ECRCaptureData, ProjectionCaptureData
from .capture_schema import (
    AnalyzerCapturePhase,
    CaptureProvider,
    CaptureTask,
    ECRCaptureMethod,
    FantasyProsECRTask,
    PageCaptureTask,
    analyzer_body_matches_phase,
)
from .extension_bridge import (
    BridgeAuthenticationError,
    BridgeCancelledError,
    BridgeClosedError,
    BridgeCommandError,
    BridgeDisconnectedError,
    BridgePayloadError,
    BridgeProtocolError,
    BridgeStateError,
    BridgeTimeoutError,
    ExtensionCommandBridge,
)


class ExtensionCaptureBackend:
    """Open one capture session in the explicitly paired ordinary browser."""

    def __init__(self, bridge: ExtensionCommandBridge) -> None:
        if not callable(getattr(bridge, "execute", None)):
            raise ValueError("bridge must provide execute()")
        self._bridge = bridge

    def open(
        self,
        options: BrowserCaptureOptions,
        timeout_ms: int,
        cancelled: Callable[[], bool],
    ) -> "_ExtensionSession":
        if not isinstance(options, BrowserCaptureOptions):
            raise ValueError("options must be BrowserCaptureOptions")
        result = _execute(
            self._bridge,
            "session.open",
            {"action_delay_ms": options.action_delay_ms},
            timeout_ms,
            cancelled,
            "browser extension could not open its scan tab",
        )
        if not isinstance(result, Mapping) or result != {"opened": True}:
            raise BrowserCaptureError("browser extension returned an invalid open result")
        return _ExtensionSession(self._bridge)


class _ExtensionSession:
    def __init__(self, bridge: ExtensionCommandBridge) -> None:
        self._bridge = bridge
        self._closed = False
        self._analyzer_phase: AnalyzerCapturePhase | None = None
        self._bundle_by_url: dict[str, object] = {}

    def begin_analyzer_response_capture(self, phase: AnalyzerCapturePhase) -> None:
        self._require_open()
        if self._analyzer_phase is not None:
            raise BrowserCaptureError("analyzer response capture is already active")
        if not isinstance(phase, AnalyzerCapturePhase):
            raise BrowserCaptureError("analyzer response phase is invalid")
        _execute(
            self._bridge,
            "analyzer.begin",
            {"phase": phase.value},
            5_000,
            lambda: False,
            "browser extension could not prepare analyzer capture",
        )
        self._analyzer_phase = phase

    def navigate(self, url: str, timeout_ms: int, cancelled) -> None:
        self._require_open()
        result = _execute(
            self._bridge,
            "session.navigate",
            {"url": url, "timeout_ms": timeout_ms},
            timeout_ms,
            cancelled,
            "browser extension navigation failed",
        )
        if not isinstance(result, Mapping) or result != {"loaded": True}:
            raise BrowserCaptureError("browser extension returned an invalid navigation result")
        self._require_open()

    def finish_analyzer_response_capture(self, timeout_ms, cancelled):
        self._require_open()
        phase = self._analyzer_phase
        if phase is None:
            raise BrowserCaptureError("analyzer response capture is not active")
        result = _execute(
            self._bridge,
            "analyzer.finish",
            {},
            timeout_ms,
            cancelled,
            f"FantasyPros {phase.value} response timed out",
        )
        if not analyzer_body_matches_phase(result, phase):
            raise BrowserCaptureError("FantasyPros analyzer response was invalid")
        return result

    def abort_analyzer_response_capture(self) -> None:
        phase, self._analyzer_phase = self._analyzer_phase, None
        if phase is None or self._closed:
            return
        try:
            _execute(
                self._bridge,
                "analyzer.abort",
                {},
                2_000,
                lambda: False,
                "browser extension could not reset analyzer capture",
            )
        except BrowserCaptureError:
            # Abort is cleanup. The originating operation remains the useful error.
            return

    def capture_analyzer_bundle(self, timeout_ms, cancelled):
        self._require_open()
        result = _execute(
            self._bridge,
            "analyzer.bundle",
            {},
            min(timeout_ms, 5_000),
            cancelled,
            "analyzer bundle source could not be read",
        )
        if not isinstance(result, Mapping) or set(result) != {"url"}:
            raise BrowserCaptureError("one public analyzer bundle was not found on the page")
        return fingerprint_analyzer_bundle_url(
            result["url"],
            self._bundle_by_url,
            timeout_ms,
            cancelled,
            self._require_open,
        )

    def activate_full_analysis(self, timeout_ms, cancelled) -> None:
        self._require_open()
        result = _execute(
            self._bridge,
            "analyzer.activate_full",
            {},
            timeout_ms,
            cancelled,
            "Full Trade Analysis action failed",
        )
        if not isinstance(result, Mapping) or result.get("clicked") is not True:
            raise BrowserCaptureError("one allowlisted Full Trade Analysis control was not found")

    def assert_page_provenance(
        self, task: CaptureTask, planned_url: str, timeout_ms, cancelled
    ) -> None:
        self._require_open()
        value = _execute(
            self._bridge,
            "page.provenance",
            {},
            timeout_ms,
            cancelled,
            "browser page provenance could not be read",
        )
        if not isinstance(value, Mapping) or set(value) != {
            "protocol", "hostname", "port", "pathname"
        }:
            raise BrowserCaptureError("browser page provenance was invalid")
        expected = urlsplit(planned_url)
        if (
            value["protocol"] != "https:"
            or value["port"] not in ("", "443")
            or value["hostname"] != (expected.hostname or "").casefold().rstrip(".")
            or not page_path_matches_task(task, value["pathname"], expected.path)
        ):
            raise BrowserCaptureError("browser redirected away from the planned source page")
        self._require_open()

    def capture_visible_tables(
        self, task, timeout_ms, action_delay_ms, cancelled
    ) -> ProjectionCaptureData:
        if (
            not isinstance(task, PageCaptureTask)
            or task.provider not in (
                CaptureProvider.FANTASYPROS,
                CaptureProvider.ESPN,
                CaptureProvider.YAHOO,
            )
            or task.projection is None
        ):
            raise BrowserCaptureError("projection table task is invalid")
        result = _execute(
            self._bridge,
            "projection.capture",
            {
                "request": projection_request(task),
                "action_delay_ms": action_delay_ms,
                "timeout_ms": timeout_ms,
            },
            timeout_ms,
            cancelled,
            "projection capture failed in the browser extension",
        )
        if result == {"status": "not_published"}:
            raise ProjectionNotPublished(
                "FantasyPros has not published projections for the requested week."
            )
        if not isinstance(result, Mapping) or set(result) != {"segments"}:
            raise BrowserCaptureError("projection extraction returned an invalid result")
        segments = result["segments"]
        if not isinstance(segments, list):
            raise BrowserCaptureError("projection extraction returned an invalid result")
        parsed = projection_capture(segments, task)
        return ProjectionCaptureData(
            parsed.tables, parsed.source_period_text, parsed.segments_captured
        )

    def capture_ecr_rankings(
        self, task: FantasyProsECRTask, timeout_ms, cancelled
    ) -> ECRCaptureData:
        if not isinstance(task, FantasyProsECRTask):
            raise BrowserCaptureError("FantasyPros ECR task is invalid")
        if task.capture_method is ECRCaptureMethod.OFFICIAL_API:
            raise BrowserCaptureError("official API capture is not a browser operation")
        raw = _execute(
            self._bridge,
            "ecr.capture",
            {},
            timeout_ms,
            cancelled,
            "FantasyPros ECR table capture failed in the browser extension",
        )
        return ecr_capture_data(raw, task)

    def capture_league_sources(self, task, timeout_ms, cancelled):
        if not isinstance(task, PageCaptureTask):
            raise BrowserCaptureError("FantasyPros league-source task is invalid")
        raw = _execute(
            self._bridge,
            "league.capture",
            {
                "expected_season": task.season,
                "expected_week": task.week,
                "timeout_ms": timeout_ms,
            },
            timeout_ms,
            cancelled,
            "FantasyPros league-source extraction failed",
        )
        return league_capture_data(raw, task)

    def read_authenticated_espn_json(
        self, season, league_id, timeout_ms, maximum_bytes, cancelled
    ):
        result = _execute(
            self._bridge,
            "espn.authenticated_json",
            {
                "season": season,
                "league_id": league_id,
                "timeout_ms": timeout_ms,
                "maximum_bytes": maximum_bytes,
            },
            timeout_ms,
            cancelled,
            "ESPN league data could not be read with the browser extension",
        )
        if not isinstance(result, Mapping) or set(result) != {"league", "pro_teams"}:
            raise BrowserCaptureError("ESPN browser data returned an invalid result")
        if not isinstance(result["league"], Mapping) or not isinstance(
            result["pro_teams"], Mapping
        ):
            raise BrowserCaptureError("ESPN browser data returned an invalid result")
        return result["league"], result["pro_teams"]

    def read_yahoo_scoring(self, task, settings_url, timeout_ms, cancelled):
        if (
            not isinstance(task, PageCaptureTask)
            or task.provider is not CaptureProvider.YAHOO
            or task.projection is None
            or task.projection.scoring not in {"STD", "HALF", "PPR"}
        ):
            raise YahooScoringError("Yahoo league scoring verification was not configured safely.")
        try:
            validate_yahoo_settings_url(task, settings_url)
            self.navigate(settings_url, timeout_ms, cancelled)
            value = _execute(
                self._bridge,
                "page.provenance",
                {},
                timeout_ms,
                cancelled,
                "Yahoo league scoring page identity could not be verified",
            )
            expected = urlsplit(settings_url)
            if (
                not isinstance(value, Mapping)
                or set(value) != {"protocol", "hostname", "port", "pathname"}
                or value["protocol"] != "https:"
                or value["port"] not in ("", "443")
                or value["hostname"] != (expected.hostname or "").casefold().rstrip(".")
                or not yahoo_settings_path_matches(task, value["pathname"], expected.path)
            ):
                raise YahooScoringError(
                    "Yahoo redirected away from the selected league's Settings page."
                )
            result = _execute(
                self._bridge,
                "yahoo.scoring",
                {},
                timeout_ms,
                cancelled,
                "Yahoo league scoring could not be verified",
            )
        except BrowserCaptureCancelled:
            raise
        except YahooScoringError:
            raise
        except (ValueError, BrowserCaptureError):
            raise YahooScoringError(
                "Yahoo league scoring could not be verified. Open that league's "
                "Settings page in Yahoo, then retry."
            ) from None
        if not isinstance(result, Mapping):
            raise YahooScoringError("Yahoo's Settings layout could not be verified.")
        if set(result) == {"scoring"} and result["scoring"] in {"STD", "HALF", "PPR"}:
            return result["scoring"]
        if result == {"error": "unsupported_receptions"}:
            raise YahooScoringError(
                "Yahoo league scoring is not Standard, Half PPR, or PPR."
            )
        raise YahooScoringError(
            "Yahoo's Settings layout could not be verified. Reload Yahoo and retry."
        )

    def wait_for_events(self, timeout_ms: int) -> None:
        self._require_open()
        if type(timeout_ms) is not int or not 0 <= timeout_ms <= 86_400_000:
            raise ValueError("timeout_ms must be a nonnegative integer")
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            time.sleep(min(0.05, deadline - time.monotonic()))
            self._require_open()

    def close(self, timeout_ms: int = 5000) -> None:
        if self._closed:
            return
        self.abort_analyzer_response_capture()
        self._closed = True
        try:
            _execute(
                self._bridge,
                "session.close",
                {"reason": "complete"},
                timeout_ms,
                lambda: False,
                "browser extension could not close its scan tab",
            )
        except BrowserCaptureDependencyError:
            return

    def _require_open(self) -> None:
        if self._closed:
            raise BrowserCaptureError("browser extension capture session is closed")


def _execute(bridge, op, payload, timeout_ms, cancelled, failure_message):
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise BrowserCaptureTimeout("browser extension operation timed out")
    try:
        return bridge.execute(op, payload, timeout_ms / 1000, cancelled)
    except BridgeCancelledError:
        raise BrowserCaptureCancelled("browser capture was cancelled") from None
    except BridgeTimeoutError:
        raise BrowserCaptureTimeout(failure_message) from None
    except (BridgeAuthenticationError, BridgeClosedError, BridgeDisconnectedError, BridgeStateError):
        raise BrowserCaptureDependencyError(
            "Connect the Fantasy Trade Evaluator browser extension, then try again."
        ) from None
    except (BridgeCommandError, BridgePayloadError, BridgeProtocolError):
        raise BrowserCaptureError(failure_message) from None


__all__ = ("ExtensionCaptureBackend",)
