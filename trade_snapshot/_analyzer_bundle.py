"""Read public analyzer-bundle provenance from the retained page."""

import time

from . import bundle_provenance
from ._capture_errors import (
    BrowserCaptureCancelled,
    BrowserCaptureError,
    BrowserCaptureTimeout,
)
from ._capture_scripts import ANALYZER_BUNDLE_SOURCE_SCRIPT


def capture_live_analyzer_bundle(
    page, cache: dict[str, object], timeout_ms: int, cancelled, require_page
):
    require_page()
    if cancelled():
        raise BrowserCaptureCancelled("browser capture was cancelled")
    try:
        url = page.evaluate(ANALYZER_BUNDLE_SOURCE_SCRIPT)
    except Exception:
        raise BrowserCaptureError("analyzer bundle source could not be read") from None
    require_page()
    return fingerprint_analyzer_bundle_url(
        url, cache, timeout_ms, cancelled, require_page
    )


def fingerprint_analyzer_bundle_url(
    url: object, cache: dict[str, object], timeout_ms: int, cancelled, require_page
):
    """Validate and hash the one public bundle URL returned by either browser adapter."""

    deadline = time.monotonic() + timeout_ms / 1000
    if not isinstance(url, str) or not url:
        raise BrowserCaptureError("one public analyzer bundle was not found on the page")
    if url in cache:
        return cache[url]
    remaining = (deadline - time.monotonic()) * 1000
    if remaining <= 0:
        raise BrowserCaptureTimeout("browser capture operation timed out")
    try:
        fingerprint = bundle_provenance.fetch_analyzer_bundle_fingerprint(
            url, timeout_seconds=min(120, remaining / 1000)
        )
    except ValueError:
        raise BrowserCaptureError("public analyzer bundle could not be fingerprinted") from None
    if cancelled():
        raise BrowserCaptureCancelled("browser capture was cancelled")
    require_page()
    cache[url] = fingerprint
    return fingerprint


__all__ = ("capture_live_analyzer_bundle", "fingerprint_analyzer_bundle_url")
