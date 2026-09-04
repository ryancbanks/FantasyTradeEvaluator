from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from tests.test_engine_bundle import engine_bundle
from tests.test_espn_activity import item, league_payload, transaction
from trade_snapshot.espn_activity import espn_activity_capture
from trade_snapshot.history_ingest import canonicalize_espn_history
from trade_snapshot.identity import (
    IdentityRegistry,
    PlayerIdentity,
    ProviderReference,
)
from trade_snapshot.league_history import (
    HistoryTimestampBasis,
    HistoryTransactionAssetKind,
    HistoryTransactionKind,
)
from trade_snapshot.weekly_assembly import AssembledWeeklyEvidence


CAPTURED_AT = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _bundle(*, league_binding_id=None):
    bundle = engine_bundle()
    manifest = replace(
        bundle.source_manifest,
        host_captured_at=CAPTURED_AT,
        league_binding_id=(
            bundle.source_manifest.league_binding_id
            if league_binding_id is None
            else league_binding_id
        ),
    )
    return replace(bundle, source_manifest=manifest)


def _assembled(source_league_id="77", bundle=None):
    bundle = bundle or _bundle()
    identities = IdentityRegistry(
        tuple(
            PlayerIdentity(
                canonical_id,
                canonical_id.upper(),
                "RB",
                "ARI",
                (ProviderReference("espn", source_id),),
            )
            for canonical_id, source_id in (
                ("p1", "101"),
                ("p2", "102"),
                ("q1", "202"),
                ("q2", "203"),
                ("w1", "303"),
            )
        )
    )
    league_inputs = SimpleNamespace(
        source_provider="espn",
        source_league_id=source_league_id,
        captured_at=CAPTURED_AT,
        league_state=SimpleNamespace(season=2026, snapshot_id="snapshot-1"),
        rosters=bundle.rosters,
        team_ids_for=lambda provider: (
            {"primary": "1", "other": "2"} if provider == "espn" else {}
        ),
    )
    assembled = object.__new__(AssembledWeeklyEvidence)
    object.__setattr__(assembled, "identities", identities)
    object.__setattr__(assembled, "league_inputs", league_inputs)
    return assembled


def _capture(
    *,
    extra_trade_items=(),
    extra_transactions=(),
    transaction_limit=1_000,
    trade_fields=None,
):
    trade_event = transaction(
        "a97ddebc-2b48-4110-9e3a-ee927830b736",
        "TRADE_ACCEPT",
        [
            item(
                101,
                1,
                2,
                from_slot=-1,
                to_slot=-1,
                overall_pick=0,
            ),
            item(
                202,
                2,
                1,
                from_slot=-1,
                to_slot=-1,
                overall_pick=0,
            ),
            *extra_trade_items,
        ],
    )
    if trade_fields:
        trade_event.update(trade_fields)
    payload = league_payload(
        [
            trade_event,
            transaction(
                "12305a93-4917-4800-9e3a-ee927830b736",
                "WAIVER",
                [
                    item(
                        303,
                        0,
                        1,
                        from_slot=-1,
                        to_slot=-1,
                        overall_pick=0,
                    )
                ],
                bid=9,
            ),
            *extra_transactions,
        ]
    )
    payload["teams"][0]["roster"]["entries"] = [
        {
            "playerId": 101,
            "lineupSlotId": 0,
            "playerPoolEntry": {"player": {"injuryStatus": "ACTIVE"}},
        },
        {
            "playerId": 102,
            "lineupSlotId": 20,
            "playerPoolEntry": {"player": {"injuryStatus": "OUT"}},
        },
    ]
    payload["teams"][1]["roster"]["entries"] = [
        {
            "playerId": 202,
            "lineupSlotId": 2,
            "playerPoolEntry": {"player": {"injuryStatus": "ACTIVE"}},
        },
        {
            "playerId": 203,
            "lineupSlotId": 20,
            "playerPoolEntry": {"player": {"injuryStatus": "ACTIVE"}},
        },
    ]
    return espn_activity_capture(
        payload,
        captured_at=CAPTURED_AT,
        transaction_limit=transaction_limit,
    )


