import json
import unittest
from urllib.parse import parse_qs, urlparse

from trade_snapshot.fantasypros import FantasyProsError, fetch_datasets


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {
            "Content-Type": "application/json",
            "ETag": '"fixture"',
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._body


class RecordingOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        return FakeResponse(next(self.responses))


class FantasyProsClientTests(unittest.TestCase):
    def test_fetches_ecr_projections_and_crosswalk_without_key_in_urls(self):
        secret = "do-not-persist-this-key"
        opener = RecordingOpener(
            [
                {
                    "year": "2026",
                    "week": "4",
                    "last_updated_ts": 1_787_000_000,
                    "players": [],
                },
                {"season": 2026, "week": 4, "players": []},
                {"sport": "NFL", "season": 2026, "week": 4, "players": []},
            ]
        )

        datasets = fetch_datasets(
            api_key=secret,
            season=2026,
            week=4,
            scoring="PPR",
            opener=opener,
            timeout=12,
        )

        self.assertEqual([dataset.name for dataset in datasets], ["ecr", "projections", "players"])
        self.assertEqual(len(opener.calls), 3)
        urls = [call[0].full_url for call in opener.calls]
        self.assertNotIn(secret, "\n".join(urls))
        self.assertTrue(all(call[0].get_header("X-api-key") == secret for call in opener.calls))
        self.assertTrue(all(call[1] == 12 for call in opener.calls))

        ecr_query = parse_qs(urlparse(urls[0]).query)
        self.assertEqual(ecr_query["position"], ["ALL"])
        self.assertEqual(ecr_query["scoring"], ["PPR"])
        self.assertEqual(ecr_query["type"], ["ROS"])
        self.assertEqual(ecr_query["week"], ["4"])

        projections_query = parse_qs(urlparse(urls[1]).query)
        self.assertEqual(projections_query["position"], ["ALL"])
        self.assertEqual(projections_query["week"], ["4"])
        self.assertEqual(projections_query["ros"], ["true"])

        players_query = parse_qs(urlparse(urls[2]).query)
        self.assertEqual(players_query["ecr"], ["included"])
        self.assertEqual(players_query["external_ids"], ["espn:yahoo"])

    def test_invalid_json_failure_does_not_echo_api_key(self):
        secret = "still-secret"

        class InvalidJsonResponse(FakeResponse):
            def __init__(self):
                self._body = b"not json"
                self.headers = {}

        def opener(request, *, timeout):
            return InvalidJsonResponse()

        with self.assertRaises(FantasyProsError) as raised:
            fetch_datasets(
                api_key=secret,
                season=2026,
                week=4,
                scoring="PPR",
                opener=opener,
            )

        self.assertNotIn(secret, str(raised.exception))

    def test_rejects_error_body_and_wrong_period(self):
        error_opener = RecordingOpener([{"error": "quota"}])
        with self.assertRaisesRegex(FantasyProsError, "unexpected JSON shape"):
            fetch_datasets(
                api_key="secret",
                season=2026,
                week=4,
                scoring="PPR",
                opener=error_opener,
            )

        wrong_period = RecordingOpener(
            [{"year": 2025, "week": 4, "players": []}]
        )
        with self.assertRaisesRegex(FantasyProsError, "wrong season"):
            fetch_datasets(
                api_key="secret",
                season=2026,
                week=4,
                scoring="PPR",
                opener=wrong_period,
            )


if __name__ == "__main__":
    unittest.main()
