"""Atomic local persistence for complete sanitized projection archives."""

from collections.abc import Iterable, Mapping
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from ._capture_common import content_id, require_captured_at, require_content_id
from ._capture_plan import CaptureProvider
from ._projection_archive_schema import (
    PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT,
    ProjectionArchive,
)
from .capture_schema import GenericTableArtifact
from .identity import IdentityRegistry


_ARCHIVE_DIRECTORY = re.compile(r"^projection_archive_[0-9a-f]{64}$")
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_SUMMARY_BYTES = 64 * 1024
_SUMMARY_FIELDS = {
    "kind", "schema_version", "schema_fingerprint", "archive_id", "providers",
    "seasons", "periods", "horizons", "scoring", "positions", "source_count",
    "segments_captured", "table_count", "row_count", "projected_points_count",
    "stat_names", "captured_at_first", "captured_at_last", "summary_id",
}
_SUMMARY_HEADER_FIELDS = {"kind", "schema_version", "schema_fingerprint", "summary_id"}


def save_projection_archive(
    archive_root: str | os.PathLike[str],
    artifacts: Iterable[GenericTableArtifact],
    *,
    known_registry: IdentityRegistry | None = None,
) -> Path:
    """Publish one immutable content-addressed archive directory."""

    archive = ProjectionArchive.from_artifacts(
        artifacts, known_registry=known_registry
    )
    root = Path(archive_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / archive.archive_id
    if target.exists():
        existing = load_projection_archive(target)
        if existing != archive:
            raise ValueError("existing projection archive does not match its content address")
        return target.resolve()
    record_bytes = _json_bytes(archive.to_record())
    if len(record_bytes) > _MAX_ARCHIVE_BYTES:
        raise ValueError("projection archive exceeds its saved-size limit")
    summary_bytes = _json_bytes(_summary_record(archive))
    if len(summary_bytes) > _MAX_SUMMARY_BYTES:
        raise ValueError("projection archive summary exceeds its saved-size limit")
    stage = Path(tempfile.mkdtemp(prefix=f".{archive.archive_id}.", dir=root))
    try:
        _write_synced(stage / "archive.json", record_bytes)
        _write_synced(stage / "summary.json", summary_bytes)
        os.replace(stage, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return target.resolve()


def load_projection_archive(path: str | os.PathLike[str]) -> ProjectionArchive:
    directory = Path(path)
    if not _ARCHIVE_DIRECTORY.fullmatch(directory.name):
        raise ValueError("projection archive directory name is invalid")
    record = _read_json(directory / "archive.json", _MAX_ARCHIVE_BYTES)
    archive = ProjectionArchive.from_record(record)
    if archive.archive_id != directory.name:
        raise ValueError("projection archive directory does not match archive_id")
    return archive


def projection_archive_catalog(
    archive_root: str | os.PathLike[str],
) -> tuple[dict[str, object], ...]:
    """Read only small deterministic sidecars; corrupt entries remain visible."""

    root = Path(archive_root)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ValueError("projection archive root must be a directory")
    rows = []
    for directory in sorted(
        (path for path in root.iterdir() if _ARCHIVE_DIRECTORY.fullmatch(path.name)),
        key=lambda path: path.name,
    ):
        try:
            if not directory.is_dir() or not (directory / "archive.json").is_file():
                raise ValueError("projection archive data file is missing")
            summary = _validated_summary(
                directory, _read_json(directory / "summary.json", _MAX_SUMMARY_BYTES)
            )
        except (OSError, ValueError) as error:
            rows.append({"status": "invalid", "archive_id": directory.name, "error": str(error)})
        else:
            rows.append({"status": "ready", **summary})
    return tuple(rows)


def _summary_record(archive: ProjectionArchive) -> dict[str, object]:
    summary = archive.summary()
    return {
        "kind": "full_projection_archive_summary",
        "schema_version": 1,
        "schema_fingerprint": PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT,
        **summary,
        "summary_id": content_id("projection_archive_summary", summary),
    }


def _validated_summary(directory: Path, record: Mapping[str, object]) -> dict[str, object]:
    if set(record) != _SUMMARY_FIELDS:
        raise ValueError("projection archive summary fields are invalid")
    if (
        record["kind"] != "full_projection_archive_summary"
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or record["schema_fingerprint"] != PROJECTION_ARCHIVE_SCHEMA_FINGERPRINT
        or record["archive_id"] != directory.name
    ):
        raise ValueError("projection archive summary identity is invalid")
    require_content_id("archive_id", record["archive_id"], "projection_archive")
    providers = _ordered_strings(record["providers"], "providers")
    if not providers or any(
        provider not in {item.value for item in CaptureProvider} for provider in providers
    ):
        raise ValueError("projection archive summary providers are invalid")
    seasons = record["seasons"]
    if (
        not isinstance(seasons, list)
        or any(type(year) is not int or not 2000 <= year <= 2200 for year in seasons)
        or not seasons
        or seasons != sorted(set(seasons))
    ):
        raise ValueError("projection archive summary seasons are invalid")
    periods = _ordered_strings(record["periods"], "periods")
    if not periods or any(
        (match := re.fullmatch(r"([0-9]{4})-W(?:0[1-9]|1[0-9]|2[0-5])", value)) is None
        or int(match.group(1)) not in seasons
        for value in periods
    ):
        raise ValueError("projection archive summary periods are invalid")
    horizons = _ordered_strings(record["horizons"], "horizons")
    if not horizons or any(value not in {"weekly", "ros"} for value in horizons):
        raise ValueError("projection archive summary horizons are invalid")
    scoring = _ordered_strings(record["scoring"], "scoring")
    if not scoring or any(value not in {"STD", "HALF", "PPR"} for value in scoring):
        raise ValueError("projection archive summary scoring is invalid")
    positions = _ordered_strings(record["positions"], "positions")
    if not positions or any(
        re.fullmatch(r"[A-Z][A-Z0-9/]{0,7}", value) is None
        for value in positions
    ):
        raise ValueError("projection archive summary positions are invalid")
    stat_names = _ordered_strings(record["stat_names"], "stat_names")
    if any(re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", value) is None for value in stat_names):
        raise ValueError("projection archive summary stat_names are invalid")
    count_limits = {
        "source_count": 10_000,
        "segments_captured": 100_000_000,
        "table_count": 2_560_000,
        "row_count": 250_000,
        "projected_points_count": 250_000,
    }
    for field, maximum in count_limits.items():
        if type(record[field]) is not int or not 0 <= record[field] <= maximum:
            raise ValueError(f"projection archive summary {field} is invalid")
    if (
        record["source_count"] < 1
        or record["segments_captured"] < record["source_count"]
        or record["table_count"] < record["source_count"]
        or record["row_count"] < record["source_count"]
    ):
        raise ValueError("projection archive summary counts are invalid")
    if record["projected_points_count"] > record["row_count"]:
        raise ValueError("projection archive summary point count is invalid")
    require_captured_at(record["captured_at_first"])
    require_captured_at(record["captured_at_last"])
    if _timestamp(record["captured_at_first"]) > _timestamp(record["captured_at_last"]):
        raise ValueError("projection archive summary capture range is invalid")
    content = {
        key: value for key, value in record.items() if key not in _SUMMARY_HEADER_FIELDS
    }
    if record["summary_id"] != content_id("projection_archive_summary", content):
        raise ValueError("projection archive summary content is invalid")
    return {
        key: value
        for key, value in record.items()
        if key not in _SUMMARY_HEADER_FIELDS
    }


def _ordered_strings(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item or len(item) > 128 for item in value)
    ):
        raise ValueError(f"projection archive summary {name} is invalid")
    try:
        ordered = sorted(set(value))
    except TypeError:
        raise ValueError(f"projection archive summary {name} is invalid") from None
    if value != ordered:
        raise ValueError(f"projection archive summary {name} is invalid")
    return tuple(value)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _read_json(path: Path, maximum: int) -> Mapping[str, object]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > maximum:
            raise ValueError("projection archive file exceeds its size limit")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"could not read projection archive: {error}") from None
    if not isinstance(value, Mapping):
        raise ValueError("projection archive file must contain a JSON object")
    return value


def _json_bytes(value: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("projection archive contains an invalid JSON value") from None
    return encoded + b"\n"


def _write_synced(path: Path, body: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _reject_constant(value: str):
    raise ValueError(f"projection archive contains non-finite JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"projection archive contains duplicate JSON key {key!r}")
        result[key] = value
    return result


__all__ = (
    "load_projection_archive",
    "projection_archive_catalog",
    "save_projection_archive",
)
