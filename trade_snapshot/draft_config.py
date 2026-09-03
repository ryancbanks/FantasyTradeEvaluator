"""Validated, content-addressed league rules for historical draft arenas."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import math
from numbers import Real
import re
from types import MappingProxyType

from ._scenario_random import content_id
from .engine_bundle import EngineBundle
from .positions import CANONICAL_PLAYER_POSITIONS, normalize_lineup_slot, normalize_player_position
from .scoring import ScoringProfile


class DraftStrategy(str, Enum):
    NONE = "none"
    STREAMING_QB = "streaming_qb"
    STREAMING_TE = "streaming_te"
    STREAMING_DST = "streaming_dst"
    LATE_ROUND_QB = "late_round_qb"

    def allows(self, position: str, round_number: int, total_rounds: int) -> bool:
        position = normalize_player_position(position, require_supported=True)
        if type(round_number) is not int or type(total_rounds) is not int:
            raise ValueError("round numbers must be integers")
        if not 1 <= round_number <= total_rounds:
            raise ValueError("round_number must be inside the draft")
        if self is self.STREAMING_QB and position == "QB":
            return round_number > total_rounds - 3
        if self is self.STREAMING_TE and position == "TE":
            return round_number > total_rounds - 3
        if self is self.STREAMING_DST and position == "DST":
            return round_number == total_rounds
        if self is self.LATE_ROUND_QB and position == "QB":
            return round_number > 9
        return True


_DEFAULT_SLOT_ELIGIBILITY = {
    "QB": ("QB",),
    "RB": ("RB",),
    "WR": ("WR",),
    "TE": ("TE",),
    "K": ("K",),
    "DST": ("DST",),
    "DL": ("DL",),
    "LB": ("LB",),
    "DB": ("DB",),
    "IDP": ("DL", "LB", "DB", "IDP"),
    "FLEX": ("RB", "WR", "TE"),
    "SFLX": ("QB", "RB", "WR", "TE"),
    "OP": ("QB", "RB", "WR", "TE"),
    "UTIL": tuple(sorted(CANONICAL_PLAYER_POSITIONS)),
}
_LINEAR_STAT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class DraftLeagueConfig:
    name: str
    team_count: int
    starting_slots: tuple[str, ...]
    bench_slots: int
    slot_eligibility: Mapping[str, tuple[str, ...]]
    position_limits: Mapping[str, int]
    scoring_weights: Mapping[str, float]
    regular_season_weeks: tuple[int, ...]
    playoff_team_count: int
    playoff_weeks: tuple[int, ...]
    strategy_counts: Mapping[DraftStrategy, int]
    config_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text("config name", self.name)
        _integer("team_count", self.team_count, 2, 32)
        _integer("bench_slots", self.bench_slots, 0, 40)
        slots = tuple(normalize_lineup_slot(slot) for slot in self.starting_slots)
        if not slots or len(slots) > 16 or len(slots) + self.bench_slots > 60:
            raise ValueError("starting and bench slot count is invalid")
        eligibility = _eligibility_map(self.slot_eligibility, slots)
        limits = _position_limits(self.position_limits, len(slots) + self.bench_slots)
        _validate_slot_limit_feasibility(
            slots, eligibility, limits, len(slots) + self.bench_slots
        )
        weights = _number_map("scoring_weights", self.scoring_weights)
        if not weights or not any(weights.values()):
            raise ValueError("scoring_weights must contain at least one non-zero rule")
        regular = _weeks("regular_season_weeks", self.regular_season_weeks)
        playoffs = _weeks("playoff_weeks", self.playoff_weeks)
        if regular[-1] >= playoffs[0]:
            raise ValueError("playoff weeks must begin after the regular season")
        _integer("playoff_team_count", self.playoff_team_count, 1, self.team_count)
        required_playoff_weeks = max(1, math.ceil(math.log2(self.playoff_team_count)))
        if len(playoffs) != required_playoff_weeks:
            raise ValueError(
                "playoff_weeks must contain exactly one week per bracket round"
            )
        strategies = _strategies(self.strategy_counts, self.team_count)
        object.__setattr__(self, "starting_slots", slots)
        object.__setattr__(self, "slot_eligibility", eligibility)
        object.__setattr__(self, "position_limits", limits)
        object.__setattr__(self, "scoring_weights", weights)
        object.__setattr__(self, "regular_season_weeks", regular)
        object.__setattr__(self, "playoff_weeks", playoffs)
        object.__setattr__(self, "strategy_counts", strategies)
        object.__setattr__(self, "config_id", content_id("draft_league", self._content_record()))

    @property
    def roster_size(self) -> int:
        return len(self.starting_slots) + self.bench_slots

    @property
    def total_rounds(self) -> int:
        return self.roster_size

    def strategy_seats(self) -> tuple[DraftStrategy, ...]:
        return tuple(
            strategy
            for strategy in DraftStrategy
            for _ in range(self.strategy_counts.get(strategy, 0))
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "team_count": self.team_count,
            "starting_slots": list(self.starting_slots),
            "bench_slots": self.bench_slots,
            "slot_eligibility": {key: list(value) for key, value in self.slot_eligibility.items()},
            "position_limits": dict(self.position_limits),
            "scoring_weights": dict(self.scoring_weights),
            "regular_season_weeks": list(self.regular_season_weeks),
            "playoff_team_count": self.playoff_team_count,
            "playoff_weeks": list(self.playoff_weeks),
            "strategy_counts": {
                strategy.value: self.strategy_counts.get(strategy, 0)
                for strategy in DraftStrategy
            },
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "draft_league_config",
            "schema_version": 1,
            **self._content_record(),
            "config_id": self.config_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "DraftLeagueConfig":
        content = {
            "name", "team_count", "starting_slots", "bench_slots", "slot_eligibility",
            "position_limits", "scoring_weights", "regular_season_weeks",
            "playoff_team_count", "playoff_weeks", "strategy_counts",
        }
        if not isinstance(record, Mapping) or set(record) != content | {
            "kind", "schema_version", "config_id"
        }:
            raise ValueError("draft league config fields are invalid")
        if record["kind"] != "draft_league_config" or record["schema_version"] != 1:
            raise ValueError("draft league config kind or schema version is invalid")
        raw_strategies = _mapping("strategy_counts", record["strategy_counts"])
        if set(raw_strategies) != {row.value for row in DraftStrategy}:
            raise ValueError("strategy_counts fields are invalid")
        try:
            strategies = {DraftStrategy(key): value for key, value in raw_strategies.items()}
        except ValueError:
            raise ValueError("strategy_counts contains an unknown strategy") from None
        config = cls(
            name=record["name"], team_count=record["team_count"],
            starting_slots=tuple(_array("starting_slots", record["starting_slots"])),
            bench_slots=record["bench_slots"],
            slot_eligibility={
                key: tuple(_array(f"slot_eligibility.{key}", value))
                for key, value in _mapping("slot_eligibility", record["slot_eligibility"]).items()
            },
            position_limits=_mapping("position_limits", record["position_limits"]),
            scoring_weights=_mapping("scoring_weights", record["scoring_weights"]),
            regular_season_weeks=tuple(_array("regular_season_weeks", record["regular_season_weeks"])),
            playoff_team_count=record["playoff_team_count"],
            playoff_weeks=tuple(_array("playoff_weeks", record["playoff_weeks"])),
            strategy_counts=strategies,
        )
        if record["config_id"] != config.config_id:
            raise ValueError("draft league config content does not match config_id")
        return config

    @classmethod
    def standard_ppr(cls, *, team_count: int = 12, name: str = "12-team PPR") -> "DraftLeagueConfig":
        starters = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "DST", "K")
        playoff_team_count = min(6, team_count)
        playoff_rounds = max(1, math.ceil(math.log2(playoff_team_count)))
        return cls(
            name=name, team_count=team_count, starting_slots=starters, bench_slots=7,
            slot_eligibility=default_slot_eligibility(starters),
            position_limits={"QB": 3, "RB": 6, "WR": 6, "TE": 3, "DST": 2, "K": 2},
            scoring_weights={
                "passing_yards": 0.04, "passing_tds": 4.0, "interceptions": -2.0,
                "rushing_yards": 0.1, "rushing_tds": 6.0,
                "receiving_yards": 0.1, "receiving_tds": 6.0, "receptions": 1.0,
                "fumbles_lost": -2.0, "field_goals": 3.0, "extra_points": 1.0,
                "dst_sacks": 1.0, "dst_interceptions": 2.0,
                "dst_fumble_recoveries": 2.0, "dst_touchdowns": 6.0,
                "dst_safeties": 2.0, "dst_points_allowed_0": 10.0,
            },
            regular_season_weeks=tuple(range(1, 15)),
            playoff_team_count=playoff_team_count,
            playoff_weeks=tuple(range(18 - playoff_rounds, 18)),
            strategy_counts={DraftStrategy.NONE: team_count},
        )


def default_slot_eligibility(slots: tuple[str, ...] | list[str]) -> dict[str, tuple[str, ...]]:
    normalized = {normalize_lineup_slot(slot) for slot in slots}
    try:
        return {slot: _DEFAULT_SLOT_ELIGIBILITY[slot] for slot in sorted(normalized)}
    except KeyError as error:
        raise ValueError(f"slot {error.args[0]!r} needs explicit eligibility") from None


def config_from_engine_bundle(bundle: EngineBundle) -> DraftLeagueConfig:
    """Create an editable draft preset from a synced league engine bundle."""

    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    state = bundle.state
    slots = tuple(normalize_lineup_slot(slot) for slot in state.roster_rules.starting_lineup_slots)
    scoring = scoring_weights_from_profile(bundle.scoring_profile)
    if not scoring:
        raise ValueError("the synced league does not expose supported linear scoring weights")
    return DraftLeagueConfig(
        name=f"Synced {bundle.scoring_profile.platform} league · {state.season}",
        team_count=len(state.teams), starting_slots=slots,
        bench_slots=state.roster_rules.roster_cap - len(slots),
        slot_eligibility=default_slot_eligibility(slots), position_limits={},
        scoring_weights=scoring,
        regular_season_weeks=tuple(range(1, state.playoff_rules.regular_season_end_week + 1)),
        playoff_team_count=state.playoff_rules.qualifier_count,
        playoff_weeks=state.playoff_rules.playoff_weeks,
        strategy_counts={DraftStrategy.NONE: len(state.teams)},
    )


def score_raw_stats(stats: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Apply explicit stat weights; unknown actual fields contribute zero."""

    if not isinstance(stats, Mapping) or not isinstance(weights, Mapping):
        raise ValueError("stats and weights must be mappings")
    total = math.fsum(float(stats.get(name, 0.0)) * weight for name, weight in weights.items())
    if not math.isfinite(total):
        raise ValueError("scored fantasy points are not finite")
    return total


