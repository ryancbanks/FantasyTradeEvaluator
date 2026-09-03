"""Sanitized, content-addressed MyPlaybook league-source captures."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite

from ._analyzer_types import BundleFingerprint

from ._capture_artifacts import (
    _ARTIFACT_FIELDS,
    _metadata_arguments,
    _metadata_record,
    _require_artifact_header,
    _require_matching_id,
    _set_metadata,
    _validate_metadata,
)
from ._capture_common import (
    content_id,
    freeze_json,
    normalize_json,
    sanitize_capture_body,
    schema_fingerprint,
    thaw_json,
    require_json_int,
)
from ._capture_plan import CaptureKind, CaptureProvider
from ._capture_validation import enum_value, exact_fields


class LeagueSourceKind(str, Enum):
    BOOTSTRAP = "bootstrap"
    ANALYZER_INIT = "analyzer_init"
    PROJECTED_STANDINGS = "projected_standings"


REQUIRED_LEAGUE_SOURCES = frozenset(LeagueSourceKind)
_SOURCE_PAYLOAD_FIELDS = {
    LeagueSourceKind.BOOTSTRAP: {"current_week", "league", "players", "teams", "rosters"},
    LeagueSourceKind.ANALYZER_INIT: {"best_free_agent_ids", "standings"},
    LeagueSourceKind.PROJECTED_STANDINGS: {"playoffsTeam", "standings"},
}
LEAGUE_SOURCE_SCHEMA_FINGERPRINT = schema_fingerprint(
    "fantasypros_league_source_artifact",
    {
        "fields": [
            "metadata", "team_count", "complete", "bundle_url", "bundle_sha256",
            "sources[].source", "sources[].body"
        ],
        "required_sources": sorted(source.value for source in REQUIRED_LEAGUE_SOURCES),
        "source_payload_fields": {
            source.value: sorted(fields) for source, fields in _SOURCE_PAYLOAD_FIELDS.items()
        },
        "semantic_coverage": [
            "team_count", "team_ids", "rosters", "current_standings",
            "best_free_agent_ids", "projected_standings",
            "task_season_week_linkage",
        ],
        "schedule_boundary": "external_capture_required",
        "sanitization": {
            "source_body_fields": ["payload"],
            "recursive_secret_and_transport_key_removal": True,
            "recursive_url_value_removal": True,
            "portable_json_only": True,
            "max_depth": 24,
            "max_nodes": 250000,
            "max_string_length": 100000,
        },
        "policy_version": "trade-analyzer-bootstrap-passive-init-v6-configurable-host-id",
    },
)


@dataclass(frozen=True, slots=True)
class LeagueSource:
    source: LeagueSourceKind | str
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        source = enum_value(LeagueSourceKind, "source", self.source)
        sanitized = sanitize_capture_body(self.body)
        if not isinstance(sanitized, dict) or set(sanitized) != {"payload"}:
            raise ValueError("league source body must contain only payload")
        payload = sanitized["payload"]
        if not isinstance(payload, (dict, list)) or not payload:
            raise ValueError("league source payload must be a non-empty JSON object or array")
        _validate_source_payload(source, payload)
        _bounded_tree(sanitized)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "body", freeze_json(sanitized))

    def to_record(self) -> dict[str, object]:
        return {"source": self.source.value, "body": thaw_json(self.body)}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "LeagueSource":
        exact_fields(record, {"source", "body"}, "league source")
        raw = normalize_json(record["body"], "league source body")
        if raw != sanitize_capture_body(raw):
            raise ValueError("stored league source contains secret, transport, or URL data")
        return cls(record["source"], raw)


@dataclass(frozen=True, slots=True)
class FantasyProsLeagueArtifact:
    task_id: str
    provider: CaptureProvider | str
    season: int
    week: int
    kind: CaptureKind | str
    captured_at: str
    team_count: int
    complete: bool
    bundle_url: str
    bundle_sha256: str
    sources: tuple[LeagueSource, ...]
    artifact_id: str = field(init=False)

    def __init__(
        self,
        task_id: str,
        provider: CaptureProvider | str,
        season: int,
        week: int,
        kind: CaptureKind | str,
        captured_at: str,
        team_count: int,
        complete: bool,
        bundle_url: str,
        bundle_sha256: str,
        sources: Iterable[LeagueSource],
    ) -> None:
        metadata = _validate_metadata(
            task_id, provider, season, week, kind, captured_at, CaptureKind.LEAGUE_SOURCE
        )
        if metadata[1] is not CaptureProvider.FANTASYPROS:
            raise ValueError("league source artifacts must use FantasyPros")
        team_count = require_json_int("team_count", team_count, minimum=2, maximum=100)
        if complete is not True:
            raise ValueError("league source capture must be complete")
        bundle = BundleFingerprint(bundle_url, bundle_sha256)
        if isinstance(sources, (str, bytes)):
            raise ValueError("sources must contain LeagueSource values")
        try:
            rows = tuple(sources)
        except TypeError:
            raise ValueError("sources must contain LeagueSource values") from None
        if any(not isinstance(row, LeagueSource) for row in rows):
            raise ValueError("sources must contain LeagueSource values")
        kinds = tuple(row.source for row in rows)
        if len(set(kinds)) != len(kinds) or set(kinds) != REQUIRED_LEAGUE_SOURCES:
            raise ValueError("league source capture must contain every required source once")
        rows = tuple(sorted(rows, key=lambda row: row.source.value))
        _validate_capture_coverage(team_count, metadata[2], metadata[3], rows)
        _set_metadata(self, metadata)
        object.__setattr__(self, "team_count", team_count)
        object.__setattr__(self, "complete", True)
        object.__setattr__(self, "bundle_url", bundle.url)
        object.__setattr__(self, "bundle_sha256", bundle.sha256)
        object.__setattr__(self, "sources", rows)
        object.__setattr__(self, "artifact_id", content_id("capleague", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        return {
            **_metadata_record(self),
            "team_count": self.team_count,
            "complete": self.complete,
            "bundle_url": self.bundle_url,
            "bundle_sha256": self.bundle_sha256,
            "sources": [source.to_record() for source in self.sources],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema_fingerprint": LEAGUE_SOURCE_SCHEMA_FINGERPRINT,
            **self._content_record(),
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "FantasyProsLeagueArtifact":
        exact_fields(
            record,
            _ARTIFACT_FIELDS
            | {"team_count", "complete", "bundle_url", "bundle_sha256", "sources"},
            "FantasyPros league source artifact",
        )
        _require_artifact_header(record, LEAGUE_SOURCE_SCHEMA_FINGERPRINT)
        if not isinstance(record["sources"], list):
            raise ValueError("league source artifact sources must be a JSON array")
        artifact = cls(
            **_metadata_arguments(record), team_count=record["team_count"],
            complete=record["complete"],
            bundle_url=record["bundle_url"], bundle_sha256=record["bundle_sha256"],
            sources=tuple(LeagueSource.from_record(row) for row in record["sources"]),
        )
        _require_matching_id(record, artifact.artifact_id)
        return artifact


def _bounded_tree(value: object) -> None:
    stack, nodes = [(value, 0)], 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 250000 or depth > 24:
            raise ValueError("league source body exceeds its structural limits")
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str) and len(current) > 100000:
            raise ValueError("league source body contains an oversized string")


def _validate_source_payload(source: LeagueSourceKind, payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != _SOURCE_PAYLOAD_FIELDS[source]:
        raise ValueError(f"{source.value} payload fields do not match the league-source schema")
    if source is LeagueSourceKind.BOOTSTRAP:
        _positive_int(payload["current_week"], "current_week", maximum=25)
        _league(payload["league"])
        _players(payload["players"])
        _teams(payload["teams"])
        _rosters(payload["rosters"])
    elif source is LeagueSourceKind.ANALYZER_INIT:
        _current_standings(payload["standings"])
        _free_agent_ids(payload["best_free_agent_ids"])
    else:
        _projected_standings(payload["standings"])
        _positive_int(payload["playoffsTeam"], "playoffsTeam", maximum=100)


def _validate_capture_coverage(
    team_count: int,
    season: int,
    week: int,
    sources: tuple[LeagueSource, ...],
) -> None:
    payloads = {
        source.source: thaw_json(source.body)["payload"] for source in sources
    }
    bootstrap = payloads[LeagueSourceKind.BOOTSTRAP]
    if bootstrap["current_week"] != week or bootstrap["league"]["season"] != season:
        raise ValueError("bootstrap season and week must match capture task metadata")
    team_ids = _ids(bootstrap["teams"], "team_id")
    if len(team_ids) != team_count:
        raise ValueError("bootstrap team coverage does not match team_count")
    if bootstrap["league"]["team_count"] != team_count:
        raise ValueError("league team_count does not match captured team coverage")
    if _ids(bootstrap["rosters"], "team_id") != team_ids:
        raise ValueError("roster team coverage does not match team_count")
    known_players = _ids(bootstrap["players"], "player_id")
    roster_players = {
        player_id for roster in bootstrap["rosters"] for player_id in roster["player_ids"]
    }
    if not roster_players <= known_players:
        raise ValueError("roster players are missing from the captured player identity table")
    projected = payloads[LeagueSourceKind.PROJECTED_STANDINGS]["standings"]
    current = payloads[LeagueSourceKind.ANALYZER_INIT]["standings"]
    if _ids(current, "teamId") != team_ids or _ids(projected, "teamId") != team_ids:
        raise ValueError("standings team coverage does not match team_count")
    best_free_agents = set(
        payloads[LeagueSourceKind.ANALYZER_INIT]["best_free_agent_ids"]
    )
    if not best_free_agents <= known_players:
        raise ValueError(
            "best free agents are missing from the captured player identity table"
        )
    if best_free_agents & roster_players:
        raise ValueError("best free agents cannot already belong to a league roster")
    if payloads[LeagueSourceKind.PROJECTED_STANDINGS]["playoffsTeam"] > team_count:
        raise ValueError("playoffsTeam cannot exceed team_count")


def _league(value: object) -> None:
    required = {"team_count", "season", "playoff_teams", "roster_size", "scoring"}
    allowed = required | {
        "id", "name", "team_id", "team_name", "host", "sport", "positions",
        "host_league_id",
        "playoffs_start_week", "playoffs_end_week", "playoff_reseeding",
        "basic_scoring", "total_rounds", "has_rosters", "is_manual",
    }
    _object_fields(value, required, allowed, "league")
    _positive_int(value["team_count"], "league team_count", maximum=100)
    _positive_int(value["season"], "league season", maximum=9999)
    _positive_int(value["playoff_teams"], "playoff_teams", maximum=100)
    _positive_int(value["roster_size"], "roster_size", maximum=100)
    if not isinstance(value["scoring"], str) or not value["scoring"].strip():
        raise ValueError("league scoring must be non-empty text")
    if str(value.get("host", "")).upper() == "ESPN" and value.get(
        "host_league_id"
    ) is not None:
        _id(value.get("host_league_id"), "host_league_id")


def _players(value: object) -> None:
    required = {"player_id", "name"}
    allowed = required | {
        "team_id", "position_id", "position", "positions", "eligibility",
        "eligibility_espn", "eligibility_yahoo", "espn_id", "yahoo_id",
    }
    _rows(value, required, allowed, "players")
    if len(_ids(value, "player_id")) != len(value):
        raise ValueError("players must have unique player_id values")
    for row in value:
        if not isinstance(row["name"], str) or not row["name"].strip():
            raise ValueError("player name must be non-empty text")
    for field in ("espn_id", "yahoo_id"):
        ids = [
            _id(row[field], field)
            for row in value
            if row.get(field) is not None
        ]
        if len(ids) != len(set(ids)):
            raise ValueError(f"players must have unique {field} values")


def _teams(value: object) -> None:
    fields = {"team_id", "team_name"}
    _rows(value, fields, fields | {"needs"}, "teams")


def _rosters(value: object) -> None:
    _rows(value, {"team_id", "player_ids"}, {"team_id", "player_ids"}, "rosters")
    seen: set[str] = set()
    for row in value:
        players = row["player_ids"]
        if not isinstance(players, list) or not players:
            raise ValueError("each roster must contain player_ids")
        player_ids = {_id(item, "player_id") for item in players}
        if len(player_ids) != len(players) or seen & player_ids:
            raise ValueError("roster player_ids must be unique within the league")
        seen.update(player_ids)


def _current_standings(value: object) -> None:
    fields = {"teamId", "wins", "losses", "ties"}
    _rows(value, fields, fields, "current standings")
    for row in value:
        for name in fields - {"teamId"}:
            number = row[name]
            if (
                isinstance(number, bool) or not isinstance(number, (int, float))
                or not isfinite(number) or number < 0
            ):
                raise ValueError(f"current standings {name} must be nonnegative numeric data")


def _free_agent_ids(value: object) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 1000
        or any(_id(item, "best free-agent ID") != item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(
            "best_free_agent_ids must contain unique positive decimal provider IDs"
        )


def _projected_standings(value: object) -> None:
    fields = {
        "teamId", "teamName", "rank_proj", "rank_current", "wins_current",
        "losses_current", "wins_proj", "losses_proj", "playoffs_odds",
        "championship_odds",
    }
    _rows(value, fields, fields, "projected standings")
    for row in value:
        for name in fields - {"teamId", "teamName"}:
            number = row[name]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not isfinite(number):
                raise ValueError(f"projected standings {name} must be finite numeric data")
        for name in ("playoffs_odds", "championship_odds"):
            if not 0 <= row[name] <= 100:
                raise ValueError(f"projected standings {name} must be a percentage")


def _rows(value: object, required: set[str], allowed: set[str], label: str) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    for row in value:
        _object_fields(row, required, allowed, label)


def _object_fields(
    value: object, required: set[str], allowed: set[str], label: str
) -> None:
    if not isinstance(value, dict) or not required <= set(value) <= allowed:
        raise ValueError(f"{label} fields do not match the league-source schema")


def _ids(rows: list[object], field: str) -> set[str]:
    values = {_id(row[field], field) for row in rows}
    if len(values) != len(rows):
        raise ValueError(f"{field} values must be unique")
    return values


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.isdigit() or value == "0":
        raise ValueError(f"{label} must be a positive decimal provider ID")
    return value


def _positive_int(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 1 through {maximum}")
    return value


__all__ = (
    "FantasyProsLeagueArtifact", "LEAGUE_SOURCE_SCHEMA_FINGERPRINT",
    "LeagueSource", "LeagueSourceKind", "REQUIRED_LEAGUE_SOURCES",
)
