from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tests.capture_fixtures import league_sources
from tests.test_engine_bundle import engine_bundle
from tests.test_player_profiles import _public_data, profile_snapshot
from trade_snapshot.analyzer_contract import CURRENT_BUNDLE_FINGERPRINT
from trade_snapshot.capture_schema import (
    CaptureKind,
    CapturePlan,
    ECRRankingRow,
    FantasyProsECRArtifact,
    FantasyProsECRTask,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    LeagueSource,
    LeagueSourceKind,
    PageCaptureTask,
    ProjectionTableSpec,
    VisibleTable,
    VisibleTableCell,
)
from trade_snapshot.espn_free_read import (
    EspnFreeReadClient,
    EspnFreeReadError,
    EspnUnauthorizedError,
)
from trade_snapshot._espn_browser_read import read_authenticated_espn_json
from trade_snapshot.browser_capture import (
    BrowserCaptureCancelled,
    BrowserCaptureError,
    BrowserCaptureOptions,
    BrowserExtensionUpgradeRequired,
    YahooScoringError,
)
from trade_snapshot.identity import IdentityRegistry
from trade_snapshot.identity_io import load_identity_registry
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryTeam,
    HistoryTeamRoster,
    LeagueHistoryCapture,
    make_league_key,
)
from trade_snapshot.independent_weekly_assembly import IndependentWeeklyEngine
from trade_snapshot.production_calibration import (
    BrowserCalibrationFactory,
    CalibrationCaptureContext,
    InteractiveSignInGate,
)
from trade_snapshot.player_profile_materialize import PlayerProfileMaterializationError
from trade_snapshot.player_lab_projections import PlayerLabProjectionSnapshot
from trade_snapshot.public_player_data import (
    DataAvailability,
    PublicPlayerDataCancelled,
    PublicPlayerDataError,
)
from trade_snapshot.production_collection import (
    CalibrationCallbacks,
    ProductionWeeklyCollectionWorkflow,
    _yahoo_projection_task,
    create_production_weekly_collection_workflow,
)
from trade_snapshot.weekly_assembly import AssembledWeeklyEvidence
from trade_snapshot.weekly_collection import (
    WeeklyCollectionError,
    WeeklyCollectionPublication,
    WeeklyCollectionRequest,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _public_unavailable(*_args, **_kwargs):
    raise PublicPlayerDataError("public profile fixture is intentionally unavailable")


class _Gate:
    def is_ready(self, task):
        return True


class _Collector:
    def __init__(self):
        self.calls = []
        self.open_calls = []
        self.close_count = 0
        self.authenticated_calls = []
        self.authenticated_result = None
        self.authenticated_error = None
        self.yahoo_scoring_calls = []
        self.yahoo_scoring_error = None

    def open_session(self, options, **kwargs):
        self.open_calls.append((options, kwargs))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close_count += 1
        return False

    def collect(self, plan, options, **kwargs):
        self.calls.append((plan, options, kwargs))
        if len(plan.tasks) == 1 and plan.tasks[0].kind is CaptureKind.LEAGUE_SOURCE:
            return (_league_artifact(plan.tasks[0]),)
        return tuple(_artifact(task) for task in plan.tasks)

    def read_authenticated_espn_json(self, *args):
        self.authenticated_calls.append(args)
        if self.authenticated_error is not None:
            raise self.authenticated_error
        if self.authenticated_result is None:
            raise AssertionError("authenticated ESPN fallback was not expected")
        return self.authenticated_result

    def verify_yahoo_scoring(self, *args):
        self.yahoo_scoring_calls.append(args)
        if self.yahoo_scoring_error is not None:
            raise self.yahoo_scoring_error
        return "PPR"


class ProductionWeeklyCollectionTests(unittest.TestCase):
    def test_collects_two_single_page_phases_and_forwards_strict_calibration(self):
        collector = _Collector()
        host = _host_snapshot()
        assembled = _assembled()
        bundle = engine_bundle()
        records = {}

        def reader(season, league_id, cancelled):
            records["read"] = season, league_id, cancelled()
            return {"league": True}, {"pro_teams": True}

        def adapter(league, pro_teams, **kwargs):
            records["adapter"] = league, pro_teams, kwargs
            return host

        def schedule_adapter(payload, **kwargs):
            schedule = SimpleNamespace(season=2026)
            records["schedule"] = payload, kwargs, schedule
            return schedule

        def assembler(**kwargs):
            records["assembly"] = kwargs
            return assembled

        callbacks = CalibrationCallbacks(lambda *_: None, lambda *_: None)

        def calibration_factory(value, primary_team, context):
            records["calibration"] = value, primary_team, context
            return callbacks

        def refresher(evidence, **kwargs):
            records["refresh"] = evidence, kwargs
            return SimpleNamespace(bundle=bundle)

        activity = object()

        def activity_adapter(payload, **kwargs):
            records["activity"] = payload, kwargs
            return activity

        def history_adapter(source, assembled_value, bundle_value, **kwargs):
            records["history"] = source, assembled_value, bundle_value, kwargs
            return _history_pair(bundle_value)

        workflow = ProductionWeeklyCollectionWorkflow(
            sign_in_gate=_Gate(),
            calibration_factory=calibration_factory,
            collector=collector,
            espn_reader=reader,
            host_adapter=adapter,
            schedule_adapter=schedule_adapter,
            plan_builder=_small_plan,
            assembler=assembler,
            public_player_reader=_public_unavailable,
            refresher=refresher,
            activity_adapter=activity_adapter,
            history_adapter=history_adapter,
            now=lambda: NOW,
        )
        progress = []
        with TemporaryDirectory() as directory:
            identity_path = Path(directory) / "identity-registry.json"
            result = workflow(
                _request(allow_surrogate_power=True), data_directory=Path(directory),
                progress=progress.append, cancelled=lambda: False,
            )
            self.assertEqual(load_identity_registry(identity_path), assembled.identities)

        self.assertIsInstance(result, WeeklyCollectionPublication)
        self.assertEqual(
            result.bundle.player_lab_projections,
            assembled.player_lab_projections,
        )
        self.assertIsNone(result.bundle.player_profiles)
        self.assertEqual(records["read"][:2], (2026, "123"))
        self.assertEqual(records["activity"], ({"league": True}, {"captured_at": NOW}))
        self.assertEqual(records["adapter"][2]["expected_team_count"], 2)
        self.assertEqual(records["adapter"][2]["captured_at"], NOW)
        self.assertEqual(records["schedule"][0], {"pro_teams": True})
        self.assertEqual(records["schedule"][1]["captured_at"], NOW)
        self.assertIs(records["calibration"][0], assembled)
        self.assertEqual(records["calibration"][1], "canonical-team-1")
        self.assertTrue(records["calibration"][2].allow_surrogate_power)
        self.assertIs(records["refresh"][0], assembled.evidence)
        self.assertIs(records["refresh"][1]["calibrate"], callbacks.calibrate)
        self.assertIs(records["refresh"][1]["verify_reuse"], callbacks.verify_reuse)
        self.assertTrue(records["refresh"][1]["allow_surrogate_power"])
        self.assertIs(records["history"][0], activity)
        self.assertIs(records["history"][1], assembled)
        self.assertIs(records["history"][2], result.bundle)
        self.assertEqual(records["history"][3], {"bundle_captured_at": NOW})
        self.assertEqual(len(collector.calls), 2)
        self.assertEqual(len(collector.open_calls), 1)
        self.assertEqual(collector.close_count, 1)
        self.assertEqual(collector.authenticated_calls, [])
        self.assertEqual(len(collector.yahoo_scoring_calls), 1)
        yahoo_task, yahoo_url = collector.yahoo_scoring_calls[0]
        self.assertEqual(yahoo_task.provider.value, "yahoo")
        self.assertEqual(yahoo_task.projection.scoring, "PPR")
        self.assertEqual(
            yahoo_url,
            "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
        )
        first, second = collector.calls
        self.assertIs(first[1], second[1])
        self.assertTrue(first[1].headed)
        self.assertEqual(first[1].action_delay_ms, 200)
        self.assertLessEqual(first[1].overall_timeout_ms, 3_600_000)
        self.assertIs(first[2]["sign_in_gate"], second[2]["sign_in_gate"])
        bindings = second[2]["navigation_bindings"]
        by_provider = {task.provider.value: task.task_id for task in second[0].tasks}
        self.assertEqual(
            bindings[by_provider["espn"]],
            "https://fantasy.espn.com/football/players/projections?leagueId=123",
        )
        self.assertEqual(
            bindings[by_provider["yahoo"]],
            "https://football.fantasysports.yahoo.com/f1/456/players?status=ALL",
        )
        assembly = records["assembly"]
        self.assertEqual(assembly["expected_team_count"], 2)
        self.assertEqual(assembly["response_schema_sha256"], _schema_suffix())
        self.assertIs(assembly["nfl_schedule"], records["schedule"][2])
        self.assertEqual(assembly["analyzer_bundle"].sha256, CURRENT_BUNDLE_FINGERPRINT.sha256)
        self.assertLess(progress[-1].fraction, .99)
        self.assertTrue(any("Found 2 teams" in row.message for row in progress))

    def test_collects_public_player_data_once_and_attaches_portable_profiles(self):
        bundle = engine_bundle()
        public_data = _public_data()
        profiles = profile_snapshot(*bundle.player_names)
        calls = []

        def reader(season, **kwargs):
            calls.append(("read", season, kwargs))
            return public_data

        def builder(**kwargs):
            calls.append(("build", kwargs))
            return profiles

        progress = []
        workflow = _workflow(
            collector=_Collector(),
            public_player_reader=reader,
            profile_builder=builder,
        )
        with TemporaryDirectory() as directory:
            result = workflow(
                _request(),
                data_directory=Path(directory),
                progress=progress.append,
                cancelled=lambda: False,
            )

        self.assertIs(result.bundle.player_profiles, profiles)
        self.assertEqual(
            result.bundle.player_lab_projections,
            _assembled().player_lab_projections,
        )
        self.assertIsNone(bundle.player_profiles)
        self.assertEqual(calls[0][0:2], ("read", 2026))
        self.assertFalse(calls[0][2]["cancelled"]())
        self.assertIs(calls[0][2]["clock"](), NOW)
        self.assertEqual(calls[1][0], "build")
        self.assertEqual(calls[1][1]["league_snapshot_id"], bundle.state.snapshot_id)
        self.assertEqual(calls[1][1]["as_of_week"], bundle.state.first_remaining_week)
        self.assertIs(calls[1][1]["public_data"], public_data)
        self.assertEqual([row.fraction for row in progress], sorted(row.fraction for row in progress))
        self.assertTrue(any("Player Lab retained" in row.message for row in progress))

    def test_reuses_public_player_data_for_another_collection_in_the_same_week(self):
        bundle = engine_bundle()
        public_data = _public_data()
        profiles = profile_snapshot(*bundle.player_names)
        reads = []
        builds = []

        def reader(season, **_kwargs):
            reads.append(season)
            return public_data

        def builder(**kwargs):
            builds.append(kwargs["public_data"])
            return profiles

        workflow = _workflow(
            collector=_Collector(),
            public_player_reader=reader,
            profile_builder=builder,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow(
                _request(), data_directory=root,
                progress=lambda _row: None, cancelled=lambda: False,
            )
            second_progress = []
            workflow(
                _request(), data_directory=root,
                progress=second_progress.append, cancelled=lambda: False,
            )

        self.assertEqual(reads, [2026])
        self.assertEqual(builds, [public_data, public_data])
        self.assertTrue(any("Reusing public Player Lab data" in row.message for row in second_progress))

    def test_explicit_public_player_refresh_bypasses_the_weekly_cache(self):
        public_data = _public_data()
        profiles = profile_snapshot(*engine_bundle().player_names)
        reads = []
        workflow = _workflow(
            collector=_Collector(),
            public_player_reader=lambda season, **_kwargs: (
                reads.append(season) or public_data
            ),
            profile_builder=lambda **_kwargs: profiles,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow(
                _request(), data_directory=root,
                progress=lambda _row: None, cancelled=lambda: False,
            )
            workflow(
                _request(refresh_public_player_data=True), data_directory=root,
                progress=lambda _row: None, cancelled=lambda: False,
            )

        self.assertEqual(reads, [2026, 2026])

    def test_transiently_unavailable_public_source_is_retried_next_scan(self):
        healthy = _public_data()
        degraded = replace(
            healthy,
            provenance=tuple(
                replace(
                    row,
                    availability=DataAvailability.UNAVAILABLE,
                    content_sha256=None,
                    byte_count=0,
                )
                if row.provider == "dynastyprocess"
                else row
                for row in healthy.provenance
            ),
        )
        profiles = profile_snapshot(*engine_bundle().player_names)
        returned = iter((degraded, healthy))
        reads = []

        def reader(season, **_kwargs):
            reads.append(season)
            return next(returned)

        workflow = _workflow(
            collector=_Collector(),
            public_player_reader=reader,
            profile_builder=lambda **_kwargs: profiles,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_progress = []
            workflow(
                _request(), data_directory=root,
                progress=first_progress.append, cancelled=lambda: False,
            )
            workflow(
                _request(), data_directory=root,
                progress=lambda _row: None, cancelled=lambda: False,
            )
            cached = root / "public-player-cache" / "2026-week-01.json.gz"
            self.assertTrue(cached.is_file())

        self.assertEqual(reads, [2026, 2026])
        self.assertTrue(
            any("retry automatically" in row.message for row in first_progress)
        )

    def test_public_profile_source_failure_degrades_without_losing_trade_engine(self):
        def unavailable(*_args, **_kwargs):
            raise PublicPlayerDataError("upstream unavailable")

        progress = []
        workflow = _workflow(
            collector=_Collector(), public_player_reader=unavailable
        )
        with TemporaryDirectory() as directory:
            result = workflow(
                _request(),
                data_directory=Path(directory),
                progress=progress.append,
                cancelled=lambda: False,
            )

        self.assertIsNone(result.bundle.player_profiles)
        self.assertEqual(
            result.bundle.player_lab_projections,
            _assembled().player_lab_projections,
        )
        self.assertTrue(any("history is unavailable" in row.message for row in progress))

    def test_independent_engine_receives_the_same_profile_attachment(self):
        bundle = engine_bundle()
        independent = object.__new__(IndependentWeeklyEngine)
        object.__setattr__(independent, "bundle", bundle)
        object.__setattr__(independent, "identities", IdentityRegistry(()))
        object.__setattr__(
            independent,
            "player_lab_projections",
            _empty_lab_snapshot(bundle),
        )
        profiles = profile_snapshot(*bundle.player_names)
        build_calls = []

        def build(**kwargs):
            build_calls.append(kwargs)
            return profiles

        workflow = _workflow(
            collector=_Collector(),
            espn_reader=lambda *_args: ({"teams": [{}, {}]}, {}),
            independent_plan_builder=_small_independent_plan,
            independent_assembler=lambda **_kwargs: independent,
            public_player_reader=lambda *_args, **_kwargs: _public_data(),
            profile_builder=build,
        )
        with TemporaryDirectory() as directory:
            result = workflow(
                _request(use_fantasypros=False),
                data_directory=Path(directory),
                progress=lambda _: None,
                cancelled=lambda: False,
            )

        self.assertIs(result.bundle.player_profiles, profiles)
        self.assertIs(
            result.bundle.player_lab_projections,
            independent.player_lab_projections,
        )
        self.assertEqual(len(build_calls), 1)
        self.assertIs(build_calls[0]["identities"], independent.identities)

    def test_public_profile_cancellation_publishes_nothing(self):
        def cancelled(*_args, **_kwargs):
            raise PublicPlayerDataCancelled("cancelled")

        workflow = _workflow(
            collector=_Collector(), public_player_reader=cancelled
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(WeeklyCollectionError, "cancelled"):
                workflow(
                    _request(),
                    data_directory=root,
                    progress=lambda _: None,
                    cancelled=lambda: False,
                )
            self.assertFalse((root / "identity-registry.json").exists())

    def test_profile_identity_incompatibility_degrades_without_guessing(self):
        def incompatible(**_kwargs):
            raise PlayerProfileMaterializationError("identity conflict")

        progress = []
        workflow = _workflow(
            collector=_Collector(),
            public_player_reader=lambda *_args, **_kwargs: _public_data(),
            profile_builder=incompatible,
        )
        with TemporaryDirectory() as directory:
            result = workflow(
                _request(),
                data_directory=Path(directory),
                progress=progress.append,
                cancelled=lambda: False,
            )

        self.assertIsNone(result.bundle.player_profiles)
        self.assertEqual(
            result.bundle.player_lab_projections,
            _assembled().player_lab_projections,
        )
        self.assertTrue(any("could not be matched safely" in row.message for row in progress))

    def test_profile_bundle_cross_validation_failure_does_not_block_trade_engine(self):
        profiles = profile_snapshot(*engine_bundle().player_names)
        mismatched = replace(
            profiles,
            players=(
                replace(profiles.players[0], nfl_team_id="CONFLICT"),
                *profiles.players[1:],
            ),
        )
        progress = []
        workflow = _workflow(
            collector=_Collector(),
            public_player_reader=lambda *_args, **_kwargs: _public_data(),
            profile_builder=lambda **_kwargs: mismatched,
        )
        with TemporaryDirectory() as directory:
            result = workflow(
                _request(), data_directory=Path(directory),
                progress=progress.append, cancelled=lambda: False,
            )

        self.assertIsNone(result.bundle.player_profiles)
        self.assertTrue(any("could not be matched safely" in row.message for row in progress))

    def test_legacy_team_count_is_only_a_mismatch_guard(self):
        collector = _Collector()
        workflow = _workflow(collector=collector)
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "signed-in FantasyPros league has 2 teams"
        ):
            workflow(
                _request(expected_team_count=3),
                data_directory=Path(directory),
                progress=lambda _: None,
                cancelled=lambda: False,
            )
        self.assertEqual(len(collector.calls), 1)

    def test_sanitized_capture_failure_is_preserved_for_troubleshooting(self):
        class FailingCollector(_Collector):
            def collect(self, *_args, **_kwargs):
                raise BrowserCaptureError(
                    "FantasyPros loaded, but its analyzer initialization was not captured"
                )

        workflow = _workflow(collector=FailingCollector())
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError,
            "FantasyPros loaded, but its analyzer initialization was not captured.*"
            "No weekly bundle was published",
        ):
            workflow(
                _request(), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )

    def test_discovered_team_count_must_match_espn(self):
        collector = _Collector()
        workflow = _workflow(
            collector=collector,
            host=_host_snapshot(expected_team_count=3),
        )
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "strict validation"
        ):
            workflow(
                _request(),
                data_directory=Path(directory),
                progress=lambda _: None,
                cancelled=lambda: False,
            )
        self.assertEqual(len(collector.calls), 1)

    def test_missing_yahoo_and_cross_league_configuration_fail_before_reads(self):
        collector = _Collector()
        workflow = _workflow(collector=collector)
        missing = WeeklyCollectionRequest(
            2026, 1, "PPR", 2,
            "https://fantasy.espn.com/football/league?leagueId=123",
            None,
        )
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "numeric Yahoo"
        ):
            workflow(
                missing, data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )
        self.assertEqual(collector.calls, [])

        collector = _Collector()
        workflow = _workflow(collector=collector)
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "does not match"
        ):
            workflow(
                _request(host_id="999"), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )
        self.assertEqual(len(collector.calls), 1)

    def test_source_mismatch_and_identity_schema_failure_publish_nothing(self):
        collector = _Collector()
        wrong_host = _host_snapshot(source_league_id="999")
        workflow = _workflow(collector=collector, host=wrong_host)
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "strict validation"
        ):
            workflow(
                _request(), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )
        self.assertEqual(len(collector.calls), 1)

        collector = _Collector()

        def invalid_identity(**kwargs):
            raise ValueError("private player mismatch detail")

        workflow = _workflow(collector=collector, assembler=invalid_identity)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                WeeklyCollectionError, "strict validation"
            ) as raised:
                workflow(
                    _request(), data_directory=Path(directory),
                    progress=lambda _: None, cancelled=lambda: False,
                )
            self.assertFalse((Path(directory) / "identity-registry.json").exists())
        self.assertNotIn("private player", str(raised.exception))
        self.assertEqual(len(collector.calls), 2)

    def test_validation_failure_retains_safe_stage_and_league_diagnostics(self):
        def invalid_identity(**_kwargs):
            raise ValueError("private player mismatch detail")

        workflow = _workflow(collector=_Collector(), assembler=invalid_identity)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                WeeklyCollectionError,
                "cross-source identity and weekly evidence assembly.*Local diagnostic",
            ) as raised:
                workflow(
                    _request(), data_directory=root,
                    progress=lambda _: None, cancelled=lambda: False,
                )
            failure = json.loads(
                (root / "diagnostics" / "latest-weekly-validation-error.json")
                .read_text(encoding="utf-8")
            )
            league = json.loads(
                (root / "diagnostics" / "latest-fantasypros-league.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(
            failure["stage"], "cross-source identity and weekly evidence assembly"
        )
        self.assertEqual(failure["exception_type"], "ValueError")
        self.assertTrue(failure["league_capture_available"])
        self.assertTrue(failure["frames"])
        self.assertNotIn("private player mismatch detail", json.dumps(failure))
        self.assertNotIn("private player mismatch detail", str(raised.exception))
        self.assertEqual(league["kind"], "fantasypros_league_capture_diagnostic")

    def test_refresh_failure_does_not_persist_identity_registry(self):
        def failed_refresh(*_args, **_kwargs):
            raise ValueError("private refresh failure")

        workflow = _workflow(collector=_Collector(), refresher=failed_refresh)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                WeeklyCollectionError, "strict validation"
            ) as raised:
                workflow(
                    _request(), data_directory=Path(directory),
                    progress=lambda _: None, cancelled=lambda: False,
                )
            self.assertFalse((Path(directory) / "identity-registry.json").exists())
        self.assertNotIn("private refresh", str(raised.exception))

    def test_espn_scoring_mismatch_fails_before_projection_capture(self):
        collector = _Collector()
        host = _host_snapshot(scoring_profile=SimpleNamespace(
            platform="espn",
            settings={"scoring_settings": {"playerRankType": "STANDARD"}},
        ))
        workflow = _workflow(collector=collector, host=host)
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "strict validation"
        ):
            workflow(
                _request(), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )
        self.assertEqual(len(collector.calls), 1)

    def test_yahoo_scoring_failure_is_actionable_and_stops_before_projections(self):
        collector = _Collector()
        collector.yahoo_scoring_error = YahooScoringError(
            "Yahoo league scoring is Half PPR, but this refresh is set to PPR."
        )
        workflow = _workflow(collector=collector)
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "Yahoo league scoring is Half PPR.*set to PPR"
        ):
            workflow(
                _request(), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )
        self.assertEqual(len(collector.yahoo_scoring_calls), 1)
        self.assertEqual(len(collector.calls), 1)

    def test_outdated_extension_error_preserves_upgrade_instructions(self):
        class OutdatedCollector(_Collector):
            def open_session(self, *_args, **_kwargs):
                raise BrowserExtensionUpgradeRequired(
                    "Update the browser extension to version 0.2.0 or newer, "
                    "reload it, then reconnect."
                )

        workflow = _workflow(collector=OutdatedCollector())
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError,
            r"Update.*version 0\.2\.0 or newer.*reload.*reconnect",
        ):
            workflow(
                _request(),
                data_directory=Path(directory),
                progress=lambda _: None,
                cancelled=lambda: False,
            )

    def test_yahoo_scoring_preflight_selects_current_week_from_multiweek_plan(self):
        url = "https://football.fantasysports.yahoo.com/f1/players"
        tasks = tuple(
            PageCaptureTask(
                "yahoo", 2026, week, "visible_table", url,
                projection=ProjectionTableSpec(horizon, "PPR", (position,)),
            )
            for week, horizon, position in (
                (2, "weekly", "QB"),
                (1, "ros", "WR"),
                (1, "weekly", "RB"),
            )
        )
        selected = _yahoo_projection_task(CapturePlan(tasks), _request())
        self.assertEqual(selected.week, 1)
        self.assertEqual(selected.projection.horizon.value, "weekly")
        self.assertEqual(selected.projection.position_scope, ("RB",))

    def test_no_argument_factory_exposes_interactive_gate(self):
        workflow = create_production_weekly_collection_workflow()
        self.assertIsInstance(workflow, ProductionWeeklyCollectionWorkflow)
        self.assertIsInstance(workflow.sign_in_gate, InteractiveSignInGate)

    def test_explicit_espn_denial_uses_same_signed_in_session_once(self):
        collector = _Collector()
        collector.authenticated_result = (
            {"private_league": True},
            {"pro_teams": True},
        )

        def denied(*_args):
            raise EspnUnauthorizedError("denied")

        workflow = _workflow(collector=collector, espn_reader=denied)
        with TemporaryDirectory() as directory:
            result = workflow(
                _request(), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )

        self.assertIsNotNone(result)
        self.assertEqual(len(collector.open_calls), 1)
        self.assertEqual(collector.close_count, 1)
        self.assertEqual(len(collector.authenticated_calls), 1)
        task, runtime_url, season, league_id = collector.authenticated_calls[0]
        self.assertEqual(task.provider.value, "espn")
        self.assertEqual(task.kind, CaptureKind.VISIBLE_TABLE)
        self.assertEqual(
            runtime_url,
            "https://fantasy.espn.com/football/players/projections?leagueId=123",
        )
        self.assertEqual((season, league_id), (2026, "123"))

    def test_malformed_public_response_does_not_enter_authenticated_fallback(self):
        collector = _Collector()

        def malformed(*_args):
            raise EspnFreeReadError("ESPN returned invalid JSON data")

        workflow = _workflow(collector=collector, espn_reader=malformed)
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            WeeklyCollectionError, "invalid JSON"
        ):
            workflow(
                _request(), data_directory=Path(directory),
                progress=lambda _: None, cancelled=lambda: False,
            )
        self.assertEqual(collector.authenticated_calls, [])
        self.assertEqual(collector.close_count, 1)

    def test_authenticated_fallback_cancellation_publishes_nothing(self):
        collector = _Collector()
        collector.authenticated_error = BrowserCaptureCancelled("cancelled")

        def denied(*_args):
            raise EspnUnauthorizedError("denied")

        workflow = _workflow(collector=collector, espn_reader=denied)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(WeeklyCollectionError, "cancelled"):
                workflow(
                    _request(), data_directory=root,
                    progress=lambda _: None, cancelled=lambda: False,
                )
            self.assertFalse((root / "identity-registry.json").exists())
        self.assertEqual(len(collector.authenticated_calls), 1)


