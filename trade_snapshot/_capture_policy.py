"""Provider-link and analyzer-response selection policies."""

from collections.abc import Iterable, Mapping
from math import isfinite
from numbers import Real
import re
from urllib.parse import urlsplit

from ._capture_common import sanitized_visible_link
from ._capture_plan import (
    AnalyzerCapturePhase,
    CaptureProvider,
    _enum_value,
)


ANALYZER_BODY_POLICY_DESCRIPTOR = {
    "version": 3,
    "ordinary": {
        "periods": ["ros", "dynasty"],
        "paths": [
            "*.powerRankings.before[].teamId",
            "*.powerRankings.before[].score_decimal",
            "*.powerRankings.after[].teamId",
            "*.powerRankings.after[].score_decimal",
        ],
    },
    "full": {
        "paths": [
            "playoffs.oddsBefore_team1", "playoffs.oddsAfter_team1",
            "playoffs.oddsBefore_team2", "playoffs.oddsAfter_team2",
        ]
    },
    "error_keys_rejected_recursively": ["error", "errors"],
}


def public_player_link(
    provider: CaptureProvider | str,
    value: object,
) -> str | None:
    """Return one sanitized provider-public player link, otherwise ``None``."""

    try:
        provider = _enum_value(CaptureProvider, "provider", provider)
        link = sanitized_visible_link(value)
    except ValueError:
        return None
    parsed = urlsplit(link)
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path
    if provider is CaptureProvider.ESPN:
        allowed = (
            host in {"espn.com", "www.espn.com"}
            and re.fullmatch(r"/nfl/player/_/id/[0-9]+(?:/[^/]*)?/?", path)
        )
    elif provider is CaptureProvider.YAHOO:
        allowed = (
            host == "sports.yahoo.com"
            and (
                re.fullmatch(r"/nfl/players/[0-9]+/?", path)
                or re.fullmatch(
                    r"/nfl/teams/[a-z0-9]+(?:-[a-z0-9]+)*/?",
                    path,
                    re.IGNORECASE,
                )
            )
        )
    elif provider is CaptureProvider.FANTASYPROS:
        allowed = (
            host in {"fantasypros.com", "www.fantasypros.com"}
            and re.fullmatch(r"/nfl/players/[a-z0-9-]+\.php", path, re.IGNORECASE)
        )
    elif provider is CaptureProvider.CBS:
        allowed = (
            host in {"cbssports.com", "www.cbssports.com"}
            and (
                re.fullmatch(
                    r"/nfl/players/[0-9]+/[a-z0-9-]+/fantasy/?",
                    path,
                    re.IGNORECASE,
                )
                or re.fullmatch(
                    r"/nfl/teams/[a-z]{2,3}/[a-z0-9]+(?:-[a-z0-9]+)*/?",
                    path,
                    re.IGNORECASE,
                )
            )
        )
    elif provider is CaptureProvider.FFTODAY:
        allowed = (
            host in {"fftoday.com", "www.fftoday.com"}
            and re.fullmatch(
                r"/stats/players/[0-9]+/[A-Za-z0-9_.'-]+/?",
                path,
            )
        )
    elif provider is CaptureProvider.FANTASYSHARKS:
        allowed = (
            host in {"fantasysharks.com", "www.fantasysharks.com"}
            and path == "/apps/bert/players/playerpage.php"
            and re.fullmatch(r"id=[1-9][0-9]{0,9}", parsed.query or "")
        )
    else:
        allowed = False
    return link if allowed else None


def validate_public_player_links(
    provider: CaptureProvider,
    links: Iterable[str],
) -> None:
    for link in links:
        if public_player_link(provider, link) != link:
            raise ValueError(
                "table links may contain only provider public identity paths"
            )


def validate_analyzer_body_phase(
    phase: AnalyzerCapturePhase,
    body: object,
) -> None:
    if not isinstance(body, Mapping):
        raise ValueError("analyzer response body must remain a JSON object after sanitization")
    if _contains_error(body):
        raise ValueError("analyzer error body cannot satisfy a capture phase")
    if phase is AnalyzerCapturePhase.ORDINARY_POWER:
        _validate_ordinary(body)
    else:
        _validate_playoffs(body)


