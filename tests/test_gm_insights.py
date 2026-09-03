from collections import Counter
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest

from tests.test_engine_bundle import engine_bundle
from tests.test_gm_trade_valuation import (
    LEAGUE_KEY,
    REQUEST_AT,
    SOURCE_AT,
    TRADE_AT,
    capture,
    current_bundle,
    drifted_current_bundle,
    history,
    trade,
)
from trade_snapshot.gm_insights import build_gm_insights
from trade_snapshot._gm_team_profiles import (
    _proposal_guidance,
    _trade_activity,
    _trade_value_summary,
    _value_label,
)
from trade_snapshot.league_history import (
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeamRoster,
    HistoryTransactionAsset,
    HistoryTransactionAssetKind,
    LeagueHistorySnapshot,
)


def team(report, team_id):
    return next(row for row in report["teams"] if row["team_id"] == team_id)


def healthy_comparison_report(count, *, transaction_complete=True):
    base = engine_bundle()
    source_bundles = [
        current_bundle(base, f"healthy-source-{index}") for index in range(count)
    ]
    requested = drifted_current_bundle(base)
    trade_times = [
        REQUEST_AT - timedelta(hours=6 * (count - index))
        for index in range(count)
    ]
    trades = tuple(
        trade(f"healthy-trade-{index}", recorded_at)
        for index, recorded_at in enumerate(trade_times, start=1)
    )
    source_bindings = tuple(
        HistoryBundleBinding(
            LEAGUE_KEY,
            2026,
            source.bundle_id,
            recorded_at - timedelta(hours=1),
        )
        for source, recorded_at in zip(source_bundles, trade_times)
    )
    requested_binding = HistoryBundleBinding(
        LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
    )
    history_captures = _staged_captures(
        trades,
        source_bindings,
        transaction_complete=transaction_complete,
    )
    snapshot = LeagueHistorySnapshot(
        requested_binding,
        (*source_bindings, requested_binding),
        history_captures,
    )
    bundles = {source.bundle_id: source for source in source_bundles}
    return build_gm_insights(
        requested, snapshot, bundle_loader=bundles.__getitem__
    )


def _staged_captures(trades, source_bindings, *, transaction_complete=True):
    rows = []
    cumulative = []
    for event, binding in zip(trades, source_bindings):
        rows.append(
            capture(
                tuple(cumulative),
                binding.captured_at - timedelta(minutes=1),
                injury_status="ACTIVE",
            )
        )
        cumulative.append(event)
        rows.append(
            capture(
                tuple(cumulative),
                event.recorded_at + timedelta(hours=1),
                injury_status="ACTIVE",
            )
        )
    latest = capture(tuple(cumulative), injury_status="ACTIVE")
    if not transaction_complete:
        latest = replace(latest, transaction_history_complete=False)
    return (*rows, latest)