class ProductionCalibrationTests(unittest.TestCase):
    def test_gate_reports_pending_confirms_exact_provider_and_resets(self):
        gate = InteractiveSignInGate()
        fp = PageCaptureTask(
            "fantasypros", 2026, 1, "league_source",
            "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
        )
        espn = PageCaptureTask(
            "espn", 2026, 1, "visible_table",
            "https://fantasy.espn.com/football/players/projections",
            projection=ProjectionTableSpec("weekly", "PPR", ("ALL",)),
        )
        self.assertFalse(gate.is_ready(fp))
        self.assertEqual(gate.status()["pending_provider"], "fantasypros")
        with self.assertRaisesRegex(ValueError, "does not match"):
            gate.confirm("espn")
        self.assertEqual(gate.confirm("fantasypros"), "fantasypros")
        self.assertTrue(gate.is_ready(fp))
        self.assertFalse(gate.is_ready(espn))
        self.assertEqual(gate.status()["pending_provider"], "espn")
        gate.reset()
        self.assertEqual(gate.status(), {
            "pending_provider": None, "confirmed_providers": [],
        })

    def test_concrete_factory_captures_350_initial_and_101_reuse_experiments(self):
        from tests.test_methodology_reuse import fingerprint, formula

        assembled = _assembled()
        fp = fingerprint()
        evidence = SimpleNamespace(
            methodology_fingerprint=fp,
            analyzer_bundle=fp.analyzer_bundle,
            state=SimpleNamespace(snapshot_id="snapshot-1"),
        )
        object.__setattr__(assembled, "evidence", evidence)
        collector = SimpleNamespace(calls=[])

        def collect(plan, options, **kwargs):
            collector.calls.append((plan, options, kwargs))
            return ("captured",)

        collector.collect = collect
        gate = InteractiveSignInGate()
        with TemporaryDirectory() as directory:
            context = CalibrationCaptureContext(
                collector,
                BrowserCaptureOptions(
                    Path(directory) / "profile", headed=True,
                    overall_timeout_ms=3_600_000, action_delay_ms=200,
                ),
                gate,
                SimpleNamespace(is_set=lambda: False),
                2026,
                1,
                lambda: NOW,
            )
            initial, reuse = object(), object()
            batches = {
                initial: SimpleNamespace(plan="initial-plan"),
                reuse: SimpleNamespace(plan="reuse-plan"),
            }
            prepared = []

            def prepare(*_args, **kwargs):
                prepared.append(kwargs)
                return initial if kwargs["training_experiment_count"] == 250 else reuse

            exact_formula = formula()
            report = object()
            with (
                patch("trade_snapshot.production_calibration.prepare_weekly_calibration_session", prepare),
                patch("trade_snapshot.production_calibration.build_calibration_capture_batch", side_effect=lambda session, **_: batches[session]),
                patch("trade_snapshot.production_calibration.observations_from_calibration_artifacts", return_value={"observation": object()}),
                patch("trade_snapshot.production_calibration.complete_calibration_session", return_value=exact_formula),
                patch("trade_snapshot.production_calibration.verification_report_from_calibration_session", return_value=report),
            ):
                callbacks = BrowserCalibrationFactory()(assembled, "canonical-team-1", context)
                self.assertIs(callbacks.calibrate(evidence, fp), exact_formula)
                self.assertIs(callbacks.verify_reuse(evidence, exact_formula, fp), report)

        self.assertEqual(
            [(row["training_experiment_count"], row["held_out_experiment_count"])
             for row in prepared],
            [(250, 100), (1, 100)],
        )
        self.assertEqual([row[0] for row in collector.calls], ["initial-plan", "reuse-plan"])
        self.assertTrue(all(row[1].action_delay_ms >= 200 for row in collector.calls))
        self.assertTrue(all(row[2]["sign_in_gate"] is gate for row in collector.calls))


