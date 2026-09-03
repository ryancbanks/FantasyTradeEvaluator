import http.client
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from tests.test_app_service import payload, wait_for_job
from tests.test_engine_bundle import engine_bundle
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest
from trade_snapshot.engine_bundle import load_engine_bundle, save_engine_bundle
from trade_snapshot.local_server import create_local_server
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeam,
    HistoryTeamRoster,
    LeagueHistoryCapture,
    LeagueHistoryStore,
    make_league_key,
)
from trade_snapshot.weekly_collection import (
    LEAGUE_HISTORY_FILENAME,
    WeeklyCollectionError,
    WeeklyCollectionJobs,
    WeeklyCollectionPublication,
    WeeklyCollectionProgress,
    WeeklyCollectionRequest,
    WeeklyCollectionStage,
)


def request_payload(**changes):
    value = {
        "season": 2026,
        "week": 1,
        "scoring": "PPR",
        "host_league_url": "https://fantasy.espn.com/football/league?leagueId=123",
        "yahoo_projection_league_url": (
            "https://football.fantasysports.yahoo.com/f1/456/players"
        ),
        "include_future_weekly": True,
    }
    value.update(changes)
    return value


def valid_request(**changes):
    return WeeklyCollectionRequest.from_payload(request_payload(**changes))


