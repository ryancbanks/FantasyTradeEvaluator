"""One bounded public read of ESPN's full-season projection table."""

from collections.abc import Callable, Mapping
import json
from math import isfinite
import re
from time import monotonic
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .browser_capture import BrowserCaptureCancelled, BrowserCaptureError
from .capture_schema import CaptureProvider, PageCaptureTask, RankingHorizon


_READ_ROOT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
_PLAYER_LIMIT = 5000
_MAXIMUM_SAFE_JSON_INTEGER = (1 << 53) - 1
_SCORING_FORMAT = {"STD": 1, "PPR": 3, "HALF": 8}
_SCORING_LABEL = {"STD": "Standard", "PPR": "PPR", "HALF": "Half PPR"}
_POSITION = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
_PRO_TEAM = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE",
    6: "DAL", 7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND",
    12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE",
    18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT",
    24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}
_RAW_STATS = (
    ("CMP", "1"),
    ("PASS ATT", "0"),
    ("PASS YDS", "3"),
    ("PASS TD", "4"),
    ("INT", "15"),
    ("RUSH ATT", "23"),
    ("RUSH YDS", "24"),
    ("RUSH TD", "25"),
    ("REC", "53"),
    ("TGT", "58"),
    ("REC YDS", "42"),
    ("REC TD", "43"),
    ("FUM", "72"),
    ("FUM LOST", "73"),
)
_HEADERS = ("PLAYER", "TEAM", "POS", "GP", "FPTS", "FPPG") + tuple(
    name for name, _ in _RAW_STATS
)


class EspnProjectionReadError(BrowserCaptureError):
    """ESPN's public season-projection response could not be proven safe."""


class EspnSeasonProjectionClient:
    """Fetch and sanitize one page-backed ESPN season projection response."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        maximum_bytes: int = 16 * 1024 * 1024,
        opener: Callable | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be from 1 through 60")
        if type(maximum_bytes) is not int or not 1024 <= maximum_bytes <= 64 * 1024 * 1024:
            raise ValueError("maximum_bytes must be from 1024 through 67108864")
        if opener is not None and not callable(opener):
            raise ValueError("opener must be callable")
        self._timeout = float(timeout_seconds)
        self._maximum = maximum_bytes
        self._opener = opener or _open_without_redirects

    def __call__(self, task: PageCaptureTask, cancelled: Callable[[], bool]) -> dict[str, object]:
        _task(task)
        if not callable(cancelled):
            raise ValueError("cancelled must be callable")
        scoring = task.projection.scoring
        format_id = _SCORING_FORMAT[scoring]
        expected_url = espn_season_projection_url(task.season, scoring)
        _check_cancelled(cancelled)
        payload = self._read(
            expected_url,
            espn_season_projection_filter(task.season),
            cancelled,
        )
        _check_cancelled(cancelled)
        return espn_season_projection_segment(
            payload,
            season=task.season,
            scoring=scoring,
            league_format_id=format_id,
            position_scope=task.projection.position_scope,
        )

    def _read(self, expected_url, fantasy_filter, cancelled):
        deadline = monotonic() + self._timeout
        request = Request(
            expected_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "fantasy-trade-evaluator/0.2.1",
                "X-Fantasy-Filter": fantasy_filter,
                "X-Fantasy-Platform": "espn-fantasy-web",
                "X-Fantasy-Source": "kona",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                if getattr(response, "status", 200) != 200 or response.geturl() != expected_url:
                    raise EspnProjectionReadError(
                        "ESPN returned an unexpected season-projection response"
                    )
                headers = response.headers
                media_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if media_type != "application/json" or headers.get("Content-Encoding"):
                    raise EspnProjectionReadError(
                        "ESPN returned an unsupported season-projection response"
                    )
                declared = headers.get("Content-Length")
                if declared is not None and _content_length(declared) > self._maximum:
                    raise EspnProjectionReadError(
                        "ESPN season projections exceeded the size limit"
                    )
                body = _bounded_body(response, self._maximum, deadline, cancelled)
        except EspnProjectionReadError:
            raise
        except HTTPError as error:
            if error.geturl() == expected_url and error.code in {401, 403}:
                raise EspnProjectionReadError(
                    "ESPN denied the public season-projection read"
                ) from None
            raise EspnProjectionReadError(
                "ESPN season projections could not be read"
            ) from None
        except BrowserCaptureCancelled:
            raise
        except Exception:
            raise EspnProjectionReadError(
                "ESPN season projections could not be read"
            ) from None
        if not body or len(body) > self._maximum:
            raise EspnProjectionReadError("ESPN season projections had an invalid size")
        try:
            value = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise EspnProjectionReadError(
                "ESPN season projections were not valid JSON"
            ) from None
        if not isinstance(value, Mapping):
            raise EspnProjectionReadError("ESPN season projections had an invalid shape")
        return value


def espn_season_projection_url(season: int, scoring: str) -> str:
    """Return the exact public endpoint backing ESPN's projection page."""

    season = _season(season)
    scoring = _scoring(scoring)
    return (
        f"{_READ_ROOT}/seasons/{season}/segments/0/leaguedefaults/"
        f"{_SCORING_FORMAT[scoring]}?view=kona_player_info"
    )


