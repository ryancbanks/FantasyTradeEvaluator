"""Validated, presentation-neutral records for the Excel results workbook."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import fsum, isfinite
from numbers import Real
from types import MappingProxyType

from .data_readiness import DataReadinessSnapshot
from .league_search import LeagueSearchOutcome
from .league_state import LeagueState
from .methodology_attestation import MethodologyAttestation
from .season import SeasonProjection
from .surrogate_disclosure import SurrogateDisclosure


@dataclass(frozen=True, slots=True)
class WorkbookSource:
    name: str
    evidence_id: str
    captured_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text("source name", self.name))
        object.__setattr__(self, "evidence_id", _text("evidence_id", self.evidence_id))
        object.__setattr__(self, "captured_at", _aware("captured_at", self.captured_at))


@dataclass(frozen=True, slots=True)
class TradeWorkbookContext:
    snapshot_id: str
    scoring_profile_id: str
    nfl_schedule_id: str
    ensemble_config_id: str
    strength_model_id: str
    scenario_run_id: str
    primary_team_id: str
    primary_team_name: str
    generated_at: datetime
    minimum_power_delta: float
    scenario_count: int
    power_engine_mode: str
    calibration_status: str
    methodology_evidence_kind: str
    methodology_record_id: str
    formula_id: str
    formula_source_fit_id: str
    methodology_fingerprint_id: str
    formula_action: str
    methodology_current_evidence_id: str
    methodology_quality_gate: str
    methodology_holdout_count: int
    holdout_max_absolute_score_error: float
    holdout_display_match_rate: float
    holdout_validated_balanced_package_sizes: tuple[int, ...]
    data_readiness: DataReadinessSnapshot
    sources: tuple[WorkbookSource, ...]

    def __post_init__(self) -> None:
        for name in (
            "snapshot_id",
            "scoring_profile_id",
            "nfl_schedule_id",
            "ensemble_config_id",
            "strength_model_id",
            "scenario_run_id",
            "primary_team_id",
            "primary_team_name",
            "power_engine_mode",
            "calibration_status",
            "methodology_evidence_kind",
            "methodology_record_id",
            "formula_id",
            "formula_source_fit_id",
            "methodology_fingerprint_id",
            "formula_action",
            "methodology_current_evidence_id",
            "methodology_quality_gate",
        ):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(self, "generated_at", _aware("generated_at", self.generated_at))
        object.__setattr__(
            self,
            "minimum_power_delta",
            _finite("minimum_power_delta", self.minimum_power_delta),
        )
        if type(self.scenario_count) is not int or self.scenario_count < 1:
            raise ValueError("scenario_count must be a positive integer")
        if (
            type(self.methodology_holdout_count) is not int
            or self.methodology_holdout_count < 1
        ):
            raise ValueError("methodology_holdout_count must be a positive integer")
        error = _finite(
            "holdout_max_absolute_score_error",
            self.holdout_max_absolute_score_error,
        )
        rate = _finite("holdout_display_match_rate", self.holdout_display_match_rate)
        if error < 0 or not 0 <= rate <= 1:
            raise ValueError("workbook holdout quality metrics are invalid")
        object.__setattr__(self, "holdout_max_absolute_score_error", error)
        object.__setattr__(self, "holdout_display_match_rate", rate)
        if self.power_engine_mode not in {"holdout_validated", "surrogate"}:
            raise ValueError(
                "power_engine_mode must be holdout_validated or surrogate"
            )
        attested = self.power_engine_mode == "holdout_validated"
        if (
            self.calibration_status != ("exact" if attested else "surrogate")
            or self.methodology_evidence_kind
            != (
                "blind_holdout_attestation"
                if attested
                else "surrogate_disclosure"
            )
            or self.formula_action
            not in ({"reuse", "recalibrate"} if attested else {"recalibrate"})
        ):
            raise ValueError("workbook power-method provenance is inconsistent")
        sizes = tuple(self.holdout_validated_balanced_package_sizes)
        if any(type(value) is not int or value < 1 for value in sizes) or len(
            set(sizes)
        ) != len(sizes):
            raise ValueError(
                "holdout_validated_balanced_package_sizes must contain distinct "
                "positive integers"
            )
        if attested != bool(sizes):
            raise ValueError(
                "only a holdout-validated engine may declare validated package sizes"
            )
        object.__setattr__(
            self,
            "holdout_validated_balanced_package_sizes",
            tuple(sorted(sizes)),
        )
        if not isinstance(self.data_readiness, DataReadinessSnapshot):
            raise ValueError("data_readiness must be a DataReadinessSnapshot")
        sources = tuple(self.sources)
        if any(not isinstance(row, WorkbookSource) for row in sources):
            raise ValueError("sources must contain WorkbookSource values")
        names = tuple(row.name for row in sources)
        if len(set(names)) != len(names):
            raise ValueError("sources contains a duplicate source name")
        object.__setattr__(self, "sources", tuple(sorted(sources, key=lambda row: row.name)))


@dataclass(frozen=True, slots=True)
class WorkbookTradeRow:
    counterparty_team_id: str
    counterparty_team_name: str
    outgoing_player_ids: tuple[str, ...]
    outgoing_player_names: tuple[str, ...]
    incoming_player_ids: tuple[str, ...]
    incoming_player_names: tuple[str, ...]
    primary_power_delta: float
    counterparty_power_delta: float
    primary_playoff_before: float
    primary_playoff_after: float
    counterparty_playoff_before: float
    counterparty_playoff_after: float
    candidate_index: int
    power_methodology_status: str
    primary_added_player_ids: tuple[str, ...] = ()
    primary_added_player_names: tuple[str, ...] = ()
    primary_dropped_player_ids: tuple[str, ...] = ()
    primary_dropped_player_names: tuple[str, ...] = ()
    counterparty_added_player_ids: tuple[str, ...] = ()
    counterparty_added_player_names: tuple[str, ...] = ()
    counterparty_dropped_player_ids: tuple[str, ...] = ()
    counterparty_dropped_player_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("counterparty_team_id", "counterparty_team_name"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        for id_name, display_name in (
            ("outgoing_player_ids", "outgoing_player_names"),
            ("incoming_player_ids", "incoming_player_names"),
        ):
            ids = _texts(id_name, getattr(self, id_name), unique=True)
            names = _texts(display_name, getattr(self, display_name), unique=False)
            if len(ids) != len(names):
                raise ValueError(f"{id_name} and {display_name} must have equal lengths")
            object.__setattr__(self, id_name, ids)
            object.__setattr__(self, display_name, names)
        for id_name, display_name in (
            ("primary_added_player_ids", "primary_added_player_names"),
            ("primary_dropped_player_ids", "primary_dropped_player_names"),
            ("counterparty_added_player_ids", "counterparty_added_player_names"),
            ("counterparty_dropped_player_ids", "counterparty_dropped_player_names"),
        ):
            ids = _optional_texts(id_name, getattr(self, id_name), unique=True)
            names = _optional_texts(display_name, getattr(self, display_name), unique=False)
            if len(ids) != len(names):
                raise ValueError(f"{id_name} and {display_name} must have equal lengths")
            object.__setattr__(self, id_name, ids)
            object.__setattr__(self, display_name, names)
        for name in (
            "primary_power_delta",
            "counterparty_power_delta",
            "primary_playoff_before",
            "primary_playoff_after",
            "counterparty_playoff_before",
            "counterparty_playoff_after",
        ):
            value = _finite(name, getattr(self, name))
            if "playoff" in name and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)
        if type(self.candidate_index) is not int or self.candidate_index < 0:
            raise ValueError("candidate_index must be a non-negative integer")
        if self.power_methodology_status not in {
            "holdout_validated",
            "extrapolated",
            "surrogate",
            "surrogate_extrapolated",
        }:
            raise ValueError(
                "power_methodology_status must be holdout_validated, extrapolated, "
                "surrogate, or surrogate_extrapolated"
            )

    @property
    def primary_playoff_delta(self) -> float:
        return self.primary_playoff_after - self.primary_playoff_before

    @property
    def counterparty_playoff_delta(self) -> float:
        return self.counterparty_playoff_after - self.counterparty_playoff_before

    @property
    def combined_playoff_delta(self) -> float:
        return self.primary_playoff_delta + self.counterparty_playoff_delta

    @property
    def is_mutual_gain(self) -> bool:
        return self.primary_playoff_delta > 0 and self.counterparty_playoff_delta > 0


@dataclass(frozen=True, slots=True)
class WorkbookTeamOutlook:
    team_id: str
    team_name: str
    current_wins: int
    current_losses: int
    current_ties: int
    expected_final_wins: float
    expected_final_losses: float
    expected_final_ties: float
    mean_rank: float
    playoff_probability: float
    current_rank: int | None = None
    expected_final_points_for: float | None = None
    expected_final_points_against: float | None = None
    rank_distribution: tuple[float, ...] = ()
    seed_distribution: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _text("team_id", self.team_id))
        object.__setattr__(self, "team_name", _text("team_name", self.team_name))
        for name in ("current_wins", "current_losses", "current_ties"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "expected_final_wins",
            "expected_final_losses",
            "expected_final_ties",
            "mean_rank",
            "playoff_probability",
        ):
            value = _finite(name, getattr(self, name))
            if name == "playoff_probability" and not 0 <= value <= 1:
                raise ValueError("playoff_probability must be between 0 and 1")
            object.__setattr__(self, name, value)
        if self.current_rank is not None and (
            type(self.current_rank) is not int or self.current_rank < 1
        ):
            raise ValueError("current_rank must be a positive integer or None")
        for name in ("expected_final_points_for", "expected_final_points_against"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(name, value))
        rank_distribution = _distribution(
            "rank_distribution",
            self.rank_distribution,
            expected_total=1.0,
        )
        seed_distribution = _distribution(
            "seed_distribution",
            self.seed_distribution,
            expected_total=self.playoff_probability,
        )
        object.__setattr__(self, "rank_distribution", rank_distribution)
        object.__setattr__(self, "seed_distribution", seed_distribution)


def workbook_trade_rows(
    outcome: LeagueSearchOutcome,
    team_names: Mapping[str, str],
    player_names: Mapping[str, str],
    methodology_evidence: MethodologyAttestation | SurrogateDisclosure,
) -> tuple[WorkbookTradeRow, ...]:
    if not isinstance(outcome, LeagueSearchOutcome):
        raise ValueError("outcome must be a LeagueSearchOutcome")
    teams = _name_map("team_names", team_names)
    players = _name_map("player_names", player_names)
    if not isinstance(
        methodology_evidence, (MethodologyAttestation, SurrogateDisclosure)
    ):
        raise ValueError("methodology_evidence has an invalid type")
    rows = tuple(
        _trade_row(row, teams, players, methodology_evidence)
        for row in outcome.qualified_trades
    )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                not row.is_mutual_gain,
                -row.combined_playoff_delta,
                row.counterparty_team_name.casefold(),
                row.candidate_index,
            ),
        )
    )


def team_outlook_rows(
    state: LeagueState, projection: SeasonProjection
) -> tuple[WorkbookTeamOutlook, ...]:
    if not isinstance(state, LeagueState) or not isinstance(projection, SeasonProjection):
        raise ValueError("state and projection must be league projection values")
    if (
        state.snapshot_id != projection.snapshot_id
        or state.scoring_profile_id != projection.scoring_profile_id
    ):
        raise ValueError("state and projection identities do not match")
    names = {team.team_id: team.name for team in state.teams}
    standings = {row.team_id: row for row in state.standings}
    projected_rows = tuple(projection.teams)
    projected_ids = tuple(row.team_id for row in projected_rows)
    if len(set(projected_ids)) != len(projected_ids) or set(projected_ids) != set(names):
        raise ValueError("projection teams must exactly cover the league")
    rows = []
    for projected in projected_rows:
        standing = standings[projected.team_id]
        if projected.current_standing != standing:
            raise ValueError("projected current standing does not match league state")
        if (
            len(projected.rank_distribution) != len(names)
            or len(projected.seed_distribution)
            != state.playoff_rules.qualifier_count
        ):
            raise ValueError("projected distribution dimensions do not match league rules")
        mean_rank = fsum(
            rank * probability
            for rank, probability in enumerate(projected.rank_distribution, 1)
        )
        if abs(mean_rank - projected.mean_rank) > 1e-9:
            raise ValueError("projected mean rank does not match rank distribution")
        rows.append(
            WorkbookTeamOutlook(
                projected.team_id,
                names[projected.team_id],
                standing.wins,
                standing.losses,
                standing.ties,
                projected.expected_final_wins,
                projected.expected_final_losses,
                projected.expected_final_ties,
                projected.mean_rank,
                projected.playoff_probability,
                current_rank=projected.current_rank,
                expected_final_points_for=projected.expected_final_points_for,
                expected_final_points_against=projected.expected_final_points_against,
                rank_distribution=projected.rank_distribution,
                seed_distribution=projected.seed_distribution,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.mean_rank, row.team_name.casefold())))


def _trade_row(value, team_names, player_names, methodology_evidence):
    result = value.result
    odds = (
        result.primary_playoff_before,
        result.primary_playoff_after,
        result.counterparty_playoff_before,
        result.counterparty_playoff_after,
    )
    if any(number is None for number in odds):
        raise ValueError("workbook results require playoff odds for both teams")
    outgoing = result.outgoing_player_ids
    incoming = result.incoming_player_ids
    power_status = methodology_evidence.power_result_status(
        outgoing_count=len(outgoing),
        incoming_count=len(incoming),
        has_roster_adjustment=any(
            (
                result.primary_added_player_ids,
                result.primary_dropped_player_ids,
                result.counterparty_added_player_ids,
                result.counterparty_dropped_player_ids,
            )
        ),
    )
    try:
        team_name = team_names[value.counterparty_team_id]
        outgoing_names = tuple(player_names[player_id] for player_id in outgoing)
        incoming_names = tuple(player_names[player_id] for player_id in incoming)
        adjustment_names = {
            name.replace("_ids", "_names"): tuple(
                player_names[player_id] for player_id in getattr(result, name)
            )
            for name in (
                "primary_added_player_ids",
                "primary_dropped_player_ids",
                "counterparty_added_player_ids",
                "counterparty_dropped_player_ids",
            )
        }
    except KeyError as error:
        raise ValueError(f"missing display name for ID {error.args[0]!r}") from None
    return WorkbookTradeRow(
        value.counterparty_team_id,
        team_name,
        outgoing,
        outgoing_names,
        incoming,
        incoming_names,
        result.primary_display_power_delta,
        result.counterparty_display_power_delta,
        odds[0] / 100,
        odds[1] / 100,
        odds[2] / 100,
        odds[3] / 100,
        result.candidate_index,
        power_status,
        primary_added_player_ids=result.primary_added_player_ids,
        primary_added_player_names=adjustment_names["primary_added_player_names"],
        primary_dropped_player_ids=result.primary_dropped_player_ids,
        primary_dropped_player_names=adjustment_names["primary_dropped_player_names"],
        counterparty_added_player_ids=result.counterparty_added_player_ids,
        counterparty_added_player_names=adjustment_names[
            "counterparty_added_player_names"
        ],
        counterparty_dropped_player_ids=result.counterparty_dropped_player_ids,
        counterparty_dropped_player_names=adjustment_names[
            "counterparty_dropped_player_names"
        ],
    )


def _name_map(name: str, value: Mapping[str, str]):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    result = {_text(f"{name} key", key): _text(f"{name} value", child) for key, child in value.items()}
    return MappingProxyType(result)


def _texts(name: str, values: Iterable[str], *, unique: bool) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        result = tuple(_text(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if not result or (unique and len(set(result)) != len(result)):
        raise ValueError(f"{name} must be non-empty and contain no duplicate IDs")
    return result


def _optional_texts(name: str, values: Iterable[str], *, unique: bool) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        result = tuple(_text(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if unique and len(set(result)) != len(result):
        raise ValueError(f"{name} cannot contain duplicate IDs")
    return result


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _distribution(
    name: str,
    values: Iterable[float],
    *,
    expected_total: float,
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        result = tuple(_finite(name, value) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(value < 0 or value > 1 for value in result):
        raise ValueError(f"{name} values must be between 0 and 1")
    total = fsum(result)
    if result and abs(total - expected_total) > 1e-9:
        raise ValueError(f"{name} values have the wrong probability total")
    return result


def _aware(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)
