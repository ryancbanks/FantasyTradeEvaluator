from dataclasses import replace
import http.client
import json
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from tests.draft_fixtures import small_draft_config, small_historical_corpus
import trade_snapshot.local_server as local_server
from trade_snapshot.draft_espn_live import EspnDraftObservation
from trade_snapshot.draft_history import DraftPlayerBoard
from trade_snapshot.draft_training import EvolutionConfig
from trade_snapshot.local_server import create_local_server


class _EspnDraftAdapterStub:
    def __init__(self, observation):
        self.observation = observation
        self.calls = []

    def poll(self, **kwargs):
        self.calls.append(kwargs)
        return self.observation


class DraftLocalServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.server = create_local_server(self.directory.name)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method, path, value=None):
        headers = {"X-FTE-Token": self.server.app_token}
        body = "" if method == "POST" and value is None else None
        if value is not None:
            body = json.dumps(value, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse(); raw = response.read(); headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, json.loads(raw) if "application/json" in headers.get("Content-Type", "") and "attachment" not in headers.get("Content-Disposition", "") else raw

    def wait(self, job_id):
        for _ in range(500):
            status, _, job = self.request("GET", f"/api/draft/jobs/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("job timeout")

    def test_training_model_board_assistant_and_benchmark_routes(self):
        corpus = small_historical_corpus(); config = small_draft_config()
        status, _, imported = self.request("POST", "/api/draft/corpora/import", corpus.to_record())
        self.assertEqual(status, 201); self.assertEqual(imported["corpus_id"], corpus.corpus_id)
        evolution = EvolutionConfig(4, 1, 1, .25, .1, 1000, 4, (2025,), 3)
        request = {"corpus_id": corpus.corpus_id, "league_config": config.to_record(), "evolution_config": evolution.to_record()}
        self.assertEqual(self.request("POST", "/api/draft/trainings/estimate", request)[0], 200)
        status, _, job = self.request("POST", "/api/draft/trainings", request)
        self.assertEqual(status, 202); self.assertEqual(self.wait(job["job_id"])["status"], "complete")
        status, _, result = self.request("GET", f"/api/draft/jobs/{job['job_id']}/result")
        self.assertEqual(status, 200); model_id = result["model"]["model_id"]
        status, _, promoted = self.request(
            "POST", f"/api/draft/checkpoints/{job['job_id']}/promote"
        )
        self.assertEqual(status, 200)
        self.assertEqual(promoted["model_id"], model_id)
        status, headers, body = self.request("GET", f"/api/draft/models/{model_id}/export")
        self.assertEqual(status, 200); self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"fantasy_draft_model", body)

        season = corpus.seasons[0]
        board = DraftPlayerBoard(
            2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
            tuple(replace(row, player_id=f"now-{row.player_id}", actual_weeks=()) for row in season.players),
        )
        self.assertEqual(self.request("POST", "/api/draft/boards/import", board.to_record())[0], 201)
        status, _, assistant = self.request("POST", "/api/draft/assistants", {
            "model_id": model_id, "board_id": board.board_id,
            "user_drafter_number": 1, "strategy": "none",
        })
        self.assertEqual(status, 201); session_id = assistant["session_id"]
        player_id = assistant["recommendations"][0]["player_id"]
        self.assertEqual(self.request("POST", f"/api/draft/assistants/{session_id}/picks", {
            "player_id": player_id, "drafter_number": 1,
        })[0], 200)
        self.assertEqual(self.request("GET", f"/api/draft/assistants/{session_id}/players")[0], 200)
        self.assertEqual(self.request("POST", f"/api/draft/assistants/{session_id}/undo")[0], 200)

        espn = _EspnDraftAdapterStub(EspnDraftObservation(
            "123", 2026, ("10", "20", "30", "40"),
            ((1, player_id),), False, True,
        ))
        self.server.app_service.draft_lab._espn_draft_adapter = espn
        status, _, synced = self.request(
            "POST", f"/api/draft/assistants/{session_id}/espn-sync",
            {"league_id": "123", "season": 2026},
        )
        self.assertEqual(status, 200)
        self.assertEqual(synced["picks"][0]["player_id"], player_id)
        self.assertEqual(synced["live_sync"]["provider"], "espn")
        self.assertEqual(synced["draft_binding"]["league_id"], "123")
        self.assertEqual(espn.calls[0]["league_id"], "123")
        reopened = self.request("GET", f"/api/draft/assistants/{session_id}")[2]
        self.assertEqual(reopened["draft_binding"], synced["draft_binding"])
        catalog = self.request("GET", "/api/draft/catalog")[2]
        self.assertEqual(
            catalog["assistant_sessions"][0]["draft_binding"],
            synced["draft_binding"],
        )
        self.assertEqual(self.request(
            "POST", f"/api/draft/assistants/{session_id}/espn-sync",
            {"league_id": "123", "season": 2026, "cookie": "forbidden"},
        )[0], 400)

        status, _, benchmark = self.request("POST", "/api/draft/benchmarks", {
            "model_id": model_id, "trials": 2, "seed": 7,
            "candidate_window": 4, "evaluation_years": [2025],
        })
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(benchmark["job_id"])["status"], "complete")

    def test_catalog_and_draft_routes_require_the_private_token(self):
        status, _, catalog = self.request("GET", "/api/draft/catalog")
        self.assertEqual(status, 200)
        self.assertIn("league_presets", catalog)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        connection.request("GET", "/api/draft/catalog")
        response = connection.getresponse(); response.read(); connection.close()
        self.assertEqual(response.status, 403)

    def test_current_board_upload_uses_the_bounded_draft_data_limit(self):
        original = local_server._MAX_DRAFT_DATA_BYTES
        local_server._MAX_DRAFT_DATA_BYTES = 32
        try:
            status, _, response = self.request(
                "POST",
                "/api/draft/boards/import",
                {"padding": "x" * 64},
            )
        finally:
            local_server._MAX_DRAFT_DATA_BYTES = original

        self.assertEqual(status, 400)
        self.assertIn("request body is too large", response["error"])


if __name__ == "__main__":
    unittest.main()
