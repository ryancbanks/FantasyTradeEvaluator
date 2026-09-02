"""Transactional weekly refresh with bounded calibration and offline evaluation."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from ._analyzer_types import BundleFingerprint
from .ecr import EcrSnapshot
from .engine_bundle import EngineBundle, save_engine_bundle
from .ensemble import EnsembleConfig
from .formula_verification import FormulaVerificationReport
from .league_state import LeagueState
from .methodology import PowerMethodology
from .methodology_reuse import (
    FormulaAction,
    FormulaReuseDecision,
    MethodologyFingerprint,
    decide_formula_reuse,
    formula_static_incompatibility_reasons,
)
from .nfl_schedule import NflSchedule
from .projections import RemainingSeasonProjection, WeeklyProjection
from .scenario_config import CorrelatedScenarioConfig, PlayerEligibility
from .scoring import ScoringProfile
from .strength import CalibrationStatus, RoleDefinition
from .strength_formula import (
    StrengthFormula,
    load_strength_formula,
    save_strength_formula,
)
from .trade_space import TeamRoster
from .waiver_pool import WaiverPool
from .weekly_engine import build_weekly_engine


class RefreshStage(str, Enum):
    VALIDATING = "validating"
    VERIFYING_FORMULA = "verifying_formula"
    REUSING_FORMULA = "reusing_formula"
    CALIBRATING = "calibrating"
    BUILDING_ENGINE = "building_engine"
    SAVING = "saving"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class RefreshProgress:
    stage: RefreshStage
    fraction: float
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, RefreshStage):
            raise ValueError("stage must be a RefreshStage")
        if isinstance(self.fraction, bool) or not 0 <= self.fraction <= 1:
            raise ValueError("fraction must be between zero and one")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be a non-empty string")


@dataclass(frozen=True, slots=True)
class WeeklyRefreshEvidence:
    state: LeagueState
    scoring_profile: ScoringProfile
    rosters: tuple[TeamRoster, ...]
    projection_evidence: tuple[WeeklyProjection | RemainingSeasonProjection, ...]
    nfl_schedule: NflSchedule
    ecr_snapshots: tuple[EcrSnapshot, ...]
    eligibilities: tuple[PlayerEligibility, ...]
    player_positions: Mapping[str, str]
    player_nfl_team_ids: Mapping[str, str]
    player_names: Mapping[str, str]
    ensemble_config: EnsembleConfig
    scenario_config: CorrelatedScenarioConfig
    analyzer_bundle: BundleFingerprint
    response_schema_sha256: str
    power_methodology: PowerMethodology
    role_definitions: tuple[RoleDefinition, ...]
    waiver_pool: WaiverPool

    def __post_init__(self) -> None:
        if not isinstance(self.state, LeagueState):
            raise ValueError("state must be a LeagueState")
        if not isinstance(self.scoring_profile, ScoringProfile):
            raise ValueError("scoring_profile must be a ScoringProfile")
        if self.scoring_profile.scoring_profile_id != self.state.scoring_profile_id:
            raise ValueError("league state does not match the exact scoring profile")
        _rows("rosters", self.rosters, TeamRoster)
        evidence = tuple(self.projection_evidence)
        if not evidence or any(
            not isinstance(row, (WeeklyProjection, RemainingSeasonProjection))
            for row in evidence
        ):
            raise ValueError("projection_evidence must contain normalized projections")
        if not isinstance(self.nfl_schedule, NflSchedule):
            raise ValueError("nfl_schedule must be an NflSchedule")
        if self.nfl_schedule.season != self.state.season:
            raise ValueError("NFL schedule season does not match league state")
        _rows("ecr_snapshots", self.ecr_snapshots, EcrSnapshot)
        _rows("eligibilities", self.eligibilities, PlayerEligibility)
        _rows("role_definitions", self.role_definitions, RoleDefinition)
        if not isinstance(self.waiver_pool, WaiverPool):
            raise ValueError("waiver_pool must be a WaiverPool")
        if (
            self.waiver_pool.snapshot_id != self.state.snapshot_id
            or self.waiver_pool.scoring_profile_id != self.state.scoring_profile_id
        ):
            raise ValueError("waiver pool identity does not match league state")
        if self.waiver_pool.minimum_pool_size < self.state.roster_rules.roster_cap:
            raise ValueError("waiver pool cannot fill a complete active roster")
        if not isinstance(self.ensemble_config, EnsembleConfig):
            raise ValueError("ensemble_config must be an EnsembleConfig")
        if not isinstance(self.scenario_config, CorrelatedScenarioConfig):
            raise ValueError("scenario_config must be a CorrelatedScenarioConfig")
        if not isinstance(self.analyzer_bundle, BundleFingerprint):
            raise ValueError("analyzer_bundle must be a BundleFingerprint")
        if not isinstance(self.power_methodology, PowerMethodology):
            raise ValueError("power_methodology must be a PowerMethodology")
        object.__setattr__(self, "rosters", tuple(self.rosters))
        object.__setattr__(self, "projection_evidence", evidence)
        object.__setattr__(self, "ecr_snapshots", tuple(self.ecr_snapshots))
        object.__setattr__(self, "eligibilities", tuple(self.eligibilities))
        object.__setattr__(self, "role_definitions", tuple(self.role_definitions))
        positions = _mapping("player_positions", self.player_positions)
        teams = _mapping("player_nfl_team_ids", self.player_nfl_team_ids)
        names = _mapping("player_names", self.player_names)
        if set(positions) != set(teams) or set(positions) != set(names):
            raise ValueError(
                "player positions, NFL teams, and names must cover the same players"
            )
        owned = {
            player_id for roster in self.rosters for player_id in roster.player_ids
        }
        waiver = set(self.waiver_pool.player_ids)
        if owned & waiver:
            raise ValueError("waiver pool players cannot belong to a league roster")
        if set(positions) != owned | waiver:
            raise ValueError(
                "calculation players must be exactly the owned and waiver-pool players"
            )
        eligibility_ids = {row.canonical_player_id for row in self.eligibilities}
        if eligibility_ids != set(positions):
            raise ValueError("eligibility must cover every calculation player exactly")
        for team_id in teams.values():
            for week in self.state.remaining_regular_season_weeks:
                self.nfl_schedule.team_week(team_id, week)
        object.__setattr__(self, "player_positions", positions)
        object.__setattr__(self, "player_nfl_team_ids", teams)
        object.__setattr__(self, "player_names", names)

    @property
    def methodology_fingerprint(self) -> MethodologyFingerprint:
        return MethodologyFingerprint(
            self.analyzer_bundle,
            self.response_schema_sha256,
            self.power_methodology,
            self.role_definitions,
        )


@dataclass(frozen=True, slots=True)
class WeeklyRefreshResult:
    bundle: EngineBundle
    formula: StrengthFormula
    formula_decision: FormulaReuseDecision
    bundle_path: Path
    formula_path: Path
    reuse_verification: FormulaVerificationReport | None


class RefreshCancelled(RuntimeError):
    pass


class CalibrationRequired(RuntimeError):
    def __init__(self, decision: FormulaReuseDecision):
        self.decision = decision
        super().__init__("; ".join(decision.reasons))


Calibrator = Callable[[WeeklyRefreshEvidence, MethodologyFingerprint], StrengthFormula]
FormulaVerifier = Callable[
    [WeeklyRefreshEvidence, StrengthFormula, MethodologyFingerprint],
    FormulaVerificationReport,
]
ProgressCallback = Callable[[RefreshProgress], None]
CancellationCheck = Callable[[], bool]


def refresh_weekly_engine(
    evidence: WeeklyRefreshEvidence,
    *,
    formula_path: str | Path,
    bundle_directory: str | Path,
    calibrate: Calibrator | None = None,
    verify_reuse: FormulaVerifier | None = None,
    force_recalibration: bool = False,
    allow_surrogate_power: bool = False,
    progress: ProgressCallback | None = None,
    cancelled: CancellationCheck | None = None,
) -> WeeklyRefreshResult:
    """Revalidate or calibrate once, then build an entirely local weekly engine."""

    if not isinstance(evidence, WeeklyRefreshEvidence):
        raise ValueError("evidence must be WeeklyRefreshEvidence")
    if not isinstance(allow_surrogate_power, bool):
        raise ValueError("allow_surrogate_power must be a boolean")
    formula_target = Path(formula_path)
    bundle_target = Path(bundle_directory)
    _emit(progress, RefreshStage.VALIDATING, 0.05, "Checking this week's evidence")
    _cancel(cancelled)
    saved = _load_optional_formula(formula_target)
    fingerprint = evidence.methodology_fingerprint
    decision = decide_formula_reuse(
        saved,
        fingerprint,
        season=evidence.state.season,
        scoring_profile_id=evidence.state.scoring_profile_id,
    )
    if force_recalibration:
        decision = FormulaReuseDecision(
            FormulaAction.RECALIBRATE,
            ("recalibration was requested",),
            fingerprint.fingerprint_id,
        )

    verification = None
    if decision.action is FormulaAction.REUSE:
        _emit(
            progress,
            RefreshStage.VERIFYING_FORMULA,
            0.15,
            "Revalidating the saved formula on this week's holdouts",
        )
        _cancel(cancelled)
        if verify_reuse is None:
            rejection_reasons = ("weekly formula verification was not supplied",)
        else:
            verification = verify_reuse(evidence, saved, fingerprint)
            if not isinstance(verification, FormulaVerificationReport):
                raise ValueError(
                    "verify_reuse must return a FormulaVerificationReport"
                )
            rejection_reasons = verification.rejection_reasons(
                formula_id=saved.formula_id,
                methodology_fingerprint_id=fingerprint.fingerprint_id,
                weekly_snapshot_id=evidence.state.snapshot_id,
            )
        _cancel(cancelled)
        if rejection_reasons:
            decision = FormulaReuseDecision(
                FormulaAction.RECALIBRATE,
                rejection_reasons,
                fingerprint.fingerprint_id,
            )

    if decision.action is FormulaAction.REUSE:
        formula = saved
        _emit(
            progress,
            RefreshStage.REUSING_FORMULA,
            0.3,
            "Reusing the revalidated FantasyPros scoring method",
        )
    else:
        if calibrate is None:
            raise CalibrationRequired(decision)
        _emit(
            progress,
            RefreshStage.CALIBRATING,
            0.2,
            "Calibrating the FantasyPros scoring method",
        )
        _cancel(cancelled)
        formula = calibrate(evidence, fingerprint)
        if formula.calibration.status is CalibrationStatus.EXACT:
            compatible = decide_formula_reuse(
                formula,
                fingerprint,
                season=evidence.state.season,
                scoring_profile_id=evidence.state.scoring_profile_id,
            )
            if compatible.action is not FormulaAction.REUSE:
                raise ValueError(
                    "calibration did not produce a reusable exact formula: "
                    + "; ".join(compatible.reasons)
                )
        elif (
            formula.calibration.status is CalibrationStatus.SURROGATE
            and allow_surrogate_power
        ):
            reasons = formula_static_incompatibility_reasons(
                formula,
                fingerprint,
                season=evidence.state.season,
                scoring_profile_id=evidence.state.scoring_profile_id,
            )
            if formula.trained_snapshot_id != evidence.state.snapshot_id:
                reasons = (*reasons, "surrogate was not fitted on this weekly snapshot")
            if reasons:
                raise ValueError(
                    "surrogate calibration is incompatible with this week: "
                    + "; ".join(reasons)
                )
        else:
            raise ValueError(
                "calibration did not produce an exact formula; surrogate power "
                "requires explicit opt-in"
            )

    _cancel(cancelled)
    _emit(
        progress,
        RefreshStage.BUILDING_ENGINE,
        0.7,
        "Building local weekly projections and playoff simulations",
    )
    bundle = build_weekly_engine(
        state=evidence.state,
        scoring_profile=evidence.scoring_profile,
        rosters=evidence.rosters,
        projection_evidence=evidence.projection_evidence,
        nfl_schedule=evidence.nfl_schedule,
        ecr_snapshots=evidence.ecr_snapshots,
        eligibilities=evidence.eligibilities,
        player_positions=evidence.player_positions,
        player_nfl_team_ids=evidence.player_nfl_team_ids,
        player_names=evidence.player_names,
        ensemble_config=evidence.ensemble_config,
        scenario_config=evidence.scenario_config,
        strength_formula=formula,
        waiver_pool=evidence.waiver_pool,
        methodology_fingerprint=fingerprint,
        formula_decision=decision,
        reuse_verification=verification,
        allow_surrogate_power=allow_surrogate_power,
    )
    _cancel(cancelled)
    _emit(progress, RefreshStage.SAVING, 0.9, "Saving the offline weekly engine")
    if decision.action is FormulaAction.REUSE:
        saved_formula_path = formula_target.resolve()
    elif formula.calibration.status is CalibrationStatus.EXACT:
        saved_formula_path = save_strength_formula(formula, formula_target)
    else:
        saved_formula_path = save_strength_formula(
            formula, _surrogate_formula_path(formula_target, formula)
        )
    saved_bundle_path = save_engine_bundle(
        bundle,
        bundle_target / f"{bundle.bundle_id}.json",
    )
    ready_message = (
        "Weekly SURROGATE engine is ready; power results are approximate"
        if formula.calibration.status is CalibrationStatus.SURROGATE
        else "Weekly exact-method engine is ready"
    )
    _emit(progress, RefreshStage.COMPLETE, 1.0, ready_message)
    return WeeklyRefreshResult(
        bundle,
        formula,
        decision,
        saved_bundle_path,
        saved_formula_path,
        verification,
    )


def _load_optional_formula(path: Path) -> StrengthFormula | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("formula_path must identify a file")
    return load_strength_formula(path)


def _surrogate_formula_path(path: Path, formula: StrengthFormula) -> Path:
    digest = formula.formula_id.rsplit("_", 1)[-1]
    return path.with_name(f"{path.stem}.surrogate-{digest}.json")


def _emit(callback, stage, fraction, message):
    if callback is not None:
        callback(RefreshProgress(stage, fraction, message))


def _cancel(check):
    if check is not None and check():
        raise RefreshCancelled("weekly refresh was cancelled")


def _rows(name: str, values: Iterable[object], expected_type: type) -> tuple[object, ...]:
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if not rows or any(not isinstance(row, expected_type) for row in rows):
        raise ValueError(f"{name} must contain {expected_type.__name__} values")
    return rows


def _mapping(name: str, value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise ValueError(f"{name} must map non-empty strings to non-empty strings")
        result[key] = item
    return MappingProxyType(dict(sorted(result.items())))


__all__ = (
    "CalibrationRequired",
    "RefreshCancelled",
    "FormulaVerifier",
    "FormulaVerificationReport",
    "RefreshProgress",
    "RefreshStage",
    "WeeklyRefreshEvidence",
    "WeeklyRefreshResult",
    "refresh_weekly_engine",
)
