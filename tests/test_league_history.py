from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from trade_snapshot._league_history_schema import V1_INDEXES, V1_TABLES
from trade_snapshot._scenario_random import canonical_json
from trade_snapshot.league_history import (
    LEAGUE_HISTORY_APPLICATION_ID,
    LEAGUE_HISTORY_SCHEMA_VERSION,
    HistoryAcquisitionOutcome,
    HistoryBundleBinding,
    HistoryRosterPlayer,
    HistoryTeam,
    HistoryTeamRoster,
    HistoryTimestampBasis,
    HistoryTransaction,
    HistoryTransactionAsset,
    HistoryTransactionAssetKind,
    HistoryTransactionKind,
    LeagueHistoryCapture,
    LeagueHistoryConflictError,
    LeagueHistoryStore,
    LeagueHistoryStoreError,
    make_league_key,
)


NOW = datetime(2026, 9, 8, 18, 30, tzinfo=timezone.utc)
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
BUNDLE_1 = "engine_" + "1" * 64
BUNDLE_2 = "engine_" + "2" * 64


def teams():
    return (HistoryTeam("team-a", "Alpha"), HistoryTeam("team-b", "Bravo"))


def rosters(*, alpha_player="player-a"):
    return (
        HistoryTeamRoster(
            "team-a",
            (
                HistoryRosterPlayer(alpha_player, "QB"),
                HistoryRosterPlayer("player-c", "BENCH"),
            ),
        ),
        HistoryTeamRoster(
            "team-b", (HistoryRosterPlayer("player-b", "RB"),)
        ),
    )


def trade(transaction_id="trade-1", *, recorded_at=NOW - timedelta(days=2)):
    return HistoryTransaction(
        transaction_id,
        recorded_at,
        HistoryTimestampBasis.EXECUTED_AT,
        1,
        HistoryTransactionKind.TRADE,
        (
            HistoryTransactionAsset(0, "player-a", "team-a", "team-b"),
            HistoryTransactionAsset(1, "player-b", "team-b", "team-a"),
        ),
    )


def capture(
    league_key,
    *,
    captured_at=NOW,
    transactions=None,
    roster_rows=None,
    transaction_history_complete=True,
    roster_complete=True,
    lineup_complete=True,
):
    return LeagueHistoryCapture(
        league_key=league_key,
        season=2026,
        captured_at=captured_at,
        coverage_start=START,
        coverage_end=captured_at,
        transaction_history_complete=transaction_history_complete,
        roster_complete=roster_complete,
        lineup_complete=lineup_complete,
        teams=teams(),
        transactions=tuple(transactions if transactions is not None else (trade(),)),
        rosters=tuple(roster_rows if roster_rows is not None else rosters()),
    )


def write_v1_store(path, row, binding):
    transaction_row = row.transactions[0]
    capture_body = canonical_json(
        {
            "rosters": [item.to_record() for item in row.rosters],
            "teams": [item.to_record() for item in row.teams],
            "transactions": [transaction_row.to_record()],
        }
    )
    with closing(sqlite3.connect(path)) as database, database:
        for statement in V1_TABLES:
            database.execute(statement)
        for statement in V1_INDEXES:
            database.execute(statement)
        database.execute(
            "INSERT INTO history_capture VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row.capture_id,
                row.league_key,
                row.season,
                _canonical_time(row.captured_at),
                _canonical_time(row.coverage_start),
                _canonical_time(row.coverage_end),
                1,
                1,
                1,
                capture_body,
            ),
        )
        database.execute(
            "INSERT INTO transaction_event VALUES (?,?,?,?,?,?,?)",
            (
                row.league_key,
                row.season,
                transaction_row.transaction_id,
                _canonical_time(transaction_row.recorded_at),
                transaction_row.timestamp_basis.value,
                transaction_row.kind.value,
                canonical_json(transaction_row.to_record()),
            ),
        )
        database.execute(
            "INSERT INTO capture_transaction VALUES (?,?,?,?)",
            (
                row.capture_id,
                row.league_key,
                row.season,
                transaction_row.transaction_id,
            ),
        )
        database.execute(
            "INSERT INTO bundle_binding VALUES (?,?,?,?,?)",
            (
                binding.bundle_id,
                binding.league_key,
                binding.season,
                _canonical_time(binding.captured_at),
                canonical_json(
                    {
                        "bundle_id": binding.bundle_id,
                        "captured_at": _canonical_time(binding.captured_at),
                        "league_key": binding.league_key,
                        "season": binding.season,
                    }
                ),
            ),
        )
        database.execute(f"PRAGMA application_id = {LEAGUE_HISTORY_APPLICATION_ID}")
        database.execute("PRAGMA user_version = 1")


