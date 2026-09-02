"""Bind selected analyzer observations to canonical calibration inputs."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from ._scenario_random import content_id
from .analyzer_contract import (
    AnalyzerObservation,
    AnalyzerPeriod,
    AnalyzerTradeRequest,
    BundleFingerprint,
)
from .calibration_fit import (
    CalibrationCorpus,
    CalibrationTradeObservation,
    RosterPowerSample,
)
from .calibration_plan import (
    CalibrationExperiment,
    CalibrationExperimentPlan,
    CalibrationExperimentPurpose,
)
from .feature_engineering import StrengthFeatureSet
from .strength import RoleDefinition
from .trade_space import TeamRoster


__all__ = (
    "PreparedCalibrationEvidence",
    "analyzer_request_for_experiment",
    "prepare_calibration_evidence",
)


@dataclass(frozen=True, slots=True)
class PreparedCalibrationEvidence:
    corpus: CalibrationCorpus
    bundle: BundleFingerprint
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.corpus, CalibrationCorpus):
            raise ValueError("corpus must be a CalibrationCorpus")
        if not isinstance(self.bundle, BundleFingerprint):
            raise ValueError("bundle must be a BundleFingerprint")
        object.__setattr__(
            self,
            "evidence_id",
            content_id(
                "prepared-calibration-evidence",
                {
                    "bundle_sha256": self.bundle.sha256,
                    "bundle_url": self.bundle.url,
                    "corpus_id": self.corpus.corpus_id,
                },
            ),
        )


def analyzer_request_for_experiment(
    experiment: CalibrationExperiment,
    *,
    team_provider_ids: Mapping[str, str],
    player_provider_ids: Mapping[str, str],
    period: AnalyzerPeriod = AnalyzerPeriod.ROS,
) -> AnalyzerTradeRequest:
    """Translate a canonical experiment to one transport-free provider request."""

    if not isinstance(experiment, CalibrationExperiment):
        raise ValueError("experiment must be a CalibrationExperiment")
    teams = _id_mapping("team_provider_ids", team_provider_ids)
    players = _id_mapping("player_provider_ids", player_provider_ids)
    try:
        return AnalyzerTradeRequest(
            period=period,
            team1_id=teams[experiment.team1_id],
            team2_id=teams[experiment.team2_id],
            team1_gets=_provider_package(
                players[player] for player in experiment.team2_gives
            ),
            team2_gets=_provider_package(
                players[player] for player in experiment.team1_gives
            ),
        )
    except KeyError as error:
        raise ValueError(f"calibration provider mapping is missing {error.args[0]!r}") from None


def prepare_calibration_evidence(
    plan: CalibrationExperimentPlan,
    observations: Mapping[str, AnalyzerObservation],
    features: StrengthFeatureSet,
    roles: tuple[RoleDefinition, ...],
    rosters: tuple[TeamRoster, ...],
    *,
    team_provider_ids: Mapping[str, str],
    player_provider_ids: Mapping[str, str],
    period: AnalyzerPeriod = AnalyzerPeriod.ROS,
) -> PreparedCalibrationEvidence:
    """Create leakage-safe training/holdout rows from the designed experiment run."""

    if not isinstance(plan, CalibrationExperimentPlan):
        raise ValueError("plan must be a CalibrationExperimentPlan")
    if not isinstance(features, StrengthFeatureSet):
        raise ValueError("features must be a StrengthFeatureSet")
    if plan.snapshot_id != features.snapshot_id:
        raise ValueError("calibration plan and feature snapshot do not match")
    role_rows = tuple(roles)
    if (
        not role_rows
        or any(not isinstance(row, RoleDefinition) for row in role_rows)
        or tuple(row.role_id for row in role_rows) != plan.role_ids
    ):
        raise ValueError("roles do not match the calibration plan")
    roster_rows = tuple(rosters)
    by_team = {row.team_id: row for row in roster_rows}
    if len(by_team) != len(roster_rows) or set(by_team) != {
        plan.primary_team_id,
        *(row.team2_id for row in plan.experiments),
    }:
        raise ValueError("rosters do not exactly cover calibration experiment teams")
    if any(row.current_size != len(row.player_ids) for row in roster_rows):
        raise ValueError("calibration evidence requires complete team rosters")
    if not isinstance(observations, Mapping) or set(observations) != {
        row.experiment_id for row in plan.experiments
    }:
        raise ValueError("observations must exactly cover the calibration plan")
    teams = _id_mapping("team_provider_ids", team_provider_ids)
    players = _id_mapping("player_provider_ids", player_provider_ids)

    baseline_scores: dict[str, float] = {}
    training_samples = []
    heldouts = []
    bundles = set()
    for experiment in plan.experiments:
        observation = observations[experiment.experiment_id]
        if not isinstance(observation, AnalyzerObservation):
            raise ValueError("observations must contain AnalyzerObservation values")
        expected = analyzer_request_for_experiment(
            experiment,
            team_provider_ids=teams,
            player_provider_ids=players,
            period=period,
        )
        if observation.request != expected:
            raise ValueError(
                f"observation does not match experiment {experiment.experiment_id!r}"
            )
        bundles.add(observation.bundle)
        before1 = by_team[experiment.team1_id].player_ids
        before2 = by_team[experiment.team2_id].player_ids
        after1, after2 = _after_rosters(experiment, before1, before2)
        power1, power2 = observation.power.team1, observation.power.team2
        _baseline_score(baseline_scores, experiment.team1_id, power1.raw_before)
        _baseline_score(baseline_scores, experiment.team2_id, power2.raw_before)
        if experiment.purpose is CalibrationExperimentPurpose.TRAINING:
            training_samples.extend(
                (
                    RosterPowerSample(
                        f"{experiment.experiment_id}:team1",
                        experiment.team1_id,
                        after1,
                        power1.raw_after,
                    ),
                    RosterPowerSample(
                        f"{experiment.experiment_id}:team2",
                        experiment.team2_id,
                        after2,
                        power2.raw_after,
                    ),
                )
            )
        else:
            heldouts.append(
                CalibrationTradeObservation(
                    experiment.experiment_id,
                    experiment.team1_id,
                    experiment.team2_id,
                    before1,
                    after1,
                    before2,
                    after2,
                    power1.raw_before,
                    power1.raw_after,
                    power2.raw_before,
                    power2.raw_after,
                )
            )
    if len(bundles) != 1:
        raise ValueError("calibration observations use different analyzer bundles")
    if set(baseline_scores) != set(by_team):
        missing = min(set(by_team).difference(baseline_scores))
        raise ValueError(f"calibration observations never measured team {missing!r}")
    baseline_samples = tuple(
        RosterPowerSample(
            f"baseline:{team_id}", team_id, by_team[team_id].player_ids, score
        )
        for team_id, score in sorted(baseline_scores.items())
    )
    corpus = CalibrationCorpus(
        snapshot_id=features.snapshot_id,
        season=features.season,
        scoring_profile_id=features.scoring_profile_id,
        role_definitions=role_rows,
        player_features=features.player_features,
        baseline_rosters=roster_rows,
        samples=(*baseline_samples, *training_samples),
        held_out_trades=heldouts,
    )
    return PreparedCalibrationEvidence(corpus, next(iter(bundles)))


def _after_rosters(experiment, before1, before2):
    left, right = set(before1), set(before2)
    if not set(experiment.team1_gives) <= left or not set(experiment.team2_gives) <= right:
        raise ValueError("calibration experiment player ownership does not match rosters")
    after1 = tuple(sorted(left.difference(experiment.team1_gives).union(experiment.team2_gives)))
    after2 = tuple(sorted(right.difference(experiment.team2_gives).union(experiment.team1_gives)))
    return after1, after2


def _baseline_score(scores, team_id, value):
    if team_id in scores and scores[team_id] != value:
        raise ValueError(
            f"baseline power changed during calibration capture for team {team_id!r}"
        )
    scores[team_id] = value


def _id_mapping(name, value):
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result = {}
    for canonical, provider in value.items():
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(f"{name} keys must be non-empty strings")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"{name} values must be non-empty strings")
        result[canonical] = provider
    if len(set(result.values())) != len(result):
        raise ValueError(f"{name} provider IDs must be unique")
    return result


def _provider_package(values):
    rows = tuple(values)
    return tuple(
        sorted(
            rows,
            key=lambda value: (0, int(value)) if value.isdecimal() else (1, value),
        )
    )