def scoring_weights_from_profile(profile: ScoringProfile) -> dict[str, float]:
    """Return only true linear player-stat rules from a captured profile.

    ESPN's portable profile keeps scoring rules in a ``scoringItems`` array;
    recursively flattening the surrounding settings would silently turn metadata
    such as home-team bonuses into player-stat weights. ESPN stat identifiers are
    retained verbatim so an imported historical pack can carry the exact matching
    ``espn_stat_<id>`` actual fields without a brittle translation table.
    """

    if not isinstance(profile, ScoringProfile):
        raise ValueError("profile must be a ScoringProfile")
    settings = profile.settings
    if profile.platform == "espn" and isinstance(settings.get("scoring_settings"), Mapping):
        items = settings["scoring_settings"].get("scoringItems")
        if not isinstance(items, (list, tuple)) or not items:
            return {}
        result: dict[str, float] = {}
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("ESPN scoringItems must contain objects")
            stat_id = item.get("statId")
            points = item.get("points")
            unknown = set(item).difference(
                {"statId", "points", "pointsOverrides", "isReverseItem"}
            )
            if unknown:
                raise ValueError(
                    f"ESPN scoring item field {min(map(str, unknown))!r} is unsupported"
                )
            if type(stat_id) is not int or stat_id < 0:
                raise ValueError("ESPN scoring item statId is invalid")
            if isinstance(points, bool) or not isinstance(points, Real) or not math.isfinite(float(points)):
                raise ValueError("ESPN scoring item points are invalid")
            overrides = item.get("pointsOverrides")
            if overrides not in (None, {}, []):
                raise ValueError(
                    "ESPN position-specific scoring overrides need manual Draft Lab scoring"
                )
            if item.get("isReverseItem") not in (None, False):
                raise ValueError(
                    "ESPN reverse or threshold scoring needs manual Draft Lab scoring"
                )
            key = f"espn_stat_{stat_id}"
            if key in result:
                raise ValueError("ESPN scoringItems contain a duplicate statId")
            result[key] = float(points)
        return dict(sorted(result.items()))
    normalized = settings.get("normalized_linear_stat_weights")
    version = settings.get("normalized_linear_stat_weights_version")
    if version != 1 or not isinstance(normalized, Mapping) or not normalized:
        return {}
    if any(
        not isinstance(key, str)
        or not _LINEAR_STAT_NAME.fullmatch(key)
        or isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        for key, value in normalized.items()
    ):
        raise ValueError("normalized linear scoring weights are invalid")
    return {str(key): float(value) for key, value in sorted(normalized.items())}


