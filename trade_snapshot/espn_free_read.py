"""Bounded, cookie-free reads of ESPN's public fantasy-football JSON."""

from collections.abc import Callable, Mapping
import json
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


_READ_ROOT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


class EspnFreeReadError(RuntimeError):
    """Safe failure from the bounded public ESPN read boundary."""


class EspnUnauthorizedError(EspnFreeReadError):
    """The public ESPN read requires the user's existing browser session."""


class EspnFreeReadClient:
    """Perform exactly two bounded, unauthenticated ESPN JSON reads per refresh."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        maximum_bytes: int = 32 * 1024 * 1024,
        opener: Callable | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValueError("timeout_seconds must be from 1 through 30")
        if type(maximum_bytes) is not int or not 1024 <= maximum_bytes <= 64 * 1024 * 1024:
            raise ValueError("maximum_bytes must be from 1024 through 67108864")
        if opener is not None and not callable(opener):
            raise ValueError("opener must be callable")
        self._timeout = float(timeout_seconds)
        self._maximum = maximum_bytes
        self._opener = opener or _open_without_redirects

    def __call__(
        self, season: int, league_id: str, cancelled: Callable[[], bool]
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        league_url, pro_team_url = self.urls(season, league_id)
        _check_cancelled(cancelled)
        league = self._read(league_url, cancelled)
        _check_cancelled(cancelled)
        pro_teams = self._read(pro_team_url, cancelled)
        _check_cancelled(cancelled)
        return league, pro_teams

    @classmethod
    def urls(cls, season: int, league_id: str) -> tuple[str, str]:
        """Return the only two allowlisted JSON URLs used by a refresh."""

        _season(season)
        _provider_id(league_id)
        return cls._league_url(season, league_id), cls._pro_team_url(season)

    @staticmethod
    def _league_url(season: int, league_id: str) -> str:
        views = ("mTeam", "mRoster", "mSettings", "mMatchup", "mStandings")
        query = urlencode(tuple(("view", view) for view in views))
        return f"{_READ_ROOT}/seasons/{season}/segments/0/leagues/{league_id}?{query}"

    @staticmethod
    def _pro_team_url(season: int) -> str:
        return f"{_READ_ROOT}/seasons/{season}?view=proTeamSchedules_wl"

    def _read(self, expected_url: str, cancelled) -> Mapping[str, object]:
        request = Request(
            expected_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "fantasy-trade-evaluator/0.1",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                if response.geturl() != expected_url:
                    raise EspnFreeReadError("ESPN returned an unexpected read response")
                if status in {401, 403}:
                    raise EspnUnauthorizedError(
                        "ESPN league read access was not authorized"
                    )
                if status != 200:
                    raise EspnFreeReadError("ESPN returned an unexpected read response")
                headers = response.headers
                media_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if media_type != "application/json" or headers.get("Content-Encoding"):
                    raise EspnFreeReadError("ESPN returned an unsupported response format")
                declared = headers.get("Content-Length")
                if declared is not None and _content_length(declared) > self._maximum:
                    raise EspnFreeReadError("ESPN response exceeded the size limit")
                body = _bounded_body(
                    response, self._maximum, self._timeout, cancelled
                )
        except EspnFreeReadError:
            raise
        except HTTPError as error:
            if error.code in {401, 403} and error.geturl() == expected_url:
                raise EspnUnauthorizedError(
                    "ESPN league read access was not authorized"
                ) from None
            raise EspnFreeReadError("ESPN league data could not be read") from None
        except Exception:
            raise EspnFreeReadError("ESPN league data could not be read") from None
        if not body or len(body) > self._maximum:
            raise EspnFreeReadError("ESPN response had an invalid size")
        try:
            value = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise EspnFreeReadError("ESPN returned invalid JSON data") from None
        if not isinstance(value, Mapping):
            raise EspnFreeReadError("ESPN response must be a JSON object")
        return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _open_without_redirects(request, *, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _bounded_body(response, maximum, timeout, cancelled):
    read_once = getattr(response, "read1", None)
    if not callable(read_once):
        return response.read(maximum + 1)
    deadline = monotonic() + timeout
    chunks, size = [], 0
    while size <= maximum:
        _check_cancelled(cancelled)
        if monotonic() >= deadline:
            raise EspnFreeReadError("ESPN response exceeded the time limit")
        chunk = read_once(min(64 * 1024, maximum + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _season(value):
    if type(value) is not int or not 2012 <= value <= 9999:
        raise ValueError("season must be an integer from 2012 through 9999")


def _provider_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or value.startswith("0") or len(value) > 20:
        raise ValueError("league_id must be a positive decimal provider ID")


def _content_length(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise EspnFreeReadError("ESPN returned an invalid content length") from None
    if parsed < 0:
        raise EspnFreeReadError("ESPN returned an invalid content length")
    return parsed


def _check_cancelled(check):
    if not callable(check):
        raise ValueError("cancelled must be callable")
    value = check()
    if not isinstance(value, bool):
        raise ValueError("cancelled must return a boolean")
    if value:
        raise EspnFreeReadError("Weekly collection was cancelled")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant {value}")


__all__ = ("EspnFreeReadClient", "EspnFreeReadError", "EspnUnauthorizedError")
