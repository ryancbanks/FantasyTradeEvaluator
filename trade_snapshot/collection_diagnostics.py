"""Sanitized local diagnostics for failed weekly collection runs."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import traceback

from .capture_schema import FantasyProsLeagueArtifact


_DIAGNOSTIC_DIRECTORY = "diagnostics"
_LATEST_LEAGUE_FILE = "latest-fantasypros-league.json"
_LATEST_FAILURE_FILE = "latest-weekly-validation-error.json"


def save_fantasypros_league_capture(
    data_directory: str | Path,
    artifact: FantasyProsLeagueArtifact,
) -> Path | None:
    """Retain the latest already-sanitized league artifact for local replay."""

    if not isinstance(artifact, FantasyProsLeagueArtifact):
        raise ValueError("artifact must be a FantasyProsLeagueArtifact")
    try:
        record = {
            "kind": "fantasypros_league_capture_diagnostic",
            "schema_version": 1,
            "artifact": artifact.to_record(),
        }
    except (TypeError, ValueError):
        return None
    return _best_effort_write(data_directory, _LATEST_LEAGUE_FILE, record)


def save_validation_failure(
    data_directory: str | Path,
    *,
    stage: str,
    error: ValueError,
    captured_at: datetime,
    league_capture_available: bool,
) -> str | None:
    """Save code-location evidence without persisting exception data values."""

    if not isinstance(stage, str) or not stage.strip():
        raise ValueError("stage must be non-empty text")
    if not isinstance(error, ValueError):
        raise ValueError("error must be a ValueError")
    if not isinstance(league_capture_available, bool):
        raise ValueError("league_capture_available must be a boolean")
    if (
        not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        raise ValueError("captured_at must be timezone-aware")
    package_root = Path(__file__).resolve().parent
    frames = []
    for frame in traceback.extract_tb(error.__traceback__):
        try:
            path = Path(frame.filename).resolve()
        except OSError:
            continue
        try:
            relative = path.relative_to(package_root)
        except ValueError:
            continue
        frames.append(
            {
                "module": relative.as_posix(),
                "function": frame.name,
                "line": frame.lineno,
            }
        )
    identity = {
        "stage": stage.strip(),
        "exception_type": type(error).__name__,
        "league_capture_available": league_capture_available,
        "frames": frames,
    }
    digest = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    diagnostic_id = digest[:12]
    record = {
        "kind": "weekly_validation_error_diagnostic",
        "schema_version": 1,
        "diagnostic_id": diagnostic_id,
        "captured_at": captured_at.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        **identity,
    }
    return (
        diagnostic_id
        if _best_effort_write(data_directory, _LATEST_FAILURE_FILE, record) is not None
        else None
    )


def _best_effort_write(
    data_directory: str | Path,
    filename: str,
    record: dict[str, object],
) -> Path | None:
    try:
        root = Path(data_directory).resolve() / _DIAGNOSTIC_DIRECTORY
        root.mkdir(parents=True, exist_ok=True)
        target = root / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target
    except (OSError, TypeError, ValueError):
        return None


__all__ = ("save_fantasypros_league_capture", "save_validation_failure")