def _eligibility_map(value: object, slots: tuple[str, ...]) -> MappingProxyType:
    raw = _mapping("slot_eligibility", value)
    required = set(slots)
    if set(raw) != required:
        raise ValueError("slot_eligibility must define every configured starting slot exactly")
    result = {}
    for slot, positions in raw.items():
        normalized_slot = normalize_lineup_slot(slot)
        if normalized_slot != slot:
            raise ValueError("slot_eligibility keys must already be normalized")
        if isinstance(positions, (str, bytes)):
            raise ValueError("slot eligible positions must be a sequence")
        normalized = tuple(sorted({
            normalize_player_position(position, require_supported=True)
            for position in positions
        }))
        if not normalized:
            raise ValueError("every slot needs at least one eligible position")
        result[slot] = normalized
    return MappingProxyType(dict(sorted(result.items())))


def _position_limits(value: object, roster_size: int) -> MappingProxyType:
    raw = _mapping("position_limits", value)
    result = {}
    for position, maximum in raw.items():
        normalized = normalize_player_position(position, require_supported=True)
        if normalized != position:
            raise ValueError("position_limits keys must already be normalized")
        _integer(f"position limit {position}", maximum, 1, roster_size)
        result[position] = maximum
    return MappingProxyType(dict(sorted(result.items())))


