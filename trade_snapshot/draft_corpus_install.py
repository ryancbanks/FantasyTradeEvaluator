"""Bounded, resumable installation of Draft Lab's public starter corpus."""

import json
import os
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from ._scenario_random import content_id
from .draft_corpus_builder import (
    SourceStamp,
    StarterCorpusFiles,
    build_starter_corpus,
)
from .draft_corpus_sources import STARTER_CORPUS_YEARS, STARTER_TRANSFORM_VERSION
from .draft_persistence import DraftFileStore

_ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "fantasyfootballcalculator.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "www.fantasyfootballcalculator.com",
    }
)
_RELEASE_API = "https://api.github.com/repos/nflverse/nflverse-data/releases/tags/{}"
_FFC_URL = (
    "https://fantasyfootballcalculator.com/api/v1/adp/ppr?teams=12&year={}&position=all"
)
_CHUNK_BYTES = 128 * 1024
_STATE_WRITE_BYTES = 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 30
_MAX_RELEASE_METADATA_BYTES = 16 * 1024 * 1024
_MAX_ASSET_BYTES = {
    "schedule": 2 * 1024 * 1024,
    "ffc_adp": 2 * 1024 * 1024,
    "player_stats": 4 * 1024 * 1024,
    "team_stats": 1024 * 1024,
    "roster": 32 * 1024 * 1024,
}
_ASSET_KEY = re.compile(r"^[a-z_]+(?::[0-9]{4})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CorpusInstallCancelled(InterruptedError):
    pass


class CorpusInstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CorpusAsset:
    key: str
    role: str
    season: int | None
    filename: str
    url: str
    maximum_bytes: int
    expected_size: int | None = None
    expected_sha256: str | None = None
    source_updated_at: str | None = None

    def __post_init__(self):
        if not _ASSET_KEY.fullmatch(self.key) or self.role not in _MAX_ASSET_BYTES:
            raise ValueError("corpus install asset key or role is invalid")
        if self.season is not None and type(self.season) is not int:
            raise ValueError("corpus install asset season is invalid")
        if Path(self.filename).name != self.filename or not self.filename:
            raise ValueError("corpus install asset filename is unsafe")
        _validate_https_url(self.url)
        cap = _MAX_ASSET_BYTES[self.role]
        if type(self.maximum_bytes) is not int or not 1 <= self.maximum_bytes <= cap:
            raise ValueError("corpus install asset size cap is invalid")
        if self.expected_size is not None and (
            type(self.expected_size) is not int
            or not 1 <= self.expected_size <= self.maximum_bytes
        ):
            raise ValueError("corpus install expected asset size is invalid")
        if self.expected_sha256 is not None and not _SHA256.fullmatch(
            self.expected_sha256
        ):
            raise ValueError("corpus install asset digest is invalid")
        if self.source_updated_at is not None:
            _timestamp(self.source_updated_at)

    def to_record(self):
        return {
            "key": self.key,
            "role": self.role,
            "season": self.season,
            "filename": self.filename,
            "url": self.url,
            "maximum_bytes": self.maximum_bytes,
            "expected_size": self.expected_size,
            "expected_sha256": self.expected_sha256,
            "source_updated_at": self.source_updated_at,
        }

    @classmethod
    def from_record(cls, record):
        keys = {
            "key",
            "role",
            "season",
            "filename",
            "url",
            "maximum_bytes",
            "expected_size",
            "expected_sha256",
            "source_updated_at",
        }
        _exact_keys("corpus asset", record, keys)
        return cls(**record)


@dataclass(frozen=True, slots=True)
class CorpusInstallManifest:
    years: tuple[int, ...]
    assets: tuple[CorpusAsset, ...]
    transform_version: int = STARTER_TRANSFORM_VERSION
    manifest_id: str = field(init=False)

    def __post_init__(self):
        years = tuple(self.years)
        assets = tuple(self.assets)
        if years != tuple(sorted(set(years))) or not years:
            raise ValueError("corpus install years must be unique and increasing")
        if not set(years).issubset(STARTER_CORPUS_YEARS):
            raise ValueError("corpus install includes an unsupported year")
        if self.transform_version != STARTER_TRANSFORM_VERSION:
            raise ValueError("corpus install transform version is unsupported")
        if not assets or any(not isinstance(row, CorpusAsset) for row in assets):
            raise ValueError("corpus install assets are invalid")
        keys = [row.key for row in assets]
        if len(keys) != len(set(keys)):
            raise ValueError("corpus install contains duplicate asset keys")
        filenames = [row.filename for row in assets]
        if len(filenames) != len(set(filenames)):
            raise ValueError("corpus install contains duplicate asset filenames")
        _validate_manifest_roles(years, assets)
        object.__setattr__(self, "years", years)
        object.__setattr__(
            self, "assets", tuple(sorted(assets, key=lambda row: row.key))
        )
        object.__setattr__(
            self,
            "manifest_id",
            content_id("draft_corpus_install_manifest", self._content_record()),
        )

    def _content_record(self):
        return {
            "transform_version": self.transform_version,
            "years": list(self.years),
            "assets": [row.to_record() for row in self.assets],
        }

    def to_record(self):
        return {
            "kind": "draft_corpus_install_manifest",
            "schema_version": 1,
            **self._content_record(),
            "manifest_id": self.manifest_id,
        }

    @classmethod
    def from_record(cls, record):
        keys = {
            "kind",
            "schema_version",
            "transform_version",
            "years",
            "assets",
            "manifest_id",
        }
        _exact_keys("corpus install manifest", record, keys)
        if (
            record["kind"] != "draft_corpus_install_manifest"
            or record["schema_version"] != 1
        ):
            raise ValueError("corpus install manifest kind or version is invalid")
        if not isinstance(record["years"], list) or not isinstance(
            record["assets"], list
        ):
            raise ValueError("corpus install manifest arrays are invalid")
        manifest = cls(
            tuple(record["years"]),
            tuple(CorpusAsset.from_record(row) for row in record["assets"]),
            record["transform_version"],
        )
        if record["manifest_id"] != manifest.manifest_id:
            raise ValueError("corpus install manifest content does not match its ID")
        return manifest


class SecureHttpTransport:
    """HTTPS-only urllib transport with redirect-host enforcement."""

    def __init__(self):
        self._opener = build_opener(_AllowlistedRedirectHandler())

    def open(self, request: Request, timeout: int = _REQUEST_TIMEOUT_SECONDS):
        _validate_https_url(request.full_url)
        return self._opener.open(request, timeout=timeout)


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def resolve_starter_manifest(
    *,
    years: tuple[int, ...] = STARTER_CORPUS_YEARS,
    transport=None,
) -> CorpusInstallManifest:
    """Resolve exact nflverse release assets once so an install stays pinned."""

    years = tuple(years)
    transport = transport or SecureHttpTransport()
    releases = {
        tag: _release_assets(transport, tag)
        for tag in ("schedules", "stats_player", "stats_team", "weekly_rosters")
    }
    assets = []
    assets.append(
        _github_asset(
            releases["schedules"], "games.csv.gz", "schedule", None, "schedule"
        )
    )
    for year in years:
        assets.extend(
            (
                CorpusAsset(
                    key=f"ffc_adp:{year}",
                    role="ffc_adp",
                    season=year,
                    filename=f"ffc_adp_{year}.json",
                    url=_FFC_URL.format(year),
                    maximum_bytes=_MAX_ASSET_BYTES["ffc_adp"],
                ),
                _github_asset(
                    releases["stats_player"],
                    f"stats_player_week_{year}.csv.gz",
                    "player_stats",
                    year,
                    f"player_stats:{year}",
                ),
                _github_asset(
                    releases["stats_team"],
                    f"stats_team_week_{year}.csv.gz",
                    "team_stats",
                    year,
                    f"team_stats:{year}",
                ),
            )
        )
    for year in sorted(set(years) | {year - 1 for year in years}):
        release_assets = releases["weekly_rosters"]
        compressed = f"roster_weekly_{year}.csv.gz"
        filename = (
            compressed if compressed in release_assets else f"roster_weekly_{year}.csv"
        )
        assets.append(
            _github_asset(release_assets, filename, "roster", year, f"roster:{year}")
        )
    return CorpusInstallManifest(years, tuple(assets))


class DraftCorpusInstaller:
    """Install, validate, and persist one pinned starter corpus sequentially."""

    def __init__(
        self,
        data_directory: str | os.PathLike[str],
        *,
        store: DraftFileStore | None = None,
        transport=None,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ):
        self.root = Path(data_directory).resolve() / "draft-corpus-installs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store or DraftFileStore(data_directory)
        self.transport = transport or SecureHttpTransport()
        self._clock = clock
        self._sleep = sleeper
        self._last_ffc_request: float | None = None
        self._lock = RLock()
        self._running = False

    def install(self, *, should_cancel=lambda: False, on_progress=lambda record: None):
        with self._lock:
            if self._running:
                raise RuntimeError("a starter corpus installation is already running")
            self._running = True
        state = None
        phase = "manifest"
        try:
            manifest = self._manifest(on_progress, should_cancel)
            directory = self.root / manifest.manifest_id
            downloads = directory / "downloads"
            downloads.mkdir(parents=True, exist_ok=True)
            manifest_path = directory / "manifest.json"
            _write_json_once_or_verify(manifest_path, manifest.to_record())
            receipt_path = directory / "receipt.json"
            existing = self._existing_receipt(receipt_path, manifest)
            if existing is not None:
                on_progress(_progress_from_receipt(existing))
                return existing
            state_path = directory / "state.json"
            state = _load_state(state_path, manifest)
            phase = "download"
            state["status"] = "downloading"
            state["error"] = None
            _write_json_atomic(state_path, state)
            for index, asset in enumerate(manifest.assets, 1):
                _cancel(should_cancel)
                progress = {
                    "phase": "download",
                    "asset_index": index,
                    "asset_count": len(manifest.assets),
                    "asset_key": asset.key,
                }
                on_progress(progress)
                self._download(
                    asset,
                    downloads,
                    state,
                    state_path,
                    should_cancel,
                    on_progress,
                    progress,
                )
            phase = "build"
            state["status"] = "building"
            _write_json_atomic(state_path, state)
            on_progress({"phase": "build", "season_count": len(manifest.years)})
            build = build_starter_corpus(
                _builder_files(manifest, downloads, state),
                years=manifest.years,
                should_cancel=should_cancel,
                on_season=lambda season, completed, total: on_progress(
                    {
                        "phase": "build",
                        "season": season,
                        "completed_seasons": completed,
                        "season_count": total,
                    }
                ),
            )
            _cancel(should_cancel)
            phase = "persist"
            summary = self.store.import_corpus(build.corpus.to_record())
            installed_at = datetime.now(timezone.utc).isoformat()
            receipt = {
                "kind": "draft_corpus_install_receipt",
                "schema_version": 1,
                "manifest_id": manifest.manifest_id,
                "transform_version": manifest.transform_version,
                "status": build.status,
                "status_label": "Ready"
                if build.status == "ready"
                else "Ready with gaps",
                "corpus_id": build.corpus.corpus_id,
                "installed_at": installed_at,
                "serialized_bytes": build.serialized_bytes,
                "coverage": build.coverage,
                "sources": _source_receipt(manifest, state),
                "summary": summary,
            }
            _write_json_atomic(receipt_path, receipt)
            state.update(
                {
                    "status": build.status,
                    "phase": "complete",
                    "corpus_id": build.corpus.corpus_id,
                    "error": None,
                }
            )
            _write_json_atomic(state_path, state)
            on_progress(_progress_from_receipt(receipt))
            return receipt
        except InterruptedError:
            if state is not None:
                state.update({"status": "paused", "phase": phase, "error": None})
                _write_json_atomic(state_path, state)
            raise CorpusInstallCancelled(
                "starter corpus installation paused safely"
            ) from None
        except Exception as error:
            if state is not None:
                state.update(
                    {
                        "status": "incompatible"
                        if phase in {"build", "persist"}
                        else "failed",
                        "phase": phase,
                        "error": str(error)[:2_048],
                    }
                )
                _write_json_atomic(state_path, state)
            if isinstance(error, (ValueError, CorpusInstallError)):
                raise
            raise CorpusInstallError(
                f"starter corpus installation failed: {error}"
            ) from None
        finally:
            with self._lock:
                self._running = False

    def catalog(self):
        rows = []
        for path in sorted(self.root.glob("*/receipt.json")):
            try:
                rows.append(_validate_receipt(_read_json(path, 8 * 1024 * 1024)))
            except (OSError, ValueError) as error:
                rows.append(
                    {
                        "status": "incompatible",
                        "file": str(path.name),
                        "error": str(error),
                    }
                )
        return tuple(rows)

    def recoverable_state(self):
        cached = self.root / f"starter-manifest-v{STARTER_TRANSFORM_VERSION}.json"
        if not cached.is_file():
            return {"status": "not_installed", "phase": "manifest"}
        try:
            manifest = CorpusInstallManifest.from_record(
                _read_json(cached, 1024 * 1024)
            )
            state_path = self.root / manifest.manifest_id / "state.json"
            if not state_path.is_file():
                return {"status": "not_installed", "phase": "download"}
            return _public_state(_load_state(state_path, manifest))
        except (OSError, ValueError) as error:
            return {"status": "incompatible", "phase": "manifest", "error": str(error)}

    def _manifest(self, on_progress, should_cancel):
        cached = self.root / f"starter-manifest-v{STARTER_TRANSFORM_VERSION}.json"
        if cached.is_file():
            return CorpusInstallManifest.from_record(_read_json(cached, 1024 * 1024))
        _cancel(should_cancel)
        on_progress({"phase": "manifest", "message": "Pinning public source files"})
        manifest = resolve_starter_manifest(transport=self.transport)
        _write_json_atomic(cached, manifest.to_record())
        return manifest

    def _existing_receipt(self, path, manifest):
        if not path.is_file():
            return None
        receipt = _validate_receipt(_read_json(path, 8 * 1024 * 1024))
        if receipt["manifest_id"] != manifest.manifest_id:
            raise ValueError("starter corpus receipt does not match its manifest")
        try:
            corpus = self.store.load_corpus(receipt["corpus_id"])
        except FileNotFoundError:
            return None
        if corpus.corpus_id != receipt["corpus_id"]:
            raise ValueError("starter corpus receipt points to a different corpus")
        return receipt

    def _download(
        self, asset, directory, state, state_path, should_cancel, on_progress, progress
    ):
        final_path = directory / asset.filename
        row = state["assets"].setdefault(asset.key, _initial_asset_state())
        if final_path.is_file() and _saved_asset_is_valid(final_path, asset, row):
            row.update(
                {
                    "status": "complete",
                    "bytes": final_path.stat().st_size,
                    "sha256": _file_sha256(final_path),
                }
            )
            return
        if final_path.exists():
            final_path.unlink()
        part_path = final_path.with_name(f"{final_path.name}.part")
        start = part_path.stat().st_size if part_path.is_file() else 0
        if start > asset.maximum_bytes:
            part_path.unlink()
            start = 0
        if asset.expected_size is not None and start == asset.expected_size:
            digest = _file_sha256(part_path)
            if asset.expected_sha256 is None or digest == asset.expected_sha256:
                _replace_with_retry(part_path, final_path)
                row.update({"status": "complete", "bytes": start, "sha256": digest})
                _write_json_atomic(state_path, state)
                return
            part_path.unlink()
            start = 0
        if asset.role == "ffc_adp" and self._last_ffc_request is not None:
            delay = 1.0 - (self._clock() - self._last_ffc_request)
            if delay > 0:
                self._sleep(delay)
        headers = {
            "Accept": "application/json"
            if asset.role == "ffc_adp"
            else "application/octet-stream",
            "User-Agent": "FantasyTradeEvaluator/0.2.0 historical-corpus-installer",
        }
        if start:
            headers["Range"] = f"bytes={start}-"
            if row.get("etag"):
                headers["If-Range"] = row["etag"]
        request = Request(asset.url, headers=headers)
        try:
            response = self.transport.open(request, timeout=_REQUEST_TIMEOUT_SECONDS)
        except (HTTPError, URLError, TimeoutError) as error:
            raise CorpusInstallError(
                f"could not download {asset.key}: {error}"
            ) from None
        if asset.role == "ffc_adp":
            self._last_ffc_request = self._clock()
        with response:
            _validate_https_url(response.geturl())
            status = response.getcode()
            append = start > 0 and status == 206
            if start and status == 206:
                _validate_content_range(response.headers.get("Content-Range"), start)
            elif status == 200:
                start = 0
                append = False
            else:
                raise CorpusInstallError(f"download {asset.key} returned HTTP {status}")
            length = _optional_header_integer(response.headers.get("Content-Length"))
            if length is not None and start + length > asset.maximum_bytes:
                raise CorpusInstallError(f"download {asset.key} exceeds its size limit")
            etag = response.headers.get("ETag")
            if etag is not None and len(etag) <= 512:
                row["etag"] = etag
            mode = "ab" if append else "wb"
            downloaded = start
            last_saved = start
            with part_path.open(mode) as output:
                while True:
                    _cancel(should_cancel)
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > asset.maximum_bytes:
                        raise CorpusInstallError(
                            f"download {asset.key} exceeds its size limit"
                        )
                    output.write(chunk)
                    if downloaded - last_saved >= _STATE_WRITE_BYTES:
                        row.update({"status": "downloading", "bytes": downloaded})
                        _write_json_atomic(state_path, state)
                        on_progress({**progress, "downloaded_bytes": downloaded})
                        last_saved = downloaded
        if asset.expected_size is not None and downloaded != asset.expected_size:
            raise CorpusInstallError(
                f"download {asset.key} size {downloaded} does not match pinned size {asset.expected_size}"
            )
        digest = _file_sha256(part_path)
        if asset.expected_sha256 is not None and digest != asset.expected_sha256:
            raise CorpusInstallError(
                f"download {asset.key} does not match its pinned SHA-256"
            )
        _replace_with_retry(part_path, final_path)
        row.update({"status": "complete", "bytes": downloaded, "sha256": digest})
        _write_json_atomic(state_path, state)


def _release_assets(transport, tag):
    request = Request(
        _RELEASE_API.format(tag),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "FantasyTradeEvaluator/0.2.0 historical-corpus-installer",
        },
    )
    try:
        response = transport.open(request, timeout=_REQUEST_TIMEOUT_SECONDS)
    except (HTTPError, URLError, TimeoutError) as error:
        raise CorpusInstallError(
            f"could not resolve nflverse {tag!r} release: {error}"
        ) from None
    with response:
        _validate_https_url(response.geturl())
        payload = response.read(_MAX_RELEASE_METADATA_BYTES + 1)
    if len(payload) > _MAX_RELEASE_METADATA_BYTES:
        raise CorpusInstallError("nflverse release metadata exceeds its size limit")
    try:
        record = json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CorpusInstallError(
            f"nflverse {tag!r} release metadata is invalid: {error}"
        ) from None
    if not isinstance(record, Mapping) or not isinstance(record.get("assets"), list):
        raise CorpusInstallError(f"nflverse {tag!r} release has no asset list")
    assets = {}
    for raw in record["assets"]:
        if not isinstance(raw, Mapping):
            raise CorpusInstallError("nflverse release asset metadata is invalid")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise CorpusInstallError("nflverse release asset name is invalid")
        if name in assets:
            raise CorpusInstallError(f"nflverse release has duplicate asset {name!r}")
        assets[name] = raw
    return assets


