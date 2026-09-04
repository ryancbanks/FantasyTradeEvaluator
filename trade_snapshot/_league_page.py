"""Validate one bounded league-source result from the signed-in page."""

from collections.abc import Mapping

from ._capture_errors import BrowserCaptureError
from ._capture_league import LeagueSource, REQUIRED_LEAGUE_SOURCES
from ._capture_runtime import LeagueCaptureData
from ._league_script import LEAGUE_SOURCE_SCRIPT


_CAPTURE_FAILURES = {
    "signed_out": "FantasyPros requires a signed-in league",
    "provenance": "FantasyPros redirected away from the Trade Analyzer",
    "bootstrap": "FantasyPros Trade Analyzer page data did not finish loading",
    "bootstrap_incomplete": "FantasyPros loaded, but its league roster data was incomplete",
    "analyzer_init_incomplete": (
        "FantasyPros loaded, but the Trade Analyzer initialization response was not captured"
    ),
    "projected_standings_incomplete": (
        "FantasyPros loaded, but projected standings were unavailable for this league"
    ),
    "task_dimensions": "The FantasyPros collection season or week was invalid",
}


def capture_league_sources(page, task, timeout_ms, cancelled, require_page):
    if task.kind.value != "league_source" or task.provider.value != "fantasypros":
        raise BrowserCaptureError("FantasyPros league-source task is invalid")
    require_page()
    if cancelled():
        from ._capture_errors import BrowserCaptureCancelled

        raise BrowserCaptureCancelled("browser capture was cancelled")
    try:
        raw = page.evaluate(
            LEAGUE_SOURCE_SCRIPT,
            {
                "timeout_ms": timeout_ms,
                "expected_season": task.season,
                "expected_week": task.week,
            },
        )
    except Exception:
        raise BrowserCaptureError("FantasyPros league-source extraction failed") from None
    require_page()
    return league_capture_data(raw, task)


def league_capture_data(raw, task):
    """Validate a raw extension or page result at the common trust boundary."""

    if task.kind.value != "league_source" or task.provider.value != "fantasypros":
        raise BrowserCaptureError("FantasyPros league-source task is invalid")
    if not isinstance(raw, Mapping) or set(raw) != {"team_count", "sources"}:
        detail = raw.get("error") if isinstance(raw, Mapping) else None
        if isinstance(detail, str) and detail in _CAPTURE_FAILURES:
            raise BrowserCaptureError(_CAPTURE_FAILURES[detail])
        raise BrowserCaptureError("FantasyPros league sources were incomplete")
    rows = raw["sources"]
    if not isinstance(rows, list) or len(rows) != len(REQUIRED_LEAGUE_SOURCES) or any(
        not isinstance(row, Mapping) or set(row) != {"source", "body"} for row in rows
    ):
        raise BrowserCaptureError("FantasyPros league sources were incomplete")
    try:
        return LeagueCaptureData(
            raw["team_count"], (LeagueSource(row["source"], row["body"]) for row in rows)
        )
    except (KeyError, ValueError, TypeError):
        raise BrowserCaptureError("FantasyPros league sources failed validation") from None


__all__ = ("capture_league_sources", "league_capture_data")
