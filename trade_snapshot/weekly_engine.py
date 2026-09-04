"""Assemble one immutable offline engine from normalized weekly evidence."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .ecr import EcrSnapshot
from .engine_bundle import EngineBundle
from .ensemble import EnsembleConfig, EnsembleProjection, fuse_weekly_projections
from .feature_engineering import StrengthFeatureSet, build_strength_features
from .league_state import LeagueState
from .formula_verification import FormulaVerificationReport
from .fantasypros_benchmark import FantasyProsLeagueBenchmark
from .methodology_attestation import MethodologyAttestation
from .methodology_reuse import FormulaReuseDecision, MethodologyFingerprint
from .nfl_schedule import NflSchedule, NflTeamWeekStatus
from .projections import RemainingSeasonProjection, WeeklyProjection
from .projection_schedule import materialize_weekly_grid, normalize_ros_active_weeks
from .projection_source_policy import (
    validate_no_composite_double_count,
    validate_selectable_projection_providers,
)
from .projection_source import ProjectionSourceManifest
from .positions import normalize_player_position
from .scenario_config import CorrelatedScenarioConfig, PlayerEligibility
from .scoring import ScoringProfile
from .source_manifest import WeeklySourceManifest
from .strength import CalibrationStatus
from .strength_formula import StrengthFormula
from .surrogate_disclosure import SurrogateDisclosure
from .trade_space import TeamRoster
from .waiver_pool import WaiverPool


@dataclass(frozen=True, slots=True)
class WeeklyModelInputs:
    projections: tuple[EnsembleProjection, ...]
    features: StrengthFeatureSet


def build_weekly_engine(
    *,
    state: LeagueState,
    scoring_profile: ScoringProfile,
    rosters: Iterable[TeamRoster],
    projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    ecr_snapshots: Iterable[EcrSnapshot],
    eligibilities: Iterable[PlayerEligibility],
    player_positions: Mapping[str, str],
    player_nfl_team_ids: Mapping[str, str],
    player_names: Mapping[str, str],
    nfl_schedule: NflSchedule,
    source_manifest: WeeklySourceManifest,
    projection_source_manifest: ProjectionSourceManifest,
    fantasypros_benchmark: FantasyProsLeagueBenchmark,
    ensemble_config: EnsembleConfig,
    scenario_config: CorrelatedScenarioConfig,
    strength_formula: StrengthFormula,
    waiver_pool: WaiverPool,
    methodology_fingerprint: MethodologyFingerprint,
    formula_decision: FormulaReuseDecision,
    reuse_verification: FormulaVerificationReport | None,
    allow_surrogate_power: bool = False,
) -> EngineBundle:
    """Fuse providers, refresh calibrated scores, and seal a weekly bundle."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if not isinstance(scoring_profile, ScoringProfile):
        raise ValueError("scoring_profile must be a ScoringProfile")
    if scoring_profile.scoring_profile_id != state.scoring_profile_id:
        raise ValueError("league state does not match the exact scoring profile")
    if not isinstance(source_manifest, WeeklySourceManifest):
        raise ValueError("source_manifest must be a WeeklySourceManifest")
    if source_manifest.host_snapshot_id != state.snapshot_id:
        raise ValueError("source manifest does not match the league snapshot")
    if not isinstance(projection_source_manifest, ProjectionSourceManifest):
        raise ValueError(
            "projection_source_manifest must be a ProjectionSourceManifest"
        )
    if (
        projection_source_manifest.evaluation_scoring_profile_id
        != state.scoring_profile_id
    ):
        raise ValueError(
            "projection source manifest does not match the league scoring profile"
        )
    if not isinstance(fantasypros_benchmark, FantasyProsLeagueBenchmark):
        raise ValueError(
            "fantasypros_benchmark must be a FantasyProsLeagueBenchmark"
        )
    if fantasypros_benchmark.snapshot_id != state.snapshot_id:
        raise ValueError("FantasyPros benchmark does not match the league snapshot")
    rosters = tuple(rosters)
    evidence = _normalize_ros_evidence(
        tuple(projection_evidence),
        player_nfl_team_ids,
        nfl_schedule,
    )
    projection_source_manifest.validate_projection_evidence(evidence)
    ecr = tuple(ecr_snapshots)
    eligibility = tuple(eligibilities)
    if not isinstance(ensemble_config, EnsembleConfig):
        raise ValueError("ensemble_config must be an EnsembleConfig")
    if not isinstance(scenario_config, CorrelatedScenarioConfig):
        raise ValueError("scenario_config must be a CorrelatedScenarioConfig")
    if not isinstance(strength_formula, StrengthFormula):
        raise ValueError("strength_formula must be a StrengthFormula")
    if not isinstance(waiver_pool, WaiverPool):
        raise ValueError("waiver_pool must be a WaiverPool")
    if not isinstance(methodology_fingerprint, MethodologyFingerprint):
        raise ValueError("methodology_fingerprint must be a MethodologyFingerprint")
    if not isinstance(formula_decision, FormulaReuseDecision):
        raise ValueError("formula_decision must be a FormulaReuseDecision")
    if reuse_verification is not None and not isinstance(
        reuse_verification, FormulaVerificationReport
    ):
        raise ValueError(
            "reuse_verification must be a FormulaVerificationReport or None"
        )
    if not isinstance(allow_surrogate_power, bool):
        raise ValueError("allow_surrogate_power must be a boolean")
    prepared = prepare_weekly_model_inputs(
        state=state,
        projection_evidence=evidence,
        ecr_snapshots=ecr,
        eligibilities=eligibility,
        player_positions=player_positions,
        player_nfl_team_ids=player_nfl_team_ids,
        nfl_schedule=nfl_schedule,
        ensemble_config=ensemble_config,
    )
    model = strength_formula.build_model(prepared.features, rosters)
    if strength_formula.calibration.status is CalibrationStatus.EXACT:
        attestation = MethodologyAttestation.from_refresh(
            formula=strength_formula,
            strength_model=model,
            methodology_fingerprint=methodology_fingerprint,
            formula_decision=formula_decision,
            reuse_verification=reuse_verification,
        )
        disclosure = None
    elif (
        strength_formula.calibration.status is CalibrationStatus.SURROGATE
        and allow_surrogate_power
    ):
        attestation = None
        disclosure = SurrogateDisclosure.from_refresh(
            formula=strength_formula,
            strength_model=model,
            methodology_fingerprint=methodology_fingerprint,
            formula_decision=formula_decision,
        )
    else:
        raise ValueError(
            "a nonexact power formula requires explicit surrogate publication opt-in"
        )
    return EngineBundle(
        state=state,
        scoring_profile=scoring_profile,
        rosters=rosters,
        projections=prepared.projections,
        eligibilities=eligibility,
        nfl_schedule=nfl_schedule,
        source_manifest=source_manifest,
        projection_source_manifest=projection_source_manifest,
        fantasypros_benchmark=fantasypros_benchmark,
        ensemble_config=ensemble_config,
        scenario_config=scenario_config,
        strength_formula=strength_formula,
        strength_model=model,
        ecr_snapshots=ecr,
        projection_evidence=evidence,
        player_names=player_names,
        waiver_pool=waiver_pool,
        methodology_attestation=attestation,
        surrogate_disclosure=disclosure,
    )


