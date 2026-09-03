"""Bounded FantasyPros calibration session and exact-formula publication."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from ._scenario_random import content_id
from .analyzer_contract import AnalyzerObservation, AnalyzerTradeRequest
from .calibration_fit import CalibrationDiagnostics, fit_strength_surrogate
from .calibration_observations import (
    analyzer_request_for_experiment,
    prepare_calibration_evidence,
)
from .calibration_plan import CalibrationExperimentPlan, design_calibration_experiments
from .feature_engineering import StrengthFeatureSet, require_available_features
from .formula_verification import REQUIRED_BALANCED_PACKAGE_SIZES
from .methodology import PowerMethodology
from .methodology_reuse import MethodologyFingerprint
from .strength import CalibrationStatus, RoleDefinition
from .strength_formula import StrengthFormula
from .trade_space import TeamRoster
from .weekly_engine import prepare_weekly_model_inputs
from .weekly_refresh import WeeklyRefreshEvidence


@dataclass(frozen=True, slots=True)
class CalibrationSession:
    features: StrengthFeatureSet
    roles: tuple[RoleDefinition, ...]
    rosters: tuple[TeamRoster, ...]
    methodology: PowerMethodology
    fingerprint: MethodologyFingerprint
    plan: CalibrationExperimentPlan
    requests: Mapping[str, AnalyzerTradeRequest]
    team_provider_ids: Mapping[str, str]
    player_provider_ids: Mapping[str, str]
    session_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.features, StrengthFeatureSet):
            raise ValueError("features must be a StrengthFeatureSet")
        if not isinstance(self.methodology, PowerMethodology):
            raise ValueError("methodology must be a PowerMethodology")
        if not isinstance(self.fingerprint, MethodologyFingerprint):
            raise ValueError("fingerprint must be a MethodologyFingerprint")
        if not isinstance(self.plan, CalibrationExperimentPlan):
            raise ValueError("plan must be a CalibrationExperimentPlan")
        roles = tuple(self.roles)
        rosters = tuple(self.rosters)
        if roles != self.fingerprint.role_definitions or tuple(
            row.role_id for row in roles
        ) != self.plan.role_ids:
            raise ValueError("session roles do not match its plan and fingerprint")
        if self.plan.snapshot_id != self.features.snapshot_id:
            raise ValueError("session plan and features use different snapshots")
        teams = _mapping("team_provider_ids", self.team_provider_ids)
        players = _mapping("player_provider_ids", self.player_provider_ids)
        requests = _request_mapping(self.requests, self.plan, teams, players)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "rosters", rosters)
        object.__setattr__(self, "team_provider_ids", teams)
        object.__setattr__(self, "player_provider_ids", players)
        object.__setattr__(self, "requests", requests)
        object.__setattr__(
            self,
            "session_id",
            content_id(
                "calibration-session",
                {
                    "feature_set_id": self.features.feature_set_id,
                    "methodology_fingerprint_id": self.fingerprint.fingerprint_id,
                    "plan_id": self.plan.plan_id,
                    "requests": {
                        key: _request_record(value)
                        for key, value in sorted(requests.items())
                    },
                },
            ),
        )


class CalibrationNotExact(RuntimeError):
    def __init__(
        self,
        diagnostics: CalibrationDiagnostics,
        candidate_formula: StrengthFormula,
        *,
        surrogate_eligible: bool,
    ):
        self.diagnostics = diagnostics
        self.candidate_formula = candidate_formula
        self.surrogate_eligible = surrogate_eligible
        super().__init__(
            "FantasyPros methodology was not replicated exactly on blind holdouts"
        )


def prepare_calibration_session(
    *,
    features: StrengthFeatureSet,
    roles: tuple[RoleDefinition, ...],
    rosters: tuple[TeamRoster, ...],
    primary_team_id: str,
    methodology: PowerMethodology,
    fingerprint: MethodologyFingerprint,
    team_provider_ids: Mapping[str, str],
    player_provider_ids: Mapping[str, str],
    training_experiment_count: int = 250,
    held_out_experiment_count: int = 100,
) -> CalibrationSession:
    """Design a small, diverse ordinary-power experiment instead of all trades."""

    if not isinstance(methodology, PowerMethodology):
        raise ValueError("methodology must be a PowerMethodology")
    require_available_features(
        features,
        (*methodology.residual_feature_names, *methodology.role_feature_names),
    )
    plan = design_calibration_experiments(
        features,
        roles,
        rosters,
        primary_team_id=primary_team_id,
        residual_feature_names=methodology.residual_feature_names,
        role_feature_names=methodology.role_feature_names,
        training_experiment_count=training_experiment_count,
        held_out_experiment_count=held_out_experiment_count,
    )
    requests = {
        experiment.experiment_id: analyzer_request_for_experiment(
            experiment,
            team_provider_ids=team_provider_ids,
            player_provider_ids=player_provider_ids,
        )
        for experiment in plan.experiments
    }
    return CalibrationSession(
        features,
        tuple(roles),
        tuple(rosters),
        methodology,
        fingerprint,
        plan,
        requests,
        team_provider_ids,
        player_provider_ids,
    )


def prepare_weekly_calibration_session(
    evidence: WeeklyRefreshEvidence,
    *,
    primary_team_id: str,
    team_provider_ids: Mapping[str, str],
    player_provider_ids: Mapping[str, str],
    training_experiment_count: int = 250,
    held_out_experiment_count: int = 100,
) -> CalibrationSession:
    """Use the same provider evidence and features that the local engine will score."""

    if not isinstance(evidence, WeeklyRefreshEvidence):
        raise ValueError("evidence must be WeeklyRefreshEvidence")
    prepared = prepare_weekly_model_inputs(
        state=evidence.state,
        projection_evidence=evidence.projection_evidence,
        ecr_snapshots=evidence.ecr_snapshots,
        eligibilities=evidence.eligibilities,
        player_positions=evidence.player_positions,
        player_nfl_team_ids=evidence.player_nfl_team_ids,
        nfl_schedule=evidence.nfl_schedule,
        ensemble_config=evidence.ensemble_config,
    )
    return prepare_calibration_session(
        features=prepared.features,
        roles=evidence.role_definitions,
        rosters=evidence.rosters,
        primary_team_id=primary_team_id,
        methodology=evidence.power_methodology,
        fingerprint=evidence.methodology_fingerprint,
        team_provider_ids=team_provider_ids,
        player_provider_ids=player_provider_ids,
        training_experiment_count=training_experiment_count,
        held_out_experiment_count=held_out_experiment_count,
    )


def complete_calibration_session(
    session: CalibrationSession,
    observations: Mapping[str, AnalyzerObservation],
    *,
    captured_at: datetime,
    allow_surrogate_power: bool = False,
) -> StrengthFormula:
    """Fit and blind-test; publish a nonreusable surrogate only by opt-in."""

    if not isinstance(session, CalibrationSession):
        raise ValueError("session must be a CalibrationSession")
    if not isinstance(allow_surrogate_power, bool):
        raise ValueError("allow_surrogate_power must be a boolean")
    prepared = prepare_calibration_evidence(
        session.plan,
        observations,
        session.features,
        session.roles,
        session.rosters,
        team_provider_ids=session.team_provider_ids,
        player_provider_ids=session.player_provider_ids,
    )
    if prepared.bundle != session.fingerprint.analyzer_bundle:
        raise ValueError("captured analyzer bundle changed during calibration")
    fitted = fit_strength_surrogate(
        prepared.corpus,
        session.methodology.fit_config(),
        bundle=prepared.bundle,
        response_schema_sha256=session.fingerprint.response_schema_sha256,
        captured_at=captured_at,
    )
    formula = StrengthFormula.from_fitted(fitted)
    if fitted.model.calibration.status is CalibrationStatus.EXACT:
        return formula
    eligible = _surrogate_is_publishable(fitted, formula)
    if allow_surrogate_power and eligible:
        return formula
    raise CalibrationNotExact(
        fitted.diagnostics,
        formula,
        surrogate_eligible=eligible,
    )


def _surrogate_is_publishable(fitted, formula) -> bool:
    """Allow only a healthy fit whose blind exactness check was the failure."""

    diagnostics = fitted.diagnostics
    return (
        fitted.model.calibration.status is CalibrationStatus.SURROGATE
        and diagnostics.converged
        and diagnostics.identifiable
        and diagnostics.training_max_absolute_error
        <= fitted.config.exact_raw_tolerance
        and diagnostics.held_out_trade_count
        >= fitted.config.minimum_exact_holdouts
        and diagnostics.held_out_distinct_perturbation_count
        >= fitted.config.minimum_exact_holdouts
        and REQUIRED_BALANCED_PACKAGE_SIZES.issubset(
            formula.held_out_balanced_package_sizes
        )
    )


def _request_mapping(value, plan, teams, players):
    if not isinstance(value, Mapping) or set(value) != {
        row.experiment_id for row in plan.experiments
    }:
        raise ValueError("requests must exactly cover the calibration plan")
    result = {}
    for experiment in plan.experiments:
        request = value[experiment.experiment_id]
        expected = analyzer_request_for_experiment(
            experiment,
            team_provider_ids=teams,
            player_provider_ids=players,
        )
        if request != expected:
            raise ValueError("calibration request does not match its experiment")
        result[experiment.experiment_id] = request
    return MappingProxyType(dict(sorted(result.items())))


def _mapping(name, value):
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise ValueError(f"{name} must map non-empty strings to non-empty strings")
        result[key] = item
    if len(set(result.values())) != len(result):
        raise ValueError(f"{name} provider IDs must be unique")
    return MappingProxyType(dict(sorted(result.items())))


def _request_record(request):
    return {
        "period": request.period.value,
        "team1_id": request.team1_id,
        "team2_id": request.team2_id,
        "team1_gets": list(request.team1_gets),
        "team2_gets": list(request.team2_gets),
        "team1_adds": list(request.team1_adds),
        "team2_adds": list(request.team2_adds),
        "team1_drops": list(request.team1_drops),
        "team2_drops": list(request.team2_drops),
    }


__all__ = (
    "CalibrationNotExact",
    "CalibrationSession",
    "complete_calibration_session",
    "prepare_calibration_session",
    "prepare_weekly_calibration_session",
)
