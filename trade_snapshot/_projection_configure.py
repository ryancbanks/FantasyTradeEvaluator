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
    prior_content_fingerprint = None
    content_change_providers = {"fftoday", "fantasysharks"}
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
            "ready", "changed", "waiting", "error"
        }:
            raise BrowserCaptureError("projection filter configuration returned invalid data")
        fingerprint = result.get("fingerprint")
        if (
            task.provider.value in content_change_providers
            and result["action"] != "error"
            and (
                not isinstance(fingerprint, str)
                or not fingerprint
                or len(fingerprint) > 8192
            )
        ):
            raise BrowserCaptureError("projection filter configuration returned invalid data")
        if (
            task.provider.value in content_change_providers
            and result["action"] == "changed"
            and not isinstance(result.get("require_change"), bool)
        ):
            raise BrowserCaptureError("projection filter configuration returned invalid data")
        if result["action"] == "ready":
            if (
                task.provider.value not in content_change_providers
                or prior_content_fingerprint != fingerprint
            ):
                return
            result = {"action": "waiting"}
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
            dimension = result.get("dimension")
            if task.provider.value == "espn" and dimension in {
                "season", "period", "scoring", "position"
            }:
                raise BrowserCaptureError(
                    f"ESPN projections did not expose one verifiable {dimension} filter."
                )
            raise BrowserCaptureError("projection filters were ambiguous")
        if result["action"] == "changed":
            if task.provider.value in content_change_providers:
                prior_content_fingerprint = (
                    fingerprint if result["require_change"] else None
                )
            changes += 1
            if changes > 12:
                if task.provider.value == "yahoo":
                    raise BrowserCaptureError(
                        "Yahoo Player List did not finish applying the requested filters."
                    )
                raise BrowserCaptureError(
                    "projection filters did not reach the requested state"
                )
        wait(min(action_delay_ms, remaining_ms(deadline)), cancelled)


__all__ = ("configure_projection", "projection_request")