def _github_asset(release, filename, role, season, key):
    try:
        raw = release[filename]
    except KeyError:
        raise CorpusInstallError(f"nflverse release is missing {filename}") from None
    required = {"name", "size", "browser_download_url", "updated_at"}
    if not required.issubset(raw) or raw["name"] != filename:
        raise CorpusInstallError(f"nflverse metadata for {filename} is incomplete")
    size = raw["size"]
    cap = _MAX_ASSET_BYTES[role]
    if type(size) is not int or not 1 <= size <= cap:
        raise CorpusInstallError(f"nflverse asset {filename} exceeds its size limit")
    digest = raw.get("digest")
    expected_sha = None
    if digest is not None:
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise CorpusInstallError(f"nflverse asset {filename} digest is unsupported")
        expected_sha = digest.removeprefix("sha256:")
    return CorpusAsset(
        key=key,
        role=role,
        season=season,
        filename=filename,
        url=raw["browser_download_url"],
        maximum_bytes=cap,
        expected_size=size,
        expected_sha256=expected_sha,
        source_updated_at=raw["updated_at"],
    )


def _builder_files(manifest, downloads, state):
    roles = defaultdict(dict)
    schedule = None
    stamps = {}
    for asset in manifest.assets:
        path = downloads / asset.filename
        row = state["assets"][asset.key]
        stamps[asset.key] = SourceStamp(
            asset.url, row["sha256"], row["bytes"], asset.source_updated_at
        )
        if asset.role == "schedule":
            schedule = path
        else:
            roles[asset.role][asset.season] = path
    if schedule is None:
        raise ValueError("starter corpus manifest has no schedule")
    return StarterCorpusFiles(
        schedule,
        roles["ffc_adp"],
        roles["player_stats"],
        roles["team_stats"],
        roles["roster"],
        stamps,
    )


