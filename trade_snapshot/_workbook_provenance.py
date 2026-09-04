"""Validated reproducibility evidence for two-team workbook exports."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from math import isfinite
from numbers import Real

from ._scenario_random import canonical_json, content_id
from ._search_store_records import (
    SearchRunDefinition,
    _strict_json_loads,
    _thaw_json,
)
from .league_search import LeagueSearchOutcome


_REQUEST_FIELDS = {
    "allow_surrogate_power",
    "bundle_id",
    "counterparty_team_ids",
    "primary_team_id",
    "scenario_count",
    "seed",
    "settings",
    "trade_constraints",
}


@dataclass(frozen=True, slots=True)
class TwoTeamExportProvenance:
    """Exact request, bundle, waiver-pool, and pair-run export evidence."""

    bundle_id: str
    waiver_pool_id: str
    request_id: str
    request_json: str
    search_runs: tuple[SearchRunDefinition, ...]

    def __post_init__(self) -> None:
        for name in ("bundle_id", "waiver_pool_id", "request_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        request = _request_record(self.request_json)
        if request["bundle_id"] != self.bundle_id or content_id(
            "app-search", request
        ) != self.request_id:
            raise ValueError("request identity does not match export provenance")
        runs = _search_runs(self.search_runs, request)
        object.__setattr__(self, "search_runs", runs)

    @classmethod
    def from_records(
        cls,
        *,
        bundle_id: str,
        waiver_pool_id: str,
        request_id: str,
        request_record: Mapping[str, object],
        search_runs: Iterable[SearchRunDefinition],
    ) -> "TwoTeamExportProvenance":
        if not isinstance(request_record, Mapping):
            raise ValueError("request_record must be a mapping")
        try:
            runs = tuple(search_runs)
        except TypeError:
            raise ValueError("search_runs must be an iterable") from None
        return cls(
            bundle_id,
            waiver_pool_id,
            request_id,
            canonical_json(request_record),
            runs,
        )

    @classmethod
    def from_outcome(
        cls,
        *,
        bundle_id: str,
        waiver_pool_id: str,
        request_id: str,
        request_record: Mapping[str, object],
        outcome: LeagueSearchOutcome,
    ) -> "TwoTeamExportProvenance":
        if not isinstance(outcome, LeagueSearchOutcome):
            raise ValueError("outcome must be a LeagueSearchOutcome")
        progress = outcome.progress
        runs = tuple(pair.search.run_definition for pair in outcome.pairs)
        if (
            progress.cancelled
            or progress.completed_pair_count != progress.pair_count
            or len(outcome.pairs) != progress.pair_count
            or progress.examined_candidate_count != progress.total_candidate_count
            or sum(row.total_candidate_count for row in runs)
            != progress.total_candidate_count
            or any(
                pair.counterparty_team_id != pair.search.run_definition.counterparty_team_id
                for pair in outcome.pairs
            )
            or sum(pair.search.progress.power_qualified_count for pair in outcome.pairs)
            != progress.qualified_trade_count
            or sum(
                pair.search.progress.mutual_playoff_gain_count
                for pair in outcome.pairs
            )
            != progress.mutual_playoff_gain_count
        ):
            raise ValueError("only a completed league search can be exported")
        return cls.from_records(
            bundle_id=bundle_id,
            waiver_pool_id=waiver_pool_id,
            request_id=request_id,
            request_record=request_record,
            search_runs=runs,
        )

    @property
    def request_record(self) -> dict[str, object]:
        return _strict_json_loads(self.request_json)

    @property
    def resolved_counterparty_team_ids(self) -> tuple[str, ...]:
        return tuple(row.counterparty_team_id for row in self.search_runs)

    @property
    def total_candidate_count(self) -> int:
        return sum(row.total_candidate_count for row in self.search_runs)

    @property
    def trade_constraints_json(self) -> str:
        return canonical_json(self.request_record["trade_constraints"])

    @property
    def search_settings_json(self) -> str:
        return canonical_json(self.request_record["settings"])

    @property
    def require_no_drops(self) -> bool:
        return self.request_record["trade_constraints"]["require_no_drops"]

    @property
    def scenario_seed(self) -> int:
        return self.request_record["seed"]

    @property
    def requested_counterparty_display(self) -> str:
        requested = self.request_record["counterparty_team_ids"]
        return ", ".join(requested) if requested else "ALL OTHER TEAMS"

    @property
    def roster_adjustment_ids(self) -> tuple[str, ...]:
        return tuple(
            row.trade_constraint_record["roster_adjustment_id"]
            for row in self.search_runs
        )

    @property
    def search_run_rows(self) -> tuple[tuple[str, str, int, str], ...]:
        return tuple(
            (
                row.counterparty_team_id,
                row.run_id,
                row.total_candidate_count,
                canonical_json(row.to_record()),
            )
            for row in self.search_runs
        )


def _request_record(value: object) -> dict[str, object]:
    try:
        request = _strict_json_loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("request_json must be canonical strict JSON") from None
    if (
        not isinstance(request, dict)
        or not _REQUEST_FIELDS <= set(request)
        or set(request).difference(_REQUEST_FIELDS | {"trade_format"})
        or request.get("trade_format", "two_team") != "two_team"
        or canonical_json(request) != value
    ):
        raise ValueError("request_json is not a canonical two-team request")
    counterparties = request["counterparty_team_ids"]
    settings, constraints = request["settings"], request["trade_constraints"]
    if (
        not isinstance(counterparties, list)
        or any(not isinstance(row, str) or not row for row in counterparties)
        or len(set(counterparties)) != len(counterparties)
        or not isinstance(settings, dict)
        or not isinstance(constraints, dict)
        or type(constraints.get("require_no_drops")) is not bool
        or not _finite(settings.get("minimum_displayed_power_delta"))
        or type(request["allow_surrogate_power"]) is not bool
        or type(request["scenario_count"]) is not int
        or request["scenario_count"] < 1
        or type(request["seed"]) is not int
    ):
        raise ValueError("two-team request provenance fields are invalid")
    return request


def _search_runs(values, request) -> tuple[SearchRunDefinition, ...]:
    runs = tuple(values)
    if not runs or any(not isinstance(row, SearchRunDefinition) for row in runs):
        raise ValueError("search_runs must contain pair search definitions")
    counterparties = tuple(row.counterparty_team_id for row in runs)
    if len(set(counterparties)) != len(runs) or len(
        {row.run_id for row in runs}
    ) != len(runs):
        raise ValueError("search_runs contains a duplicate pair or run")
    requested = request["counterparty_team_ids"]
    if requested and tuple(requested) != counterparties:
        raise ValueError("search runs do not match requested counterparties")
    for run in runs:
        definition = _thaw_json(run.trade_constraint_record)
        if (
            run.primary_team_id != request["primary_team_id"]
            or not isinstance(definition.get("algorithm"), str)
            or not definition["algorithm"].strip()
            or not isinstance(definition.get("candidate_order"), dict)
            or not isinstance(definition.get("scenario_run_id"), str)
            or not definition["scenario_run_id"].strip()
            or not isinstance(definition.get("roster_adjustment_id"), str)
            or not definition["roster_adjustment_id"].strip()
            or definition.get("settings") != request["settings"]
            or definition.get("trade_constraints") != request["trade_constraints"]
        ):
            raise ValueError("search run does not match the search request")
    return runs


def _text(name, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _finite(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, Real) and isfinite(value)


__all__ = ("TwoTeamExportProvenance",)
