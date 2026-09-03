"""In-memory protocol bridge between the local app and a browser extension.

The bridge deliberately contains no HTTP code.  ``create_pairing`` is intended to
be called only behind the local application's existing app-token check; transport
adapters can then expose ``connect``, ``poll``, ``complete``, ``status``, and
``disconnect`` to the extension.  Pairing and session secrets live only for the
lifetime of this object and are never written to disk.

There is exactly one command slot.  A paired extension repeatedly polls that slot,
claims each command once, completes it, and reuses the same browser-side worker for
the next command.  This avoids both an unbounded queue and a new browser worker for
every capture phase.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
import json
import math
import re
from secrets import token_hex, token_urlsafe
from threading import Condition, RLock
from time import monotonic
from typing import TypeAlias, cast


PROTOCOL_VERSION = 1
MINIMUM_EXTENSION_VERSION = "0.2.0"
SESSION_TOKEN_HEADER = "X-FTE-Extension-Token"
PAIR_REQUEST_MAX_BYTES = 4 * 1024
POLL_WAIT_MAX_SECONDS = 20.0
COMMAND_PAYLOAD_MAX_BYTES = 256 * 1024
COMMAND_RESULT_MAX_BYTES = 64 * 1024 * 1024

# Capabilities are the complete operation vocabulary for protocol v1.  Requiring
# this exact set prevents a partially compatible extension from starting a scan.
V1_CAPABILITIES = (
    "session.open",
    "session.navigate",
    "analyzer.begin",
    "analyzer.finish",
    "analyzer.abort",
    "analyzer.bundle",
    "analyzer.activate_full",
    "page.provenance",
    "projection.capture",
    "ecr.capture",
    "league.capture",
    "espn.authenticated_json",
    "yahoo.scoring",
    "session.wait",
    "session.close",
)
V1_OPERATIONS = frozenset(V1_CAPABILITIES)

_EXTENSION_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}")
_SECRET = re.compile(r"[0-9A-Za-z_-]{32,128}")
_MISSING = object()
_MAX_JSON_DEPTH = 32

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ExtensionBridgeError(RuntimeError):
    """Base class for sanitized extension bridge failures."""


class BridgeAuthenticationError(ExtensionBridgeError):
    """A pairing code or extension session token was not accepted."""


class BridgeProtocolError(ExtensionBridgeError):
    """The peer requested an unsupported protocol, capability, or operation."""


class BridgeStateError(ExtensionBridgeError):
    """The requested transition is not valid in the current pairing state."""


class BridgeBusyError(ExtensionBridgeError):
    """The bridge's single command slot is occupied."""


class BridgePayloadError(ExtensionBridgeError):
    """A command or result did not satisfy the bounded JSON contract."""


class BridgeStaleCommandError(ExtensionBridgeError):
    """A completion does not belong to the currently claimed command."""


class BridgeTimeoutError(ExtensionBridgeError):
    """The extension did not finish a command before its deadline."""


class BridgeCancelledError(ExtensionBridgeError):
    """The app cancelled a pending extension command."""


class BridgeDisconnectedError(ExtensionBridgeError):
    """The paired extension disconnected during a command."""


class BridgeClosedError(ExtensionBridgeError):
    """The owning local application closed the bridge."""


class BridgeCommandError(ExtensionBridgeError):
    """The extension reported a sanitized command failure."""


@dataclass(slots=True)
class _Command:
    command_id: str
    op: str
    payload: dict[str, JsonValue]
    deadline: float
    cancelled: Callable[[], bool] | None
    state: str = "queued"
    result: JsonValue | object = _MISSING
    error: str | None = None
    terminal_error: ExtensionBridgeError | None = None


@dataclass(frozen=True, slots=True)
class _CompletionReceipt:
    command_id: str
    kind: str
    digest: bytes