def prepare_weekly_model_inputs(
    *,
    state: LeagueState,
    projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    ecr_snapshots: Iterable[EcrSnapshot],
    eligibilities: Iterable[PlayerEligibility],
    player_positions: Mapping[str, str],
    player_nfl_team_ids: Mapping[str, str],
    nfl_schedule: NflSchedule,
    ensemble_config: EnsembleConfig,
) -> WeeklyModelInputs:
    """Build the exact shared player inputs used by calibration and local scoring."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if not isinstance(ensemble_config, EnsembleConfig):
        raise ValueError("ensemble_config must be an EnsembleConfig")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be an NflSchedule")
    evidence = _normalize_ros_evidence(
        tuple(projection_evidence),
        player_nfl_team_ids,
        nfl_schedule,
    )
    ecr = tuple(ecr_snapshots)
    eligibility = tuple(eligibilities)
    positions = _positions(player_positions)
    ensembles = prepare_projection_ensemble(
        state,
        evidence,
        player_positions=positions,
        player_nfl_team_ids=player_nfl_team_ids,
        nfl_schedule=nfl_schedule,
        ensemble_config=ensemble_config,
    )
    providers = tuple(
        row.provider for row in ensemble_config.provider_weights
    )
    features = build_strength_features(
        ecr,
        ensembles,
        eligibility,
        provider_names=providers,
        projection_evidence=evidence,
        remaining_week_scopes={
            player_id: tuple(
                row.week
                for row in nfl_schedule.team_weeks
                if row.nfl_team_id == player_nfl_team_ids[player_id]
                and row.week >= state.first_remaining_week
                and row.status is NflTeamWeekStatus.SCHEDULED
            )
            for player_id in positions
        },
    )
    return WeeklyModelInputs(ensembles, features)


def prepare_projection_ensemble(
    state: LeagueState,
    projection_evidence: Iterable[WeeklyProjection | RemainingSeasonProjection],
    *,
    player_positions: Mapping[str, str],
    player_nfl_team_ids: Mapping[str, str],
    nfl_schedule: NflSchedule,
    ensemble_config: EnsembleConfig,
) -> tuple[EnsembleProjection, ...]:
    """Materialize and fuse provider evidence without requiring ECR features."""

    if not isinstance(state, LeagueState):
        raise ValueError("state must be a LeagueState")
    if not isinstance(ensemble_config, EnsembleConfig):
        raise ValueError("ensemble_config must be an EnsembleConfig")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be an NflSchedule")
    positions = _positions(player_positions)
    providers = tuple(row.provider for row in ensemble_config.provider_weights)
    validate_selectable_projection_providers(providers)
    validate_no_composite_double_count(providers)
    weekly = materialize_weekly_grid(
        state,
        tuple(projection_evidence),
        player_ids=positions,
        provider_names=providers,
        nfl_schedule=nfl_schedule,
        player_nfl_team_ids=player_nfl_team_ids,
    )
    return _fuse_grid(state, weekly, positions, ensemble_config)


__all__ = (
    "WeeklyModelInputs",
    "build_weekly_engine",
    "prepare_projection_ensemble",
    "prepare_weekly_model_inputs",
)


def _fuse_grid(state, rows, positions, config) -> tuple[EnsembleProjection, ...]:
    providers = tuple(row.provider for row in config.provider_weights)
    groups = defaultdict(list)
    identity = (state.snapshot_id, state.scoring_profile_id, state.season)
    for row in rows:
        if row.canonical_player_id is None:
            continue
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("weekly projection identity does not match league state")
        if row.week not in state.remaining_regular_season_weeks:
            continue
        groups[(row.canonical_player_id, row.week)].append(row)
    expected = {
        (player_id, week)
        for player_id in positions
        for week in state.remaining_regular_season_weeks
    }
    if set(groups) != expected:
        missing = expected.difference(groups)
        extra = set(groups).difference(expected)
        detail = min(missing or extra)
        raise ValueError(f"weekly provider evidence does not form the player/week grid: {detail!r}")
    result = []
    for key in sorted(expected):
        provider_rows = groups[key]
        actual = tuple(row.provider for row in provider_rows)
        if len(set(actual)) != len(actual) or set(actual) != set(providers):
            raise ValueError(f"player/week {key!r} does not have every configured provider")
        result.append(fuse_weekly_projections(provider_rows, positions[key[0]], config))
    return tuple(result)


def _positions(value):
    if not isinstance(value, Mapping) or not value:
        raise ValueError("player_positions must be a non-empty mapping")
    result = {}
    for player_id, position in value.items():
        if not isinstance(player_id, str) or not player_id.strip():
            raise ValueError("player_positions keys must be non-empty strings")
        if not isinstance(position, str) or not position.strip():
            raise ValueError("player_positions values must be non-empty strings")
        result[player_id.strip()] = normalize_player_position(position)
    return result


def _normalize_ros_evidence(rows, player_nfl_team_ids, nfl_schedule):
    if not isinstance(player_nfl_team_ids, Mapping):
        raise ValueError("player_nfl_team_ids must be a mapping")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be an NflSchedule")
    if any(
        not isinstance(row, (WeeklyProjection, RemainingSeasonProjection))
        for row in rows
    ):
        raise ValueError("projection_evidence must contain normalized projections")
    result = []
    for row in rows:
        player_id = row.canonical_player_id
        if isinstance(row, RemainingSeasonProjection) and player_id is not None:
            nfl_team_id = player_nfl_team_ids.get(player_id)
            if nfl_team_id is not None:
                row = normalize_ros_active_weeks(
                    row,
                    nfl_team_id=nfl_team_id,
                    nfl_schedule=nfl_schedule,
                )
        result.append(row)
    return tuple(result)