class GeneralManagerInsightsTests(unittest.TestCase):
    def test_no_history_is_an_explicit_json_safe_state(self):
        bundle = engine_bundle()

        report = build_gm_insights(bundle, None)

        self.assertEqual(report["status"], "not_collected")
        self.assertEqual(
            [row["team_id"] for row in report["teams"]], ["other", "primary"]
        )
        self.assertFalse(report["scope"]["offers_observed"])
        self.assertIsNone(report["coverage"]["transactions"]["completed_events"])
        for row in report["teams"]:
            self.assertEqual(
                set(row),
                {
                    "team_id",
                    "team_name",
                    "roster_compatibility",
                    "history_insights",
                },
            )
            compatibility = row["roster_compatibility"]
            self.assertFalse(compatibility["scope"]["behavioral_history_used"])
            self.assertFalse(compatibility["scope"]["manager_acceptance_modeled"])
            self.assertEqual(row["history_insights"]["status"], "not_collected")
            self.assertIn(
                "deal_accessibility",
                row["history_insights"]["unavailable_sections"],
            )
            self.assertNotIn("trade_activity", row)
            self.assertNotIn("trade_value", row)
            self.assertNotIn("proposal_guidance", row)
        encoded = json.dumps(report, allow_nan=False, sort_keys=True)
        self.assertNotIn('"manager_acceptance_modeled": true', encoded)

    def test_zero_eligible_captures_uses_compatibility_only_state(self):
        bundle = engine_bundle()
        binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, bundle.bundle_id, REQUEST_AT
        )
        future_capture = capture(
            (trade(),), captured_at=REQUEST_AT + timedelta(days=1)
        )
        snapshot = LeagueHistorySnapshot(
            binding, (binding,), (future_capture,)
        )

        report = build_gm_insights(bundle, snapshot)

        self.assertEqual(report["status"], "not_collected")
        self.assertEqual(report["coverage"]["capture_count"], 0)
        for row in report["teams"]:
            self.assertEqual(
                set(row),
                {
                    "team_id",
                    "team_name",
                    "roster_compatibility",
                    "history_insights",
                },
            )
            self.assertNotIn("proposal_guidance", row)

    def test_complete_single_trade_is_exact_but_tendency_label_is_withheld(self):
        source = engine_bundle()
        requested = current_bundle(source)
        snapshot = history(source, requested, (trade(),))
        loader = {source.bundle_id: source}.__getitem__

        first = build_gm_insights(requested, snapshot, bundle_loader=loader)
        second = build_gm_insights(requested, snapshot, bundle_loader=loader)
        primary = team(first, "primary")

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["coverage"]["valuations"]["valued_trades"], 1)
        self.assertEqual(
            first["coverage"]["valuations"]["current_revalued_trades"], 1
        )
        self.assertEqual(
            first["coverage"]["valuations"]["foresight_eligible_trades"], 0
        )
        self.assertEqual(primary["trade_value"]["status"], "insufficient_sample")
        self.assertEqual(primary["trade_value"]["methodology_counts"], {"exact": 1})
        self.assertIsNone(primary["trade_value"]["plain_language_alias"])
        metric = primary["trade_value"]["relative_power_edge"]
        self.assertEqual(metric["sample"]["raw_n"], 1)
        self.assertEqual(metric["confidence"]["status"], "uncertain")
        self.assertIn(
            "At least three exact at-time valued trades",
            " ".join(metric["confidence"]["reasons"]),
        )
        self.assertNotIn("acceptance_probability", json.dumps(first, sort_keys=True))
        self.assertIn(
            "private person",
            " ".join(first["methodology"]["limitations"]),
        )
        self.assertIn(
            "not a promise or prediction",
            " ".join(primary["proposal_guidance"]["caveats"]),
        )
        accessibility = primary["deal_accessibility"]
        self.assertNotIn("index_0_to_100", accessibility)
        self.assertEqual(
            accessibility["primary_metric"]["metric_id"],
            "next_two_week_trade_propensity",
        )
        self.assertIn("partner_breadth", accessibility["supporting_facets"])
        opportunity = primary["counterparty_value_opportunity"]
        team_edge = primary["trade_value"]["relative_power_edge"]
        reversed_edge = opportunity["relative_power_opportunity"]
        self.assertAlmostEqual(reversed_edge["estimate"], -team_edge["estimate"])
        self.assertAlmostEqual(
            reversed_edge["interval"]["lower"], -team_edge["interval"]["upper"]
        )
        self.assertAlmostEqual(
            reversed_edge["interval"]["upper"], -team_edge["interval"]["lower"]
        )
        self.assertEqual(
            opportunity["formula"],
            "negative_of_team_contemporaneous_relative_power_edge",
        )
        event = primary["evidence"][0]["valuation"]
        self.assertIsNotNone(event["at_time"])
        self.assertIsNotNone(event["current_revaluation"])
        self.assertEqual(event["comparison"]["status"], "comparable_raw")
        self.assertFalse(event["comparison"]["foresight_eligible"])
        self.assertIn(
            "source_health_capture_missing",
            event["comparison"]["foresight_ineligibility_reasons"],
        )
        hindsight = primary["hindsight_value_drift"]
        self.assertEqual(hindsight["foresight_eligible_trades"], 0)
        self.assertIsNone(hindsight["relative_power_edge_drift"]["estimate"])

    def test_unsupported_trade_asset_is_visible_but_never_partially_valued(self):
        source = engine_bundle()
        requested = current_bundle(source)
        original = trade()
        mixed_trade = replace(
            original,
            assets=(
                *original.assets,
                HistoryTransactionAsset(
                    2,
                    None,
                    "other",
                    "primary",
                    "source_asset_" + "d" * 64,
                    HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER,
                ),
            ),
        )
        snapshot = history(source, requested, (mixed_trade,))

        report = build_gm_insights(
            requested,
            snapshot,
            bundle_loader={source.bundle_id: source}.__getitem__,
        )
        evidence = team(report, "primary")["evidence"][0]

        self.assertEqual(report["coverage"]["transactions"]["completed_trades"], 1)
        self.assertEqual(report["coverage"]["valuations"]["valued_trades"], 0)
        self.assertEqual(
            report["coverage"]["valuations"]["unvalued_reasons"],
            {"trade_contains_unsupported_or_unresolved_asset": 1},
        )
        self.assertIn("Unsupported non-player asset", evidence["received"])
        self.assertIsNone(evidence["valuation"]["at_time"])
        self.assertEqual(
            team(report, "primary")["trade_style"]["status"],
            "insufficient_sample",
        )
        self.assertIsNone(team(report, "primary")["trade_style"]["package_shape"])
        self.assertTrue(
            any(
                "fully resolved player-only" in reason
                for reason in team(report, "primary")["proposal_guidance"][
                    "counterevidence"
                ]
            )
        )
        self.assertIn(
            "trade_contains_unsupported_or_unresolved_asset",
            evidence["valuation"]["comparison"][
                "foresight_ineligibility_reasons"
            ],
        )

    def test_partial_capture_marks_frequency_evidence_descriptive_only(self):
        source = engine_bundle()
        requested = current_bundle(source)
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        source_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, source.bundle_id, SOURCE_AT
        )
        partial_capture = capture((trade(),), complete=False)
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (source_binding, requested_binding),
            (partial_capture,),
        )

        report = build_gm_insights(
            requested,
            snapshot,
            bundle_loader={source.bundle_id: source}.__getitem__,
        )
        primary = team(report, "primary")

        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["coverage"]["transactions"]["status"], "partial")
        self.assertEqual(
            primary["trade_activity"]["trades_per_10_weeks"]["confidence"]["status"],
            "unavailable",
        )
        self.assertEqual(report["scope"]["observed_scoring_periods"], 0)
        self.assertIsNone(
            primary["trade_activity"]["trades_per_10_weeks"]["estimate"]
        )
        self.assertIsNone(
            primary["trade_activity"]["next_two_week_trade_propensity"]["estimate"]
        )
        self.assertEqual(
            primary["proposal_guidance"]["confidence"], "uncertain"
        )
        self.assertEqual(primary["roster_construction"]["status"], "current_bundle_only")
        self.assertEqual(
            primary["lineup_behavior"]["captured_lineup_snapshots"], 0
        )
        self.assertTrue(
            any(
                "coverage is partial" in reason.lower()
                for reason in primary["proposal_guidance"]["counterevidence"]
            )
        )

    def test_three_valued_trades_cross_the_sample_threshold_deterministically(self):
        base = engine_bundle()
        source_bundles = [current_bundle(base, f"source-{index}") for index in range(3)]
        requested = current_bundle(base, "requested")
        trade_times = [REQUEST_AT - timedelta(hours=value) for value in (18, 12, 6)]
        trades = tuple(
            trade(f"trade-{index}", recorded_at)
            for index, recorded_at in enumerate(trade_times, start=1)
        )
        source_bindings = tuple(
            HistoryBundleBinding(
                LEAGUE_KEY,
                2026,
                source.bundle_id,
                recorded_at - timedelta(hours=1),
            )
            for source, recorded_at in zip(source_bundles, trade_times)
        )
        requested_binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, requested.bundle_id, REQUEST_AT
        )
        snapshot = LeagueHistorySnapshot(
            requested_binding,
            (*source_bindings, requested_binding),
            _staged_captures(trades, source_bindings),
        )
        bundles = {source.bundle_id: source for source in source_bundles}

        report = build_gm_insights(
            requested, snapshot, bundle_loader=bundles.__getitem__
        )
        primary = team(report, "primary")

        self.assertEqual(report["coverage"]["valuations"]["valued_trades"], 3)
        self.assertEqual(primary["trade_value"]["status"], "available")
        self.assertEqual(
            primary["trade_value"]["relative_power_edge"]["sample"]["raw_n"], 3
        )
        self.assertEqual(primary["trade_value"]["methodology_counts"], {"exact": 3})
        self.assertEqual(
            report,
            build_gm_insights(
                requested, snapshot, bundle_loader=bundles.__getitem__
            ),
        )

    def test_five_healthy_comparisons_support_cautious_hindsight_signal(self):
        report = healthy_comparison_report(5)
        primary = team(report, "primary")
        other = team(report, "other")

        self.assertEqual(report["coverage"]["valuations"]["valued_trades"], 5)
        self.assertEqual(
            report["coverage"]["valuations"]["foresight_eligible_trades"], 5
        )
        primary_drift = primary["hindsight_value_drift"]
        other_drift = other["hindsight_value_drift"]
        self.assertEqual(primary_drift["status"], "available")
        self.assertEqual(primary_drift["foresight_eligible_trades"], 5)
        self.assertEqual(primary_drift["label"], "Negative hindsight value drift")
        self.assertEqual(
            primary_drift["plain_language_alias"], "bad foresight signal"
        )
        self.assertEqual(other_drift["label"], "Positive hindsight value drift")
        self.assertEqual(
            other_drift["plain_language_alias"], "good foresight signal"
        )
        self.assertAlmostEqual(
            primary_drift["relative_power_edge_drift"]["estimate"],
            -other_drift["relative_power_edge_drift"]["estimate"],
        )
        self.assertIn(
            "does not establish skill or causality",
            " ".join(primary["proposal_guidance"]["supporting_evidence"]),
        )
        limitations = " ".join(primary_drift["limitations"])
        self.assertIn(
            "captured physical injury or incomplete weekly health evidence are excluded",
            limitations,
        )
        self.assertIn("not a causal measure of managerial skill", limitations)

    def test_two_healthy_comparisons_do_not_emit_a_foresight_score(self):
        report = healthy_comparison_report(2)
        drift = team(report, "primary")["hindsight_value_drift"]

        self.assertEqual(drift["status"], "insufficient_sample")
        self.assertEqual(drift["foresight_eligible_trades"], 2)
        self.assertIsNone(drift["plain_language_alias"])
        self.assertIsNone(drift["relative_power_edge_drift"]["estimate"])
        self.assertEqual(
            drift["relative_power_edge_drift"]["sample"]["raw_n"], 2
        )

    def test_partial_transaction_coverage_never_emits_foresight_tendency(self):
        report = healthy_comparison_report(5, transaction_complete=False)
        drift = team(report, "primary")["hindsight_value_drift"]

        self.assertEqual(report["coverage"]["transactions"]["status"], "partial")
        self.assertEqual(drift["foresight_eligible_trades"], 5)
        self.assertEqual(drift["status"], "partial")
        self.assertIsNone(drift["plain_language_alias"])
        self.assertIsNone(team(report, "primary")["trade_value"]["plain_language_alias"])
        self.assertIn(
            "Not enough contemporaneous trades",
            team(report, "primary")["counterparty_value_opportunity"]["label"],
        )
        self.assertEqual(
            drift["relative_power_edge_drift"]["confidence"]["status"],
            "descriptive_only",
        )
        self.assertFalse(
            drift["relative_power_edge_drift"]["evidence"]["coverage_complete"]
        )

    def test_value_alias_and_value_guidance_require_three_exact_valuations(self):
        pooled = {
            "interval_80": (0.8, 1.2),
            "interval_90": (0.7, 1.3),
            "interval_95": (0.6, 1.4),
        }

        inferred = _value_label(pooled, 0, True)
        value = {"plain_language_alias": inferred["alias"]}
        guidance = _proposal_guidance(
            "Primary",
            {
                "completed_trades": 5,
                "unique_partners": 1,
                "trades_per_10_weeks": {"league_percentile": 0.5},
            },
            value,
            {
                "status": "insufficient_sample",
                "package_shape": None,
            },
            {
                "acquisitions_per_10_weeks": {"league_percentile": None},
                "waiver_awards": 0,
                "free_agent_additions": 0,
            },
            {"active_fullness": 0.5},
            {"plain_language_alias": None},
            (),
            True,
        )

        self.assertIsNone(inferred["alias"])
        self.assertIn("exact at-time", " ".join(inferred["reasons"]))
        self.assertTrue(
            any(
                "does not establish a stingy, generous, or even tendency"
                in row
                for row in guidance["counterevidence"]
            )
        )
        self.assertFalse(
            any(
                phrase in action
                for action in guidance["actions"]
                for phrase in ("value-capturing", "concessionary", "even-value")
            )
        )

    def test_approximate_rows_cannot_dominate_exact_value_signal(self):
        def valued_row(methodology_status, edge):
            own = SimpleNamespace(
                team_id="primary",
                relative_power_edge=edge,
                power_delta=edge / 2,
                playoff_probability_delta=None,
            )
            other = SimpleNamespace(
                team_id="other",
                relative_power_edge=-edge,
                power_delta=-edge / 2,
                playoff_probability_delta=None,
            )
            valuation = SimpleNamespace(
                methodology_status=methodology_status,
                outcomes=(own, other),
            )
            return valuation, own

        exact = tuple(valued_row("exact", 2.0) for _ in range(3))
        approximate = tuple(
            valued_row("surrogate", -20.0) for _ in range(20)
        )

        summary = _trade_value_summary(
            (*exact, *approximate),
            (2.0, -2.0) * 3,
            {"primary": 1.0, "other": -1.0},
            True,
        )

        self.assertEqual(summary["exact_valued_trades"], 3)
        self.assertEqual(
            summary["relative_power_edge"]["sample"]["raw_n"], 3
        )
        self.assertGreater(summary["relative_power_edge"]["estimate"], 0)
        self.assertLess(
            summary["all_methodologies_relative_power_edge_mean"], 0
        )
        self.assertEqual(
            summary["methodology_counts"], {"exact": 3, "surrogate": 20}
        )

    def test_offseason_trade_stays_raw_but_not_in_completed_week_rates(self):
        offseason = replace(trade("offseason"), effective_week=0)
        week_one = replace(trade("week-one"), effective_week=1)
        row = SimpleNamespace(
            team_id="primary",
            trades=(offseason, week_one),
            trade_weeks=frozenset({0, 1}),
            partners=Counter({"other": 2}),
            first_observed_at={
                offseason.transaction_id: REQUEST_AT - timedelta(hours=2),
                week_one.transaction_id: REQUEST_AT - timedelta(hours=1),
            },
        )

        activity = _trade_activity(
            row,
            1,
            {"primary": 10.0},
            True,
        )

        self.assertEqual(activity["completed_trades"], 2)
        self.assertEqual(activity["frequency_eligible_completed_trades"], 1)
        self.assertEqual(activity["trades_per_10_weeks"]["sample"]["raw_n"], 1)
        self.assertEqual(activity["trade_active_week_rate"]["estimate"], 1.0)

    def test_stale_capture_downgrades_coverage_and_current_roster_evidence(self):
        bundle = engine_bundle()
        binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, bundle.bundle_id, REQUEST_AT
        )
        stale = capture(
            (trade(),), captured_at=REQUEST_AT - timedelta(hours=2)
        )
        snapshot = LeagueHistorySnapshot(binding, (binding,), (stale,))

        report = build_gm_insights(bundle, snapshot)

        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["coverage"]["current_capture_fresh_for_bundle"])
        self.assertEqual(report["coverage"]["transactions"]["status"], "partial")
        self.assertEqual(report["coverage"]["rosters"]["status"], "partial")
        self.assertEqual(
            team(report, "primary")["roster_construction"]["status"],
            "current_bundle_only",
        )

    def test_retention_waits_until_transaction_is_first_observed(self):
        bundle = engine_bundle()
        event = replace(
            trade("not-used"),
            transaction_id="free-agent-retention",
            kind="free_agent",
            recorded_at=TRADE_AT - timedelta(hours=4),
            assets=(HistoryTransactionAsset(0, "w1", None, "primary"),),
        )
        before_execution = capture(
            (), event.recorded_at + timedelta(minutes=30)
        )
        roster_after = (
            HistoryTeamRoster(
                "primary",
                (
                    HistoryRosterPlayer("p1", "FLEX"),
                    HistoryRosterPlayer("p2", "BENCH"),
                    HistoryRosterPlayer("w1", "BENCH"),
                ),
            ),
            HistoryTeamRoster(
                "other",
                (
                    HistoryRosterPlayer("q1", "FLEX"),
                    HistoryRosterPlayer("q2", "BENCH"),
                ),
            ),
        )
        first_observed = replace(
            capture(
                (event,), event.recorded_at + timedelta(hours=2)
            ),
            rosters=roster_after,
        )
        follow_up = replace(capture((event,)), rosters=roster_after)
        binding = HistoryBundleBinding(
            LEAGUE_KEY, 2026, bundle.bundle_id, REQUEST_AT
        )
        snapshot = LeagueHistorySnapshot(
            binding,
            (binding,),
            (before_execution, first_observed, follow_up),
        )

        report = build_gm_insights(bundle, snapshot)
        retention = team(report, "primary")["acquisition_behavior"][
            "next_snapshot_retention"
        ]

        self.assertEqual(retention["eligible_additions"], 1)
        self.assertEqual(retention["retained_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
