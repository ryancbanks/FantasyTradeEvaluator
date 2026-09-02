"""Drive projection filters through visible provider controls."""

from collections.abc import Mapping

from ._capture_errors import BrowserCaptureError
from ._projection_config_script import CONFIGURE_PROJECTION_SCRIPT


def projection_request(task) -> dict[str, object]:
    """Return the exact typed dimensions shared by configuration and extraction."""

    return {
        "provider": task.provider.value,
        "season": task.season,
        "week": task.week,
        "horizon": task.projection.horizon.value,
        "scoring": task.projection.scoring,
        "positions": list(task.projection.position_scope),
    }


def configure_projection(
    page, task, action_delay_ms, deadline, cancelled, wait, remaining_ms, require_page
) -> None:
    request = projection_request(task)
    changes = 0
    while True:
        if cancelled():
            from ._capture_errors import BrowserCaptureCancelled

            raise BrowserCaptureCancelled("browser capture was cancelled")
        try:
            result = page.evaluate(CONFIGURE_PROJECTION_SCRIPT, request)
        except Exception:
            raise BrowserCaptureError("projection filter configuration failed") from None
        require_page()
        if not isinstance(result, Mapping) or result.get("action") not in {
            "ready", "changed", "error"
        }:
            raise BrowserCaptureError("projection filter configuration returned invalid data")
        if result["action"] == "ready":
            return
        if result["action"] == "error":
            if task.provider.value == "yahoo":
                dimension = result.get("dimension")
                messages = {
                    "yahoo controls": (
                        "Yahoo Player List did not expose its required projection filters."
                    ),
                    "yahoo period": (
                        "Yahoo Player List does not offer the selected projection period."
                    ),
                    "yahoo position": (
                        "Yahoo Player List does not offer the requested player position."
                    ),
                }
                raise BrowserCaptureError(messages.get(
                    dimension,
                    "Yahoo Player List filters could not be verified.",
                ))
            raise BrowserCaptureError("projection filters were ambiguous")
        changes += 1
        if changes > 6:
            if task.provider.value == "yahoo":
                raise BrowserCaptureError(
                    "Yahoo Player List did not finish applying the requested filters."
                )
            raise BrowserCaptureError("projection filters did not reach the requested state")
        wait(min(action_delay_ms, remaining_ms(deadline)), cancelled)


__all__ = ("configure_projection", "projection_request")
