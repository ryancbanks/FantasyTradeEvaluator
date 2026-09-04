"""Small local-only catalog that assigns stable opaque IDs to private leagues."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
from threading import Lock
from uuid import uuid4


_LOCK = Lock()
_SCHEMA_VERSION = 1
_BINDING_ID = re.compile(r"^league_[0-9a-f]{32}$")


def get_or_create_league_binding(
    path: str | os.PathLike[str],
    provider: str,
    source_league_id: str,
) -> str:
    provider = _text("provider", provider).casefold()
    source_league_id = _text("source_league_id", source_league_id)
    target = Path(path)
    with _LOCK:
        entries = _load(target) if target.exists() else []
        for row in entries:
            if (
                row["provider"] == provider
                and row["source_league_id"] == source_league_id
            ):
                return row["league_binding_id"]
        binding_id = f"league_{uuid4().hex}"
        entries.append(
            {
                "league_binding_id": binding_id,
                "provider": provider,
                "source_league_id": source_league_id,
            }
        )
        _save(target, entries)
        return binding_id


def _load(path):
    try:
        record = json.loads(path.read_text("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read the local league-binding catalog: {error}") from None
    if (
        not isinstance(record, Mapping)
        or set(record) != {"schema_version", "bindings"}
        or record["schema_version"] != _SCHEMA_VERSION
        or not isinstance(record["bindings"], list)
    ):
        raise ValueError("local league-binding catalog fields are invalid")
    entries = []
    keys = set()
    binding_ids = set()
    for row in record["bindings"]:
        if not isinstance(row, Mapping) or set(row) != {
            "league_binding_id", "provider", "source_league_id"
        }:
            raise ValueError("local league-binding entry fields are invalid")
        entry = {
            "league_binding_id": _text("league_binding_id", row["league_binding_id"]),
            "provider": _text("provider", row["provider"]).casefold(),
            "source_league_id": _text("source_league_id", row["source_league_id"]),
        }
        if not _BINDING_ID.fullmatch(entry["league_binding_id"]):
            raise ValueError("local league_binding_id is invalid")
        key = entry["provider"], entry["source_league_id"]
        if key in keys or entry["league_binding_id"] in binding_ids:
            raise ValueError("local league-binding catalog contains duplicate identities")
        keys.add(key)
        binding_ids.add(entry["league_binding_id"])
        entries.append(entry)
    return entries


def _save(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "bindings": sorted(
            entries,
            key=lambda row: (row["provider"], row["source_league_id"]),
        ),
    }
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"league-binding catalog contains duplicate JSON key {key!r}")
        result[key] = value
    return result


__all__ = ("get_or_create_league_binding",)
