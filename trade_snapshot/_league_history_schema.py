"""SQLite schema identity and the single supported league-history migration."""

from __future__ import annotations

import json
import re
import sqlite3

from ._scenario_random import canonical_json, content_id


LEAGUE_HISTORY_SCHEMA_VERSION = 2
LEAGUE_HISTORY_APPLICATION_ID = 1_179_927_880  # ASCII "FTEH"


CURRENT_TABLES = (
    "CREATE TABLE history_capture ("
    "capture_id TEXT PRIMARY KEY,league_key TEXT NOT NULL,"
    "season INTEGER NOT NULL CHECK(season>0),captured_at TEXT NOT NULL,"
    "coverage_start TEXT NOT NULL,coverage_end TEXT NOT NULL,"
    "transactions_complete INTEGER NOT NULL CHECK(transactions_complete IN (0,1)),"
    "roster_complete INTEGER NOT NULL CHECK(roster_complete IN (0,1)),"
    "lineup_complete INTEGER NOT NULL CHECK(lineup_complete IN (0,1)),"
    "host_snapshot_id TEXT,identity_schema_version INTEGER NOT NULL "
    "CHECK(identity_schema_version IN (1,2)),roster_ownership_id TEXT NOT NULL,"
    "acquisition_json TEXT NOT NULL,capture_json TEXT NOT NULL,"
    "UNIQUE(league_key,season,captured_at),"
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
    "host_snapshot_id TEXT,host_captured_at TEXT,history_capture_id TEXT,"
    "roster_ownership_id TEXT,binding_json TEXT NOT NULL,"
    "FOREIGN KEY(history_capture_id) REFERENCES history_capture(capture_id))",
)
CURRENT_INDEXES = (
    "CREATE INDEX history_capture_league_season_time ON "
    "history_capture(league_key,season,captured_at)",
    "CREATE INDEX bundle_binding_league_season_time ON "
    "bundle_binding(league_key,season,captured_at)",
)

V1_TABLES = (
    "CREATE TABLE history_capture ("
    "capture_id TEXT PRIMARY KEY,league_key TEXT NOT NULL,"
    "season INTEGER NOT NULL CHECK(season>0),captured_at TEXT NOT NULL,"
    "coverage_start TEXT NOT NULL,coverage_end TEXT NOT NULL,"
    "transactions_complete INTEGER NOT NULL CHECK(transactions_complete IN (0,1)),"
    "roster_complete INTEGER NOT NULL CHECK(roster_complete IN (0,1)),"
    "lineup_complete INTEGER NOT NULL CHECK(lineup_complete IN (0,1)),"
    "capture_json TEXT NOT NULL,UNIQUE(league_key,season,captured_at),"
    "UNIQUE(capture_id,league_key,season))",
    CURRENT_TABLES[1],
    CURRENT_TABLES[2],
    "CREATE TABLE bundle_binding ("
    "bundle_id TEXT PRIMARY KEY,league_key TEXT NOT NULL,"
    "season INTEGER NOT NULL CHECK(season>0),captured_at TEXT NOT NULL,"
    "binding_json TEXT NOT NULL)",
)
V1_INDEXES = CURRENT_INDEXES


def prepare_schema(connection: sqlite3.Connection, error_type: type[Exception]) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if version > LEAGUE_HISTORY_SCHEMA_VERSION:
        raise error_type("league history store uses a newer database schema")
    if version == 0:
        if application_id not in (0, LEAGUE_HISTORY_APPLICATION_ID):
            raise error_type("unversioned database belongs to another application")
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if existing:
            raise error_type("unversioned database is not an empty league history store")
        with connection:
            _create_current(connection)
            connection.execute(f"PRAGMA application_id = {LEAGUE_HISTORY_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {LEAGUE_HISTORY_SCHEMA_VERSION}")
    elif version == 1:
        if application_id != LEAGUE_HISTORY_APPLICATION_ID:
            raise error_type("league history store schema identity is invalid")
        _require_objects(connection, V1_TABLES, V1_INDEXES, error_type)
        _migrate_v1(connection)
    elif version != LEAGUE_HISTORY_SCHEMA_VERSION:
        raise error_type("league history store schema cannot be migrated")
    require_schema(connection, error_type)


def require_schema(connection: sqlite3.Connection, error_type: type[Exception]) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    if (
        version != LEAGUE_HISTORY_SCHEMA_VERSION
        or application_id != LEAGUE_HISTORY_APPLICATION_ID
    ):
        raise error_type("league history store schema identity is invalid")
    _require_objects(connection, CURRENT_TABLES, CURRENT_INDEXES, error_type)


