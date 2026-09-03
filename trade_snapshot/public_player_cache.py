"""Atomic, local reuse of sanitized public Player Lab evidence by NFL week."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .public_player_data import PublicPlayerDataSnapshot


_CACHE_SCHEMA_VERSION = 1
_MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_DECODED_BYTES = 256 * 1024 * 1024


class PublicPlayerCacheError(RuntimeError):
    """A local public-data cache entry could not be trusted or persisted."""


def load_public_player_week(
    data_directory: str | Path,
    season: int,
    week: int,
) -> PublicPlayerDataSnapshot | None:
    """Load one exact season/week snapshot, or return ``None`` when absent."""

    target = _cache_path(data_directory, season, week)
    if not target.exists():
        return None
    if not target.is_file():
        raise PublicPlayerCacheError("public player-data cache path is not a file")
    try:
        compressed_size = target.stat().st_size
        if not 0 < compressed_size <= _MAX_COMPRESSED_BYTES:
            raise PublicPlayerCacheError("public player-data cache has an unsafe size")
        with gzip.open(target, "rb") as source:
            decoded = source.read(_MAX_DECODED_BYTES + 1)
        if len(decoded) > _MAX_DECODED_BYTES:
            raise PublicPlayerCacheError("public player-data cache exceeds its decoded limit")
        record = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(record, dict) or set(record) != {
            "kind", "schema_version", "season", "week", "player_data",
        }:
            raise PublicPlayerCacheError("public player-data cache fields are invalid")
        if (
            record["kind"] != "public_player_week_cache"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != _CACHE_SCHEMA_VERSION
            or type(record["season"]) is not int
            or record["season"] != season
            or type(record["week"]) is not int
            or record["week"] != week
        ):
            raise PublicPlayerCacheError("public player-data cache context is invalid")
        snapshot = PublicPlayerDataSnapshot.from_record(record["player_data"])
        if snapshot.season != season:
            raise PublicPlayerCacheError("cached public player data has the wrong season")
        return snapshot
    except PublicPlayerCacheError:
        raise
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError) as error:
        raise PublicPlayerCacheError("public player-data cache is invalid") from error


def save_public_player_week(
    data_directory: str | Path,
    season: int,
    week: int,
    snapshot: PublicPlayerDataSnapshot,
) -> Path:
    """Atomically persist one sanitized, content-verified weekly snapshot."""

    target = _cache_path(data_directory, season, week)
    if not isinstance(snapshot, PublicPlayerDataSnapshot) or snapshot.season != season:
        raise PublicPlayerCacheError("public player-data cache snapshot is invalid")
    record = {
        "kind": "public_player_week_cache",
        "schema_version": _CACHE_SCHEMA_VERSION,
        "season": season,
        "week": week,
        "player_data": snapshot.to_record(),
    }
    try:
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_DECODED_BYTES:
            raise PublicPlayerCacheError("public player-data cache exceeds its decoded limit")
        compressed = gzip.compress(encoded, compresslevel=6, mtime=0)
        if len(compressed) > _MAX_COMPRESSED_BYTES:
            raise PublicPlayerCacheError("public player-data cache exceeds its stored limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with NamedTemporaryFile(
                mode="wb", prefix=f".{target.name}.", suffix=".tmp",
                dir=target.parent, delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(compressed)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return target
    except PublicPlayerCacheError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise PublicPlayerCacheError("public player-data cache could not be saved") from error


def _cache_path(data_directory: str | Path, season: int, week: int) -> Path:
    if type(season) is not int or not 2012 <= season <= 9999:
        raise PublicPlayerCacheError("cache season is invalid")
    if type(week) is not int or not 1 <= week <= 25:
        raise PublicPlayerCacheError("cache week is invalid")
    root = Path(data_directory).resolve()
    return root / "public-player-cache" / f"{season}-week-{week:02d}.json.gz"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicPlayerCacheError("public player-data cache contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise PublicPlayerCacheError(f"public player-data cache contains {value}")


__all__ = (
    "PublicPlayerCacheError",
    "load_public_player_week",
    "save_public_player_week",
)
