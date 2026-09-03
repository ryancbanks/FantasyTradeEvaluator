from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import trade_snapshot.workbook_model as workbook_model_module
from tests.test_engine_bundle import engine_bundle
from tests.test_three_way_search import components as three_way_components
from tests.test_surrogate_disclosure import surrogate_bundle
from trade_snapshot.app_service import LocalAppService, LocalSearchRequest, _SearchJob
from trade_snapshot.three_way_search import ThreeWaySearchOutcome


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
    def test_active_job_catalog_exposes_safe_search_context_and_enforces_one_owner(self):
        bundle = engine_bundle()
        request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            active = _SearchJob("a" * 32, request, status="running")
            service._jobs[active.job_id] = active

            catalog = service.active_job_catalog()
            self.assertEqual(
                catalog,
                {
                    "search": service.job(active.job_id),
                    "weekly_collection": None,
                    "draft": None,
                },
            )
            self.assertEqual(
                catalog["search"]["request"],
                {
                    "bundle_id": bundle.bundle_id,
                    "primary_team_id": "primary",
                    "counterparty_team_ids": [],
                },
            )
            self.assertNotIn("host_league_url", catalog["search"]["request"])

            second = _SearchJob("b" * 32, request, status="queued")
            service._jobs[second.job_id] = second
            with self.assertRaisesRegex(RuntimeError, "multiple trade-search"):
                service.active_job_catalog()

            active.status = "complete"
            second.status = "complete"
            self.assertIsNone(service.active_search_job())

    def test_latest_terminal_search_is_recoverable_until_the_ui_acknowledges_it(self):
        bundle = engine_bundle()
        request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            first = _SearchJob("a" * 32, request, status="running")
            service._jobs[first.job_id] = first
            service._set_job(first, status="complete")

            self.assertEqual(
                service.active_job_catalog()["search"]["job_id"],
                first.job_id,
            )

            second = _SearchJob("b" * 32, request, status="running")
            service._jobs[second.job_id] = second
            service._set_job(second, status="failed", error="test failure")

            self.assertFalse(
                service.acknowledge_search_activity(first.job_id)["acknowledged"]
            )
            self.assertEqual(
                service.active_job_catalog()["search"]["job_id"],
                second.job_id,
            )
            self.assertTrue(
                service.acknowledge_search_activity(second.job_id)["acknowledged"]
            )
            self.assertIsNone(service.active_job_catalog()["search"])
            self.assertFalse(
                service.acknowledge_search_activity(second.job_id)["acknowledged"]
            )

    def test_dashboard_calculation_blocks_other_heavy_work(self):
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            pending = Future()
            service._dashboard_futures["busy"] = pending

            with self.assertRaisesRegex(RuntimeError, "league dashboard"):
                service._require_draft_capacity()

        self.assertFalse(pending.done())

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

    def test_dashboard_cache_is_bounded_and_least_recently_used(self):
        bundle = engine_bundle()
        bundle_ids = tuple(f"engine_{digit * 64}" for digit in "012")
        build_count = 0

        def build(*_args):
            nonlocal build_count
            build_count += 1
            return {"calculation": build_count}

        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.app_service._MAX_DASHBOARD_CACHE_SIZE", 2
        ):
            service = LocalAppService(directory)
            with patch.object(service, "_load_bundle", return_value=bundle), patch.object(
                service, "_season_baseline"
            ) as season_baseline, patch(
                "trade_snapshot.app_service.build_league_dashboard",
                side_effect=build,
            ):
                from trade_snapshot.trade_impact import prepare_season_baseline

                season_baseline.return_value = prepare_season_baseline(
                    bundle.state,
                    bundle.rosters,
                    bundle.projections,
                    bundle.eligibilities,
                    bundle.scenario_config,
                )
                first = service.league_dashboard(bundle_ids[0])
                service.league_dashboard(bundle_ids[1])
                self.assertIs(service.league_dashboard(bundle_ids[0]), first)
                service.league_dashboard(bundle_ids[2])
                self.assertIs(service.league_dashboard(bundle_ids[0]), first)
                service.league_dashboard(bundle_ids[1])

        self.assertEqual(build_count, 4)

    def test_matching_dashboard_and_search_share_one_season_baseline(self):
        bundle = engine_bundle()
        request_payload = payload(bundle.bundle_id)
        request_payload.update(
            scenario_count=bundle.scenario_config.scenario_count,
            seed=bundle.scenario_config.seed,
        )
        request = LocalSearchRequest.from_payload(request_payload)

        from trade_snapshot.trade_impact import prepare_season_baseline as prepare

        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            with patch(
                "trade_snapshot.app_service.prepare_season_baseline",
                wraps=prepare,
            ) as prepared:
                service.league_dashboard(bundle.bundle_id)
                started = service.start_search(request)
                finished = wait_for_job(service, started["job_id"])

            retained = service._jobs[started["job_id"]]

        self.assertEqual(finished["status"], "complete")
        prepared.assert_called_once()
        self.assertIsNotNone(retained.context)
        self.assertFalse(hasattr(retained, "baseline"))

    def test_search_and_player_dashboard_work_are_coordinated_both_directions(self):
        bundle = engine_bundle()
        request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        with TemporaryDirectory() as directory:
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            service._player_outlook_futures["busy"] = Future()
            with self.assertRaisesRegex(RuntimeError, "Player Lab"):
                service.start_search(request)
            with self.assertRaisesRegex(RuntimeError, "Player Lab"):
                service._require_draft_capacity()
            service._player_outlook_futures.clear()

            service._dashboard_futures["busy"] = Future()
            with self.assertRaisesRegex(RuntimeError, "league dashboard"):
                service.start_search(request)
            service._dashboard_futures.clear()

            service._jobs["active"] = SimpleNamespace(status="running")
            with self.assertRaisesRegex(RuntimeError, "trade search"):
                service.league_dashboard(bundle.bundle_id)
            with self.assertRaisesRegex(RuntimeError, "trade search"):
                service.player_outlook(bundle.bundle_id)

    def test_terminal_search_job_history_is_bounded(self):
        bundle = engine_bundle()
        request = LocalSearchRequest.from_payload(payload(bundle.bundle_id))
        job_ids = []
        with TemporaryDirectory() as directory, patch(
            "trade_snapshot.app_service._MAX_RETAINED_SEARCH_JOBS", 2
        ):
            service = LocalAppService(directory)
            service.import_bundle(bundle.to_record())
            for _ in range(3):
                started = service.start_search(request)
                job_ids.append(started["job_id"])
                self.assertEqual(
                    wait_for_job(service, started["job_id"])["status"],
                    "complete",
                )

            with self.assertRaises(KeyError):
                service.job(job_ids[0])
            self.assertEqual(set(service._jobs), set(job_ids[1:]))

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
            methodology_mode="exact",
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
            ), patch("trade_snapshot.app_service.load_engine_bundle", return_value=bundle):
                estimate = service.estimate_search(request)
                started = service.start_search(request)
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
            with patch(
                "trade_snapshot.workbook_model._trade_row",
                wraps=workbook_model_module._trade_row,
            ) as converted:
                limited_preview = service.job_results(started["job_id"], limit=1)
            exported = service.export_job(started["job_id"])
            export_path = service.export_path(exported["filename"])
            outcome = service._jobs[started["job_id"]].outcome

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
            summary["methodology"]["exact_trade_scope"],
            "balanced packages with no adds or drops",
        )
        self.assertEqual(estimate["candidate_count"], 4)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["progress"]["completion_fraction"], 1)
        self.assertEqual(finished["progress"]["qualified_trade_count"], 4)
        self.assertEqual(exported["trade_count"], 4)
        self.assertEqual(preview["total_count"], 4)
        self.assertEqual(len(preview["rows"]), 4)
        self.assertEqual(limited_preview["total_count"], 4)
        self.assertEqual(limited_preview["shown_count"], 1)
        self.assertEqual(len(limited_preview["rows"]), 1)
        self.assertEqual(converted.call_count, 1)
        self.assertTrue(
            all(pair.search.database_path is not None for pair in outcome.pairs)
        )
        self.assertEqual(
            {row["power_methodology_status"] for row in preview["rows"]},
            {"exact"},
        )
        self.assertEqual(
            {row["power_methodology_status"] for row in preview["rows"]},
            {"exact"},
        )
        self.assertEqual(len(preview["team_outlook"]), 2)
        self.assertEqual(
            {row["team_name"] for row in preview["team_outlook"]},
            {"Primary", "Other"},
        )
        self.assertTrue(
            all(0 <= row["playoff_probability"] <= 1 for row in preview["team_outlook"])
        )
        self.assertTrue(export_path.name.endswith(".xlsx"))
        self.assertEqual(resumed_finished["status"], "complete")
        self.assertEqual(len(databases), 1)

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
            methodology_mode="exact",
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
            ), patch("trade_snapshot.app_service.load_engine_bundle", return_value=bundle):
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
        self.assertIsNone(summary["methodology"]["exact_trade_scope"])
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