def _migrate_v1(connection: sqlite3.Connection) -> None:
    captures = [dict(row) for row in connection.execute("SELECT * FROM history_capture")]
    transactions = [
        tuple(row) for row in connection.execute("SELECT * FROM transaction_event")
    ]
    links = [tuple(row) for row in connection.execute("SELECT * FROM capture_transaction")]
    bindings = [dict(row) for row in connection.execute("SELECT * FROM bundle_binding")]
    migrated_captures = _migrated_capture_rows(captures, transactions, links)
    migrated_bindings = _migrated_binding_rows(bindings)
    _replace_v1_schema(
        connection,
        migrated_captures,
        transactions,
        links,
        migrated_bindings,
    )


def _migrated_capture_rows(captures, transactions, links):
    event_by_id = {
        (row[0], row[1], row[2]): _strict_json_loads(row[6])
        for row in transactions
    }
    _validate_v1_events(transactions, event_by_id)
    links_by_capture: dict[str, list[tuple[object, ...]]] = {}
    capture_identity = {
        row["capture_id"]: (row["league_key"], row["season"]) for row in captures
    }
    for link in links:
        if (
            capture_identity.get(link[0]) != (link[1], link[2])
            or (link[1], link[2], link[3]) not in event_by_id
        ):
            raise ValueError("legacy capture transaction link is invalid")
        links_by_capture.setdefault(str(link[0]), []).append(link)

    result = []
    for row in captures:
        body = _strict_json_loads(row["capture_json"])
        if not isinstance(body, dict) or set(body) not in (
            {"teams", "rosters"},
            {"teams", "rosters", "transactions"},
        ):
            raise ValueError("legacy history capture body is invalid")
        linked = links_by_capture.get(row["capture_id"], [])
        linked_ids = {link[3] for link in linked}
        transaction_records = body.get("transactions")
        if transaction_records is None:
            transaction_records = [
                event_by_id[(link[1], link[2], link[3])] for link in linked
            ]
        elif (
            not isinstance(transaction_records, list)
            or {
                item.get("transaction_id")
                for item in transaction_records
                if isinstance(item, dict)
            }
            != linked_ids
            or any(not isinstance(item, dict) for item in transaction_records)
        ):
            raise ValueError("legacy capture transaction versions are invalid")
        transaction_records = list(transaction_records)
        transaction_records.sort(
            key=lambda item: (item["recorded_at"], item["transaction_id"])
        )
        _validate_v1_capture(row, body, transaction_records)
        event_times = [item["recorded_at"] for item in transaction_records]
        roster_id = _roster_ownership_id(body["rosters"])
        acquisition = {
            "attempted_at": row["captured_at"],
            "completeness_policy": "legacy_v1_unknown",
            "earliest_source_event_at": min(event_times, default=None),
            "latest_source_event_at": max(event_times, default=None),
            "normalized_transaction_count": len(transaction_records),
            "outcome": "legacy_unknown",
            "provider": "legacy",
            "returned_transaction_count": None,
            "skipped": [],
            "transaction_limit": None,
        }
        migrated_body = canonical_json(
            {
                "acquisition_evidence": acquisition,
                "host_snapshot_id": None,
                "identity_schema_version": 1,
                "roster_ownership_id": roster_id,
                "rosters": body["rosters"],
                "teams": body["teams"],
                "transactions": transaction_records,
            }
        )
        result.append(
            (
                row["capture_id"],
                row["league_key"],
                row["season"],
                row["captured_at"],
                row["coverage_start"],
                row["coverage_end"],
                row["transactions_complete"],
                row["roster_complete"],
                row["lineup_complete"],
                None,
                1,
                roster_id,
                canonical_json(acquisition),
                migrated_body,
            )
        )

    return result


def _migrated_binding_rows(bindings):
    result = []
    for row in bindings:
        body = _strict_json_loads(row["binding_json"])
        if not isinstance(body, dict) or set(body) != {
            "bundle_id", "captured_at", "league_key", "season"
        }:
            raise ValueError("legacy history binding body is invalid")
        _validate_v1_binding(row, body)
        body.update(
            {
                "history_capture_id": None,
                "host_captured_at": None,
                "host_snapshot_id": None,
                "roster_ownership_id": None,
            }
        )
        result.append(
            (
                row["bundle_id"],
                row["league_key"],
                row["season"],
                row["captured_at"],
                None,
                None,
                None,
                None,
                canonical_json(body),
            )
        )

    return result


def _validate_v1_events(rows, records):
    from .league_history import HistoryTransaction, _timestamp

    for row in rows:
        transaction = HistoryTransaction.from_record(records[(row[0], row[1], row[2])])
        if (
            row[2] != transaction.transaction_id
            or row[3] != _timestamp(transaction.recorded_at)
            or row[4] != transaction.timestamp_basis.value
            or row[5] != transaction.kind.value
        ):
            raise ValueError("legacy transaction columns conflict with its record")


