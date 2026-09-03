from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tests.capture_fixtures import league_sources
from tests.ecr_fixtures import ecr_source_details
from tests.test_engine_bundle import engine_bundle
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
    ProjectionNotPublished,
    YahooScoringError,
    YahooScoringMismatch,
)
from trade_snapshot.identity import IdentityRegistry
from trade_snapshot.identity_io import load_identity_registry
from trade_snapshot.production_calibration import (
    BrowserCalibrationFactory,
    CalibrationCaptureContext,
    InteractiveSignInGate,
)
from trade_snapshot.calibration_workflow import CalibrationNotExact
from trade_snapshot.methodology_reuse import FormulaAction, FormulaReuseDecision
from trade_snapshot.production_collection import (
    CalibrationCallbacks,
    ProductionWeeklyCollectionWorkflow,
    _available_projection_ensemble,
    _collect_remaining_sources,
    _source_artifacts,
    _yahoo_projection_task,
    create_production_weekly_collection_workflow,
)
from trade_snapshot.source_plan import build_weekly_source_plan
from trade_snapshot.projection_source import ProjectionAttemptStatus
from trade_snapshot.weekly_assembly import AssembledWeeklyEvidence
from trade_snapshot.weekly_collection import (
    WeeklyCollectionError,
    WeeklyCollectionRequest,
)
from trade_snapshot.weekly_refresh import CalibrationRequired


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


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
    def test_plan_records_unpublished_and_optional_failures_without_losing_quorum(self):
        complete = build_weekly_source_plan(
            season=2026,
            as_of_week=1,
            remaining_weeks=(1, 2),
            scoring="PPR",
            player_positions=("RB",),
        )
        plan = CapturePlan(
            task for task in complete.tasks if task.kind is not CaptureKind.LEAGUE_SOURCE
        )

        class OutcomeCollector(_Collector):
            def collect(self, task_plan, options, **kwargs):
                self.calls.append((task_plan, options, kwargs))
                task = task_plan.tasks[0]
                if (
                    isinstance(task, PageCaptureTask)
                    and task.provider.value == "fantasypros"
                    and task.week == 2
                ):
                    raise ProjectionNotPublished("sanitized unpublished page")
                if isinstance(task, PageCaptureTask) and task.provider.value == "espn":
                    raise BrowserCaptureError("sanitized unsupported layout")
                return (_artifact(task),)

        collector = OutcomeCollector()
        rows, attempts = _collect_remaining_sources(
            collector,
            plan,
            BrowserCaptureOptions(Path("profile")),
            object(),
            _Gate(),
            {},
            first_remaining_week=1,
            attempt_clock=lambda: NOW,
        )
        projections, ecr = _source_artifacts(rows, plan, "PPR", attempts)
        ensemble = _available_projection_ensemble(projections)

        self.assertEqual(len(ecr), 2)
        self.assertEqual(
            {row.provider.value for row in projections},
            {"fantasypros", "yahoo"},
        )
        self.assertEqual(
            tuple(row.provider for row in ensemble.provider_weights),
            ("fantasypros", "yahoo"),
        )
        self.assertEqual(
            {status: sum(row.status is status for row in attempts) for status in ProjectionAttemptStatus},
            {
                ProjectionAttemptStatus.CAPTURED: 3,
                ProjectionAttemptStatus.NOT_PUBLISHED: 1,
                ProjectionAttemptStatus.UNAVAILABLE: 2,
            },
        )
        unsuccessful = tuple(
            row for row in attempts if row.status is not ProjectionAttemptStatus.CAPTURED
        )
        self.assertTrue(all(row.attempted_at == NOW for row in unsuccessful))

    def test_yahoo_preflight_failure_becomes_attempts_when_two_sources_remain(self):
        collector = _Collector()
        collector.yahoo_scoring_error = YahooScoringError(
            "sanitized Yahoo settings page failure"
        )
        assembled = _assembled()
        recorded = {}

        def assembler(**kwargs):
            recorded.update(kwargs)
            return assembled

        def plan_builder(**dimensions):
            season = dimensions["season"]
            week = dimensions["as_of_week"]
            scoring = dimensions["scoring"]
            projection = ProjectionTableSpec("weekly", scoring, ("RB",))
            return CapturePlan((
                PageCaptureTask(
                    "fantasypros", season, week, "league_source",
                    "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
                ),
                PageCaptureTask(
                    "fantasypros", season, week, "visible_table",
                    "https://www.fantasypros.com/nfl/projections/rb.php"
                    "?week=1&scoring=PPR",
                    projection=projection,
                ),
                PageCaptureTask(
                    "espn", season, week, "visible_table",
                    "https://fantasy.espn.com/football/players/projections",
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

        workflow = _workflow(collector=collector, assembler=assembler)
        workflow._plan_builder = plan_builder
        with TemporaryDirectory() as directory:
            workflow(
                _request(),
                data_directory=Path(directory),
                progress=lambda _: None,
                cancelled=lambda: False,
            )

        attempts = recorded["projection_source_attempts"]
        yahoo = tuple(row for row in attempts if row.provider.value == "yahoo")
        self.assertEqual(len(yahoo), 1)
        self.assertIs(yahoo[0].status, ProjectionAttemptStatus.UNAVAILABLE)
        self.assertEqual(yahoo[0].attempted_at, NOW)
        self.assertEqual(
            {row.provider.value for row in recorded["projection_artifacts"]},
            {"fantasypros", "espn"},
        )
        captured_providers = {
            call[0].tasks[0].provider.value
            for call in collector.calls[1:]
        }
        self.assertNotIn("yahoo", captured_providers)

    def test_projection_plan_covers_rostered_and_empty_starting_positions(self):
        captured = {}

        def plan_builder(**dimensions):
            captured.update(dimensions)
            return _small_plan(**dimensions)

        workflow = _workflow(collector=_Collector())
        workflow._plan_builder = plan_builder
        host = _host_snapshot(
            players=(SimpleNamespace(position="QB"), SimpleNamespace(position="DL")),
            roster_rules=SimpleNamespace(starting_lineup_slots=("QB", "K")),
        )

        workflow._remaining_plan(_request(), host)

        self.assertEqual(tuple(captured["remaining_weeks"]), tuple(range(1, 19)))
        self.assertEqual(set(captured["player_positions"]), {"QB", "DL", "K"})

    def test_default_collection_plan_attempts_every_remaining_fantasypros_week(self):
        workflow = _workflow(collector=_Collector())
        workflow._plan_builder = build_weekly_source_plan

        plan = workflow._remaining_plan(_request(), _host_snapshot())

        fantasypros = tuple(
            task
            for task in plan.tasks
            if isinstance(task, PageCaptureTask)
            and task.provider.value == "fantasypros"
        )
        self.assertEqual(
            {(task.week, task.projection.position_scope) for task in fantasypros},
            {(week, ("RB",)) for week in range(1, 19)},
        )
        optional_weekly = tuple(
            task
            for task in plan.tasks
            if isinstance(task, PageCaptureTask)
            and task.provider.value in {"espn", "yahoo"}
            and task.projection.horizon.value == "weekly"
        )
        self.assertEqual(
            {(task.provider.value, task.week) for task in optional_weekly},
            {("espn", 1), ("yahoo", 1)},
        )

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

        workflow = ProductionWeeklyCollectionWorkflow(
            sign_in_gate=_Gate(),
            calibration_factory=calibration_factory,
            collector=collector,
            espn_reader=reader,
            host_adapter=adapter,
            schedule_adapter=schedule_adapter,
            plan_builder=_small_plan,
            assembler=assembler,
            refresher=refresher,
            now=lambda: NOW,
        )
        progress = []
        with TemporaryDirectory() as directory:
            data_root = Path(directory)
            identity_path = data_root / "identity-registry.json"
            result = workflow(
                _request(allow_surrogate_power=True), data_directory=data_root,
                progress=progress.append, cancelled=lambda: False,
            )
            self.assertEqual(load_identity_registry(identity_path), assembled.identities)
            public_captures = tuple(
                (data_root / "raw-captures" / "public").glob("*.json")
            )
            private_captures = tuple(
                (data_root / "raw-captures" / "private-leagues").glob("*/*.json")
            )
            self.assertEqual(len(public_captures), 3)
            self.assertEqual(len(private_captures), 1)
            self.assertEqual(
                {path.stem for path in public_captures},
                {
                    artifact.artifact_id
                    for artifact in (
                        *records["assembly"]["projection_artifacts"],
                        *records["assembly"]["ecr_artifacts"],
                    )
                },
            )
            self.assertEqual(
                private_captures[0].stem,
                records["assembly"]["fantasypros_league"].artifact_id,
            )
            self.assertTrue(
                all(path.stem.startswith(("captable_", "capecr_")) for path in public_captures)
            )
            self.assertTrue(private_captures[0].stem.startswith("capleague_"))
            self.assertRegex(private_captures[0].parent.name, r"^league_[0-9a-f]{32}$")

        self.assertIs(result, bundle)
        self.assertEqual(records["read"][:2], (2026, "123"))
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
        self.assertEqual(len(collector.calls), 4)
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
        first, *source_calls = collector.calls
        self.assertTrue(all(first[1] is row[1] for row in source_calls))
        self.assertTrue(first[1].headed)
        self.assertEqual(first[1].action_delay_ms, 200)
        self.assertLessEqual(first[1].overall_timeout_ms, 3_600_000)
        self.assertTrue(all(
            first[2]["sign_in_gate"] is row[2]["sign_in_gate"]
            for row in source_calls
        ))
        bindings = {
            task_id: url
            for row in source_calls
            for task_id, url in (row[2]["navigation_bindings"] or {}).items()
        }
        by_provider = {
            row[0].tasks[0].provider.value: row[0].tasks[0].task_id
            for row in source_calls
            if isinstance(row[0].tasks[0], PageCaptureTask)
        }
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
        self.assertEqual(len(collector.calls), 4)

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

    def test_calibration_failures_describe_validation_scope_without_exact_claims(self):
        failures = (
            (
                CalibrationRequired(FormulaReuseDecision(
                    FormulaAction.RECALIBRATE,
                    ("weekly calibration required",),
                    "methodology-fingerprint",
                )),
                False,
                "blind-holdout validated",
            ),
            (
                CalibrationNotExact(
                    None,
                    None,
                    surrogate_eligible=True,
                ),
                False,
                "did not pass the blind-holdout validation gate",
            ),
            (
                CalibrationNotExact(
                    None,
                    None,
                    surrogate_eligible=False,
                ),
                True,
                "neither the blind-holdout validation gate",
            ),
        )
        for failure, allow_surrogate, expected in failures:
            with self.subTest(expected=expected), TemporaryDirectory() as directory:
                def failed_refresh(*_args, failure=failure, **_kwargs):
                    raise failure

                workflow = _workflow(
                    collector=_Collector(),
                    refresher=failed_refresh,
                )
                with self.assertRaises(WeeklyCollectionError) as raised:
                    workflow(
                        _request(allow_surrogate_power=allow_surrogate),
                        data_directory=Path(directory),
                        progress=lambda _: None,
                        cancelled=lambda: False,
                    )
                message = str(raised.exception)
                self.assertIn(expected, message)
                self.assertNotIn("exact FantasyPros", message)
                self.assertNotIn("exact blind replication", message)

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

    def test_yahoo_scoring_mismatch_is_actionable_and_stops_before_projections(self):
        collector = _Collector()
        collector.yahoo_scoring_error = YahooScoringMismatch(
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

        def opener(request, *, timeout):
            calls.append((request, timeout))
            return _Response(request.full_url, b'{"ok":true}')

        client = EspnFreeReadClient(timeout_seconds=7, maximum_bytes=2048, opener=opener)
        league, pro_teams = client(2026, "123", lambda: False)

        self.assertEqual(league, {"ok": True})
        self.assertEqual(pro_teams, {"ok": True})
        self.assertEqual(len(calls), 2)
        first = calls[0][0]
        self.assertEqual(first.get_method(), "GET")
        self.assertIn("/seasons/2026/segments/0/leagues/123?", first.full_url)
        self.assertEqual(first.full_url.count("view="), 5)
        self.assertTrue(calls[1][0].full_url.endswith("?view=proTeamSchedules_wl"))
        headers = {key.casefold() for key, _ in first.header_items()}
        self.assertFalse({"cookie", "authorization", "x-fantasy-filter"} & headers)
        self.assertEqual({timeout for _, timeout in calls}, {7.0})

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

        def transport(request, *, timeout):
            calls.append(request)
            return _Response(request.full_url, b'{"ok":true}')

        result = read_authenticated_espn_json(
            context,
            2026,
            "123",
            4000,
            2048,
            lambda: False,
            transport=transport,
        )

        self.assertEqual(result, ({"ok": True}, {"ok": True}))
        self.assertEqual(tuple(context.urls), EspnFreeReadClient.urls(2026, "123"))
        self.assertEqual(len(calls), 2)
        for request in calls:
            headers = dict(request.header_items())
            self.assertEqual(headers["Cookie"], "espn_s2=worker-only-secret")
        self.assertNotIn("worker-only-secret", repr(result))
        self.assertNotIn("Cookie", repr(result))

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
    host_id="123", *, expected_team_count=None, allow_surrogate_power=False
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
            "yahoo", season, week, "visible_table",
            "https://football.fantasysports.yahoo.com/f1/players",
            projection=projection,
        ),
        FantasyProsECRTask(
            season, week, "weekly", scoring, ("RB",), (), None,
            "https://www.fantasypros.com/nfl/rankings/ppr-rb.php",
        ),
    ))


def _artifact(task):
    if isinstance(task, FantasyProsECRTask):
        return FantasyProsECRArtifact.from_task(
            task,
            expert_ids=("1",),
            source_scoring=task.source_scoring,
            last_updated_text="today",
            last_updated_at="2026-09-01T00:00:00Z",
            captured_at="2026-09-01T01:00:00Z",
            source_details=ecr_source_details(
                season=task.season,
                week=task.week,
                horizon=task.horizon.value,
                position=task.position_scope[0],
                source_scoring=task.source_scoring,
            ),
            rankings=(ECRRankingRow(
                "1001", "Player 1001", "ARI", "RB", 1, 1, 1, 1, 0, "RB1",
                {"ECR": "1"},
            ),),
        )
    links = {
        "fantasypros": "https://www.fantasypros.com/nfl/projections/player-one.php",
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
        "roster_rules": SimpleNamespace(starting_lineup_slots=("RB",)),
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
    return value


def _workflow(
    *, collector, host=None, assembler=None, refresher=None, espn_reader=None
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
        assembler=assembler or (lambda **_kwargs: assembled),
        refresher=refresher or (
            lambda *_args, **_kwargs: SimpleNamespace(bundle=engine_bundle())
        ),
        now=lambda: NOW,
    )


def _schema_suffix():
    from trade_snapshot.capture_schema import ANALYZER_RESPONSE_SCHEMA_FINGERPRINT
    return ANALYZER_RESPONSE_SCHEMA_FINGERPRINT.split("_", 1)[1]


if __name__ == "__main__":
    unittest.main()