class ExtensionCommandBridge:
    """A capacity-one, token-protected command exchange for protocol v1."""

    def __init__(
        self,
        *,
        pair_ttl_seconds: float = 120.0,
        maximum_poll_wait_seconds: float = POLL_WAIT_MAX_SECONDS,
        maximum_command_timeout_seconds: float = 15 * 60.0,
        maximum_command_bytes: int = COMMAND_PAYLOAD_MAX_BYTES,
        maximum_result_bytes: int = COMMAND_RESULT_MAX_BYTES,
        clock: Callable[[], float] = monotonic,
        secret_factory: Callable[[], str] = lambda: token_urlsafe(24),
    ) -> None:
        self._pair_ttl = _positive_number(
            "pair_ttl_seconds", pair_ttl_seconds, maximum=5 * 60.0
        )
        self._maximum_poll_wait = _positive_number(
            "maximum_poll_wait_seconds",
            maximum_poll_wait_seconds,
            maximum=POLL_WAIT_MAX_SECONDS,
        )
        self._maximum_command_timeout = _positive_number(
            "maximum_command_timeout_seconds",
            maximum_command_timeout_seconds,
            maximum=60 * 60.0,
        )
        self._maximum_command_bytes = _positive_integer(
            "maximum_command_bytes", maximum_command_bytes
        )
        self._maximum_result_bytes = _positive_integer(
            "maximum_result_bytes", maximum_result_bytes
        )
        if not callable(clock):
            raise ValueError("clock must be callable")
        if not callable(secret_factory):
            raise ValueError("secret_factory must be callable")
        self._clock = clock
        self._secret_factory = secret_factory
        self._condition = Condition(RLock())
        self._pair_code: str | None = None
        self._pair_deadline: float | None = None
        self._session_token: str | None = None
        self._extension_version: str | None = None
        self._command: _Command | None = None
        self._last_completion: _CompletionReceipt | None = None
        self._closed = False

    def __enter__(self) -> "ExtensionCommandBridge":
        with self._condition:
            self._require_open()
        return self

    def __exit__(self, _kind, _error, _traceback) -> bool:
        self.close()
        return False

    @property
    def state(self) -> str:
        """Return ``unpaired``, ``pairing``, ``paired``, or ``closed``."""

        with self._condition:
            if self._closed:
                return "closed"
            self._expire_pairing()
            if self._session_token is not None:
                return "paired"
            if self._pair_code is not None:
                return "pairing"
            return "unpaired"

    def create_pairing(self) -> dict[str, JsonValue]:
        """Create/rotate the app-authorized, one-use pairing offer.

        A transport must protect this call with the local application's existing
        app token.  Starting a new offer revokes a lost or stale extension session
        and cancels its command.  The pair code is never accepted as a session token.
        """

        with self._condition:
            self._require_open()
            if self._session_token is not None:
                self._drop_session(
                    BridgeDisconnectedError(
                        "extension session was replaced by a new pairing"
                    )
                )
            self._pair_code = self._new_secret()
            self._pair_deadline = self._clock() + self._pair_ttl
            self._condition.notify_all()
            return {
                "protocol_version": PROTOCOL_VERSION,
                "state": "pairing",
                "pair_code": self._pair_code,
                "expires_in_seconds": self._pair_ttl,
                "capabilities": list(V1_CAPABILITIES),
            }

    def public_status(self) -> dict[str, JsonValue]:
        """Return a non-secret summary for an app-token-protected UI route.

        This method intentionally does not authenticate an extension session.  Its
        transport route must require the local app token instead.
        """

        with self._condition:
            self._expire_pairing()
            if self._closed:
                state = "closed"
            elif self._session_token is not None:
                state = "paired"
            elif self._pair_code is not None:
                state = "pairing"
            else:
                state = "unpaired"
            record: dict[str, JsonValue] = {
                "protocol_version": PROTOCOL_VERSION,
                "state": state,
                "capabilities": list(V1_CAPABILITIES),
                "command_busy": self._command is not None,
            }
            if self._session_token is not None:
                record["extension_version"] = self._extension_version
            return record

    def connect(
        self,
        pair_code: str,
        protocol_version: int,
        capabilities: Sequence[str],
        extension_version: str,
    ) -> dict[str, JsonValue]:
        """Consume a current pair code and mint a separate in-memory session token."""

        _require_protocol(protocol_version, capabilities, extension_version)
        with self._condition:
            self._require_open()
            self._expire_pairing()
            if self._session_token is not None:
                raise BridgeStateError("an extension is already paired")
            if self._pair_code is None:
                raise BridgeAuthenticationError("no active extension pairing")
            if not _secret_matches(pair_code, self._pair_code):
                raise BridgeAuthenticationError("extension pairing code was not accepted")
            used_pair_code = self._pair_code
            self._pair_code = None
            self._pair_deadline = None
            self._session_token = self._new_secret(disallow=used_pair_code)
            self._extension_version = extension_version
            self._last_completion = None
            self._condition.notify_all()
            return {
                "protocol_version": PROTOCOL_VERSION,
                "state": "paired",
                "session_token": self._session_token,
                "capabilities": list(V1_CAPABILITIES),
                "poll_wait_max_seconds": self._maximum_poll_wait,
            }

    def poll(
        self, session_token: str, wait_seconds: float = POLL_WAIT_MAX_SECONDS
    ) -> dict[str, JsonValue]:
        """Wait briefly for the next command and claim it at most once."""

        wait = _nonnegative_number(
            "wait_seconds", wait_seconds, maximum=self._maximum_poll_wait
        )
        with self._condition:
            self._require_session(session_token)
            deadline = self._clock() + wait
            while True:
                command = self._command
                if command is not None and command.state == "queued":
                    command.state = "claimed"
                    return {
                        "protocol_version": PROTOCOL_VERSION,
                        "state": "command",
                        "command_id": command.command_id,
                        "op": command.op,
                        "payload": _copy_json(command.payload),
                    }
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return {"protocol_version": PROTOCOL_VERSION, "state": "idle"}
                self._condition.wait(remaining)
                self._require_session(session_token)

    def complete(
        self,
        session_token: str,
        command_id: str,
        *,
        result: object = _MISSING,
        error: object = _MISSING,
    ) -> dict[str, JsonValue]:
        """Complete a claimed command, replaying an identical lost acknowledgement."""

        if (result is _MISSING) == (error is _MISSING):
            raise BridgePayloadError("completion must contain exactly one result or error")
        with self._condition:
            self._require_session(session_token)
            command = self._command
            if not isinstance(command_id, str):
                raise BridgeStaleCommandError("command completion is stale")
            if (
                command is None
                or not compare_digest(command.command_id, command_id)
                or command.state != "claimed"
            ):
                return self._replay_completion(command_id, result=result, error=error)
            self._reject_late_completion(command)
            kind, normalized, digest = self._normalize_completion(
                result=result, error=error
            )
            self._reject_late_completion(command)
            if kind == "error":
                assert isinstance(normalized, str)
                command.error = normalized
                command.state = "failed"
            else:
                command.result = normalized
                command.state = "complete"
            self._last_completion = _CompletionReceipt(
                command_id=command.command_id,
                kind=kind,
                digest=digest,
            )
            self._condition.notify_all()
            return _accepted_completion(command.command_id)

    def status(self, session_token: str) -> dict[str, JsonValue]:
        """Authenticate a heartbeat and report the current command state."""

        with self._condition:
            self._require_session(session_token)
            command = self._command
            command_record: JsonValue = None
            if command is not None:
                command_record = {
                    "command_id": command.command_id,
                    "op": command.op,
                    "state": command.state,
                }
            return {
                "protocol_version": PROTOCOL_VERSION,
                "state": "paired",
                "extension_version": self._extension_version,
                "capabilities": list(V1_CAPABILITIES),
                "command": command_record,
            }

    def disconnect(self, session_token: str) -> dict[str, JsonValue]:
        """Revoke the extension session and wake any waiting producer."""

        with self._condition:
            self._require_session(session_token)
            self._drop_session(
                BridgeDisconnectedError("extension disconnected during command")
            )
            return {"protocol_version": PROTOCOL_VERSION, "state": "unpaired"}

    def execute(
        self,
        op: str,
        payload: Mapping[str, object],
        timeout: float,
        cancelled: Callable[[], bool] | None,
    ) -> JsonValue:
        """Submit one v1 operation and wait for its extension result.

        ``timeout`` is in seconds and is capped at the bridge's configured maximum.
        Cancellation is checked at least every 50 ms while a command is pending.
        """

        if not isinstance(op, str) or op not in V1_OPERATIONS:
            raise BridgeProtocolError("operation is not allowed by extension protocol v1")
        if not isinstance(payload, Mapping):
            raise BridgePayloadError("command payload must be a JSON object")
        normalized = _bounded_json(
            payload,
            self._maximum_command_bytes,
            "command payload",
            require_mapping=True,
        )
        assert isinstance(normalized, dict)
        command_timeout = _positive_number(
            "timeout", timeout, maximum=self._maximum_command_timeout
        )
        if cancelled is not None and not callable(cancelled):
            raise ValueError("cancelled must be callable or None")
        if cancelled is not None and cancelled():
            raise BridgeCancelledError("extension command was cancelled")

        with self._condition:
            self._require_open()
            if self._session_token is None:
                raise BridgeStateError("no extension is paired")
            if self._command is not None:
                raise BridgeBusyError("another extension command is already in flight")
            deadline = self._clock() + command_timeout
            command = _Command(token_hex(16), op, normalized, deadline, cancelled)
            self._command = command
            self._condition.notify_all()
            try:
                while True:
                    if command.terminal_error is not None:
                        raise command.terminal_error
                    if command.state == "complete":
                        return cast(JsonValue, command.result)
                    if command.state == "failed":
                        raise BridgeCommandError(
                            command.error or "extension command failed"
                        )
                    if cancelled is not None and cancelled():
                        raise BridgeCancelledError("extension command was cancelled")
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise BridgeTimeoutError("extension command timed out")
                    wait = remaining if cancelled is None else min(remaining, 0.05)
                    self._condition.wait(wait)
            finally:
                self._release(command)

    def close(self) -> None:
        """Permanently revoke secrets and fail any pending command."""

        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._drop_session(BridgeClosedError("extension bridge is closed"))

    def _require_open(self) -> None:
        if self._closed:
            raise BridgeClosedError("extension bridge is closed")

    def _require_session(self, supplied: str) -> None:
        self._require_open()
        if self._session_token is None or not _secret_matches(
            supplied, self._session_token
        ):
            raise BridgeAuthenticationError("extension session token was not accepted")

    def _expire_pairing(self) -> None:
        if (
            self._pair_code is not None
            and self._pair_deadline is not None
            and self._clock() >= self._pair_deadline
        ):
            self._pair_code = None
            self._pair_deadline = None

    def _new_secret(self, *, disallow: str | None = None) -> str:
        for _ in range(8):
            value = self._secret_factory()
            if not isinstance(value, str) or not _SECRET.fullmatch(value):
                raise ValueError("secret_factory returned an invalid URL-safe secret")
            if disallow is None or not compare_digest(value, disallow):
                return value
        raise RuntimeError("secret_factory did not return a distinct session secret")

    def _release(self, command: _Command) -> None:
        if self._command is command:
            self._command = None
            self._condition.notify_all()

    def _reject_late_completion(self, command: _Command) -> None:
        if command.cancelled is not None and command.cancelled():
            command.terminal_error = BridgeCancelledError(
                "extension command was cancelled"
            )
        elif self._clock() >= command.deadline:
            command.terminal_error = BridgeTimeoutError(
                "extension command timed out"
            )
        else:
            return
        self._release(command)
        raise BridgeStaleCommandError("command completion is stale")

    def _normalize_completion(
        self,
        *,
        result: object,
        error: object,
    ) -> tuple[str, JsonValue, bytes]:
        if error is not _MISSING:
            kind = "error"
            normalized: JsonValue = _bounded_error(error)
        else:
            kind = "result"
            normalized = _bounded_json(
                result,
                self._maximum_result_bytes,
                "command result",
            )
        return kind, normalized, _completion_digest(normalized)

    def _replay_completion(
        self,
        command_id: str,
        *,
        result: object,
        error: object,
    ) -> dict[str, JsonValue]:
        receipt = self._last_completion
        if receipt is None or not compare_digest(receipt.command_id, command_id):
            raise BridgeStaleCommandError("command completion is stale")
        kind, _normalized, digest = self._normalize_completion(
            result=result, error=error
        )
        if kind != receipt.kind or not compare_digest(digest, receipt.digest):
            raise BridgeStaleCommandError("command completion is stale")
        return _accepted_completion(receipt.command_id)

    def _drop_session(self, error: ExtensionBridgeError) -> None:
        command = self._command
        if command is not None and command.state in {"queued", "claimed"}:
            command.terminal_error = error
        self._command = None
        self._last_completion = None
        self._session_token = None
        self._extension_version = None
        self._pair_code = None
        self._pair_deadline = None
        self._condition.notify_all()


