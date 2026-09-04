from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tests.test_engine_bundle import engine_bundle
from tests.test_three_way_search import components as three_way_components
from tests.test_surrogate_disclosure import surrogate_bundle
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeam,
    HistoryTeamRoster,
    LeagueHistoryCapture,
    LeagueHistoryStore,
)
from trade_snapshot.league_state import Tiebreaker
from trade_snapshot.three_way_search import ThreeWaySearchOutcome
from trade_snapshot.trade_impact import prepare_season_baseline
from trade_snapshot.weekly_collection import (
    LEAGUE_HISTORY_FILENAME,
    WeeklyHistoryAttempt,
    WeeklyHistoryReason,
)


def payload(bundle_id):
    return {
        "bundle_id": bundle_id,
        "primary_team_id": "primary",
        "counterparty_team_ids": [],
        "min_outgoing": 1,
        "max_outgoing": 1,
        "min_incoming": 1,
        "max_incoming": 1,
        "max_total_players": 2,
        "max_imbalance": 0,
        "balanced_only": True,
        "skip_fantasypros_small_trades": False,
        "locked_player_ids": [],
        "require_no_drops": True,
        "minimum_power_delta": -100,
        "checkpoint_interval": 1,
        "scenario_count": 3,
        "seed": 19,
    }


def player_filter(player_id, mode="include"):
    return {
        "player_ids": [player_id],
        "player_mode": mode,
        "positions": [],
        "position_mode": None,
    }


def filter_expression(operator, *operands):
    return {"operator": operator, "operands": list(operands)}


