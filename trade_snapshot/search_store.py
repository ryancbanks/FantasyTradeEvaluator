"""Resumable SQLite checkpoints and qualified trade results."""

from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
import os
import json
from itertools import islice
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
from ._writer_lock import ExclusiveWriterLock, WriterOwnershipError


DATABASE_SCHEMA_VERSION = 2
MAX_QUALIFIED_RESULT_BATCH_SIZE = 1_000


@dataclass(frozen=True, slots=True)
class SearchResumeSummary:
    """Bounded-memory progress recovered from one persisted pair search."""

    next_candidate_index: int
    qualified_result_count: int
    playoff_evaluated_count: int
    mutual_playoff_gain_count: int

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
        self._writer_lock: ExclusiveWriterLock | None = None
        self.journal_mode = "unknown"
        try:
            writer_lock = ExclusiveWriterLock(self.path)
            writer_lock.acquire()
            self._writer_lock = writer_lock
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
        except WriterOwnershipError as error:
            self.close()
            raise SearchStoreError(str(error)) from None
        except (sqlite3.Error, OSError, ValueError) as error:
            self.close()
            raise SearchStoreError(f"could not open search store: {error}") from None

    def __enter__(self) -> "SearchStore":
        self._require_open()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        finally:
            if self._writer_lock is not None:
                self._writer_lock.close()
                self._writer_lock = None

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
        if next_candidate_index is not None:
            self.upsert_qualified_results(
                (result,), next_candidate_index=next_candidate_index
            )
            return
        values = self._result_values(result)
        connection = self._require_open()
        with connection:
            connection.execute(_UPSERT_RESULT_SQL, values)

    def upsert_qualified_results(
        self,
        results: Iterable[QualifiedSearchResult],
        *,
        next_candidate_index: int,
    ) -> None:
        """Atomically persist one bounded result batch and its checkpoint."""

        rows = _bounded_result_batch(results)
        target = self._checked_checkpoint(next_candidate_index)
        seen_indexes = set()
        values = []
        for result in rows:
            if isinstance(result, QualifiedSearchResult):
                if result.candidate_index in seen_indexes:
                    raise ValueError("results contain a duplicate candidate_index")
                seen_indexes.add(result.candidate_index)
            values.append(self._result_values(result, target=target))

        connection = self._require_open()
        with connection:
            connection.executemany(_UPSERT_RESULT_SQL, values)
            self._advance_checkpoint(connection, target)

    def _result_values(
        self,
        result: QualifiedSearchResult,
        *,
        target: int | None = None,
    ) -> tuple[object, ...]:
        if not isinstance(result, QualifiedSearchResult):
            raise ValueError("result must be a QualifiedSearchResult")
        if result.candidate_index >= self.run.total_candidate_count:
            raise ValueError("candidate_index is outside this search run")
        if target is not None and target <= result.candidate_index:
            raise ValueError("next_candidate_index must be after the saved candidate")
        return (
            result.candidate_index,
            _canonical_json(list(result.outgoing_player_ids)),
            _canonical_json(list(result.incoming_player_ids)),
            *(
                _canonical_json(list(getattr(result, name)))
                for name in _ADJUSTMENT_FIELDS
            ),
            *(getattr(result, name) for name in _POWER_FIELDS + _ODDS_FIELDS),
        )

    def resume(self) -> SearchResumeState:
        connection = self._require_open()
        next_index = self._stored_checkpoint(connection)
        try:
            rows = connection.execute("SELECT * FROM qualified_result ORDER BY candidate_index")
            results = tuple(self._result_from_row(saved) for saved in rows)
            if results and results[-1].candidate_index >= self.run.total_candidate_count:
                raise ValueError("stored candidate_index is outside this search run")
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as error:
            raise SearchStoreError(f"stored qualified result is invalid: {error}") from None
        return SearchResumeState(next_index, results)

    def resume_summary(self) -> SearchResumeSummary:
        """Validate persisted rows while retaining only aggregate progress."""

        connection = self._require_open()
        next_index = self._stored_checkpoint(connection)
        qualified_count = playoff_count = mutual_count = 0
        try:
            rows = connection.execute(
                "SELECT * FROM qualified_result ORDER BY candidate_index"
            )
            for saved in rows:
                result = self._result_from_row(saved)
                if result.candidate_index >= self.run.total_candidate_count:
                    raise ValueError("stored candidate_index is outside this search run")
                if result.candidate_index >= next_index:
                    raise ValueError("stored result is at or beyond the search checkpoint")
                qualified_count += 1
                playoff_count += int(result.primary_playoff_before is not None)
                mutual_count += int(_is_mutual_gain(result))
        except (ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as error:
            raise SearchStoreError(
                f"stored qualified result is invalid: {error}"
            ) from None
        return SearchResumeSummary(
            next_index,
            qualified_count,
            playoff_count,
            mutual_count,
        )

    def persisted_summary(self) -> SearchResumeSummary:
        """Read trusted aggregate progress after this process commits a batch."""

        connection = self._require_open()
        next_index = self._stored_checkpoint(connection)
        try:
            row = connection.execute(
                "SELECT COUNT(*), COUNT(primary_playoff_before), "
                "COALESCE(SUM(CASE WHEN primary_playoff_after > "
                "primary_playoff_before AND counterparty_playoff_after > "
                "counterparty_playoff_before THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN candidate_index >= ? THEN 1 ELSE 0 END), 0) "
                "FROM qualified_result",
                (next_index,),
            ).fetchone()
            summary = SearchResumeSummary(
                next_index,
                _sqlite_integer("stored qualified result count", row[0]),
                _sqlite_integer("stored playoff evaluation count", row[1]),
                _sqlite_integer("stored mutual gain count", row[2]),
            )
            if _sqlite_integer("stored uncheckpointed result count", row[3]):
                raise ValueError("stored result is at or beyond the search checkpoint")
            return summary
        except (ValueError, TypeError, sqlite3.Error) as error:
            raise SearchStoreError(
                f"stored search progress is invalid: {error}"
            ) from None

    def _stored_checkpoint(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(_SELECT_CHECKPOINT_SQL).fetchone()
        if row is None:
            raise SearchStoreError("search store is missing its run definition")
        next_index = _sqlite_integer("stored next_candidate_index", row[0])
        if next_index > self.run.total_candidate_count:
            raise SearchStoreError("stored checkpoint exceeds total_candidate_count")
        return next_index

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


def _bounded_result_batch(
    values: Iterable[QualifiedSearchResult],
) -> tuple[QualifiedSearchResult, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("results must be an iterable of qualified results")
    try:
        iterator = iter(values)
    except TypeError:
        raise ValueError("results must be an iterable of qualified results") from None
    rows = tuple(islice(iterator, MAX_QUALIFIED_RESULT_BATCH_SIZE + 1))
    if len(rows) > MAX_QUALIFIED_RESULT_BATCH_SIZE:
        raise ValueError(
            "results must contain at most "
            f"{MAX_QUALIFIED_RESULT_BATCH_SIZE} values"
        )
    return rows


def read_search_results(
    database_path: str | os.PathLike[str],
    limit: int | None = None,
    *,
    expected_run_id: str | None = None,
    expected_result_count: int | None = None,
    maximum_candidate_index: int | None = None,
    best_first: bool = False,
    mutual_only: bool = False,
) -> tuple[QualifiedSearchResult, ...]:
    """Read validated pair results after the writer connection has closed."""

    if limit is not None:
        return tuple(
            iter_search_results(
                database_path,
                limit,
                expected_run_id=expected_run_id,
                expected_result_count=expected_result_count,
                maximum_candidate_index=maximum_candidate_index,
                best_first=best_first,
                mutual_only=mutual_only,
            )
        )
    expected_count = _optional_count(expected_result_count)
    maximum_index = _optional_count(maximum_candidate_index)
    path = _existing_path(database_path)
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            run, checkpoint = _load_existing_run(connection, expected_run_id)
            _validate_watermark(maximum_index, checkpoint, run.total_candidate_count)
            results = _validated_results(
                connection,
                run,
                expected_count,
                maximum_index,
            )
            if mutual_only:
                results = tuple(row for row in results if _is_mutual_gain(row))
            if best_first:
                results = tuple(sorted(results, key=search_result_rank_key))
            return results
    except SearchStoreError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SearchStoreError(f"could not read search results: {error}") from None


def iter_search_results(
    database_path: str | os.PathLike[str],
    limit: int | None = None,
    *,
    expected_run_id: str | None = None,
    expected_result_count: int | None = None,
    maximum_candidate_index: int | None = None,
    best_first: bool = False,
    mutual_only: bool = False,
) -> Iterator[QualifiedSearchResult]:
    """Stream one validated result set in deterministic order."""

    clause, limit_parameters = _limit_clause(limit)
    expected_count = _optional_count(expected_result_count)
    maximum_index = _optional_count(maximum_candidate_index)
    path = _existing_path(database_path)

    def generate() -> Iterator[QualifiedSearchResult]:
        try:
            with closing(sqlite3.connect(path)) as connection:
                connection.row_factory = sqlite3.Row
                run, checkpoint = _load_existing_run(connection, expected_run_id)
                _validate_watermark(
                    maximum_index, checkpoint, run.total_candidate_count
                )
                _validate_result_aggregate(
                    connection,
                    run,
                    expected_count,
                    maximum_index,
                )
                query, parameters = _result_query(
                    best_first=best_first,
                    mutual_only=mutual_only,
                    maximum_candidate_index=maximum_index,
                )
                for row in connection.execute(
                    query + clause, (*parameters, *limit_parameters)
                ):
                    result = SearchStore._result_from_row(row)
                    _validate_result(result, run)
                    yield result
        except SearchStoreError:
            raise
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise SearchStoreError(f"could not read search results: {error}") from None

    return generate()


def _validated_results(connection, run, expected_count, maximum_index):
    results = []
    try:
        where = " WHERE candidate_index < ?" if maximum_index is not None else ""
        parameters = (maximum_index,) if maximum_index is not None else ()
        for row in connection.execute(
            "SELECT * FROM qualified_result" + where + " ORDER BY candidate_index",
            parameters,
        ):
            result = SearchStore._result_from_row(row)
            _validate_result(result, run)
            results.append(result)
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SearchStoreError(f"stored qualified result is invalid: {error}") from None
    if expected_count is not None and len(results) != expected_count:
        raise SearchStoreError(
            "stored qualified result count does not match search progress"
        )
    return tuple(results)


def _validate_result_aggregate(connection, run, expected_count, maximum_index) -> None:
    try:
        if maximum_index is None:
            selected = "1"
            parameters = (run.total_candidate_count,)
        else:
            selected = "candidate_index < ?"
            parameters = (
                maximum_index,
                maximum_index,
                run.total_candidate_count,
            )
        row = connection.execute(
            "SELECT "
            f"COALESCE(SUM(CASE WHEN {selected} THEN 1 ELSE 0 END), 0), "
            f"COUNT(DISTINCT CASE WHEN {selected} THEN candidate_index END), "
            "COALESCE(SUM(CASE WHEN typeof(candidate_index) != 'integer' OR "
            "candidate_index < 0 OR candidate_index >= ? THEN 1 ELSE 0 END), 0) "
            "FROM qualified_result",
            parameters,
        ).fetchone()
        count = _sqlite_integer("stored qualified result count", row[0])
        distinct_count = _sqlite_integer(
            "stored distinct candidate index count", row[1]
        )
        invalid_count = _sqlite_integer("stored invalid candidate index count", row[2])
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise SearchStoreError(f"stored qualified result is invalid: {error}") from None
    if invalid_count or distinct_count != count:
        raise SearchStoreError("stored qualified result candidate indexes are invalid")
    if expected_count is not None and count != expected_count:
        raise SearchStoreError(
            "stored qualified result count does not match search progress"
        )


def _validate_result(result, run) -> None:
    if result.candidate_index >= run.total_candidate_count:
        raise ValueError("stored candidate_index is outside this search run")


def _load_existing_run(connection, expected_run_id):
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != DATABASE_SCHEMA_VERSION:
        raise SearchStoreError("search result store has an unsupported database schema")
    row = connection.execute(_SELECT_RUN_SQL).fetchone()
    if row is None:
        raise SearchStoreError("search result store is missing its run definition")
    try:
        run = SearchRunDefinition.from_record(_strict_json_loads(row["definition_json"]))
        checkpoint = _sqlite_integer(
            "stored next_candidate_index", row["next_candidate_index"]
        )
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise SearchStoreError(f"stored run definition is invalid: {error}") from None
    if row["run_id"] != run.run_id:
        raise SearchStoreError("stored run definition does not match its run ID")
    if expected_run_id is not None and run.run_id != expected_run_id:
        raise SearchRunMismatchError("search result store belongs to a different run")
    if checkpoint > run.total_candidate_count:
        raise SearchStoreError("stored checkpoint exceeds total_candidate_count")
    return run, checkpoint


def _validate_watermark(maximum_index, checkpoint, total_candidate_count) -> None:
    if maximum_index is not None and (
        maximum_index > checkpoint or maximum_index > total_candidate_count
    ):
        raise SearchStoreError("search result snapshot exceeds its stored checkpoint")


def _result_query(
    *,
    best_first: bool,
    mutual_only: bool,
    maximum_candidate_index: int | None,
) -> tuple[str, tuple[int, ...]]:
    conditions = []
    parameters = ()
    if maximum_candidate_index is not None:
        conditions.append("candidate_index < ?")
        parameters = (maximum_candidate_index,)
    if mutual_only:
        conditions.append(_MUTUAL_GAIN_SQL)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    if best_first:
        order = (
            f" ORDER BY {_MUTUAL_GAIN_SQL} DESC, {_COMBINED_PLAYOFF_DELTA_SQL} "
            "DESC, candidate_index"
        )
    else:
        order = " ORDER BY candidate_index"
    return "SELECT * FROM qualified_result" + where + order, parameters


def _limit_clause(limit: int | None) -> tuple[str, tuple[int, ...]]:
    if limit is None:
        return "", ()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer or None")
    return " LIMIT ?", (limit,)


def _optional_count(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_result_count must be a non-negative integer or None")
    return value


def _existing_path(value: object) -> Path:
    try:
        text = os.fspath(value)
    except TypeError:
        raise ValueError("database_path must be a filesystem path") from None
    if not isinstance(text, str) or not text:
        raise ValueError("database_path must be a non-empty filesystem path")
    path = Path(text).resolve()
    if not path.is_file():
        raise SearchStoreError("search result store does not exist")
    return path


def _is_mutual_gain(result: QualifiedSearchResult) -> bool:
    return (
        result.primary_playoff_before is not None
        and result.primary_playoff_after > result.primary_playoff_before
        and result.counterparty_playoff_after > result.counterparty_playoff_before
    )


def search_result_rank_key(
    result: QualifiedSearchResult,
) -> tuple[bool, float, int]:
    """Canonical best-first key shared by in-memory and stored outcomes."""

    if not isinstance(result, QualifiedSearchResult):
        raise ValueError("result must be a QualifiedSearchResult")
    if result.primary_playoff_before is None:
        combined_delta = float("-inf")
    else:
        combined_delta = (
            (
                result.primary_playoff_after / 100
                - result.primary_playoff_before / 100
            )
            + (
                result.counterparty_playoff_after / 100
                - result.counterparty_playoff_before / 100
            )
        )
    return not _is_mutual_gain(result), -combined_delta, result.candidate_index


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
_MUTUAL_GAIN_SQL = (
    "(primary_playoff_after > primary_playoff_before AND "
    "counterparty_playoff_after > counterparty_playoff_before)"
)
_COMBINED_PLAYOFF_DELTA_SQL = (
    "((primary_playoff_after / 100.0 - primary_playoff_before / 100.0) + "
    "(counterparty_playoff_after / 100.0 - "
    "counterparty_playoff_before / 100.0))"
)
