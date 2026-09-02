"""Provider-neutral free-agent evidence for the Independent Edition."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType

from ._scenario_random import content_id
from .positions import normalize_lineup_slot, normalize_player_position
from .projection_source_policy import INDEPENDENT_PROJECTION_PROVIDERS


INDEPENDENT_WAIVER_ALGORITHM = "independent-ensemble-ros-value-v2"


@dataclass(frozen=True, slots=True)
class IndependentWaiverCandidate:
    canonical_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    eligible_slots: tuple[str, ...]
    provider_player_ids: Mapping[str, str]
    projected_remaining_points: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_player_id", _text("canonical_player_id", self.canonical_player_id))
        object.__setattr__(self, "display_name", _text("display_name", self.display_name))
        position = normalize_player_position(self.position, require_supported=True)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "nfl_team_id", _text("nfl_team_id", self.nfl_team_id).upper())
        slots = _slots(self.eligible_slots)
        if position not in slots:
            raise ValueError("waiver candidate eligibility must include its position")
        object.__setattr__(self, "eligible_slots", slots)
        object.__setattr__(self, "provider_player_ids", _provider_ids(self.provider_player_ids))
        points = _number("projected_remaining_points", self.projected_remaining_points)
        if points < 0:
            raise ValueError("projected_remaining_points cannot be negative")
        object.__setattr__(self, "projected_remaining_points", points)


@dataclass(frozen=True, slots=True)
class IndependentWaiverPlayer:
    canonical_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    eligible_slots: tuple[str, ...]
    provider_player_ids: Mapping[str, str]
    projected_remaining_points: float
    source_order: int

    def __post_init__(self) -> None:
        candidate = IndependentWaiverCandidate(
            self.canonical_player_id,
            self.display_name,
            self.position,
            self.nfl_team_id,
            self.eligible_slots,
            self.provider_player_ids,
            self.projected_remaining_points,
        )
        for name in (
            "canonical_player_id", "display_name", "position", "nfl_team_id",
            "eligible_slots", "provider_player_ids", "projected_remaining_points",
        ):
            object.__setattr__(self, name, getattr(candidate, name))
        if type(self.source_order) is not int or self.source_order < 1:
            raise ValueError("source_order must be a positive integer")

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "display_name": self.display_name,
            "eligible_slots": list(self.eligible_slots),
            "nfl_team_id": self.nfl_team_id,
            "position": self.position,
            "projected_remaining_points": self.projected_remaining_points,
            "provider_player_ids": dict(self.provider_player_ids),
            "source_order": self.source_order,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "IndependentWaiverPlayer":
        fields = {
            "canonical_player_id", "display_name", "eligible_slots", "nfl_team_id",
            "position", "projected_remaining_points", "provider_player_ids", "source_order",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("independent waiver player fields are invalid")
        if not isinstance(record["eligible_slots"], list):
            raise ValueError("eligible_slots must be a JSON array")
        return cls(
            record["canonical_player_id"],
            record["display_name"],
            record["position"],
            record["nfl_team_id"],
            tuple(record["eligible_slots"]),
            record["provider_player_ids"],
            record["projected_remaining_points"],
            record["source_order"],
        )


@dataclass(frozen=True, slots=True)
class IndependentWaiverPool:
    snapshot_id: str
    scoring_profile_id: str
    required_positions: tuple[str, ...]
    minimum_pool_size: int
    players: tuple[IndependentWaiverPlayer, ...]
    selection_algorithm: str = INDEPENDENT_WAIVER_ALGORITHM
    waiver_pool_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _text("snapshot_id", self.snapshot_id))
        object.__setattr__(self, "scoring_profile_id", _text("scoring_profile_id", self.scoring_profile_id))
        required = tuple(sorted({normalize_player_position(value, require_supported=True) for value in self.required_positions}))
        if not required:
            raise ValueError("required_positions cannot be empty")
        if type(self.minimum_pool_size) is not int or self.minimum_pool_size < 1:
            raise ValueError("minimum_pool_size must be a positive integer")
        rows = tuple(self.players)
        if not rows or any(not isinstance(row, IndependentWaiverPlayer) for row in rows):
            raise ValueError("players must contain IndependentWaiverPlayer values")
        if len({row.canonical_player_id for row in rows}) != len(rows):
            raise ValueError("independent waiver players must be unique")
        if tuple(row.source_order for row in rows) != tuple(range(1, len(rows) + 1)):
            raise ValueError("independent waiver source_order must be contiguous")
        if len(rows) < self.minimum_pool_size:
            raise ValueError("independent waiver pool is too small")
        if len(rows) > max(16, 4 * self.minimum_pool_size):
            raise ValueError("independent waiver pool exceeds its deterministic bound")
        if not set(required).issubset(row.position for row in rows):
            raise ValueError("independent waiver pool does not cover each required position")
        if self.selection_algorithm != INDEPENDENT_WAIVER_ALGORITHM:
            raise ValueError("independent waiver selection algorithm is unsupported")
        object.__setattr__(self, "required_positions", required)
        object.__setattr__(self, "players", rows)
        object.__setattr__(self, "waiver_pool_id", content_id("independent-waiver", self._content_record()))

    @property
    def player_ids(self) -> tuple[str, ...]:
        return tuple(row.canonical_player_id for row in self.players)

    def _content_record(self) -> dict[str, object]:
        return {
            "minimum_pool_size": self.minimum_pool_size,
            "players": [row.to_record() for row in self.players],
            "required_positions": list(self.required_positions),
            "scoring_profile_id": self.scoring_profile_id,
            "selection_algorithm": self.selection_algorithm,
            "snapshot_id": self.snapshot_id,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "independent_weekly_waiver_pool",
            "schema_version": 1,
            **self._content_record(),
            "waiver_pool_id": self.waiver_pool_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "IndependentWaiverPool":
        content = {
            "minimum_pool_size", "players", "required_positions", "scoring_profile_id",
            "selection_algorithm", "snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != content | {"kind", "schema_version", "waiver_pool_id"}:
            raise ValueError("independent waiver pool record fields are invalid")
        if record["kind"] != "independent_weekly_waiver_pool" or record["schema_version"] != 1:
            raise ValueError("independent waiver pool schema is invalid")
        if not isinstance(record["players"], list) or not isinstance(record["required_positions"], list):
            raise ValueError("independent waiver pool arrays are invalid")
        pool = cls(
            record["snapshot_id"],
            record["scoring_profile_id"],
            tuple(record["required_positions"]),
            record["minimum_pool_size"],
            tuple(IndependentWaiverPlayer.from_record(row) for row in record["players"]),
            record["selection_algorithm"],
        )
        if record["waiver_pool_id"] != pool.waiver_pool_id:
            raise ValueError("independent waiver pool content does not match waiver_pool_id")
        return pool


def select_independent_waiver_pool(
    *,
    snapshot_id: str,
    scoring_profile_id: str,
    candidates: Iterable[IndependentWaiverCandidate],
    required_positions: Iterable[str],
    minimum_pool_size: int,
) -> IndependentWaiverPool:
    rows = tuple(candidates)
    if not rows or any(not isinstance(row, IndependentWaiverCandidate) for row in rows):
        raise ValueError("candidates must contain IndependentWaiverCandidate values")
    if len({row.canonical_player_id for row in rows}) != len(rows):
        raise ValueError("independent waiver candidates must be unique")
    required = tuple(sorted({normalize_player_position(value, require_supported=True) for value in required_positions}))
    if not required:
        raise ValueError("required_positions cannot be empty")
    if type(minimum_pool_size) is not int or minimum_pool_size < 1:
        raise ValueError("minimum_pool_size must be a positive integer")
    ranked = tuple(sorted(rows, key=lambda row: (-row.projected_remaining_points, row.canonical_player_id)))
    selected: list[IndependentWaiverCandidate] = []
    selected_ids: set[str] = set()

    def add(row: IndependentWaiverCandidate) -> None:
        if row.canonical_player_id not in selected_ids:
            selected.append(row)
            selected_ids.add(row.canonical_player_id)

    for position in required:
        candidate = next((row for row in ranked if row.position == position), None)
        if candidate is None:
            raise ValueError(f"independent waiver pool cannot cover {position!r}")
        add(candidate)
    target = max(minimum_pool_size, len(required))
    for row in ranked:
        if len(selected) >= target:
            break
        add(row)
    if len(selected) < target:
        raise ValueError("independent waiver candidates cannot fill the required pool")
    ordered = tuple(sorted(selected, key=lambda row: (-row.projected_remaining_points, row.canonical_player_id)))
    players = tuple(
        IndependentWaiverPlayer(
            row.canonical_player_id,
            row.display_name,
            row.position,
            row.nfl_team_id,
            row.eligible_slots,
            row.provider_player_ids,
            row.projected_remaining_points,
            index,
        )
        for index, row in enumerate(ordered, 1)
    )
    return IndependentWaiverPool(
        snapshot_id,
        scoring_profile_id,
        required,
        minimum_pool_size,
        players,
    )


def _provider_ids(value: object) -> Mapping[str, str]:
    if (
        not isinstance(value, Mapping)
        or "espn" not in value
        or not set(value) <= set(INDEPENDENT_PROJECTION_PROVIDERS)
    ):
        raise ValueError(
            "provider_player_ids must contain ESPN and only independent sources"
        )
    result = {
        provider: _text(f"{provider} player ID", player_id)
        for provider, player_id in value.items()
    }
    return MappingProxyType(dict(sorted(result.items())))


def _slots(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("eligible_slots must be an iterable")
    try:
        slots = tuple(sorted({normalize_lineup_slot(value) for value in values}))
    except TypeError:
        raise ValueError("eligible_slots must be an iterable") from None
    if not slots:
        raise ValueError("eligible_slots cannot be empty")
    return slots


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


__all__ = (
    "INDEPENDENT_WAIVER_ALGORITHM",
    "IndependentWaiverCandidate",
    "IndependentWaiverPlayer",
    "IndependentWaiverPool",
    "select_independent_waiver_pool",
)
