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


def _assembled(source_league_id="77"):
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
        league_state=SimpleNamespace(season=2026),
        team_ids_for=lambda provider: (
            {"primary": "1", "other": "2"} if provider == "espn" else {}
        ),
    )
    assembled = object.__new__(AssembledWeeklyEvidence)
    object.__setattr__(assembled, "identities", identities)
    object.__setattr__(assembled, "league_inputs", league_inputs)
    return assembled


def _capture(*, extra_trade_items=()):
    payload = league_payload(
        [
            transaction(
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
            ),
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
    return espn_activity_capture(payload, captured_at=CAPTURED_AT)


class HistoryIngestTests(unittest.TestCase):
    def test_canonicalizes_activity_without_retaining_provider_league_identity(self):
        bundle = engine_bundle()

        capture, binding = canonicalize_espn_history(
            _capture(),
            _assembled(),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )

        self.assertTrue(capture.league_key.startswith("league_"))
        self.assertEqual(len(capture.league_key), len("league_") + 64)
        self.assertFalse(hasattr(capture, "source_league_id"))
        self.assertEqual(binding.bundle_id, bundle.bundle_id)
        self.assertEqual(binding.league_key, capture.league_key)
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

    def test_canonical_history_retains_unsupported_trade_asset_without_provider_data(self):
        bundle = engine_bundle()
        source = _capture(
            extra_trade_items=(
                item(0, 2, 1, item_type="DRAFT", overall_pick=7),
            )
        )

        capture, _ = canonicalize_espn_history(
            source,
            _assembled(),
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
        bundle = engine_bundle()
        source = _capture(
            extra_trade_items=(
                item(0, 2, 1, item_type="DRAFT", overall_pick=7),
                item(0, 2, 1, item_type="DRAFT", overall_pick=8),
            )
        )

        first, _ = canonicalize_espn_history(
            source,
            _assembled(),
            bundle,
            bundle_captured_at=CAPTURED_AT,
        )
        second, _ = canonicalize_espn_history(
            source,
            _assembled(),
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
        with self.assertRaisesRegex(ValueError, "does not match"):
            canonicalize_espn_history(
                _capture(),
                _assembled(),
                engine_bundle(),
                bundle_captured_at=CAPTURED_AT + timedelta(hours=1, seconds=1),
            )

    def test_fails_closed_on_mismatched_league_or_unresolved_current_roster(self):
        bundle = engine_bundle()
        with self.assertRaisesRegex(ValueError, "does not match"):
            canonicalize_espn_history(
                _capture(),
                _assembled("different"),
                bundle,
                bundle_captured_at=CAPTURED_AT,
            )

        unresolved = _capture()
        assembled = _assembled()
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


if __name__ == "__main__":
    unittest.main()