class EspnFreeReadClientTests(unittest.TestCase):
    def test_uses_two_exact_cookie_free_bounded_reads(self):
        calls = []

        league_payload = {
            "id": 123,
            "seasonId": 2026,
            "members": [{"displayName": "PRIVATE OWNER"}],
            "teams": [{"id": 1, "owners": ["PRIVATE MEMBER"]}],
        }
        pro_team_payload = {
            "display": True,
            "settings": {
                "proTeams": [
                    {
                        "id": 1,
                        "abbrev": "ATL",
                        "byeWeek": 6,
                        "location": "Atlanta",
                        "name": "Falcons",
                        "universeId": 1,
                        "proGamesByScoringPeriod": {
                            "1": [
                                {
                                    "id": 1001,
                                    "scoringPeriodId": 1,
                                    "awayProTeamId": 1,
                                    "homeProTeamId": 2,
                                    "date": 1788000000000,
                                    "startTimeTBD": False,
                                    "statsOfficial": False,
                                    "validForLocking": True,
                                    "private": "SECRET",
                                }
                            ]
                        },
                        "teamPlayersByPosition": {"1": ["PRIVATE PLAYER"]},
                        "private": "SECRET",
                    }
                ]
            },
            "private": "SECRET",
        }

        def opener(request, *, timeout):
            calls.append((request, timeout))
            payload = (
                pro_team_payload
                if "proTeamSchedules_wl" in request.full_url
                else league_payload
            )
            return _Response(request.full_url, json.dumps(payload).encode("utf-8"))

        client = EspnFreeReadClient(timeout_seconds=7, maximum_bytes=2048, opener=opener)
        league, pro_teams = client(2026, "123", lambda: False)

        self.assertEqual(league["id"], 123)
        self.assertEqual(league["seasonId"], 2026)
        self.assertEqual(league["teams"][0]["id"], 1)
        self.assertTrue(pro_teams["display"])
        projected_team = pro_teams["settings"]["proTeams"][0]
        self.assertEqual(projected_team["id"], 1)
        self.assertEqual(projected_team["abbrev"], "ATL")
        self.assertEqual(projected_team["teamPlayersByPosition"], {})
        self.assertEqual(
            projected_team["proGamesByScoringPeriod"]["1"][0]["id"], 1001
        )
        self.assertNotIn("PRIVATE", repr((league, pro_teams)))
        self.assertNotIn("SECRET", repr((league, pro_teams)))
        self.assertEqual(len(calls), 2)
        first = calls[0][0]
        self.assertEqual(first.get_method(), "GET")
        self.assertIn("/seasons/2026/segments/0/leagues/123?", first.full_url)
        self.assertEqual(first.full_url.count("view="), 6)
        self.assertIn("view=mTransactions2", first.full_url)
        self.assertTrue(calls[1][0].full_url.endswith("?view=proTeamSchedules_wl"))
        headers = {key.casefold() for key, _ in first.header_items()}
        self.assertFalse({"cookie", "authorization"} & headers)
        self.assertIn("x-fantasy-filter", headers)
        self.assertEqual(
            dict(first.header_items())["X-fantasy-filter"],
            '{"transactions":{"limit":1000}}',
        )
        second_headers = {key.casefold() for key, _ in calls[1][0].header_items()}
        self.assertNotIn("x-fantasy-filter", second_headers)
        self.assertEqual({timeout for _, timeout in calls}, {7.0})

    def test_public_draft_read_uses_one_exact_cookie_free_endpoint(self):
        calls = []

        def opener(request, *, timeout):
            calls.append((request, timeout))
            return _Response(request.full_url, b'{"draftDetail":{"picks":[]}}')

        client = EspnFreeReadClient(timeout_seconds=5, maximum_bytes=2048, opener=opener)
        payload = client.read_draft(2026, "123", lambda: False)

        self.assertEqual(payload, {"draftDetail": {"picks": []}})
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(
            request.full_url,
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            "seasons/2026/segments/0/leagues/123?view=mDraftDetail",
        )
        self.assertEqual(timeout, 5.0)
        self.assertEqual(request.get_method(), "GET")
        headers = {key.casefold() for key, _ in request.header_items()}
        self.assertFalse({"cookie", "authorization", "x-fantasy-filter"} & headers)

    def test_public_draft_read_validates_only_numeric_source_coordinates(self):
        client = EspnFreeReadClient(
            opener=lambda *_args, **_kwargs: self.fail("network should not be reached")
        )
        for season, league_id in ((2011, "123"), (2026, "0"), (2026, "abc")):
            with self.subTest(season=season, league_id=league_id), self.assertRaises(ValueError):
                client.read_draft(season, league_id, lambda: False)

    def test_distinguishes_only_an_explicit_access_denial(self):
        def denied(request, *, timeout):
            raise HTTPError(request.full_url, 401, "denied", {}, None)

        with self.assertRaises(EspnUnauthorizedError):
            EspnFreeReadClient(opener=denied)(2026, "123", lambda: False)

        malformed = _Response("EXPECTED", b"not-json")

        def malformed_response(request, *, timeout):
            malformed.url = request.full_url
            return malformed

        with self.assertRaises(EspnFreeReadError) as raised:
            EspnFreeReadClient(opener=malformed_response)(
                2026, "123", lambda: False
            )
        self.assertNotIsInstance(raised.exception, EspnUnauthorizedError)

        redirected_denial = _Response("https://evil.test/login", b"{}")
        redirected_denial.status = 401
        with self.assertRaises(EspnFreeReadError) as raised:
            EspnFreeReadClient(opener=lambda *_args, **_kwargs: redirected_denial)(
                2026, "123", lambda: False
            )
        self.assertNotIsInstance(raised.exception, EspnUnauthorizedError)

    def test_rejects_redirects_oversize_duplicate_keys_and_cancellation(self):
        cases = (
            _Response("https://evil.test/redirect", b"{}"),
            _Response("EXPECTED", b"x" * 2049),
            _Response("EXPECTED", b'{"same":1,"same":2}'),
        )
        for response in cases:
            def opener(request, *, timeout, response=response):
                if response.url == "EXPECTED":
                    response.url = request.full_url
                return response

            with self.subTest(body=response.body[:20]), self.assertRaises(EspnFreeReadError):
                EspnFreeReadClient(maximum_bytes=2048, opener=opener)(
                    2026, "123", lambda: False
                )
        with self.assertRaisesRegex(EspnFreeReadError, "cancelled"):
            EspnFreeReadClient(opener=lambda *_args, **_kwargs: None)(
                2026, "123", lambda: True
            )

    def test_authenticated_reader_keeps_cookie_header_inside_bounded_transport(self):
        context = _CookieContext()
        calls = []

        league_payload = {
            "id": 123,
            "members": [{"displayName": "PRIVATE OWNER"}],
        }
        pro_team_payload = {
            "settings": {"proTeams": [{"id": 1, "abbrev": "ATL"}]}
        }

        def transport(request, *, timeout):
            calls.append(request)
            payload = (
                pro_team_payload
                if "proTeamSchedules_wl" in request.full_url
                else league_payload
            )
            return _Response(request.full_url, json.dumps(payload).encode("utf-8"))

        result = read_authenticated_espn_json(
            context,
            2026,
            "123",
            4000,
            2048,
            lambda: False,
            transport=transport,
        )

        self.assertEqual(result[0]["id"], 123)
        self.assertEqual(
            result[1],
            {
                "settings": {
                    "proTeams": [
                        {
                            "id": 1,
                            "abbrev": "ATL",
                            "proGamesByScoringPeriod": {},
                        }
                    ]
                }
            },
        )
        self.assertEqual(tuple(context.urls), EspnFreeReadClient.urls(2026, "123"))
        self.assertEqual(len(calls), 2)
        for request in calls:
            headers = dict(request.header_items())
            self.assertEqual(headers["Cookie"], "espn_s2=worker-only-secret")
        self.assertNotIn("worker-only-secret", repr(result))
        self.assertNotIn("Cookie", repr(result))
        self.assertNotIn("PRIVATE OWNER", repr(result))

        with self.assertRaisesRegex(EspnFreeReadError, "cancelled"):
            read_authenticated_espn_json(
                context,
                2026,
                "123",
                4000,
                2048,
                lambda: True,
                transport=lambda *_args, **_kwargs: self.fail("transport was called"),
            )


