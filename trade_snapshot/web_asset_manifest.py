"""Single inventory for web assets served and checked by every release path."""

from types import MappingProxyType


WEB_ASSET_ROUTES = MappingProxyType(
    {
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/app_tabs.js": ("app_tabs.js", "text/javascript; charset=utf-8"),
        "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
        "/dashboard_charts.js": (
            "dashboard_charts.js",
            "text/javascript; charset=utf-8",
        ),
        "/dashboard_ui.js": ("dashboard_ui.js", "text/javascript; charset=utf-8"),
        "/draft_lab.css": ("draft_lab.css", "text/css; charset=utf-8"),
        "/draft_lab.js": ("draft_lab.js", "text/javascript; charset=utf-8"),
        "/gm_insights.css": ("gm_insights.css", "text/css; charset=utf-8"),
        "/gm_insights_evidence_ui.js": (
            "gm_insights_evidence_ui.js",
            "text/javascript; charset=utf-8",
        ),
        "/gm_insights_format.js": (
            "gm_insights_format.js",
            "text/javascript; charset=utf-8",
        ),
        "/gm_insights_ui.js": (
            "gm_insights_ui.js",
            "text/javascript; charset=utf-8",
        ),
        "/league_ui.js": ("league_ui.js", "text/javascript; charset=utf-8"),
        "/player_lab.css": ("player_lab.css", "text/css; charset=utf-8"),
        "/player_lab_catalog_ui.js": (
            "player_lab_catalog_ui.js",
            "text/javascript; charset=utf-8",
        ),
        "/player_lab_profile_ui.js": (
            "player_lab_profile_ui.js",
            "text/javascript; charset=utf-8",
        ),
        "/player_lab_ui.js": (
            "player_lab_ui.js",
            "text/javascript; charset=utf-8",
        ),
        "/progress_ui.js": ("progress_ui.js", "text/javascript; charset=utf-8"),
        "/results_workbench.js": (
            "results_workbench.js",
            "text/javascript; charset=utf-8",
        ),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/three_way_ui.js": ("three_way_ui.js", "text/javascript; charset=utf-8"),
        "/trade_filter_ui.js": (
            "trade_filter_ui.js",
            "text/javascript; charset=utf-8",
        ),
        "/trade_timing.css": ("trade_timing.css", "text/css; charset=utf-8"),
        "/trade_timing_ui.js": (
            "trade_timing_ui.js",
            "text/javascript; charset=utf-8",
        ),
    }
)
REQUIRED_WEB_ASSETS = (
    "index.html",
    *(filename for filename, _content_type in WEB_ASSET_ROUTES.values()),
)


__all__ = ("REQUIRED_WEB_ASSETS", "WEB_ASSET_ROUTES")