def wait_for_collection(operation, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = operation(job_id)
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(0.01)
    raise AssertionError("weekly collection did not finish")


class SuccessfulWorkflow:
    def __init__(self):
        self.calls = []

    def __call__(self, request, *, data_directory, progress, cancelled):
        self.calls.append((request, data_directory))
        progress(
            WeeklyCollectionProgress(
                WeeklyCollectionStage.COLLECTING_FANTASYPROS,
                0.4,
                "Collecting FantasyPros ECR",
            )
        )
        progress(
            WeeklyCollectionProgress(
                WeeklyCollectionStage.BUILDING,
                0.9,
                "Building the local engine",
            )
        )
        return engine_bundle()


class InteractiveWorkflow(SuccessfulWorkflow):
    def __init__(self):
        super().__init__()
        self.sign_in_gate = self
        self.pending = None
        self.confirmed = []

    def __call__(self, request, *, data_directory, progress, cancelled):
        self.pending = "fantasypros"
        while self.pending is not None and not cancelled():
            time.sleep(0.005)
        return super().__call__(
            request,
            data_directory=data_directory,
            progress=progress,
            cancelled=cancelled,
        )

    def status(self):
        return {
            "pending_provider": self.pending,
            "confirmed_providers": list(self.confirmed),
        }

    def confirm(self):
        if self.pending is None:
            raise ValueError("no provider sign-in is waiting for confirmation")
        provider = self.pending
        self.pending = None
        self.confirmed.append(provider)
        return provider


class HistoryWorkflow(SuccessfulWorkflow):
    def __call__(self, request, *, data_directory, progress, cancelled):
        bundle = super().__call__(
            request,
            data_directory=data_directory,
            progress=progress,
            cancelled=cancelled,
        )
        return history_publication(bundle)


def history_publication(bundle):
    captured_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    league_key = make_league_key("espn", "test-private-league")
    names = {team.team_id: team.name for team in bundle.state.teams}
    capture = LeagueHistoryCapture(
        league_key=league_key,
        season=bundle.state.season,
        captured_at=captured_at,
        coverage_start=captured_at,
        coverage_end=captured_at,
        transaction_history_complete=True,
        roster_complete=True,
        lineup_complete=True,
        teams=tuple(HistoryTeam(team_id, names[team_id]) for team_id in names),
        transactions=(),
        rosters=tuple(
            HistoryTeamRoster(
                roster.team_id,
                tuple(
                    HistoryRosterPlayer(player_id, "BENCH")
                    for player_id in roster.player_ids
                ),
            )
            for roster in bundle.rosters
        ),
    )
    return WeeklyCollectionPublication(
        bundle,
        capture,
        HistoryBundleBinding(
            league_key,
            bundle.state.season,
            bundle.bundle_id,
            captured_at,
        ),
    )


class WeeklyCollectionRequestTests(unittest.TestCase):
    def test_direct_request_defaults_to_current_week_plus_ros(self):
        request = WeeklyCollectionRequest(2026, 1, "PPR")
        self.assertFalse(request.include_future_weekly)

    def test_accepts_purpose_specific_urls_and_auto_discovers_team_count(self):
        record = request_payload()
        record["yahoo_projection_league_url"] = (
            "https://football.fantasysports.yahoo.com/f1/456/players"
        )
        request = WeeklyCollectionRequest.from_payload(record)
        self.assertIsNone(request.expected_team_count)
        self.assertEqual(request.scoring, "PPR")
        self.assertFalse(request.allow_surrogate_power)
        self.assertEqual(valid_request(expected_team_count=12).expected_team_count, 12)
        with self.assertRaisesRegex(ValueError, "2 through 32"):
            valid_request(expected_team_count=1)
        self.assertTrue(
            valid_request(allow_surrogate_power=True).allow_surrogate_power
        )
        with self.assertRaisesRegex(ValueError, "boolean"):
            valid_request(allow_surrogate_power="yes")

    def test_normalizes_host_pages_to_minimal_purpose_specific_urls(self):
        request = valid_request(
            host_league_url=(
                "https://fantasy.espn.com/football/league/standings"
                "?leagueId=123&view=standings"
            ),
            yahoo_projection_league_url=(
                "https://football.fantasysports.yahoo.com/f1/456/players/"
            ),
        )
        self.assertEqual(
            request.host_league_url,
            "https://fantasy.espn.com/football/league?leagueId=123",
        )
        self.assertEqual(
            request.yahoo_projection_league_url,
            "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
        )

    def test_accepts_current_espn_pages_and_discards_unneeded_query_state(self):
        pages = (
            "https://fantasy.espn.com/football?leagueId=123",
            "https://fantasy.espn.com/football/league?leagueId=123",
            "https://fantasy.espn.com/football/team?leagueId=123&teamId=9",
            "https://fantasy.espn.com/football/league/standings?leagueId=123",
            "https://fantasy.espn.com/football/league/scoreboard?"
            "scoringPeriodId=4&LeagueID=123&view=mMatchup",
            "https://fantasy.espn.com/football/league/schedule?leagueId=123",
            "https://fantasy.espn.com/football/league/rosters?leagueId=123",
            "https://fantasy.espn.com/football/league/settings?leagueId=123",
            "https://fantasy.espn.com/football/league/transactions?leagueId=123",
            "https://fantasy.espn.com/football/players/add?leagueId=123",
            "https://fantasy.espn.com/football/league/members?leagueId=123",
            "https://fantasy.espn.com/football/invite/accept?leagueId=123",
            "https://www.espn.com/fantasy/football/league/standings?"
            "leagueId=123&seasonId=2026",
            "https://www.espn.com/fantasy/football?leagueId=123",
            "https://fantasy.espn.com/football/#/league?leagueId=123",
            "https://fantasy.espn.com/football/#/league/history?leagueId=123",
        )
        for page in pages:
            with self.subTest(page=page):
                request = valid_request(host_league_url=page)
                self.assertTrue(
                    request.host_league_url.startswith(
                        "https://fantasy.espn.com/football/league?leagueId=123"
                    )
                )

    def test_accepts_yahoo_home_team_filtered_and_season_prefixed_pages(self):
        pages = {
            "https://football.fantasysports.yahoo.com/f1/456":
                "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
            "https://football.fantasysports.yahoo.com/f1/456/7?module=team":
                "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
            "https://football.fantasysports.yahoo.com/f1/456/players?"
            "status=ALL&pos=O&stat1=S_PW#players":
                "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
            "https://football.fantasysports.yahoo.com/f1/456/playersearch":
                "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
            "https://football.fantasysports.yahoo.com/2026/f1/456/players?"
            "status=A&pos=P":
                "https://football.fantasysports.yahoo.com/2026/f1/456/players?status=ALL",
        }
        for page, expected in pages.items():
            with self.subTest(page=page):
                self.assertEqual(
                    valid_request(
                        yahoo_projection_league_url=page
                    ).yahoo_projection_league_url,
                    expected,
                )

    def test_rejects_unknown_fields_and_unsafe_or_wrong_purpose_urls(self):
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            WeeklyCollectionRequest.from_payload(request_payload(cookie="secret"))
        with self.assertRaisesRegex(ValueError, "ESPN Fantasy Football"):
            valid_request(host_league_url="https://example.com/league/123")
        with self.assertRaisesRegex(ValueError, "numeric Yahoo"):
            valid_request(
                yahoo_projection_league_url=(
                    "https://football.fantasys.yahoo.com/f1/456/players"
                )
            )
        with self.assertRaisesRegex(ValueError, "numeric Yahoo"):
            valid_request(
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/league/custom-name"
                )
            )
        with self.assertRaisesRegex(ValueError, "safe HTTPS"):
            valid_request(host_league_url="https://user:password@fantasy.espn.com/football/league")

    def test_rejects_wrong_seasons_duplicate_ids_and_unrelated_provider_pages(self):
        invalid = (
            {"host_league_url": (
                "https://fantasy.espn.com/football/league?leagueId=123&seasonId=2025"
            )},
            {"yahoo_projection_league_url": (
                "https://football.fantasysports.yahoo.com/2025/f1/456/players"
            )},
            {"host_league_url": (
                "https://fantasy.espn.com/football/league?leagueId=123&LeagueID=123"
            )},
            {"host_league_url": (
                "https://fantasy.espn.com/football/league?leagueId=123&"
                "seasonId=2026&SeasonID=2026"
            )},
            {"host_league_url": (
                "https://fantasy.espn.com/baseball/league?leagueId=123"
            )},
            {"yahoo_projection_league_url": (
                "https://football.fantasysports.yahoo.com/f1/456/settings"
            )},
            {"yahoo_projection_league_url": (
                "https://football.fantasysports.yahoo.com/f1/456/players/extra"
            )},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                valid_request(**changes)


class WeeklyCollectionJobTests(unittest.TestCase):
    def test_interactive_sign_in_status_and_confirmation_are_job_scoped(self):
        workflow = InteractiveWorkflow()
        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", workflow)
            started = jobs.start(valid_request())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                current = jobs.job(started["job_id"])
                if (
                    current["sign_in"] is not None
                    and current["sign_in"]["pending_provider"] == "fantasypros"
                ):
                    confirmed = jobs.confirm_sign_in(started["job_id"])
                    break
                time.sleep(0.005)
            else:
                self.fail("interactive collection did not expose its sign-in gate")
            finished = wait_for_collection(jobs.job, started["job_id"])
        self.assertEqual(confirmed["confirmed_provider"], "fantasypros")
        self.assertIsNone(confirmed["sign_in"]["pending_provider"])
        self.assertEqual(finished["status"], "complete")

    def test_publishes_complete_bundle_and_does_not_expose_private_urls(self):
        workflow = SuccessfulWorkflow()
        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", workflow)
            started = jobs.start(valid_request())
            finished = wait_for_collection(jobs.job, started["job_id"])
            bundle_path = Path(directory) / "bundles" / f"{finished['bundle_id']}.json"
            bundle_exists = bundle_path.is_file()

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["progress"]["stage"], "ready")
        self.assertTrue(bundle_exists)
        self.assertNotIn("host_league_url", finished["request"])
        self.assertNotIn("yahoo_projection_league_url", finished["request"])
        self.assertIsNone(finished["request"]["expected_team_count"])
        self.assertFalse(finished["request"]["allow_surrogate_power"])
        self.assertEqual(len(workflow.calls), 1)

    def test_publishes_bound_history_before_exposing_the_weekly_bundle(self):
        workflow = HistoryWorkflow()
        expected = engine_bundle()
        observed = {}
        original_ingest = LeagueHistoryStore.ingest

        def observing_ingest(store, capture, *, bundle=None):
            staged_path = (
                Path(directory)
                / "bundles"
                / ".weekly-publications"
                / f"{expected.bundle_id}.json"
            )
            observed["staged_bundle"] = load_engine_bundle(staged_path)
            observed["final_existed"] = (
                Path(directory) / "bundles" / f"{expected.bundle_id}.json"
            ).exists()
            return original_ingest(store, capture, bundle=bundle)

        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.weekly_collection.LeagueHistoryStore.ingest",
            new=observing_ingest,
        ):
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", workflow)
            started = jobs.start(valid_request())
            finished = wait_for_collection(jobs.job, started["job_id"])
            history = LeagueHistoryStore(
                Path(directory) / LEAGUE_HISTORY_FILENAME
            ).snapshot_for_bundle(finished["bundle_id"])
            bundle_exists = (
                Path(directory) / "bundles" / f"{finished['bundle_id']}.json"
            ).is_file()

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(observed["staged_bundle"].to_record(), expected.to_record())
        self.assertFalse(observed["final_existed"])
        self.assertTrue(bundle_exists)
        self.assertIsNotNone(history)
        self.assertEqual(history.bundle_id, finished["bundle_id"])

    def test_history_failure_does_not_expose_an_unbound_bundle(self):
        workflow = HistoryWorkflow()
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.weekly_collection.LeagueHistoryStore.ingest",
            side_effect=RuntimeError("history unavailable"),
        ):
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", workflow)
            started = jobs.start(valid_request())
            finished = wait_for_collection(jobs.job, started["job_id"])
            bundles = tuple((Path(directory) / "bundles").glob("*.json"))

        self.assertEqual(finished["status"], "failed")
        self.assertEqual(bundles, ())
        self.assertNotIn("history unavailable", finished["error"])

    def test_final_save_failure_keeps_a_bound_exact_stage_and_startup_recovers(self):
        workflow = HistoryWorkflow()
        expected = engine_bundle()
        real_save = save_engine_bundle

        def fail_final_save(bundle, path):
            target = Path(path)
            if target.parent.name == ".weekly-publications":
                return real_save(bundle, target)
            raise OSError("simulated final publication failure")

        with TemporaryDirectory() as directory:
            bundle_directory = Path(directory) / "bundles"
            staged_path = (
                bundle_directory
                / ".weekly-publications"
                / f"{expected.bundle_id}.json"
            )
            final_path = bundle_directory / f"{expected.bundle_id}.json"
            with patch(
                "trade_snapshot.weekly_collection.save_engine_bundle",
                side_effect=fail_final_save,
            ):
                jobs = WeeklyCollectionJobs(directory, bundle_directory, workflow)
                started = jobs.start(valid_request())
                failed = wait_for_collection(jobs.job, started["job_id"])

            bound_history = LeagueHistoryStore(
                Path(directory) / LEAGUE_HISTORY_FILENAME
            ).snapshot_for_bundle(expected.bundle_id)
            staged_record = load_engine_bundle(staged_path).to_record()
            final_existed_before_recovery = final_path.exists()

            recovered_jobs = WeeklyCollectionJobs(directory, bundle_directory, None)
            recovered_record = load_engine_bundle(final_path).to_record()
            stage_exists_after_recovery = staged_path.exists()

        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("simulated final", failed["error"])
        self.assertIsNotNone(bound_history)
        self.assertEqual(staged_record, expected.to_record())
        self.assertFalse(final_existed_before_recovery)
        self.assertEqual(recovered_record, expected.to_record())
        self.assertFalse(stage_exists_after_recovery)
        self.assertFalse(recovered_jobs.available)

    def test_startup_does_not_promote_an_unbound_private_stage(self):
        expected = engine_bundle()
        with TemporaryDirectory() as directory:
            bundle_directory = Path(directory) / "bundles"
            staged_path = (
                bundle_directory
                / ".weekly-publications"
                / f"{expected.bundle_id}.json"
            )
            final_path = bundle_directory / f"{expected.bundle_id}.json"
            save_engine_bundle(expected, staged_path)

            WeeklyCollectionJobs(directory, bundle_directory, None)
            final_exists = final_path.exists()
            stage_remains_private = staged_path.exists()

        self.assertFalse(final_exists)
        self.assertTrue(stage_remains_private)

    def test_cancellation_and_expected_failure_publish_nothing(self):
        entered = Event()

        def blocking(_request, *, data_directory, progress, cancelled):
            entered.set()
            while not cancelled():
                time.sleep(0.005)
            raise WeeklyCollectionError("cancelled")

        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", blocking)
            started = jobs.start(valid_request())
            self.assertTrue(entered.wait(1))
            jobs.cancel(started["job_id"])
            finished = wait_for_collection(jobs.job, started["job_id"])
            files = tuple((Path(directory) / "bundles").glob("*.json"))

        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNone(finished["error"])
        self.assertEqual(files, ())

    def test_unexpected_failure_is_fail_closed_and_does_not_leak_details(self):
        def broken(_request, *, data_directory, progress, cancelled):
            raise RuntimeError("cookie=private-value")

        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", broken)
            started = jobs.start(valid_request())
            finished = wait_for_collection(jobs.job, started["job_id"])
            files = tuple((Path(directory) / "bundles").glob("*.json"))

        self.assertEqual(finished["status"], "failed")
        self.assertNotIn("private-value", finished["error"])
        self.assertEqual(files, ())


