"""Local-only, content-addressed retention for sanitized browser captures."""

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from ._scenario_random import canonical_json
from .capture_schema import (
    CaptureArtifact,
    CaptureTask,
    FantasyProsECRArtifact,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    artifact_to_record,
)

_ARTIFACT_ID = re.compile(r"^(?:captable|capecr|capleague)_[0-9a-f]{64}$")
_LEAGUE_BINDING_ID = re.compile(r"^league_[0-9a-f]{32}$")
_CREDENTIAL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "csrf_token",
        "espn_s2",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "session_token",
        "set_cookie",
        "signature",
        "swid",
        "token",
        "x_api_key",
    }
)


def archive_public_captures(
    data_directory: str | os.PathLike[str],
    captures: Iterable[tuple[CaptureTask, CaptureArtifact]],
) -> tuple[Path, ...]:
    """Retain sanitized projection/ECR artifacts outside portable app outputs."""

    rows = _captures(captures)
    if not rows or any(
        not isinstance(artifact, (GenericTableArtifact, FantasyProsECRArtifact))
        for _, artifact in rows
    ):
        raise ValueError("public raw captures must contain projection or ECR artifacts")
    directory = Path(data_directory).resolve() / "raw-captures" / "public"
    return _archive(directory, rows)


def archive_private_league_capture(
    data_directory: str | os.PathLike[str],
    league_binding_id: str,
    capture: tuple[CaptureTask, FantasyProsLeagueArtifact],
) -> Path:
    """Retain a private league artifact in its opaque local league partition."""

    if (
        not isinstance(league_binding_id, str)
        or not _LEAGUE_BINDING_ID.fullmatch(league_binding_id)
    ):
        raise ValueError("league_binding_id must be an opaque local league binding")
    rows = _captures((capture,))
    if len(rows) != 1 or not isinstance(rows[0][1], FantasyProsLeagueArtifact):
        raise ValueError("private raw capture must be one FantasyPros league artifact")
    directory = (
        Path(data_directory).resolve()
        / "raw-captures"
        / "private-leagues"
        / league_binding_id
    )
    return _archive(directory, rows)[0]


def _archive(directory: Path, captures) -> tuple[Path, ...]:
    records = []
    for task, artifact in captures:
        record = artifact_to_record(artifact, task)
        _credential_free(record)
        artifact_id = getattr(artifact, "artifact_id", None)
        if (
            not isinstance(artifact_id, str)
            or not _ARTIFACT_ID.fullmatch(artifact_id)
            or record.get("artifact_id") != artifact_id
        ):
            raise ValueError("raw capture lacks a valid content-addressed artifact ID")
        records.append((artifact_id, (canonical_json(record) + "\n").encode("utf-8")))
    directory.mkdir(parents=True, exist_ok=True)
    return tuple(_store(directory, artifact_id, payload) for artifact_id, payload in records)


def _store(directory, artifact_id, payload):
    target = directory / f"{artifact_id}.json"
    if target.exists():
        if _read(target) != payload:
            raise ValueError("raw capture archive contains an artifact ID collision")
        return target.resolve()
    # Keep the atomic staging name short. The final content-addressed path can
    # legitimately sit close to Windows' legacy path limit once it is nested in
    # a per-league private partition; repeating the artifact ID here can push
    # only the temporary write over that limit.
    temporary = directory / f".capture-{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if _read(target) != payload:
        raise ValueError("raw capture archive failed post-write verification")
    return target.resolve()


def _captures(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("captures must be an iterable of task/artifact pairs")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("captures must be an iterable of task/artifact pairs") from None
    if any(
        not isinstance(row, tuple)
        or len(row) != 2
        or not isinstance(row[0], CaptureTask)
        for row in rows
    ):
        raise ValueError("captures must contain CaptureTask/artifact pairs")
    return rows


def _credential_free(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _CREDENTIAL_KEYS:
                raise ValueError("raw capture archive refuses credential-bearing fields")
            _credential_free(item)
        return
    if isinstance(value, list):
        for item in value:
            _credential_free(item)
        return
    if isinstance(value, str) and "://" in value:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("raw capture archive refuses credential-bearing URLs")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = key.casefold().replace("-", "_")
            if normalized in _CREDENTIAL_KEYS:
                raise ValueError("raw capture archive refuses credential-bearing URLs")


def _read(path):
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValueError(f"could not verify raw capture archive: {error}") from None


__all__ = ("archive_private_league_capture", "archive_public_captures")
