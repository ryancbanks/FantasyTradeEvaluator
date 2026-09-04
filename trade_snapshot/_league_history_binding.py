"""Validation and record encoding for bundle-to-history bindings."""

from __future__ import annotations

from typing import Mapping


def normalized_binding_fields(binding) -> dict[str, object]:
    from .league_history import (
        HISTORY_CAPTURE_BINDING_TOLERANCE,
        _aware_datetime,
        _bundle_id,
        _identifier,
        _league_key,
        _season,
    )

    values = {
        "league_key": _league_key(binding.league_key),
        "season": _season(binding.season),
        "bundle_id": _bundle_id(binding.bundle_id),
        "captured_at": _aware_datetime("bundle captured_at", binding.captured_at),
        "host_snapshot_id": binding.host_snapshot_id,
        "host_captured_at": binding.host_captured_at,
        "history_capture_id": binding.history_capture_id,
        "roster_ownership_id": binding.roster_ownership_id,
    }
    exact = tuple(values[name] for name in (
        "host_snapshot_id",
        "host_captured_at",
        "history_capture_id",
        "roster_ownership_id",
    ))
    if any(value is not None for value in exact) and any(
        value is None for value in exact
    ):
        raise ValueError("exact bundle binding evidence must be complete")
    if values["host_snapshot_id"] is None:
        return values

    values["host_snapshot_id"] = _identifier(
        "binding host_snapshot_id", values["host_snapshot_id"]
    )
    host_captured_at = _aware_datetime(
        "binding host_captured_at", values["host_captured_at"]
    )
    if (
        host_captured_at > values["captured_at"]
        or values["captured_at"] - host_captured_at
        > HISTORY_CAPTURE_BINDING_TOLERANCE
    ):
        raise ValueError("host capture is stale or later than bundle binding")
    values["host_captured_at"] = host_captured_at
    values["history_capture_id"] = _identifier(
        "history_capture_id", values["history_capture_id"]
    )
    roster_id = _identifier(
        "roster_ownership_id", values["roster_ownership_id"]
    )
    if not roster_id.startswith("history-roster_"):
        raise ValueError("roster_ownership_id has an invalid identity prefix")
    values["roster_ownership_id"] = roster_id
    return values


def binding_record(binding) -> dict[str, object]:
    from .league_history import _timestamp

    return {
        "league_key": binding.league_key,
        "season": binding.season,
        "bundle_id": binding.bundle_id,
        "captured_at": _timestamp(binding.captured_at),
        "history_capture_id": binding.history_capture_id,
        "host_captured_at": (
            None
            if binding.host_captured_at is None
            else _timestamp(binding.host_captured_at)
        ),
        "host_snapshot_id": binding.host_snapshot_id,
        "roster_ownership_id": binding.roster_ownership_id,
    }


def binding_from_record(binding_type, value: object):
    from .league_history import _datetime_from_record

    legacy = {"league_key", "season", "bundle_id", "captured_at"}
    current = legacy | {
        "history_capture_id",
        "host_captured_at",
        "host_snapshot_id",
        "roster_ownership_id",
    }
    if not isinstance(value, Mapping) or set(value) not in (legacy, current):
        raise ValueError("bundle binding fields are invalid")
    return binding_type(
        value["league_key"],
        value["season"],
        value["bundle_id"],
        _datetime_from_record("bundle captured_at", value["captured_at"]),
        value.get("host_snapshot_id"),
        (
            None
            if value.get("host_captured_at") is None
            else _datetime_from_record(
                "binding host_captured_at", value["host_captured_at"]
            )
        ),
        value.get("history_capture_id"),
        value.get("roster_ownership_id"),
    )


__all__ = (
    "binding_from_record",
    "binding_record",
    "normalized_binding_fields",
)
