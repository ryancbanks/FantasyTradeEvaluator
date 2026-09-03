"""Strict, content-addressed historical data for local draft training.

The record deliberately keeps preseason observations and realized weekly outcomes
in separate fields.  Drafting code consumes :class:`PreseasonPlayer` only; actual
statistics are joined after a lineup is locked.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import json
import math
from numbers import Real
from types import MappingProxyType
from typing import Any

from ._scenario_random import content_id
from .draft_feature_policy import (
    PRESEASON_FEATURE_POLICY_VERSION,
    validate_preseason_feature_name,
)
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_player_position


HISTORICAL_CORPUS_SCHEMA_VERSION = 1
SUPPORTED_DRAFT_SEASONS = frozenset(
    {2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023, 2024, 2025}
)
_MAX_PLAYERS_PER_SEASON = 5_000
# The fitted vector adds one missing-value bit per imported feature plus 87
# fixed context features.  Keep imports within FeatureSchema's 256-name limit
# so a corpus that validates here cannot fail later merely because it is wide.
_MAX_FEATURES = 169
_MAX_WEEKS = 25


class ActualWeekStatus(str, Enum):
    PLAYED = "played"
    INACTIVE = "inactive"
    BYE = "bye"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class DataProvenance:
    source: str
    captured_at: str
    scope: str
    license: str | None = None
    source_url: str | None = None
    preseason_feature_names: tuple[str, ...] = ()
    preseason_source_as_of: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text("provenance source", self.source)
        _text("provenance scope", self.scope)
        captured_at = _timestamp("provenance captured_at", self.captured_at)
        for name in ("license", "source_url"):
            value = getattr(self, name)
            if value is not None:
                _text(f"provenance {name}", value)
        names = _text_sequence(
            "provenance preseason_feature_names", self.preseason_feature_names
        )
        for name in names:
            validate_preseason_feature_name(name)
        source_dates = _preseason_source_dates(self.preseason_source_as_of)
        if bool(names) != bool(source_dates):
            raise ValueError(
                "preseason feature provenance needs feature names and source-as-of dates"
            )
        if any(
            _timestamp("preseason source_as_of", value) > captured_at
            for value in source_dates.values()
        ):
            raise ValueError("preseason source_as_of cannot be after captured_at")
        object.__setattr__(self, "preseason_feature_names", tuple(sorted(names)))
        object.__setattr__(self, "preseason_source_as_of", source_dates)

    def to_record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "captured_at": self.captured_at,
            "scope": self.scope,
            "license": self.license,
            "source_url": self.source_url,
            "preseason_feature_names": list(self.preseason_feature_names),
            "preseason_source_as_of": {
                str(season): value
                for season, value in self.preseason_source_as_of.items()
            },
        }


@dataclass(frozen=True, slots=True)
class ActualPlayerWeek:
    week: int
    status: ActualWeekStatus
    stats: Mapping[str, float]

    def __post_init__(self) -> None:
        _integer("actual week", self.week, 1, _MAX_WEEKS)
        if not isinstance(self.status, ActualWeekStatus):
            raise ValueError("actual week status is invalid")
        stats = _number_map("actual stats", self.stats, allow_missing=False)
        if self.status is not ActualWeekStatus.PLAYED and stats:
            raise ValueError("only played weeks may contain actual statistics")
        if self.status is ActualWeekStatus.PLAYED and not stats:
            raise ValueError("played weeks require actual statistics")
        object.__setattr__(self, "stats", stats)

    def to_record(self) -> dict[str, object]:
        return {"week": self.week, "status": self.status.value, "stats": dict(self.stats)}


@dataclass(frozen=True, slots=True)
class PreseasonPlayer:
    """A player snapshot whose identifiers are metadata, never brain features."""

    player_id: str
    display_name: str
    position: str
    eligible_positions: tuple[str, ...]
    nfl_team_id: str
    bye_week: int
    nfl_experience_years: int
    rookie: bool
    first_year_on_team: bool
    preseason_features: Mapping[str, float | None]
    actual_weeks: tuple[ActualPlayerWeek, ...]

    def __post_init__(self) -> None:
        _text("player_id", self.player_id)
        _text("display_name", self.display_name)
        _text("nfl_team_id", self.nfl_team_id)
        position = normalize_player_position(self.position, require_supported=True)
        positions = tuple(
            normalize_player_position(value, require_supported=True)
            for value in self.eligible_positions
        )
        if not positions or len(set(positions)) != len(positions):
            raise ValueError("eligible_positions must be non-empty and unique")
        if position not in positions:
            raise ValueError("primary position must be eligible")
        _integer("bye_week", self.bye_week, 1, _MAX_WEEKS)
        _integer("nfl_experience_years", self.nfl_experience_years, 0, 40)
        if type(self.rookie) is not bool or type(self.first_year_on_team) is not bool:
            raise ValueError("rookie and first_year_on_team must be booleans")
        if self.rookie != (self.nfl_experience_years == 0):
            raise ValueError("rookie must agree with zero NFL experience")
        features = _number_map(
            "preseason_features", self.preseason_features, allow_missing=True
        )
        if not features:
            raise ValueError("preseason_features cannot be empty")
        if len(features) > _MAX_FEATURES:
            raise ValueError("preseason_features exceeds the supported field limit")
        for name in features:
            validate_preseason_feature_name(name)
        weeks = tuple(self.actual_weeks)
        if any(not isinstance(row, ActualPlayerWeek) for row in weeks):
            raise ValueError("actual_weeks must contain ActualPlayerWeek values")
        if len({row.week for row in weeks}) != len(weeks):
            raise ValueError("actual_weeks cannot contain duplicate weeks")
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "eligible_positions", tuple(sorted(positions)))
        object.__setattr__(self, "preseason_features", features)
        object.__setattr__(self, "actual_weeks", tuple(sorted(weeks, key=lambda row: row.week)))

    def to_record(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "position": self.position,
            "eligible_positions": list(self.eligible_positions),
            "nfl_team_id": self.nfl_team_id,
            "bye_week": self.bye_week,
            "nfl_experience_years": self.nfl_experience_years,
            "rookie": self.rookie,
            "first_year_on_team": self.first_year_on_team,
            "preseason_features": dict(self.preseason_features),
            "actual_weeks": [row.to_record() for row in self.actual_weeks],
        }


@dataclass(frozen=True, slots=True)
class HistoricalSeason:
    """One leak-free pairing of that season's preseason view and outcomes."""

    season: int
    preseason_as_of: str
    season_kickoff_at: str
    available_weeks: tuple[int, ...]
    players: tuple[PreseasonPlayer, ...]

    def __post_init__(self) -> None:
        if self.season not in SUPPORTED_DRAFT_SEASONS:
            raise ValueError("season is outside the requested historical training set")
        captured = _timestamp("preseason_as_of", self.preseason_as_of)
        kickoff = _timestamp("season_kickoff_at", self.season_kickoff_at)
        if (
            kickoff.year != self.season
            or captured.year != self.season
            or captured >= kickoff
        ):
            raise ValueError("preseason_as_of must be before that season's kickoff")
        weeks = tuple(self.available_weeks)
        if (
            not weeks
            or any(type(week) is not int or not 1 <= week <= _MAX_WEEKS for week in weeks)
            or tuple(sorted(set(weeks))) != weeks
        ):
            raise ValueError("available_weeks must be unique increasing NFL weeks")
        players = tuple(self.players)
        if not players or len(players) > _MAX_PLAYERS_PER_SEASON:
            raise ValueError("historical season player count is invalid")
        if any(not isinstance(player, PreseasonPlayer) for player in players):
            raise ValueError("players must contain PreseasonPlayer values")
        ids = tuple(player.player_id for player in players)
        if len(set(ids)) != len(ids):
            raise ValueError("historical season contains duplicate player IDs")
        if len(_preseason_feature_names(players)) > _MAX_FEATURES:
            raise ValueError(
                "historical season preseason feature union exceeds the supported limit"
            )
        expected = set(weeks)
        for player in players:
            actual = {row.week for row in player.actual_weeks}
            if actual != expected:
                raise ValueError(
                    f"player {player.player_id!r} must explicitly cover every available week"
                )
            bye_rows = [
                row.week
                for row in player.actual_weeks
                if row.status is ActualWeekStatus.BYE
            ]
            expected_bye_rows = [player.bye_week] if player.bye_week in expected else []
            if bye_rows != expected_bye_rows:
                raise ValueError("bye status must agree with the preseason bye week")
        object.__setattr__(self, "available_weeks", weeks)
        object.__setattr__(self, "players", tuple(sorted(players, key=lambda row: row.player_id)))

    def to_record(self) -> dict[str, object]:
        return {
            "season": self.season,
            "preseason_as_of": self.preseason_as_of,
            "season_kickoff_at": self.season_kickoff_at,
            "available_weeks": list(self.available_weeks),
            "players": [row.to_record() for row in self.players],
        }