def _validate_slot_limit_feasibility(slots, eligibility, limits, roster_size) -> None:
    """Reject position caps that make a legal lineup or roster impossible."""

    effective_capacity = sum(
        limits.get(position, roster_size)
        for position in CANONICAL_PLAYER_POSITIONS
    )
    if effective_capacity < roster_size:
        raise ValueError("position_limits cannot fill a complete configured roster")

    position_copies = tuple(
        position
        for position in sorted(CANONICAL_PLAYER_POSITIONS)
        for _ in range(min(limits.get(position, roster_size), len(slots)))
    )
    matched: dict[int, int] = {}

    def place(copy_index: int, visited: set[int]) -> bool:
        position = position_copies[copy_index]
        for slot_index, slot in enumerate(slots):
            if slot_index in visited or position not in eligibility[slot]:
                continue
            visited.add(slot_index)
            incumbent = matched.get(slot_index)
            if incumbent is None or place(incumbent, visited):
                matched[slot_index] = copy_index
                return True
        return False

    for copy_index in range(len(position_copies)):
        place(copy_index, set())
    if len(matched) != len(slots):
        raise ValueError(
            "position_limits cannot fill every configured starting slot"
        )


def _strategies(value: object, team_count: int) -> MappingProxyType:
    if not isinstance(value, Mapping):
        raise ValueError("strategy_counts must be a mapping")
    result = {strategy: 0 for strategy in DraftStrategy}
    for key, count in value.items():
        if not isinstance(key, DraftStrategy):
            raise ValueError("strategy_counts keys must be DraftStrategy values")
        _integer(f"strategy count {key.value}", count, 0, team_count)
        result[key] = count
    if sum(result.values()) != team_count:
        raise ValueError("strategy counts must add up to team_count")
    return MappingProxyType(result)


def _number_map(name: str, value: object) -> MappingProxyType:
    raw = _mapping(name, value)
    result = {}
    for key, child in raw.items():
        _text(f"{name} key", key)
        if not _LINEAR_STAT_NAME.fullmatch(key):
            raise ValueError(f"{name} keys must be lowercase portable stat names")
        if isinstance(child, bool) or not isinstance(child, Real) or not math.isfinite(float(child)):
            raise ValueError(f"{name}.{key} must be a finite number")
        number = float(child)
        if abs(number) > 1e9:
            raise ValueError(f"{name}.{key} is outside the supported range")
        result[key] = 0.0 if number == 0 else number
    return MappingProxyType(dict(sorted(result.items())))


def _weeks(name: str, value: object) -> tuple[int, ...]:
    try:
        weeks = tuple(value)
    except TypeError:
        raise ValueError(f"{name} must be a sequence") from None
    if not weeks or any(type(week) is not int or not 1 <= week <= 25 for week in weeks):
        raise ValueError(f"{name} must contain NFL week numbers")
    if tuple(sorted(set(weeks))) != weeks:
        raise ValueError(f"{name} must be unique and increasing")
    return weeks


def _mapping(name: str, value: object) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _array(name: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(f"{name} must be non-empty text")


def _integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")


__all__ = (
    "DraftLeagueConfig", "DraftStrategy", "config_from_engine_bundle",
    "default_slot_eligibility", "score_raw_stats", "scoring_weights_from_profile",
)