class WeeklyCollectionServiceTests(unittest.TestCase):
    def test_collection_registers_bundle_and_search_does_not_call_collection_workflow(self):
        workflow = SuccessfulWorkflow()
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory, weekly_collection_workflow=workflow)
            started = service.start_weekly_collection(valid_request())
            finished = wait_for_collection(service.weekly_collection, started["job_id"])
            calls_after_collection = len(workflow.calls)
            search = service.start_search(
                LocalSearchRequest.from_payload(payload(bundle.bundle_id))
            )
            search_finished = wait_for_job(service, search["job_id"])
            readiness = service.bundle_readiness()

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(search_finished["status"], "complete")
        self.assertEqual(len(workflow.calls), calls_after_collection)
        self.assertTrue(readiness["ready"])

    def test_missing_workflow_reports_import_fallback_before_starting_a_job(self):
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            with self.assertRaisesRegex(RuntimeError, "Import a complete weekly bundle"):
                service.start_weekly_collection(valid_request())
            readiness = service.bundle_readiness()
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["collection_available"])


class WeeklyCollectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.workflow = SuccessfulWorkflow()
        self.server = create_local_server(
            self.directory.name,
            weekly_collection_workflow=self.workflow,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method, path, value=None):
        body = "" if value is None else json.dumps(value)
        headers = {"X-FTE-Token": self.server.app_token}
        if value is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_background_start_status_and_readiness_routes(self):
        status, started = self.request(
            "POST", "/api/weekly-collections", request_payload(
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                )
            )
        )
        self.assertEqual(status, 202)
        finished = wait_for_collection(
            lambda job_id: self.request(
                "GET", f"/api/weekly-collections/{job_id}"
            )[1],
            started["job_id"],
        )
        status, catalog = self.request("GET", "/api/bundles")
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(status, 200)
        self.assertTrue(catalog["readiness"]["ready"])

    def test_interface_exposes_collection_and_fail_closed_readiness_controls(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("Scan league &amp; collect", page)
        self.assertNotIn('id="expectedTeamCount"', page)
        self.assertIn("League size, every team, and every roster are detected", page)
        self.assertIn('id="hostLeagueUrl"', page)
        self.assertNotIn(
            'id="includeFutureWeekly" type="checkbox" checked', page
        )
        self.assertIn('id="yahooProjectionUrl"', page)
        self.assertIn('id="readiness"', page)


class InteractiveWeeklyCollectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.workflow = InteractiveWorkflow()
        self.server = create_local_server(
            self.directory.name,
            weekly_collection_workflow=self.workflow,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, method, path, value=None):
        body = "" if value is None else json.dumps(value)
        headers = {"X-FTE-Token": self.server.app_token}
        if value is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_sign_in_route_releases_the_exact_waiting_collection(self):
        status, started = self.request(
            "POST", "/api/weekly-collections", request_payload()
        )
        self.assertEqual(status, 202)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, current = self.request(
                "GET", f"/api/weekly-collections/{started['job_id']}"
            )
            self.assertEqual(status, 200)
            if (
                current["sign_in"] is not None
                and current["sign_in"]["pending_provider"] == "fantasypros"
            ):
                break
            time.sleep(0.005)
        else:
            self.fail("HTTP collection did not expose its sign-in gate")

        status, confirmed = self.request(
            "POST", f"/api/weekly-collections/{started['job_id']}/sign-in"
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["confirmed_provider"], "fantasypros")
        self.assertIsNone(confirmed["sign_in"]["pending_provider"])

        finished = wait_for_collection(
            lambda job_id: self.request(
                "GET", f"/api/weekly-collections/{job_id}"
            )[1],
            started["job_id"],
        )
        self.assertEqual(finished["status"], "complete")


if __name__ == "__main__":
    unittest.main()
