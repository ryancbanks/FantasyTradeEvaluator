"""Join verified weekly captures into one refresh-ready local evidence set.

This module is deliberately browser-free.  Volatile page adapters produce the
typed artifacts; this boundary proves that they describe one league/week,
resolves every rostered identity, and supplies the exact inputs consumed by the
offline engine.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from ._analyzer_types import BundleFingerprint
from .capture_normalize import (
    ecr_provider_records,
    ecr_snapshot_from_artifact,
    projection_evidence_from_artifact,
    projection_provider_records,
)
from .capture_schema import (
    FantasyProsECRArtifact,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    LeagueSourceKind,
    RankingHorizon,
)
from .ecr import EcrPeriod, EcrSnapshot
from .ensemble import EnsembleConfig
from .identity import IdentityRegistry
from .identity_match import ProviderPlayerRecord, reconcile_player_identities
from .league_ingest import NormalizedLeagueInputs, host_player_records, normalize_host_league_snapshot
from .league_source import VerifiedHostLeagueSnapshot
from .methodology import DEFAULT_POWER_METHODOLOGY, PowerMethodology
from .nfl_schedule import NflSchedule
from .positions import normalize_player_position
from .player_lab_projection_builder import build_player_lab_projection_snapshot
from .player_lab_projections import PlayerLabProjectionSnapshot
from .projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
)
from .projection_source_policy import (
    INDEPENDENT_PROJECTION_PROVIDERS,
    select_projection_sources,
    validate_no_composite_double_count,
    validate_selectable_projection_providers,
)
from .role_design import build_calibration_roles
from .scenario_config import CorrelatedScenarioConfig, FactorLoadings
from .scenario_config import PlayerEligibility
from .waiver_pool import (
    WaiverCandidate,
    required_waiver_positions,
    select_waiver_pool,
    waiver_eligible_slots,
)
from .weekly_refresh import WeeklyRefreshEvidence


@dataclass(frozen=True, slots=True)
class AssembledWeeklyEvidence:
    """Refresh evidence plus the exact provider mappings needed for calibration."""

    evidence: WeeklyRefreshEvidence
    identities: IdentityRegistry
    league_inputs: NormalizedLeagueInputs
    fantasypros_team_ids: Mapping[str, str]
    fantasypros_player_ids: Mapping[str, str]
    player_lab_projections: PlayerLabProjectionSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, WeeklyRefreshEvidence):
            raise ValueError("evidence must be WeeklyRefreshEvidence")
        if not isinstance(self.identities, IdentityRegistry):
            raise ValueError("identities must be IdentityRegistry")
        if not isinstance(self.league_inputs, NormalizedLeagueInputs):
            raise ValueError("league_inputs must be NormalizedLeagueInputs")
        if not isinstance(self.player_lab_projections, PlayerLabProjectionSnapshot):
            raise ValueError(
                "player_lab_projections must be a PlayerLabProjectionSnapshot"
            )
        object.__setattr__(
            self,
            "fantasypros_team_ids",
            _frozen_mapping("fantasypros_team_ids", self.fantasypros_team_ids),
        )
        object.__setattr__(
            self,
            "fantasypros_player_ids",
            _frozen_mapping("fantasypros_player_ids", self.fantasypros_player_ids),
        )


def assemble_weekly_refresh_evidence(
    *,
    host_snapshot: VerifiedHostLeagueSnapshot,
    fantasypros_league: FantasyProsLeagueArtifact,
    projection_artifacts: Iterable[GenericTableArtifact],
    ecr_artifacts: Iterable[FantasyProsECRArtifact],
    nfl_schedule: NflSchedule,
    analyzer_bundle: BundleFingerprint,
    response_schema_sha256: str,
    scoring: str,
    expected_team_count: int = 18,
    previous_identities: IdentityRegistry | None = None,
    ensemble_config: EnsembleConfig | None = None,
    scenario_config: CorrelatedScenarioConfig | None = None,
    power_methodology: PowerMethodology = DEFAULT_POWER_METHODOLOGY,
    broad_consensus: bool = False,
) -> AssembledWeeklyEvidence:
    """Build one complete local-engine input or fail before publishing anything."""

    if not isinstance(host_snapshot, VerifiedHostLeagueSnapshot):
        raise ValueError("host_snapshot must be VerifiedHostLeagueSnapshot")
    if not isinstance(fantasypros_league, FantasyProsLeagueArtifact):
        raise ValueError("fantasypros_league must be FantasyProsLeagueArtifact")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be NflSchedule")
    if not isinstance(analyzer_bundle, BundleFingerprint):
        raise ValueError("analyzer_bundle must be BundleFingerprint")
    captured_bundle = BundleFingerprint(
        fantasypros_league.bundle_url,
        fantasypros_league.bundle_sha256,
    )
    if analyzer_bundle != captured_bundle:
        raise ValueError(
            "analyzer_bundle must match the bundle captured with the FantasyPros league"
        )
    if not isinstance(power_methodology, PowerMethodology):
        raise ValueError("power_methodology must be PowerMethodology")
    if previous_identities is not None and not isinstance(previous_identities, IdentityRegistry):
        raise ValueError("previous_identities must be IdentityRegistry or None")
    if type(expected_team_count) is not int or expected_team_count < 2:
        raise ValueError("expected_team_count must be an integer of at least 2")
    if host_snapshot.expected_team_count != expected_team_count:
        raise ValueError("host snapshot does not match expected_team_count")
    if fantasypros_league.team_count != expected_team_count:
        raise ValueError("FantasyPros league does not match expected_team_count")
    if (
        host_snapshot.season != fantasypros_league.season
        or host_snapshot.first_remaining_week != fantasypros_league.week
    ):
        raise ValueError("host and FantasyPros captures do not describe one season/week")
    if nfl_schedule.season != host_snapshot.season:
        raise ValueError("NFL schedule does not describe the host season")

    scoring = _scoring(scoring)
    _validate_fantasypros_league_scoring(fantasypros_league, scoring)
    projections = _typed_tuple(
        "projection_artifacts", projection_artifacts, GenericTableArtifact
    )
    ecr = _typed_tuple("ecr_artifacts", ecr_artifacts, FantasyProsECRArtifact)
    projection_providers = _validate_artifact_dimensions(
        host_snapshot.season,
        host_snapshot.first_remaining_week,
        scoring,
        projections,
        ecr,
    )
    selection = select_projection_sources(
        projection_providers,
        broad_consensus=broad_consensus,
        fantasypros_available=True,
    )
    ensemble = ensemble_config or selection.ensemble_config()
    configured_providers = {
        row.provider for row in ensemble.provider_weights
    }
    validate_selectable_projection_providers(configured_providers)
    validate_no_composite_double_count(configured_providers)
    if configured_providers != set(selection.providers):
        raise ValueError(
            "projection ensemble providers must exactly match the selected forecast sources"
        )

    identity_records = _dedupe_player_records(
        (
            *host_player_records(host_snapshot),
            *(row for artifact in ecr for row in ecr_provider_records(artifact)),
            *(
                row
                for artifact in projections
                for row in projection_provider_records(
                    artifact, known_registry=previous_identities
                )
            ),
        )
    )
    identities = reconcile_player_identities(
        identity_records,
        previous_identities,
        anchor_provider="fantasypros",
    )
    league_inputs = normalize_host_league_snapshot(host_snapshot, identities)
    rostered = frozenset(
        player_id for roster in league_inputs.rosters for player_id in roster.player_ids
    )
    team_ids = _fantasypros_team_ids(
        fantasypros_league,
        league_inputs,
        identities,
    )

    ecr_snapshots = _merge_ecr_artifacts(
        ecr,
        identities,
        snapshot_id=host_snapshot.snapshot_id,
        scoring_profile_id=host_snapshot.scoring_profile.scoring_profile_id,
    )
    all_projection_evidence = _projection_evidence(
        projections,
        identities,
        snapshot_id=host_snapshot.snapshot_id,
        scoring_profile_id=host_snapshot.scoring_profile.scoring_profile_id,
        applicable_weeks=league_inputs.league_state.remaining_regular_season_weeks,
        nfl_schedule=nfl_schedule,
    )
    players = {row.canonical_player_id: row for row in identities.players}
    waiver_pool = _waiver_pool(
        fantasypros_league,
        league_inputs,
        players,
        ecr_snapshots,
        all_projection_evidence,
        nfl_schedule,
        selection.providers,
        selection.minimum_observed_sources,
    )
    computation_players = rostered | frozenset(waiver_pool.player_ids)
    player_lab_projections = build_player_lab_projection_snapshot(
        state=league_inputs.league_state,
        projection_evidence=all_projection_evidence,
        player_names={
            player_id: player.display_name for player_id, player in players.items()
        },
        player_positions={
            player_id: player.position for player_id, player in players.items()
        },
        player_nfl_team_ids={
            player_id: player.nfl_team_id for player_id, player in players.items()
        },
        nfl_schedule=nfl_schedule,
        ensemble_config=ensemble,
        exclude_player_ids=computation_players,
    )
    player_ids = _fantasypros_player_ids(computation_players, identities)
    projection_evidence = tuple(
        row
        for row in all_projection_evidence
        if row.canonical_player_id in computation_players
    )
    required_evidence_providers = tuple(dict.fromkeys(
        (*selection.providers, "fantasypros")
    ))
    projection_evidence = _complete_projection_evidence(
        projection_evidence,
        computation_players,
        required_evidence_providers,
        projections,
        snapshot_id=host_snapshot.snapshot_id,
        scoring_profile_id=host_snapshot.scoring_profile.scoring_profile_id,
        season=host_snapshot.season,
        applicable_weeks=league_inputs.league_state.remaining_regular_season_weeks,
    )
    positions = {
        player_id: normalize_player_position(players[player_id].position)
        for player_id in sorted(computation_players)
    }
    names = {
        player_id: players[player_id].display_name
        for player_id in sorted(computation_players)
    }
    nfl_teams = {
        player_id: players[player_id].nfl_team_id
        for player_id in sorted(computation_players)
    }
    eligibility_by_player = {
        row.canonical_player_id: row for row in league_inputs.eligibilities
    }
    eligibility_by_player.update(
        {
            row.canonical_player_id: PlayerEligibility(
                row.canonical_player_id, row.eligible_slots
            )
            for row in waiver_pool.players
        }
    )
    eligibilities = tuple(
        eligibility_by_player[player_id] for player_id in sorted(computation_players)
    )
    roles = build_calibration_roles(
        league_inputs.league_state.roster_rules,
        league_inputs.rosters,
        positions,
        eligibilities,
    )
    scenarios = scenario_config or CorrelatedScenarioConfig(
        10_000,
        20_260_901,
        FactorLoadings(0.0, 0.0, 0.0, 1.0),
    )
    evidence = WeeklyRefreshEvidence(
        state=league_inputs.league_state,
        scoring_profile=host_snapshot.scoring_profile,
        rosters=league_inputs.rosters,
        projection_evidence=projection_evidence,
        nfl_schedule=nfl_schedule,
        ecr_snapshots=ecr_snapshots,
        eligibilities=eligibilities,
        player_positions=positions,
        player_nfl_team_ids=nfl_teams,
        player_names=names,
        ensemble_config=ensemble,
        scenario_config=scenarios,
        analyzer_bundle=analyzer_bundle,
        response_schema_sha256=response_schema_sha256,
        power_methodology=power_methodology,
        role_definitions=roles,
        waiver_pool=waiver_pool,
    )
    return AssembledWeeklyEvidence(
        evidence,
        identities,
        league_inputs,
        team_ids,
        player_ids,
        player_lab_projections,
    )


def _validate_artifact_dimensions(
    season, week, scoring, projections, ecr
) -> tuple[str, ...]:
    for row in (*projections, *ecr):
        if row.season != season or row.scoring != scoring:
            raise ValueError("weekly artifacts do not share season and scoring")
        if row.week < week:
            raise ValueError("weekly artifacts cannot predate the first remaining week")
    horizons = {row.horizon for row in ecr}
    if horizons != {RankingHorizon.WEEKLY, RankingHorizon.ROS}:
        raise ValueError("ECR artifacts must include weekly and rest-of-season rankings")
    if any(row.week != week for row in ecr):
        raise ValueError("ECR artifacts must use the first remaining week")
    providers = validate_selectable_projection_providers(
        {row.provider.value for row in projections}
    )
    allowed = {"fantasypros", *INDEPENDENT_PROJECTION_PROVIDERS}
    if "fantasypros" not in providers or not set(providers) <= allowed:
        raise ValueError(
            "projection artifacts must include FantasyPros and only supported sources"
        )
    required_horizons = {RankingHorizon.WEEKLY, RankingHorizon.ROS}
    for provider in providers:
        horizons = {
            row.horizon for row in projections if row.provider.value == provider
        }
        if provider == "cbs":
            complete = horizons == {RankingHorizon.ROS}
        elif provider == "fftoday":
            # Weekly IDP pages cannot be joined safely because they omit
            # stable player identities.  ROS-only is complete when those are
            # the only FFToday positions requested.
            complete = (
                RankingHorizon.ROS in horizons
                and horizons <= required_horizons
            )
        else:
            complete = horizons == required_horizons
        if not complete:
            raise ValueError(
                f"{provider} projection artifacts have incomplete period coverage"
            )
    return providers


def _dedupe_player_records(values) -> tuple[ProviderPlayerRecord, ...]:
    result: dict[tuple[str, str], ProviderPlayerRecord] = {}
    for row in values:
        if not isinstance(row, ProviderPlayerRecord):
            raise ValueError("identity evidence contains an invalid player record")
        key = row.provider, row.provider_player_id
        previous = result.get(key)
        if previous is not None and previous != row:
            raise ValueError(f"provider identity metadata conflicts for {key!r}")
        result[key] = row
    if not result:
        raise ValueError("identity evidence cannot be empty")
    return tuple(result[key] for key in sorted(result))


def _fantasypros_player_ids(calculation_player_ids, identities) -> Mapping[str, str]:
    by_canonical = {row.canonical_player_id: row for row in identities.players}
    result = {}
    for canonical in sorted(calculation_player_ids):
        player = by_canonical[canonical]
        references = tuple(
            row.provider_player_id
            for row in player.provider_references
            if row.provider == "fantasypros"
        )
        if len(references) != 1:
            raise ValueError(
                f"calculation player {canonical!r} lacks one exact FantasyPros ID"
            )
        result[canonical] = references[0]
    return MappingProxyType(result)


def _fantasypros_team_ids(artifact, league_inputs, identities) -> Mapping[str, str]:
    bootstrap = _league_payload(artifact, LeagueSourceKind.BOOTSTRAP)
    fp_rosters = {}
    for row in bootstrap["rosters"]:
        canonical = []
        for provider_id in row["player_ids"]:
            identity = identities.lookup("fantasypros", provider_id)
            if identity is None:
                raise ValueError(
                    "FantasyPros roster contains a player without an exact identity"
                )
            canonical.append(identity.canonical_player_id)
        fp_rosters[row["team_id"]] = frozenset(canonical)
    local_rosters = {
        row.team_id: frozenset(row.player_ids) for row in league_inputs.rosters
    }
    result = {}
    used = set()
    for canonical_team_id, players in sorted(local_rosters.items()):
        matches = [team_id for team_id, roster in fp_rosters.items() if roster == players]
        if len(matches) != 1 or matches[0] in used:
            raise ValueError(
                "FantasyPros and host rosters do not prove a one-to-one team mapping"
            )
        used.add(matches[0])
        result[canonical_team_id] = matches[0]
    if len(used) != artifact.team_count:
        raise ValueError("FantasyPros team mapping does not cover the complete league")
    return MappingProxyType(result)


def _merge_ecr_artifacts(
    artifacts,
    identities,
    *,
    snapshot_id,
    scoring_profile_id,
) -> tuple[EcrSnapshot, EcrSnapshot]:
    by_horizon = defaultdict(list)
    for artifact in artifacts:
        by_horizon[artifact.horizon].append(
            ecr_snapshot_from_artifact(
                artifact,
                identities,
                snapshot_id=snapshot_id,
                scoring_profile_id=scoring_profile_id,
            )
        )
    result = []
    for horizon, period in (
        (RankingHorizon.WEEKLY, EcrPeriod.WEEKLY),
        (RankingHorizon.ROS, EcrPeriod.REST_OF_SEASON),
    ):
        rows = by_horizon[horizon]
        rankings = tuple(
            ranking
            for snapshot in rows
            for ranking in snapshot.rankings
        )
        if not rankings:
            raise ValueError(f"{horizon.value} ECR has no player coverage")
        canonical = [row.canonical_player_id for row in rankings]
        provider = [row.fantasypros_player_id for row in rankings]
        if len(set(canonical)) != len(canonical) or len(set(provider)) != len(provider):
            raise ValueError(f"{horizon.value} ECR position pages overlap")
        captured_at = max(row.captured_at for row in rows)
        source_times = [row.source_updated_at for row in rows]
        source_updated_at = (
            None if any(value is None for value in source_times) else min(source_times)
        )
        expert_ids = tuple(
            sorted({expert for row in rows for expert in row.expert_ids})
        )
        result.append(
            EcrSnapshot(
                snapshot_id,
                scoring_profile_id,
                rows[0].season,
                rows[0].as_of_week,
                period,
                captured_at,
                source_updated_at,
                expert_ids,
                len(expert_ids),
                rankings,
            )
        )
    return tuple(result)


def _projection_evidence(
    artifacts,
    identities,
    *,
    snapshot_id,
    scoring_profile_id,
    applicable_weeks,
    nfl_schedule,
):
    result = []
    for artifact in artifacts:
        rows = projection_evidence_from_artifact(
            artifact,
            identities,
            snapshot_id=snapshot_id,
            scoring_profile_id=scoring_profile_id,
            applicable_weeks=applicable_weeks,
            nfl_schedule=nfl_schedule,
        )
        result.extend(row for row in rows if row.canonical_player_id is not None)
    if not result:
        raise ValueError("projection artifacts have no resolved-player coverage")
    keys = []
    for row in result:
        period = row.week if hasattr(row, "week") else "ros"
        keys.append((row.provider, row.canonical_player_id, period))
    if len(set(keys)) != len(keys):
        raise ValueError("projection artifacts overlap for one provider/player/period")
    return tuple(result)


def _complete_projection_evidence(
    evidence,
    player_ids,
    providers,
    artifacts,
    *,
    snapshot_id,
    scoring_profile_id,
    season,
    applicable_weeks,
):
    """Represent selected-source noncoverage explicitly instead of imputing it."""

    rows = list(evidence)
    required = set(providers)
    covered = {(row.provider, row.canonical_player_id) for row in rows}
    captured_by_provider = {
        provider: max(
            _capture_time(row.captured_at)
            for row in artifacts
            if row.provider.value == provider
        )
        for provider in required
    }
    for provider in sorted(required):
        for player_id in sorted(player_ids):
            if (provider, player_id) in covered:
                continue
            rows.append(
                RemainingSeasonProjection(
                    canonical_player_id=player_id,
                    snapshot_id=snapshot_id,
                    scoring_profile_id=scoring_profile_id,
                    provider=provider,
                    provider_player_id=f"not-published:{player_id}",
                    season=season,
                    applicable_weeks=applicable_weeks,
                    status=ProjectionStatus.NOT_PUBLISHED,
                    origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                    captured_at=captured_by_provider[provider],
                )
            )
    return tuple(rows)


def _waiver_pool(
    artifact,
    league_inputs,
    players,
    ecr_snapshots,
    projection_evidence,
    nfl_schedule,
    forecast_providers,
    minimum_forecast_sources,
):
    state = league_inputs.league_state
    rostered = frozenset(
        player_id for roster in league_inputs.rosters for player_id in roster.player_ids
    )
    required_positions = required_waiver_positions(
        state.roster_rules.starting_lineup_slots,
        (players[player_id].position for player_id in rostered),
    )
    materializable = _materializable_projection_player_ids(
        projection_evidence,
        state.remaining_regular_season_weeks,
        forecast_providers,
        minimum_forecast_sources,
        required_provider="fantasypros",
    )
    by_period = {row.period: row for row in ecr_snapshots}
    weekly = {
        row.canonical_player_id: row
        for row in by_period[EcrPeriod.WEEKLY].rankings
    }
    ros = {
        row.canonical_player_id: row
        for row in by_period[EcrPeriod.REST_OF_SEASON].rankings
    }
    candidate_ids = (
        set(weekly) & set(ros) & materializable & set(players)
    ).difference(rostered)
    candidates = []
    for player_id in sorted(candidate_ids):
        identity = players[player_id]
        position = normalize_player_position(identity.position, require_supported=True)
        if position not in required_positions or not _has_complete_schedule(
            nfl_schedule,
            identity.nfl_team_id,
            state.remaining_regular_season_weeks,
        ):
            continue
        candidates.append(
            WaiverCandidate(
                player_id,
                ros[player_id].fantasypros_player_id,
                identity.display_name,
                position,
                identity.nfl_team_id,
                waiver_eligible_slots(
                    position,
                    state.roster_rules.starting_lineup_slots,
                ),
                ros[player_id].rank_ecr,
            )
        )
    best_ids = _league_payload(
        artifact, LeagueSourceKind.ANALYZER_INIT
    )["best_free_agent_ids"]
    return select_waiver_pool(
        snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        candidates=candidates,
        fantasypros_best_player_ids=best_ids,
        required_positions=required_positions,
        minimum_pool_size=state.roster_rules.roster_cap,
    )


def _materializable_projection_player_ids(
    rows, weeks, providers, minimum_sources, *, required_provider
) -> set[str]:
    required_weeks = frozenset(weeks)
    coverage = defaultdict(lambda: defaultdict(set))
    for row in rows:
        period = row.week if hasattr(row, "week") else "ros"
        coverage[row.canonical_player_id][row.provider].add(period)
    def complete(periods, provider):
        available = periods.get(provider, ())
        return "ros" in available or required_weeks.issubset(available)

    return {
        player_id
        for player_id, provider_periods in coverage.items()
        if complete(provider_periods, required_provider)
        and sum(complete(provider_periods, provider) for provider in providers)
        >= minimum_sources
    }


def _has_complete_schedule(schedule, nfl_team_id, weeks) -> bool:
    try:
        for week in weeks:
            schedule.team_week(nfl_team_id, week)
    except ValueError:
        return False
    return True


def _capture_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("artifact captured_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("artifact captured_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_fantasypros_league_scoring(artifact, scoring) -> None:
    label = _league_payload(artifact, LeagueSourceKind.BOOTSTRAP)["league"]["scoring"]
    normalized = _scoring(label)
    if normalized != scoring:
        raise ValueError("FantasyPros league scoring does not match projection scoring")


def _league_payload(artifact, source_kind):
    source = next(row for row in artifact.sources if row.source is source_kind)
    return source.to_record()["body"]["payload"]


def _scoring(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("scoring must be STD, HALF, or PPR")
    token = value.strip().upper().replace("_", " ").replace("-", " ")
    aliases = {
        "STD": "STD",
        "STANDARD": "STD",
        "NON PPR": "STD",
        "HALF": "HALF",
        "HALF PPR": "HALF",
        "0.5 PPR": "HALF",
        "PPR": "PPR",
        "FULL PPR": "PPR",
    }
    try:
        return aliases[token]
    except KeyError:
        raise ValueError("scoring must be STD, HALF, or PPR") from None


def _typed_tuple(name, values, expected_type):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if not rows or any(not isinstance(row, expected_type) for row in rows):
        raise ValueError(f"{name} must contain {expected_type.__name__} values")
    return rows


def _frozen_mapping(name, value):
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


__all__ = ("AssembledWeeklyEvidence", "assemble_weekly_refresh_evidence")
