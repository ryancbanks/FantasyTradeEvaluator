import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .fantasypros import fetch_datasets
from .model import DatasetPayload, SnapshotRequest, SnapshotResult


PROVIDERS = ("fantasypros", "espn", "yahoo", "league_state")
CAPTURE_KEYS = {"schema_version", "provider", "captured_at", "payload"}
SECRET_LIKE_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "csrftoken",
    "espns2",
    "key",
    "password",
    "refreshtoken",
    "sessionid",
    "setcookie",
    "swid",
    "token",
    "xapikey",
    "xsrftoken",
}


class SnapshotInputError(ValueError):
    """A capture or output setting is invalid."""


def create_snapshot(
    request: SnapshotRequest,
    output_root: Path | str,
    *,
    imported_files: Mapping[str, Path | str] | None = None,
    fantasypros_api_key: str | None = None,
    fantasypros_fetcher: Callable = fetch_datasets,
    timeout: float = 30,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SnapshotResult:
    """Build one immutable snapshot directory and publish it with an atomic rename."""

    imported_files = dict(imported_files or {})
    unknown = set(imported_files).difference(PROVIDERS)
    if unknown:
        raise SnapshotInputError(f"unknown capture provider: {sorted(unknown)[0]}")
    if timeout <= 0:
        raise SnapshotInputError("timeout must be greater than zero")

    created = _require_aware_datetime(clock())
    created_at = _iso_utc(created)
    imported = {
        provider: _load_capture(Path(path), provider)
        for provider, path in imported_files.items()
    }

    sources: dict[str, dict[str, Any]] = {}
    for provider in PROVIDERS:
        if provider in imported:
            sources[provider] = _import_source(imported[provider], created_at)
        else:
            sources[provider] = _unavailable_source(provider, created_at)

    if "fantasypros" not in imported and fantasypros_api_key:
        try:
            datasets = fantasypros_fetcher(
                api_key=fantasypros_api_key,
                season=request.season,
                week=request.week,
                scoring=request.scoring,
                timeout=timeout,
            )
        except Exception:
            sources["fantasypros"] = _error_source(created_at)
        else:
            sources["fantasypros"] = _api_source(tuple(datasets), created_at)

    snapshot_id = _snapshot_id(request, created)
    failed = tuple(
        provider for provider in PROVIDERS if sources[provider]["status"] != "available"
    )
    ready = not failed
    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "ready_for_offline_compute": ready,
        "request": {
            "sport": "nfl",
            "season": request.season,
            "week": request.week,
            "scoring": request.scoring,
        },
        "sources": sources,
    }
    final_path = _write_atomically(Path(output_root), snapshot_id, manifest)
    return SnapshotResult(
        path=final_path,
        failed_sources=failed,
        ready_for_offline_compute=ready,
    )


def _load_capture(path: Path, expected_provider: str) -> DatasetPayload:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        raise SnapshotInputError(f"could not read capture file: {path.name}") from None
    except json.JSONDecodeError:
        raise SnapshotInputError(f"capture file is not valid JSON: {path.name}") from None

    if not isinstance(document, dict) or set(document) != CAPTURE_KEYS:
        raise SnapshotInputError("capture must contain exactly schema_version, provider, captured_at, and payload")
    if document["schema_version"] != 1:
        raise SnapshotInputError("unsupported capture schema_version")
    if document["provider"] != expected_provider:
        raise SnapshotInputError(f"capture provider must be {expected_provider}")
    captured_at = _parse_capture_time(document["captured_at"])
    _reject_secret_like_keys(document["payload"])
    _canonical_json(document["payload"])
    return DatasetPayload(
        name="capture",
        payload=document["payload"],
        source_metadata={"capture_schema_version": 1, "input_file": path.name},
        source_as_of=captured_at,
    )


def _parse_capture_time(value: object) -> str:
    if not isinstance(value, str):
        raise SnapshotInputError("captured_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SnapshotInputError("captured_at must be an ISO-8601 timestamp") from None
    return _iso_utc(_require_aware_datetime(parsed))


def _require_aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotInputError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _reject_secret_like_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).casefold() if character.isalnum())
            if normalized in SECRET_LIKE_KEYS:
                raise SnapshotInputError(f"capture payload contains forbidden secret-like key: {key}")
            _reject_secret_like_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_like_keys(child)


