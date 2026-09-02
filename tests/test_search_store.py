from dataclasses import FrozenInstanceError
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trade_snapshot.search_store import (
    DATABASE_SCHEMA_VERSION,
    QualifiedSearchResult,
    SearchRunDefinition,
    SearchRunMismatchError,
    SearchStore,
    SearchStoreError,
)


def definition(**changes):
    values = {
        "snapshot_id": "snapshot-1",
        "strength_model_id": "strength-v1-abc",
        "primary_team_id": "primary",
        "counterparty_team_id": "counterparty",
        "trade_constraint_record": {
            "balanced_only": False,
            "max_outgoing": 4,
            "excluded_size_pairs": [[1, 1], [2, 2], [3, 3]],
            "locked_player_ids": [],
            "minimum_displayed_power_delta": -5.0,
        },
        "total_candidate_count": 20,
    }
    values.update(changes)
    return SearchRunDefinition(**values)


def qualified(index, **changes):
    values = {
        "candidate_index": index,
        "outgoing_player_ids": (f"p{index}",),
        "incoming_player_ids": (f"c{index}",),
        "primary_raw_power_delta": 1.234,
        "primary_display_power_delta": 1.2,
        "counterparty_raw_power_delta": -0.044,
        "counterparty_display_power_delta": 0.0,
    }
    values.update(changes)
    return QualifiedSearchResult(**values)


