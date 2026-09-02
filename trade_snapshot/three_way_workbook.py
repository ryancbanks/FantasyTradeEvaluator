"""Validated, presentation-neutral rows for three-team trade exports."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from math import isfinite
from numbers import Real

from .three_way_search_records import ThreeWayQualifiedResult


@dataclass(frozen=True, slots=True)
class ThreeWayExportProvenance:
    """Canonical request and run evidence carried into a three-way workbook."""

    request_id: str
    search_run_id: str
    participant_team_ids: tuple[str, str, str]
    participant_team_names: tuple[str, str, str]
    total_candidate_count: int
    seed: int
    trade_constraints_json: str
    power_settings_json: str
    free_agent_allocation_policy: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text("request_id", self.request_id))
        object.__setattr__(
            self, "search_run_id", _text("search_run_id", self.search_run_id)
        )
        team_ids = _texts(
            "participant_team_ids", self.participant_team_ids, required=True, unique=True
        )
        team_names = _texts(
            "participant_team_names",
            self.participant_team_names,
            required=True,
            unique=False,
        )
        if len(team_ids) != 3 or len(team_names) != 3:
            raise ValueError("export provenance requires exactly three participant teams")
        if (
            isinstance(self.total_candidate_count, bool)
            or not isinstance(self.total_candidate_count, int)
            or self.total_candidate_count < 0
        ):
            raise ValueError("total_candidate_count must be a non-negative integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        for name in ("trade_constraints_json", "power_settings_json"):
            value = getattr(self, name)
            if not isinstance(value, str) or value != _canonical_json_object(name, value):
                raise ValueError(f"{name} must be a canonical JSON object")
        policy = self.free_agent_allocation_policy
        if policy is not None:
            policy = _text("free_agent_allocation_policy", policy)
        object.__setattr__(self, "participant_team_ids", team_ids)
        object.__setattr__(self, "participant_team_names", team_names)
        object.__setattr__(self, "free_agent_allocation_policy", policy)

    @classmethod
    def from_records(
        cls,
        *,
        request_id: str,
        search_run_id: str,
        participant_team_ids: Iterable[str],
        participant_team_names: Iterable[str],
        total_candidate_count: int,
        seed: int,
        trade_constraint_record: Mapping[str, object],
        power_settings_record: Mapping[str, object],
        free_agent_allocation_policy: str | None = None,
    ) -> "ThreeWayExportProvenance":
        return cls(
            request_id,
            search_run_id,
            tuple(participant_team_ids),
            tuple(participant_team_names),
            total_candidate_count,
            seed,
            _json_record("trade_constraint_record", trade_constraint_record),
            _json_record("power_settings_record", power_settings_record),
            free_agent_allocation_policy,
        )

    @property
    def trade_constraints_display(self) -> str:
        return _pretty_json(self.trade_constraints_json)

    @property
    def power_settings_display(self) -> str:
        return _pretty_json(self.power_settings_json)


@dataclass(frozen=True, slots=True)
class ThreeWayWorkbookTransfer:
    source_team_id: str
    source_team_name: str
    destination_team_id: str
    destination_team_name: str
    player_ids: tuple[str, ...]
    player_names: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "source_team_id",
            "source_team_name",
            "destination_team_id",
            "destination_team_name",
        ):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        if self.source_team_id == self.destination_team_id:
            raise ValueError("a workbook transfer must move between different teams")
        ids = _texts("player_ids", self.player_ids, required=True, unique=True)
        names = _texts("player_names", self.player_names, required=True, unique=False)
        if len(ids) != len(names):
            raise ValueError("player IDs and names must have equal lengths")
        object.__setattr__(self, "player_ids", ids)
        object.__setattr__(self, "player_names", names)

    @property
    def description(self) -> str:
        return (
            f"{self.source_team_name} → {self.destination_team_name}: "
            + "; ".join(self.player_names)
        )


@dataclass(frozen=True, slots=True)
class ThreeWayWorkbookTeamImpact:
    team_id: str
    team_name: str
    sent_player_ids: tuple[str, ...]
    sent_player_names: tuple[str, ...]
    received_player_ids: tuple[str, ...]
    received_player_names: tuple[str, ...]
    added_player_ids: tuple[str, ...]
    added_player_names: tuple[str, ...]
    dropped_player_ids: tuple[str, ...]
    dropped_player_names: tuple[str, ...]
    power_delta: float
    playoff_before: float
    playoff_after: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _text("team_id", self.team_id))
        object.__setattr__(self, "team_name", _text("team_name", self.team_name))
        for id_name, display_name, required in (
            ("sent_player_ids", "sent_player_names", True),
            ("received_player_ids", "received_player_names", True),
            ("added_player_ids", "added_player_names", False),
            ("dropped_player_ids", "dropped_player_names", False),
        ):
            ids = _texts(id_name, getattr(self, id_name), required=required, unique=True)
            names = _texts(
                display_name,
                getattr(self, display_name),
                required=required,
                unique=False,
            )
            if len(ids) != len(names):
                raise ValueError(f"{id_name} and {display_name} must have equal lengths")
            object.__setattr__(self, id_name, ids)
            object.__setattr__(self, display_name, names)
        object.__setattr__(self, "power_delta", _finite("power_delta", self.power_delta))
        for name in ("playoff_before", "playoff_after"):
            value = _finite(name, getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)

    @property
    def playoff_delta(self) -> float:
        return self.playoff_after - self.playoff_before


@dataclass(frozen=True, slots=True)
class ThreeWayWorkbookRow:
    candidate_index: int
    transfers: tuple[ThreeWayWorkbookTransfer, ...]
    team_impacts: tuple[ThreeWayWorkbookTeamImpact, ...]
    power_methodology_status: str

    def __post_init__(self) -> None:
        if isinstance(self.candidate_index, bool) or not isinstance(
            self.candidate_index, int
        ) or self.candidate_index < 0:
            raise ValueError("candidate_index must be a non-negative integer")
        transfers = tuple(self.transfers)
        impacts = tuple(self.team_impacts)
        if not transfers or any(
            not isinstance(row, ThreeWayWorkbookTransfer) for row in transfers
        ):
            raise ValueError("transfers must contain workbook transfer rows")
        if len(impacts) != 3 or any(
            not isinstance(row, ThreeWayWorkbookTeamImpact) for row in impacts
        ):
            raise ValueError("team_impacts must contain exactly three team impacts")
        if len({row.team_id for row in impacts}) != 3:
            raise ValueError("team_impacts contains a duplicate team")
        if self.power_methodology_status not in {
            "extrapolated",
            "surrogate_extrapolated",
        }:
            raise ValueError("three-team power must be labeled as extrapolated")
        object.__setattr__(self, "transfers", transfers)
        object.__setattr__(self, "team_impacts", impacts)

    @property
    def all_teams_gain(self) -> bool:
        return all(row.playoff_delta > 0 for row in self.team_impacts)

    @property
    def combined_playoff_delta(self) -> float:
        return sum(row.playoff_delta for row in self.team_impacts)


def three_way_workbook_rows(
    results: Iterable[ThreeWayQualifiedResult],
    team_names: Mapping[str, str],
    player_names: Mapping[str, str],
    power_engine_mode: str,
) -> tuple[ThreeWayWorkbookRow, ...]:
    """Resolve stored IDs and rank all-team gains before other survivors."""

    if not isinstance(team_names, Mapping) or not isinstance(player_names, Mapping):
        raise ValueError("team_names and player_names must be mappings")
    if power_engine_mode not in {"exact", "surrogate"}:
        raise ValueError("power_engine_mode must be exact or surrogate")
    status = (
        "extrapolated"
        if power_engine_mode == "exact"
        else "surrogate_extrapolated"
    )
    try:
        rows = tuple(
            ThreeWayWorkbookRow(
                result.candidate_index,
                tuple(
                    ThreeWayWorkbookTransfer(
                        transfer.source_team_id,
                        team_names[transfer.source_team_id],
                        transfer.destination_team_id,
                        team_names[transfer.destination_team_id],
                        transfer.player_ids,
                        tuple(player_names[player_id] for player_id in transfer.player_ids),
                    )
                    for transfer in result.transfers
                ),
                tuple(
                    ThreeWayWorkbookTeamImpact(
                        impact.team_id,
                        team_names[impact.team_id],
                        impact.sent_player_ids,
                        tuple(
                            player_names[player_id]
                            for player_id in impact.sent_player_ids
                        ),
                        impact.received_player_ids,
                        tuple(
                            player_names[player_id]
                            for player_id in impact.received_player_ids
                        ),
                        impact.added_player_ids,
                        tuple(
                            player_names[player_id]
                            for player_id in impact.added_player_ids
                        ),
                        impact.dropped_player_ids,
                        tuple(
                            player_names[player_id]
                            for player_id in impact.dropped_player_ids
                        ),
                        impact.display_power_delta,
                        impact.playoff_before / 100,
                        impact.playoff_after / 100,
                    )
                    for impact in result.team_results
                ),
                status,
            )
            for result in results
        )
    except KeyError as error:
        raise ValueError(f"missing display name for ID {error.args[0]!r}") from None
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                not row.all_teams_gain,
                -row.combined_playoff_delta,
                row.candidate_index,
            ),
        )
    )


def _texts(name, values, *, required, unique):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        result = tuple(_text(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if required and not result:
        raise ValueError(f"{name} cannot be empty")
    if unique and len(set(result)) != len(result):
        raise ValueError(f"{name} cannot contain duplicates")
    return result


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _finite(name, value):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _json_record(name, value) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError(f"{name} must contain finite JSON values") from None


def _canonical_json_object(name, value) -> str:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(f"{name} must be a canonical JSON object") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a canonical JSON object")
    return _json_record(name, parsed)


def _pretty_json(value: str) -> str:
    return json.dumps(
        json.loads(value), ensure_ascii=False, indent=2, sort_keys=True
    )


__all__ = (
    "ThreeWayExportProvenance",
    "ThreeWayWorkbookRow",
    "ThreeWayWorkbookTeamImpact",
    "ThreeWayWorkbookTransfer",
    "three_way_workbook_rows",
)
