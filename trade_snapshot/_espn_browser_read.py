"""Authenticated ESPN JSON reads that never leave the browser worker."""

from collections.abc import Callable, Mapping
import re

from ._capture_errors import BrowserCaptureError
from .espn_free_read import EspnFreeReadClient, _open_without_redirects


_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MAXIMUM_COOKIE_COUNT = 200
_MAXIMUM_COOKIE_HEADER_BYTES = 16 * 1024


def read_authenticated_espn_json(
    context,
    season: int,
    league_id: str,
    timeout_ms: int,
    maximum_bytes: int,
    cancelled: Callable[[], bool],
    *,
    transport: Callable | None = None,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """Read the two public endpoints with only URL-scoped profile cookies.

    The request and cookie header exist only inside this call in the isolated browser
    process.  The returned value contains the two validated JSON objects only.
    """

    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise BrowserCaptureError("authenticated ESPN timeout was invalid")
    if not callable(cancelled):
        raise BrowserCaptureError("authenticated ESPN cancellation check was invalid")
    urls = frozenset(EspnFreeReadClient.urls(season, league_id))
    open_request = transport or _open_without_redirects
    if not callable(open_request):
        raise BrowserCaptureError("authenticated ESPN transport was invalid")

    def opener(request, *, timeout):
        if request.full_url not in urls or request.get_method() != "GET":
            raise BrowserCaptureError("authenticated ESPN URL was not allowlisted")
        cookie_header = _cookie_header(context, request.full_url)
        if cookie_header:
            request.add_unredirected_header("Cookie", cookie_header)
        return open_request(request, timeout=timeout)

    per_read_seconds = max(1.0, min(30.0, timeout_ms / 2000.0))
    return EspnFreeReadClient(
        timeout_seconds=per_read_seconds,
        maximum_bytes=maximum_bytes,
        opener=opener,
    )(season, league_id, cancelled)


def _cookie_header(context, url: str) -> str:
    try:
        rows = context.cookies([url])
    except Exception:
        raise BrowserCaptureError(
            "authenticated ESPN cookies could not be read"
        ) from None
    if not isinstance(rows, list) or len(rows) > _MAXIMUM_COOKIE_COUNT:
        raise BrowserCaptureError("authenticated ESPN cookies were invalid")
    pairs = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise BrowserCaptureError("authenticated ESPN cookies were invalid")
        name, value = row.get("name"), row.get("value")
        if (
            not isinstance(name, str)
            or _COOKIE_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or not value.isascii()
            or any(character in value for character in ("\r", "\n", "\0", ";"))
        ):
            raise BrowserCaptureError("authenticated ESPN cookies were invalid")
        pairs.append(f"{name}={value}")
    header = "; ".join(pairs)
    if len(header.encode("ascii")) > _MAXIMUM_COOKIE_HEADER_BYTES:
        raise BrowserCaptureError("authenticated ESPN cookies exceeded the size limit")
    return header


__all__ = ("read_authenticated_espn_json",)
