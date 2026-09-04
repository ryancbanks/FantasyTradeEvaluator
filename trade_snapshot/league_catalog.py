"""Local, season-scoped league profiles and immutable bundle associations."""

import base64
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .weekly_collection import _host_league_url, _yahoo_projection_url

CATALOG_SCHEMA_VERSION = 2
_PROFILE_ID = re.compile(r"^league_[0-9a-f]{32}$")
_BUNDLE_ID = re.compile(r"^engine_[0-9a-f]{64}$")
_NOT_SET = object()
_PROFILE_PAGE_INDEXES = (
    """CREATE INDEX IF NOT EXISTS league_profiles_active_page
        ON league_profiles (archived, created_at, profile_id)""",
    """CREATE INDEX IF NOT EXISTS league_profiles_all_page
        ON league_profiles (created_at, profile_id)""",
)


@dataclass(frozen=True, slots=True)
class LeagueProfile:
    profile_id: str
    name: str
    season: int
    scoring: str
    espn_league_id: str | None
    yahoo_league_id: str | None
    my_team_id: str | None
    archived: bool
    created_at: str
    updated_at: str

    @property
    def espn_collection_url(self) -> str | None:
        if self.espn_league_id is None:
            return None
        return (
            "https://fantasy.espn.com/football/league?"
            f"leagueId={self.espn_league_id}"
        )

    @property
    def yahoo_collection_url(self) -> str | None:
        if self.yahoo_league_id is None:
            return None
        return (
            "https://football.fantasysports.yahoo.com/f1/"
            f"{self.yahoo_league_id}/players?status=ALL"
        )

    def to_record(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "season": self.season,
            "scoring": self.scoring,
            "espn_league_id": self.espn_league_id,
            "espn_collection_url": self.espn_collection_url,
            "yahoo_league_id": self.yahoo_league_id,
            "yahoo_collection_url": self.yahoo_collection_url,
            "my_team_id": self.my_team_id,
            "archived": self.archived,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class LeagueProfilePage:
    profiles: tuple[LeagueProfile, ...]
    total: int
    next_cursor: str | None

    def to_record(self) -> dict[str, object]:
        return {
            "profiles": [profile.to_record() for profile in self.profiles],
            "total": self.total,
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True, slots=True)
class LeagueBundleAssociation:
    bundle_id: str
    profile_id: str
    season: int
    week: int
    team_count: int
    power_engine_mode: str
    associated_at: str

    def to_record(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "profile_id": self.profile_id,
            "season": self.season,
            "week": self.week,
            "team_count": self.team_count,
            "power_engine_mode": self.power_engine_mode,
            "associated_at": self.associated_at,
        }


class LeagueCatalog:
    """Persist private league connections separately from portable bundles."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_profile(
        self,
        name: object,
        season: object,
        scoring: object,
        *,
        espn_league_url: object = None,
        yahoo_league_url: object = None,
    ) -> LeagueProfile:
        clean_name = _name(name)
        clean_season = _season(season)
        clean_scoring = _scoring(scoring)
        espn_id = _espn_id(espn_league_url, clean_season)
        yahoo_id = _yahoo_id(yahoo_league_url, clean_season)
        created_at = _now()
        profile_id = f"league_{uuid4().hex}"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO league_profiles (
                        profile_id, name, season, scoring, espn_league_id,
                        yahoo_league_id,
                        my_team_id, archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (
                        profile_id, clean_name, clean_season, clean_scoring,
                        espn_id, yahoo_id, created_at, created_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise _connection_conflict(error) from None
            return _profile(self._require_profile(connection, profile_id))

    def get_profile(self, profile_id: object) -> LeagueProfile:
        clean_id = _profile_identifier(profile_id)
        with self._connection() as connection:
            return _profile(self._require_profile(connection, clean_id))

    def update_profile(
        self,
        profile_id: object,
        *,
        name: object = _NOT_SET,
        season: object = _NOT_SET,
        scoring: object = _NOT_SET,
        espn_league_url: object = _NOT_SET,
        yahoo_league_url: object = _NOT_SET,
    ) -> LeagueProfile:
        if all(value is _NOT_SET for value in (
            name, season, scoring, espn_league_url, yahoo_league_url
        )):
            raise ValueError("at least one league profile field must be updated")
        clean_id = _profile_identifier(profile_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _profile(self._require_profile(connection, clean_id))
            clean_name = current.name if name is _NOT_SET else _name(name)
            clean_season = current.season if season is _NOT_SET else _season(season)
            clean_scoring = (
                current.scoring if scoring is _NOT_SET else _scoring(scoring)
            )
            has_bundles = connection.execute(
                "SELECT 1 FROM league_bundles WHERE profile_id = ? LIMIT 1",
                (clean_id,),
            ).fetchone() is not None
            espn_id = (
                current.espn_league_id
                if espn_league_url is _NOT_SET
                else _espn_id(espn_league_url, clean_season)
            )
            yahoo_id = (
                current.yahoo_league_id
                if yahoo_league_url is _NOT_SET
                else _yahoo_id(yahoo_league_url, clean_season)
            )
            if has_bundles:
                if clean_season != current.season:
                    raise ValueError(
                        "season cannot change while the league has an associated bundle"
                    )
                if clean_scoring != current.scoring:
                    raise ValueError(
                        "scoring cannot change while the league has an associated bundle; "
                        "create a new league workspace instead"
                    )
                if (
                    current.espn_league_id is not None
                    and espn_id != current.espn_league_id
                ):
                    raise ValueError(
                        "ESPN league cannot change while the league has an associated "
                        "bundle; create a new league workspace instead"
                    )
            try:
                connection.execute(
                    """
                    UPDATE league_profiles
                    SET name = ?, season = ?, scoring = ?, espn_league_id = ?,
                        yahoo_league_id = ?, updated_at = ?
                    WHERE profile_id = ?
                    """,
                    (
                        clean_name, clean_season, clean_scoring, espn_id,
                        yahoo_id, _now(), clean_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise _connection_conflict(error) from None
            return _profile(self._require_profile(connection, clean_id))

    def archive_profile(self, profile_id: object) -> LeagueProfile:
        return self._set_archived(profile_id, True)

    def restore_profile(self, profile_id: object) -> LeagueProfile:
        return self._set_archived(profile_id, False)

    def save_my_team(
        self, profile_id: object, team_id: object | None
    ) -> LeagueProfile:
        clean_id = _profile_identifier(profile_id)
        clean_team_id = _team_identifier(team_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_profile(connection, clean_id)
            connection.execute(
                """
                UPDATE league_profiles SET my_team_id = ?, updated_at = ?
                WHERE profile_id = ?
                """,
                (clean_team_id, _now(), clean_id),
            )
            return _profile(self._require_profile(connection, clean_id))

    def list_profiles(
        self,
        *,
        season: object | None = None,
        include_archived: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> LeagueProfilePage:
        if not isinstance(include_archived, bool):
            raise ValueError("include_archived must be a boolean")
        if type(limit) is not int or not 1 <= limit <= 250:
            raise ValueError("limit must be an integer from 1 through 250")
        clean_season = None if season is None else _season(season)
        anchor = None if cursor is None else _decode_cursor(cursor)
        clauses, parameters = [], []
        if clean_season is not None:
            clauses.append("season = ?")
            parameters.append(clean_season)
        if not include_archived:
            clauses.append("archived = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        page_clauses = list(clauses)
        page_parameters = list(parameters)
        if anchor is not None:
            page_clauses.append(
                "(created_at > ? OR (created_at = ? AND profile_id > ?))"
            )
            page_parameters.extend((anchor[0], anchor[0], anchor[1]))
        page_where = f"WHERE {' AND '.join(page_clauses)}" if page_clauses else ""
        with self._connection() as connection:
            connection.execute("BEGIN")
            total = connection.execute(
                f"SELECT COUNT(*) FROM league_profiles {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT * FROM league_profiles {page_where}
                ORDER BY created_at, profile_id LIMIT ?
                """,
                (*page_parameters, limit + 1),
            ).fetchall()
        visible = rows[:limit]
        next_cursor = (
            _encode_cursor(visible[-1]["created_at"], visible[-1]["profile_id"])
            if len(rows) > limit
            else None
        )
        return LeagueProfilePage(tuple(map(_profile, visible)), total, next_cursor)

    def associate_bundle(
        self,
        profile_id: object,
        *,
        bundle_id: object,
        season: object,
        week: object,
        team_count: object,
        power_engine_mode: object,
        scoring: object,
        expected_espn_league_id: str | None = None,
    ) -> LeagueBundleAssociation:
        clean_profile_id = _profile_identifier(profile_id)
        values = _bundle_values(
            bundle_id, season, week, team_count, power_engine_mode
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = _profile(self._require_profile(connection, clean_profile_id))
            if owner.archived:
                raise ValueError("restore the league profile before adding a bundle")
            if owner.season != values[1]:
                raise ValueError("bundle season must match the league profile season")
            if owner.scoring != _scoring(scoring):
                raise ValueError(
                    "bundle reception scoring must match the league profile scoring"
                )
            if (
                expected_espn_league_id is not None
                and owner.espn_league_id != expected_espn_league_id
            ):
                raise ValueError(
                    "ESPN league changed while weekly collection was running"
                )
            existing = connection.execute(
                "SELECT * FROM league_bundles WHERE bundle_id = ?", (values[0],)
            ).fetchone()
            if existing is not None:
                association = _association(existing)
                if association.profile_id != clean_profile_id:
                    raise ValueError("bundle is already associated with another league")
                if _association_values(association) != values:
                    raise ValueError("saved bundle summary does not match this bundle")
                return association
            connection.execute(
                """
                INSERT INTO league_bundles (
                    bundle_id, profile_id, season, week, team_count,
                    power_engine_mode, associated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (values[0], clean_profile_id, *values[1:], _now()),
            )
            return _association(connection.execute(
                "SELECT * FROM league_bundles WHERE bundle_id = ?", (values[0],)
            ).fetchone())

    def bundle_association(
        self, bundle_id: object
    ) -> LeagueBundleAssociation | None:
        clean_id = _bundle_identifier(bundle_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM league_bundles WHERE bundle_id = ?", (clean_id,)
            ).fetchone()
        return None if row is None else _association(row)

    def list_bundle_associations(
        self, profile_id: object
    ) -> tuple[LeagueBundleAssociation, ...]:
        clean_id = _profile_identifier(profile_id)
        with self._connection() as connection:
            connection.execute("BEGIN")
            self._require_profile(connection, clean_id)
            rows = connection.execute(
                """
                SELECT * FROM league_bundles WHERE profile_id = ?
                ORDER BY season DESC, week DESC, bundle_id
                """,
                (clean_id,),
            ).fetchall()
        return tuple(map(_association, rows))

    def list_bundle_ids(self, profile_id: object) -> tuple[str, ...]:
        return tuple(
            row.bundle_id for row in self.list_bundle_associations(profile_id)
        )

    def associated_bundle_ids(self) -> frozenset[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT bundle_id FROM league_bundles").fetchall()
        return frozenset(row["bundle_id"] for row in rows)

    def _set_archived(self, profile_id: object, archived: bool) -> LeagueProfile:
        clean_id = _profile_identifier(profile_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_profile(connection, clean_id)
            connection.execute(
                "UPDATE league_profiles SET archived = ?, updated_at = ? "
                "WHERE profile_id = ?",
                (int(archived), _now(), clean_id),
            )
            return _profile(self._require_profile(connection, clean_id))

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > CATALOG_SCHEMA_VERSION:
                raise RuntimeError(
                    f"league catalog schema {version} is newer than this app supports"
                )
            if version == 0:
                statements = (
                    """CREATE TABLE IF NOT EXISTS league_profiles (
                        profile_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        season INTEGER NOT NULL,
                        scoring TEXT NOT NULL CHECK (scoring IN ('STD', 'HALF', 'PPR')),
                        espn_league_id TEXT,
                        yahoo_league_id TEXT,
                        my_team_id TEXT,
                        archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )""",
                    """CREATE UNIQUE INDEX IF NOT EXISTS league_profiles_espn
                        ON league_profiles (season, espn_league_id)
                        WHERE espn_league_id IS NOT NULL""",
                    """CREATE TABLE IF NOT EXISTS league_bundles (
                        bundle_id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL REFERENCES league_profiles(profile_id)
                            ON DELETE RESTRICT ON UPDATE CASCADE,
                        season INTEGER NOT NULL,
                        week INTEGER NOT NULL,
                        team_count INTEGER NOT NULL,
                        power_engine_mode TEXT NOT NULL,
                        associated_at TEXT NOT NULL
                    )""",
                    """CREATE INDEX IF NOT EXISTS league_bundles_profile
                        ON league_bundles (profile_id, season, week)""",
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}"
                )
            elif version == 1:
                # Yahoo supplies reusable projection/scoring context; it is not
                # the host-league identity. Multiple ESPN leagues may therefore
                # share one Yahoo source in the same season.
                connection.execute("DROP INDEX IF EXISTS league_profiles_yahoo")
                connection.execute(
                    f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}"
                )
            # These indexes do not change the stored format, so existing v2
            # catalogs receive the scale optimization without a migration.
            for statement in _PROFILE_PAGE_INDEXES:
                connection.execute(statement)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_profile(
        connection: sqlite3.Connection, profile_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM league_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown league profile")
        return row


def _profile(row: sqlite3.Row) -> LeagueProfile:
    return LeagueProfile(
        row["profile_id"], row["name"], row["season"], row["scoring"],
        row["espn_league_id"], row["yahoo_league_id"], row["my_team_id"],
        bool(row["archived"]), row["created_at"], row["updated_at"],
    )


def _association(row: sqlite3.Row) -> LeagueBundleAssociation:
    return LeagueBundleAssociation(
        row["bundle_id"], row["profile_id"], row["season"], row["week"],
        row["team_count"], row["power_engine_mode"], row["associated_at"],
    )


def _name(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 120
        or not value.isprintable()
    ):
        raise ValueError("name must be 1 to 120 printable characters without outer spaces")
    return value


def _season(value: object) -> int:
    if type(value) is not int or not 2012 <= value <= 9999:
        raise ValueError("season must be an integer from 2012 through 9999")
    return value


def _scoring(value: object) -> str:
    if not isinstance(value, str) or value not in {"STD", "HALF", "PPR"}:
        raise ValueError("scoring must be STD, HALF, or PPR")
    return value


def _profile_identifier(value: object) -> str:
    if not isinstance(value, str) or _PROFILE_ID.fullmatch(value) is None:
        raise ValueError("profile_id is invalid")
    return value


def _bundle_identifier(value: object) -> str:
    if not isinstance(value, str) or _BUNDLE_ID.fullmatch(value) is None:
        raise ValueError("bundle_id is invalid")
    return value


def _team_identifier(value: object | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 200
        or not value.isprintable()
    ):
        raise ValueError("team_id must be null or 1 to 200 printable characters")
    return value


def _espn_id(value: object, season: int) -> str | None:
    canonical = _host_league_url(value, season)
    if canonical is None:
        return None
    return parse_qs(urlsplit(canonical).query)["leagueId"][0]


def _yahoo_id(value: object, season: int) -> str | None:
    canonical = _yahoo_projection_url(value, season)
    if canonical is None:
        return None
    return urlsplit(canonical).path.split("/f1/", 1)[1].split("/", 1)[0]


def _bundle_values(
    bundle_id: object,
    season: object,
    week: object,
    team_count: object,
    power_engine_mode: object,
) -> tuple[str, int, int, int, str]:
    clean_id = _bundle_identifier(bundle_id)
    clean_season = _season(season)
    if type(week) is not int or not 1 <= week <= 25:
        raise ValueError("week must be an integer from 1 through 25")
    if type(team_count) is not int or not 2 <= team_count <= 10_000:
        raise ValueError("team_count must be an integer from 2 through 10,000")
    if power_engine_mode not in {"exact", "surrogate", "independent"}:
        raise ValueError(
            "power_engine_mode must be exact, surrogate, or independent"
        )
    return clean_id, clean_season, week, team_count, power_engine_mode


def _association_values(
    association: LeagueBundleAssociation,
) -> tuple[str, int, int, int, str]:
    return (
        association.bundle_id, association.season, association.week,
        association.team_count, association.power_engine_mode,
    )


def _connection_conflict(error: sqlite3.IntegrityError) -> ValueError:
    detail = str(error)
    if "espn_league_id" in detail:
        return ValueError("this ESPN league is already connected for that season")
    return ValueError("league profile conflicts with an existing record")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _encode_cursor(created_at: str, profile_id: str) -> str:
    raw = json.dumps([created_at, profile_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        created_at, profile_id = decoded
        parsed = datetime.fromisoformat(created_at)
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError
        return created_at, _profile_identifier(profile_id)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("cursor is invalid") from None


__all__ = (
    "CATALOG_SCHEMA_VERSION",
    "LeagueBundleAssociation",
    "LeagueCatalog",
    "LeagueProfile",
    "LeagueProfilePage",
)