def _import_source(dataset: DatasetPayload, observed_at: str) -> dict[str, Any]:
    return {
        "status": "available",
        "mode": "import",
        "freshness": {
            "state": "unverified",
            "basis": "capture_timestamp",
            "observed_at": observed_at,
            "source_as_of": dataset.source_as_of,
        },
        "datasets": [dataset],
    }


def _api_source(datasets: tuple[DatasetPayload, ...], observed_at: str) -> dict[str, Any]:
    expected = {"ecr", "projections", "players"}
    if len(datasets) != len(expected) or {dataset.name for dataset in datasets} != expected:
        raise ValueError("FantasyPros fetcher did not return ecr, projections, and players datasets")
    return {
        "status": "available",
        "mode": "official_api",
        "freshness": {
            "state": "unverified",
            "basis": "live_retrieval_without_common_publication_timestamp",
            "observed_at": observed_at,
            "source_as_of": None,
        },
        "datasets": list(datasets),
    }


def _unavailable_source(provider: str, observed_at: str) -> dict[str, Any]:
    reason_codes = {
        "fantasypros": "api_key_or_capture_required",
        "espn": "licensed_adapter_or_capture_required",
        "yahoo": "licensed_adapter_or_capture_required",
        "league_state": "capture_required",
    }
    messages = {
        "fantasypros": "No capture was supplied and FANTASYPROS_API_KEY was not configured.",
        "espn": "No capture was supplied; no licensed ESPN projection adapter is configured.",
        "yahoo": "No capture was supplied; no licensed Yahoo projection adapter is configured.",
        "league_state": "No league-state capture was supplied.",
    }
    return {
        "status": "unavailable",
        "mode": "none",
        "reason_code": reason_codes[provider],
        "message": messages[provider],
        "freshness": {
            "state": "unavailable",
            "basis": "no_source_payload",
            "observed_at": observed_at,
            "source_as_of": None,
        },
        "datasets": [],
    }


def _error_source(observed_at: str) -> dict[str, Any]:
    return {
        "status": "error",
        "mode": "official_api",
        "reason_code": "fetch_failed",
        "message": "FantasyPros fetch failed; no FantasyPros data was stored.",
        "freshness": {
            "state": "error",
            "basis": "fetch_failed",
            "observed_at": observed_at,
            "source_as_of": None,
        },
        "datasets": [],
    }


def _snapshot_id(request: SnapshotRequest, created: datetime) -> str:
    timestamp = created.strftime("%Y%m%dT%H%M%S%fZ")
    return f"nfl-{request.season}-w{request.week:02d}-{request.scoring.casefold()}-{timestamp}"


def _write_atomically(output_root: Path, snapshot_id: str, manifest: dict[str, Any]) -> Path:
    final_path = output_root / snapshot_id
    if final_path.exists():
        raise SnapshotInputError(f"snapshot already exists: {snapshot_id}")
    output_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}-", dir=output_root))
    try:
        data_dir = stage / "data"
        data_dir.mkdir()
        for provider, source in manifest["sources"].items():
            stored_datasets = []
            for dataset in source["datasets"]:
                _reject_secret_like_keys(dataset.source_metadata)
                payload = _canonical_json(dataset.payload)
                relative_path = Path("data") / f"{provider}-{dataset.name}.json"
                (stage / relative_path).write_bytes(payload)
                stored_datasets.append(
                    {
                        "name": dataset.name,
                        "path": relative_path.as_posix(),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "media_type": "application/json",
                        "source_metadata": dict(dataset.source_metadata),
                    }
                )
            source["datasets"] = stored_datasets

        (stage / "manifest.json").write_bytes(_pretty_json(manifest))
        _publish_directory(stage, final_path)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return final_path


def _publish_directory(stage: Path, final_path: Path, attempts: int = 5) -> None:
    """Atomically publish a snapshot despite brief Windows file-indexer locks."""

    for attempt in range(attempts):
        try:
            os.replace(stage, final_path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (2**attempt))


def _canonical_json(value: object) -> bytes:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise SnapshotInputError("provider payload must contain valid JSON values") from None
    return (serialized + "\n").encode("utf-8")


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