class _Response:
    status = 200

    def __init__(self, url, body, headers=None):
        self.url = url
        self.body = body
        self.headers = headers or {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, maximum):
        return self.body[:maximum]


class _CookieContext:
    def __init__(self):
        self.urls = []

    def cookies(self, urls):
        self.urls.extend(urls)
        return [{"name": "espn_s2", "value": "worker-only-secret"}]


def _request(
    host_id="123",
    *,
    expected_team_count=None,
    allow_surrogate_power=False,
    use_fantasypros=True,
    refresh_public_player_data=False,
):
    return WeeklyCollectionRequest(
        season=2026,
        week=1,
        scoring="PPR",
        expected_team_count=expected_team_count,
        host_league_url=(
            f"https://fantasy.espn.com/football/league?leagueId={host_id}"
        ),
        yahoo_projection_league_url=(
            "https://football.fantasysports.yahoo.com/f1/456/players"
        ),
        allow_surrogate_power=allow_surrogate_power,
        use_fantasypros=use_fantasypros,
        refresh_public_player_data=refresh_public_player_data,
    )


def _league_artifact(task):
    sources = []
    for source in league_sources():
        record = source.to_record()
        if source.source is LeagueSourceKind.BOOTSTRAP:
            record["body"]["payload"]["league"].update({
                "host": "ESPN", "host_league_id": "123", "team_id": "1",
            })
        sources.append(LeagueSource(record["source"], record["body"]))
    return FantasyProsLeagueArtifact(
        task.task_id, task.provider, task.season, task.week, task.kind,
        "2026-09-01T00:00:00Z", 2, True,
        CURRENT_BUNDLE_FINGERPRINT.url, CURRENT_BUNDLE_FINGERPRINT.sha256, sources,
    )