def is_valid_loopback_host(host: str, expected_port: int) -> bool:
    """Match the existing local app's loopback ``Host`` policy exactly."""

    if (
        not isinstance(host, str)
        or type(expected_port) is not int
        or not 0 <= expected_port <= 65535
        or host.count(":") > 1
    ):
        return False
    hostname, separator, port = host.partition(":")
    return hostname in {"127.0.0.1", "localhost"} and (
        not separator or port == str(expected_port)
    )


def extension_version_is_supported(value: object) -> bool:
    """Return whether a Chrome-style version contains current capture evidence."""

    if not isinstance(value, str):
        return False
    parts = value.split(".")
    if not 1 <= len(parts) <= 4 or any(
        not part.isascii()
        or not part.isdigit()
        or (len(part) > 1 and part.startswith("0"))
        or int(part) > 65_535
        for part in parts
    ):
        return False
    version = tuple(int(part) for part in parts) + (0,) * (4 - len(parts))
    minimum_parts = tuple(int(part) for part in MINIMUM_EXTENSION_VERSION.split("."))
    minimum = minimum_parts + (0,) * (4 - len(minimum_parts))
    return version >= minimum


def _require_protocol(
    protocol_version: object,
    capabilities: object,
    extension_version: object,
) -> None:
    if type(protocol_version) is not int or protocol_version != PROTOCOL_VERSION:
        raise BridgeProtocolError(
            f"unsupported extension protocol; expected {PROTOCOL_VERSION}"
        )
    if (
        not isinstance(capabilities, (list, tuple))
        or any(not isinstance(value, str) for value in capabilities)
        or len(capabilities) != len(V1_CAPABILITIES)
        or frozenset(capabilities) != V1_OPERATIONS
    ):
        raise BridgeProtocolError("extension capabilities do not match protocol v1")
    if (
        not isinstance(extension_version, str)
        or not _EXTENSION_VERSION.fullmatch(extension_version)
    ):
        raise BridgeProtocolError("extension_version is invalid")


