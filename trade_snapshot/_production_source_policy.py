"""Runtime-only source linkage and navigation policy for weekly collection."""

from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit

from .capture_schema import (
    ANALYZER_RESPONSE_SCHEMA_FINGERPRINT,
    CaptureKind,
    CaptureProvider,
    LeagueSourceKind,
)
from .weekly_collection import WeeklyCollectionError


_ESPN_PROJECTION_URL = "https://fantasy.espn.com/football/players/projections"


def league_metadata(artifact):
    source = next(
        row for row in artifact.sources if row.source is LeagueSourceKind.BOOTSTRAP
    )
    league = source.to_record()["body"]["payload"]["league"]
    if not isinstance(league, dict):
        raise ValueError("FantasyPros league metadata is invalid")
    return league


def espn_host_id(metadata, configured_url):
    host = str(metadata.get("host", "")).strip()
    if host and host.upper() != "ESPN":
        raise WeeklyCollectionError("The captured FantasyPros league is not linked to ESPN.")
    host_id = metadata.get("host_league_id")
    if host_id is not None:
        provider_id("host_league_id", host_id)
    configured_id = None
    if configured_url is not None:
        parsed = urlsplit(configured_url)
        values = dict(parse_qsl(parsed.query, keep_blank_values=True))
        configured_id = values.get("leagueId")
        provider_id("configured ESPN leagueId", configured_id)
        if (parsed.hostname or "").casefold() != "fantasy.espn.com":
            raise WeeklyCollectionError(
                "The configured ESPN league link could not be verified."
            )
        if host_id is not None and configured_id != host_id:
            raise WeeklyCollectionError(
                "The configured host league does not match the signed-in FantasyPros league."
            )
    resolved = host_id or configured_id
    if resolved is None:
        raise WeeklyCollectionError(
            "Paste an ESPN League Home link so the app can identify the linked league."
        )
    return resolved


def runtime_bindings(plan, host_id, yahoo_url):
    espn_url = espn_projection_url(host_id)
    result = {}
    for task in plan.tasks:
        if task.kind is not CaptureKind.VISIBLE_TABLE:
            continue
        if task.provider is CaptureProvider.ESPN:
            result[task.task_id] = espn_url
        elif task.provider is CaptureProvider.YAHOO:
            if yahoo_url is None:
                raise ValueError("Yahoo projection task requires a Yahoo league URL")
            result[task.task_id] = yahoo_url
    return result


def espn_projection_url(host_id):
    """Build the only signed-in ESPN page allowed to precede fallback reads."""

    provider_id("host_league_id", host_id)
    return f"{_ESPN_PROJECTION_URL}?{urlencode({'leagueId': host_id})}"


def validate_host_scoring(host, requested):
    profile = getattr(host, "scoring_profile", None)
    if getattr(profile, "platform", "").casefold() != "espn":
        raise ValueError("host scoring profile must come from ESPN")
    settings = getattr(profile, "settings", None)
    scoring = settings.get("scoring_settings") if isinstance(settings, Mapping) else None
    rank_type = scoring.get("playerRankType") if isinstance(scoring, Mapping) else None
    expected = {"STD": "STANDARD", "HALF": "HALF_PPR", "PPR": "PPR"}.get(requested)
    if rank_type != expected:
        raise ValueError("ESPN playerRankType does not match requested scoring")


def response_schema_digest():
    prefix, separator, digest = ANALYZER_RESPONSE_SCHEMA_FINGERPRINT.partition("_")
    if prefix != "capschema" or separator != "_" or len(digest) != 64:
        raise ValueError("analyzer response schema fingerprint is invalid")
    return digest


def provider_id(name, value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or value.startswith("0") or len(value) > 20:
        raise ValueError(f"{name} must be a positive decimal provider ID")


__all__ = (
    "espn_host_id", "espn_projection_url", "league_metadata", "provider_id", "response_schema_digest",
    "runtime_bindings", "validate_host_scoring",
)