class SearchRunDefinitionTests(unittest.TestCase):
    def test_is_content_addressed_order_independent_and_deeply_immutable(self):
        source = {
            "b": [2, {"nested": True}],
            "a": 1,
        }
        first = definition(trade_constraint_record=source)
        second = definition(
            trade_constraint_record={"a": 1, "b": [2, {"nested": True}]}
        )

        source["b"][1]["nested"] = False
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first, second)
        self.assertTrue(first.trade_constraint_record["b"][1]["nested"])
        with self.assertRaises(TypeError):
            first.trade_constraint_record["new"] = 1
        with self.assertRaises(TypeError):
            first.trade_constraint_record["b"][1]["new"] = 1
        with self.assertRaises(FrozenInstanceError):
            first.snapshot_id = "changed"

        record = first.to_record()
        json.dumps(record, allow_nan=False)
        self.assertEqual(SearchRunDefinition.from_record(record), first)

    def test_rejects_non_json_secrets_tampering_and_invalid_identity(self):
        invalid_records = (
            ({"sizes": (1, 2)}, "strict JSON"),
            ({"sizes": {1, 2}}, "strict JSON"),
            ({"limit": math.nan}, "finite JSON"),
            ({"headers": {"Authorization": "secret"}}, "secret-like"),
        )
        for record, message in invalid_records:
            with self.subTest(record=record):
                with self.assertRaisesRegex(ValueError, message):
                    definition(trade_constraint_record=record)

        for changes, message in (
            ({"total_candidate_count": -1}, "non-negative"),
            ({"total_candidate_count": 1 << 63}, "SQLite"),
            ({"primary_team_id": "same", "counterparty_team_id": "same"}, "different"),
            ({"schema_version": 2}, "schema_version"),
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    definition(**changes)

        record = definition().to_record()
        record["total_candidate_count"] += 1
        with self.assertRaisesRegex(ValueError, "run_id"):
            SearchRunDefinition.from_record(record)


class QualifiedSearchResultTests(unittest.TestCase):
    def test_supports_complete_optional_playoff_odds_and_is_immutable(self):
        result = qualified(
            3,
            primary_playoff_before=20.516,
            primary_playoff_after=51.7,
            counterparty_playoff_before=34.812,
            counterparty_playoff_after=9.9,
        )
        self.assertEqual(result.primary_playoff_after, 51.7)
        with self.assertRaises(FrozenInstanceError):
            result.candidate_index = 4

    def test_rejects_partial_odds_invalid_packages_and_nonfinite_deltas(self):
        with self.assertRaisesRegex(ValueError, "both teams"):
            qualified(0, primary_playoff_before=20, primary_playoff_after=30)
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            qualified(
                0,
                primary_playoff_before=-1,
                primary_playoff_after=30,
                counterparty_playoff_before=40,
                counterparty_playoff_after=50,
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            qualified(0, outgoing_player_ids=("a", "a"))
        with self.assertRaisesRegex(ValueError, "cannot share"):
            qualified(0, outgoing_player_ids=("a",), incoming_player_ids=("a",))
        with self.assertRaisesRegex(ValueError, "finite number"):
            qualified(0, primary_raw_power_delta=math.inf)


class SearchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "search.sqlite3"
        self.run = definition()

    def tearDown(self):
        self.temporary.cleanup()

    def test_creates_versioned_store_and_resumes_monotonic_checkpoint(self):
        with SearchStore(self.path, self.run) as store:
            self.assertIn(store.journal_mode, {"wal", "delete", "unsupported"})
            self.assertEqual(store.resume().next_candidate_index, 0)
            store.checkpoint(8)
            store.checkpoint(3)
            self.assertEqual(store.resume().next_candidate_index, 8)

        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(
                database.execute("PRAGMA user_version").fetchone()[0],
                DATABASE_SCHEMA_VERSION,
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM qualified_result").fetchone()[0],
                0,
            )
        with SearchStore(self.path, self.run) as reopened:
            self.assertEqual(reopened.resume().next_candidate_index, 8)

    def test_upserts_idempotently_orders_results_and_can_checkpoint_atomically(self):
        with SearchStore(self.path, self.run) as store:
            fifth = qualified(5)
            second = qualified(
                2,
                primary_playoff_before=20,
                primary_playoff_after=30,
                counterparty_playoff_before=40,
                counterparty_playoff_after=35,
            )
            store.upsert_qualified_result(fifth)
            store.upsert_qualified_result(second, next_candidate_index=3)
            store.upsert_qualified_result(fifth)
            updated = qualified(5, primary_display_power_delta=9.9)
            store.upsert_qualified_result(updated, next_candidate_index=6)

            state = store.resume()
            self.assertEqual(state.next_candidate_index, 6)
            self.assertEqual(
                [result.candidate_index for result in state.qualified_results],
                [2, 5],
            )
            self.assertEqual(state.qualified_results[1].primary_display_power_delta, 9.9)
            self.assertEqual(state.qualified_results[0], second)

    def test_bounds_fail_before_writes_and_run_mismatch_is_strict(self):
        with SearchStore(self.path, self.run) as store:
            with self.assertRaisesRegex(ValueError, "outside"):
                store.upsert_qualified_result(qualified(20))
            with self.assertRaisesRegex(ValueError, "exceeds"):
                store.checkpoint(21)
            with self.assertRaisesRegex(ValueError, "after"):
                store.upsert_qualified_result(qualified(4), next_candidate_index=4)
            self.assertEqual(store.resume().qualified_results, ())

        with self.assertRaises(SearchRunMismatchError):
            SearchStore(
                self.path,
                definition(trade_constraint_record={"balanced_only": True}),
            )

    def test_future_schema_corruption_and_closed_store_fail_clearly(self):
        future_path = Path(self.temporary.name) / "future.sqlite3"
        with closing(sqlite3.connect(future_path)) as database:
            database.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(SearchStoreError, "newer"):
            SearchStore(future_path, self.run)

        store = SearchStore(self.path, self.run)
        store.upsert_qualified_result(qualified(0))
        store.close()
        store.close()
        with self.assertRaisesRegex(SearchStoreError, "closed"):
            store.resume()

        with closing(sqlite3.connect(self.path)) as database:
            database.execute(
                "UPDATE qualified_result SET outgoing_json = 'NaN' WHERE candidate_index = 0"
            )
            database.commit()
        with SearchStore(self.path, self.run) as corrupted:
            with self.assertRaisesRegex(SearchStoreError, "invalid"):
                corrupted.resume()

    def test_migrates_version_one_results_without_losing_resume_state(self):
        old_path = Path(self.temporary.name) / "version-one.sqlite3"
        definition_json = json.dumps(
            self.run.to_record(), sort_keys=True, separators=(",", ":")
        )
        with closing(sqlite3.connect(old_path)) as database:
            database.execute(
                "CREATE TABLE search_run (singleton INTEGER PRIMARY KEY, run_id TEXT NOT NULL, "
                "definition_json TEXT NOT NULL, next_candidate_index INTEGER NOT NULL)"
            )
            database.execute(
                "CREATE TABLE qualified_result (candidate_index INTEGER PRIMARY KEY, "
                "outgoing_json TEXT NOT NULL, incoming_json TEXT NOT NULL, "
                "primary_raw_power_delta REAL NOT NULL, primary_display_power_delta REAL NOT NULL, "
                "counterparty_raw_power_delta REAL NOT NULL, counterparty_display_power_delta REAL NOT NULL, "
                "primary_playoff_before REAL, primary_playoff_after REAL, "
                "counterparty_playoff_before REAL, counterparty_playoff_after REAL)"
            )
            database.execute(
                "INSERT INTO search_run VALUES (1, ?, ?, 0)",
                (self.run.run_id, definition_json),
            )
            database.execute("PRAGMA user_version = 1")
            database.commit()

        adjusted = qualified(
            2,
            primary_added_player_ids=("fa1",),
            counterparty_dropped_player_ids=("bench",),
        )
        with SearchStore(old_path, self.run) as store:
            store.upsert_qualified_result(adjusted, next_candidate_index=3)
            self.assertEqual(store.resume().qualified_results, (adjusted,))
        with closing(sqlite3.connect(old_path)) as database:
            self.assertEqual(
                database.execute("PRAGMA user_version").fetchone()[0],
                DATABASE_SCHEMA_VERSION,
            )


if __name__ == "__main__":
    unittest.main()