@dataclass(frozen=True, slots=True)
class HistoricalCorpus:
    seasons: tuple[HistoricalSeason, ...]
    provenance: tuple[DataProvenance, ...]
    corpus_id: str = field(init=False)

    def __post_init__(self) -> None:
        seasons = tuple(self.seasons)
        provenance = tuple(self.provenance)
        if not seasons or any(not isinstance(row, HistoricalSeason) for row in seasons):
            raise ValueError("seasons must contain HistoricalSeason values")
        if len({row.season for row in seasons}) != len(seasons):
            raise ValueError("corpus cannot contain duplicate seasons")
        if len(
            {
                name
                for season in seasons
                for name in _preseason_feature_names(season.players)
            }
        ) > _MAX_FEATURES:
            raise ValueError(
                "historical corpus preseason feature union exceeds the supported limit"
            )
        if not provenance or any(not isinstance(row, DataProvenance) for row in provenance):
            raise ValueError("provenance must contain at least one valid source")
        feature_names = {
            name
            for season in seasons
            for name in _preseason_feature_names(season.players)
        }
        bound_names: set[str] = set()
        duplicate_names: set[str] = set()
        for row in provenance:
            for name in row.preseason_feature_names:
                if name in bound_names:
                    duplicate_names.add(name)
                bound_names.add(name)
        if duplicate_names:
            raise ValueError(
                f"preseason feature {min(duplicate_names)!r} has duplicate provenance"
            )
        unknown_names = bound_names.difference(feature_names)
        if unknown_names:
            raise ValueError(
                f"provenance declares unknown preseason feature {min(unknown_names)!r}"
            )
        unbound_names = feature_names.difference(bound_names)
        if unbound_names:
            raise ValueError(
                f"preseason feature {min(unbound_names)!r} has no provenance binding"
            )
        season_features = {
            season.season: _preseason_feature_names(season.players)
            for season in seasons
        }
        seasons_by_year = {season.season: season for season in seasons}
        for row in provenance:
            expected_years = {
                season
                for season, names in season_features.items()
                if set(row.preseason_feature_names).intersection(names)
            }
            if set(row.preseason_source_as_of) != expected_years:
                raise ValueError(
                    f"provenance {row.source!r} source-as-of seasons do not match "
                    "its bound preseason features"
                )
            for season, source_as_of in row.preseason_source_as_of.items():
                if _timestamp("preseason source_as_of", source_as_of) > _timestamp(
                    "preseason_as_of", seasons_by_year[season].preseason_as_of
                ):
                    raise ValueError(
                        f"provenance {row.source!r} was not available by the "
                        f"{season} preseason snapshot"
                    )
        seasons = tuple(sorted(seasons, key=lambda row: row.season))
        provenance = tuple(sorted(provenance, key=lambda row: (row.source, row.scope)))
        object.__setattr__(self, "seasons", seasons)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "corpus_id", content_id("draft_corpus", self._content_record()))

    @property
    def available_seasons(self) -> tuple[int, ...]:
        return tuple(row.season for row in self.seasons)

    def _content_record(self) -> dict[str, object]:
        return {
            "preseason_feature_policy_version": PRESEASON_FEATURE_POLICY_VERSION,
            "seasons": [row.to_record() for row in self.seasons],
            "provenance": [row.to_record() for row in self.provenance],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "historical_draft_corpus",
            "schema_version": HISTORICAL_CORPUS_SCHEMA_VERSION,
            **self._content_record(),
            "corpus_id": self.corpus_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "HistoricalCorpus":
        keys = {
            "kind", "schema_version", "corpus_id", "seasons", "provenance",
            "preseason_feature_policy_version",
        }
        _exact_object("historical corpus", record, keys)
        if (
            record["kind"] != "historical_draft_corpus"
            or record["schema_version"] != HISTORICAL_CORPUS_SCHEMA_VERSION
            or record["preseason_feature_policy_version"]
            != PRESEASON_FEATURE_POLICY_VERSION
        ):
            raise ValueError(
                "historical corpus kind, schema, or feature policy version is invalid"
            )
        seasons = tuple(_season_from_record(row) for row in _array("seasons", record["seasons"]))
        provenance = tuple(
            DataProvenance(**_exact_object(
                "provenance row",
                row,
                {
                    "source", "captured_at", "scope", "license", "source_url",
                    "preseason_feature_names", "preseason_source_as_of",
                },
            ))
            for row in _array("provenance", record["provenance"])
        )
        corpus = cls(seasons, provenance)
        if record["corpus_id"] != corpus.corpus_id:
            raise ValueError("historical corpus content does not match corpus_id")
        return corpus

    def summary(self) -> dict[str, object]:
        return {
            "corpus_id": self.corpus_id,
            "status": "ready",
            "preseason_feature_policy_version": PRESEASON_FEATURE_POLICY_VERSION,
            "seasons": list(self.available_seasons),
            "season_count": len(self.seasons),
            "player_seasons": sum(len(row.players) for row in self.seasons),
            "feature_names": sorted({
                name
                for season in self.seasons
                for player in season.players
                for name in player.preseason_features
            }),
            "sources": [row.to_record() for row in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class DraftPlayerBoard:
    """A current preseason player pool for the deployable draft assistant."""

    season: int
    preseason_as_of: str
    season_kickoff_at: str
    players: tuple[PreseasonPlayer, ...]
    espn_player_ids: Mapping[str, str] = field(default_factory=dict)
    board_id: str = field(init=False)

    def __post_init__(self) -> None:
        _integer("board season", self.season, 2012, 9999)
        captured = _timestamp("board preseason_as_of", self.preseason_as_of)
        kickoff = _timestamp("board season_kickoff_at", self.season_kickoff_at)
        if (
            kickoff.year != self.season
            or captured.year != self.season
            or captured >= kickoff
        ):
            raise ValueError("board preseason_as_of must be before that season's kickoff")
        players = tuple(self.players)
        if not players or len(players) > _MAX_PLAYERS_PER_SEASON or any(
            not isinstance(player, PreseasonPlayer) for player in players
        ):
            raise ValueError("draft board player count is invalid")
        if len({player.player_id for player in players}) != len(players):
            raise ValueError("draft board contains duplicate player IDs")
        if len(_preseason_feature_names(players)) > _MAX_FEATURES:
            raise ValueError(
                "draft board preseason feature union exceeds the supported limit"
            )
        if any(player.actual_weeks for player in players):
            raise ValueError("draft board players cannot contain future actual outcomes")
        players = tuple(sorted(players, key=lambda row: row.player_id))
        espn_ids = _espn_player_ids(self.espn_player_ids, players)
        object.__setattr__(self, "players", players)
        object.__setattr__(self, "espn_player_ids", espn_ids)
        object.__setattr__(self, "board_id", content_id("draft_board", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        record = {
            "season": self.season,
            "preseason_as_of": self.preseason_as_of,
            "season_kickoff_at": self.season_kickoff_at,
            "preseason_feature_policy_version": PRESEASON_FEATURE_POLICY_VERSION,
            "players": [row.to_record() for row in self.players],
        }
        if self.espn_player_ids:
            record["espn_player_ids"] = dict(self.espn_player_ids)
        return record

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "draft_player_board",
            "schema_version": 2 if self.espn_player_ids else 1,
            **self._content_record(), "board_id": self.board_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "DraftPlayerBoard":
        common = {
            "kind", "schema_version", "board_id", "season", "preseason_as_of",
            "season_kickoff_at", "preseason_feature_policy_version", "players",
        }
        if not isinstance(record, Mapping):
            raise ValueError("draft player board fields are invalid")
        version = record.get("schema_version")
        if (
            record.get("kind") != "draft_player_board"
            or type(version) is not int
            or version not in {1, 2}
        ):
            raise ValueError("draft player board kind or schema version is invalid")
        keys = common if version == 1 else common | {"espn_player_ids"}
        _exact_object("draft player board", record, keys)
        if (
            record["preseason_feature_policy_version"]
            != PRESEASON_FEATURE_POLICY_VERSION
        ):
            raise ValueError("draft player board feature policy version is invalid")
        board = cls(
            record["season"], record["preseason_as_of"], record["season_kickoff_at"],
            tuple(_player_from_record(row) for row in _array("players", record["players"])),
            {} if version == 1 else record["espn_player_ids"],
        )
        if board.to_record()["schema_version"] != version:
            raise ValueError("draft player board schema version is not canonical")
        if record["board_id"] != board.board_id:
            raise ValueError("draft player board content does not match board_id")
        return board

    def summary(self) -> dict[str, object]:
        return {
            "board_id": self.board_id,
            "season": self.season,
            "as_of": self.preseason_as_of,
            "preseason_feature_policy_version": PRESEASON_FEATURE_POLICY_VERSION,
            "player_count": len(self.players),
            "espn_mapped_player_count": len(self.espn_player_ids),
            "positions": sorted({player.position for player in self.players}),
        }


def _espn_player_ids(
    value: object, players: tuple[PreseasonPlayer, ...]
) -> MappingProxyType:
    if not isinstance(value, Mapping) or len(value) > len(players):
        raise ValueError("espn_player_ids must be an object with at most one ID per player")
    player_ids = {player.player_id for player in players}
    result: dict[str, str] = {}
    seen_provider_ids: set[str] = set()
    for player_id, provider_id in value.items():
        if player_id not in player_ids:
            raise ValueError("espn_player_ids contains a player outside this draft board")
        if (
            not isinstance(provider_id, str)
            or not _espn_player_id(provider_id)
        ):
            raise ValueError("ESPN player IDs must be non-zero decimal strings")
        if provider_id in seen_provider_ids:
            raise ValueError("ESPN player IDs must be unique")
        result[player_id] = provider_id
        seen_provider_ids.add(provider_id)
    return MappingProxyType(dict(sorted(result.items())))


def _preseason_feature_names(
    players: Sequence[PreseasonPlayer],
) -> frozenset[str]:
    return frozenset(
        name for player in players for name in player.preseason_features
    )


def _espn_player_id(value: str) -> bool:
    digits = value[1:] if value.startswith("-") else value
    return (
        bool(digits)
        and digits.isascii()
        and digits.isdigit()
        and not digits.startswith("0")
        and len(digits) <= 20
    )


def _season_from_record(value: object) -> HistoricalSeason:
    row = _exact_object(
        "historical season",
        value,
        {"season", "preseason_as_of", "season_kickoff_at", "available_weeks", "players"},
    )
    return HistoricalSeason(
        season=row["season"],
        preseason_as_of=row["preseason_as_of"],
        season_kickoff_at=row["season_kickoff_at"],
        available_weeks=tuple(_array("available_weeks", row["available_weeks"])),
        players=tuple(_player_from_record(item) for item in _array("players", row["players"])),
    )


def _player_from_record(value: object) -> PreseasonPlayer:
    keys = {
        "player_id", "display_name", "position", "eligible_positions", "nfl_team_id",
        "bye_week", "nfl_experience_years", "rookie", "first_year_on_team",
        "preseason_features", "actual_weeks",
    }
    row = _exact_object("historical player", value, keys)
    weeks = []
    for value in _array("actual_weeks", row["actual_weeks"]):
        week = _exact_object("actual week", value, {"week", "status", "stats"})
        try:
            status = ActualWeekStatus(week["status"])
        except (TypeError, ValueError):
            raise ValueError("actual week status is invalid") from None
        weeks.append(ActualPlayerWeek(week["week"], status, week["stats"]))
    return PreseasonPlayer(
        player_id=row["player_id"], display_name=row["display_name"],
        position=row["position"], eligible_positions=tuple(_array("eligible_positions", row["eligible_positions"])),
        nfl_team_id=row["nfl_team_id"], bye_week=row["bye_week"],
        nfl_experience_years=row["nfl_experience_years"], rookie=row["rookie"],
        first_year_on_team=row["first_year_on_team"], preseason_features=row["preseason_features"],
        actual_weeks=tuple(weeks),
    )


def _number_map(name: str, value: object, *, allow_missing: bool) -> MappingProxyType:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    result: dict[str, float | None] = {}
    for key, raw in value.items():
        _text(f"{name} key", key)
        if raw is None and allow_missing:
            result[key] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise ValueError(f"{name}.{key} must be a finite number")
        number = float(raw)
        if not math.isfinite(number) or abs(number) > 1e12:
            raise ValueError(f"{name}.{key} must be a bounded finite number")
        result[key] = 0.0 if number == 0 else number
    return MappingProxyType(dict(sorted(result.items())))


def _exact_object(name: str, value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} fields are invalid")
    return value


def _array(name: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _text_sequence(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must contain unique feature names")
    try:
        result = tuple(value)
    except TypeError:
        raise ValueError(f"{name} must contain unique feature names") from None
    if any(not isinstance(row, str) for row in result) or len(set(result)) != len(result):
        raise ValueError(f"{name} must contain unique feature names")
    return result


def _preseason_source_dates(value: object) -> MappingProxyType:
    if not isinstance(value, Mapping) or len(value) > len(SUPPORTED_DRAFT_SEASONS):
        raise ValueError("preseason_source_as_of must map seasons to timestamps")
    result: dict[int, str] = {}
    for raw_season, raw_timestamp in value.items():
        if type(raw_season) is int:
            season = raw_season
        elif (
            isinstance(raw_season, str)
            and raw_season.isascii()
            and raw_season.isdigit()
            and str(int(raw_season)) == raw_season
        ):
            season = int(raw_season)
        else:
            raise ValueError("preseason_source_as_of season keys are invalid")
        if season not in SUPPORTED_DRAFT_SEASONS or season in result:
            raise ValueError("preseason_source_as_of season keys are invalid")
        parsed = _timestamp("preseason source_as_of", raw_timestamp)
        if parsed.year != season:
            raise ValueError("preseason source_as_of must be in its NFL season")
        result[season] = raw_timestamp
    return MappingProxyType(dict(sorted(result.items())))


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2_048:
        raise ValueError(f"{name} must be non-empty text")


def _timestamp(name: str, value: object) -> datetime:
    _text(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


__all__ = (
    "ActualPlayerWeek", "ActualWeekStatus", "DataProvenance", "HistoricalCorpus",
    "HistoricalSeason", "DraftPlayerBoard", "PreseasonPlayer",
    "PRESEASON_FEATURE_POLICY_VERSION", "SUPPORTED_DRAFT_SEASONS",
    "validate_preseason_feature_name",
)
