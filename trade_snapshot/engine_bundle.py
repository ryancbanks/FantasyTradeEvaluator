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
from .ensemble import EnsembleProjection, ensemble_from_record, ensemble_to_record
from .league_io import league_state_from_record, league_state_to_record
from .league_state import LeagueState
from .methodology_attestation import MethodologyAttestation
from .projection_io import projection_from_record, projection_to_record
from .projections import RemainingSeasonProjection, WeeklyProjection
from .scenario_config import CorrelatedScenarioConfig, PlayerEligibility
from .scoring import ScoringProfile
from .strength import StrengthModel
from .surrogate_disclosure import SurrogateDisclosure
from .trade_space import TeamRoster
from .waiver_pool import WaiverPool


_BUNDLE_SCHEMA_VERSION = 7


@dataclass(frozen=True, slots=True)
class EngineBundle:
    state: LeagueState
    scoring_profile: ScoringProfile
    rosters: tuple[TeamRoster, ...]
    projections: tuple[EnsembleProjection, ...]
    eligibilities: tuple[PlayerEligibility, ...]
    scenario_config: CorrelatedScenarioConfig
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
        return "exact" if self.methodology_attestation is not None else "surrogate"

    def _content_record(self) -> dict[str, object]:
        return {
            "ecr_snapshots": [row.to_record() for row in self.ecr_snapshots],
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
            "player_names": dict(self.player_names),
            "projection_evidence": [
                projection_to_record(row) for row in self.projection_evidence
            ],
            "projections": [ensemble_to_record(row) for row in self.projections],
            "rosters": [
                {
                    "current_size": row.current_size,
                    "player_ids": list(row.player_ids),
                    "reserve_slot_by_player": dict(row.reserve_slot_by_player),
                    "reserve_slot_counts": dict(row.reserve_slot_counts),
                    "roster_cap": row.roster_cap,
                    "team_id": row.team_id,
                }
                for row in self.rosters
            ],
            "scoring_profile": self.scoring_profile.to_record(),
            "scenario_config": self.scenario_config.to_record(),
            "strength_model": self.strength_model.to_record(),
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
            "eligibilities",
            "league_state",
            "methodology_attestation",
            "player_names",
            "projection_evidence",
            "projections",
            "rosters",
            "scoring_profile",
            "scenario_config",
            "strength_model",
            "surrogate_disclosure",
            "waiver_pool",
        }
        if not isinstance(record, Mapping) or set(record) != content_keys | {
            "kind",
            "schema_version",
            "bundle_id",
        }:
            raise ValueError("engine bundle record fields are invalid")
        if (
            record["kind"] != "fantasy_trade_engine_bundle"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != _BUNDLE_SCHEMA_VERSION
        ):
            raise ValueError("engine bundle kind or schema version is invalid")
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
            scenario_config=CorrelatedScenarioConfig.from_record(
                _mapping("scenario_config", record["scenario_config"])
            ),
            strength_model=StrengthModel.from_record(
                _mapping("strength_model", record["strength_model"])
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


_MAX_ENGINE_BUNDLE_BYTES = 256 * 1024 * 1024


def load_engine_bundle(path: str | os.PathLike[str]) -> EngineBundle:
    source = Path(path)
    try:
        if source.stat().st_size > _MAX_ENGINE_BUNDLE_BYTES:
            raise ValueError("engine bundle exceeds its size limit")
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
    if {row.team_id for row in rosters} != team_ids or len(rosters) != len(team_ids):
        raise ValueError("rosters must exactly cover league teams")
    if any(
        row.current_size != len(row.player_ids)
        or row.roster_cap != state.roster_rules.roster_cap
        or row.reserve_slot_counts != state.roster_rules.reserve_slot_counts
        for row in rosters
    ):
        raise ValueError("rosters must be complete and use the league roster capacities")
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
    for row in evidence:
        if (row.snapshot_id, row.scoring_profile_id, row.season) != identity:
            raise ValueError("projection evidence identity does not match league state")


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
        "current_size",
        "player_ids",
        "reserve_slot_by_player",
        "reserve_slot_counts",
        "roster_cap",
        "team_id",
    }:
        raise ValueError("roster record fields are invalid")
    return TeamRoster(
        row["team_id"],
        tuple(_json_array("player_ids", row["player_ids"])),
        row["current_size"],
        row["roster_cap"],
        _mapping("reserve_slot_by_player", row["reserve_slot_by_player"]),
        _mapping("reserve_slot_counts", row["reserve_slot_counts"]),
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
