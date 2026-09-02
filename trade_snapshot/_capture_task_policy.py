"""Small provider-page policies for capture tasks."""

from urllib.parse import parse_qsl, urlsplit, urlunsplit
import re

from ._capture_common import schema_fingerprint


VISIBLE_PAGE_PATHS = {
    "fantasypros": r"/nfl/projections/[a-z0-9-]+\.php",
    "espn": r"/football/players/projections/?",
    "yahoo": r"/f1/players/?",
    "cbs": (
        r"/fantasy/football/stats/(?:QB|RB|WR|TE|K|DST)/20[0-9]{2}/"
        r"season/projections/(?:ppr|nonppr)/?"
    ),
    "fftoday": r"/rankings/player(?:wk)?proj\.php",
    "fantasysharks": r"/apps/bert/forecasts/projections\.php",
}
VISIBLE_PAGE_HOSTS = {
    "fantasypros": frozenset({"fantasypros.com", "www.fantasypros.com"}),
    "espn": frozenset({"fantasy.espn.com"}),
    "yahoo": frozenset({"football.fantasysports.yahoo.com"}),
    "cbs": frozenset({"cbssports.com", "www.cbssports.com"}),
    "fftoday": frozenset({"fftoday.com", "www.fftoday.com"}),
    "fantasysharks": frozenset(
        {"fantasysharks.com", "www.fantasysharks.com"}
    ),
}
FFTODAY_POSITION_SCOPES = {
    "weekly": frozenset({"QB", "RB", "WR", "TE", "K"}),
    "ros": frozenset({"QB", "RB", "WR", "TE", "K", "DL", "LB", "DB"}),
}
YAHOO_BOUND_PROJECTION_PATH = (
    r"/(?:20[0-9]{2}/)?f1/(?P<league>[1-9][0-9]{0,19})/"
    r"(?P<page>players|playersearch)/?"
)
YAHOO_BOUND_SETTINGS_PATH = (
    r"/(?:20[0-9]{2}/)?f1/(?P<league>[1-9][0-9]{0,19})/settings/?"
)


def validate_visible_table_task(provider, url: str, projection=None) -> None:
    name = getattr(provider, "value", provider)
    parsed = urlsplit(url)
    if (
        (parsed.hostname or "").casefold() not in VISIBLE_PAGE_HOSTS.get(name, frozenset())
        or not re.fullmatch(VISIBLE_PAGE_PATHS.get(name, r"(?!)"), parsed.path)
    ):
        raise ValueError("visible_table task URL does not match the provider projection page")
    if name == "fftoday" and projection is not None:
        horizon = getattr(getattr(projection, "horizon", None), "value", None)
        positions = getattr(projection, "position_scope", ())
        expected_path = {
            "weekly": "/rankings/playerwkproj.php",
            "ros": "/rankings/playerproj.php",
        }.get(horizon)
        if (
            expected_path != parsed.path
            or len(positions) != 1
            or positions[0] not in FFTODAY_POSITION_SCOPES.get(horizon, frozenset())
        ):
            raise ValueError(
                "FFToday projection task requests an unsupported period or position"
            )


def runtime_path_matches_task(task, runtime_path: str) -> bool:
    """Allow a runtime-only Yahoo league binding for a generic Yahoo task."""

    expected = urlsplit(task.url).path
    return runtime_path == expected or (
        getattr(task.provider, "value", task.provider) == "yahoo"
        and bool(re.fullmatch(YAHOO_BOUND_PROJECTION_PATH, runtime_path))
    )


def page_path_matches_task(task, actual_path: str, planned_path: str) -> bool:
    """Accept only the planned path, plus Yahoo's same-league page alias."""

    expected = planned_path
    if actual_path == expected:
        return True
    if getattr(task.provider, "value", task.provider) != "yahoo":
        return False
    actual = re.fullmatch(YAHOO_BOUND_PROJECTION_PATH, actual_path)
    planned = re.fullmatch(YAHOO_BOUND_PROJECTION_PATH, planned_path)
    seasons = {_yahoo_season(actual_path), _yahoo_season(planned_path)} - {None}
    return bool(
        actual
        and planned
        and actual.group("league") == planned.group("league")
        and (not seasons or seasons == {str(task.season)})
    )


