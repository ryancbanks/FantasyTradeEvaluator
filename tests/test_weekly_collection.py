import http.client
import json
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from tests.test_app_service import payload, wait_for_job
from tests.test_engine_bundle import engine_bundle
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest
from trade_snapshot.engine_bundle import load_engine_bundle, save_engine_bundle
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeam,
    HistoryTeamRoster,
    LeagueHistoryCapture,
    LeagueHistoryStore,
)
from trade_snapshot.local_server import create_local_server
from trade_snapshot.operation_timing import OperationTiming
from trade_snapshot.weekly_collection import (
    LEAGUE_HISTORY_FILENAME,
    WeeklyCollectionError,
    WeeklyCollectionJobs,
    WeeklyCollectionProgress,
    WeeklyCollectionPublication,
    WeeklyCollectionRequest,
    WeeklyCollectionStage,
    WeeklyHistoryAttempt,
    load_weekly_history_attempt,
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


class _TimedSignInGate:
    def __init__(self):
        self.pending = None
        self.confirmed = []
        self.listeners = []

    def subscribe_wait_state(self, listener):
        self.listeners.append(listener)

        def unsubscribe():
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    def begin_wait(self):
        self.pending = "fantasypros"
        for listener in tuple(self.listeners):
            listener(True)

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
        for listener in tuple(self.listeners):
            listener(False)
        return provider


class TimedInteractiveWorkflow(SuccessfulWorkflow):
    def __init__(self):
        super().__init__()
        self.sign_in_gate = _TimedSignInGate()
        self.waiting_for_sign_in = Event()
        self.continued_after_sign_in = Event()
        self.finish_work = Event()

    def __call__(self, request, *, data_directory, progress, cancelled):
        self.sign_in_gate.begin_wait()
        self.waiting_for_sign_in.set()
        while self.sign_in_gate.pending is not None and not cancelled():
            time.sleep(0.001)
        self.continued_after_sign_in.set()
        if not self.finish_work.wait(1):
            raise RuntimeError("test did not release post-sign-in work")
        return super().__call__(
            request,
            data_directory=data_directory,
            progress=progress,
            cancelled=cancelled,
        )


class TerminalRaceGate(_TimedSignInGate):
    def __init__(self):
        super().__init__()
        self.waiting_for_sign_in = Event()
        self.confirmation_dispatched = Event()
        self.release_confirmation = Event()

    def begin_wait(self):
        super().begin_wait()
        self.waiting_for_sign_in.set()

    def confirm(self):
        if self.pending is None:
            raise ValueError("no provider sign-in is waiting for confirmation")
        provider = self.pending
        self.pending = None
        self.confirmed.append(provider)
        dispatched = tuple(self.listeners)
        self.confirmation_dispatched.set()
        if not self.release_confirmation.wait(1):
            raise RuntimeError("test did not release confirmation")
        for listener in dispatched:
            listener(False)
        return provider


class TerminalRaceWorkflow:
    def __init__(self):
        self.sign_in_gate = TerminalRaceGate()
        self.fail = Event()

    def __call__(self, request, *, data_directory, progress, cancelled):
        self.sign_in_gate.begin_wait()
        if not self.fail.wait(1):
            raise RuntimeError("test did not release workflow failure")
        raise WeeklyCollectionError("forced collection failure")


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


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
    league_key = bundle.source_manifest.league_binding_id
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
    def test_direct_request_defaults_to_maximum_weekly_projection_coverage(self):
        request = WeeklyCollectionRequest(2026, 1, "PPR")
        self.assertTrue(request.include_future_weekly)
        self.assertIsNone(request.yahoo_projection_league_url)
        self.assertFalse(request.refresh_public_player_data)

    def test_accepts_purpose_specific_urls_and_auto_discovers_team_count(self):
        record = request_payload()
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
        self.assertTrue(
            valid_request(refresh_public_player_data=True).refresh_public_player_data
        )
        with self.assertRaisesRegex(ValueError, "boolean"):
            valid_request(refresh_public_player_data="yes")

    def test_normalizes_host_pages_to_minimal_purpose_specific_urls(self):
        request = valid_request(
            host_league_url=(
                "https://fantasy.espn.com/football/league/standings"
                "?leagueId=123&view=standings"
            ),
        )
        self.assertEqual(
            request.host_league_url,
            "https://fantasy.espn.com/football/league?leagueId=123",
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

    def test_yahoo_league_pages_are_normalized_to_the_complete_player_list(self):
        expected = (
            "https://football.fantasysports.yahoo.com/"
            "f1/456/players?status=ALL"
        )
        self.assertEqual(valid_request().yahoo_projection_league_url, expected)
        self.assertIsNone(
            valid_request(yahoo_projection_league_url=None).yahoo_projection_league_url
        )
        for value in (
            "https://football.fantasysports.yahoo.com/f1/456",
            "https://football.fantasysports.yahoo.com/f1/456/9",
            "https://football.fantasysports.yahoo.com/f1/456/playersearch",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    valid_request(
                        yahoo_projection_league_url=value
                    ).yahoo_projection_league_url,
                    expected,
                )
        with self.assertRaisesRegex(ValueError, "web address"):
            valid_request(yahoo_projection_league_url=False)

    def test_rejects_unknown_fields_and_unsafe_or_wrong_purpose_urls(self):
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            WeeklyCollectionRequest.from_payload(request_payload(cookie="secret"))
        with self.assertRaisesRegex(ValueError, "ESPN Fantasy Football"):
            valid_request(host_league_url="https://example.com/league/123")
        with self.assertRaisesRegex(ValueError, "safe HTTPS"):
            valid_request(host_league_url="https://user:password@fantasy.espn.com/football/league")

    def test_rejects_wrong_seasons_duplicate_ids_and_unrelated_provider_pages(self):
        invalid = (
            {"host_league_url": (
                "https://fantasy.espn.com/football/league?leagueId=123&seasonId=2025"
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
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                valid_request(**changes)


class WeeklyHistoryAttemptTests(unittest.TestCase):
    def test_not_provided_does_not_claim_a_provider_or_capture_evidence(self):
        record = WeeklyHistoryAttempt.not_provided().to_record()

        self.assertEqual(record["status"], "not_provided")
        self.assertIsNone(record["source_provider"])
        self.assertIsNone(record["attempted_at"])
        self.assertIsNone(record["capture_id"])
        self.assertIsNone(record["transactions_complete"])

    def test_not_provided_rejects_invented_attempt_evidence(self):
        with self.assertRaisesRegex(ValueError, "cannot invent evidence"):
            WeeklyHistoryAttempt(
                "not_provided",
                "not_provided",
                None,
                source_provider="espn",
            )
        with self.assertRaisesRegex(ValueError, "status and reason"):
            WeeklyHistoryAttempt(
                "unavailable",
                "not_provided",
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                source_provider="espn",
            )

    def test_loader_rejects_a_document_bound_to_a_different_bundle(self):
        bundle_id = "engine_" + "1" * 64
        with TemporaryDirectory() as directory:
            attempt_directory = Path(directory) / "history-attempts"
            attempt_directory.mkdir()
            (attempt_directory / f"{bundle_id}.json").write_text(
                json.dumps(
                    {
                        "bundle_id": "engine_" + "2" * 64,
                        "history_attempt": WeeklyHistoryAttempt.not_provided().to_record(),
                        "schema_version": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "identity"):
                load_weekly_history_attempt(directory, bundle_id)

    def test_publication_rejects_attempt_from_a_different_capture(self):
        publication = history_publication(engine_bundle())
        mismatched_attempt = replace(
            publication.history_attempt,
            capture_id="history-from-a-different-capture",
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            WeeklyCollectionPublication(
                publication.bundle,
                publication.history_capture,
                publication.history_binding,
                mismatched_attempt,
            )


class WeeklyCollectionJobTests(unittest.TestCase):
    def test_scoped_workspace_is_used_and_associated_only_after_publication(self):
        workflow = SuccessfulWorkflow()
        observed = {}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "leagues" / ("league_" + "a" * 32)
            bundle_directory = root / "bundles"
            jobs = WeeklyCollectionJobs(directory, bundle_directory, workflow)

            def published(bundle):
                observed["bundle_id"] = bundle.bundle_id
                observed["bundle_exists"] = (
                    bundle_directory / f"{bundle.bundle_id}.json"
                ).is_file()

            started = jobs.start(
                valid_request(),
                data_directory=workspace,
                on_published=published,
            )
            finished = wait_for_collection(jobs.job, started["job_id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(workflow.calls[0][1], workspace.resolve())
        self.assertEqual(observed["bundle_id"], finished["bundle_id"])
        self.assertTrue(observed["bundle_exists"])

    def test_scoped_workspace_and_publication_callback_are_validated(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            jobs = WeeklyCollectionJobs(
                directory,
                Path(directory) / "bundles",
                SuccessfulWorkflow(),
            )
            with self.assertRaisesRegex(ValueError, "inside application data"):
                jobs.start(valid_request(), data_directory=outside)
            with self.assertRaisesRegex(ValueError, "on_published"):
                jobs.start(valid_request(), on_published="not callable")

    def test_bound_history_is_kept_in_the_selected_league_workspace(self):
        workflow = HistoryWorkflow()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "leagues" / ("league_" + "b" * 32)
            jobs = WeeklyCollectionJobs(directory, root / "bundles", workflow)
            started = jobs.start(valid_request(), data_directory=workspace)
            finished = wait_for_collection(jobs.job, started["job_id"])
            scoped = LeagueHistoryStore(
                workspace / LEAGUE_HISTORY_FILENAME
            ).snapshot_for_bundle(finished["bundle_id"])
            global_history_exists = (root / LEAGUE_HISTORY_FILENAME).exists()

        self.assertEqual(finished["status"], "complete")
        self.assertIsNotNone(scoped)
        self.assertFalse(global_history_exists)

    def test_failed_workspace_association_leaves_a_visible_unassigned_bundle(self):
        def broken_association(_bundle):
            raise RuntimeError("private catalog detail")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = WeeklyCollectionJobs(
                directory,
                root / "bundles",
                SuccessfulWorkflow(),
            )
            started = jobs.start(
                valid_request(),
                data_directory=root / "leagues" / ("league_" + "c" * 32),
                on_published=broken_association,
            )
            finished = wait_for_collection(jobs.job, started["job_id"])
            bundle_exists = (
                root / "bundles" / f"{finished['bundle_id']}.json"
            ).is_file()

        self.assertEqual(finished["status"], "failed")
        self.assertTrue(bundle_exists)
        self.assertIn("Unassigned imports", finished["error"])
        self.assertNotIn("private catalog detail", finished["error"])

    def test_sign_in_wait_is_excluded_without_polling_and_later_work_is_counted(self):
        workflow = TimedInteractiveWorkflow()
        clock = FakeClock()
        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(
                directory,
                Path(directory) / "bundles",
                workflow,
                timing_factory=lambda: OperationTiming(clock=clock),
            )
            started = jobs.start(valid_request())
            self.assertTrue(workflow.waiting_for_sign_in.wait(1))

            # The gate transition, not a UI poll, pauses active-time accounting.
            clock.advance(120)
            jobs.confirm_sign_in(started["job_id"])
            self.assertTrue(workflow.continued_after_sign_in.wait(1))

            clock.advance(4.25)
            workflow.finish_work.set()
            finished = wait_for_collection(jobs.job, started["job_id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["operation"]["activity"], "terminal")
        self.assertEqual(finished["operation"]["elapsed_seconds"], 4.25)

    def test_in_flight_confirmation_is_harmless_after_job_terminalizes(self):
        workflow = TerminalRaceWorkflow()
        confirmation = []
        errors = []
        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(
                directory, Path(directory) / "bundles", workflow
            )
            started = jobs.start(valid_request())
            self.assertTrue(workflow.sign_in_gate.waiting_for_sign_in.wait(1))

            def confirm():
                try:
                    confirmation.append(jobs.confirm_sign_in(started["job_id"]))
                except Exception as error:
                    errors.append(error)

            thread = Thread(target=confirm)
            thread.start()
            self.assertTrue(workflow.sign_in_gate.confirmation_dispatched.wait(1))
            workflow.fail.set()
            finished = wait_for_collection(jobs.job, started["job_id"])
            workflow.sign_in_gate.release_confirmation.set()
            thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(confirmation[0]["confirmed_provider"], "fantasypros")
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["operation"]["activity"], "terminal")

    def test_active_catalog_tracks_only_the_single_running_collection(self):
        entered = Event()

        def blocking(_request, *, data_directory, progress, cancelled):
            entered.set()
            while not cancelled():
                time.sleep(0.005)
            raise WeeklyCollectionError("cancelled")

        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(
                directory,
                Path(directory) / "bundles",
                blocking,
            )
            started = jobs.start(valid_request())
            self.assertTrue(entered.wait(1))
            self.assertEqual(jobs.active_job()["job_id"], started["job_id"])
            with self.assertRaisesRegex(RuntimeError, "already running"):
                jobs.start(valid_request())
            jobs.cancel(started["job_id"])
            wait_for_collection(jobs.job, started["job_id"])
            self.assertIsNone(jobs.active_job())

    def test_terminal_job_history_is_bounded(self):
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.weekly_collection._MAX_RETAINED_COLLECTION_JOBS", 2
        ):
            jobs = WeeklyCollectionJobs(
                directory,
                Path(directory) / "bundles",
                SuccessfulWorkflow(),
            )
            job_ids = []
            for _ in range(3):
                started = jobs.start(valid_request())
                job_ids.append(started["job_id"])
                self.assertEqual(
                    wait_for_collection(jobs.job, started["job_id"])["status"],
                    "complete",
                )

            with self.assertRaises(KeyError):
                jobs.job(job_ids[0])
            self.assertEqual(set(jobs._jobs), set(job_ids[1:]))

    def test_latest_terminal_collection_is_recoverable_until_acknowledged(self):
        with TemporaryDirectory() as directory:
            jobs = WeeklyCollectionJobs(
                directory,
                Path(directory) / "bundles",
                SuccessfulWorkflow(),
            )
            first = jobs.start(valid_request())
            wait_for_collection(jobs.job, first["job_id"])
            recovered_first = jobs.recoverable_job()
            self.assertEqual(recovered_first["job_id"], first["job_id"])
            self.assertEqual(recovered_first["operation"]["status"], "complete")
            self.assertEqual(recovered_first["operation"]["activity"], "terminal")

            second = jobs.start(valid_request())
            wait_for_collection(jobs.job, second["job_id"])
            self.assertFalse(
                jobs.acknowledge_activity(first["job_id"])["acknowledged"]
            )
            self.assertEqual(jobs.recoverable_job()["job_id"], second["job_id"])
            self.assertTrue(
                jobs.acknowledge_activity(second["job_id"])["acknowledged"]
            )
            self.assertIsNone(jobs.recoverable_job())
            self.assertFalse(
                jobs.acknowledge_activity(second["job_id"])["acknowledged"]
            )

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
            attempt_path = (
                Path(directory)
                / "history-attempts"
                / f"{finished['bundle_id']}.json"
            )
            attempt_record = json.loads(attempt_path.read_text(encoding="utf-8"))
            loaded_attempt = load_weekly_history_attempt(
                directory,
                finished["bundle_id"],
            )

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["progress"]["stage"], "ready")
        self.assertTrue(bundle_exists)
        self.assertNotIn("host_league_url", finished["request"])
        self.assertNotIn("yahoo_projection_league_url", finished["request"])
        self.assertTrue(
            finished["request"]["yahoo_projection_league_configured"]
        )
        self.assertIsNone(finished["request"]["expected_team_count"])
        self.assertFalse(finished["request"]["allow_surrogate_power"])
        self.assertFalse(finished["request"]["refresh_public_player_data"])
        self.assertEqual(finished["history_attempt"]["status"], "not_provided")
        self.assertIsNone(finished["history_attempt"]["source_provider"])
        self.assertEqual(
            attempt_record["history_attempt"], finished["history_attempt"]
        )
        self.assertEqual(loaded_attempt.to_record(), finished["history_attempt"])
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

    def test_optional_history_failure_still_publishes_the_core_bundle(self):
        workflow = HistoryWorkflow()
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.weekly_collection.LeagueHistoryStore.ingest",
            side_effect=RuntimeError("history unavailable"),
        ):
            jobs = WeeklyCollectionJobs(directory, Path(directory) / "bundles", workflow)
            started = jobs.start(valid_request())
            finished = wait_for_collection(jobs.job, started["job_id"])
            bundles = tuple((Path(directory) / "bundles").glob("*.json"))
            attempt_path = (
                Path(directory)
                / "history-attempts"
                / f"{finished['bundle_id']}.json"
            )
            attempt_record = json.loads(attempt_path.read_text(encoding="utf-8"))

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(len(bundles), 1)
        self.assertEqual(finished["history_attempt"]["status"], "unavailable")
        self.assertEqual(
            finished["history_attempt"]["reason_code"], "store_unavailable"
        )
        self.assertEqual(attempt_record["bundle_id"], finished["bundle_id"])
        self.assertEqual(
            attempt_record["history_attempt"], finished["history_attempt"]
        )
        self.assertNotIn("history unavailable", json.dumps(attempt_record))
        self.assertIsNone(finished["error"])

    def test_final_save_failure_keeps_a_validated_stage_and_startup_recovers(self):
        workflow = HistoryWorkflow()
        expected = engine_bundle()
        with TemporaryDirectory() as directory:
            bundle_directory = Path(directory) / "bundles"
            staged_path = (
                bundle_directory
                / ".weekly-publications"
                / f"{expected.bundle_id}.json"
            )
            final_path = bundle_directory / f"{expected.bundle_id}.json"
            with patch(
                "trade_snapshot.weekly_collection.save_bundle_with_summary",
                side_effect=OSError("simulated final publication failure"),
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

    def test_startup_promotes_a_validated_stage_without_optional_history(self):
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
            stage_exists = staged_path.exists()

        self.assertTrue(final_exists)
        self.assertFalse(stage_exists)

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
        self.assertTrue(finished["operation"]["cancel_requested"])
        self.assertEqual(finished["operation"]["status"], "cancelled")
        self.assertEqual(finished["operation"]["activity"], "terminal")
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
        self.assertEqual(finished["operation"]["status"], "failed")
        self.assertEqual(finished["operation"]["activity"], "terminal")
        self.assertEqual(files, ())


class WeeklyCollectionServiceTests(unittest.TestCase):
    def test_yahoo_only_profile_can_collect_with_fantasypros(self):
        with TemporaryDirectory() as directory:
            service = LocalAppService(
                directory,
                weekly_collection_workflow=SuccessfulWorkflow(),
            )
            profile = service.create_league_profile({
                "name": "FantasyPros League",
                "season": 2026,
                "scoring": "PPR",
                "host_league_url": "",
                "yahoo_projection_league_url": (
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                ),
            })
            readiness = service.league_bundle_catalog(
                profile["profile_id"]
            )["readiness"]

        self.assertTrue(readiness["collection_available"])
        self.assertTrue(readiness["fantasypros_collection_available"])
        self.assertFalse(readiness["independent_collection_available"])
        self.assertIn("FantasyPros-assisted collection is available", readiness["message"])

    def test_profile_collection_uses_its_workspace_and_registers_the_week(self):
        workflow = HistoryWorkflow()
        with TemporaryDirectory() as directory:
            service = LocalAppService(
                directory,
                weekly_collection_workflow=workflow,
            )
            profile = service.create_league_profile({
                "name": "Home League",
                "season": 2026,
                "scoring": "PPR",
                "host_league_url": (
                    "https://fantasy.espn.com/football/team?"
                    "leagueId=123&teamId=6&seasonId=2026"
                ),
                "yahoo_projection_league_url": (
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                ),
            })
            started = service.start_profile_weekly_collection(
                profile["profile_id"],
                {
                    "week": 1,
                    "include_future_weekly": False,
                    "allow_surrogate_power": False,
                    "use_fantasypros": True,
                    "use_broad_consensus": True,
                    "refresh_public_player_data": False,
                },
            )
            finished = wait_for_collection(
                service.weekly_collection,
                started["job_id"],
            )
            catalog = service.league_bundle_catalog(profile["profile_id"])
            workspace = Path(directory) / "leagues" / profile["profile_id"]
            history = service._league_history_store(
                finished["bundle_id"]
            ).snapshot_for_bundle(finished["bundle_id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(workflow.calls[0][1], workspace.resolve())
        self.assertEqual(
            [row["bundle_id"] for row in catalog["bundles"]],
            [finished["bundle_id"]],
        )
        self.assertIsNotNone(history)

    def test_assigning_a_direct_collection_preserves_only_its_history_evidence(self):
        workflow = HistoryWorkflow()
        unrelated_bundle_id = "engine_" + "9" * 64
        with TemporaryDirectory() as directory:
            root = Path(directory)
            service = LocalAppService(
                directory,
                weekly_collection_workflow=workflow,
            )
            started = service.start_weekly_collection(valid_request())
            finished = wait_for_collection(
                service.weekly_collection,
                started["job_id"],
            )
            bundle_id = finished["bundle_id"]
            source_store = LeagueHistoryStore(root / LEAGUE_HISTORY_FILENAME)
            source_snapshot = source_store.snapshot_for_bundle(bundle_id)
            unrelated_capture = replace(
                source_snapshot.captures[0],
                league_key="league_" + "f" * 32,
            )
            source_store.ingest(
                unrelated_capture,
                bundle=HistoryBundleBinding(
                    unrelated_capture.league_key,
                    unrelated_capture.season,
                    unrelated_bundle_id,
                    unrelated_capture.captured_at,
                ),
            )
            profile = service.create_league_profile({
                "name": "Assigned direct collection",
                "season": 2026,
                "scoring": "PPR",
                "host_league_url": (
                    "https://fantasy.espn.com/football/league?leagueId=123"
                ),
                "yahoo_projection_league_url": (
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                ),
            })

            service.assign_bundle_to_league(profile["profile_id"], bundle_id)

            workspace = root / "leagues" / profile["profile_id"]
            destination_store = LeagueHistoryStore(
                workspace / LEAGUE_HISTORY_FILENAME
            )
            migrated = destination_store.snapshot_for_bundle(bundle_id)
            unrelated_migrated = destination_store.snapshot_for_bundle(
                unrelated_bundle_id
            )
            migrated_attempt = load_weekly_history_attempt(workspace, bundle_id)
            insights = service.gm_insights(bundle_id)

        self.assertEqual(migrated, source_snapshot)
        self.assertIsNone(unrelated_migrated)
        self.assertEqual(migrated_attempt.to_record(), finished["history_attempt"])
        self.assertNotEqual(insights["status"], "not_collected")
        self.assertEqual(insights["data_readiness"]["store_status"], "available")
        self.assertEqual(
            insights["data_readiness"]["collection_attempt"]["status"],
            "captured",
        )

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
            "POST", "/api/weekly-collections", request_payload()
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

    def test_yahoo_league_link_is_accepted_at_the_api_boundary(self):
        status, started = self.request(
            "POST",
            "/api/weekly-collections",
            request_payload(
                yahoo_projection_league_url=(
                    "https://football.fantasysports.yahoo.com/f1/456/players"
                )
            ),
        )

        self.assertEqual(status, 202)
        finished = wait_for_collection(
            lambda job_id: self.request(
                "GET", f"/api/weekly-collections/{job_id}"
            )[1],
            started["job_id"],
        )
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(
            self.workflow.calls[-1][0].yahoo_projection_league_url,
            "https://football.fantasysports.yahoo.com/"
            "f1/456/players?status=ALL",
        )

    def test_interface_exposes_collection_and_fail_closed_readiness_controls(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("Scan selected league &amp; collect", page)
        self.assertNotIn('id="expectedTeamCount"', page)
        self.assertIn("League size, every team, and every roster are detected", page)
        self.assertIn('id="hostLeagueUrl"', page)
        self.assertIn(
            'id="includeFutureWeekly" type="checkbox" checked', page
        )
        self.assertIn("recommended for matchup-level forecasts", page)
        self.assertIn('id="yahooProjectionUrl"', page)
        self.assertIn("Yahoo league or player-list link", page)
        self.assertIn('id="useFantasyPros" type="checkbox" checked', page)
        self.assertIn('id="refreshPublicPlayerData" type="checkbox"', page)
        self.assertIn('id="useBroadConsensus" type="checkbox" checked', page)
        self.assertIn("sign in to ESPN and Yahoo normally", page)
        self.assertIn('id="sourceDebug"', page)
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