class HistoryIngestTests(unittest.TestCase):
    def test_canonicalizes_activity_without_retaining_provider_league_identity(self):
        bundle = _bundle()

        capture, binding = canonicalize_espn_history(
            _capture(),
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )

        self.assertEqual(
            capture.league_key, bundle.source_manifest.league_binding_id
        )
        self.assertFalse(hasattr(capture, "source_league_id"))
        self.assertEqual(binding.bundle_id, bundle.bundle_id)
        self.assertEqual(binding.league_key, capture.league_key)
        self.assertEqual(binding.history_capture_id, capture.capture_id)
        self.assertEqual(binding.host_snapshot_id, "snapshot-1")
        self.assertEqual(
            binding.roster_ownership_id, capture.roster_ownership_id
        )
        self.assertEqual(
            capture.acquisition_evidence.returned_transaction_count, 2
        )
        self.assertEqual(capture.acquisition_evidence.transaction_limit, 1_000)
        self.assertTrue(capture.transaction_history_complete)
        self.assertEqual(
            [(row.team_id, row.name) for row in capture.teams],
            [("other", "Other"), ("primary", "Primary")],
        )
        self.assertEqual(
            [(row.team_id, [(player.canonical_player_id, player.lineup_slot,
                             player.injury_status)
                            for player in row.players]) for row in capture.rosters],
            [
                (
                    "other",
                    [("q1", "RB", "ACTIVE"), ("q2", "BENCH", "ACTIVE")],
                ),
                (
                    "primary",
                    [("p1", "QB", "ACTIVE"), ("p2", "BENCH", "OUT")],
                ),
            ],
        )
        trade = next(
            row for row in capture.transactions
            if row.kind is HistoryTransactionKind.TRADE
        )
        waiver = next(
            row for row in capture.transactions
            if row.kind is HistoryTransactionKind.WAIVER
        )
        self.assertEqual(trade.kind, HistoryTransactionKind.TRADE)
        self.assertEqual(trade.timestamp_basis, HistoryTimestampBasis.ESPN_PROPOSED_DATE)
        self.assertEqual(
            {(row.canonical_player_id, row.from_team_id, row.to_team_id)
             for row in trade.assets},
            {("p1", "primary", "other"), ("q1", "other", "primary")},
        )
        self.assertEqual(waiver.kind, HistoryTransactionKind.WAIVER)
        self.assertEqual(waiver.bid_amount, 9)
        self.assertRegex(trade.transaction_id, r"^espn_event_[0-9a-f]{64}$")
        self.assertTrue(all(row.source_asset_key for row in trade.assets))
        serialized = capture.to_record()
        self.assertNotIn("source_player_id", repr(serialized))
        self.assertNotIn("source_transaction_id", repr(serialized))
        self.assertNotIn("a97ddebc-2b48-4110-9e3a-ee927830b736", repr(serialized))

    def test_preserves_distinct_source_action_times_without_inventing_execution(self):
        bundle = _bundle()
        source = _capture(
            trade_fields={
                "acceptedDate": 1_788_800_350_000,
                "processDate": 1_788_800_375_000,
                "expirationDate": 1_788_900_400_000,
            }
        )

        capture, _ = canonicalize_espn_history(
            source,
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )

        source_trade = next(row for row in source.transactions if row.kind.value == "trade")
        history_trade = next(
            row for row in capture.transactions
            if row.kind is HistoryTransactionKind.TRADE
        )
        self.assertEqual(history_trade.recorded_at, source_trade.proposed_at)
        self.assertEqual(history_trade.accepted_at, source_trade.accepted_at)
        self.assertEqual(history_trade.processed_at, source_trade.processed_at)
        self.assertEqual(history_trade.expires_at, source_trade.expires_at)
        self.assertFalse(hasattr(history_trade, "executed_at"))
        self.assertEqual(
            type(history_trade).from_record(history_trade.to_record()),
            history_trade,
        )

    def test_canonical_history_retains_unsupported_trade_asset_without_provider_data(self):
        bundle = _bundle()
        source = _capture(
            extra_trade_items=(
                item(0, 2, 1, item_type="DRAFT", overall_pick=7),
            )
        )

        capture, _ = canonicalize_espn_history(
            source,
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )

        trade = next(
            row for row in capture.transactions
            if row.kind is HistoryTransactionKind.TRADE
        )
        unsupported = [
            row
            for row in trade.assets
            if row.asset_kind
            is HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER
        ]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(
            (unsupported[0].from_team_id, unsupported[0].to_team_id),
            ("other", "primary"),
        )
        self.assertNotIn("overall_pick", repr(capture))

    def test_repeated_unsupported_assets_have_distinct_stable_pseudonyms(self):
        bundle = _bundle()
        source = _capture(
            extra_trade_items=(
                item(0, 2, 1, item_type="DRAFT", overall_pick=7),
                item(0, 2, 1, item_type="DRAFT", overall_pick=8),
            )
        )

        first, _ = canonicalize_espn_history(
            source,
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )
        second, _ = canonicalize_espn_history(
            source,
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )
        first_trade = next(
            row for row in first.transactions
            if row.kind is HistoryTransactionKind.TRADE
        )
        second_trade = next(
            row for row in second.transactions
            if row.kind is HistoryTransactionKind.TRADE
        )
        keys = [
            row.source_asset_key
            for row in first_trade.assets
            if row.asset_kind
            is HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER
        ]

        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(first_trade, second_trade)

    def test_rejects_activity_capture_stale_for_selected_bundle(self):
        bundle = _bundle()
        with self.assertRaisesRegex(ValueError, "does not match"):
            canonicalize_espn_history(
                _capture(),
                _assembled(bundle=bundle),
                bundle,
                bundle_captured_at=CAPTURED_AT + timedelta(hours=1, seconds=1),
            )

    def test_fails_closed_on_mismatched_league_or_unresolved_current_roster(self):
        bundle = _bundle()
        with self.assertRaisesRegex(ValueError, "does not match"):
            canonicalize_espn_history(
                _capture(),
                _assembled("different", bundle),
                bundle,
                bundle_captured_at=CAPTURED_AT,
            )

        unresolved = _capture()
        assembled = _assembled(bundle=bundle)
        reduced = IdentityRegistry(tuple(
            player for player in assembled.identities.players
            if player.canonical_player_id != "p2"
        ))
        object.__setattr__(assembled, "identities", reduced)
        with self.assertRaisesRegex(ValueError, "not exactly resolved"):
            canonicalize_espn_history(
                unresolved,
                assembled,
                bundle,
                bundle_captured_at=CAPTURED_AT,
            )

    def test_local_binding_salts_history_identifiers_without_source_id_hashing(self):
        first_bundle = _bundle(league_binding_id="league_" + "a" * 32)
        second_bundle = _bundle(league_binding_id="league_" + "b" * 32)

        first, _ = canonicalize_espn_history(
            _capture(),
            _assembled(bundle=first_bundle),
            first_bundle,
            bundle_captured_at=CAPTURED_AT,
        )
        second, _ = canonicalize_espn_history(
            _capture(),
            _assembled(bundle=second_bundle),
            second_bundle,
            bundle_captured_at=CAPTURED_AT,
        )

        self.assertNotEqual(first.league_key, second.league_key)
        self.assertNotEqual(
            first.transactions[0].transaction_id,
            second.transactions[0].transaction_id,
        )
        self.assertNotIn("'77'", repr(first.to_record()))

    def test_acquisition_evidence_explains_filtered_rows_and_provider_cap(self):
        bundle = _bundle()
        cancelled = transaction(
            "cancelled-1",
            "WAIVER",
            [item(303, 0, 1)],
            status="CANCELED",
            date=1_788_800_300_000,
        )
        source = _capture(extra_transactions=(cancelled,))

        capture, _ = canonicalize_espn_history(
            source,
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )

        evidence = capture.acquisition_evidence
        self.assertEqual(evidence.returned_transaction_count, 3)
        self.assertEqual(evidence.normalized_transaction_count, 2)
        self.assertEqual(evidence.skipped[0].count, 1)
        self.assertEqual(
            evidence.skipped[0].reason_code,
            "not_executed",
        )
        self.assertEqual(
            evidence.earliest_source_event_at,
            source.earliest_returned_proposed_at,
        )
        self.assertLess(
            evidence.earliest_source_event_at,
            min(row.recorded_at for row in capture.transactions),
        )

        capped_source = _capture(transaction_limit=2)
        capped, _ = canonicalize_espn_history(
            capped_source,
            _assembled(bundle=bundle),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )
        self.assertFalse(capped.transaction_history_complete)
        self.assertEqual(capped.acquisition_evidence.outcome.value, "captured_partial")

    def test_rejects_activity_roster_that_does_not_match_bound_bundle(self):
        bundle = _bundle()
        assembled = _assembled(bundle=bundle)
        source = _capture()
        mismatched = replace(
            source,
            rosters=(
                replace(source.rosters[0], entries=source.rosters[0].entries[:1]),
                source.rosters[1],
            ),
        )

        with self.assertRaisesRegex(ValueError, "rosters do not exactly match"):
            canonicalize_espn_history(
                mismatched,
                assembled,
                bundle,
                bundle_captured_at=CAPTURED_AT,
            )


if __name__ == "__main__":
    unittest.main()