def _validate_manifest_roles(years, assets):
    keys = {row.key for row in assets}
    expected = {"schedule"}
    for year in years:
        expected.update(
            {f"ffc_adp:{year}", f"player_stats:{year}", f"team_stats:{year}"}
        )
    expected.update(
        f"roster:{year}" for year in set(years) | {year - 1 for year in years}
    )
    if keys != expected:
        raise ValueError(
            "corpus install manifest does not contain the required source set"
        )
    for asset in assets:
        if asset.key == "schedule" and (
            asset.role != "schedule" or asset.season is not None
        ):
            raise ValueError("corpus schedule asset metadata is invalid")
        if asset.key != "schedule" and asset.key != f"{asset.role}:{asset.season}":
            raise ValueError("corpus season asset metadata is invalid")


def _initial_state(manifest):
    return {
        "kind": "draft_corpus_install_state",
        "schema_version": 1,
        "manifest_id": manifest.manifest_id,
        "status": "queued",
        "phase": "download",
        "corpus_id": None,
        "error": None,
        "assets": {asset.key: _initial_asset_state() for asset in manifest.assets},
    }


def _initial_asset_state():
    return {"status": "queued", "bytes": 0, "sha256": None, "etag": None}


def _load_state(path, manifest):
    if not path.is_file():
        return _initial_state(manifest)
    state = _read_json(path, 2 * 1024 * 1024)
    keys = {
        "kind",
        "schema_version",
        "manifest_id",
        "status",
        "phase",
        "corpus_id",
        "error",
        "assets",
    }
    _exact_keys("corpus install state", state, keys)
    if (
        state["kind"] != "draft_corpus_install_state"
        or state["schema_version"] != 1
        or state["manifest_id"] != manifest.manifest_id
        or not isinstance(state["assets"], Mapping)
        or set(state["assets"]) != {asset.key for asset in manifest.assets}
    ):
        raise ValueError("corpus install state is incompatible")
    for row in state["assets"].values():
        _exact_keys(
            "corpus install asset state", row, {"status", "bytes", "sha256", "etag"}
        )
        if type(row["bytes"]) is not int or row["bytes"] < 0:
            raise ValueError("corpus install asset progress is invalid")
        if row["sha256"] is not None and not _SHA256.fullmatch(row["sha256"]):
            raise ValueError("corpus install saved digest is invalid")
    return state


