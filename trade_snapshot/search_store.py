"""Resumable SQLite checkpoints and qualified trade results."""

import os
import json
from pathlib import Path
import sqlite3
from ._search_store_records import (
    QualifiedSearchResult,
    SearchResumeState,
    SearchRunDefinition,
    _ADJUSTMENT_FIELDS,
    _ODDS_FIELDS,
    _POWER_FIELDS,
    _canonical_json,
    _sqlite_integer,
    _strict_json_loads,
)


DATABASE_SCHEMA_VERSION = 2

class SearchStoreError(RuntimeError):
    pass


class SearchRunMismatchError(SearchStoreError):
    pass


class SearchStore:
    """One SQLite file bound to exactly one search run definition."""

    def __init__(self, database_path: str | os.PathLike[str], run: SearchRunDefinition):
        if not isinstance(run, SearchRunDefinition):
            raise ValueError("run must be a SearchRunDefinition")
        try:
            path_text = os.fspath(database_path)
        except TypeError:
            raise ValueError("database_path must be a filesystem path") from None
        if not isinstance(path_text, str) or not path_text:
            raise ValueError("database_path must be a non-empty filesystem path")
        self.path, self.run = Path(path_text), run
        self._connection: sqlite3.Connection | None = None
        self.journal_mode = "unknown"
        try:
            connection = sqlite3.connect(path_text, timeout=30.0)
            connection.row_factory = sqlite3.Row
            self._connection = connection
            connection.execute("PRAGMA busy_timeout = 30000")
            self.journal_mode = self._try_wal(connection)
            self._migrate(connection)
            self._bind_run(connection)
        except SearchStoreError:
            self.close()
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            self.close()
            raise SearchStoreError(f"could not open search store: {error}") from None

    def __enter__(self) -> "SearchStore":
        self._require_open()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def checkpoint(self, next_candidate_index: int) -> None:
        target = self._checked_checkpoint(next_candidate_index)
        connection = self._require_open()
        with connection:
            self._advance_checkpoint(connection, target)

    def upsert_qualified_result(
        self,
        result: QualifiedSearchResult,
        *,
        next_candidate_index: int | None = None,
    ) -> None:
        if not isinstance(result, QualifiedSearchResult):
            raise ValueError("result must be a QualifiedSearchResult")
        if result.candidate_index >= self.run.total_candidate_count:
            raise ValueError("candidate_index is outside this search run")
        target = None if next_candidate_index is None else self._checked_checkpoint(next_candidate_index)
        if target is not None and target <= result.candidate_index:
            raise ValueError("next_candidate_index must be after the saved candidate")
        values = (
            result.candidate_index,
            _canonical_json(list(result.outgoing_player_ids)),
            _canonical_json(list(result.incoming_player_ids)),
            *(
                _canonical_json(list(getattr(result, name)))
                for name in _ADJUSTMENT_FIELDS
            ),
            *(getattr(result, name) for name in _POWER_FIELDS + _ODDS_FIELDS),
        )
        connection = self._require_open()
        with connection:
            connection.execute(_UPSERT_RESULT_SQL, values)
            if target is not None:
                self._advance_checkpoint(connection, target)

    def resume(self) -> SearchResumeState:
        connection = self._require_open()
        row = connection.execute(_SELECT_CHECKPOINT_SQL).fetchone()
        if row is None:
            raise SearchStoreError("search store is missing its run definition")
        next_index = _sqlite_integer("stored next_candidate_index", row[0])
        if next_index > self.run.total_candidate_count:
            raise SearchStoreError("stored checkpoint exceeds total_candidate_count")
        try:
            rows = connection.execute("SELECT * FROM qualified_result ORDER BY candidate_index")
            results = tuple(self._result_from_row(saved) for saved in rows)
            if results and results[-1].candidate_index >= self.run.total_candidate_count:
                raise ValueError("stored candidate_index is outside this search run")
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as error:
            raise SearchStoreError(f"stored qualified result is invalid: {error}") from None
        return SearchResumeState(next_index, results)

    def _checked_checkpoint(self, value: int) -> int:
        target = _sqlite_integer("next_candidate_index", value)
        if target > self.run.total_candidate_count:
            raise ValueError("next_candidate_index exceeds total_candidate_count")
        return target

    @staticmethod
    def _try_wal(connection: sqlite3.Connection) -> str:
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            return str(row[0]).casefold() if row else "unknown"
        except sqlite3.DatabaseError:
            return "unsupported"

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > DATABASE_SCHEMA_VERSION:
            raise SearchStoreError("search store uses a newer database schema")
        if version == 0:
            with connection:
                connection.execute(_CREATE_RUN_SQL)
                connection.execute(_CREATE_RESULT_SQL)
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        elif version == 1:
            with connection:
                for name in _ADJUSTMENT_FIELDS:
                    connection.execute(
                        f"ALTER TABLE qualified_result ADD COLUMN {name}_json "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
                connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        elif version != DATABASE_SCHEMA_VERSION:
            raise SearchStoreError("search store database schema cannot be migrated")

    def _bind_run(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(_SELECT_RUN_SQL).fetchone()
        if row is None:
            with connection:
                connection.execute(
                    "INSERT INTO search_run VALUES (1, ?, ?, 0)",
                    (self.run.run_id, _canonical_json(self.run.to_record())),
                )
            return
        try:
            stored = SearchRunDefinition.from_record(_strict_json_loads(row["definition_json"]))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise SearchStoreError(f"stored run definition is invalid: {error}") from None
        if row["run_id"] != stored.run_id or stored != self.run:
            raise SearchRunMismatchError("search store is bound to a different run definition")
        checkpoint = _sqlite_integer("stored next_candidate_index", row["next_candidate_index"])
        if checkpoint > self.run.total_candidate_count:
            raise SearchStoreError("stored checkpoint exceeds total_candidate_count")

    @staticmethod
    def _advance_checkpoint(connection: sqlite3.Connection, target: int) -> None:
        connection.execute(_UPDATE_CHECKPOINT_SQL, (target, target))

    @staticmethod
    def _result_from_row(row: sqlite3.Row) -> QualifiedSearchResult:
        outgoing = _strict_json_loads(row["outgoing_json"])
        incoming = _strict_json_loads(row["incoming_json"])
        adjustments = {
            name: _strict_json_loads(row[f"{name}_json"])
            for name in _ADJUSTMENT_FIELDS
        }
        if (
            not isinstance(outgoing, list)
            or not isinstance(incoming, list)
            or any(not isinstance(value, list) for value in adjustments.values())
        ):
            raise ValueError("stored player packages must be JSON arrays")
        values = {name: row[name] for name in _POWER_FIELDS + _ODDS_FIELDS}
        return QualifiedSearchResult(
            row["candidate_index"],
            tuple(outgoing),
            tuple(incoming),
            **values,
            **{name: tuple(value) for name, value in adjustments.items()},
        )

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SearchStoreError("search store is closed")
        return self._connection


_CREATE_RUN_SQL = ("CREATE TABLE search_run (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                   "run_id TEXT NOT NULL,definition_json TEXT NOT NULL,"
                   "next_candidate_index INTEGER NOT NULL CHECK(next_candidate_index>=0))")
_CREATE_RESULT_SQL = (
    "CREATE TABLE qualified_result (candidate_index INTEGER PRIMARY KEY CHECK(candidate_index>=0),"
    "outgoing_json TEXT NOT NULL,incoming_json TEXT NOT NULL,"
    "primary_added_player_ids_json TEXT NOT NULL,"
    "primary_dropped_player_ids_json TEXT NOT NULL,"
    "counterparty_added_player_ids_json TEXT NOT NULL,"
    "counterparty_dropped_player_ids_json TEXT NOT NULL,"
    "primary_raw_power_delta REAL NOT NULL,primary_display_power_delta REAL NOT NULL,"
    "counterparty_raw_power_delta REAL NOT NULL,counterparty_display_power_delta REAL NOT NULL,"
    "primary_playoff_before REAL,primary_playoff_after REAL,"
    "counterparty_playoff_before REAL,counterparty_playoff_after REAL,"
    "CHECK((primary_playoff_before IS NULL AND primary_playoff_after IS NULL AND "
    "counterparty_playoff_before IS NULL AND counterparty_playoff_after IS NULL) OR "
    "(primary_playoff_before IS NOT NULL AND primary_playoff_after IS NOT NULL AND "
    "counterparty_playoff_before IS NOT NULL AND counterparty_playoff_after IS NOT NULL)))"
)
_RESULT_COLUMNS = (
    "candidate_index",
    "outgoing_json",
    "incoming_json",
    *(f"{name}_json" for name in _ADJUSTMENT_FIELDS),
    *_POWER_FIELDS,
    *_ODDS_FIELDS,
)
_UPDATES = ",".join(f"{name}=excluded.{name}" for name in _RESULT_COLUMNS[1:])
_UPSERT_RESULT_SQL = (
    f"INSERT INTO qualified_result ({','.join(_RESULT_COLUMNS)}) "
    f"VALUES ({','.join('?' for _ in _RESULT_COLUMNS)}) "
    f"ON CONFLICT(candidate_index) DO UPDATE SET {_UPDATES}"
)
_SELECT_CHECKPOINT_SQL = "SELECT next_candidate_index FROM search_run WHERE singleton=1"
_SELECT_RUN_SQL = "SELECT run_id,definition_json,next_candidate_index FROM search_run WHERE singleton=1"
_UPDATE_CHECKPOINT_SQL = "UPDATE search_run SET next_candidate_index=? WHERE singleton=1 AND next_candidate_index<?"