def _small_plan(**dimensions):
    season, week, scoring = (
        dimensions["season"], dimensions["as_of_week"], dimensions["scoring"]
    )
    projection = ProjectionTableSpec("weekly", scoring, ("ALL",))
    return CapturePlan((
        PageCaptureTask(
            "fantasypros", season, week, "league_source",
            "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
        ),
        PageCaptureTask(
            "espn", season, week, "visible_table",
            "https://fantasy.espn.com/football/players/projections",
            projection=projection,
        ),
        PageCaptureTask(
            "fantasypros", season, week, "visible_table",
            "https://www.fantasypros.com/nfl/projections/rb.php",
            projection=projection,
        ),
        PageCaptureTask(
            "yahoo", season, week, "visible_table",
            "https://football.fantasysports.yahoo.com/f1/players",
            projection=projection,
        ),
        FantasyProsECRTask(
            season, week, "weekly", scoring, ("RB",), (), None,
            "https://www.fantasypros.com/nfl/rankings/ppr-rb.php",
        ),
    ))


def _small_independent_plan(**dimensions):
    return CapturePlan(
        task for task in _small_plan(**dimensions).tasks
        if task.provider.value in {"espn", "yahoo"}
    )


def _artifact(task):
    if isinstance(task, FantasyProsECRTask):
        return FantasyProsECRArtifact.from_task(
            task,
            expert_ids=("1",),
            last_updated_text="today",
            last_updated_at="2026-09-01T00:00:00Z",
            captured_at="2026-09-01T01:00:00Z",
            rankings=(ECRRankingRow(
                "1001", "Player 1001", "ARI", "RB", 1, 1, 1, 1, 0, "RB1",
                {"ECR": "1"},
            ),),
        )
    links = {
        "fantasypros": "https://www.fantasypros.com/nfl/players/player-one.php",
        "espn": "https://www.espn.com/nfl/player/_/id/201/player-one",
        "yahoo": "https://sports.yahoo.com/nfl/players/301/",
    }
    table = VisibleTable((
        (VisibleTableCell("PLAYER"), VisibleTableCell("FPTS")),
        (VisibleTableCell("Player One", (links[task.provider.value],)),
         VisibleTableCell("12.0")),
    ))
    return GenericTableArtifact(
        task.task_id, task.provider, task.season, task.week, task.kind,
        "2026-09-01T01:00:00Z", task.projection.horizon,
        task.projection.scoring, task.projection.position_scope,
        "Week 1 PPR", 1, True, (table,),
    )


