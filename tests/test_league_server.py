import http.client
import json
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import quote

from tests.test_engine_bundle import engine_bundle
from trade_snapshot.local_server import create_local_server
from trade_snapshot.weekly_collection import WeeklyCollectionRequest


def profile_payload(name="Home League"):
    return {
        "name": name,
        "season": 2026,
        "scoring": "PPR",
        "host_league_url": "",
        "yahoo_projection_league_url": "",
    }


class LeagueServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.server = create_local_server(self.directory.name)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method, path, *, value=None, token=True):
        headers = {}
        if token:
            headers["X-FTE-Token"] = self.server.app_token
        body = None
        if value is not None:
            body = json.dumps(value, separators=(",", ":"))
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            body = ""
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def json_request(self, method, path, *, value=None, token=True):
        status, headers, raw = self.request(
            method, path, value=value, token=token
        )
        return status, headers, json.loads(raw)

    def create_profile(self, name="Home League"):
        status, _, record = self.json_request(
            "POST", "/api/leagues", value=profile_payload(name)
        )
        self.assertEqual(status, 201)
        return record

    def test_serves_workspace_scripts_without_an_application_token(self):
        expectations = {
            "/league_ui.js": b"window.LeagueUi",
            "/progress_ui.js": b"window.ProgressUi",
            "/results_workbench.js": b"window.ResultsWorkbench",
        }
        for path, marker in expectations.items():
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path, token=False)
                self.assertEqual(status, 200)
                self.assertIn("text/javascript", headers["Content-Type"])
                self.assertIn(marker, body)

    def test_profile_crud_and_strict_cursor_pagination(self):
        first = self.create_profile("First")
        second = self.create_profile("Second")

        status, _, page = self.json_request(
            "GET", "/api/leagues?limit=1&include_archived=false"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["profiles"]), 1)
        self.assertIsInstance(page["next_cursor"], str)
        cursor = quote(page["next_cursor"], safe="")
        status, _, next_page = self.json_request(
            "GET", f"/api/leagues?limit=1&include_archived=false&cursor={cursor}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(next_page["profiles"]), 1)
        self.assertEqual(
            {page["profiles"][0]["profile_id"], next_page["profiles"][0]["profile_id"]},
            {first["profile_id"], second["profile_id"]},
        )

        profile_id = first["profile_id"]
        status, _, updated = self.json_request(
            "POST", f"/api/leagues/{profile_id}", value={"name": "Renamed"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["name"], "Renamed")
        status, _, archived = self.json_request(
            "POST", f"/api/leagues/{profile_id}/archive"
        )
        self.assertEqual(status, 200)
        self.assertTrue(archived["archived"])
        status, _, visible = self.json_request("GET", "/api/leagues")
        self.assertEqual(status, 200)
        visible_ids = {row["profile_id"] for row in visible["profiles"]}
        self.assertNotIn(profile_id, visible_ids)
        status, _, restored = self.json_request(
            "POST", f"/api/leagues/{profile_id}/restore"
        )
        self.assertEqual(status, 200)
        self.assertFalse(restored["archived"])

        bad_queries = (
            "limit=1&limit=2",
            "include_archived=1",
            "limit=0",
            "cursor=",
            "unknown=value",
        )
        for query in bad_queries:
            with self.subTest(query=query):
                status, _, error = self.json_request(
                    "GET", f"/api/leagues?{query}"
                )
                self.assertEqual(status, 400)
                self.assertIn("error", error)
        status, _, _ = self.json_request("GET", "/api/leagues", token=False)
        self.assertEqual(status, 403)

    def test_profile_bundle_import_detail_catalog_and_saved_team(self):
        profile = self.create_profile()
        profile_id = profile["profile_id"]
        bundle = engine_bundle()
        status, _, imported = self.json_request(
            "POST",
            f"/api/leagues/{profile_id}/bundles/import",
            value=bundle.to_record(),
        )
        self.assertEqual(status, 201)
        self.assertEqual(imported["bundle_id"], bundle.bundle_id)

        status, _, catalog = self.json_request(
            "GET", f"/api/leagues/{profile_id}/bundles"
        )
        self.assertEqual(status, 200)
        self.assertTrue(catalog["readiness"]["ready"])
        self.assertFalse(catalog["readiness"]["collection_available"])
        self.assertEqual(catalog["bundles"][0]["bundle_id"], bundle.bundle_id)
        status, _, detail = self.json_request(
            "GET", f"/api/bundles/{bundle.bundle_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["bundle_id"], bundle.bundle_id)
        self.assertEqual(
            {team["team_id"] for team in detail["teams"]}, {"primary", "other"}
        )

        status, _, updated = self.json_request(
            "POST",
            f"/api/leagues/{profile_id}/team",
            value={"bundle_id": bundle.bundle_id, "team_id": "primary"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["my_team_id"], "primary")

    def test_unassigned_import_can_be_assigned_to_a_saved_league(self):
        profile = self.create_profile()
        bundle = engine_bundle()
        status, _, _ = self.json_request(
            "POST", "/api/leagues/unassigned/bundles/import", value=bundle.to_record()
        )
        self.assertEqual(status, 201)
        status, _, catalog = self.json_request(
            "GET", "/api/leagues/unassigned/bundles"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["bundle_id"] for row in catalog["bundles"]],
            [bundle.bundle_id],
        )
        self.assertFalse(catalog["readiness"]["collection_available"])

        status, _, association = self.json_request(
            "POST",
            f"/api/leagues/{profile['profile_id']}/bundles/{bundle.bundle_id}/assign",
        )
        self.assertEqual(status, 200)
        self.assertEqual(association["profile_id"], profile["profile_id"])
        status, _, catalog = self.json_request(
            "GET", "/api/leagues/unassigned/bundles"
        )
        self.assertEqual(status, 200)
        self.assertEqual(catalog["bundles"], [])

    def test_weekly_collection_dispatches_profile_and_legacy_payloads(self):
        profile_id = "league_" + "a" * 32
        collection_payload = {
            "league_profile_id": profile_id,
            "week": 7,
            "include_future_weekly": True,
            "allow_surrogate_power": False,
        }
        with patch.object(
            self.server.app_service,
            "start_profile_weekly_collection",
            return_value={"job_id": "profile-job", "status": "queued"},
        ) as start_profile:
            status, _, started = self.json_request(
                "POST", "/api/weekly-collections", value=collection_payload
            )
        self.assertEqual(status, 202)
        self.assertEqual(started["job_id"], "profile-job")
        start_profile.assert_called_once_with(
            profile_id,
            {
                "week": 7,
                "include_future_weekly": True,
                "allow_surrogate_power": False,
            },
        )

        legacy_payload = {
            "season": 2026,
            "week": 7,
            "scoring": "PPR",
            "include_future_weekly": False,
        }
        with patch.object(
            self.server.app_service,
            "start_weekly_collection",
            return_value={"job_id": "legacy-job", "status": "queued"},
        ) as start_legacy:
            status, _, started = self.json_request(
                "POST", "/api/weekly-collections", value=legacy_payload
            )
        self.assertEqual(status, 202)
        self.assertEqual(started["job_id"], "legacy-job")
        request = start_legacy.call_args.args[0]
        self.assertIsInstance(request, WeeklyCollectionRequest)
        self.assertEqual((request.season, request.week), (2026, 7))


if __name__ == "__main__":
    unittest.main()
