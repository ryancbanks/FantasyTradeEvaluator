"""SQLite persistence for the public league-history records."""

from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3

from ._scenario_random import canonical_json
from ._league_history_evidence import merge_transaction_versions
from .league_history import (
    LEAGUE_HISTORY_APPLICATION_ID,
    LEAGUE_HISTORY_SCHEMA_VERSION,
    HistoryBundleBinding,
    HistoryTeam,
    HistoryTeamRoster,
    HistoryTransaction,
    LeagueHistoryCapture,
    LeagueHistoryConflictError,
    LeagueHistorySnapshot,
    LeagueHistoryStoreError,
    _array,
    _bundle_id,
    _datetime_from_record,
    _record,
    _timestamp,
)


class LeagueHistoryStore:
    """A versioned append-only SQLite store with immutable event identities."""

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.path = _path(database_path)
        try:
            with closing(self._connection()) as connection:
                self._prepare_schema(connection)
                self.journal_mode = self._try_wal(connection)
        except LeagueHistoryStoreError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise LeagueHistoryStoreError(
                f"could not open league history store: {error}"
            ) from None

    def ingest(
        self,
        capture: LeagueHistoryCapture,
        *,
        bundle: HistoryBundleBinding | None = None,
    ) -> str:
        """Atomically append one capture and optional bundle binding."""

        if not isinstance(capture, LeagueHistoryCapture):
            raise ValueError("capture must be a LeagueHistoryCapture")
        if bundle is not None:
            if not isinstance(bundle, HistoryBundleBinding):
                raise ValueError("bundle must be a HistoryBundleBinding or None")
            if (bundle.league_key, bundle.season) != (
                capture.league_key,
                capture.season,
            ):
                raise ValueError("bundle binding does not match history capture")
        try:
            with closing(self._connection()) as connection, connection:
                self._require_schema(connection)
                self._insert_capture(connection, capture)
                if bundle is not None:
                    self._insert_binding(connection, bundle)
            return capture.capture_id
        except LeagueHistoryStoreError:
            raise
        except sqlite3.IntegrityError as error:
            raise LeagueHistoryConflictError(
                f"league history conflicts with immutable stored data: {error}"
            ) from None
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise LeagueHistoryStoreError(
                f"could not append league history: {error}"
            ) from None

    def bind_bundle(
        self,
        league_key: str,
        season: int,
        bundle_id: str,
        captured_at: datetime,
    ) -> HistoryBundleBinding:
        binding = HistoryBundleBinding(league_key, season, bundle_id, captured_at)
        try:
            with closing(self._connection()) as connection, connection:
                self._require_schema(connection)
                self._insert_binding(connection, binding)
            return binding
        except LeagueHistoryStoreError:
            raise
        except sqlite3.IntegrityError as error:
            raise LeagueHistoryConflictError(
                f"bundle conflicts with immutable league history: {error}"
            ) from None
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise LeagueHistoryStoreError(
                f"could not bind bundle to league history: {error}"
            ) from None

    def snapshot_for_bundle(self, bundle_id: str) -> LeagueHistorySnapshot | None:
        bundle_id = _bundle_id(bundle_id)
        try:
            with closing(self._connection()) as connection:
                connection.execute("BEGIN")
                self._require_schema(connection)
                binding_row = connection.execute(
                    _SELECT_BINDING_SQL, (bundle_id,)
                ).fetchone()
                if binding_row is None:
                    return None
                requested = _binding_from_row(binding_row)
                bindings = tuple(
                    _binding_from_row(row)
                    for row in connection.execute(
                        _SELECT_LEAGUE_BINDINGS_SQL,
                        (requested.league_key, requested.season),
                    )
                )
                captures = tuple(
                    self._capture_from_row(connection, row)
                    for row in connection.execute(
                        _SELECT_CAPTURES_SQL,
                        (requested.league_key, requested.season),
                    )
                )
                return LeagueHistorySnapshot(requested, bindings, captures)
        except LeagueHistoryStoreError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LeagueHistoryStoreError(
                f"stored league history is invalid: {error}"
            ) from None

    def revision_for_bundle(self, bundle_id: str) -> str | None:
        snapshot = self.snapshot_for_bundle(bundle_id)
        return None if snapshot is None else snapshot.history_revision

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _try_wal(connection: sqlite3.Connection) -> str:
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            return str(row[0]).casefold() if row else "unknown"
        except sqlite3.DatabaseError:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).casefold() if row else "unknown"

    @classmethod
    def _prepare_schema(cls, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if version > LEAGUE_HISTORY_SCHEMA_VERSION:
            raise LeagueHistoryStoreError(
                "league history store uses a newer database schema"
            )
        if version == 0:
            if application_id not in (0, LEAGUE_HISTORY_APPLICATION_ID):
                raise LeagueHistoryStoreError(
                    "unversioned database belongs to another application"
                )
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing:
                raise LeagueHistoryStoreError(
                    "unversioned database is not an empty league history store"
                )
            with connection:
                for statement in _CREATE_TABLES:
                    connection.execute(statement)
                for statement in _CREATE_INDEXES:
                    connection.execute(statement)
                connection.execute(
                    f"PRAGMA application_id = {LEAGUE_HISTORY_APPLICATION_ID}"
                )
                connection.execute(
                    f"PRAGMA user_version = {LEAGUE_HISTORY_SCHEMA_VERSION}"
                )
        elif version != LEAGUE_HISTORY_SCHEMA_VERSION:
            raise LeagueHistoryStoreError(
                "league history store schema cannot be migrated"
            )
        cls._require_schema(connection)

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if (
            version != LEAGUE_HISTORY_SCHEMA_VERSION
            or application_id != LEAGUE_HISTORY_APPLICATION_ID
        ):
            raise LeagueHistoryStoreError(
                "league history store schema identity is invalid"
            )
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        actual = {
            (row["type"], row["name"]): _normalized_sql(row["sql"])
            for row in rows
            if row["sql"] is not None
        }
        expected = {
            **{("table", _schema_name(sql)): _normalized_sql(sql) for sql in _CREATE_TABLES},
            **{("index", _schema_name(sql)): _normalized_sql(sql) for sql in _CREATE_INDEXES},
        }
        if actual != expected:
            raise LeagueHistoryStoreError(
                "league history store table schema is invalid"
            )

    @staticmethod
    def _insert_capture(
        connection: sqlite3.Connection, capture: LeagueHistoryCapture
    ) -> None:
        existing_time = connection.execute(
            "SELECT capture_id FROM history_capture WHERE "
            "league_key=? AND season=? AND captured_at=?",
            (capture.league_key, capture.season, _timestamp(capture.captured_at)),
        ).fetchone()
        if existing_time is not None and existing_time["capture_id"] != capture.capture_id:
            raise LeagueHistoryConflictError(
                "capture time is already bound to different history content"
            )
        for transaction in capture.transactions:
            LeagueHistoryStore._insert_transaction(
                connection, capture.league_key, capture.season, transaction
            )
        body = canonical_json(
            {
                "teams": [row.to_record() for row in capture.teams],
                "transactions": [
                    row.to_record() for row in capture.transactions
                ],
                "rosters": [row.to_record() for row in capture.rosters],
            }
        )
        values = (
            capture.capture_id,
            capture.league_key,
            capture.season,
            _timestamp(capture.captured_at),
            _timestamp(capture.coverage_start),
            _timestamp(capture.coverage_end),
            int(capture.transaction_history_complete),
            int(capture.roster_complete),
            int(capture.lineup_complete),
            body,
        )
        connection.execute(_INSERT_CAPTURE_SQL, values)
        saved = connection.execute(
            "SELECT * FROM history_capture WHERE capture_id=?", (capture.capture_id,)
        ).fetchone()
        if saved is None or _capture_columns(saved) != values:
            raise LeagueHistoryConflictError(
                "capture_id is already bound to different history content"
            )
        for transaction in capture.transactions:
            link = (
                capture.capture_id,
                capture.league_key,
                capture.season,
                transaction.transaction_id,
            )
            connection.execute(
                _INSERT_CAPTURE_TRANSACTION_SQL,
                link,
            )
            saved_link = connection.execute(
                "SELECT * FROM capture_transaction WHERE "
                "capture_id=? AND transaction_id=?",
                (capture.capture_id, transaction.transaction_id),
            ).fetchone()
            if saved_link is None or tuple(saved_link) != link:
                raise LeagueHistoryConflictError(
                    "capture transaction link conflicts with immutable history"
                )

    @staticmethod
    def _insert_transaction(
        connection: sqlite3.Connection,
        league_key: str,
        season: int,
        transaction: HistoryTransaction,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM transaction_event WHERE "
            "league_key=? AND season=? AND transaction_id=?",
            (league_key, season, transaction.transaction_id),
        ).fetchone()
        if existing is None:
            merged = transaction
            encoded = canonical_json(merged.to_record())
            connection.execute(
                _INSERT_TRANSACTION_SQL,
                (
                    league_key,
                    season,
                    merged.transaction_id,
                    _timestamp(merged.recorded_at),
                    merged.timestamp_basis.value,
                    merged.kind.value,
                    encoded,
                ),
            )
        else:
            stored = _transaction_from_row(existing, league_key, season)
            try:
                merged = merge_transaction_versions(stored, transaction)
            except ValueError as error:
                raise LeagueHistoryConflictError(str(error)) from None
            if merged != stored:
                # Legacy schema-v1 capture bodies joined the mutable best-known
                # event row. Snapshot their old transaction versions before the
                # one permitted canonical-resolution enrichment.
                LeagueHistoryStore._backfill_capture_transaction_versions(
                    connection,
                    league_key,
                    season,
                    transaction.transaction_id,
                )
                encoded = canonical_json(merged.to_record())
                connection.execute(
                    "UPDATE transaction_event SET recorded_at=?,timestamp_basis=?,"
                    "kind=?,transaction_json=? WHERE league_key=? AND season=? "
                    "AND transaction_id=?",
                    (
                        _timestamp(merged.recorded_at),
                        merged.timestamp_basis.value,
                        merged.kind.value,
                        encoded,
                        league_key,
                        season,
                        merged.transaction_id,
                    ),
                )
            else:
                encoded = canonical_json(merged.to_record())
        saved = connection.execute(
            "SELECT * FROM transaction_event WHERE "
            "league_key=? AND season=? AND transaction_id=?",
            (league_key, season, transaction.transaction_id),
        ).fetchone()
        expected = (
            league_key,
            season,
            merged.transaction_id,
            _timestamp(merged.recorded_at),
            merged.timestamp_basis.value,
            merged.kind.value,
            encoded,
        )
        if saved is None or _transaction_columns(saved) != expected:
            raise LeagueHistoryConflictError(
                "transaction_id is already bound to different executed content"
            )

    @staticmethod
    def _backfill_capture_transaction_versions(
        connection: sqlite3.Connection,
        league_key: str,
        season: int,
        transaction_id: str,
    ) -> None:
        rows = connection.execute(
            _SELECT_CAPTURE_ROWS_FOR_TRANSACTION_SQL,
            (league_key, season, transaction_id),
        ).fetchall()
        for row in rows:
            body = _strict_json_loads(row["capture_json"])
            if not isinstance(body, dict):
                raise ValueError("stored capture body must be an object")
            if set(body) == {"teams", "rosters", "transactions"}:
                continue
            body = _record(body, {"teams", "rosters"}, "stored capture body")
            # Validate the legacy row against its content ID before enriching
            # only the storage representation.
            LeagueHistoryStore._capture_from_row(connection, row)
            transactions = tuple(
                _transaction_from_row(item, row["league_key"], row["season"])
                for item in connection.execute(
                    _SELECT_CAPTURE_TRANSACTIONS_SQL, (row["capture_id"],)
                )
            )
            encoded = canonical_json(
                {
                    "teams": body["teams"],
                    "transactions": [item.to_record() for item in transactions],
                    "rosters": body["rosters"],
                }
            )
            connection.execute(
                "UPDATE history_capture SET capture_json=? WHERE capture_id=?",
                (encoded, row["capture_id"]),
            )

    @staticmethod
    def _insert_binding(
        connection: sqlite3.Connection, binding: HistoryBundleBinding
    ) -> None:
        encoded = canonical_json(binding.to_record())
        connection.execute(
            _INSERT_BINDING_SQL,
            (
                binding.bundle_id,
                binding.league_key,
                binding.season,
                _timestamp(binding.captured_at),
                encoded,
            ),
        )
        saved = connection.execute(
            "SELECT * FROM bundle_binding WHERE bundle_id=?",
            (binding.bundle_id,),
        ).fetchone()
        expected = (
            binding.bundle_id,
            binding.league_key,
            binding.season,
            _timestamp(binding.captured_at),
            encoded,
        )
        if saved is None or _binding_columns(saved) != expected:
            raise LeagueHistoryConflictError(
                "bundle_id is already bound to different league history"
            )

    @staticmethod
    def _capture_from_row(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> LeagueHistoryCapture:
        body = _strict_json_loads(row["capture_json"])
        if not isinstance(body, dict):
            raise ValueError("stored capture body must be an object")
        global_transactions = tuple(
            _transaction_from_row(item, row["league_key"], row["season"])
            for item in connection.execute(
                _SELECT_CAPTURE_TRANSACTIONS_SQL, (row["capture_id"],)
            )
        )
        if set(body) == {"teams", "rosters"}:
            transactions = global_transactions
        else:
            body = _record(
                body,
                {"teams", "rosters", "transactions"},
                "stored capture body",
            )
            transactions = tuple(
                HistoryTransaction.from_record(item)
                for item in _array(
                    "history transactions", body["transactions"]
                )
            )
            current_by_id = {
                item.transaction_id: item for item in global_transactions
            }
            if set(current_by_id) != {
                item.transaction_id for item in transactions
            }:
                raise ValueError(
                    "stored capture transaction versions conflict with its links"
                )
            for transaction in transactions:
                merge_transaction_versions(
                    transaction, current_by_id[transaction.transaction_id]
                )
        capture = LeagueHistoryCapture(
            league_key=row["league_key"],
            season=row["season"],
            captured_at=_datetime_from_record("captured_at", row["captured_at"]),
            coverage_start=_datetime_from_record(
                "coverage_start", row["coverage_start"]
            ),
            coverage_end=_datetime_from_record("coverage_end", row["coverage_end"]),
            transaction_history_complete=_stored_boolean(
                "transaction_history_complete", row["transactions_complete"]
            ),
            roster_complete=_stored_boolean(
                "roster_complete", row["roster_complete"]
            ),
            lineup_complete=_stored_boolean(
                "lineup_complete", row["lineup_complete"]
            ),
            teams=tuple(
                HistoryTeam.from_record(item)
                for item in _array("history teams", body["teams"])
            ),
            transactions=transactions,
            rosters=tuple(
                HistoryTeamRoster.from_record(item)
                for item in _array("history rosters", body["rosters"])
            ),
        )
        if capture.capture_id != row["capture_id"]:
            raise ValueError("stored capture does not match capture_id")
        return capture


def _binding_from_row(row: sqlite3.Row) -> HistoryBundleBinding:
    binding = HistoryBundleBinding.from_record(_strict_json_loads(row["binding_json"]))
    expected = (
        binding.bundle_id,
        binding.league_key,
        binding.season,
        _timestamp(binding.captured_at),
        row["binding_json"],
    )
    if _binding_columns(row) != expected:
        raise ValueError("stored bundle binding columns conflict with its record")
    return binding


def _transaction_from_row(
    row: sqlite3.Row, league_key: str, season: int
) -> HistoryTransaction:
    transaction = HistoryTransaction.from_record(
        _strict_json_loads(row["transaction_json"])
    )
    expected = (
        league_key,
        season,
        transaction.transaction_id,
        _timestamp(transaction.recorded_at),
        transaction.timestamp_basis.value,
        transaction.kind.value,
        row["transaction_json"],
    )
    if _transaction_columns(row) != expected:
        raise ValueError("stored transaction columns conflict with its record")
    return transaction


def _capture_columns(row: sqlite3.Row) -> tuple[object, ...]:
    return tuple(
        row[name]
        for name in (
            "capture_id",
            "league_key",
            "season",
            "captured_at",
            "coverage_start",
            "coverage_end",
            "transactions_complete",
            "roster_complete",
            "lineup_complete",
            "capture_json",
        )
    )


def _transaction_columns(row: sqlite3.Row) -> tuple[object, ...]:
    return tuple(
        row[name]
        for name in (
            "league_key",
            "season",
            "transaction_id",
            "recorded_at",
            "timestamp_basis",
            "kind",
            "transaction_json",
        )
    )


def _binding_columns(row: sqlite3.Row) -> tuple[object, ...]:
    return tuple(
        row[name]
        for name in (
            "bundle_id",
            "league_key",
            "season",
            "captured_at",
            "binding_json",
        )
    )


def _strict_json_loads(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("stored JSON must be text")

    def reject_constant(constant: str) -> None:
        raise ValueError(f"stored JSON contains non-finite constant {constant}")

    def unique_object(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError(f"stored JSON contains duplicate key {key!r}")
            result[key] = child
        return result

    return json.loads(
        value, parse_constant=reject_constant, object_pairs_hook=unique_object
    )


def _stored_boolean(name: str, value: object) -> bool:
    if value not in (0, 1):
        raise ValueError(f"stored {name} must be zero or one")
    return bool(value)


def _path(value: object) -> Path:
    try:
        path = os.fspath(value)
    except TypeError:
        raise ValueError("database_path must be a filesystem path") from None
    if not isinstance(path, str) or not path:
        raise ValueError("database_path must be a non-empty filesystem path")
    return Path(path).resolve()


def _schema_name(statement: str) -> str:
    match = re.match(r"CREATE (?:TABLE|INDEX) ([a-z_]+)", statement)
    if match is None:
        raise AssertionError("schema statement does not have a canonical name")
    return match.group(1)


def _normalized_sql(value: object) -> str:
    return "" if not isinstance(value, str) else "".join(value.split()).casefold()


_CREATE_TABLES = (
    "CREATE TABLE history_capture ("
    "capture_id TEXT PRIMARY KEY,league_key TEXT NOT NULL,"
    "season INTEGER NOT NULL CHECK(season>0),captured_at TEXT NOT NULL,"
    "coverage_start TEXT NOT NULL,coverage_end TEXT NOT NULL,"
    "transactions_complete INTEGER NOT NULL CHECK(transactions_complete IN (0,1)),"
    "roster_complete INTEGER NOT NULL CHECK(roster_complete IN (0,1)),"
    "lineup_complete INTEGER NOT NULL CHECK(lineup_complete IN (0,1)),"
    "capture_json TEXT NOT NULL,UNIQUE(league_key,season,captured_at),"
    "UNIQUE(capture_id,league_key,season))",
    "CREATE TABLE transaction_event ("
    "league_key TEXT NOT NULL,season INTEGER NOT NULL CHECK(season>0),"
    "transaction_id TEXT NOT NULL,recorded_at TEXT NOT NULL,"
    "timestamp_basis TEXT NOT NULL,kind TEXT NOT NULL,transaction_json TEXT NOT NULL,"
    "PRIMARY KEY(league_key,season,transaction_id))",
    "CREATE TABLE capture_transaction ("
    "capture_id TEXT NOT NULL,league_key TEXT NOT NULL,season INTEGER NOT NULL,"
    "transaction_id TEXT NOT NULL,PRIMARY KEY(capture_id,transaction_id),"
    "FOREIGN KEY(capture_id,league_key,season) REFERENCES "
    "history_capture(capture_id,league_key,season),"
    "FOREIGN KEY(league_key,season,transaction_id) REFERENCES "
    "transaction_event(league_key,season,transaction_id))",
    "CREATE TABLE bundle_binding ("
    "bundle_id TEXT PRIMARY KEY,league_key TEXT NOT NULL,"
    "season INTEGER NOT NULL CHECK(season>0),captured_at TEXT NOT NULL,"
    "binding_json TEXT NOT NULL)",
)
_CREATE_INDEXES = (
    "CREATE INDEX history_capture_league_season_time ON "
    "history_capture(league_key,season,captured_at)",
    "CREATE INDEX bundle_binding_league_season_time ON "
    "bundle_binding(league_key,season,captured_at)",
)
_INSERT_CAPTURE_SQL = (
    "INSERT OR IGNORE INTO history_capture VALUES (?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_TRANSACTION_SQL = (
    "INSERT OR IGNORE INTO transaction_event VALUES (?,?,?,?,?,?,?)"
)
_INSERT_CAPTURE_TRANSACTION_SQL = (
    "INSERT OR IGNORE INTO capture_transaction VALUES (?,?,?,?)"
)
_INSERT_BINDING_SQL = "INSERT OR IGNORE INTO bundle_binding VALUES (?,?,?,?,?)"
_SELECT_BINDING_SQL = "SELECT * FROM bundle_binding WHERE bundle_id=?"
_SELECT_LEAGUE_BINDINGS_SQL = (
    "SELECT * FROM bundle_binding WHERE league_key=? AND season=? "
    "ORDER BY captured_at,bundle_id"
)
_SELECT_CAPTURES_SQL = (
    "SELECT * FROM history_capture WHERE league_key=? AND season=? "
    "ORDER BY captured_at,capture_id"
)
_SELECT_CAPTURE_TRANSACTIONS_SQL = (
    "SELECT event.* FROM capture_transaction AS link "
    "JOIN transaction_event AS event ON event.league_key=link.league_key "
    "AND event.season=link.season AND event.transaction_id=link.transaction_id "
    "WHERE link.capture_id=? ORDER BY event.recorded_at,event.transaction_id"
)
_SELECT_CAPTURE_ROWS_FOR_TRANSACTION_SQL = (
    "SELECT capture.* FROM history_capture AS capture "
    "JOIN capture_transaction AS link ON link.capture_id=capture.capture_id "
    "WHERE link.league_key=? AND link.season=? AND link.transaction_id=? "
    "ORDER BY capture.captured_at,capture.capture_id"
)
