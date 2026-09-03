from dataclasses import FrozenInstanceError
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from trade_snapshot.search_store import (
    DATABASE_SCHEMA_VERSION,
    MAX_QUALIFIED_RESULT_BATCH_SIZE,
    QualifiedSearchResult,
    SearchRunDefinition,
    SearchRunMismatchError,
    SearchStore,
    SearchStoreError,
    iter_search_results,
    read_search_results,
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

    def test_writer_ownership_is_exclusive_across_processes_and_crash_safe(self):
        child_attempt = """
import json
import sys
from trade_snapshot.search_store import SearchRunDefinition, SearchStore, SearchStoreError
run = SearchRunDefinition.from_record(json.loads(sys.argv[2]))
try:
    store = SearchStore(sys.argv[1], run)
except SearchStoreError as error:
    print(error)
    raise SystemExit(0 if 'active writer' in str(error) else 2)
store.close()
raise SystemExit(3)
"""
        child_crash = """
import json
import os
import sys
from trade_snapshot.search_store import SearchRunDefinition, SearchStore
run = SearchRunDefinition.from_record(json.loads(sys.argv[2]))
store = SearchStore(sys.argv[1], run)
os._exit(0)
"""
        serialized = json.dumps(self.run.to_record())
        with SearchStore(self.path, self.run):
            attempted = subprocess.run(
                [sys.executable, "-c", child_attempt, str(self.path), serialized],
                cwd=Path(__file__).parents[1],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(attempted.returncode, 0, attempted.stderr)
            self.assertIn("active writer", attempted.stdout)

        crashed = subprocess.run(
            [sys.executable, "-c", child_crash, str(self.path), serialized],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        with SearchStore(self.path, self.run) as recovered:
            self.assertEqual(recovered.resume().next_candidate_index, 0)

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

    def test_batch_upsert_is_idempotent_and_empty_batch_can_checkpoint(self):
        with SearchStore(self.path, self.run) as store:
            batch = (qualified(5), qualified(2))
            store.upsert_qualified_results(batch, next_candidate_index=6)
            store.upsert_qualified_results(batch, next_candidate_index=6)
            store.upsert_qualified_results((), next_candidate_index=9)
            store.upsert_qualified_results((), next_candidate_index=3)

            state = store.resume()

        self.assertEqual(state.next_candidate_index, 9)
        self.assertEqual(
            tuple(row.candidate_index for row in state.qualified_results),
            (2, 5),
        )

    def test_batch_is_bounded_unique_and_fully_validated_before_writes(self):
        yielded = 0

        def oversized():
            nonlocal yielded
            for _ in range(MAX_QUALIFIED_RESULT_BATCH_SIZE + 2):
                yielded += 1
                yield qualified(0)

        with SearchStore(self.path, self.run) as store:
            with self.assertRaisesRegex(ValueError, "at most"):
                store.upsert_qualified_results(
                    oversized(), next_candidate_index=1
                )
            self.assertEqual(yielded, MAX_QUALIFIED_RESULT_BATCH_SIZE + 1)
            with self.assertRaisesRegex(ValueError, "iterable"):
                store.upsert_qualified_results("not-results", next_candidate_index=1)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                store.upsert_qualified_results(
                    (qualified(1), qualified(1)), next_candidate_index=2
                )
            with self.assertRaisesRegex(ValueError, "outside"):
                store.upsert_qualified_results(
                    (qualified(1), qualified(20)), next_candidate_index=20
                )
            with closing(sqlite3.connect(self.path)) as database:
                database.execute(
                    "CREATE TRIGGER reject_second_result BEFORE INSERT "
                    "ON qualified_result WHEN NEW.candidate_index=2 "
                    "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
                )
                database.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                store.upsert_qualified_results(
                    (qualified(1), qualified(2)), next_candidate_index=3
                )

            state = store.resume()

        self.assertEqual(state.next_candidate_index, 0)
        self.assertEqual(state.qualified_results, ())

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

    def test_resume_summary_rejects_a_result_beyond_the_durable_checkpoint(self):
        with SearchStore(self.path, self.run) as store:
            store.upsert_qualified_result(qualified(0))
            with self.assertRaisesRegex(SearchStoreError, "checkpoint"):
                store.resume_summary()

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

    def test_post_close_reader_ranks_limits_streams_and_honors_snapshot_watermark(self):
        rows = (
            qualified(
                1,
                primary_playoff_before=20,
                primary_playoff_after=25,
                counterparty_playoff_before=30,
                counterparty_playoff_after=35,
            ),
            qualified(
                2,
                primary_playoff_before=20,
                primary_playoff_after=40,
                counterparty_playoff_before=30,
                counterparty_playoff_after=25,
            ),
            qualified(
                3,
                primary_playoff_before=20,
                primary_playoff_after=22,
                counterparty_playoff_before=30,
                counterparty_playoff_after=33,
            ),
        )
        with SearchStore(self.path, self.run) as store:
            store.upsert_qualified_results(rows, next_candidate_index=4)

        common = {
            "expected_run_id": self.run.run_id,
            "expected_result_count": 3,
            "maximum_candidate_index": 4,
            "best_first": True,
        }
        self.assertEqual(
            tuple(row.candidate_index for row in read_search_results(self.path, 2, **common)),
            (1, 3),
        )
        self.assertEqual(
            tuple(row.candidate_index for row in iter_search_results(self.path, **common)),
            (1, 3, 2),
        )

        with SearchStore(self.path, self.run) as store:
            store.upsert_qualified_result(qualified(5), next_candidate_index=6)
        self.assertEqual(
            tuple(row.candidate_index for row in read_search_results(self.path, **common)),
            (1, 3, 2),
        )

    def test_limited_reader_decodes_only_preview_and_checks_aggregate_identity(self):
        with SearchStore(self.path, self.run) as store:
            store.upsert_qualified_results(
                (qualified(1), qualified(2)), next_candidate_index=3
            )
        with closing(sqlite3.connect(self.path)) as database:
            database.execute(
                "UPDATE qualified_result SET outgoing_json='NaN' "
                "WHERE candidate_index=2"
            )
            database.commit()

        with patch.object(
            SearchStore,
            "_result_from_row",
            wraps=SearchStore._result_from_row,
        ) as decode:
            preview = read_search_results(
                self.path,
                1,
                expected_run_id=self.run.run_id,
                expected_result_count=2,
                maximum_candidate_index=3,
                best_first=True,
            )
        self.assertEqual(tuple(row.candidate_index for row in preview), (1,))
        self.assertEqual(decode.call_count, 1)
        with patch.object(
            SearchStore,
            "_result_from_row",
            wraps=SearchStore._result_from_row,
        ) as stream_decode:
            streamed_preview = tuple(
                iter_search_results(
                    self.path,
                    1,
                    expected_run_id=self.run.run_id,
                    expected_result_count=2,
                    maximum_candidate_index=3,
                    best_first=True,
                )
            )
        self.assertEqual(tuple(row.candidate_index for row in streamed_preview), (1,))
        self.assertEqual(stream_decode.call_count, 1)

        with self.assertRaisesRegex(SearchStoreError, "count"):
            read_search_results(
                self.path,
                1,
                expected_result_count=1,
                maximum_candidate_index=3,
            )
        with self.assertRaisesRegex(SearchStoreError, "checkpoint"):
            read_search_results(
                self.path,
                1,
                expected_result_count=2,
                maximum_candidate_index=4,
            )
        with self.assertRaises(SearchRunMismatchError):
            read_search_results(self.path, expected_run_id="search-not-this-run")

        with closing(sqlite3.connect(self.path)) as database:
            database.execute(
                "UPDATE qualified_result SET outgoing_json='NaN' "
                "WHERE candidate_index=1"
            )
            database.commit()
        with self.assertRaisesRegex(SearchStoreError, "read search results"):
            read_search_results(
                self.path,
                1,
                expected_result_count=2,
                maximum_candidate_index=3,
            )


if __name__ == "__main__":
    unittest.main()