def _host_snapshot(**changes):
    values = {
        "source_provider": "espn",
        "source_league_id": "123",
        "season": 2026,
        "first_remaining_week": 1,
        "expected_team_count": 2,
        "players": (SimpleNamespace(position="RB"),),
        "playoff_rules": SimpleNamespace(regular_season_end_week=2),
        "scoring_profile": SimpleNamespace(
            platform="espn",
            settings={"scoring_settings": {"playerRankType": "PPR"}},
        ),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _assembled():
    value = object.__new__(AssembledWeeklyEvidence)
    object.__setattr__(value, "evidence", object())
    object.__setattr__(value, "fantasypros_team_ids", {
        "canonical-team-1": "1", "canonical-team-2": "2",
    })
    object.__setattr__(value, "fantasypros_player_ids", {
        "canonical-player-1": "1001", "canonical-player-2": "1002",
    })
    object.__setattr__(value, "identities", IdentityRegistry(()))
    object.__setattr__(
        value,
        "player_lab_projections",
        _empty_lab_snapshot(engine_bundle()),
    )
    return value


def _history_pair(bundle):
    league_key = make_league_key("espn", "123")
    capture = LeagueHistoryCapture(
        league_key=league_key,
        season=bundle.state.season,
        captured_at=NOW,
        coverage_start=NOW,
        coverage_end=NOW,
        transaction_history_complete=True,
        roster_complete=False,
        lineup_complete=False,
        teams=tuple(HistoryTeam(team.team_id, team.name) for team in bundle.state.teams),
        transactions=(),
        rosters=tuple(
            HistoryTeamRoster(roster.team_id, ()) for roster in bundle.rosters
        ),
    )
    return capture, HistoryBundleBinding(
        league_key,
        bundle.state.season,
        bundle.bundle_id,
        NOW,
    )


def _empty_lab_snapshot(bundle):
    return PlayerLabProjectionSnapshot(
        league_snapshot_id=bundle.state.snapshot_id,
        scoring_profile_id=bundle.state.scoring_profile_id,
        season=bundle.state.season,
        as_of_week=bundle.state.first_remaining_week,
        remaining_weeks=bundle.state.remaining_regular_season_weeks,
        provider_names=("espn",),
    )


def _workflow(
    *,
    collector,
    host=None,
    assembler=None,
    refresher=None,
    espn_reader=None,
    public_player_reader=None,
    profile_builder=None,
    independent_plan_builder=None,
    independent_assembler=None,
):
    assembled = _assembled()
    return ProductionWeeklyCollectionWorkflow(
        sign_in_gate=_Gate(),
        calibration_factory=lambda *_: CalibrationCallbacks(lambda *_: None, lambda *_: None),
        collector=collector,
        espn_reader=espn_reader or (lambda *_: ({}, {})),
        host_adapter=lambda *_args, **_kwargs: host or _host_snapshot(),
        schedule_adapter=lambda *_args, **_kwargs: SimpleNamespace(season=2026),
        plan_builder=_small_plan,
        independent_plan_builder=(
            independent_plan_builder or _small_independent_plan
        ),
        independent_assembler=(
            independent_assembler
            or (lambda *_args, **_kwargs: None)
        ),
        assembler=assembler or (lambda **_kwargs: assembled),
        public_player_reader=(
            public_player_reader or _public_unavailable
        ),
        profile_builder=profile_builder or (lambda **_kwargs: None),
        refresher=refresher or (
            lambda *_args, **_kwargs: SimpleNamespace(bundle=engine_bundle())
        ),
        activity_adapter=lambda *_args, **_kwargs: object(),
        history_adapter=(
            lambda _activity, _assembled, bundle, **_kwargs: _history_pair(bundle)
        ),
        now=lambda: NOW,
    )


def _schema_suffix():
    from trade_snapshot.capture_schema import ANALYZER_RESPONSE_SCHEMA_FINGERPRINT
    return ANALYZER_RESPONSE_SCHEMA_FINGERPRINT.split("_", 1)[1]


if __name__ == "__main__":
    unittest.main()
