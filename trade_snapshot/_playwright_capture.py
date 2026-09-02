"""Optional Playwright adapter for the strict browser-capture protocol."""

from collections.abc import Mapping
import time
from urllib.parse import urlsplit

from ._analyzer_bundle import capture_live_analyzer_bundle
from ._capture_scripts import (
    ADVANCE_PROJECTION_SCRIPT, ANALYZER_TAP_SCRIPT,
    ECR_TABLE_SCRIPT,
    FULL_ANALYSIS_ACTION_SCRIPT,
    PAGE_PROVENANCE_SCRIPT,
    PROJECTION_TABLE_SCRIPT,
    SINGLE_PAGE_SCRIPT,
    TAKE_ANALYZER_BODY_SCRIPT,
    YAHOO_SCORING_SCRIPT,
)
from ._capture_task_policy import (
    page_path_matches_task,
    validate_yahoo_settings_url,
    yahoo_settings_path_matches,
)
from ._ecr_page import ecr_capture_data
from ._league_page import capture_league_sources
from ._projection_tables import projection_capture
from ._projection_configure import configure_projection, projection_request
from ._playwright_backend import PlaywrightCaptureBackend
from .browser_capture import (
    BrowserCaptureCancelled,
    BrowserCaptureDependencyError,
    BrowserCaptureError,
    BrowserCaptureOptions,
    BrowserCaptureTimeout,
    ProjectionCaptureData,
    YahooScoringError,
)
from .capture_schema import (
    AnalyzerCapturePhase,
    CaptureProvider,
    CaptureTask,
    ECRCaptureMethod,
    FantasyProsECRTask,
    analyzer_body_matches_phase,
)


_POLL_MS, _STABILITY_MS, _STABILITY_SAMPLES = 50, 200, 3