def yahoo_settings_url(players_url: str, season: int) -> str:
    """Convert one normalized Yahoo player-list URL to its same-league settings page."""

    if not isinstance(players_url, str) or type(season) is not int:
        raise ValueError("Yahoo player-list URL is invalid")
    try:
        parsed, port = urlsplit(players_url), urlsplit(players_url).port
    except ValueError:
        raise ValueError("Yahoo player-list URL is invalid") from None
    match = re.fullmatch(YAHOO_BOUND_PROJECTION_PATH, parsed.path)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    path_season = _yahoo_season(parsed.path)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username or parsed.password or port not in (None, 443)
        or (parsed.hostname or "").casefold().rstrip(".")
        != "football.fantasysports.yahoo.com"
        or parsed.fragment or not match or match.group("page") != "players"
        or query != [("status", "ALL")]
        or (path_season is not None and path_season != str(season))
    ):
        raise ValueError("Yahoo player-list URL is invalid")
    path = f"/{season}/f1/{match.group('league')}/settings"
    return urlunsplit(("https", "football.fantasysports.yahoo.com", path, "", ""))


def yahoo_settings_path_matches(task, actual_path: str, planned_path: str) -> bool:
    """Accept only a same-league Yahoo settings path for the requested season."""

    actual = re.fullmatch(YAHOO_BOUND_SETTINGS_PATH, actual_path)
    planned = re.fullmatch(YAHOO_BOUND_SETTINGS_PATH, planned_path)
    seasons = {_yahoo_season(actual_path), _yahoo_season(planned_path)} - {None}
    return bool(
        actual
        and planned
        and actual.group("league") == planned.group("league")
        and (not seasons or seasons == {str(task.season)})
    )


def validate_yahoo_settings_url(task, value: str) -> None:
    """Reject settings navigation outside the task's Yahoo league/season boundary."""

    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("Yahoo settings URL is invalid")
    try:
        parsed, port = urlsplit(value), urlsplit(value).port
    except ValueError:
        raise ValueError("Yahoo settings URL is invalid") from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username or parsed.password or port not in (None, 443)
        or (parsed.hostname or "").casefold().rstrip(".")
        != "football.fantasysports.yahoo.com"
        or parsed.query or parsed.fragment
        or _yahoo_season(parsed.path) != str(task.season)
        or not yahoo_settings_path_matches(task, parsed.path, parsed.path)
    ):
        raise ValueError("Yahoo settings URL is invalid")


def _yahoo_season(path: str) -> str | None:
    match = re.fullmatch(r"/(?:((?:20)[0-9]{2})/)?f1/.*", path)
    return match.group(1) if match else None


def capture_plan_fingerprint(task_fingerprint: str, ecr_fingerprint: str) -> str:
    return schema_fingerprint(
        "capture_plan",
        {
            "fields": ["schema_version", "schema_fingerprint", "tasks", "plan_id"],
            "child_task_fingerprints": [task_fingerprint, ecr_fingerprint],
        },
    )


def page_task_fingerprint(kinds, analyzer_phases, provider_hosts) -> str:
    return schema_fingerprint(
        "page_capture_task",
        {
            "fields": [
                "provider", "season", "week", "kind", "url", "analyzer_phase",
                "analyzer_trade", "projection", "task_id",
            ],
            "kinds": [kind.value for kind in kinds],
            "analyzer_phases": [phase.value for phase in analyzer_phases],
            "provider_hosts": {
                provider.value: sorted(hosts) for provider, hosts in provider_hosts.items()
            },
            "visible_page_paths": VISIBLE_PAGE_PATHS,
            "visible_page_hosts": {
                name: sorted(hosts) for name, hosts in VISIBLE_PAGE_HOSTS.items()
            },
            "yahoo_bound_projection_path": YAHOO_BOUND_PROJECTION_PATH,
            "fftoday_position_scopes": {
                horizon: sorted(positions)
                for horizon, positions in FFTODAY_POSITION_SCOPES.items()
            },
            "league_source_path": "/nfl/myplaybook/trade-analyzer.php",
            "policy_version": "bound-public-projection-surfaces-v7",
        },
    )


def validate_league_source_task(provider, url: str, attached: tuple[object, ...]) -> None:
    parsed = urlsplit(url)
    if getattr(provider, "value", provider) != "fantasypros":
        raise ValueError("league_source tasks must use FantasyPros")
    if (
        (parsed.hostname or "").casefold()
        not in {"fantasypros.com", "www.fantasypros.com"}
        or parsed.path != "/nfl/myplaybook/trade-analyzer.php"
    ):
        raise ValueError("league_source tasks require the FantasyPros trade analyzer page")
    if any(value is not None for value in attached):
        raise ValueError("league_source tasks cannot include analyzer or projection fields")


__all__ = (
    "FFTODAY_POSITION_SCOPES", "VISIBLE_PAGE_HOSTS", "VISIBLE_PAGE_PATHS",
    "capture_plan_fingerprint",
    "page_path_matches_task", "page_task_fingerprint",
    "runtime_path_matches_task",
    "validate_league_source_task",
    "validate_visible_table_task", "validate_yahoo_settings_url",
    "yahoo_settings_path_matches", "yahoo_settings_url",
)
