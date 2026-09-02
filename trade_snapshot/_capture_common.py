"""Shared validation and canonicalization for persisted browser captures."""

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
import re
from types import MappingProxyType
from urllib.parse import unquote, urlsplit, urlunsplit


MAX_SAFE_INTEGER = (1 << 53) - 1
_SECRET_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "csrftoken",
        "espns2",
        "key",
        "leaguekey",
        "oauth",
        "oauthtoken",
        "nonce",
        "password",
        "refreshtoken",
        "secret",
        "sessionid",
        "signature",
        "setcookie",
        "swid",
        "ticket",
        "token",
        "jwt",
        "xapikey",
        "xsrf",
        "xsrftoken",
    }
)
_TRANSPORT_KEYS = frozenset(
    {
        "browserstorage",
        "headers",
        "href",
        "endpoint",
        "endpointurl",
        "localstorage",
        "query",
        "queryparams",
        "request",
        "requestheaders",
        "requestmethod",
        "requestquery",
        "requesturi",
        "requesturl",
        "sourceurl",
        "sessionstorage",
        "storage",
        "url",
        "uri",
    }
)


def normalize_json(value: object, path: str = "value") -> object:
    """Return a detached portable JSON tree without coercing its semantics."""

    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            normalized[key] = normalize_json(child, f"{path}.{key}")
        return normalized
    if isinstance(value, list):
        return [normalize_json(child, f"{path}[{index}]") for index, child in enumerate(value)]
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(f"{path} integer is outside the portable JSON range")
        return value
    if type(value) is float:
        if not isfinite(value) or abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(f"{path} number is not a finite portable JSON number")
        return value
    raise ValueError(f"{path} contains a non-JSON value")


def sanitize_capture_body(value: object) -> object:
    """Deeply remove transport and secret-bearing fields from a JSON body."""

    normalized = normalize_json(value, "capture body")
    sanitized = _sanitize_normalized(normalized)
    return None if sanitized is _REDACTED else sanitized


_REDACTED = object()


def _sanitize_normalized(value: object) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, child in value.items():
            if is_forbidden_capture_key(key):
                continue
            sanitized = _sanitize_normalized(child)
            if sanitized is not _REDACTED:
                result[key] = sanitized
        return result
    if isinstance(value, list):
        result = [_sanitize_normalized(child) for child in value]
        return [child for child in result if child is not _REDACTED]
    if isinstance(value, str) and looks_like_url(value):
        return _REDACTED
    return value


def normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def is_forbidden_capture_key(value: str) -> bool:
    key = normalized_key(value)
    if key in _SECRET_KEYS | _TRANSPORT_KEYS:
        return True
    secret_fragments = (
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "csrftoken",
        "headers",
        "oauth",
        "nonce",
        "password",
        "query",
        "refreshtoken",
        "secret",
        "session",
        "signature",
        "storage",
        "ticket",
        "token",
        "xapikey",
        "xsrf",
    )
    if any(fragment in key for fragment in secret_fragments):
        return True
    if key.startswith(("auth", "bearer", "jwt")) or key.endswith(
        ("token", "secret", "apikey", "signature", "ticket", "nonce")
    ):
        return True
    return key.endswith(("url", "uri", "headers", "storage"))


def looks_like_url(value: str) -> bool:
    stripped = value.strip().casefold()
    for _ in range(3):
        decoded = unquote(stripped)
        if decoded == stripped:
            break
        stripped = decoded
    uri = re.compile(
        r"\b[a-z][a-z0-9+.-]{1,31}://|"
        r"(?:https?|ftp|file|data|blob|javascript|mailto|tel|sms|urn|about):|"
        r"(?<!:)//|\bwww\."
    )
    if uri.search(stripped) or stripped.startswith(("/", "?")):
        return True
    return bool(re.search(r"(?:^|\s)[^\s?]{0,200}\?[a-z0-9_.%+-]+=[^\s]*", stripped))


def canonical_json(value: object) -> str:
    normalized = normalize_json(value)
    return json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_id(prefix: str, value: object) -> str:
    digest = sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def schema_fingerprint(name: str, fields: object) -> str:
    return content_id("capschema", {"name": name, "version": 1, "fields": fields})


def freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(child) for child in value)
    return value


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError(f"{name} cannot contain control characters")
    return value


def require_content_id(name: str, value: object, prefix: str) -> str:
    text = require_text(name, value)
    expected_prefix = f"{prefix}_"
    digest = text.removeprefix(expected_prefix)
    if (
        not text.startswith(expected_prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{name} must be a valid {prefix} content identifier")
    return text


def require_json_int(name: str, value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def require_captured_at(value: object) -> str:
    text = require_text("captured_at", value)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
        text,
    ):
        raise ValueError("captured_at must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        raise ValueError("captured_at must be an RFC3339 UTC timestamp") from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("captured_at must be UTC")
    return text


def require_safe_https_url(value: object, *, allowed_hosts: frozenset[str] | None = None) -> str:
    text = require_text("url", value)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        raise ValueError("url is invalid") from None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("url must be canonical HTTPS without credentials, query, or fragment")
    if allowed_hosts is not None and host not in allowed_hosts:
        raise ValueError(f"url host {host!r} is not allowed for this provider")
    return urlunsplit(("https", parsed.netloc.casefold(), parsed.path or "/", "", ""))


def sanitized_visible_link(value: object) -> str:
    """Keep an HTTPS anchor destination while removing query and fragment data."""

    text = require_text("url", value)
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise ValueError("url is invalid") from None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        host in {"fantasysharks.com", "www.fantasysharks.com"}
        and parsed.path == "/apps/bert/players/playerpage.php"
        and re.fullmatch(r"id=[1-9][0-9]{0,9}", parsed.query or "")
        and not parsed.fragment
    ):
        base = require_safe_https_url(
            urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        )
        return f"{base}?{parsed.query}"
    stripped = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return require_safe_https_url(stripped)
