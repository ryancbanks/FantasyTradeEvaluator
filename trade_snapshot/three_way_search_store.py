"""Versioned SQLite checkpoints for three-team trade searches."""

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3

from ._search_store_records import _canonical_json, _strict_json_loads
from .three_way_search_records import (
    ThreeWayQualifiedResult,
    ThreeWaySearchRunDefinition,
    _decimal_integer,
)


THREE_WAY_DATABASE_SCHEMA_VERSION = 1
THREE_WAY_DATABASE_APPLICATION_ID = 1177769811  # ASCII "F3WS"


class ThreeWaySearchStoreError(RuntimeError):
    pass


class ThreeWaySearchRunMismatchError(ThreeWaySearchStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ThreeWayResumeState:
    next_candidate_index: int
    qualified_result_count: int
    all_playoff_gain_count: int


class ThreeWaySearchStore:
    """One SQLite file bound to exactly one three-team run definition."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        run: ThreeWaySearchRunDefinition,
    ) -> None:
        if not isinstance(run, ThreeWaySearchRunDefinition):
            raise ValueError("run must be a ThreeWaySearchRunDefinition")
        self.path = _path(database_path)
        self.run = run
        self._connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=30.0)
            connection.row_factory = sqlite3.Row
            self._connection = connection
            connection.execute("PRAGMA busy_timeout = 30000")
            initialized = self._prepare_schema(connection)
            self._bind_run(connection, initialized=initialized)
            self._try_wal(connection)
        except ThreeWaySearchStoreError:
            self.close()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            self.close()
            raise ThreeWaySearchStoreError(
                f"could not open three-way search store: {error}"
            ) from None

    def __enter__(self) -> "ThreeWaySearchStore":
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
        result: ThreeWayQualifiedResult,
        *,
        next_candidate_index: int | None = None,
    ) -> None:
        if not isinstance(result, ThreeWayQualifiedResult):
            raise ValueError("result must be a ThreeWayQualifiedResult")
        _require_result_run(result, self.run)
        if result.candidate_index >= self.run.total_candidate_count:
            raise ValueError("candidate_index is outside this search run")
        target = None
        if next_candidate_index is not None:
            target = self._checked_checkpoint(next_candidate_index)
            if target <= result.candidate_index:
                raise ValueError("next_candidate_index must be after the saved candidate")
        connection = self._require_open()
        values = (
            str(result.candidate_index),
            _canonical_json(result.to_record()),
            int(result.all_teams_gain),
            result.combined_playoff_delta,
        )
        with connection:
            connection.execute(_UPSERT_RESULT_SQL, values)
            if target is not None:
                self._advance_checkpoint(connection, target)

    def resume(self) -> ThreeWayResumeState:
        connection = self._require_open()
        stored, next_index = _load_run(connection)
        if stored != self.run:
            raise ThreeWaySearchRunMismatchError(
                "three-way search store is bound to a different run"
            )
        qualified_count = 0
        gain_count = 0
        try:
            for saved in connection.execute(
                "SELECT candidate_index_text, result_json, all_teams_gain, "
                "combined_playoff_delta FROM qualified_result"
            ):
                result = _decode_result(saved, self.run)
                qualified_count += 1
                gain_count += int(result.all_teams_gain)
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as error:
            raise ThreeWaySearchStoreError(
                f"stored three-way result is invalid: {error}"
            ) from None
        return ThreeWayResumeState(next_index, qualified_count, gain_count)

    def results(
        self, limit: int | None = None
    ) -> tuple[ThreeWayQualifiedResult, ...]:
        return _query_results(self._require_open(), limit, self.run)

    def _checked_checkpoint(self, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("next_candidate_index must be a non-negative integer")
        if value > self.run.total_candidate_count:
            raise ValueError("next_candidate_index exceeds total_candidate_count")
        return value

    @staticmethod
    def _try_wal(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.DatabaseError:
            pass

    @staticmethod
    def _prepare_schema(connection: sqlite3.Connection) -> bool:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > THREE_WAY_DATABASE_SCHEMA_VERSION:
            raise ThreeWaySearchStoreError(
                "three-way search store uses a newer database schema"
            )
        if version == 0:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if existing:
                raise ThreeWaySearchStoreError(
                    "unversioned database is not an empty three-way search store"
                )
            with connection:
                connection.execute(_CREATE_RUN_SQL)
                connection.execute(_CREATE_RESULT_SQL)
                connection.execute(
                    f"PRAGMA application_id = {THREE_WAY_DATABASE_APPLICATION_ID}"
                )
                connection.execute(
                    f"PRAGMA user_version = {THREE_WAY_DATABASE_SCHEMA_VERSION}"
                )
            initialized = True
        elif version != THREE_WAY_DATABASE_SCHEMA_VERSION:
            raise ThreeWaySearchStoreError(
                "three-way search store schema cannot be migrated"
            )
        else:
            initialized = False
        _require_existing_schema(connection)
        return initialized

    def _bind_run(
        self, connection: sqlite3.Connection, *, initialized: bool
    ) -> None:
        row = connection.execute(_SELECT_RUN_SQL).fetchone()
        if row is None:
            if not initialized:
                raise ThreeWaySearchStoreError(
                    "three-way search store is missing its run definition"
                )
            with connection:
                connection.execute(
                    "INSERT INTO search_run VALUES (1, ?, ?, '0')",
                    (self.run.run_id, _canonical_json(self.run.to_record())),
                )
            return
        stored, _ = _load_run(connection, row=row)
        if stored != self.run:
            raise ThreeWaySearchRunMismatchError(
                "three-way search store is bound to a different run"
            )

    @staticmethod
    def _advance_checkpoint(connection: sqlite3.Connection, target: int) -> None:
        row = connection.execute(
            "SELECT next_candidate_index_text FROM search_run WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ThreeWaySearchStoreError(
                "three-way search store is missing its run definition"
            )
        current = _stored_index("stored next_candidate_index", row[0])
        if target > current:
            connection.execute(
                "UPDATE search_run SET next_candidate_index_text=? WHERE singleton=1",
                (str(target),),
            )

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ThreeWaySearchStoreError("three-way search store is closed")
        return self._connection


def read_three_way_results(
    database_path: str | os.PathLike[str],
    limit: int | None = None,
    *,
    expected_run_id: str | None = None,
) -> tuple[ThreeWayQualifiedResult, ...]:
    """Read best-first results after the search connection has closed."""

    _limit_clause(limit)
    path = _path(database_path)
    if not path.is_file():
        raise ThreeWaySearchStoreError("three-way search result store does not exist")
    try:
        with closing(sqlite3.connect(path)) as connection:
            _require_existing_schema(connection)
            connection.row_factory = sqlite3.Row
            run, _ = _load_run(connection)
            if expected_run_id is not None and run.run_id != expected_run_id:
                raise ThreeWaySearchRunMismatchError(
                    "three-way search result store belongs to a different run"
                )
            return _query_results(connection, limit, run)
    except ThreeWaySearchStoreError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise ThreeWaySearchStoreError(
            f"could not read three-way search results: {error}"
        ) from None


def _query_results(
    connection: sqlite3.Connection,
    limit: int | None,
    run: ThreeWaySearchRunDefinition,
):
    clause, parameters = _limit_clause(limit)
    try:
        if limit is None:
            results = [
                _decode_result(row, run)
                for row in connection.execute(
                    "SELECT candidate_index_text, result_json, all_teams_gain, "
                    "combined_playoff_delta FROM qualified_result"
                )
            ]
            results.sort(
                key=lambda result: (
                    not result.all_teams_gain,
                    -result.combined_playoff_delta,
                    result.candidate_index,
                )
            )
            return tuple(results)
        for row in connection.execute(
            "SELECT candidate_index_text, result_json, all_teams_gain, "
            "combined_playoff_delta FROM qualified_result"
        ):
            _decode_result(row, run)
        rows = connection.execute(
            "SELECT candidate_index_text, result_json, all_teams_gain, "
            "combined_playoff_delta FROM qualified_result "
            "ORDER BY all_teams_gain DESC, combined_playoff_delta DESC, "
            "LENGTH(candidate_index_text), candidate_index_text"
            + clause,
            parameters,
        )
        return tuple(_decode_result(row, run) for row in rows)
    except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as error:
        raise ThreeWaySearchStoreError(
            f"stored three-way result is invalid: {error}"
        ) from None


def _limit_clause(limit: int | None) -> tuple[str, tuple[int, ...]]:
    if limit is None:
        return "", ()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer or None")
    return " LIMIT ?", (limit,)


def _path(value: object) -> Path:
    try:
        text = os.fspath(value)
    except TypeError:
        raise ValueError("database_path must be a filesystem path") from None
    if not isinstance(text, str) or not text:
        raise ValueError("database_path must be a non-empty filesystem path")
    return Path(text).resolve()


def _stored_index(name: str, value: object) -> int:
    try:
        return _decimal_integer(name, value)
    except ValueError as error:
        raise ThreeWaySearchStoreError(str(error)) from None


def _load_run(connection: sqlite3.Connection, *, row=None):
    if row is None:
        row = connection.execute(_SELECT_RUN_SQL).fetchone()
    if row is None:
        raise ThreeWaySearchStoreError(
            "three-way search store is missing its run definition"
        )
    try:
        stored = ThreeWaySearchRunDefinition.from_record(
            _strict_json_loads(row["definition_json"])
        )
        checkpoint = _decimal_integer(
            "stored next_candidate_index", row["next_candidate_index_text"]
        )
        if checkpoint > stored.total_candidate_count:
            raise ValueError("stored checkpoint exceeds total_candidate_count")
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ThreeWaySearchStoreError(
            f"stored three-way run definition is invalid: {error}"
        ) from None
    if row["run_id"] != stored.run_id:
        raise ThreeWaySearchStoreError(
            "stored three-way run definition does not match its run ID"
        )
    return stored, checkpoint


def _require_result_run(
    result: ThreeWayQualifiedResult, run: ThreeWaySearchRunDefinition
) -> None:
    if tuple(row.team_id for row in result.team_results) != run.participant_team_ids:
        raise ValueError("result teams do not match the three-way search run")


def _decode_result(row, run):
    result = ThreeWayQualifiedResult.from_record(
        _strict_json_loads(row["result_json"])
    )
    if result.candidate_index != _stored_index(
        "stored candidate_index", row["candidate_index_text"]
    ):
        raise ValueError("stored result does not match its candidate index")
    _require_result_run(result, run)
    if result.candidate_index >= run.total_candidate_count:
        raise ValueError("stored candidate index is outside this search run")
    if row["all_teams_gain"] not in (0, 1) or (
        result.all_teams_gain != bool(row["all_teams_gain"])
    ):
        raise ValueError("stored all-team gain marker is inconsistent")
    if result.combined_playoff_delta != row["combined_playoff_delta"]:
        raise ValueError("stored combined playoff delta is inconsistent")
    return result


def _require_existing_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if (
        version != THREE_WAY_DATABASE_SCHEMA_VERSION
        or application_id != THREE_WAY_DATABASE_APPLICATION_ID
    ):
        raise ThreeWaySearchStoreError(
            "three-way search store schema identity is invalid"
        )
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    actual = {row[0]: _normalized_sql(row[1]) for row in rows}
    expected = {
        "search_run": _normalized_sql(_CREATE_RUN_SQL),
        "qualified_result": _normalized_sql(_CREATE_RESULT_SQL),
    }
    if actual != expected:
        raise ThreeWaySearchStoreError(
            "three-way search store table schema is invalid"
        )


def _normalized_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(value.split()).casefold()


_CREATE_RUN_SQL = (
    "CREATE TABLE search_run (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    "run_id TEXT NOT NULL,definition_json TEXT NOT NULL,"
    "next_candidate_index_text TEXT NOT NULL)"
)
_CREATE_RESULT_SQL = (
    "CREATE TABLE qualified_result (candidate_index_text TEXT PRIMARY KEY,"
    "result_json TEXT NOT NULL,all_teams_gain INTEGER NOT NULL "
    "CHECK(all_teams_gain IN (0,1)),combined_playoff_delta REAL NOT NULL)"
)
_SELECT_RUN_SQL = (
    "SELECT run_id,definition_json,next_candidate_index_text "
    "FROM search_run WHERE singleton=1"
)
_UPSERT_RESULT_SQL = (
    "INSERT INTO qualified_result VALUES (?, ?, ?, ?) "
    "ON CONFLICT(candidate_index_text) DO UPDATE SET "
    "result_json=excluded.result_json,all_teams_gain=excluded.all_teams_gain,"
    "combined_playoff_delta=excluded.combined_playoff_delta"
)