def _saved_asset_is_valid(path, asset, state):
    size = path.stat().st_size
    if size > asset.maximum_bytes or (
        asset.expected_size is not None and size != asset.expected_size
    ):
        return False
    expected = asset.expected_sha256 or state.get("sha256")
    return expected is not None and _file_sha256(path) == expected


def _source_receipt(manifest, state):
    return [
        {
            "key": asset.key,
            "role": asset.role,
            "season": asset.season,
            "url": asset.url,
            "size": state["assets"][asset.key]["bytes"],
            "sha256": state["assets"][asset.key]["sha256"],
            "source_updated_at": asset.source_updated_at,
            "license": None if asset.role == "ffc_adp" else "CC-BY-4.0",
        }
        for asset in manifest.assets
    ]


def _validate_receipt(record):
    keys = {
        "kind",
        "schema_version",
        "manifest_id",
        "transform_version",
        "status",
        "status_label",
        "corpus_id",
        "installed_at",
        "serialized_bytes",
        "coverage",
        "sources",
        "summary",
    }
    _exact_keys("corpus install receipt", record, keys)
    if (
        record["kind"] != "draft_corpus_install_receipt"
        or record["schema_version"] != 1
        or record["status"] not in {"ready", "ready_with_gaps"}
        or not isinstance(record["corpus_id"], str)
        or not isinstance(record["coverage"], Mapping)
        or not isinstance(record["sources"], list)
        or not isinstance(record["summary"], Mapping)
    ):
        raise ValueError("corpus install receipt is invalid")
    _timestamp(record["installed_at"])
    return record


