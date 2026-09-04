"""Bounded HTTPS reads for public, weekly player-data snapshots."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_CHUNK_BYTES = 64 * 1024
_MAX_REDIRECTS = 3
_MAX_URL_CHARS = 4096
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_REDIRECT_HOSTS_BY_SOURCE = {
    "api.sleeper.app": frozenset({"api.sleeper.app"}),
    "github.com": frozenset(
        {
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
        }
    ),
    "raw.githubusercontent.com": frozenset({"raw.githubusercontent.com"}),
}
_SAFE_RESPONSE_HEADERS = frozenset(
    {"content-encoding", "content-length", "content-type", "etag", "last-modified"}
)


class PublicPlayerDataError(RuntimeError):
    """A public player-data source failed its bounded acquisition contract."""


class PublicPlayerDataCancelled(PublicPlayerDataError):
    """The caller cancelled public player-data acquisition."""


@dataclass(frozen=True, slots=True)
class DownloadedPublicData:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def __post_init__(self) -> None:
        if self.status not in {200, 404}:
            raise ValueError("download status must be 200 or 404")
        if not isinstance(self.body, bytes):
            raise ValueError("download body must be bytes")
        normalized = tuple(self.headers)
        if any(
            not isinstance(key, str)
            or key not in _SAFE_RESPONSE_HEADERS
            or not isinstance(value, str)
            or len(value) > 512
            for key, value in normalized
        ):
            raise ValueError("download headers contain unsupported values")
        if len({key for key, _ in normalized}) != len(normalized):
            raise ValueError("download headers contain duplicate names")
        if self.status == 404 and self.body:
            raise ValueError("an unavailable download cannot contain a body")
        object.__setattr__(self, "headers", normalized)

    @property
    def header_map(self) -> Mapping[str, str]:
        return dict(self.headers)


def bounded_https_get(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    cancelled: Callable[[], bool],
) -> DownloadedPublicData:
    """Read one allowlisted public URL without retries or unbounded buffering."""

    source_host = _https_host(url)
    allowed_redirect_hosts = _REDIRECT_HOSTS_BY_SOURCE.get(source_host)
    if allowed_redirect_hosts is None:
        raise ValueError("public player-data URL must use an allowlisted source host")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 120
    ):
        raise ValueError("timeout_seconds must be greater than zero and at most 120")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024 * 1024:
        raise ValueError("max_bytes must be between 1 and 256 MiB")
    _raise_if_cancelled(cancelled)
    deadline = monotonic() + float(timeout_seconds)
    current_url = url
    for redirect_count in range(_MAX_REDIRECTS + 1):
        _raise_if_cancelled(cancelled)
        response = _open_response(_request(current_url), deadline)
        try:
            _remaining_seconds(deadline)
            status = response.getcode()
            if status == 404:
                return DownloadedPublicData(404, (), b"")
            if status in _REDIRECT_STATUSES:
                if redirect_count == _MAX_REDIRECTS:
                    raise PublicPlayerDataError(
                        "public player-data source returned too many redirects"
                    )
                current_url = _validated_redirect_url(
                    current_url,
                    response.headers.get("Location"),
                    allowed_redirect_hosts,
                )
                continue
            if status != 200:
                raise PublicPlayerDataError(
                    f"public player-data source returned HTTP {status}"
                )
            return _download_response(response, deadline, max_bytes, cancelled)
        except PublicPlayerDataError:
            raise
        except (TimeoutError, URLError, OSError):
            raise PublicPlayerDataError(
                "public player-data response was interrupted"
            ) from None
        finally:
            response.close()
    raise AssertionError("redirect loop exceeded its fixed bound")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _open_without_redirects(request, *, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _request(url):
    return Request(
        url,
        headers={
            "Accept": "application/json,text/csv,application/gzip;q=0.9,*/*;q=0.1",
            "User-Agent": "FantasyTradeEvaluator/0.2.0 public-weekly-data",
        },
        method="GET",
    )


def _open_response(request, deadline):
    try:
        return _open_without_redirects(
            request, timeout=_remaining_seconds(deadline)
        )
    except HTTPError as error:
        if error.code == 404 or error.code in _REDIRECT_STATUSES:
            return error
        error.close()
        raise PublicPlayerDataError(
            f"public player-data source returned HTTP {error.code}"
        ) from None
    except (TimeoutError, URLError, OSError):
        raise PublicPlayerDataError(
            "public player-data source could not be reached"
        ) from None


def _download_response(response, deadline, max_bytes, cancelled):
    headers = _safe_headers(response.headers)
    length = dict(headers).get("content-length")
    if length is not None and (not length.isdigit() or int(length) > max_bytes):
        raise PublicPlayerDataError("public player-data response exceeded its size limit")
    expected_length = None if length is None else int(length)
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    body = bytearray()
    while True:
        _raise_if_cancelled(cancelled)
        remaining = _remaining_seconds(deadline)
        is_closed = getattr(response, "isclosed", None)
        if callable(is_closed) and is_closed():
            break
        _set_response_timeout(response, remaining)
        chunk = reader(min(_CHUNK_BYTES, max_bytes - len(body) + 1))
        _remaining_seconds(deadline)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > max_bytes:
            raise PublicPlayerDataError(
                "public player-data response exceeded its size limit"
            )
    if expected_length is not None and len(body) != expected_length:
        raise PublicPlayerDataError(
            "public player-data response did not match Content-Length"
        )
    return DownloadedPublicData(200, headers, bytes(body))


def _set_response_timeout(response, timeout):
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    socket = getattr(raw, "_sock", None)
    setter = getattr(socket, "settimeout", None)
    if not callable(setter):
        raise PublicPlayerDataError(
            "public player-data transport could not enforce its time limit"
        )
    try:
        setter(timeout)
    except (OSError, ValueError):
        raise PublicPlayerDataError(
            "public player-data response was interrupted"
        ) from None


def _remaining_seconds(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise PublicPlayerDataError("public player-data response timed out")
    return remaining


def _validated_redirect_url(current_url, location, allowed_hosts):
    if (
        not isinstance(location, str)
        or not location
        or len(location) > _MAX_URL_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in location)
    ):
        raise PublicPlayerDataError(
            "public player-data source returned an invalid redirect"
        )
    redirected_url = urljoin(current_url, location)
    try:
        redirected_host = _https_host(redirected_url)
    except ValueError:
        raise PublicPlayerDataError(
            "public player-data source returned an invalid redirect"
        ) from None
    if redirected_host not in allowed_hosts:
        raise PublicPlayerDataError(
            "public player-data source redirected to an unsupported host"
        )
    return redirected_url


def _safe_headers(headers) -> tuple[tuple[str, str], ...]:
    result = {}
    for key, value in headers.items():
        name = key.casefold()
        if name in _SAFE_RESPONSE_HEADERS:
            text = str(value).strip()
            if len(text) <= 512:
                result[name] = text
    return tuple(sorted(result.items()))


def _https_host(url: object) -> str:
    if (
        not isinstance(url, str)
        or not url
        or len(url) > _MAX_URL_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise ValueError("public player-data URL must be text")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            "public player-data URL must be a credential-free HTTPS URL"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("public player-data URL must be a credential-free HTTPS URL")
    return parsed.hostname.casefold()


def _raise_if_cancelled(cancelled: Callable[[], bool]) -> None:
    if not callable(cancelled):
        raise ValueError("cancelled must be callable")
    if cancelled():
        raise PublicPlayerDataCancelled("public player-data collection was cancelled")


__all__ = (
    "DownloadedPublicData",
    "PublicPlayerDataCancelled",
    "PublicPlayerDataError",
    "bounded_https_get",
)