def espn_season_projection_filter(season: int) -> str:
    """Build the bounded filter observed on ESPN's full projection page."""

    season = _season(season)
    value = {
        "players": {
            "filterStatsForExternalIds": {"value": [season]},
            "filterSlotIds": {
                "value": [
                    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
                    16, 17, 18, 19, 23, 24,
                ]
            },
            "filterStatsForSourceIds": {"value": [1]},
            "useFullProjectionTable": {"value": True},
            "sortAppliedStatTotal": {
                "sortAsc": False, "sortPriority": 3, "value": f"10{season}",
            },
            "sortDraftRanks": {
                "sortPriority": 2, "sortAsc": True, "value": "PPR",
            },
            "sortPercOwned": {"sortPriority": 4, "sortAsc": False},
            "limit": _PLAYER_LIMIT,
            "filterRanksForSlotIds": {
                "value": [0, 2, 4, 6, 17, 16, 8, 9, 10, 12, 13, 24, 11, 14, 15]
            },
        }
    }
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def espn_season_projection_segment(
    payload: object,
    *,
    season: int,
    scoring: str,
    league_format_id: int,
    position_scope: tuple[str, ...] = ("ALL",),
) -> dict[str, object]:
    """Project ESPN JSON into the exact sanitized visible-table segment shape."""

    season = _season(season)
    scoring = _scoring(scoring)
    if type(league_format_id) is not int or league_format_id != _SCORING_FORMAT[scoring]:
        raise EspnProjectionReadError(
            "ESPN season-projection scoring did not match the request"
        )
    positions = _positions(position_scope)
    if not isinstance(payload, Mapping) or set(payload) != {"players"}:
        raise EspnProjectionReadError("ESPN season projections had an invalid shape")
    wrappers = payload["players"]
    if not isinstance(wrappers, list) or not wrappers or len(wrappers) >= _PLAYER_LIMIT:
        raise EspnProjectionReadError(
            "ESPN season projections did not prove a complete player response"
        )
    selected = set(_POSITION.values()) if positions == ("ALL",) else set(positions)
    rows = []
    seen = set()
    for wrapper in wrappers:
        player = _mapping(wrapper, "player wrapper").get("player")
        player = _mapping(player, "player")
        player_id = _player_id(player.get("id"))
        if player_id in seen:
            raise EspnProjectionReadError("ESPN season projections repeated a player ID")
        seen.add(player_id)
        stats = player.get("stats")
        if (
            not isinstance(stats, list)
            or len(stats) > 512
            or any(not isinstance(row, Mapping) for row in stats)
        ):
            raise EspnProjectionReadError("ESPN player projection history was invalid")
        matches = [
            row for row in stats
            if isinstance(row, Mapping)
            and row.get("seasonId") == season
            and row.get("statSourceId") == 1
            and row.get("scoringPeriodId") == 0
        ]
        if len(matches) > 1:
            raise EspnProjectionReadError(
                "ESPN season projections repeated an exact projection row"
            )
        if not matches:
            continue
        position_id = _integer(player.get("defaultPositionId"), "player position")
        team_id = _integer(player.get("proTeamId"), "NFL team")
        if position_id not in _POSITION or team_id not in _PRO_TEAM:
            raise EspnProjectionReadError(
                "ESPN season projections contained unsupported player metadata"
            )
        position = _POSITION[position_id]
        if position not in selected:
            continue
        projection = matches[0]
        if (
            projection.get("id") != f"10{season}"
            or projection.get("externalId") != str(season)
            or projection.get("statSplitTypeId") != 0
        ):
            raise EspnProjectionReadError(
                "ESPN season projection provenance did not match the request"
            )
        total = _number(projection.get("appliedTotal"), "applied total")
        average = _number(projection.get("appliedAverage"), "applied average")
        raw_stats = _mapping(projection.get("stats"), "projected stats")
        games = _optional_number(raw_stats.get("210"), "games played")
        if games is None or games == 0:
            if abs(total) > 1e-9 or abs(average) > 1e-9:
                raise EspnProjectionReadError(
                    "ESPN projection average was inconsistent with games played"
                )
        elif not 0 < games <= 25 or abs(total / games - average) > 1e-6 * max(1, abs(average)):
            raise EspnProjectionReadError(
                "ESPN projection average was inconsistent with games played"
            )
        name = _name(player.get("fullName"))
        link_id = player_id[1:] if player_id.startswith("-") else player_id
        player_link = f"https://www.espn.com/nfl/player/_/id/{link_id}"
        if position == "DST":
            # ESPN encodes defenses as negative player IDs.  The public-link
            # schema uses the unsigned route form, so a stable suffix keeps it
            # distinct from the rare ordinary player with the same absolute ID.
            player_link += "/team-defense"
        row = [
            _cell(name, player_link),
            _cell(_PRO_TEAM[team_id]),
            _cell(position),
            _cell(_format_number(games)),
            _cell(_format_number(total)),
            _cell(_format_number(average)),
        ]
        for _, stat_id in _RAW_STATS:
            row.append(_cell(_format_number(_optional_number(raw_stats.get(stat_id), stat_id))))
        rows.append((total, player_id, row))
    if not rows:
        raise EspnProjectionReadError(
            "ESPN returned no exact current-season projections for the requested positions"
        )
    rows.sort(key=lambda item: (-item[0], _numeric_id_sort(item[1])))
    projected_count = len(rows)
    period = (
        f"ESPN {season} full season; {_SCORING_LABEL[scoring]} format "
        f"{league_format_id}; {projected_count} of {len(wrappers)} returned players projected"
    )
    return {
        "availability": "available",
        "source": {
            "season": season,
            "week": None,
            "horizon": "ros",
            "scoring": scoring,
            "positions": list(positions),
            "period_text": period,
        },
        "tables": [{
            "rows": [
                [_cell(header) for header in _HEADERS],
                *(row for _, _, row in rows),
            ]
        }],
    }


