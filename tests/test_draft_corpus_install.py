import io
import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from tests.draft_fixtures import small_historical_corpus
from trade_snapshot.draft_corpus_builder import StarterCorpusBuild
from trade_snapshot.draft_corpus_install import (
    CorpusAsset,
    CorpusInstallCancelled,
    CorpusInstallError,
    CorpusInstallManifest,
    DraftCorpusInstaller,
)


class _Response:
    def __init__(self, payload, url, status=200, headers=None, after_first_read=None):
        self._source = io.BytesIO(payload)
        self._url = url
        self._status = status
        self.headers = headers or {"Content-Length": str(len(payload)), "ETag": '"v1"'}
        self._after_first_read = after_first_read
        self._read_once = False

    def read(self, amount=-1):
        chunk = self._source.read(amount)
        if chunk and not self._read_once:
            self._read_once = True
            if self._after_first_read is not None:
                self._after_first_read()
        return chunk

    def geturl(self):
        return self._url

    def getcode(self):
        return self._status

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


class _Transport:
    def __init__(self, payloads, interrupt_url=None, interrupted=None):
        self.payloads = payloads
        self.interrupt_url = interrupt_url
        self.interrupted = interrupted
        self.calls = []
        self.did_interrupt = False

    def open(self, request, timeout):
        self.calls.append((request.full_url, dict(request.header_items()), timeout))
        payload = self.payloads[request.full_url]
        range_header = request.get_header("Range")
        start = (
            int(range_header.removeprefix("bytes=").removesuffix("-"))
            if range_header
            else 0
        )
        body = payload[start:]
        status = 206 if start else 200
        headers = {"Content-Length": str(len(body)), "ETag": '"v1"'}
        if start:
            headers["Content-Range"] = (
                f"bytes {start}-{len(payload) - 1}/{len(payload)}"
            )
        callback = None
        if request.full_url == self.interrupt_url and not self.did_interrupt:
            self.did_interrupt = True
            callback = self.interrupted.set
        return _Response(body, request.full_url, status, headers, callback)


class DraftCorpusInstallerTests(unittest.TestCase):
    def test_rejects_non_allowlisted_or_non_https_sources(self):
        for url in ("http://github.com/file", "https://evil.example/file"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "allowlist"):
                CorpusAsset("schedule", "schedule", None, "file.csv.gz", url, 100)

    def test_interrupted_download_resumes_and_completed_install_is_idempotent(self):
        manifest, payloads = _manifest_and_payloads()
        interrupted = Event()
        first_url = next(
            row.url for row in manifest.assets if row.key == "ffc_adp:2025"
        )
        transport = _Transport(payloads, first_url, interrupted)
        corpus = small_historical_corpus()
        build = StarterCorpusBuild(
            corpus,
            "ready_with_gaps",
            {"gap_count": 2},
            len(json.dumps(corpus.to_record())),
        )
        with (
            TemporaryDirectory() as directory,
            patch(
                "trade_snapshot.draft_corpus_install.build_starter_corpus",
                return_value=build,
            ),
        ):
            root = Path(directory)
            installer = DraftCorpusInstaller(
                root, transport=transport, clock=lambda: 10.0, sleeper=lambda _: None
            )
            _cache_manifest(installer, manifest)
            with self.assertRaises(CorpusInstallCancelled):
                installer.install(should_cancel=interrupted.is_set)
            state = installer.recoverable_state()
            self.assertEqual(state["status"], "paused")
            self.assertTrue(
                (
                    installer.root
                    / manifest.manifest_id
                    / "downloads"
                    / "ffc_adp_2025.json.part"
                ).is_file()
            )

            interrupted.clear()
            receipt = installer.install()
            calls_after_install = len(transport.calls)
            repeated = installer.install()

            self.assertEqual(receipt, repeated)
            self.assertEqual(receipt["status_label"], "Ready with gaps")
            self.assertEqual(receipt["corpus_id"], corpus.corpus_id)
            self.assertEqual(len(transport.calls), calls_after_install)
            resumed = [
                headers
                for url, headers, _ in transport.calls
                if url == first_url and "Range" in headers
            ]
            self.assertEqual(len(resumed), 1)
            self.assertEqual(installer.store.load_corpus(corpus.corpus_id), corpus)

    def test_digest_mismatch_fails_without_promoting_part_file(self):
        manifest, payloads = _manifest_and_payloads(corrupt_digest=True)
        transport = _Transport(payloads)
        with TemporaryDirectory() as directory:
            installer = DraftCorpusInstaller(directory, transport=transport)
            _cache_manifest(installer, manifest)
            with self.assertRaisesRegex(CorpusInstallError, "SHA-256"):
                installer.install()
            state = installer.recoverable_state()
            self.assertEqual(state["status"], "failed")
            download = (
                installer.root
                / manifest.manifest_id
                / "downloads"
                / "ffc_adp_2025.json"
            )
            self.assertFalse(download.exists())
            self.assertTrue(download.with_name(download.name + ".part").is_file())


def _manifest_and_payloads(*, corrupt_digest=False):
    specs = (
        ("schedule", "schedule", None, "games.csv.gz"),
        ("ffc_adp:2025", "ffc_adp", 2025, "ffc_adp_2025.json"),
        ("player_stats:2025", "player_stats", 2025, "stats_player_week_2025.csv.gz"),
        ("team_stats:2025", "team_stats", 2025, "stats_team_week_2025.csv.gz"),
        ("roster:2024", "roster", 2024, "roster_weekly_2024.csv.gz"),
        ("roster:2025", "roster", 2025, "roster_weekly_2025.csv.gz"),
    )
    assets = []
    payloads = {}
    for index, (key, role, season, filename) in enumerate(specs):
        payload = bytes([65 + index]) * (300_000 if role == "ffc_adp" else 31 + index)
        url = f"https://github.com/nflverse/nflverse-data/releases/download/test/{filename}"
        payloads[url] = payload
        digest = sha256(payload).hexdigest()
        if corrupt_digest and role == "ffc_adp":
            digest = "0" * 64
        assets.append(
            CorpusAsset(
                key,
                role,
                season,
                filename,
                url,
                max(len(payload), 512_000),
                len(payload),
                digest,
                None if role == "ffc_adp" else "2026-01-01T00:00:00+00:00",
            )
        )
    return CorpusInstallManifest((2025,), tuple(assets)), payloads


def _cache_manifest(installer, manifest):
    path = installer.root / "starter-manifest-v1.json"
    path.write_text(json.dumps(manifest.to_record()), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
