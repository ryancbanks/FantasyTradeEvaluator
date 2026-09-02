"""Backend startup for the optional, killable Playwright capture worker."""

from .browser_capture import (
    BrowserCaptureDependencyError, BrowserCaptureError, BrowserCaptureOptions,
)


class PlaywrightCaptureBackend:
    def __init__(self, loader=None) -> None:
        self._loader = loader

    def open(self, options: BrowserCaptureOptions, timeout_ms: int | None = None, cancelled=None):
        if not isinstance(options, BrowserCaptureOptions):
            raise ValueError("options must be BrowserCaptureOptions")
        timeout_ms = options.navigation_timeout_ms if timeout_ms is None else timeout_ms
        cancelled = (lambda: False) if cancelled is None else cancelled
        if self._loader is None:
            from ._playwright_worker import open_worker_session

            return open_worker_session(options, timeout_ms, cancelled)
        return open_local_session(options, self._loader)


def open_local_session(options: BrowserCaptureOptions, loader=None):
    from ._playwright_capture import _PlaywrightSession

    sync_playwright, timeout_error = (loader or _load_playwright)()
    try:
        manager = sync_playwright().start()
    except Exception:
        raise BrowserCaptureDependencyError(
            "Playwright could not start; reinstall its runtime and Chromium browser."
        ) from None
    try:
        context = manager.chromium.launch_persistent_context(
            str(options.profile_directory), headless=not options.headed,
            accept_downloads=False, timeout=options.navigation_timeout_ms, channel="chromium",
        )
    except Exception as error:
        try:
            manager.stop()
        except Exception:
            pass
        message = str(error).lower()
        if "executable doesn't exist" in message or "playwright install" in message:
            raise BrowserCaptureDependencyError(
                "Playwright Chromium is not installed; run 'playwright install chromium'."
            ) from None
        raise BrowserCaptureError("persistent browser profile could not be opened") from None
    try:
        return _PlaywrightSession(context, manager, timeout_error)
    except Exception:
        try:
            context.close()
        except Exception:
            pass
        try:
            manager.stop()
        except Exception:
            pass
        raise BrowserCaptureError("persistent browser page could not be initialized") from None


def _load_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserCaptureDependencyError(
            "Browser capture requires optional Playwright support; install 'playwright' "
            "and run 'playwright install chromium'."
        ) from None
    return sync_playwright, PlaywrightTimeoutError


__all__ = ("PlaywrightCaptureBackend",)