def _canonical_time(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class HistoryRecordTests(unittest.TestCase):
    def test_history_evidence_imports_before_service_without_cycle(self):
        result = subprocess.run(
            (
                sys.executable,
                "-c",
                "import trade_snapshot._league_history_evidence; "
                "import trade_snapshot.app_service; "
                "import trade_snapshot.local_server",
            ),
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_league_key_is_stable_pseudonymous_and_rejects_unsafe_source_ids(self):
        first = make_league_key(" ESPN ", "private-league-77")
        self.assertEqual(first, make_league_key("espn", "private-league-77"))
        self.assertRegex(first, r"^league_[0-9a-f]{64}$")
        self.assertNotIn("private", first)
        self.assertNotEqual(first, make_league_key("espn", "private-league-78"))
        for provider, source_id in (
            ("https://example.test", "77"),
            ("espn", "https://example.test/77"),
            ("espn", "token=secret"),
        ):
            with self.subTest(provider=provider, source_id=source_id):
                with self.assertRaises(ValueError):
                    make_league_key(provider, source_id)

    def test_executed_transaction_supports_unresolved_player_without_raw_identity(self):
        event = HistoryTransaction(
            "waiver-1",
            NOW - timedelta(hours=2),
            HistoryTimestampBasis.ESPN_PROPOSED_DATE,
            2,
            HistoryTransactionKind.WAIVER,
            (
                HistoryTransactionAsset(0, None, None, "team-a"),
                HistoryTransactionAsset(1, "player-c", "team-a", None),
            ),
            bid_amount=11,
        )
        self.assertIsNone(event.assets[0].canonical_player_id)
        self.assertEqual(event.participant_team_ids, ("team-a",))
        self.assertEqual(event.bid_amount, 11.0)
        self.assertEqual(event.timestamp_basis, HistoryTimestampBasis.ESPN_PROPOSED_DATE)
        self.assertEqual(event.to_record()["execution_status"], "executed")
        self.assertEqual(event.to_record()["timestamp_basis"], "espn_proposed_date")

    def test_rejects_nonexecuted_or_structurally_invalid_movements(self):
        with self.assertRaisesRegex(ValueError, "different teams"):
            HistoryTransaction(
                "trade-1",
                NOW,
                HistoryTimestampBasis.EXECUTED_AT,
                1,
                HistoryTransactionKind.TRADE,
                (HistoryTransactionAsset(0, "p1", "team-a", "team-a"),),
            )
        with self.assertRaisesRegex(ValueError, "contiguous"):
            HistoryTransaction(
                "drop-1",
                NOW,
                HistoryTimestampBasis.EXECUTED_AT,
                1,
                HistoryTransactionKind.DROP,
                (HistoryTransactionAsset(2, "p1", "team-a", None),),
            )
        record = trade().to_record()
        record["execution_status"] = "pending"
        with self.assertRaisesRegex(ValueError, "executed"):
            HistoryTransaction.from_record(record)
        with self.assertRaisesRegex(ValueError, "bid_amount"):
            replace(trade(), bid_amount=-1)
        with self.assertRaisesRegex(ValueError, "timestamp_basis"):
            replace(trade(), timestamp_basis="provider_guess")

    def test_capture_validates_coverage_roster_and_transaction_ownership(self):
        key = make_league_key("espn", "77")
        with self.assertRaisesRegex(ValueError, "coverage"):
            replace(capture(key), coverage_end=NOW + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "every team"):
            replace(capture(key), rosters=rosters()[:1])
        unknown_team_trade = replace(
            trade(),
            assets=(
                HistoryTransactionAsset(0, "player-a", "team-a", "team-x"),
                HistoryTransactionAsset(1, "player-b", "team-b", "team-a"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown team"):
            replace(capture(key), transactions=(unknown_team_trade,))
        with self.assertRaisesRegex(ValueError, "lineup_complete"):
            replace(capture(key), roster_complete=False, lineup_complete=True)

    def test_v2_capture_round_trip_authenticates_acquisition_provenance(self):
        row = capture(make_league_key("espn", "77"))
        record = row.to_record()

        self.assertEqual(LeagueHistoryCapture.from_record(record), row)
        changed = json.loads(json.dumps(record))
        changed["acquisition_evidence"]["provider"] = "other"
        with self.assertRaisesRegex(ValueError, "capture_id"):
            LeagueHistoryCapture.from_record(changed)


class LeagueHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "league-history.sqlite3"
        self.key = make_league_key("espn", "private-league-77")
        self.store = LeagueHistoryStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_ingest_bind_and_query_return_deterministic_json_ready_history(self):
        row = capture(self.key)
        binding = HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW)
        self.assertEqual(self.store.ingest(row, bundle=binding), row.capture_id)

        snapshot = self.store.snapshot_for_bundle(BUNDLE_1)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.bundle_id, BUNDLE_1)
        self.assertEqual(snapshot.league_key, self.key)
        self.assertEqual(snapshot.bundle_bindings, (binding,))
        self.assertEqual(snapshot.captures, (row,))
        self.assertEqual(snapshot.transactions, (trade(),))
        self.assertEqual(snapshot.latest_teams, teams())
        self.assertEqual(self.store.revision_for_bundle(BUNDLE_1), snapshot.history_revision)
        record = snapshot.to_record()
        self.assertEqual(record["history_revision"], snapshot.history_revision)
        self.assertEqual(record["transactions"][0]["execution_status"], "executed")
        json.dumps(record, allow_nan=False)

        reopened = LeagueHistoryStore(self.path).snapshot_for_bundle(BUNDLE_1)
        self.assertEqual(reopened, snapshot)

    def test_exact_reingestion_and_rebinding_are_noops(self):
        row = capture(self.key)
        self.store.ingest(row)
        self.store.bind_bundle(self.key, 2026, BUNDLE_1, NOW)
        before = self.store.revision_for_bundle(BUNDLE_1)
        self.store.ingest(row)
        self.store.bind_bundle(self.key, 2026, BUNDLE_1, NOW)
        self.assertEqual(self.store.revision_for_bundle(BUNDLE_1), before)
        snapshot = self.store.snapshot_for_bundle(BUNDLE_1)
        self.assertEqual(len(snapshot.captures), 1)
        self.assertEqual(len(snapshot.transactions), 1)

        self.store.bind_bundle(self.key, 2026, BUNDLE_2, NOW)
        self.assertEqual(
            self.store.snapshot_for_bundle(BUNDLE_1).bundle_bindings,
            (snapshot.requested_binding,),
        )

    def test_each_bundle_snapshot_is_bounded_to_its_own_capture_time(self):
        first = capture(self.key, captured_at=NOW - timedelta(hours=1))
        first_binding = HistoryBundleBinding(
            self.key, 2026, BUNDLE_1, NOW - timedelta(hours=1)
        )
        self.store.ingest(first, bundle=first_binding)
        old_revision = self.store.revision_for_bundle(BUNDLE_1)

        second_event = HistoryTransaction(
            "free-agent-1",
            NOW - timedelta(minutes=30),
            HistoryTimestampBasis.EXECUTED_AT,
            2,
            HistoryTransactionKind.FREE_AGENT,
            (HistoryTransactionAsset(0, "player-d", None, "team-a"),),
        )
        second = capture(
            self.key,
            captured_at=NOW,
            transactions=(trade(), second_event),
            roster_rows=rosters(alpha_player="player-d"),
        )
        second_binding = HistoryBundleBinding(self.key, 2026, BUNDLE_2, NOW)
        self.store.ingest(second, bundle=second_binding)

        old_snapshot = self.store.snapshot_for_bundle(BUNDLE_1)
        self.assertEqual(old_snapshot.history_revision, old_revision)
        self.assertEqual(old_snapshot.bundle_bindings, (first_binding,))
        self.assertEqual(
            tuple(row.transaction_id for row in old_snapshot.transactions),
            ("trade-1",),
        )
        self.assertEqual(old_snapshot.evidence_as_of, first_binding.captured_at)

        current_snapshot = self.store.snapshot_for_bundle(BUNDLE_2)
        self.assertEqual(
            current_snapshot.bundle_bindings, (first_binding, second_binding)
        )
        self.assertEqual(
            tuple(row.transaction_id for row in current_snapshot.transactions),
            ("trade-1", "free-agent-1"),
        )
        self.assertNotEqual(old_snapshot.history_revision, current_snapshot.history_revision)

    def test_conflicting_transaction_rolls_back_entire_capture(self):
        original = capture(self.key)
        self.store.ingest(original, bundle=HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW))
        conflicting = replace(
            trade(),
            assets=(
                HistoryTransactionAsset(0, "different-a", "team-a", "team-b"),
                HistoryTransactionAsset(1, "player-b", "team-b", "team-a"),
            ),
        )
        new_event = HistoryTransaction(
            "drop-2",
            NOW - timedelta(hours=1),
            HistoryTimestampBasis.EXECUTED_AT,
            1,
            HistoryTransactionKind.DROP,
            (HistoryTransactionAsset(0, "player-c", "team-a", None),),
        )
        attempted = capture(
            self.key,
            captured_at=NOW + timedelta(days=1),
            transactions=(new_event, conflicting),
        )
        with self.assertRaises(LeagueHistoryConflictError):
            self.store.ingest(attempted)
        snapshot = self.store.snapshot_for_bundle(BUNDLE_1)
        self.assertEqual(snapshot.captures, (original,))
        self.assertEqual(snapshot.transactions, (trade(),))

    def test_conflicting_capture_timestamp_or_bundle_binding_fails_closed(self):
        original = capture(self.key)
        self.store.ingest(original)
        changed = replace(original, transaction_history_complete=False)
        with self.assertRaises(LeagueHistoryConflictError):
            self.store.ingest(changed)

        self.store.bind_bundle(self.key, 2026, BUNDLE_1, NOW)
        with self.assertRaises(LeagueHistoryConflictError):
            self.store.bind_bundle(
                make_league_key("espn", "another-league"), 2026, BUNDLE_1, NOW
            )

    def test_atomic_capture_and_binding_roll_back_together(self):
        self.store.bind_bundle(self.key, 2026, BUNDLE_1, NOW)
        row = capture(self.key, captured_at=NOW + timedelta(days=1))
        bad_binding = HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW + timedelta(days=1))
        with self.assertRaises(LeagueHistoryConflictError):
            self.store.ingest(row, bundle=bad_binding)
        self.assertEqual(self.store.snapshot_for_bundle(BUNDLE_1).captures, ())

    def test_unknown_bundle_returns_none(self):
        self.assertIsNone(self.store.snapshot_for_bundle(BUNDLE_1))
        self.assertIsNone(self.store.revision_for_bundle(BUNDLE_1))
        self.assertIsNone(self.store.snapshot_for_bundle("local-bundle-1"))

    def test_proposal_timestamp_basis_and_bid_round_trip_exactly(self):
        event = replace(
            trade(),
            timestamp_basis=HistoryTimestampBasis.ESPN_PROPOSED_DATE,
            bid_amount=7.5,
            accepted_at=NOW - timedelta(days=2, minutes=-1),
            processed_at=NOW - timedelta(days=2, minutes=-2),
            expires_at=NOW + timedelta(days=1),
        )
        row = capture(self.key, transactions=(event,))
        self.store.ingest(row, bundle=HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW))
        restored = self.store.snapshot_for_bundle(BUNDLE_1).transactions[0]
        self.assertEqual(restored.recorded_at, event.recorded_at)
        self.assertEqual(restored.timestamp_basis, HistoryTimestampBasis.ESPN_PROPOSED_DATE)
        self.assertEqual(restored.bid_amount, 7.5)
        self.assertEqual(restored.accepted_at, event.accepted_at)
        self.assertEqual(restored.processed_at, event.processed_at)
        self.assertEqual(restored.expires_at, event.expires_at)

    def test_database_stores_no_raw_league_id_and_preserves_partial_coverage(self):
        row = capture(
            self.key,
            transaction_history_complete=False,
            roster_complete=False,
            lineup_complete=False,
            roster_rows=(),
        )
        self.store.ingest(row, bundle=HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW))
        serialized = self.path.read_bytes()
        self.assertNotIn(b"private-league-77", serialized)
        restored = self.store.snapshot_for_bundle(BUNDLE_1).captures[0]
        self.assertFalse(restored.transaction_history_complete)
        self.assertFalse(restored.roster_complete)
        self.assertFalse(restored.lineup_complete)

    def test_store_uses_versioned_application_schema_and_attempts_wal(self):
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(
                database.execute("PRAGMA user_version").fetchone()[0],
                LEAGUE_HISTORY_SCHEMA_VERSION,
            )
            self.assertEqual(
                database.execute("PRAGMA application_id").fetchone()[0],
                LEAGUE_HISTORY_APPLICATION_ID,
            )
        self.assertIn(self.store.journal_mode, {"wal", "delete", "truncate", "persist", "memory", "off"})

    def test_schema_v1_database_is_migrated_without_losing_history(self):
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        row = replace(capture(self.key), identity_schema_version=1)
        binding = HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW)
        write_v1_store(legacy_path, row, binding)

        migrated = LeagueHistoryStore(legacy_path)
        snapshot = migrated.snapshot_for_bundle(BUNDLE_1)

        self.assertEqual(snapshot.captures[0].capture_id, row.capture_id)
        self.assertEqual(snapshot.transactions, row.transactions)
        self.assertIsNone(snapshot.requested_binding.history_capture_id)
        self.assertEqual(
            snapshot.captures[0].acquisition_evidence.outcome,
            HistoryAcquisitionOutcome.LEGACY_UNKNOWN,
        )
        with closing(sqlite3.connect(legacy_path)) as database:
            self.assertEqual(
                database.execute("PRAGMA user_version").fetchone()[0], 2
            )

    def test_failed_v1_migration_rolls_back_without_modifying_source_store(self):
        legacy_path = Path(self.temporary.name) / "corrupt-legacy.sqlite3"
        row = replace(capture(self.key), identity_schema_version=1)
        binding = HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW)
        write_v1_store(legacy_path, row, binding)
        with closing(sqlite3.connect(legacy_path)) as database, database:
            database.execute(
                "UPDATE history_capture SET capture_json='{}'"
            )

        with self.assertRaises(LeagueHistoryStoreError):
            LeagueHistoryStore(legacy_path)

        with closing(sqlite3.connect(legacy_path)) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM history_capture").fetchone()[0],
                1,
            )

    def test_exact_binding_must_match_capture_identity_and_roster(self):
        row = replace(capture(self.key), host_snapshot_id="snapshot-1")
        binding = HistoryBundleBinding(
            self.key,
            2026,
            BUNDLE_1,
            NOW,
            "snapshot-1",
            NOW,
            row.capture_id,
            row.roster_ownership_id,
        )

        self.store.ingest(row, bundle=binding)
        restored = self.store.snapshot_for_bundle(BUNDLE_1)
        self.assertEqual(restored.requested_binding, binding)
        self.assertEqual(
            restored.captures[0].roster_ownership_id,
            row.roster_ownership_id,
        )

        wrong = replace(binding, roster_ownership_id="history-roster_" + "f" * 64)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.store.ingest(row, bundle=wrong)

    def test_future_or_unversioned_nonempty_database_is_rejected(self):
        future = Path(self.temporary.name) / "future.sqlite3"
        with closing(sqlite3.connect(future)) as database:
            database.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(LeagueHistoryStoreError, "newer"):
            LeagueHistoryStore(future)

        unrelated = Path(self.temporary.name) / "unrelated.sqlite3"
        with closing(sqlite3.connect(unrelated)) as database:
            database.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaisesRegex(LeagueHistoryStoreError, "unversioned"):
            LeagueHistoryStore(unrelated)

        foreign = Path(self.temporary.name) / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as database, database:
            database.execute("PRAGMA application_id = 12345")
        with self.assertRaisesRegex(LeagueHistoryStoreError, "another application"):
            LeagueHistoryStore(foreign)

    def test_tampered_schema_is_rejected_on_reopen(self):
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("ALTER TABLE bundle_binding ADD COLUMN injected TEXT")
        with self.assertRaisesRegex(LeagueHistoryStoreError, "schema"):
            LeagueHistoryStore(self.path)

    def test_tampered_redundant_event_columns_are_rejected(self):
        self.store.ingest(
            capture(self.key),
            bundle=HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW),
        )
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute(
                "UPDATE transaction_event SET timestamp_basis='espn_proposed_date'"
            )
        with self.assertRaisesRegex(LeagueHistoryStoreError, "stored transaction"):
            self.store.snapshot_for_bundle(BUNDLE_1)

    def test_keyed_resolution_enrichment_preserves_old_capture_and_fails_on_remap(self):
        source_a = "source_asset_" + "a" * 64
        source_b = "source_asset_" + "b" * 64
        unresolved = replace(
            trade(),
            assets=(
                HistoryTransactionAsset(
                    0, None, "team-a", "team-b", source_a
                ),
                HistoryTransactionAsset(
                    1, "player-b", "team-b", "team-a", source_b
                ),
            ),
        )
        first = capture(self.key, transactions=(unresolved,))
        first_id = first.capture_id
        self.store.ingest(
            first,
            bundle=HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW),
        )

        # Simulate the original schema-v1 storage representation, which did
        # not snapshot transaction versions inside capture_json.
        with closing(sqlite3.connect(self.path)) as database, database:
            body = json.loads(
                database.execute(
                    "SELECT capture_json FROM history_capture WHERE capture_id=?",
                    (first_id,),
                ).fetchone()[0]
            )
            body.pop("transactions")
            database.execute(
                "UPDATE history_capture SET capture_json=? WHERE capture_id=?",
                (json.dumps(body, sort_keys=True), first_id),
            )

        resolved = replace(
            unresolved,
            assets=(
                replace(unresolved.assets[0], canonical_player_id="player-a"),
                unresolved.assets[1],
            ),
        )
        second = capture(
            self.key,
            captured_at=NOW + timedelta(days=1),
            transactions=(resolved,),
        )
        self.store.ingest(
            second,
            bundle=HistoryBundleBinding(
                self.key, 2026, BUNDLE_2, NOW + timedelta(days=1)
            ),
        )

        old_snapshot = self.store.snapshot_for_bundle(BUNDLE_1)
        self.assertEqual(len(old_snapshot.captures), 1)
        self.assertEqual(old_snapshot.captures[0].capture_id, first_id)
        self.assertIsNone(
            old_snapshot.captures[0].transactions[0].assets[0].canonical_player_id
        )
        self.assertIsNone(
            old_snapshot.transactions[0].assets[0].canonical_player_id
        )

        snapshot = self.store.snapshot_for_bundle(BUNDLE_2)
        self.assertEqual(
            snapshot.captures[1].transactions[0].assets[0].canonical_player_id,
            "player-a",
        )
        self.assertEqual(
            snapshot.transactions[0].assets[0].canonical_player_id,
            "player-a",
        )
        self.assertEqual(
            LeagueHistoryStore(self.path).snapshot_for_bundle(BUNDLE_2),
            snapshot,
        )

        conflicting = replace(
            resolved,
            assets=(
                replace(resolved.assets[0], canonical_player_id="different-a"),
                resolved.assets[1],
            ),
        )
        third = capture(
            self.key,
            captured_at=NOW + timedelta(days=2),
            transactions=(conflicting,),
        )
        with self.assertRaisesRegex(
            LeagueHistoryConflictError, "conflicting canonical"
        ):
            self.store.ingest(third)
        self.assertEqual(
            len(self.store.snapshot_for_bundle(BUNDLE_2).captures), 2
        )

    def test_source_action_time_enrichment_preserves_as_of_history(self):
        first = capture(self.key)
        self.store.ingest(
            first,
            bundle=HistoryBundleBinding(self.key, 2026, BUNDLE_1, NOW),
        )
        enriched_event = replace(
            trade(),
            accepted_at=NOW - timedelta(days=2, minutes=-1),
            processed_at=NOW - timedelta(days=2, minutes=-2),
            expires_at=NOW + timedelta(days=1),
        )
        second = capture(
            self.key,
            captured_at=NOW + timedelta(days=1),
            transactions=(enriched_event,),
        )
        self.store.ingest(
            second,
            bundle=HistoryBundleBinding(
                self.key, 2026, BUNDLE_2, NOW + timedelta(days=1)
            ),
        )

        self.assertIsNone(
            self.store.snapshot_for_bundle(BUNDLE_1).transactions[0].accepted_at
        )
        current = self.store.snapshot_for_bundle(BUNDLE_2).transactions[0]
        self.assertEqual(current.accepted_at, enriched_event.accepted_at)
        self.assertEqual(current.processed_at, enriched_event.processed_at)
        self.assertEqual(current.expires_at, enriched_event.expires_at)

        conflicting = replace(
            enriched_event,
            accepted_at=enriched_event.accepted_at + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            LeagueHistoryConflictError, "conflicting source action timestamps"
        ):
            self.store.ingest(
                capture(
                    self.key,
                    captured_at=NOW + timedelta(days=2),
                    transactions=(conflicting,),
                )
            )

    def test_unsupported_asset_kind_round_trips_without_becoming_a_player(self):
        asset = HistoryTransactionAsset(
            0,
            None,
            "team-a",
            "team-b",
            "source_asset_" + "c" * 64,
            HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER,
        )

        restored = HistoryTransactionAsset.from_record(asset.to_record())

        self.assertEqual(restored, asset)
        self.assertEqual(
            restored.to_record()["asset_kind"], "unsupported_non_player"
        )
        with self.assertRaisesRegex(ValueError, "cannot resolve"):
            replace(restored, canonical_player_id="player-a")


if __name__ == "__main__":
    unittest.main()
