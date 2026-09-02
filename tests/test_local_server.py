import http.client
from io import BytesIO
import json
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from zipfile import ZipFile

from tests.test_app_service import payload
from tests.test_engine_bundle import engine_bundle
from tests.test_surrogate_disclosure import surrogate_bundle
from trade_snapshot.extension_bridge import SESSION_TOKEN_HEADER, V1_CAPABILITIES
from trade_snapshot.local_server import create_local_server


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.server = create_local_server(self.directory.name)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(
        self, method, path, *, value=None, token=True, host=None, extra_headers=None
    ):
        headers = dict(extra_headers or {})
        body = None
        if token:
            headers["X-FTE-Token"] = self.server.app_token
        if host is not None:
            headers["Host"] = host
        if value is not None:
            body = json.dumps(value, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            body = ""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), raw)
        connection.close()
        return result

    def test_embeds_private_token_and_rejects_missing_token_and_foreign_host(self):
        status, headers, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.server.app_token.encode(), body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, _, body = self.request("GET", "/api/health", token=False)
        self.assertEqual(status, 403)
        self.assertIn(b"missing local application token", body)

        status, _, _ = self.request(
            "GET", "/api/health", host="attacker.example", token=True
        )
        self.assertEqual(status, 400)

    def test_bundle_import_and_candidate_estimate_routes(self):
        bundle = engine_bundle()
        status, _, raw = self.request(
            "POST", "/api/bundles/import", value=bundle.to_record()
        )
        summary = json.loads(raw)
        self.assertEqual(status, 201)
        self.assertEqual(summary["bundle_id"], bundle.bundle_id)

        status, _, raw = self.request(
            "POST", "/api/searches/estimate", value=payload(bundle.bundle_id)
        )
        estimate = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(estimate["candidate_count"], 4)

        filtered = payload(bundle.bundle_id)
        filtered["outgoing_filter"] = {
            "player_ids": ["p1"],
            "player_mode": "only",
            "positions": [],
            "position_mode": None,
        }
        filtered["incoming_filter"] = {
            "player_ids": ["q1"],
            "player_mode": "include",
            "positions": [],
            "position_mode": None,
        }
        status, _, raw = self.request(
            "POST", "/api/searches/estimate", value=filtered
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["candidate_count"], 1)
        self.assertEqual(
            {
                player["player_id"]
                for team in summary["teams"]
                for player in team["players"]
            },
            {"p1", "p2", "q1", "q2"},
        )

    def test_static_assets_are_served_without_exposing_filesystem_paths(self):
        status, headers, body = self.request("GET", "/app.js", token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"startSearch", body)
        self.assertIn(b"power_methodology_status", body)
        self.assertIn(b"combinations counted exactly", body)
        self.assertIn(b"allow_surrogate_power", body)
        self.assertIn(b"SURROGATE / APPROXIMATE POWER", body)
        self.assertIn(b'addEventListener("change", changeBundle)', body)
        self.assertIn(b'$("resultsPanel").classList.add("hidden")', body)
        self.assertIn(b'$("bundleSelect").disabled = true', body)
        self.assertIn(b"packageFilterPayload", body)
        self.assertIn(b"outgoing_filter", body)
        self.assertIn(b"same other team", body)
        self.assertIn(b'trade_format: format', body)
        self.assertIn(b'format === "three_team"', body)
        self.assertIn(b"candidate_count_text", body)
        self.assertIn(b"threeTeamEstimateSignature", body)
        self.assertIn(b"improve all three playoff chances", body)
        self.assertIn(b"free_agent_allocation_policy", body)
        self.assertIn(b"Count this specific three-team search", body)
        self.assertNotIn(b"Count this exact three-team search", body)
        self.assertNotIn(b"expected_team_count", body)
        self.assertNotIn(b"exact combinations across", body)
        self.assertNotIn(self.directory.name.encode(), body)
        status, _, page = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Power evidence", page)
        self.assertIn(b"allowSurrogatePower", page)
        self.assertIn(b"never be labeled exact", page)
        self.assertIn(b"League size, every team, and every roster are detected", page)
        self.assertIn(b"Connect extension", page)
        self.assertIn(b"never exports your cookies", page)
        self.assertIn(b'id="extensionPairCode"', page)
        self.assertIn(b"four-character code matches", page)
        self.assertIn(b"Pairing code ends in", body)
        self.assertIn(b"offer.pair_code.slice(-4)", body)
        self.assertIn(b'<meta name="color-scheme" content="dark">', page)
        self.assertIn(b"Package contents", page)
        self.assertIn(b"Exactly the selected players", page)
        self.assertIn(b'id="twoTeamFormat"', page)
        self.assertIn(b'id="threeTeamFormat"', page)
        self.assertIn(b'id="partnerTeamA"', page)
        self.assertIn(b'id="partnerTeamB"', page)
        self.assertIn(b"Search one selected three-team agreement", page)
        self.assertNotIn(b"Search one exact three-team agreement", page)
        self.assertIn(b'id="minOutgoingLabel"', page)
        self.assertIn(b'id="noDropsLabel"', page)
        self.assertIn(b'id="freeAgentAllocationPolicy"', page)
        self.assertIn(b"always extrapolated", page)
        self.assertIn(b"Every result requires all three teams", page)
        self.assertIn(b'id="resultsHeaderRow"', page)
        self.assertLess(page.index(b'/three_way_ui.js'), page.index(b'/app.js'))
        self.assertNotIn(b'id="expectedTeamCount"', page)

        status, _, stylesheet = self.request("GET", "/styles.css", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"color-scheme: dark", stylesheet)
        self.assertIn(b"--canvas: #071014", stylesheet)
        self.assertIn(b".trade-format-options", stylesheet)
        self.assertIn(b".team-impact-cell", stylesheet)
        self.assertIn(b".roster-adjustment-warning", stylesheet)
        self.assertNotIn(b"color-scheme: light", stylesheet)

        status, headers, three_way = self.request("GET", "/three_way_ui.js", token=False)
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"window.ThreeWayUi", three_way)
        self.assertIn(b"BigInt(raw)", three_way)
        self.assertIn(b"row.transfers", three_way)
        self.assertIn(b"row.team_impacts", three_way)
        self.assertIn(b"row.all_teams_gain", three_way)
        self.assertIn(b"Minimum each team sends", three_way)
        self.assertIn(b"Do not force any team", three_way)
        self.assertIn(b"three_team_free_agent_allocation_policy", three_way)

        status, headers, extension = self.request(
            "GET", "/browser-extension.zip", token=False
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        with ZipFile(BytesIO(extension)) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            popup_css = archive.read("popup/popup.css")
            popup_page = archive.read("popup/popup.html")
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertIn("service_worker.js", names)
        self.assertIn("popup/popup.html", names)
        self.assertNotIn("__init__.py", names)
        self.assertFalse(any(name.endswith((".py", ".pyc", ".pyo")) for name in names))
        self.assertIn(b"color-scheme: dark", popup_css)
        self.assertNotIn(b"color-scheme: light", popup_css)
        self.assertIn(b'<meta name="color-scheme" content="dark">', popup_page)

    def test_surrogate_api_summary_and_search_consent_are_explicit(self):
        bundle = surrogate_bundle()
        status, _, raw = self.request(
            "POST", "/api/bundles/import", value=bundle.to_record()
        )
        summary = json.loads(raw)
        self.assertEqual(status, 201)
        self.assertEqual(summary["power_engine_mode"], "surrogate")

        denied = payload(bundle.bundle_id)
        status, _, raw = self.request(
            "POST", "/api/searches/estimate", value=denied
        )
        self.assertEqual(status, 400)
        self.assertIn(b"SURROGATE", raw)

        accepted = payload(bundle.bundle_id)
        accepted["allow_surrogate_power"] = True
        status, _, raw = self.request(
            "POST", "/api/searches/estimate", value=accepted
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["candidate_count"], 4)

    def test_authenticated_browser_close_stops_lifecycle_server(self):
        self.server.start_browser_lifecycle(
            launch_timeout=2, idle_timeout=2, close_grace=0.05
        )
        status, _, _ = self.request("POST", "/api/session/ping")
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/session/close", token=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/session/close")
        self.assertEqual(status, 200)
        self.thread.join(timeout=1)
        self.assertFalse(self.thread.is_alive())

    def test_extension_pair_command_and_disconnect_routes_use_separate_tokens(self):
        status, _, raw = self.request(
            "POST", "/api/browser-extension/pairing"
        )
        self.assertEqual(status, 201)
        offer = json.loads(raw)
        self.assertNotIn("session_token", offer)

        status, _, raw = self.request(
            "POST",
            "/api/browser-extension/v1/pair",
            token=False,
            value={
                "pair_code": offer["pair_code"],
                "protocol_version": 1,
                "capabilities": list(V1_CAPABILITIES),
                "extension_version": "0.1.0",
            },
        )
        self.assertEqual(status, 200)
        session_token = json.loads(raw)["session_token"]
        extension_headers = {SESSION_TOKEN_HEADER: session_token}

        completed = {}

        def execute():
            completed["value"] = self.server.extension_bridge.execute(
                "session.open", {}, 2, None
            )

        worker = Thread(target=execute, daemon=True)
        worker.start()
        status, _, raw = self.request(
            "POST",
            "/api/browser-extension/v1/poll",
            token=False,
            value={"wait_seconds": 1},
            extra_headers=extension_headers,
        )
        self.assertEqual(status, 200)
        command = json.loads(raw)
        self.assertEqual(command["op"], "session.open")
        status, _, _ = self.request(
            "POST",
            "/api/browser-extension/v1/result",
            token=False,
            value={"command_id": command["command_id"], "result": {"opened": True}},
            extra_headers=extension_headers,
        )
        self.assertEqual(status, 200)
        worker.join(timeout=2)
        self.assertEqual(completed["value"], {"opened": True})

        status, _, raw = self.request("GET", "/api/browser-extension/status")
        public = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(public["state"], "paired")
        self.assertNotIn("session_token", public)
        self.assertNotIn("pair_code", public)

        status, _, _ = self.request(
            "POST",
            "/api/browser-extension/v1/disconnect",
            token=False,
            value={},
            extra_headers=extension_headers,
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