def _progress_from_receipt(receipt):
    return {
        "phase": "complete",
        "status": receipt["status"],
        "status_label": receipt["status_label"],
        "corpus_id": receipt["corpus_id"],
        "gap_count": receipt["coverage"].get("gap_count", 0),
    }


def _public_state(state):
    complete = sum(row.get("status") == "complete" for row in state["assets"].values())
    return {
        "status": state["status"],
        "phase": state["phase"],
        "corpus_id": state["corpus_id"],
        "error": state["error"],
        "completed_assets": complete,
        "asset_count": len(state["assets"]),
    }


def _validate_https_url(url):
    if not isinstance(url, str) or len(url) > 4_096:
        raise ValueError("corpus source URL is invalid")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("corpus source URL is outside the HTTPS allowlist")


def _validate_content_range(value, start):
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)", value or "")
    if match is None or int(match.group(1)) != start:
        raise CorpusInstallError("resumed download returned an invalid Content-Range")


def _optional_header_integer(value):
    if value is None:
        return None
    if not value.isascii() or not value.isdigit():
        raise CorpusInstallError("download Content-Length is invalid")
    return int(value)


def _file_sha256(path):
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_once_or_verify(path, record):
    if path.is_file():
        if _read_json(path, 1024 * 1024) != record:
            raise ValueError("saved starter corpus manifest is incompatible")
        return
    _write_json_atomic(path, record)


def _write_json_atomic(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{uuid4().hex}.tmp.json")
    try:
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path, maximum):
    if path.stat().st_size > maximum:
        raise ValueError("saved corpus install record exceeds its size limit")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"could not read saved corpus install record: {error}"
        ) from None
    if not isinstance(value, Mapping):
        raise ValueError("saved corpus install record must be an object")
    return value


def _replace_with_retry(source, destination):
    """Tolerate brief Windows scanner/indexer handles on newly written files."""

    for delay in (0.0, 0.02, 0.05, 0.1, 0.25):
        if delay:
            sleep(delay)
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if delay == 0.25:
                raise


def _exact_keys(name, value, keys):
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} fields are invalid")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid JSON constant {value!r}")


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("corpus source timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("corpus source timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("corpus source timestamp needs a timezone")
    return parsed


def _cancel(callback):
    if callback():
        raise CorpusInstallCancelled("starter corpus installation paused safely")


__all__ = (
    "CorpusAsset",
    "CorpusInstallCancelled",
    "CorpusInstallError",
    "CorpusInstallManifest",
    "DraftCorpusInstaller",
    "SecureHttpTransport",
    "resolve_starter_manifest",
)
