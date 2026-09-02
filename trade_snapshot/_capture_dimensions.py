"""Typed, JSON-safe source dimensions for weekly browser capture."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
import re

from ._capture_common import require_text


class RankingHorizon(str, Enum):
    WEEKLY = "weekly"
    ROS = "ros"


@dataclass(frozen=True, slots=True)
class AnalyzerTradeSpec:
    """The immutable trade selection; transport query construction stays runtime-only."""

    team2_id: str
    team1_gets: tuple[str, ...]
    team2_gets: tuple[str, ...]
    team1_adds: tuple[str, ...] = ()
    team2_adds: tuple[str, ...] = ()

    def __init__(
        self,
        team2_id: object,
        team1_gets: Iterable[object],
        team2_gets: Iterable[object],
        team1_adds: Iterable[object] = (),
        team2_adds: Iterable[object] = (),
    ) -> None:
        team = _decimal_id("team2_id", team2_id)
        groups = tuple(
            _id_set(name, values)
            for name, values in (
                ("team1_gets", team1_gets),
                ("team2_gets", team2_gets),
                ("team1_adds", team1_adds),
                ("team2_adds", team2_adds),
            )
        )
        if not groups[0] or not groups[1]:
            raise ValueError("both trade sides must receive at least one player")
        all_ids = [value for group in groups for value in group]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("a player cannot appear in multiple trade selections")
        object.__setattr__(self, "team2_id", team)
        for name, values in zip(
            ("team1_gets", "team2_gets", "team1_adds", "team2_adds"), groups
        ):
            object.__setattr__(self, name, values)

    def to_record(self) -> dict[str, object]:
        return {
            "team2_id": self.team2_id,
            "team1_gets": list(self.team1_gets),
            "team2_gets": list(self.team2_gets),
            "team1_adds": list(self.team1_adds),
            "team2_adds": list(self.team2_adds),
        }

    @classmethod
    def from_record(cls, value: object) -> "AnalyzerTradeSpec":
        fields = {"team2_id", "team1_gets", "team2_gets", "team1_adds", "team2_adds"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("analyzer trade fields do not match the schema")
        for field in fields - {"team2_id"}:
            if not isinstance(value[field], list):
                raise ValueError("analyzer trade player selections must be lists")
        return cls(**{field: value[field] for field in fields})


@dataclass(frozen=True, slots=True)
class ProjectionTableSpec:
    """Provider projection/stat table dimensions that must be proven on-page."""

    horizon: RankingHorizon | str
    scoring: str
    position_scope: tuple[str, ...]

    def __init__(
        self,
        horizon: RankingHorizon | str,
        scoring: object,
        position_scope: Iterable[object],
    ) -> None:
        try:
            normalized_horizon = RankingHorizon(horizon)
        except (TypeError, ValueError):
            raise ValueError("horizon must be weekly or ros") from None
        normalized_scoring = _scoring(scoring)
        positions = _positions(position_scope)
        object.__setattr__(self, "horizon", normalized_horizon)
        object.__setattr__(self, "scoring", normalized_scoring)
        object.__setattr__(self, "position_scope", positions)

    def to_record(self) -> dict[str, object]:
        return {
            "horizon": self.horizon.value,
            "scoring": self.scoring,
            "position_scope": list(self.position_scope),
        }

    @classmethod
    def from_record(cls, value: object) -> "ProjectionTableSpec":
        fields = {"horizon", "scoring", "position_scope"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("projection table fields do not match the schema")
        if not isinstance(value["position_scope"], list):
            raise ValueError("projection position_scope must be a list")
        return cls(value["horizon"], value["scoring"], value["position_scope"])


def _decimal_id(name: str, value: object) -> str:
    if type(value) is int:
        value = str(value)
    text = require_text(name, value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", text):
        raise ValueError(f"{name} must be a positive decimal provider ID")
    return text


def _id_set(name: str, values: Iterable[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of provider IDs")
    try:
        normalized = tuple(_decimal_id(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable of provider IDs") from None
    if len(normalized) != len(set(normalized)) or len(normalized) > 64:
        raise ValueError(f"{name} must contain at most 64 unique provider IDs")
    return tuple(sorted(normalized, key=int))


def _positions(values: Iterable[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("position_scope must be an iterable of positions")
    try:
        positions = tuple(require_text("position_scope", value).upper() for value in values)
    except TypeError:
        raise ValueError("position_scope must be an iterable of positions") from None
    allowed = {"ALL", "QB", "RB", "WR", "TE", "K", "DST", "DL", "LB", "DB", "IDP", "FLX"}
    if not positions or len(positions) != len(set(positions)) or not set(positions) <= allowed:
        raise ValueError("position_scope must contain unique supported positions")
    if "ALL" in positions and len(positions) != 1:
        raise ValueError("ALL cannot be combined with individual positions")
    return tuple(sorted(positions))


def _scoring(value: object) -> str:
    text = require_text("scoring", value).upper().replace("_", " ").replace("-", " ")
    aliases = {
        "STD": "STD", "STANDARD": "STD", "PPR": "PPR",
        "HALF": "HALF", "HALF PPR": "HALF",
    }
    if text not in aliases:
        raise ValueError("scoring must be STD, HALF, or PPR")
    return aliases[text]


__all__ = ("AnalyzerTradeSpec", "ProjectionTableSpec", "RankingHorizon")
