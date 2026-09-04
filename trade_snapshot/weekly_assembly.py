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
from .ecr_source import EcrHorizonEvidence
from .ensemble import EnsembleConfig
from .identity import IdentityRegistry, ProviderReference
from .identity_match import (
    ProviderIdentityLink,
    ProviderPlayerRecord,
    reconcile_player_identities,
)
from .league_ingest import NormalizedLeagueInputs, host_player_records, normalize_host_league_snapshot
from .league_source import VerifiedHostLeagueSnapshot
from .methodology import DEFAULT_POWER_METHODOLOGY, PowerMethodology
from .feature_engineering import (
    ProjectionAvailabilityRequirements,
    projection_availability_requirements,
)
from .fantasypros_benchmark import FantasyProsLeagueBenchmark
from .nfl_schedule import (
    NFL_REGULAR_SEASON_WEEKS,
    NflSchedule,
    NflTeamWeekStatus,
    validate_complete_regular_season,
)
from .player_lab_projection_builder import build_player_lab_projection_snapshot
from .player_lab_projections import PlayerLabProjectionSnapshot
from .positions import normalize_player_position
from .projection_schedule import (
    materialize_weekly_grid,
    normalize_ros_active_weeks,
    validate_weekly_projection_schedule,
)
from .projection_source import ProjectionSourceAttempt, ProjectionSourceManifest
from .projection_source_policy import (
    select_projection_sources,
    validate_no_composite_double_count,
    validate_selectable_projection_providers,
)
from .projections import (
    ProjectionStatus,
    RemainingSeasonProjection,
    WeeklyProjection,
    WeeklyProjectionOrigin,
)
from .role_design import build_calibration_roles
from .scenario_config import CorrelatedScenarioConfig, FactorLoadings
from .scenario_config import PlayerEligibility
from .source_manifest import WeeklySourceManifest
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
    league_binding_id: str | None = None,
    projection_source_attempts: Iterable[ProjectionSourceAttempt] | None = None,
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
    projection_providers = validate_selectable_projection_providers(
        {row.provider.value for row in projections}
    )
    selection = select_projection_sources(
        projection_providers,
        broad_consensus=broad_consensus,
        fantasypros_available="fantasypros" in projection_providers,
    )
    ensemble = ensemble_config or selection.ensemble_config()
    if not isinstance(ensemble, EnsembleConfig):
        raise ValueError("ensemble_config must be EnsembleConfig or None")
    configured_providers = {
        row.provider for row in ensemble.provider_weights
    }
    validate_selectable_projection_providers(configured_providers)
    validate_no_composite_double_count(configured_providers)
    if configured_providers != set(selection.providers):
        raise ValueError(
            "projection ensemble providers must exactly match the selected forecast sources"
        )
    _validate_artifact_dimensions(
        host_snapshot.season,
        host_snapshot.first_remaining_week,
        scoring,
        projections,
        ecr,
        ensemble,
    )
    _validate_capture_before_first_remaining_kickoff(
        host_snapshot,
        fantasypros_league,
        projections,
        ecr,
        nfl_schedule,
    )
    _validate_preseason_ros_capture_times(ecr, nfl_schedule)

    collected_identity_records = _dedupe_player_records(
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
    bootstrap_records, bootstrap_links = _fantasypros_bootstrap_identity_evidence(
        fantasypros_league,
        collected_identity_records,
    )
    identity_records = _dedupe_player_records(
        (*collected_identity_records, *bootstrap_records)
    )
    identities = reconcile_player_identities(
        identity_records,
        previous_identities,
        anchor_provider="fantasypros",
        verified_links=bootstrap_links,
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
    fantasypros_benchmark = FantasyProsLeagueBenchmark.from_capture(
        fantasypros_league,
        host_snapshot.snapshot_id,
        team_ids,
    )

    ecr_snapshots = _merge_ecr_artifacts(
        ecr,
        identities,
        snapshot_id=host_snapshot.snapshot_id,
        scoring_profile_id=host_snapshot.scoring_profile.scoring_profile_id,
    )
    remaining_nfl_weeks = _remaining_nfl_weeks(
        nfl_schedule,
        host_snapshot.first_remaining_week,
    )
    all_projection_evidence = _projection_evidence(
        projections,
        identities,
        snapshot_id=host_snapshot.snapshot_id,
        scoring_profile_id=host_snapshot.scoring_profile.scoring_profile_id,
        applicable_weeks=remaining_nfl_weeks,
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
        ensemble,
        power_methodology,
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
    projection_evidence = _calculation_projection_evidence(
        all_projection_evidence,
        computation_players,
        nfl_teams,
        nfl_schedule,
        provider_names=selection.providers,
    )
    projection_source_manifest = ProjectionSourceManifest.from_artifacts(
        projections,
        projection_evidence,
        attempts=projection_source_attempts,
        identities=identities,
    )
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
        source_manifest=WeeklySourceManifest.from_captures(
            host_snapshot,
            fantasypros_league,
            league_binding_id=league_binding_id,
        ),
        projection_source_manifest=projection_source_manifest,
        fantasypros_benchmark=fantasypros_benchmark,
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
    season, week, scoring, projections, ecr, ensemble_config
) -> None:
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
    projection_horizons = {row.horizon for row in projections}
    if RankingHorizon.WEEKLY not in projection_horizons:
        raise ValueError("projection artifacts must include weekly rows")
    providers = {row.provider.value for row in projections}
    configured = {row.provider for row in ensemble_config.provider_weights}
    if not configured.issubset(providers):
        raise ValueError("projection artifacts must cover every configured provider")


def _validate_preseason_ros_capture_times(ecr, nfl_schedule) -> None:
    fallback = tuple(
        row
        for row in ecr
        if row.source_details.horizon_evidence
        is EcrHorizonEvidence.PRESEASON_REST_OF_SEASON_PAGE
    )
    if not fallback:
        return
    week_one = tuple(
        row
        for row in nfl_schedule.team_weeks
        if row.week == 1 and row.status is NflTeamWeekStatus.SCHEDULED
    )
    if not week_one or any(row.kickoff_at is None for row in week_one):
        raise ValueError(
            "preseason ROS fallback requires complete NFL Week 1 kickoff times"
        )
    earliest_kickoff = min(row.kickoff_at for row in week_one)
    if any(_artifact_time(row.captured_at) >= earliest_kickoff for row in fallback):
        raise ValueError(
            "preseason ROS fallback cannot be used at or after the first NFL kickoff"
        )


def _validate_capture_before_first_remaining_kickoff(
    host_snapshot,
    fantasypros_league,
    projections,
    ecr,
    nfl_schedule,
) -> None:
    """Reject a mixed/partial current week until played-game inputs are modeled."""

    current_week = host_snapshot.first_remaining_week
    scheduled = tuple(
        row
        for row in nfl_schedule.team_weeks
        if row.week == current_week and row.status is NflTeamWeekStatus.SCHEDULED
    )
    known_kickoffs = tuple(
        row.kickoff_at for row in scheduled if row.kickoff_at is not None
    )
    if not known_kickoffs:
        return
    earliest_kickoff = min(known_kickoffs)
    capture_times = (
        host_snapshot.captured_at.astimezone(timezone.utc),
        _artifact_time(fantasypros_league.captured_at),
        *(_artifact_time(row.captured_at) for row in projections),
        *(_artifact_time(row.captured_at) for row in ecr),
    )
    if max(capture_times) >= earliest_kickoff:
        raise ValueError(
            "weekly collection began at or after the first kickoff in the first "
            "remaining week; completed-game scores and player usage are not yet "
            "modeled, so advance the remaining week or collect before kickoff"
        )


def _artifact_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (AttributeError, ValueError):
        raise ValueError("ECR captured_at must be an ISO-8601 timestamp") from None


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


_FANTASYPROS_POSITION_IDS = {
    "1": "QB",
    "2": "RB",
    "3": "WR",
    "4": "TE",
    "5": "K",
    "6": "DST",
}
_NFL_TEAM_IDS = frozenset(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH",
    }
)


def _fantasypros_bootstrap_identity_evidence(artifact, existing_records):
    """Turn captured provider crosswalks into explicit, verified identity links."""

    bootstrap = _league_payload(artifact, LeagueSourceKind.BOOTSTRAP)
    players = {row["player_id"]: row for row in bootstrap["players"]}
    rostered_ids = {
        player_id
        for roster in bootstrap["rosters"]
        for player_id in roster["player_ids"]
    }
    best_free_agent_ids = set(
        _league_payload(
            artifact, LeagueSourceKind.ANALYZER_INIT
        )["best_free_agent_ids"]
    )
    by_reference = {
        (row.provider, row.provider_player_id): row for row in existing_records
    }
    records = []
    links = []
    crosswalk_owner = {}
    for fantasypros_id in sorted(rostered_ids | best_free_agent_ids):
        raw = players[fantasypros_id]
        references = [ProviderReference("fantasypros", fantasypros_id)]
        for provider, field in (("espn", "espn_id"), ("yahoo", "yahoo_id")):
            provider_id = raw.get(field)
            if provider_id is None:
                continue
            key = provider, provider_id
            previous_owner = crosswalk_owner.get(key)
            if previous_owner is not None and previous_owner != fantasypros_id:
                raise ValueError(
                    f"FantasyPros bootstrap maps {key!r} to multiple players"
                )
            crosswalk_owner[key] = fantasypros_id
            references.append(ProviderReference(provider, provider_id))

        known = tuple(
            by_reference[reference.key]
            for reference in references
            if reference.key in by_reference
        )
        anchor = next(
            (row for row in known if row.provider == "fantasypros"),
            known[0] if known else None,
        )
        captured_position = _bootstrap_player_position(raw)
        captured_team = _bootstrap_player_team(raw)
        if anchor is None:
            if captured_position is None or captured_team is None:
                raise ValueError(
                    "FantasyPros roster contains a player without an exact identity: "
                    f"player {fantasypros_id!r} lacks usable position/team metadata "
                    "and a captured ESPN/Yahoo crosswalk"
                )
            anchor = ProviderPlayerRecord(
                "fantasypros",
                fantasypros_id,
                raw["name"],
                captured_position,
                captured_team,
            )
        if captured_position is not None and captured_position != anchor.position:
            raise ValueError(
                f"FantasyPros bootstrap position conflicts for player {fantasypros_id!r}"
            )
        if captured_team is not None and captured_team != anchor.nfl_team_id:
            raise ValueError(
                f"FantasyPros bootstrap NFL team conflicts for player {fantasypros_id!r}"
            )

        for reference in references:
            if reference.key in by_reference:
                continue
            record = ProviderPlayerRecord(
                reference.provider,
                reference.provider_player_id,
                anchor.display_name,
                anchor.position,
                anchor.nfl_team_id,
            )
            by_reference[reference.key] = record
            records.append(record)
        if len(references) > 1:
            links.append(
                ProviderIdentityLink(
                    tuple(references),
                    f"FantasyPros league bootstrap player {fantasypros_id}",
                )
            )
    return tuple(records), tuple(links)


def _bootstrap_player_position(row) -> str | None:
    values = []
    for name in ("position", "position_id"):
        value = row.get(name)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            text = str(value).strip().upper()
            text = _FANTASYPROS_POSITION_IDS.get(text, text)
            try:
                values.append(normalize_player_position(text, require_supported=True))
            except ValueError:
                pass
    raw_positions = row.get("positions")
    if isinstance(raw_positions, (list, tuple)):
        for value in raw_positions:
            try:
                values.append(normalize_player_position(value, require_supported=True))
            except ValueError:
                pass
    unique = tuple(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else None


def _bootstrap_player_team(row) -> str | None:
    value = row.get("team_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    normalized = {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"}.get(
        normalized, normalized
    )
    return normalized if normalized in _NFL_TEAM_IDS else None


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
        panels_by_position = {}
        for snapshot in rows:
            for panel in snapshot.expert_panels:
                previous = panels_by_position.get(panel.position)
                if previous is not None and previous != panel:
                    raise ValueError(
                        f"{horizon.value} ECR has conflicting expert panels "
                        f"for {panel.position}"
                    )
                panels_by_position[panel.position] = panel
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
        panels = tuple(
            panels_by_position[position]
            for position in sorted(panels_by_position)
        )
        expert_ids = tuple(sorted({
            expert_id
            for panel in panels
            for expert_id in panel.expert_ids
        }))
        result.append(
            EcrSnapshot(
                snapshot_id=snapshot_id,
                scoring_profile_id=scoring_profile_id,
                season=rows[0].season,
                as_of_week=rows[0].as_of_week,
                period=period,
                captured_at=captured_at,
                source_updated_at=source_updated_at,
                expert_ids=expert_ids,
                total_experts=len(expert_ids),
                rankings=rankings,
                expert_panels=panels,
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


def _remaining_nfl_weeks(nfl_schedule, first_remaining_week) -> tuple[int, ...]:
    validate_complete_regular_season(nfl_schedule)
    weeks = tuple(
        sorted(
            {
                row.week
                for row in nfl_schedule.team_weeks
                if row.week >= first_remaining_week
            }
        )
    )
    expected = tuple(
        week for week in NFL_REGULAR_SEASON_WEEKS if week >= first_remaining_week
    )
    if weeks != expected:
        raise ValueError(
            "NFL schedule must cover every remaining regular-season week"
        )
    return weeks


def _calculation_projection_evidence(
    rows,
    calculation_player_ids,
    player_nfl_team_ids,
    nfl_schedule,
    *,
    provider_names,
):
    """Retain calculation rows and make each ROS horizon player-specific."""

    providers = frozenset(provider_names)
    if not providers:
        raise ValueError("provider_names must not be empty")
    result = []
    for row in rows:
        player_id = row.canonical_player_id
        if player_id not in calculation_player_ids or row.provider not in providers:
            continue
        if isinstance(row, RemainingSeasonProjection):
            row = normalize_ros_active_weeks(
                row,
                nfl_team_id=player_nfl_team_ids[player_id],
                nfl_schedule=nfl_schedule,
            )
        result.append(row)
    return tuple(result)


def _waiver_pool(
    artifact,
    league_inputs,
    players,
    ecr_snapshots,
    projection_evidence,
    nfl_schedule,
    ensemble_config,
    power_methodology,
):
    state = league_inputs.league_state
    rostered = frozenset(
        player_id for roster in league_inputs.rosters for player_id in roster.player_ids
    )
    required_positions = required_waiver_positions(
        state.roster_rules.starting_lineup_slots,
    )
    materializable = _materializable_projection_player_ids(
        projection_evidence,
        {
            player_id: tuple(
                row.week
                for row in nfl_schedule.team_weeks
                if row.nfl_team_id == player.nfl_team_id
                and row.week >= state.first_remaining_week
                and row.status is NflTeamWeekStatus.SCHEDULED
            )
            for player_id, player in players.items()
        },
        calculation_weeks=state.remaining_regular_season_weeks,
        current_week=state.first_remaining_week,
        provider_names=tuple(
            row.provider for row in ensemble_config.provider_weights
        ),
        minimum_observed_sources=ensemble_config.minimum_observed_sources,
        requirements=projection_availability_requirements(
            (
                *power_methodology.residual_feature_names,
                *power_methodology.role_feature_names,
            ),
            (row.provider for row in ensemble_config.provider_weights),
        ),
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
    candidate_ids = {
        player_id
        for player_id in candidate_ids
        if _can_materialize_waiver_player(
            state,
            projection_evidence,
            player_id,
            players[player_id].nfl_team_id,
            tuple(row.provider for row in ensemble_config.provider_weights),
            nfl_schedule,
        )
    }
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
    rows,
    active_weeks_by_player,
    *,
    calculation_weeks,
    current_week,
    provider_names,
    minimum_observed_sources,
    requirements,
) -> set[str]:
    providers = tuple(provider_names)
    if (
        not providers
        or len(set(providers)) != len(providers)
        or any(not isinstance(provider, str) or not provider for provider in providers)
    ):
        raise ValueError("provider_names must contain unique provider names")
    if (
        type(minimum_observed_sources) is not int
        or not 1 <= minimum_observed_sources <= len(providers)
    ):
        raise ValueError("minimum_observed_sources is invalid")
    if not isinstance(requirements, ProjectionAvailabilityRequirements):
        raise ValueError(
            "requirements must be ProjectionAvailabilityRequirements"
        )
    try:
        calculation = frozenset(calculation_weeks)
    except TypeError:
        raise ValueError("calculation_weeks must be an iterable") from None
    if (
        not calculation
        or any(type(week) is not int or not 1 <= week <= 25 for week in calculation)
        or type(current_week) is not int
        or current_week not in calculation
    ):
        raise ValueError("calculation_weeks and current_week are invalid")
    required_providers = (
        requirements.current_providers | requirements.full_ros_providers
    )
    if not required_providers.issubset(providers):
        raise ValueError("formula-required providers must be configured providers")
    ros_observed = {}
    ros_by_pair = {}
    weekly_by_pair = defaultdict(dict)
    provider_identity = set()
    player_ids = set()
    for row in rows:
        if row.canonical_player_id is None:
            continue
        pair = row.canonical_player_id, row.provider
        player_ids.add(row.canonical_player_id)
        provider_identity.add(pair)
        if isinstance(row, RemainingSeasonProjection):
            if pair in ros_by_pair:
                raise ValueError("projection evidence contains duplicate ROS rows")
            ros_by_pair[pair] = row
            if row.status is ProjectionStatus.OBSERVED:
                ros_observed[pair] = row
        elif isinstance(row, WeeklyProjection):
            if row.week in weekly_by_pair[pair]:
                raise ValueError(
                    "projection evidence contains duplicate weekly rows"
                )
            weekly_by_pair[pair][row.week] = row
    result = set()
    for player_id in player_ids:
        active_weeks = frozenset(active_weeks_by_player.get(player_id, ()))
        if not active_weeks or any(
            (player_id, provider) not in provider_identity
            for provider in providers
        ):
            continue
        if any(
            not _provider_has_materializer_capture(
                (player_id, provider),
                calculation,
                ros_by_pair,
                weekly_by_pair,
            )
            or not _provider_schedule_statuses_are_valid(
                (player_id, provider),
                calculation,
                active_weeks,
                ros_observed,
                weekly_by_pair,
            )
            for provider in providers
        ):
            continue
        regular_weeks = active_weeks.intersection(calculation)
        week_sources = {
            week: {
                provider
                for provider in providers
                if _provider_week_available(
                    (player_id, provider),
                    week,
                    active_weeks,
                    weekly_by_pair,
                    ros_observed,
                )
            }
            for week in regular_weeks
        }
        if any(
            len(week_sources[week]) < minimum_observed_sources
            for week in regular_weeks
        ):
            continue
        full_horizon_sources = {
            provider
            for provider in providers
            if _provider_full_horizon_available(
                (player_id, provider),
                active_weeks,
                weekly_by_pair,
                ros_observed,
            )
        }
        if not requirements.full_ros_providers.issubset(full_horizon_sources):
            continue
        current_sources = week_sources.get(current_week, set())
        if not requirements.current_providers.issubset(current_sources):
            continue
        if requirements.ensemble_current and len(current_sources) < minimum_observed_sources:
            continue
        if (
            requirements.ensemble_full_ros
            and len(full_horizon_sources) < minimum_observed_sources
        ):
            continue
        result.add(player_id)
    return result


def _provider_has_materializer_capture(
    pair,
    calculation_weeks,
    ros_by_pair,
    weekly_by_pair,
):
    return pair in ros_by_pair or any(
        week in calculation_weeks for week in weekly_by_pair[pair]
    )


def _provider_schedule_statuses_are_valid(
    pair,
    calculation_weeks,
    active_weeks,
    ros_observed,
    weekly_by_pair,
):
    ros = ros_observed.get(pair)
    final_week = max(
        (*calculation_weeks, *(ros.applicable_weeks if ros is not None else ()))
    )
    first_week = min(calculation_weeks)
    for week, row in weekly_by_pair[pair].items():
        if not first_week <= week <= final_week:
            continue
        if week in active_weeks:
            if row.status is ProjectionStatus.BYE:
                return False
        elif row.status not in {
            ProjectionStatus.BYE,
            ProjectionStatus.NOT_PUBLISHED,
        }:
            return False
    return True


def _can_materialize_waiver_player(
    state,
    projection_evidence,
    player_id,
    nfl_team_id,
    providers,
    nfl_schedule,
):
    try:
        player_evidence = tuple(
            row
            for row in projection_evidence
            if row.canonical_player_id == player_id
        )
        validate_weekly_projection_schedule(
            nfl_schedule,
            {player_id: nfl_team_id},
            player_evidence,
        )
        evidence = _calculation_projection_evidence(
            player_evidence,
            {player_id},
            {player_id: nfl_team_id},
            nfl_schedule,
            provider_names=providers,
        )
        materialize_weekly_grid(
            state,
            evidence,
            player_ids=(player_id,),
            provider_names=providers,
            nfl_schedule=nfl_schedule,
            player_nfl_team_ids={player_id: nfl_team_id},
        )
    except ValueError:
        return False
    return True


def _provider_week_available(
    pair,
    week,
    active_weeks,
    weekly_by_pair,
    ros_observed,
):
    direct = weekly_by_pair[pair].get(week)
    if direct is not None and direct.status is ProjectionStatus.OBSERVED:
        return True
    if direct is not None and direct.status is not ProjectionStatus.NOT_PUBLISHED:
        return False
    ros = ros_observed.get(pair)
    if ros is None or week not in ros.applicable_weeks:
        return False
    return all(
        row.status in {
            ProjectionStatus.OBSERVED,
            ProjectionStatus.NOT_PUBLISHED,
        }
        for row_week, row in weekly_by_pair[pair].items()
        if row_week in active_weeks
    )


def _provider_full_horizon_available(
    pair,
    active_weeks,
    weekly_by_pair,
    ros_observed,
):
    ros = ros_observed.get(pair)
    if ros is not None and active_weeks.issubset(ros.applicable_weeks):
        return True
    return all(
        (row := weekly_by_pair[pair].get(week)) is not None
        and row.status is ProjectionStatus.OBSERVED
        and row.origin is WeeklyProjectionOrigin.PROVIDER_PUBLISHED
        for week in active_weeks
    )


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
