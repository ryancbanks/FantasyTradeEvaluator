"""Content-addressed, provenance-preserving weekly waiver candidates."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from ._scenario_random import content_id
from .positions import (
    CANONICAL_PLAYER_POSITIONS,
    normalize_lineup_slot,
    normalize_player_position,
)


WAIVER_POOL_SELECTION_ALGORITHM = "fantasypros-best-plus-ecr-position-fill-v1"


class WaiverPoolSource(str, Enum):
    FANTASYPROS_BEST = "fantasypros_best"
    ECR_AUGMENTATION = "ecr_augmentation"


@dataclass(frozen=True, slots=True)
class WaiverCandidate:
    canonical_player_id: str
    fantasypros_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    eligible_slots: tuple[str, ...]
    rest_of_season_ecr_rank: int

    def __post_init__(self) -> None:
        canonical_player_id = _text("canonical_player_id", self.canonical_player_id)
        _provider_id(self.fantasypros_player_id)
        display_name = _text("display_name", self.display_name)
        position = normalize_player_position(self.position, require_supported=True)
        nfl_team_id = _text("nfl_team_id", self.nfl_team_id).upper()
        slots = _slots(self.eligible_slots)
        if position not in slots:
            raise ValueError("waiver candidate eligibility must include its primary position")
        rank = _positive_int("rest_of_season_ecr_rank", self.rest_of_season_ecr_rank)
        object.__setattr__(self, "canonical_player_id", canonical_player_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "nfl_team_id", nfl_team_id)
        object.__setattr__(self, "eligible_slots", slots)
        object.__setattr__(self, "rest_of_season_ecr_rank", rank)


@dataclass(frozen=True, slots=True)
class WaiverPoolPlayer:
    canonical_player_id: str
    fantasypros_player_id: str
    display_name: str
    position: str
    nfl_team_id: str
    eligible_slots: tuple[str, ...]
    rest_of_season_ecr_rank: int
    source: WaiverPoolSource
    source_order: int

    def __post_init__(self) -> None:
        candidate = WaiverCandidate(
            self.canonical_player_id,
            self.fantasypros_player_id,
            self.display_name,
            self.position,
            self.nfl_team_id,
            self.eligible_slots,
            self.rest_of_season_ecr_rank,
        )
        if not isinstance(self.source, WaiverPoolSource):
            raise ValueError("waiver player source must be a WaiverPoolSource")
        order = _positive_int("source_order", self.source_order)
        for name in (
            "canonical_player_id",
            "fantasypros_player_id",
            "display_name",
            "position",
            "nfl_team_id",
            "eligible_slots",
            "rest_of_season_ecr_rank",
        ):
            object.__setattr__(self, name, getattr(candidate, name))
        object.__setattr__(self, "source_order", order)

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "display_name": self.display_name,
            "eligible_slots": list(self.eligible_slots),
            "fantasypros_player_id": self.fantasypros_player_id,
            "nfl_team_id": self.nfl_team_id,
            "position": self.position,
            "rest_of_season_ecr_rank": self.rest_of_season_ecr_rank,
            "source": self.source.value,
            "source_order": self.source_order,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "WaiverPoolPlayer":
        fields = {
            "canonical_player_id",
            "display_name",
            "eligible_slots",
            "fantasypros_player_id",
            "nfl_team_id",
            "position",
            "rest_of_season_ecr_rank",
            "source",
            "source_order",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("waiver pool player fields are invalid")
        if not isinstance(record["eligible_slots"], list):
            raise ValueError("waiver pool eligible_slots must be a JSON array")
        try:
            source = WaiverPoolSource(record["source"])
        except (TypeError, ValueError):
            raise ValueError("waiver pool player source is invalid") from None
        return cls(
            canonical_player_id=record["canonical_player_id"],
            fantasypros_player_id=record["fantasypros_player_id"],
            display_name=record["display_name"],
            position=record["position"],
            nfl_team_id=record["nfl_team_id"],
            eligible_slots=tuple(record["eligible_slots"]),
            rest_of_season_ecr_rank=record["rest_of_season_ecr_rank"],
            source=source,
            source_order=record["source_order"],
        )


@dataclass(frozen=True, slots=True)
class WaiverPool:
    snapshot_id: str
    scoring_profile_id: str
    required_positions: tuple[str, ...]
    minimum_pool_size: int
    players: tuple[WaiverPoolPlayer, ...]
    selection_algorithm: str = WAIVER_POOL_SELECTION_ALGORITHM
    waiver_pool_id: str = field(init=False)

    def __post_init__(self) -> None:
        _text("snapshot_id", self.snapshot_id)
        _text("scoring_profile_id", self.scoring_profile_id)
        required = tuple(
            sorted(
                {
                    normalize_player_position(position, require_supported=True)
                    for position in self.required_positions
                }
            )
        )
        if not required:
            raise ValueError("required_positions cannot be empty")
        minimum = _positive_int("minimum_pool_size", self.minimum_pool_size)
        rows = tuple(self.players)
        if not rows or any(not isinstance(row, WaiverPoolPlayer) for row in rows):
            raise ValueError("players must contain WaiverPoolPlayer values")
        canonical = tuple(row.canonical_player_id for row in rows)
        provider = tuple(row.fantasypros_player_id for row in rows)
        if len(set(canonical)) != len(rows) or len(set(provider)) != len(rows):
            raise ValueError("waiver pool players must have unique identities")
        if len(rows) < minimum:
            raise ValueError("waiver pool is too small to fill a complete active roster")
        if len(rows) > _maximum_pool_size(minimum):
            raise ValueError("waiver pool exceeds its deterministic bound")
        if not set(required).issubset(row.position for row in rows):
            raise ValueError("waiver pool does not cover every required position")
        by_source = {
            source: tuple(row.source_order for row in rows if row.source is source)
            for source in WaiverPoolSource
        }
        if not by_source[WaiverPoolSource.FANTASYPROS_BEST]:
            raise ValueError("waiver pool requires captured FantasyPros best free agents")
        if any(
            orders != tuple(range(1, len(orders) + 1))
            for orders in by_source.values()
            if orders
        ):
            raise ValueError("waiver pool source_order values must be contiguous")
        expected_source_order = tuple(
            (source, order)
            for source in WaiverPoolSource
            for order in range(1, len(by_source[source]) + 1)
        )
        if tuple((row.source, row.source_order) for row in rows) != expected_source_order:
            raise ValueError(
                "waiver pool players must keep captured and augmented source order"
            )
        if self.selection_algorithm != WAIVER_POOL_SELECTION_ALGORITHM:
            raise ValueError("waiver pool selection_algorithm is unsupported")
        object.__setattr__(self, "required_positions", required)
        object.__setattr__(self, "minimum_pool_size", minimum)
        object.__setattr__(self, "players", rows)
        object.__setattr__(
            self,
            "waiver_pool_id",
            content_id("waiver-pool", self._content_record()),
        )

    @property
    def player_ids(self) -> tuple[str, ...]:
        return tuple(row.canonical_player_id for row in self.players)

    @property
    def fantasypros_best_player_ids(self) -> tuple[str, ...]:
        """Return the exact provider IDs captured from analyzer initialization."""

        return tuple(
            row.fantasypros_player_id
            for row in self.players
            if row.source is WaiverPoolSource.FANTASYPROS_BEST
        )

    @property
    def fantasypros_best_canonical_player_ids(self) -> tuple[str, ...]:
        return tuple(
            row.canonical_player_id
            for row in self.players
            if row.source is WaiverPoolSource.FANTASYPROS_BEST
        )

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
            "kind": "weekly_waiver_pool",
            "schema_version": 1,
            **self._content_record(),
            "waiver_pool_id": self.waiver_pool_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "WaiverPool":
        content = {
            "minimum_pool_size",
            "players",
            "required_positions",
            "scoring_profile_id",
            "selection_algorithm",
            "snapshot_id",
        }
        if not isinstance(record, Mapping) or set(record) != content | {
            "kind",
            "schema_version",
            "waiver_pool_id",
        }:
            raise ValueError("waiver pool record fields are invalid")
        if record["kind"] != "weekly_waiver_pool" or record["schema_version"] != 1:
            raise ValueError("waiver pool record kind or schema version is invalid")
        if not isinstance(record["players"], list) or not isinstance(
            record["required_positions"], list
        ):
            raise ValueError("waiver pool players and required_positions must be arrays")
        pool = cls(
            snapshot_id=record["snapshot_id"],
            scoring_profile_id=record["scoring_profile_id"],
            required_positions=tuple(record["required_positions"]),
            minimum_pool_size=record["minimum_pool_size"],
            players=tuple(WaiverPoolPlayer.from_record(row) for row in record["players"]),
            selection_algorithm=record["selection_algorithm"],
        )
        if record["waiver_pool_id"] != pool.waiver_pool_id:
            raise ValueError("waiver pool content does not match waiver_pool_id")
        return pool


def select_waiver_pool(
    *,
    snapshot_id: str,
    scoring_profile_id: str,
    candidates: Iterable[WaiverCandidate],
    fantasypros_best_player_ids: Iterable[str],
    required_positions: Iterable[str],
    minimum_pool_size: int,
) -> WaiverPool:
    """Keep exact analyzer best players, then fill missing capacity by ROS ECR."""

    rows = tuple(candidates)
    if not rows or any(not isinstance(row, WaiverCandidate) for row in rows):
        raise ValueError("candidates must contain WaiverCandidate values")
    canonical = tuple(row.canonical_player_id for row in rows)
    provider = tuple(row.fantasypros_player_id for row in rows)
    if len(set(canonical)) != len(rows) or len(set(provider)) != len(rows):
        raise ValueError("waiver candidates must have unique identities")
    if isinstance(fantasypros_best_player_ids, (str, bytes)):
        raise ValueError("fantasypros_best_player_ids must be an iterable of IDs")
    try:
        best_ids = tuple(fantasypros_best_player_ids)
    except TypeError:
        raise ValueError(
            "fantasypros_best_player_ids must be an iterable of IDs"
        ) from None
    if (
        not best_ids
        or any(not _is_provider_id(value) for value in best_ids)
        or len(set(best_ids)) != len(best_ids)
    ):
        raise ValueError(
            "fantasypros_best_player_ids must be unique positive decimal IDs"
        )
    required = tuple(
        sorted(
            {
                normalize_player_position(position, require_supported=True)
                for position in required_positions
            }
        )
    )
    if not required:
        raise ValueError("required_positions cannot be empty")
    minimum = _positive_int("minimum_pool_size", minimum_pool_size)
    maximum = _maximum_pool_size(minimum)
    if len(best_ids) > maximum:
        raise ValueError("captured FantasyPros best-free-agent pool exceeds its bound")

    by_provider = {row.fantasypros_player_id: row for row in rows}
    selected: list[tuple[WaiverCandidate, WaiverPoolSource, int]] = []
    selected_ids = set()
    for order, provider_id in enumerate(best_ids, 1):
        candidate = by_provider.get(provider_id)
        if candidate is None:
            raise ValueError(
                f"captured FantasyPros best free agent {provider_id!r} lacks complete "
                "ECR, projection, identity, or schedule evidence"
            )
        selected.append((candidate, WaiverPoolSource.FANTASYPROS_BEST, order))
        selected_ids.add(candidate.canonical_player_id)

    ranked = tuple(
        sorted(
            rows,
            key=lambda row: (
                row.rest_of_season_ecr_rank,
                row.fantasypros_player_id,
                row.canonical_player_id,
            ),
        )
    )
    augmentation_order = 0

    def add(candidate: WaiverCandidate) -> None:
        nonlocal augmentation_order
        if candidate.canonical_player_id in selected_ids:
            return
        augmentation_order += 1
        selected.append(
            (candidate, WaiverPoolSource.ECR_AUGMENTATION, augmentation_order)
        )
        selected_ids.add(candidate.canonical_player_id)

    covered = {row.position for row, _, _ in selected}
    position_fill = []
    for position in required:
        if position in covered:
            continue
        candidate = next((row for row in ranked if row.position == position), None)
        if candidate is None:
            raise ValueError(f"waiver pool cannot cover required position {position!r}")
        position_fill.append(candidate)
    for candidate in sorted(
        position_fill,
        key=lambda row: (
            row.rest_of_season_ecr_rank,
            row.fantasypros_player_id,
            row.canonical_player_id,
        ),
    ):
        add(candidate)
        covered.add(candidate.position)

    target = max(minimum, len(required), len(best_ids))
    for candidate in ranked:
        if len(selected) >= target:
            break
        add(candidate)
    if len(selected) < target:
        raise ValueError("waiver candidates cannot fill a complete active roster")
    if len(selected) > maximum:
        raise ValueError("selected waiver pool exceeds its deterministic bound")

    players = tuple(
        WaiverPoolPlayer(
            candidate.canonical_player_id,
            candidate.fantasypros_player_id,
            candidate.display_name,
            candidate.position,
            candidate.nfl_team_id,
            candidate.eligible_slots,
            candidate.rest_of_season_ecr_rank,
            source,
            order,
        )
        for candidate, source, order in selected
    )
    return WaiverPool(
        snapshot_id,
        scoring_profile_id,
        required,
        minimum,
        players,
    )


def required_waiver_positions(
    starting_lineup_slots: Iterable[str],
) -> tuple[str, ...]:
    """Return every primary position that can fill a captured starting slot."""

    slots = _slots(starting_lineup_slots)
    required = tuple(
        sorted(
            position
            for position in CANONICAL_PLAYER_POSITIONS
            if position != "IDP"
            if set(waiver_eligible_slots(position, slots)).intersection(slots)
        )
    )
    if not required:
        raise ValueError("no supported player position can fill a starting slot")
    return required


def waiver_eligible_slots(
    position: str,
    starting_lineup_slots: Iterable[str],
) -> tuple[str, ...]:
    """Derive NFL lineup eligibility from primary position and captured league slots."""

    primary = normalize_player_position(position, require_supported=True)
    starting = set(_slots(starting_lineup_slots))
    eligible = {primary}
    composite_slots = {
        "RB": {"RB_WR", "FLEX"},
        "WR": {"RB_WR", "WR_TE", "FLEX"},
        "TE": {"WR_TE", "FLEX"},
    }
    eligible.update(starting.intersection(composite_slots.get(primary, set())))
    if primary in {"QB", "RB", "WR", "TE"}:
        eligible.update(starting.intersection({"SFLX", "OP", "UTIL"}))
    if primary in {"DL", "LB", "DB"} and "IDP" in starting:
        eligible.add("IDP")
    return tuple(sorted(eligible))


def _slots(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("eligible_slots must be an iterable")
    try:
        result = tuple(sorted({normalize_lineup_slot(value) for value in values}))
    except TypeError:
        raise ValueError("eligible_slots must be an iterable") from None
    if not result:
        raise ValueError("eligible_slots cannot be empty")
    return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _provider_id(value: object) -> None:
    if not _is_provider_id(value):
        raise ValueError("fantasypros_player_id must be a positive decimal ID")


def _is_provider_id(value: object) -> bool:
    return isinstance(value, str) and value.isdigit() and value != "0"


def _maximum_pool_size(minimum_pool_size: int) -> int:
    return max(16, 4 * minimum_pool_size)


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


__all__ = (
    "WAIVER_POOL_SELECTION_ALGORITHM",
    "WaiverCandidate",
    "WaiverPool",
    "WaiverPoolPlayer",
    "WaiverPoolSource",
    "required_waiver_positions",
    "select_waiver_pool",
    "waiver_eligible_slots",
)