def _validate_v1_capture(row, body, transactions):
    from .league_history import LeagueHistoryCapture

    for name in ("transactions_complete", "roster_complete", "lineup_complete"):
        if row[name] not in (0, 1):
            raise ValueError(f"legacy {name} must be zero or one")
    LeagueHistoryCapture.from_record(
        {
            "capture_id": row["capture_id"],
            "captured_at": row["captured_at"],
            "coverage_end": row["coverage_end"],
            "coverage_start": row["coverage_start"],
            "league_key": row["league_key"],
            "lineup_complete": bool(row["lineup_complete"]),
            "roster_complete": bool(row["roster_complete"]),
            "rosters": body["rosters"],
            "schema_version": 1,
            "season": row["season"],
            "teams": body["teams"],
            "transaction_history_complete": bool(row["transactions_complete"]),
            "transactions": transactions,
        }
    )


def _validate_v1_binding(row, body):
    from .league_history import HistoryBundleBinding, _timestamp

    binding = HistoryBundleBinding.from_record(body)
    if (
        row["bundle_id"] != binding.bundle_id
        or row["league_key"] != binding.league_key
        or row["season"] != binding.season
        or row["captured_at"] != _timestamp(binding.captured_at)
    ):
        raise ValueError("legacy binding columns conflict with its record")


def _replace_v1_schema(
    connection, captures, transactions, links, bindings
):
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            for name in (
                "capture_transaction",
                "bundle_binding",
                "transaction_event",
                "history_capture",
            ):
                connection.execute(f"DROP TABLE {name}")
            _create_current(connection)
            connection.executemany(
                "INSERT INTO history_capture VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                captures,
            )
            connection.executemany(
                "INSERT INTO transaction_event VALUES (?,?,?,?,?,?,?)", transactions
            )
            connection.executemany(
                "INSERT INTO capture_transaction VALUES (?,?,?,?)", links
            )
            connection.executemany(
                "INSERT INTO bundle_binding VALUES (?,?,?,?,?,?,?,?,?)",
                bindings,
            )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("migrated history contains an invalid foreign key")
            connection.execute(f"PRAGMA user_version = {LEAGUE_HISTORY_SCHEMA_VERSION}")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _create_current(connection: sqlite3.Connection) -> None:
    for statement in CURRENT_TABLES:
        connection.execute(statement)
    for statement in CURRENT_INDEXES:
        connection.execute(statement)


def _require_objects(connection, tables, indexes, error_type):
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    actual = {
        (row["type"], row["name"]): _normalized_sql(row["sql"])
        for row in rows
        if row["sql"] is not None
    }
    expected = {
        **{("table", _schema_name(sql)): _normalized_sql(sql) for sql in tables},
        **{("index", _schema_name(sql)): _normalized_sql(sql) for sql in indexes},
    }
    if actual != expected:
        raise error_type("league history store table schema is invalid")


def _roster_ownership_id(rosters: object) -> str:
    if not isinstance(rosters, list):
        raise ValueError("legacy roster evidence must be an array")
    teams = []
    for roster in rosters:
        if not isinstance(roster, dict) or set(roster) != {"players", "team_id"}:
            raise ValueError("legacy roster evidence is invalid")
        players = roster["players"]
        if not isinstance(players, list):
            raise ValueError("legacy roster players must be an array")
        player_ids = []
        for player in players:
            if not isinstance(player, dict) or "canonical_player_id" not in player:
                raise ValueError("legacy roster player evidence is invalid")
            player_ids.append(player["canonical_player_id"])
        teams.append({"player_ids": sorted(player_ids), "team_id": roster["team_id"]})
    teams.sort(key=lambda row: row["team_id"])
    return content_id("history-roster", {"teams": teams})


def _strict_json_loads(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("legacy history JSON must be text")

    def reject_constant(constant: str) -> None:
        raise ValueError(f"legacy history JSON contains {constant}")

    def unique_object(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError(f"legacy history JSON contains duplicate key {key!r}")
            result[key] = child
        return result

    return json.loads(
        value,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _schema_name(statement: str) -> str:
    match = re.match(r"CREATE (?:TABLE|INDEX) ([a-z_]+)", statement)
    if match is None:
        raise AssertionError("schema statement does not have a canonical name")
    return match.group(1)


def _normalized_sql(value: object) -> str:
    return "" if not isinstance(value, str) else "".join(value.split()).casefold()


__all__ = (
    "CURRENT_INDEXES",
    "CURRENT_TABLES",
    "LEAGUE_HISTORY_APPLICATION_ID",
    "LEAGUE_HISTORY_SCHEMA_VERSION",
    "prepare_schema",
    "require_schema",
)
