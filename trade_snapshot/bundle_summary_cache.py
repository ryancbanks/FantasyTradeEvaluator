"""Small, validated list-view caches for immutable engine bundles."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
from uuid import uuid4

from ._app_support import BUNDLE_ID_PATTERN, bundle_summary
from ._scenario_random import content_id
from .engine_bundle import (
    CANONICAL_ENGINE_BUNDLE_REVISION,
    EngineBundle,
    save_engine_bundle,
)


_SCHEMA_VERSION = 2
_MAX_SUMMARY_BYTES = 4 * 1024 * 1024
_RECORD_FIELDS = {
    "kind",
    "schema_version",
    "bundle_id",
    "engine_bundle_revision",
    "bundle_size",
    "bundle_mtime_ns",
    "summary",
    "summary_id",
}


class BundleSummaryCacheError(RuntimeError):
    """A derived bundle-list cache could not be trusted or persisted."""


def save_bundle_with_summary(
    bundle: EngineBundle, path: str | os.PathLike[str]
) -> Path:
    """Atomically save a bundle, then its replaceable list-view summary."""

    target = save_engine_bundle(bundle, path)
    try:
        save_cached_bundle_summary(bundle, target)
    except BundleSummaryCacheError:
        # The engine bundle is authoritative. A missing derived cache is repaired
        # on the next catalog read and must never make a completed week unusable.
        pass
    return target


def save_cached_bundle_summary(
    bundle: EngineBundle, bundle_path: str | os.PathLike[str]
) -> Path:
    """Write a content-checked summary bound to the bundle file's current stat."""

    if not isinstance(bundle, EngineBundle):
        raise BundleSummaryCacheError("bundle summary cache requires an engine bundle")
    source = Path(bundle_path).resolve()
    if source.stem != bundle.bundle_id or not BUNDLE_ID_PATTERN.fullmatch(source.stem):
        raise BundleSummaryCacheError("bundle summary cache path does not match the bundle")
    try:
        stat = source.stat()
    except OSError as error:
        raise BundleSummaryCacheError("bundle summary source could not be inspected") from error
    if not source.is_file():
        raise BundleSummaryCacheError("bundle summary source is not a file")
    summary = bundle_summary(bundle)
    record = {
        "kind": "fantasy_trade_engine_summary_cache",
        "schema_version": _SCHEMA_VERSION,
        "bundle_id": bundle.bundle_id,
        "engine_bundle_revision": CANONICAL_ENGINE_BUNDLE_REVISION,
        "bundle_size": stat.st_size,
        "bundle_mtime_ns": stat.st_mtime_ns,
        "summary": summary,
        "summary_id": content_id("bundle-summary", summary),
    }
    try:
        encoded = json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_SUMMARY_BYTES:
            raise BundleSummaryCacheError("bundle summary cache exceeds its size limit")
        target = _summary_path(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.json")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except BundleSummaryCacheError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise BundleSummaryCacheError("bundle summary cache could not be saved") from error
    return target


def load_cached_bundle_summary(
    bundle_path: str | os.PathLike[str],
) -> dict[str, object] | None:
    """Return a current summary, ``None`` for missing/stale, or reject corruption."""

    source = Path(bundle_path).resolve()
    target = _summary_path(source)
    if not target.exists():
        return None
    try:
        if not source.is_file() or not target.is_file():
            raise BundleSummaryCacheError("bundle summary cache path is not a file")
        raw = target.read_bytes()
        if not 0 < len(raw) <= _MAX_SUMMARY_BYTES:
            raise BundleSummaryCacheError("bundle summary cache has an unsafe size")
        record = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(record, Mapping) or set(record) != _RECORD_FIELDS:
            raise BundleSummaryCacheError("bundle summary cache fields are invalid")
        if (
            record["kind"] != "fantasy_trade_engine_summary_cache"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != _SCHEMA_VERSION
            or record["engine_bundle_revision"] != CANONICAL_ENGINE_BUNDLE_REVISION
            or record["bundle_id"] != source.stem
            or not BUNDLE_ID_PATTERN.fullmatch(source.stem)
        ):
            raise BundleSummaryCacheError("bundle summary cache identity is invalid")
        stat = source.stat()
        if (
            type(record["bundle_size"]) is not int
            or type(record["bundle_mtime_ns"]) is not int
            or record["bundle_size"] != stat.st_size
            or record["bundle_mtime_ns"] != stat.st_mtime_ns
        ):
            return None
        summary = record["summary"]
        if not isinstance(summary, Mapping) or summary.get("bundle_id") != source.stem:
            raise BundleSummaryCacheError("bundle summary cache payload is invalid")
        if record["summary_id"] != content_id("bundle-summary", summary):
            raise BundleSummaryCacheError("bundle summary cache content does not match")
        return dict(summary)
    except BundleSummaryCacheError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise BundleSummaryCacheError("bundle summary cache is invalid") from error


def _summary_path(bundle_path: Path) -> Path:
    return bundle_path.parent / ".summaries" / f"{bundle_path.stem}.json"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleSummaryCacheError("bundle summary cache contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise BundleSummaryCacheError(f"bundle summary cache contains {value}")


__all__ = (
    "BundleSummaryCacheError",
    "load_cached_bundle_summary",
    "save_bundle_with_summary",
    "save_cached_bundle_summary",
)