def _task(task):
    if (
        not isinstance(task, PageCaptureTask)
        or task.provider is not CaptureProvider.ESPN
        or task.projection is None
        or task.projection.horizon is not RankingHorizon.ROS
    ):
        raise EspnProjectionReadError(
            "ESPN's season endpoint requires one rest-of-season projection task"
        )


def _season(value):
    if type(value) is not int or not 2000 <= value <= 2200:
        raise ValueError("season must be from 2000 through 2200")
    return value


def _scoring(value):
    if value not in _SCORING_FORMAT:
        raise ValueError("scoring must be STD, HALF, or PPR")
    return value


def _positions(value):
    if not isinstance(value, tuple) or not value or len(value) != len(set(value)):
        raise EspnProjectionReadError("ESPN projection position scope was invalid")
    if "ALL" in value:
        if value != ("ALL",):
            raise EspnProjectionReadError("ALL cannot be combined with ESPN positions")
    elif not set(value) <= set(_POSITION.values()):
        raise EspnProjectionReadError("ESPN projection position scope was unsupported")
    return value


def _mapping(value, label):
    if not isinstance(value, Mapping):
        raise EspnProjectionReadError(f"ESPN {label} was invalid")
    return value


def _integer(value, label):
    if type(value) is not int or abs(value) > _MAXIMUM_SAFE_JSON_INTEGER:
        raise EspnProjectionReadError(f"ESPN {label} was invalid")
    return value


def _player_id(value):
    if type(value) is int:
        if value == 0 or abs(value) > _MAXIMUM_SAFE_JSON_INTEGER:
            raise EspnProjectionReadError("ESPN player ID was invalid")
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"-?[1-9][0-9]{0,15}", value):
        raise EspnProjectionReadError("ESPN player ID was invalid")
    return value


def _name(value):
    if (
        not isinstance(value, str)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise EspnProjectionReadError("ESPN player name was invalid")
    result = " ".join(value.split())
    if (
        not 1 <= len(result) <= 160
        or "://" in result
    ):
        raise EspnProjectionReadError("ESPN player name was invalid")
    return result


def _number(value, label):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or abs(value) > 1_000_000_000
    ):
        raise EspnProjectionReadError(f"ESPN {label} was invalid")
    return float(value)


def _optional_number(value, label):
    return None if value is None else _number(value, label)


def _format_number(value):
    if value is None:
        return "-"
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _cell(text, link=None):
    return {"text": str(text), "links": [] if link is None else [link]}


def _numeric_id_sort(value):
    return int(value)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _open_without_redirects(request, *, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _bounded_body(response, maximum, deadline, cancelled):
    read_once = getattr(response, "read1", None)
    if not callable(read_once):
        _check_cancelled(cancelled)
        body = response.read(maximum + 1)
        _check_cancelled(cancelled)
        return body
    chunks, size = [], 0
    while size <= maximum:
        _check_cancelled(cancelled)
        if monotonic() > deadline:
            raise EspnProjectionReadError("ESPN season projections exceeded the time limit")
        chunk = read_once(min(64 * 1024, maximum + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _content_length(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise EspnProjectionReadError("ESPN returned an invalid Content-Length")
    return int(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _check_cancelled(cancelled):
    if cancelled():
        raise BrowserCaptureCancelled("ESPN season-projection read was cancelled")


__all__ = (
    "EspnProjectionReadError",
    "EspnSeasonProjectionClient",
    "espn_season_projection_filter",
    "espn_season_projection_segment",
    "espn_season_projection_url",
)
