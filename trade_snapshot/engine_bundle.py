"""Portable, content-addressed weekly engine bundles for the local app."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from ._scenario_random import content_id
from .ecr import EcrPeriod, EcrSnapshot
from .ensemble import (
    EnsembleConfig,
    EnsembleProjection,
    ensemble_from_record,
    ensemble_to_record,
)
from .feature_engineering import build_strength_features
from .fantasypros_benchmark import FantasyProsLeagueBenchmark
from .league_io import league_state_from_record, league_state_to_record
from .league_state import LeagueState
from .methodology_attestation import MethodologyAttestation
from .methodology_reuse import formula_static_incompatibility_reasons
from .nfl_schedule import (
    NflSchedule,
    NflTeamWeekStatus,
    validate_complete_regular_season,
)
from .projection_io import projection_from_record, projection_to_record
from .projection_lineage import ProjectionLineageIndex
from .projection_source import ProjectionSourceManifest
from .projection_schedule import validate_weekly_projection_schedule
from .projections import ProjectionStatus, RemainingSeasonProjection, WeeklyProjection
from .scenario_config import CorrelatedScenarioConfig, PlayerEligibility
from .scoring import ScoringProfile
from .strength import StrengthModel
from .strength_formula import StrengthFormula
from .source_manifest import WeeklySourceManifest
from .surrogate_disclosure import SurrogateDisclosure
from .trade_space import TeamRoster
from .waiver_pool import WaiverPool


_BUNDLE_SCHEMA_VERSION = 8


class UnsupportedEngineBundleSchema(ValueError):
    """A recognized bundle needs a newer scan or application version."""

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        if schema_version < _BUNDLE_SCHEMA_VERSION:
            recovery = "collect the league again to create a current bundle"
        else:
            recovery = "update the application before opening this bundle"
        super().__init__(
            f"engine bundle schema {schema_version} is unsupported; {recovery}"
        )


@dataclass(frozen=True, slots=True)
class EngineBundle:
    state: LeagueState
    scoring_profile: ScoringProfile
    rosters: tuple[TeamRoster, ...]
    projections: tuple[EnsembleProjection, ...]
    eligibilities: tuple[PlayerEligibility, ...]
    nfl_schedule: NflSchedule
    source_manifest: WeeklySourceManifest
    projection_source_manifest: ProjectionSourceManifest
    fantasypros_benchmark: FantasyProsLeagueBenchmark
    ensemble_config: EnsembleConfig
    scenario_config: CorrelatedScenarioConfig
    strength_formula: StrengthFormula
    strength_model: StrengthModel
    ecr_snapshots: tuple[EcrSnapshot, ...]
    projection_evidence: tuple[WeeklyProjection | RemainingSeasonProjection, ...]
    player_names: Mapping[str, str]
    waiver_pool: WaiverPool
    methodology_attestation: MethodologyAttestation | None
    surrogate_disclosure: SurrogateDisclosure | None = None
    bundle_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, LeagueState):
            raise ValueError("state must be a LeagueState")
        if not isinstance(self.scoring_profile, ScoringProfile):
            raise ValueError("scoring_profile must be a ScoringProfile")
        rosters = _typed("rosters", self.rosters, TeamRoster)
        projections = _typed("projections", self.projections, EnsembleProjection)
        eligibilities = _typed("eligibilities", self.eligibilities, PlayerEligibility)
        ecr = _typed("ecr_snapshots", self.ecr_snapshots, EcrSnapshot)
        evidence = tuple(self.projection_evidence)
        if not evidence or any(
            not isinstance(row, (WeeklyProjection, RemainingSeasonProjection))
            for row in evidence
        ):
            raise ValueError("projection_evidence must contain normalized source projections")
        if not isinstance(self.scenario_config, CorrelatedScenarioConfig):
            raise ValueError("scenario_config must be a CorrelatedScenarioConfig")
        if not isinstance(self.nfl_schedule, NflSchedule):
            raise ValueError("nfl_schedule must be an NflSchedule")
        if not isinstance(self.source_manifest, WeeklySourceManifest):
            raise ValueError("source_manifest must be a WeeklySourceManifest")
        if self.source_manifest.host_snapshot_id != self.state.snapshot_id:
            raise ValueError("source manifest does not match the league snapshot")
        if not isinstance(self.projection_source_manifest, ProjectionSourceManifest):
            raise ValueError(
                "projection_source_manifest must be a ProjectionSourceManifest"
            )
        if (
            self.projection_source_manifest.evaluation_scoring_profile_id
            != self.state.scoring_profile_id
        ):
            raise ValueError(
                "projection source manifest does not match the league scoring profile"
            )
        if not isinstance(self.fantasypros_benchmark, FantasyProsLeagueBenchmark):
            raise ValueError(
                "fantasypros_benchmark must be a FantasyProsLeagueBenchmark"
            )
        if (
            self.fantasypros_benchmark.snapshot_id != self.state.snapshot_id
            or self.fantasypros_benchmark.source_artifact_id
            != self.source_manifest.fantasypros_league_artifact_id
        ):
            raise ValueError("FantasyPros benchmark does not match the source manifest")
        if not isinstance(self.ensemble_config, EnsembleConfig):
            raise ValueError("ensemble_config must be an EnsembleConfig")
        if not isinstance(self.strength_formula, StrengthFormula):
            raise ValueError("strength_formula must be a StrengthFormula")
        if not isinstance(self.strength_model, StrengthModel):
            raise ValueError("strength_model must be a StrengthModel")
        if not isinstance(self.waiver_pool, WaiverPool):
            raise ValueError("waiver_pool must be a WaiverPool")
        exact = isinstance(self.methodology_attestation, MethodologyAttestation)
        surrogate = isinstance(self.surrogate_disclosure, SurrogateDisclosure)
        if (self.methodology_attestation is not None and not exact) or (
            self.surrogate_disclosure is not None and not surrogate
        ):
            raise ValueError("methodology evidence has an invalid type")
        if exact == surrogate:
            raise ValueError(
                "bundle requires exactly one exact attestation or surrogate disclosure"
            )
        names = _names(self.player_names)
        _validate_identity(self, rosters, projections, eligibilities, ecr, evidence, names)
        self.projection_source_manifest.validate_projection_evidence(evidence)
        rosters = tuple(sorted(rosters, key=lambda row: row.team_id))
        projections = tuple(
            sorted(projections, key=lambda row: (row.canonical_player_id, row.week))
        )
        eligibilities = tuple(sorted(eligibilities, key=lambda row: row.canonical_player_id))
        ecr = tuple(sorted(ecr, key=lambda row: row.period.value))
        evidence = tuple(sorted(evidence, key=_evidence_key))
        object.__setattr__(self, "rosters", rosters)
        object.__setattr__(self, "projections", projections)
        object.__setattr__(self, "eligibilities", eligibilities)
        object.__setattr__(self, "ecr_snapshots", ecr)
        object.__setattr__(self, "projection_evidence", evidence)
        object.__setattr__(self, "player_names", names)
        object.__setattr__(self, "bundle_id", content_id("engine", self._content_record()))

    @property
    def methodology_evidence(self) -> MethodologyAttestation | SurrogateDisclosure:
        evidence = self.methodology_attestation or self.surrogate_disclosure
        assert evidence is not None
        return evidence

    @property
    def methodology_mode(self) -> str:
        return (
            "holdout_validated"
            if self.methodology_attestation is not None
            else "surrogate"
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "ecr_snapshots": [row.to_record() for row in self.ecr_snapshots],
            "ensemble_config": self.ensemble_config.to_record(),
            "fantasypros_benchmark": self.fantasypros_benchmark.to_record(),
            "eligibilities": [
                {
                    "canonical_player_id": row.canonical_player_id,
                    "eligible_slots": list(row.eligible_slots),
                }
                for row in self.eligibilities
            ],
            "league_state": league_state_to_record(self.state),
            "methodology_attestation": (
                None
                if self.methodology_attestation is None
                else self.methodology_attestation.to_record()
            ),
            "nfl_schedule": self.nfl_schedule.to_record(),
            "source_manifest": self.source_manifest.to_record(),
            "projection_source_manifest": self.projection_source_manifest.to_record(),
            "player_names": dict(self.player_names),
            "projection_evidence": [
                projection_to_record(row) for row in self.projection_evidence
            ],
            "projections": [ensemble_to_record(row) for row in self.projections],
            "rosters": [
                {
                    "capacity_exempt_player_ids": sorted(
                        row.capacity_exempt_player_ids
                    ),
                    "current_size": row.current_size,
                    "player_ids": list(row.player_ids),
                    "roster_cap": row.roster_cap,
                    "team_id": row.team_id,
                }
                for row in self.rosters
            ],
            "scoring_profile": self.scoring_profile.to_record(),
            "scenario_config": self.scenario_config.to_record(),
            "strength_model": self.strength_model.to_record(),
            "strength_formula": self.strength_formula.to_record(),
            "surrogate_disclosure": (
                None
                if self.surrogate_disclosure is None
                else self.surrogate_disclosure.to_record()
            ),
            "waiver_pool": self.waiver_pool.to_record(),
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "fantasy_trade_engine_bundle",
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            **self._content_record(),
            "bundle_id": self.bundle_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "EngineBundle":
        content_keys = {
            "ecr_snapshots",
            "ensemble_config",
            "fantasypros_benchmark",
            "eligibilities",
            "league_state",
            "methodology_attestation",
            "nfl_schedule",
            "source_manifest",
            "projection_source_manifest",
            "player_names",
            "projection_evidence",
            "projections",
            "rosters",
            "scoring_profile",
            "scenario_config",
            "strength_model",
            "strength_formula",
            "surrogate_disclosure",
            "waiver_pool",
        }
        if not isinstance(record, Mapping):
            raise ValueError("engine bundle record fields are invalid")
        if record.get("kind") != "fantasy_trade_engine_bundle":
            raise ValueError("engine bundle kind is invalid")
        schema_version = record.get("schema_version")
        if type(schema_version) is not int:
            raise ValueError("engine bundle schema version is invalid")
        if schema_version != _BUNDLE_SCHEMA_VERSION:
            raise UnsupportedEngineBundleSchema(schema_version)
        if set(record) != content_keys | {
            "kind",
            "schema_version",
            "bundle_id",
        }:
            raise ValueError("engine bundle record fields are invalid")
        arrays = {
            name: _json_array(name, record[name])
            for name in (
                "ecr_snapshots",
                "eligibilities",
                "projection_evidence",
                "projections",
                "rosters",
            )
        }
        bundle = cls(
            state=league_state_from_record(_mapping("league_state", record["league_state"])),
            scoring_profile=ScoringProfile.from_record(
                _mapping("scoring_profile", record["scoring_profile"])
            ),
            rosters=tuple(_roster_from_record(row) for row in arrays["rosters"]),
            projections=tuple(
                ensemble_from_record(_mapping("projection", row))
                for row in arrays["projections"]
            ),
            eligibilities=tuple(
                _eligibility_from_record(row) for row in arrays["eligibilities"]
            ),
            nfl_schedule=NflSchedule.from_record(
                _mapping("nfl_schedule", record["nfl_schedule"])
            ),
            source_manifest=WeeklySourceManifest.from_record(
                _mapping("source_manifest", record["source_manifest"])
            ),
            projection_source_manifest=ProjectionSourceManifest.from_record(
                _mapping(
                    "projection_source_manifest",
                    record["projection_source_manifest"],
                )
            ),
            fantasypros_benchmark=FantasyProsLeagueBenchmark.from_record(
                _mapping("fantasypros_benchmark", record["fantasypros_benchmark"])
            ),
            ensemble_config=EnsembleConfig.from_record(
                _mapping("ensemble_config", record["ensemble_config"])
            ),
            scenario_config=CorrelatedScenarioConfig.from_record(
                _mapping("scenario_config", record["scenario_config"])
            ),
            strength_model=StrengthModel.from_record(
                _mapping("strength_model", record["strength_model"])
            ),
            strength_formula=StrengthFormula.from_record(
                _mapping("strength_formula", record["strength_formula"])
            ),
            ecr_snapshots=tuple(
                EcrSnapshot.from_record(_mapping("ECR snapshot", row))
                for row in arrays["ecr_snapshots"]
            ),
            projection_evidence=tuple(
                projection_from_record(_mapping("projection evidence", row))
                for row in arrays["projection_evidence"]
            ),
            player_names=_mapping("player_names", record["player_names"]),
            waiver_pool=WaiverPool.from_record(
                _mapping("waiver_pool", record["waiver_pool"])
            ),
            methodology_attestation=(
                None
                if record["methodology_attestation"] is None
                else MethodologyAttestation.from_record(
                    _mapping(
                        "methodology_attestation", record["methodology_attestation"]
                    )
                )
            ),
            surrogate_disclosure=(
                None
                if record["surrogate_disclosure"] is None
                else SurrogateDisclosure.from_record(
                    _mapping(
                        "surrogate_disclosure", record["surrogate_disclosure"]
                    )
                )
            ),
        )
        if record["bundle_id"] != bundle.bundle_id:
            raise ValueError("engine bundle content does not match bundle_id")
        return bundle


def save_engine_bundle(bundle: EngineBundle, path: str | os.PathLike[str]) -> Path:
    if not isinstance(bundle, EngineBundle):
        raise ValueError("bundle must be an EngineBundle")
    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise ValueError("engine bundle path must end in .json")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.json")
    try:
        temporary.write_text(
            json.dumps(bundle.to_record(), sort_keys=True, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()


def load_engine_bundle(path: str | os.PathLike[str]) -> EngineBundle:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
        record = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read engine bundle: {error}") from None
    return EngineBundle.from_record(_mapping("engine bundle", record))


def _validate_identity(bundle, rosters, projections, eligibilities, ecr, evidence, names):
    state = bundle.state
    identity = (state.snapshot_id, state.scoring_profile_id, state.season)
    if bundle.scoring_profile.scoring_profile_id != state.scoring_profile_id:
        raise ValueError("league state does not match the exact scoring profile")
    if (
        bundle.strength_model.snapshot_id,
        bundle.strength_model.scoring_profile_id,
        bundle.strength_model.season,
    ) != identity:
        raise ValueError("strength model identity does not match league state")
    bundle.methodology_evidence.validate_bundle(
        snapshot_id=state.snapshot_id,
        strength_model=bundle.strength_model,
    )
    if (
        bundle.waiver_pool.snapshot_id != state.snapshot_id
        or bundle.waiver_pool.scoring_profile_id != state.scoring_profile_id
    ):
        raise ValueError("waiver pool identity does not match league state")
    if bundle.waiver_pool.minimum_pool_size < state.roster_rules.roster_cap:
        raise ValueError("waiver pool cannot fill a complete active roster")
    team_ids = {team.team_id for team in state.teams}
    if {row.team_id for row in bundle.fantasypros_benchmark.teams} != team_ids:
        raise ValueError("FantasyPros benchmark must exactly cover league teams")
    if {row.team_id for row in rosters} != team_ids or len(rosters) != len(team_ids):
        raise ValueError("rosters must exactly cover league teams")
    if any(
        row.current_size != len(row.player_ids)
        or row.roster_cap != state.roster_rules.roster_cap
        for row in rosters
    ):
        raise ValueError("rosters must be complete and use the league roster cap")
    owned = [player_id for row in rosters for player_id in row.player_ids]
    if len(set(owned)) != len(owned):
        raise ValueError("a player cannot be owned by multiple teams")
    waiver_ids = set(bundle.waiver_pool.player_ids)
    if waiver_ids.intersection(owned):
        raise ValueError("waiver pool players cannot belong to a league roster")
    eligibility_ids = tuple(row.canonical_player_id for row in eligibilities)
    if len(set(eligibility_ids)) != len(eligibility_ids):
        raise ValueError("eligibilities contains a duplicate player")
    projection_keys = tuple((row.canonical_player_id, row.week) for row in projections)
    if len(set(projection_keys)) != len(projection_keys):
        raise ValueError("projections contains a duplicate player/week")
    projection_ids = {player_id for player_id, _ in projection_keys}
    if projection_ids != set(eligibility_ids) or projection_ids != set(bundle.strength_model.players):
        raise ValueError("projection, eligibility, and strength player universes differ")
    if projection_ids != set(owned) | waiver_ids:
        raise ValueError(
            "calculation players must be exactly the owned and waiver-pool players"
        )
    if not set(owned).issubset(projection_ids) or not projection_ids.issubset(names):
        raise ValueError("owned or projected player is missing normalized data or a display name")
    expected_projection_keys = {
        (player_id, week)
        for player_id in projection_ids
        for week in state.remaining_regular_season_weeks
    }
    if set(projection_keys) != expected_projection_keys:
        raise ValueError("ensemble projections must form the complete remaining-season grid")
    for row in projections:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("projection identity does not match league state")
    periods = {row.period for row in ecr}
    if len(ecr) != 2 or periods != {EcrPeriod.WEEKLY, EcrPeriod.REST_OF_SEASON}:
        raise ValueError("engine bundle requires weekly and ROS ECR snapshots")
    if any((row.snapshot_id, row.scoring_profile_id, row.season) != identity for row in ecr):
        raise ValueError("ECR identity does not match league state")
    if any(row.as_of_week != state.first_remaining_week for row in ecr):
        raise ValueError("ECR as_of_week does not match the league state")
    eligibility_by_player = {
        row.canonical_player_id: row.eligible_slots for row in eligibilities
    }
    for player_id, slots in eligibility_by_player.items():
        if frozenset(slots) != bundle.strength_model.players[player_id].eligible_positions:
            raise ValueError(
                "simulation eligibility does not match strength-model eligibility"
            )
    primary_positions = _validate_primary_positions(
        projections,
        eligibility_by_player,
    )
    ecr_by_period = {
        snapshot.period: {
            row.canonical_player_id: row for row in snapshot.rankings
        }
        for snapshot in ecr
    }
    nfl_teams_by_player = {}
    for row in projections:
        nfl_teams_by_player.setdefault(row.canonical_player_id, set()).add(
            row.nfl_team_id
        )
    for waiver in bundle.waiver_pool.players:
        if names.get(waiver.canonical_player_id) != waiver.display_name:
            raise ValueError("waiver pool display name does not match player metadata")
        if eligibility_by_player.get(waiver.canonical_player_id) != waiver.eligible_slots:
            raise ValueError("waiver pool eligibility does not match calculation metadata")
        if primary_positions[waiver.canonical_player_id] != waiver.position:
            raise ValueError("waiver pool position does not match the projection grid")
        if nfl_teams_by_player.get(waiver.canonical_player_id) != {waiver.nfl_team_id}:
            raise ValueError("waiver pool NFL team does not match the projection grid")
        for period in (EcrPeriod.WEEKLY, EcrPeriod.REST_OF_SEASON):
            ranking = ecr_by_period[period].get(waiver.canonical_player_id)
            if (
                ranking is None
                or ranking.fantasypros_player_id != waiver.fantasypros_player_id
            ):
                raise ValueError("waiver pool player lacks exact FantasyPros ECR evidence")
        if (
            ecr_by_period[EcrPeriod.REST_OF_SEASON][
                waiver.canonical_player_id
            ].rank_ecr
            != waiver.rest_of_season_ecr_rank
        ):
            raise ValueError("waiver pool ROS ECR rank does not match its provenance")
    configured_providers = {
        row.provider for row in bundle.ensemble_config.provider_weights
    }
    provider_player_owners = {}
    for row in evidence:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("projection evidence identity does not match league state")
        if row.canonical_player_id not in projection_ids:
            raise ValueError(
                "projection evidence player is outside the calculation universe"
            )
        if row.provider not in configured_providers:
            raise ValueError(
                "projection evidence provider is outside the ensemble configuration"
            )
        provider_key = row.provider, row.provider_player_id
        previous_owner = provider_player_owners.setdefault(
            provider_key,
            row.canonical_player_id,
        )
        if previous_owner != row.canonical_player_id:
            raise ValueError(
                "one provider player identity maps to multiple calculation players"
            )
    ProjectionLineageIndex(projections, evidence)
    _validate_ensemble_config(bundle.ensemble_config, projections)
    player_nfl_teams = _validate_schedule(
        bundle.nfl_schedule,
        state,
        projections,
    )
    validate_weekly_projection_schedule(
        bundle.nfl_schedule,
        player_nfl_teams,
        evidence,
    )
    if bundle.strength_formula.formula_id != bundle.methodology_evidence.formula_id:
        raise ValueError("strength formula does not match methodology evidence")
    formula_reasons = formula_static_incompatibility_reasons(
        bundle.strength_formula,
        bundle.methodology_evidence.methodology_fingerprint,
        season=state.season,
        scoring_profile_id=state.scoring_profile_id,
    )
    if formula_reasons:
        raise ValueError(
            "strength formula is incompatible with methodology evidence: "
            + "; ".join(formula_reasons)
        )
    features = build_strength_features(
        ecr,
        projections,
        eligibilities,
        provider_names=tuple(
            row.provider for row in bundle.ensemble_config.provider_weights
        ),
        projection_evidence=evidence,
        remaining_week_scopes={
            player_id: tuple(
                row.week
                for row in bundle.nfl_schedule.team_weeks
                if row.nfl_team_id == nfl_team_id
                and row.week >= state.first_remaining_week
                and row.status is NflTeamWeekStatus.SCHEDULED
            )
            for player_id, nfl_team_id in player_nfl_teams.items()
        },
    )
    rebuilt_model = bundle.strength_formula.build_model(features, rosters)
    if rebuilt_model != bundle.strength_model:
        raise ValueError(
            "strength model does not match its formula, ECR, projections, and rosters"
        )


def _validate_ensemble_config(config, projections):
    weights = {row.provider: row.weight for row in config.provider_weights}
    for projection in projections:
        observations = {
            row.provider: row for row in projection.provider_observations
        }
        if set(observations) != set(weights):
            raise ValueError("ensemble provider set does not match ensemble_config")
        if any(
            observations[provider].weight != weight
            for provider, weight in weights.items()
        ):
            raise ValueError("ensemble provider weight does not match ensemble_config")
        if projection.minimum_observed_sources != config.minimum_observed_sources:
            raise ValueError("ensemble source quorum does not match ensemble_config")
        if config.position_stddev_floors.get(projection.position) != (
            projection.position_stddev_floor
        ):
            raise ValueError("ensemble uncertainty floor does not match ensemble_config")


def _validate_primary_positions(projections, eligibility_by_player):
    positions = {}
    for projection in projections:
        player_id = projection.canonical_player_id
        known = positions.setdefault(player_id, projection.position)
        if known != projection.position:
            raise ValueError("player primary position changes across projection weeks")
    for player_id, position in positions.items():
        if position not in eligibility_by_player[player_id]:
            raise ValueError(
                "player primary position is absent from calculation eligibility"
            )
    return positions


def _validate_schedule(schedule, state, projections):
    if schedule.season != state.season:
        raise ValueError("NFL schedule season does not match league state")
    validate_complete_regular_season(schedule)
    player_teams = {}
    for projection in projections:
        nfl_team_id = projection.nfl_team_id
        if not isinstance(nfl_team_id, str) or not nfl_team_id:
            raise ValueError("ensemble projection lacks an NFL team identity")
        known = player_teams.setdefault(projection.canonical_player_id, nfl_team_id)
        if known != nfl_team_id:
            raise ValueError("player NFL team changes across projection weeks")
        team_week = schedule.team_week(nfl_team_id, projection.week)
        if team_week.status is NflTeamWeekStatus.BYE:
            if projection.status is not ProjectionStatus.BYE:
                raise ValueError("ensemble projection conflicts with an NFL bye")
        elif (
            projection.status is not ProjectionStatus.OBSERVED
            or projection.nfl_game_id != team_week.nfl_game_id
            or projection.opponent_team_id != team_week.opponent_team_id
            or projection.is_home is not team_week.is_home
        ):
            raise ValueError("ensemble projection conflicts with the NFL schedule")
    return player_teams


def _typed(name, values, expected_type):
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if not rows or any(not isinstance(row, expected_type) for row in rows):
        raise ValueError(f"{name} must contain {expected_type.__name__} values")
    return rows


def _names(value):
    if not isinstance(value, Mapping) or not value:
        raise ValueError("player_names must be a non-empty mapping")
    if any(not isinstance(key, str) or not key or not isinstance(name, str) or not name.strip() for key, name in value.items()):
        raise ValueError("player_names must map non-empty IDs to non-empty names")
    return MappingProxyType(dict(sorted((key, name.strip()) for key, name in value.items())))


def _roster_from_record(value):
    row = _mapping("roster", value)
    if set(row) != {
        "capacity_exempt_player_ids",
        "current_size",
        "player_ids",
        "roster_cap",
        "team_id",
    }:
        raise ValueError("roster record fields are invalid")
    return TeamRoster(
        row["team_id"],
        tuple(_json_array("player_ids", row["player_ids"])),
        row["current_size"],
        row["roster_cap"],
        _json_array(
            "capacity_exempt_player_ids",
            row["capacity_exempt_player_ids"],
        ),
    )


def _eligibility_from_record(value):
    row = _mapping("eligibility", value)
    if set(row) != {"canonical_player_id", "eligible_slots"}:
        raise ValueError("eligibility record fields are invalid")
    return PlayerEligibility(row["canonical_player_id"], tuple(_json_array("eligible_slots", row["eligible_slots"])))


def _evidence_key(row):
    return (row.provider, row.canonical_player_id or "", row.season, getattr(row, "week", 0), row.provider_player_id)


def _json_array(name, value):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _mapping(name, value):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _reject_constant(value):
    raise ValueError(f"engine bundle contains non-finite JSON constant {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"engine bundle contains duplicate JSON key {key!r}")
        result[key] = value
    return result