def wait_for_job(service, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service.job(job_id)
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(0.01)
    raise AssertionError("search job did not finish")


class LocalAppServiceTests(unittest.TestCase):
    def test_catalog_labels_legacy_bundles_with_a_rescan_recovery(self):
        legacy = engine_bundle().to_record()
        legacy["schema_version"] = 6
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            (service.bundle_directory / "legacy.json").write_text(
                json.dumps(legacy),
                encoding="utf-8",
            )

            catalog = service.bundle_catalog()

        self.assertEqual(catalog["bundles"][0]["status"], "legacy_requires_rescan")
        self.assertEqual(catalog["bundles"][0]["schema_version"], 6)
        self.assertEqual(catalog["readiness"]["legacy_bundle_count"], 1)
        self.assertEqual(catalog["readiness"]["invalid_bundle_count"], 0)
        self.assertIn("Scan the league again", catalog["readiness"]["message"])

    def test_catalog_reports_every_incompatible_saved_bundle_alongside_ready_data(self):
        bundle = engine_bundle()
        legacy = bundle.to_record()
        legacy["schema_version"] = 7
        future = bundle.to_record()
        future["schema_version"] = 9
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            for filename, record in (
                ("legacy.json", legacy),
                ("future.json", future),
                ("invalid.json", {}),
            ):
                (service.bundle_directory / filename).write_text(
                    json.dumps(record),
                    encoding="utf-8",
                )

            catalog = service.bundle_catalog()

        status_by_file = {
            row.get("file"): row["status"] for row in catalog["bundles"]
        }
        self.assertEqual(status_by_file["legacy.json"], "legacy_requires_rescan")
        self.assertEqual(status_by_file["future.json"], "requires_app_update")
        self.assertEqual(status_by_file["invalid.json"], "invalid")
        self.assertTrue(catalog["readiness"]["ready"])
        self.assertEqual(catalog["readiness"]["ready_bundle_count"], 1)
        self.assertEqual(catalog["readiness"]["legacy_bundle_count"], 1)
        self.assertEqual(
            catalog["readiness"]["requires_app_update_bundle_count"], 1
        )
        self.assertEqual(catalog["readiness"]["invalid_bundle_count"], 1)
        self.assertIn(
            "Older-format saved weekly bundles: 1",
            catalog["readiness"]["message"],
        )
        self.assertIn(
            "Saved weekly bundles requiring a newer application: 1",
            catalog["readiness"]["message"],
        )
        self.assertIn(
            "Saved weekly bundles that failed validation: 1",
            catalog["readiness"]["message"],
        )

    def test_trade_timing_is_cached_per_primary_team(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            first = service.trade_timing(bundle.bundle_id, "primary")
            with patch(
                "trade_snapshot.app_service.build_trade_timing",
                side_effect=AssertionError("cached timing must not be rebuilt"),
            ):
                second = service.trade_timing(bundle.bundle_id, "primary")

        self.assertIs(first, second)
        self.assertEqual(first["primary_team_id"], "primary")
        self.assertFalse(first["methodology"]["manager_acceptance_modeled"])

    def test_not_ready_bundle_can_be_inspected_but_cannot_run_season_features(self):
        bundle = engine_bundle()
        playoff_rules = replace(
            bundle.state.playoff_rules,
            tiebreaker_order=(
                Tiebreaker.DIVISION_RECORD,
                Tiebreaker.RANDOM_DRAW,
            ),
        )
        bundle = replace(
            bundle,
            state=replace(bundle.state, playoff_rules=playoff_rules),
        )
        request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            estimate = service.estimate_search(request)
            with self.assertRaisesRegex(ValueError, "trade search is not ready"):
                service.start_search(request)
            with self.assertRaisesRegex(ValueError, "league dashboard is not ready"):
                service.league_dashboard(bundle.bundle_id)

        self.assertEqual(estimate["data_readiness"]["status"], "not_ready")
        self.assertIn(
            "ready expected-standings evidence",
            estimate["data_readiness"]["missing"],
        )

    def test_gm_insights_supports_old_bundles_and_refreshes_for_new_history(self):
        bundle = engine_bundle()
        captured_at = bundle.source_manifest.host_captured_at
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
            host_snapshot_id=bundle.source_manifest.host_snapshot_id,
        )
        binding = HistoryBundleBinding(
            league_key,
            bundle.state.season,
            bundle.bundle_id,
            captured_at,
            bundle.source_manifest.host_snapshot_id,
            bundle.source_manifest.host_captured_at,
            capture.capture_id,
            capture.roster_ownership_id,
        )

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            old_bundle_result = service.gm_insights(bundle.bundle_id)
            old_timing = service.trade_timing(bundle.bundle_id, "primary")
            history_store = LeagueHistoryStore(
                Path(directory) / LEAGUE_HISTORY_FILENAME
            )
            history_store.bind_bundle(
                league_key,
                bundle.state.season,
                "engine_" + "9" * 64,
                captured_at - timedelta(days=7),
            )
            history_store.ingest(capture, bundle=binding)
            captured_result = service.gm_insights(bundle.bundle_id)
            captured_timing = service.trade_timing(bundle.bundle_id, "primary")

        self.assertEqual(old_bundle_result["status"], "not_collected")
        self.assertEqual(captured_result["status"], "insufficient_sample")
        self.assertIsNotNone(captured_result["league_history_id"])
        self.assertEqual(len(captured_result["teams"]), len(bundle.state.teams))
        self.assertIsNone(old_timing["history_revision"])
        self.assertIsNotNone(captured_timing["history_revision"])
        self.assertIsNot(old_timing, captured_timing)

    def test_unreadable_optional_history_does_not_block_current_bundle_features(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            history_path = Path(directory) / LEAGUE_HISTORY_FILENAME
            history_path.write_bytes(b"not a sqlite database")
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())

            insights = service.gm_insights(bundle.bundle_id)
            timing = service.trade_timing(bundle.bundle_id, "primary")

        self.assertEqual(insights["status"], "not_collected")
        self.assertEqual(insights["data_readiness"]["store_status"], "unavailable")
        self.assertEqual(
            insights["data_readiness"]["capabilities"][
                "current_roster_compatibility"
            ]["status"],
            "ready_with_holdout_validated_scope",
        )
        self.assertEqual(timing["data_readiness"]["store_status"], "unavailable")
        self.assertEqual(
            timing["data_readiness"]["capabilities"]["completed_deal_activity"][
                "status"
            ],
            "not_ready",
        )

    def test_history_collection_failure_reason_reaches_gm_and_timing_views(self):
        bundle = engine_bundle()
        attempt = WeeklyHistoryAttempt.unavailable(
            WeeklyHistoryReason.ACTIVITY_UNAVAILABLE,
            bundle.source_manifest.host_captured_at,
        )
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            attempt_directory = Path(directory) / "history-attempts"
            attempt_directory.mkdir()
            (attempt_directory / f"{bundle.bundle_id}.json").write_text(
                json.dumps(
                    {
                        "bundle_id": bundle.bundle_id,
                        "history_attempt": attempt.to_record(),
                        "schema_version": 1,
                    }
                ),
                encoding="utf-8",
            )

            insights = service.gm_insights(bundle.bundle_id)
            timing = service.trade_timing(bundle.bundle_id, "primary")

        for result in (insights, timing):
            readiness = result["data_readiness"]
            self.assertEqual(readiness["collection_attempt"], attempt.to_record())
            self.assertIn(
                "history_collection_activity_unavailable",
                readiness["capabilities"]["completed_deal_activity"]["missing"],
            )

    def test_player_outlook_is_available_without_a_search_and_cached(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            first = service.player_outlook(bundle.bundle_id)
            with patch(
                "trade_snapshot.app_service.build_player_outlook",
                side_effect=AssertionError("cached player outlook must not be rebuilt"),
            ):
                second = service.player_outlook(bundle.bundle_id)

        self.assertIs(first, second)
        self.assertEqual(first["bundle_id"], bundle.bundle_id)
        self.assertEqual(first["data_readiness"]["status"], "ready_with_limitations")
        self.assertEqual(
            len(first["players"]),
            len({row.canonical_player_id for row in bundle.projections}),
        )

    def test_concurrent_player_outlook_requests_share_one_calculation(self):
        bundle = engine_bundle()
        calculation_started = Event()
        release_calculation = Event()
        second_started = Event()

        from trade_snapshot.player_outlook import build_player_outlook as build

        def delayed_build(*args):
            calculation_started.set()
            self.assertTrue(release_calculation.wait(5))
            return build(*args)

        def second_request(service):
            second_started.set()
            return service.player_outlook(bundle.bundle_id)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            with patch(
                "trade_snapshot.app_service.build_player_outlook",
                side_effect=delayed_build,
            ) as mocked_build, ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(service.player_outlook, bundle.bundle_id)
                self.assertTrue(calculation_started.wait(5))
                second = pool.submit(second_request, service)
                self.assertTrue(second_started.wait(5))
                time.sleep(0.02)
                self.assertFalse(second.done())
                release_calculation.set()
                first_result = first.result(timeout=5)
                second_result = second.result(timeout=5)

        self.assertIs(first_result, second_result)
        mocked_build.assert_called_once()

    def test_player_outlook_cache_is_bounded_and_least_recently_used(self):
        bundle = engine_bundle()
        bundle_ids = tuple(f"engine_{digit * 64}" for digit in "012")
        build_count = 0

        def build(_bundle):
            nonlocal build_count
            build_count += 1
            return {"calculation": build_count}

        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.app_service._MAX_PLAYER_OUTLOOK_CACHE_SIZE", 2
        ):
            service = LocalAppService(directory)
            with patch.object(
                service,
                "_bundle_path",
                side_effect=lambda bundle_id: Path(directory) / f"{bundle_id}.json",
            ), patch(
                "trade_snapshot.app_service.load_engine_bundle",
                return_value=bundle,
            ), patch(
                "trade_snapshot.app_service.build_player_outlook",
                side_effect=build,
            ):
                first = service.player_outlook(bundle_ids[0])
                service.player_outlook(bundle_ids[1])
                self.assertIs(service.player_outlook(bundle_ids[0]), first)
                service.player_outlook(bundle_ids[2])
                self.assertIs(service.player_outlook(bundle_ids[0]), first)
                service.player_outlook(bundle_ids[1])

        self.assertEqual(build_count, 4)

    def test_league_dashboard_is_available_without_a_search_and_cached(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            first = service.league_dashboard(bundle.bundle_id)
            with patch(
                "trade_snapshot.app_service.build_league_dashboard",
                side_effect=AssertionError("cached dashboard must not be rebuilt"),
            ):
                second = service.league_dashboard(bundle.bundle_id)

        self.assertIs(first, second)
        self.assertEqual(first["bundle_id"], bundle.bundle_id)
        self.assertEqual(first["data_readiness"]["status"], "ready_with_limitations")
        self.assertEqual(first["scenario_count"], bundle.scenario_config.scenario_count)
        self.assertEqual(len(first["teams"]), len(bundle.state.teams))
        self.assertEqual(first["championship_model"]["status"], "modeled_estimate")
        self.assertAlmostEqual(
            sum(row["championship_probability"] for row in first["teams"]),
            1.0,
        )
        self.assertTrue(
            all(
                row["championship_probability"] <= row["playoff_probability"]
                for row in first["teams"]
            )
        )

    def test_league_dashboard_uses_a_bounded_deterministic_scenario_prefix(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.app_service._MAX_DASHBOARD_SCENARIOS", 2
        ):
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            dashboard = service.league_dashboard(bundle.bundle_id)

        self.assertEqual(dashboard["scenario_count"], 2)
        self.assertEqual(
            dashboard["scenario_sampling"],
            {
                "bundle_scenario_count": 5,
                "dashboard_scenario_count": 2,
                "capped": True,
                "policy": "deterministic_prefix",
                "methodology": (
                    "Dashboard calculations use the first 2 deterministic draws "
                    "from the bundle's 5-scenario stream to keep the automatic "
                    "local view responsive."
                ),
            },
        )

    def test_dashboard_cap_preserves_the_bundle_player_score_floor(self):
        bundle = engine_bundle()
        bundle = replace(
            bundle,
            scenario_config=replace(
                bundle.scenario_config,
                player_score_floor=-2.5,
            ),
        )
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.app_service._MAX_DASHBOARD_SCENARIOS", 2
        ):
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            with patch(
                "trade_snapshot.app_service.prepare_season_baseline",
                wraps=prepare_season_baseline,
            ) as prepared:
                service.league_dashboard(bundle.bundle_id)

        self.assertEqual(prepared.call_args.args[4].player_score_floor, -2.5)

    def test_concurrent_dashboard_requests_share_one_calculation(self):
        bundle = engine_bundle()
        calculation_started = Event()
        release_calculation = Event()
        second_started = Event()

        from trade_snapshot.dashboard import build_league_dashboard as build

        def delayed_build(*args):
            calculation_started.set()
            self.assertTrue(release_calculation.wait(5))
            return build(*args)

        def second_request(service):
            second_started.set()
            return service.league_dashboard(bundle.bundle_id)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            with patch(
                "trade_snapshot.app_service.build_league_dashboard",
                side_effect=delayed_build,
            ) as mocked_build, ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(service.league_dashboard, bundle.bundle_id)
                self.assertTrue(calculation_started.wait(5))
                second = pool.submit(second_request, service)
                self.assertTrue(second_started.wait(5))
                time.sleep(0.02)
                self.assertFalse(second.done())
                release_calculation.set()
                first_result = first.result(timeout=5)
                second_result = second.result(timeout=5)

        self.assertIs(first_result, second_result)
        mocked_build.assert_called_once()

    def test_three_team_service_counts_runs_and_presents_every_participant(self):
        space, prepared, baseline, _ = three_way_components()
        bundle = SimpleNamespace(
            methodology_mode="holdout_validated",
            state=baseline.state,
            rosters=baseline.scenarios.rosters,
            projections=baseline.scenarios.projections,
            eligibilities=baseline.scenarios.eligibilities,
            scenario_config=baseline.scenarios.config,
            strength_model=prepared.model,
            player_names={player_id: player_id.upper() for player_id in prepared.model.players},
        )
        request_payload = payload("engine_" + "1" * 64)
        request_payload.update(
            {
                "trade_format": "three_team",
                "primary_team_id": "a",
                "counterparty_team_ids": ["c", "b"],
                "max_total_players": 3,
                "require_no_drops": False,
            }
        )
        request = LocalSearchRequest.from_payload(request_payload)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            with patch.object(
                service, "_bundle_path", return_value=Path(directory) / "bundle.json"
            ), patch(
                "trade_snapshot.app_service.load_engine_bundle", return_value=bundle
            ), patch(
                "trade_snapshot.app_service.build_bundle_data_readiness",
                return_value={
                    "capabilities": {
                        "trade_search": {
                            "status": "ready_with_limitations",
                            "missing": [],
                        }
                    }
                },
            ):
                estimate = service.estimate_search(request)
                started = service.start_search(request)
                self.assertEqual(started["bundle_id"], request.bundle_id)
                self.assertEqual(started["request_id"], request.request_id)
                self.assertEqual(started["search_request"], request.to_record())
                finished = wait_for_job(service, started["job_id"])
                self.assertEqual(finished["status"], "complete", finished)
                preview = service.job_results(started["job_id"])
                with patch(
                    "trade_snapshot.three_way_xlsx.MAX_THREE_WAY_EXPORT_ROWS", 0
                ), patch.object(
                    ThreeWaySearchOutcome,
                    "results",
                    side_effect=AssertionError("results must not materialize"),
                ), self.assertRaisesRegex(ValueError, "at most 0"):
                    service.export_job(started["job_id"])

        self.assertEqual(estimate["candidate_count_text"], str(space.candidate_count))
        self.assertEqual(estimate["participant_team_ids"], ["a", "b", "c"])
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["trade_format"], "three_team")
        self.assertEqual(
            finished["progress"]["total_candidate_count_text"],
            str(space.candidate_count),
        )
        self.assertEqual(preview["trade_format"], "three_team")
        self.assertIn("ascending team-ID order", estimate["free_agent_allocation_policy"])
        self.assertEqual(
            preview["free_agent_allocation_policy"],
            estimate["free_agent_allocation_policy"],
        )
        self.assertEqual(preview["total_count_text"], str(space.candidate_count))
        self.assertEqual(len(preview["rows"]), space.candidate_count)
        self.assertTrue(
            all(
                {impact["team_id"] for impact in row["team_impacts"]}
                == {"a", "b", "c"}
                for row in preview["rows"]
            )
        )
        self.assertEqual(
            {row["power_methodology_status"] for row in preview["rows"]},
            {"extrapolated"},
        )

    def test_three_team_request_is_explicit_canonical_and_does_not_change_legacy_identity(self):
        bundle = engine_bundle()
        legacy_payload = payload(bundle.bundle_id)
        explicit_two = dict(legacy_payload, trade_format="two_team")
        legacy = LocalSearchRequest.from_payload(legacy_payload)
        explicit = LocalSearchRequest.from_payload(explicit_two)
        self.assertEqual(legacy.request_id, explicit.request_id)
        self.assertNotIn("trade_format", legacy.to_record())

        three_payload = dict(
            legacy_payload,
            trade_format="three_team",
            counterparty_team_ids=["z-team", "a-team"],
        )
        three = LocalSearchRequest.from_payload(three_payload)
        self.assertEqual(three.counterparty_team_ids, ("a-team", "z-team"))
        self.assertEqual(three.to_record()["trade_format"], "three_team")
        reversed_partners = LocalSearchRequest.from_payload(
            dict(three_payload, counterparty_team_ids=["a-team", "z-team"])
        )
        self.assertEqual(three.request_id, reversed_partners.request_id)

    def test_three_team_request_requires_two_partners_and_cannot_skip_small_trades(self):
        bundle = engine_bundle()
        with self.assertRaisesRegex(ValueError, "trade_format"):
            LocalSearchRequest.from_payload(
                dict(payload(bundle.bundle_id), trade_format=["three_team"])
            )
        for partners in ([], ["other"], ["a", "b", "c"]):
            with self.subTest(partners=partners):
                with self.assertRaisesRegex(ValueError, "exactly two partner"):
                    LocalSearchRequest.from_payload(
                        dict(
                            payload(bundle.bundle_id),
                            trade_format="three_team",
                            counterparty_team_ids=partners,
                        )
                    )
        with self.assertRaisesRegex(ValueError, "must be false"):
            LocalSearchRequest.from_payload(
                dict(
                    payload(bundle.bundle_id),
                    trade_format="three_team",
                    counterparty_team_ids=["a", "b"],
                    skip_fantasypros_small_trades=True,
                )
            )

    def test_import_search_resume_and_excel_export_end_to_end(self):
        bundle = engine_bundle()
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            summary = service.import_bundle(bundle.to_record())
            request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
            estimate = service.estimate_search(request)
            started = service.start_search(request)
            finished = wait_for_job(service, started["job_id"])
            preview = service.job_results(started["job_id"])
            exported = service.export_job(started["job_id"])
            export_path = service.export_path(exported["filename"])
            with ZipFile(export_path) as archive:
                export_strings = archive.read("xl/sharedStrings.xml").decode()

            resumed = service.start_search(request)
            resumed_finished = wait_for_job(service, resumed["job_id"])
            databases = tuple((Path(directory) / "searches").rglob("*.sqlite3"))

        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["positions"], ["RB"])
        self.assertEqual(
            {
                player["name"]
                for team in summary["teams"]
                for player in team["players"]
            },
            {
                bundle.player_names[player_id]
                for roster in bundle.rosters
                for player_id in roster.player_ids
            },
        )
        self.assertEqual(
            summary["methodology"]["holdout_validated_trade_scope"],
            "balanced package shapes with no adds or drops",
        )
        self.assertEqual(estimate["candidate_count"], 4)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["progress"]["completion_fraction"], 1)
        self.assertEqual(finished["progress"]["qualified_trade_count"], 4)
        self.assertEqual(exported["trade_count"], 4)
        self.assertEqual(preview["total_count"], 4)
        self.assertEqual(len(preview["rows"]), 4)
        self.assertEqual(
            {row["power_methodology_status"] for row in preview["rows"]},
            {"holdout_validated"},
        )
        self.assertEqual(
            {row["search_run_id"] for row in preview["rows"]},
            set(preview["search_run_ids"]),
        )
        for row in preview["rows"]:
            self.assertTrue(row["candidate_index"].isdigit())
            self.assertEqual(row["other_team_id"], "other")
            self.assertEqual(len(row["give_player_ids"]), len(row["give"]))
            self.assertEqual(
                len(row["receive_player_ids"]), len(row["receive"])
            )
            self.assertAlmostEqual(
                row["your_playoff_after"] - row["your_playoff_before"],
                row["your_playoff_delta"],
            )
            self.assertAlmostEqual(
                row["their_playoff_after"] - row["their_playoff_before"],
                row["their_playoff_delta"],
            )
        self.assertEqual(len(preview["team_outlook"]), 2)
        self.assertEqual(
            {row["team_name"] for row in preview["team_outlook"]},
            {"Primary", "Other"},
        )
        self.assertTrue(
            all(0 <= row["playoff_probability"] <= 1 for row in preview["team_outlook"])
        )
        for row in preview["team_outlook"]:
            self.assertIsInstance(row["current_rank"], int)
            self.assertIsInstance(row["expected_final_points_for"], float)
            self.assertIsInstance(row["expected_final_points_against"], float)
            self.assertAlmostEqual(sum(row["rank_distribution"]), 1.0)
            self.assertAlmostEqual(
                sum(row["seed_distribution"]),
                row["playoff_probability"],
            )
        self.assertTrue(export_path.name.endswith(".xlsx"))
        self.assertIn("Direct Provider Projection Cells", export_strings)
        self.assertIn("Custom-Scoring Limitation", export_strings)
        self.assertIn("Outcome-Correlation Limitation", export_strings)
        self.assertIn("Marginal-Uncertainty Limitation", export_strings)
        self.assertIn("Championship-Proxy Limitation", export_strings)
        self.assertIn("As-of-Time Limitation", export_strings)
        self.assertIn("Host league snapshot (espn)", export_strings)
        self.assertIn("Engine Bundle ID", export_strings)
        self.assertIn(bundle.bundle_id, export_strings)
        self.assertIn("Waiver Pool ID", export_strings)
        self.assertIn(bundle.waiver_pool.waiver_pool_id, export_strings)
        self.assertIn("Search Request ID", export_strings)
        self.assertIn(request.request_id, export_strings)
        self.assertIn("Trade Constraints (Canonical JSON)", export_strings)
        self.assertIn("require_no_drops", export_strings)
        self.assertIn("Pair Search Definitions", export_strings)
        self.assertIn("Opaque league binding (workspace)", export_strings)
        self.assertIn(bundle.source_manifest.league_binding_id, export_strings)
        self.assertIn(bundle.source_manifest.host_snapshot_id, export_strings)
        self.assertIn("FantasyPros league artifact", export_strings)
        self.assertIn(
            bundle.source_manifest.fantasypros_league_artifact_id,
            export_strings,
        )
        self.assertIn(
            "FantasyPros comparison benchmark record (comparison only)",
            export_strings,
        )
        self.assertIn(bundle.fantasypros_benchmark.benchmark_id, export_strings)
        self.assertIn(
            "FantasyPros comparison source artifact (comparison only)",
            export_strings,
        )
        self.assertIn(
            bundle.fantasypros_benchmark.source_artifact_id,
            export_strings,
        )
        self.assertIn("NFL schedule (espn)", export_strings)
        self.assertIn(bundle.nfl_schedule.schedule_id, export_strings)
        self.assertIn("Projection source manifest", export_strings)
        self.assertIn(
            bundle.projection_source_manifest.manifest_id,
            export_strings,
        )
        self.assertEqual(resumed_finished["status"], "complete")
        self.assertEqual(len(databases), 1)

    def test_search_override_preserves_the_bundle_player_score_floor(self):
        bundle = engine_bundle()
        bundle = replace(
            bundle,
            scenario_config=replace(
                bundle.scenario_config,
                player_score_floor=-3.0,
            ),
        )
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
            with patch(
                "trade_snapshot.app_service.prepare_season_baseline",
                wraps=prepare_season_baseline,
            ) as prepared:
                started = service.start_search(request)
                finished = wait_for_job(service, started["job_id"])

        self.assertEqual(finished["status"], "complete", finished)
        self.assertEqual(prepared.call_args.args[4].player_score_floor, -3.0)

    def test_request_is_content_addressed_and_supports_explicit_roster_adjustments(self):
        bundle = engine_bundle()
        first = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        second = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        self.assertEqual(first, second)
        self.assertTrue(first.request_id.startswith("app-search_"))

        invalid = payload(bundle.bundle_id)
        invalid["require_no_drops"] = False
        invalid["max_outgoing"] = 1
        invalid["min_incoming"] = 2
        invalid["max_incoming"] = 2
        invalid["max_total_players"] = 3
        invalid["max_imbalance"] = 1
        invalid["balanced_only"] = False
        request = LocalSearchRequest.from_payload(invalid)
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            started = service.start_search(request)
            finished = wait_for_job(service, started["job_id"])
            rows = service.job_results(started["job_id"])["rows"]
        self.assertEqual(finished["status"], "complete")
        self.assertTrue(rows)
        self.assertTrue(all(row["your_drops"] for row in rows))
        self.assertEqual(
            {row["power_methodology_status"] for row in rows},
            {"extrapolated"},
        )
        self.assertTrue(
            all(row["power_methodology_status"] == "extrapolated" for row in rows)
        )

    def test_package_filters_apply_to_estimate_and_search(self):
        bundle = engine_bundle()
        filtered_payload = payload(bundle.bundle_id)
        filtered_payload.update(
            {
                "outgoing_filter": {
                    "player_ids": ["p1"],
                    "player_mode": "include",
                    "positions": [],
                    "position_mode": None,
                },
                "incoming_filter": {
                    "player_ids": ["q1"],
                    "player_mode": "only",
                    "positions": [],
                    "position_mode": None,
                },
            }
        )
        unfiltered = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        filtered = LocalSearchRequest.from_payload(filtered_payload)
        position_payload = payload(bundle.bundle_id)
        position_payload["outgoing_filter"] = {
            "player_ids": [],
            "player_mode": None,
            "positions": ["RB"],
            "position_mode": "only",
        }
        position_filtered = LocalSearchRequest.from_payload(position_payload)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            estimate = service.estimate_search(filtered)
            position_estimate = service.estimate_search(position_filtered)
            started = service.start_search(filtered)
            finished = wait_for_job(service, started["job_id"])
            rows = service.job_results(started["job_id"])["rows"]

        self.assertEqual(estimate["candidate_count"], 1)
        self.assertEqual(position_estimate["candidate_count"], 0)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["give"], ["P1"])
        self.assertEqual(rows[0]["receive"], ["Q1"])
        self.assertNotEqual(unfiltered.request_id, filtered.request_id)
        self.assertNotIn(
            "outgoing_filter", unfiltered.to_record()["trade_constraints"]
        )
        self.assertEqual(
            filtered.to_record()["trade_constraints"][
                "package_filter_semantics_version"
            ],
            1,
        )

    def test_filter_payload_and_roster_ownership_are_validated(self):
        bundle = engine_bundle()
        malformed = payload(bundle.bundle_id)
        malformed["outgoing_filter"] = {
            "player_ids": [],
            "player_mode": "include",
            "positions": [],
            "position_mode": None,
        }
        with self.assertRaisesRegex(ValueError, "player_mode must be set exactly"):
            LocalSearchRequest.from_payload(malformed)

        wrong_side = payload(bundle.bundle_id)
        wrong_side["outgoing_filter"] = {
            "player_ids": ["q1"],
            "player_mode": "include",
            "positions": [],
            "position_mode": None,
        }
        request = LocalSearchRequest.from_payload(wrong_side)
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            with self.assertRaisesRegex(ValueError, "selected primary team"):
                service.estimate_search(request)

    def test_filter_expressions_are_canonical_and_legacy_fields_are_exclusive(self):
        bundle = engine_bundle()
        first_payload = payload(bundle.bundle_id)
        first_payload["outgoing_filter_expression"] = filter_expression(
            "and",
            player_filter("p1"),
            filter_expression("not", player_filter("p2", "exclude")),
        )
        second_payload = payload(bundle.bundle_id)
        second_payload["outgoing_filter_expression"] = filter_expression(
            "and",
            filter_expression("not", player_filter("p2", "exclude")),
            player_filter("p1"),
        )

        first = LocalSearchRequest.from_payload(first_payload)
        second = LocalSearchRequest.from_payload(second_payload)
        constraints = first.to_record()["trade_constraints"]

        self.assertEqual(first.request_id, second.request_id)
        self.assertEqual(first.to_record(), second.to_record())
        self.assertEqual(constraints["package_filter_semantics_version"], 2)
        self.assertEqual(constraints["outgoing_filter"]["operator"], "and")
        self.assertNotIn("outgoing_filter_expression", first.to_record())

        different_payload = dict(first_payload)
        different_payload["outgoing_filter_expression"] = filter_expression(
            "xor", *first_payload["outgoing_filter_expression"]["operands"]
        )
        different = LocalSearchRequest.from_payload(different_payload)
        self.assertNotEqual(first.request_id, different.request_id)

        ambiguous = dict(first_payload)
        ambiguous["outgoing_filter"] = None
        with self.assertRaisesRegex(ValueError, "cannot both be provided"):
            LocalSearchRequest.from_payload(ambiguous)

        leaf_as_expression = payload(bundle.bundle_id)
        leaf_as_expression["incoming_filter_expression"] = player_filter("q1")
        with self.assertRaisesRegex(ValueError, "must be an expression"):
            LocalSearchRequest.from_payload(leaf_as_expression)

        null_expression = payload(bundle.bundle_id)
        null_expression["incoming_filter_expression"] = None
        with self.assertRaisesRegex(ValueError, "must be an expression"):
            LocalSearchRequest.from_payload(null_expression)

        expression_in_legacy_field = payload(bundle.bundle_id)
        expression_in_legacy_field["incoming_filter"] = filter_expression(
            "not", player_filter("q1")
        )
        with self.assertRaisesRegex(ValueError, "legacy package filter"):
            LocalSearchRequest.from_payload(expression_in_legacy_field)

    def test_expression_player_ownership_supports_multiple_selected_partners(self):
        _space, prepared, baseline, _ = three_way_components()
        bundle = SimpleNamespace(
            methodology_mode="holdout_validated",
            state=baseline.state,
            rosters=baseline.scenarios.rosters,
            projections=baseline.scenarios.projections,
            eligibilities=baseline.scenarios.eligibilities,
            scenario_config=baseline.scenarios.config,
            strength_model=prepared.model,
            player_names={
                player_id: player_id.upper() for player_id in prepared.model.players
            },
        )
        two_team_payload = payload("engine_" + "1" * 64)
        two_team_payload.update(
            {
                "primary_team_id": "a",
                "counterparty_team_ids": ["b", "c"],
                "incoming_filter_expression": filter_expression(
                    "or", player_filter("b1"), player_filter("c1")
                ),
            }
        )
        two_team = LocalSearchRequest.from_payload(two_team_payload)

        three_team_payload = dict(two_team_payload)
        three_team_payload.update(
            {
                "trade_format": "three_team",
                "max_outgoing": 2,
                "max_incoming": 2,
                "max_total_players": 4,
                "max_imbalance": 1,
                "balanced_only": False,
                "incoming_filter_expression": filter_expression(
                    "and", player_filter("b1"), player_filter("c1")
                ),
            }
        )
        three_team = LocalSearchRequest.from_payload(three_team_payload)

        unselected_payload = dict(two_team_payload)
        unselected_payload["incoming_filter_expression"] = filter_expression(
            "not", player_filter("d1")
        )
        unselected = LocalSearchRequest.from_payload(unselected_payload)
        wrong_side_payload = dict(three_team_payload)
        wrong_side_payload["outgoing_filter_expression"] = filter_expression(
            "not", player_filter("b1")
        )
        wrong_side = LocalSearchRequest.from_payload(wrong_side_payload)
        legacy_cross_team_payload = payload("engine_" + "1" * 64)
        legacy_cross_team_payload.update(
            {
                "primary_team_id": "a",
                "counterparty_team_ids": ["b", "c"],
                "incoming_filter": {
                    "player_ids": ["b1", "c1"],
                    "player_mode": "include",
                    "positions": [],
                    "position_mode": None,
                },
            }
        )
        legacy_cross_team = LocalSearchRequest.from_payload(
            legacy_cross_team_payload
        )
        wrapped_cross_team_payload = payload("engine_" + "1" * 64)
        wrapped_cross_team_payload.update(
            {
                "primary_team_id": "a",
                "counterparty_team_ids": ["b", "c"],
                "incoming_filter_expression": filter_expression(
                    "not",
                    {
                        "player_ids": ["b1", "c1"],
                        "player_mode": "include",
                        "positions": [],
                        "position_mode": None,
                    },
                ),
            }
        )
        wrapped_cross_team = LocalSearchRequest.from_payload(
            wrapped_cross_team_payload
        )

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            with patch.object(
                service, "_bundle_path", return_value=Path(directory) / "bundle.json"
            ), patch(
                "trade_snapshot.app_service.load_engine_bundle", return_value=bundle
            ), patch(
                "trade_snapshot.app_service.build_bundle_data_readiness",
                return_value={
                    "capabilities": {
                        "trade_search": {
                            "status": "ready_with_limitations",
                            "missing": [],
                        }
                    }
                },
            ):
                two_team_estimate = service.estimate_search(two_team)
                three_team_estimate = service.estimate_search(three_team)
                with self.assertRaisesRegex(ValueError, "selected other team"):
                    service.estimate_search(unselected)
                with self.assertRaisesRegex(ValueError, "selected primary team"):
                    service.estimate_search(wrong_side)
                with self.assertRaisesRegex(ValueError, "same other team"):
                    service.estimate_search(legacy_cross_team)
                with self.assertRaisesRegex(ValueError, "same other team"):
                    service.estimate_search(wrapped_cross_team)

        self.assertGreater(two_team_estimate["candidate_count"], 0)
        self.assertGreater(int(three_team_estimate["candidate_count_text"]), 0)

    def test_surrogate_requires_consent_then_searches_entirely_offline(self):
        bundle = surrogate_bundle()
        denied = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        accepted_payload = payload(bundle.bundle_id)
        accepted_payload["allow_surrogate_power"] = True
        accepted = LocalSearchRequest.from_payload(accepted_payload)
        extrapolated_payload = dict(accepted_payload)
        extrapolated_payload.update(
            {
                "balanced_only": False,
                "max_incoming": 2,
                "min_incoming": 2,
                "max_total_players": 3,
                "max_imbalance": 1,
                "require_no_drops": False,
            }
        )
        extrapolated = LocalSearchRequest.from_payload(extrapolated_payload)

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            summary = service.import_bundle(bundle.to_record())
            with self.assertRaisesRegex(ValueError, "SURROGATE"):
                service.estimate_search(denied)
            with patch(
                "trade_snapshot.browser_capture.BrowserCollector.collect",
                side_effect=AssertionError("local search must not collect provider data"),
            ):
                estimate = service.estimate_search(accepted)
                started = service.start_search(accepted)
                finished = wait_for_job(service, started["job_id"])
                results = service.job_results(started["job_id"])
                exported = service.export_job(started["job_id"])
                second = service.start_search(extrapolated)
                second_finished = wait_for_job(service, second["job_id"])
                extrapolated_results = service.job_results(second["job_id"])

        self.assertEqual(summary["power_engine_mode"], "surrogate")
        self.assertIsNone(
            summary["methodology"]["holdout_validated_trade_scope"]
        )
        self.assertEqual(estimate["candidate_count"], 4)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(results["power_engine_mode"], "surrogate")
        self.assertTrue(results["rows"])
        self.assertEqual(
            {row["power_methodology_status"] for row in results["rows"]},
            {"surrogate"},
        )
        self.assertEqual(exported["trade_count"], 4)
        self.assertEqual(second_finished["status"], "complete")
        self.assertTrue(extrapolated_results["rows"])
        self.assertEqual(
            {
                row["power_methodology_status"]
                for row in extrapolated_results["rows"]
            },
            {"surrogate_extrapolated"},
        )

    def test_rejects_unknown_request_fields_and_export_traversal(self):
        bundle = engine_bundle()
        bad = payload(bundle.bundle_id)
        bad["cookie"] = "secret"
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            LocalSearchRequest.from_payload(bad)
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            with self.assertRaisesRegex(ValueError, "invalid export filename"):
                service.export_path("../secret.xlsx")


if __name__ == "__main__":
    unittest.main()