def _secret_matches(supplied: object, expected: str) -> bool:
    return isinstance(supplied, str) and compare_digest(supplied, expected)


def _positive_integer(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_number(name: str, value: object, *, maximum: float) -> float:
    number = _number(name, value)
    if number <= 0 or number > maximum:
        raise ValueError(f"{name} must be greater than zero and at most {maximum:g}")
    return number


def _nonnegative_number(name: str, value: object, *, maximum: float) -> float:
    try:
        number = _number(name, value)
    except ValueError as error:
        raise BridgePayloadError(str(error)) from None
    if number < 0 or number > maximum:
        raise BridgePayloadError(f"{name} must be from zero through {maximum:g}")
    return number


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _bounded_error(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgePayloadError("command error must be a non-empty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise BridgePayloadError("command error must be valid UTF-8") from None
    if size > 2048:
        raise BridgePayloadError("command error is too large")
    return value


def _bounded_json(
    value: object,
    maximum_bytes: int,
    label: str,
    *,
    require_mapping: bool = False,
) -> JsonValue:
    try:
        normalized = _normalize_json(value, depth=0)
        if require_mapping and not isinstance(normalized, dict):
            raise BridgePayloadError(f"{label} must be a JSON object")
        encoded = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except BridgePayloadError:
        raise
    except (OverflowError, TypeError, UnicodeError, ValueError):
        raise BridgePayloadError(f"{label} must contain valid JSON values") from None
    if len(encoded) > maximum_bytes:
        raise BridgePayloadError(f"{label} is too large")
    return normalized


def _normalize_json(value: object, *, depth: int) -> JsonValue:
    if depth > _MAX_JSON_DEPTH:
        raise BridgePayloadError("JSON value is nested too deeply")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BridgePayloadError("JSON number must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BridgePayloadError("JSON object keys must be strings")
            result[key] = _normalize_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, depth=depth + 1) for item in value]
    raise BridgePayloadError("value is not JSON compatible")


def _copy_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


def _completion_digest(value: JsonValue) -> bytes:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).digest()


def _accepted_completion(command_id: str) -> dict[str, JsonValue]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "state": "accepted",
        "command_id": command_id,
    }


__all__ = [
    "COMMAND_PAYLOAD_MAX_BYTES",
    "COMMAND_RESULT_MAX_BYTES",
    "MINIMUM_EXTENSION_VERSION",
    "PAIR_REQUEST_MAX_BYTES",
    "POLL_WAIT_MAX_SECONDS",
    "PROTOCOL_VERSION",
    "SESSION_TOKEN_HEADER",
    "V1_CAPABILITIES",
    "V1_OPERATIONS",
    "BridgeAuthenticationError",
    "BridgeBusyError",
    "BridgeCancelledError",
    "BridgeClosedError",
    "BridgeCommandError",
    "BridgeDisconnectedError",
    "BridgePayloadError",
    "BridgeProtocolError",
    "BridgeStaleCommandError",
    "BridgeStateError",
    "BridgeTimeoutError",
    "ExtensionBridgeError",
    "ExtensionCommandBridge",
    "extension_version_is_supported",
    "is_valid_loopback_host",
]
