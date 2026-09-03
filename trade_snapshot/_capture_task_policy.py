"""Small provider-page policies for capture tasks."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re

from ._capture_common import require_safe_https_url, schema_fingerprint


VISIBLE_PAGE_PATHS = {
    "fantasypros": r"/nfl/projections/[a-z0-9-]+\.php",
    "espn": r"/football/players/projections/?",
    "yahoo": r"/f1/players/?",
}
VISIBLE_PAGE_HOSTS = {
    "fantasypros": frozenset({"fantasypros.com", "www.fantasypros.com"}),
    "espn": frozenset({"fantasy.espn.com"}),
    "yahoo": frozenset({"football.fantasysports.yahoo.com"}),
}
YAHOO_BOUND_PROJECTION_PATH = (
    r"/(?:20[0-9]{2}/)?f1/(?P<league>[1-9][0-9]{0,19})/"
    r"(?P<page>players|playersearch)/?"
)
YAHOO_BOUND_SETTINGS_PATH = (
    r"/(?:20[0-9]{2}/)?f1/(?P<league>[1-9][0-9]{0,19})/settings/?"
)


def canonical_visible_table_task_url(provider, url: str, *, week: int, projection) -> str:
    """Validate and canonicalize one provider projection-page URL."""

    name = getattr(provider, "value", provider)
    if name != "fantasypros":
        canonical = require_safe_https_url(
            url,
            allowed_hosts=VISIBLE_PAGE_HOSTS.get(name, frozenset()),
        )
        validate_visible_table_task(provider, canonical)
        return canonical
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        raise ValueError("visible_table task URL is invalid") from None
    if parsed.fragment:
        raise ValueError("FantasyPros projection task URL cannot contain a fragment")
    base = require_safe_https_url(
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
        allowed_hosts=VISIBLE_PAGE_HOSTS["fantasypros"],
    )
    parsed = urlsplit(base)
    positions = getattr(projection, "position_scope", ())
    if len(positions) != 1:
        raise ValueError("FantasyPros projection tasks require one exact position")
    expected_path = f"/nfl/projections/{positions[0].casefold()}.php"
    horizon = getattr(getattr(projection, "horizon", None), "value", None)
    scoring = getattr(projection, "scoring", None)
    expected_query = _fantasypros_projection_query(week, horizon, scoring)
    actual_query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    if parsed.path != expected_path or sorted(actual_query) != sorted(
        expected_query
    ) or len(actual_query) != len(expected_query):
        raise ValueError(
            "FantasyPros projection task URL must encode its exact position, period, and scoring"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(expected_query), ""))


def fantasypros_projection_url(position: str, *, week: int, horizon: str, scoring: str) -> str:
    """Build the exact public FantasyPros URL for one projection dimension set."""

    if not isinstance(position, str) or not re.fullmatch(r"[A-Z]+", position):
        raise ValueError("FantasyPros projection position is invalid")
    query = _fantasypros_projection_query(week, horizon, scoring)
    return urlunsplit((
        "https",
        "www.fantasypros.com",
        f"/nfl/projections/{position.casefold()}.php",
        urlencode(query),
        "",
    ))


def _fantasypros_projection_query(week, horizon, scoring):
    if type(week) is not int or not 1 <= week <= 25:
        raise ValueError("FantasyPros projection week is invalid")
    if horizon != "weekly" or scoring not in {"STD", "HALF", "PPR"}:
        raise ValueError("FantasyPros projection dimensions are invalid")
    query = [("week", str(week))]
    if scoring != "HALF":
        query.append(("scoring", scoring))
    return query


def validate_visible_table_task(provider, url: str) -> None:
    name = getattr(provider, "value", provider)
    parsed = urlsplit(url)
    if (
        (parsed.hostname or "").casefold() not in VISIBLE_PAGE_HOSTS.get(name, frozenset())
        or not re.fullmatch(VISIBLE_PAGE_PATHS.get(name, r"(?!)"), parsed.path)
    ):
        raise ValueError("visible_table task URL does not match the provider projection page")


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
            "fantasypros_projection_query": {
                "weekly": "week=<week>",
                "ros": "unsupported_without_a_public_visible_source",
                "scoring": {"HALF": "default", "PPR": "PPR", "STD": "STD"},
            },
            "league_source_path": "/nfl/myplaybook/trade-analyzer.php",
            "policy_version": "dimension-bound-projection-pages-v7",
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
    "VISIBLE_PAGE_HOSTS", "VISIBLE_PAGE_PATHS", "canonical_visible_table_task_url",
    "capture_plan_fingerprint", "fantasypros_projection_url",
    "page_path_matches_task", "page_task_fingerprint",
    "runtime_path_matches_task",
    "validate_league_source_task",
    "validate_visible_table_task", "validate_yahoo_settings_url",
    "yahoo_settings_path_matches", "yahoo_settings_url",
)