class _PlaywrightSession:
    def __init__(self, context, manager, timeout_error) -> None:
        self._context = context
        self._manager = manager
        self._timeout_error = timeout_error
        self._closed = False
        self._popup_error = False
        self._analyzer_phase = None
        self._bundle_by_url = {}
        pages = tuple(context.pages)
        self._page = pages[0] if pages else context.new_page()
        context.on("page", self._close_unexpected_page)
        for page in tuple(context.pages):
            if page is not self._page:
                self._close_page(page, unexpected=False)
        context.add_init_script(script=SINGLE_PAGE_SCRIPT)
        context.add_init_script(script=ANALYZER_TAP_SCRIPT)

    def _close_unexpected_page(self, page) -> None:
        if page is not self._page:
            self._close_page(page, unexpected=True)

    def _close_page(self, page, *, unexpected: bool) -> None:
        try:
            page.close(run_before_unload=False)
        except Exception:
            self._popup_error = True
            try:
                self._context.close()
            except Exception:
                pass
        else:
            self._popup_error = self._popup_error or unexpected

    def begin_analyzer_response_capture(self, phase: AnalyzerCapturePhase) -> None:
        if self._analyzer_phase is not None:
            raise BrowserCaptureError("analyzer response capture is already active")
        if not isinstance(phase, AnalyzerCapturePhase):
            raise BrowserCaptureError("analyzer response phase is invalid")
        self._analyzer_phase = phase

    def navigate(self, url: str, timeout_ms: int, cancelled) -> None:
        self._require_usable_page()
        deadline = time.monotonic() + timeout_ms / 1000
        committed = [False]
        main_frame = getattr(self._page, "main_frame", None)

        def mark_committed(frame) -> None:
            if main_frame is None or frame is main_frame:
                committed[0] = True

        self._page.on("framenavigated", mark_committed)
        try:
            _check_cancelled(cancelled)
            try:
                self._page.goto(
                    url,
                    wait_until="commit",
                    timeout=min(250, _remaining_ms(deadline)),
                )
                committed[0] = True
            except self._timeout_error:
                pass
            except Exception:
                raise BrowserCaptureError("browser navigation failed") from None
            while not committed[0]:
                self._wait(min(_POLL_MS, _remaining_ms(deadline)), cancelled)
            while True:
                _check_cancelled(cancelled)
                try:
                    self._page.wait_for_load_state(
                        "domcontentloaded", timeout=min(_POLL_MS, _remaining_ms(deadline))
                    )
                    break
                except self._timeout_error:
                    continue
                except Exception:
                    raise BrowserCaptureError("browser navigation failed") from None
        except BrowserCaptureTimeout:
            raise BrowserCaptureTimeout("browser navigation timed out") from None
        finally:
            try:
                self._page.remove_listener("framenavigated", mark_committed)
            except Exception:
                pass
        self._require_usable_page()

    def finish_analyzer_response_capture(self, timeout_ms, cancelled):
        self._require_usable_page()
        if self._analyzer_phase is None:
            raise BrowserCaptureError("analyzer response capture is not active")
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            _check_cancelled(cancelled)
            _remaining_ms(deadline)
            try:
                candidate = self._page.evaluate(TAKE_ANALYZER_BODY_SCRIPT)
            except Exception:
                raise BrowserCaptureError("FantasyPros analyzer tap could not be read") from None
            self._require_usable_page()
            if candidate is not None:
                if not isinstance(candidate, Mapping) or set(candidate) != {"kind", "value"}:
                    raise BrowserCaptureError("FantasyPros analyzer tap returned invalid data")
                if candidate["kind"] == "error":
                    raise BrowserCaptureError("FantasyPros analyzer response exceeded capture limits")
                if (
                    candidate["kind"] == "body"
                    and analyzer_body_matches_phase(candidate["value"], self._analyzer_phase)
                ):
                    return candidate["value"]
                continue
            try:
                self._wait(min(_POLL_MS, _remaining_ms(deadline)), cancelled)
            except BrowserCaptureTimeout:
                raise BrowserCaptureTimeout(
                    f"FantasyPros {self._analyzer_phase.value} response timed out"
                ) from None

    def abort_analyzer_response_capture(self) -> None:
        self._analyzer_phase = None

    def capture_analyzer_bundle(self, timeout_ms, cancelled):
        return capture_live_analyzer_bundle(
            self._page, self._bundle_by_url, timeout_ms, cancelled,
            self._require_usable_page,
        )

    def activate_full_analysis(self, timeout_ms, cancelled) -> None:
        self._require_usable_page()
        deadline = time.monotonic() + timeout_ms / 1000
        _check_cancelled(cancelled)
        _remaining_ms(deadline)
        try:
            result = self._page.evaluate(FULL_ANALYSIS_ACTION_SCRIPT)
        except Exception:
            raise BrowserCaptureError("Full Trade Analysis action failed") from None
        self._require_usable_page()
        if not isinstance(result, Mapping) or result.get("clicked") is not True:
            raise BrowserCaptureError("one allowlisted Full Trade Analysis control was not found")

    def assert_page_provenance(
        self, task: CaptureTask, planned_url: str, timeout_ms, cancelled
    ) -> None:
        self._require_usable_page()
        deadline = time.monotonic() + timeout_ms / 1000
        _check_cancelled(cancelled)
        _remaining_ms(deadline)
        try:
            value = self._page.evaluate(PAGE_PROVENANCE_SCRIPT)
        except Exception:
            raise BrowserCaptureError("browser page provenance could not be read") from None
        if not isinstance(value, Mapping) or set(value) != {
            "protocol", "hostname", "port", "pathname"
        }:
            raise BrowserCaptureError("browser page provenance was invalid")
        expected = urlsplit(planned_url)
        if (
            value["protocol"] != "https:" or value["port"] not in ("", "443")
            or value["hostname"] != (expected.hostname or "").casefold().rstrip(".")
            or not page_path_matches_task(task, value["pathname"], expected.path)
        ):
            raise BrowserCaptureError("browser redirected away from the planned source page")
        self._require_usable_page()

    def capture_visible_tables(self, task, timeout_ms, action_delay_ms, cancelled):
        if task.provider not in (
            CaptureProvider.FANTASYPROS,
            CaptureProvider.ESPN,
            CaptureProvider.YAHOO,
            CaptureProvider.CBS,
            CaptureProvider.FFTODAY,
            CaptureProvider.FANTASYSHARKS,
        ) or task.projection is None:
            raise BrowserCaptureError("projection table task is invalid")
        deadline = time.monotonic() + timeout_ms / 1000
        configure_projection(
            self._page, task, action_delay_ms, deadline, cancelled, self._wait,
            _remaining_ms, self._require_usable_page,
        )
        segments, previous, actions = [], None, 0
        while True:
            raw = self._stable_value(
                PROJECTION_TABLE_SCRIPT, projection_request(task), _projection_ready,
                _remaining_ms(deadline), cancelled, "projection table",
            )
            if raw != previous:
                segments.append(raw)
                previous = raw
            _check_cancelled(cancelled)
            _remaining_ms(deadline)
            try:
                advance = self._page.evaluate(ADVANCE_PROJECTION_SCRIPT, task.provider.value)
            except Exception:
                raise BrowserCaptureError("projection traversal action failed") from None
            self._require_usable_page()
            if not isinstance(advance, Mapping) or set(advance) != {"action"}:
                raise BrowserCaptureError("projection traversal returned invalid state")
            action = advance["action"]
            if action == "done":
                parsed = projection_capture(segments, task)
                return ProjectionCaptureData(
                    parsed.tables, parsed.source_period_text, parsed.segments_captured
                )
            if action not in {"scroll", "next"}:
                raise BrowserCaptureError("projection traversal could not prove completeness")
            actions += 1
            if actions > 10000:
                raise BrowserCaptureError("projection traversal exceeded its action limit")
            self._wait(min(action_delay_ms, _remaining_ms(deadline)), cancelled)
            if action == "next":
                changed = self._changed_projection(previous, task, deadline, cancelled)
                segments.append(changed)
                previous = changed

    def read_yahoo_scoring(self, task, settings_url, timeout_ms, cancelled):
        """Read the reception modifier from the bound Yahoo league settings page."""

        if (
            task.provider is not CaptureProvider.YAHOO
            or task.projection is None
            or task.projection.scoring not in {"STD", "HALF", "PPR"}
        ):
            raise YahooScoringError("Yahoo league scoring verification was not configured safely.")
        try:
            validate_yahoo_settings_url(task, settings_url)
        except ValueError:
            raise YahooScoringError(
                "Yahoo league scoring verification was not configured safely."
            ) from None
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            self.navigate(settings_url, _remaining_ms(deadline), cancelled)
            self._assert_yahoo_settings_provenance(task, settings_url, deadline, cancelled)
            value = self._stable_value(
                YAHOO_SCORING_SCRIPT,
                None,
                _yahoo_scoring_ready,
                _remaining_ms(deadline),
                cancelled,
                "Yahoo league scoring settings",
            )
        except BrowserCaptureCancelled:
            raise
        except YahooScoringError:
            raise
        except BrowserCaptureError:
            raise YahooScoringError(
                "Yahoo league scoring could not be verified. Open that league's "
                "Settings page in Yahoo, then retry."
            ) from None
        if set(value) != {"scoring"}:
            if value.get("error") == "unsupported_receptions":
                raise YahooScoringError(
                    "Yahoo league scoring is not Standard, Half PPR, or PPR. "
                    "This refresh supports those three reception settings."
                )
            raise YahooScoringError(
                "Yahoo's Settings layout could not be verified. Reload Yahoo and retry; "
                "if it continues, update this app before collecting."
            )
        return value["scoring"]

    def _assert_yahoo_settings_provenance(
        self, task, settings_url, deadline, cancelled
    ) -> None:
        _check_cancelled(cancelled)
        _remaining_ms(deadline)
        try:
            value = self._page.evaluate(PAGE_PROVENANCE_SCRIPT)
        except Exception:
            raise YahooScoringError(
                "Yahoo league scoring page identity could not be verified."
            ) from None
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
        self._require_usable_page()

    def capture_ecr_rankings(self, task, timeout_ms, cancelled):
        if not isinstance(task, FantasyProsECRTask):
            raise BrowserCaptureError("FantasyPros ECR task is invalid")
        if task.capture_method is ECRCaptureMethod.OFFICIAL_API:
            raise BrowserCaptureError("official API capture is not a browser operation")
        raw = self._stable_value(
            ECR_TABLE_SCRIPT,
            None,
            _ecr_ready,
            timeout_ms,
            cancelled,
            "FantasyPros ECR table",
        )
        return ecr_capture_data(raw, task)

    def capture_league_sources(self, task, timeout_ms, cancelled):
        return capture_league_sources(
            self._page, task, timeout_ms, cancelled, self._require_usable_page
        )

    def read_authenticated_espn_json(
        self, season, league_id, timeout_ms, maximum_bytes, cancelled
    ):
        """Use profile cookies without exposing them beyond this worker process."""

        from ._espn_browser_read import read_authenticated_espn_json
        from .espn_free_read import EspnFreeReadError

        self._require_usable_page()
        try:
            result = read_authenticated_espn_json(
                self._context,
                season,
                league_id,
                timeout_ms,
                maximum_bytes,
                cancelled,
            )
        except BrowserCaptureError:
            raise
        except EspnFreeReadError as error:
            raise BrowserCaptureError(str(error)) from None
        self._require_usable_page()
        return result

    def _stable_value(self, script, argument, ready, timeout_ms, cancelled, label):
        self._require_usable_page()
        deadline = time.monotonic() + timeout_ms / 1000
        previous = object()
        samples = 0
        saw_ready = False
        while True:
            _check_cancelled(cancelled)
            try:
                value = self._page.evaluate(script, argument)
            except Exception:
                raise BrowserCaptureError(f"{label} extraction failed") from None
            self._require_usable_page()
            if ready(value):
                saw_ready = True
                samples = samples + 1 if value == previous else 1
                previous = value
                if samples >= _STABILITY_SAMPLES:
                    return value
            else:
                samples = 0
                previous = object()
            try:
                self._wait(min(_STABILITY_MS, _remaining_ms(deadline)), cancelled)
            except BrowserCaptureTimeout:
                detail = "never stabilized" if saw_ready else "was not found"
                raise BrowserCaptureTimeout(f"{label} {detail}") from None

    def _changed_projection(self, previous, task, deadline, cancelled):
        while True:
            value = self._stable_value(
                PROJECTION_TABLE_SCRIPT, projection_request(task), _projection_ready,
                _remaining_ms(deadline), cancelled, "projection page transition",
            )
            if value != previous:
                return value
            self._wait(min(_POLL_MS, _remaining_ms(deadline)), cancelled)

    def _wait(self, timeout_ms: int, cancelled) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            _check_cancelled(cancelled)
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                return
            remaining = max(1, int(seconds * 1000) + 1)
            try:
                self._page.wait_for_timeout(min(_POLL_MS, remaining))
            except Exception:
                raise BrowserCaptureError("browser page closed during capture") from None
            self._require_usable_page()

    def _require_usable_page(self) -> None:
        is_closed = getattr(self._page, "is_closed", None)
        if callable(is_closed) and is_closed():
            raise BrowserCaptureError("the retained browser page was closed")
        if self._popup_error:
            raise BrowserCaptureError(
                "a second page was blocked; continue sign-in in the retained page"
            )

    def wait_for_events(self, timeout_ms: int) -> None:
        self._wait(timeout_ms, lambda: False)

    def close(self, timeout_ms: int = 5000) -> None:
        if self._closed:
            return
        self._closed = True
        self.abort_analyzer_response_capture()
        try:
            self._context.close()
        finally:
            self._manager.stop()


def _ecr_ready(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"source", "rankings"}
        and isinstance(value["source"], Mapping)
        and isinstance(value["rankings"], list)
        and bool(value["rankings"])
    )


def _projection_ready(value: object) -> bool:
    return (
        isinstance(value, Mapping) and set(value) == {"source", "tables"}
        and isinstance(value["source"], Mapping)
        and isinstance(value["tables"], list) and bool(value["tables"])
    )


def _yahoo_scoring_ready(value: object) -> bool:
    return isinstance(value, Mapping) and (
        (set(value) == {"scoring"} and value["scoring"] in {"STD", "HALF", "PPR"})
        or (set(value) == {"error"} and isinstance(value["error"], str))
    )


def _check_cancelled(cancelled) -> None:
    if cancelled():
        raise BrowserCaptureCancelled("browser capture was cancelled")


def _remaining_ms(deadline: float) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise BrowserCaptureTimeout("browser capture operation timed out")
    return remaining
