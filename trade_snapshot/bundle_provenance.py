"""Discover and fingerprint the current public FantasyPros analyzer client."""

from hashlib import sha256
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ._analyzer_types import BundleFingerprint
from ._capture_policy import ANALYZER_BODY_POLICY_DESCRIPTOR


_BUNDLE_PATH = re.compile(
    r"^/assets/js/min/pages/myplaybook/trade-analyzer/bundle-[a-f0-9]+\.js$"
)
_PAGE_LIMIT = 8 * 1024 * 1024
_BUNDLE_LIMIT = 32 * 1024 * 1024


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "script":
            return
        values = dict(attrs)
        source = values.get("src")
        if isinstance(source, str):
            self.sources.append(source)


def discover_analyzer_bundle_url(page_html: str) -> str:
    """Select exactly one current analyzer page bundle from untrusted HTML."""

    if not isinstance(page_html, str) or not page_html:
        raise ValueError("page_html must be non-empty text")
    if len(page_html.encode("utf-8")) > _PAGE_LIMIT:
        raise ValueError("analyzer page HTML exceeds the size limit")
    parser = _Scripts()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        raise ValueError("analyzer page HTML could not be parsed") from None
    matches = set()
    for source in parser.sources:
        candidate = f"https:{source}" if source.startswith("//") else source
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and parsed.hostname == "cdn.fantasypros.com"
            and parsed.username is None
            and parsed.password is None
            and port is None
            and _BUNDLE_PATH.fullmatch(parsed.path)
            and not parsed.query
            and not parsed.fragment
        ):
            matches.add(candidate)
    if len(matches) != 1:
        raise ValueError("analyzer page must expose exactly one public analyzer bundle")
    return next(iter(matches))


def fingerprint_analyzer_bundle(url: str, content: bytes) -> BundleFingerprint:
    """Hash the exact public bundle bytes after validating their origin and bounds."""

    if not isinstance(content, bytes) or not 1_024 <= len(content) <= _BUNDLE_LIMIT:
        raise ValueError("analyzer bundle content has an invalid size")
    fingerprint = BundleFingerprint(url, sha256(content).hexdigest())
    if not _BUNDLE_PATH.fullmatch(urlsplit(fingerprint.url).path):
        raise ValueError("bundle URL is not the Trade Analyzer page bundle")
    return fingerprint


def validate_analyzer_bundle_url(url: str) -> str:
    """Return one canonical public Trade Analyzer bundle URL or fail closed."""

    try:
        validated = BundleFingerprint(url, "0" * 64).url
    except ValueError:
        raise ValueError("bundle URL is not the Trade Analyzer page bundle") from None
    if not _BUNDLE_PATH.fullmatch(urlsplit(validated).path):
        raise ValueError("bundle URL is not the Trade Analyzer page bundle")
    return validated


def analyzer_response_schema_sha256() -> str:
    """Fingerprint the exact response paths admitted into calibration evidence."""

    encoded = json.dumps(
        ANALYZER_BODY_POLICY_DESCRIPTOR,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return sha256(encoded).hexdigest()


def fetch_analyzer_bundle_fingerprint_from_page_html(
    page_html: str,
    *,
    timeout_seconds: float = 30.0,
) -> BundleFingerprint:
    """Hash the bundle named by an authenticated page without retaining its HTML."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 120
    ):
        raise ValueError("timeout_seconds must be greater than zero and at most 120")
    bundle_url = discover_analyzer_bundle_url(page_html)
    return fetch_analyzer_bundle_fingerprint(
        bundle_url, timeout_seconds=timeout_seconds
    )


def fetch_analyzer_bundle_fingerprint(
    bundle_url: str,
    *,
    timeout_seconds: float = 30.0,
) -> BundleFingerprint:
    """Hash one already-discovered public analyzer bundle URL."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 120
    ):
        raise ValueError("timeout_seconds must be greater than zero and at most 120")
    validated_url = validate_analyzer_bundle_url(bundle_url)
    bundle = _read_public(
        validated_url,
        maximum=_BUNDLE_LIMIT,
        timeout=float(timeout_seconds),
        expected_url=validated_url,
    )
    return fingerprint_analyzer_bundle(validated_url, bundle)


def _read_public(url: str, *, maximum: int, timeout: float, expected_url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/javascript;q=0.9,*/*;q=0.1",
            "User-Agent": "FantasyTradeEvaluator/0.2.1 public-methodology-fingerprint",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.geturl() != expected_url:
                raise ValueError("public analyzer resource redirected unexpectedly")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum:
                raise ValueError("public analyzer resource exceeds the size limit")
            content = response.read(maximum + 1)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"could not read public analyzer resource: {error}") from None
    if not content or len(content) > maximum:
        raise ValueError("public analyzer resource has an invalid size")
    return content


__all__ = (
    "analyzer_response_schema_sha256",
    "discover_analyzer_bundle_url",
    "fetch_analyzer_bundle_fingerprint",
    "fetch_analyzer_bundle_fingerprint_from_page_html",
    "fingerprint_analyzer_bundle",
    "validate_analyzer_bundle_url",
)
