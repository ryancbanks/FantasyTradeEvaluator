"""Sanitized browser-capture exceptions shared across process boundaries."""


class BrowserCaptureError(RuntimeError):
    """Sanitized failure at the browser-capture boundary."""


class BrowserCaptureDependencyError(BrowserCaptureError):
    """The optional browser runtime is unavailable."""


class BrowserExtensionUpgradeRequired(BrowserCaptureDependencyError):
    """The paired browser extension predates required capture evidence."""


class BrowserCaptureCancelled(BrowserCaptureError):
    """The caller cancelled collection."""


class BrowserCaptureTimeout(BrowserCaptureError):
    """An operation or complete run exceeded its deadline."""


class ProjectionNotPublished(BrowserCaptureError):
    """The exact provider page proves that the requested projection is unpublished."""


class YahooScoringError(BrowserCaptureError):
    """Yahoo league scoring could not be safely verified or did not match."""


class YahooScoringMismatch(YahooScoringError):
    """Yahoo was verified, but its scoring format differs from the refresh."""


__all__ = (
    "BrowserCaptureCancelled", "BrowserCaptureDependencyError", "BrowserCaptureError",
    "BrowserCaptureTimeout", "BrowserExtensionUpgradeRequired", "ProjectionNotPublished",
    "YahooScoringError", "YahooScoringMismatch",
)