def analyzer_body_matches_phase(
    body: object,
    phase: AnalyzerCapturePhase | str,
) -> bool:
    """Check raw response semantics without sanitizing, copying, or persisting it."""

    try:
        normalized_phase = _enum_value(AnalyzerCapturePhase, "analyzer_phase", phase)
        validate_analyzer_body_phase(normalized_phase, body)
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def project_analyzer_body(
    phase: AnalyzerCapturePhase | str,
    body: object,
) -> dict[str, object]:
    """Return the complete phase result using an explicit persistence allowlist."""

    normalized_phase = _enum_value(AnalyzerCapturePhase, "analyzer_phase", phase)
    validate_analyzer_body_phase(normalized_phase, body)
    if normalized_phase is AnalyzerCapturePhase.ORDINARY_POWER:
        period_name = next(name for name in ("ros", "dynasty") if name in body)
        rankings = body[period_name]["powerRankings"]
        return {
            period_name: {
                "powerRankings": {
                    moment: [
                        {"teamId": row["teamId"], "score_decimal": row["score_decimal"]}
                        for row in rankings[moment]
                    ]
                    for moment in ("before", "after")
                }
            }
        }
    fields = (
        "oddsBefore_team1", "oddsAfter_team1",
        "oddsBefore_team2", "oddsAfter_team2",
    )
    return {"playoffs": {field: body["playoffs"][field] for field in fields}}


def _validate_ordinary(body: Mapping[str, object]) -> None:
    period_keys = [key for key in ("ros", "dynasty") if key in body]
    if len(period_keys) != 1 or "playoffs" in body:
        raise ValueError("ordinary_power response must contain one power period")
    period = body[period_keys[0]]
    if not isinstance(period, Mapping):
        raise ValueError("ordinary_power response period must be an object")
    rankings = period.get("powerRankings")
    if not isinstance(rankings, Mapping):
        raise ValueError("ordinary_power response must contain nested powerRankings")
    before = _validated_power_rows(rankings, "before")
    after = _validated_power_rows(rankings, "after")
    if before != after:
        raise ValueError("ordinary_power before/after team sets must match")


def _validate_playoffs(body: Mapping[str, object]) -> None:
    if any(key in body for key in ("ros", "dynasty")):
        raise ValueError("full_playoffs response cannot contain an ordinary power period")
    playoffs = body.get("playoffs")
    fields = (
        "oddsBefore_team1",
        "oddsAfter_team1",
        "oddsBefore_team2",
        "oddsAfter_team2",
    )
    if not isinstance(playoffs, Mapping) or not all(field in playoffs for field in fields):
        raise ValueError("full_playoffs response must contain both teams' before/after odds")
    for field in fields:
        value = playoffs[field]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("full_playoffs odds must be finite percentages")
        number = float(value)
        if not isfinite(number) or not 0 <= number <= 100:
            raise ValueError("full_playoffs odds must be finite percentages")


def _validated_power_rows(rankings: Mapping[str, object], moment: str) -> set[str]:
    rows = rankings.get(moment)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"ordinary_power {moment} rankings must be a nonempty array")
    team_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or "teamId" not in row or "score_decimal" not in row:
            raise ValueError("ordinary_power rows require teamId and score_decimal")
        team_id = row["teamId"]
        if isinstance(team_id, bool) or not (
            (type(team_id) is int and team_id >= 0)
            or (isinstance(team_id, str) and bool(team_id.strip()))
        ):
            raise ValueError("ordinary_power teamId is invalid")
        canonical_id = str(team_id)
        if canonical_id in team_ids:
            raise ValueError("ordinary_power rankings contain a duplicate teamId")
        score = row["score_decimal"]
        if isinstance(score, bool) or not isinstance(score, Real) or not isfinite(float(score)):
            raise ValueError("ordinary_power score_decimal must be finite")
        team_ids.add(canonical_id)
    return team_ids


def _contains_error(value: object) -> bool:
    if isinstance(value, Mapping):
        if "error" in value or "errors" in value:
            return True
        return any(_contains_error(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_error(child) for child in value)
    return False
