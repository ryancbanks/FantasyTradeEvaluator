"""Build an ESPN-hosted weekly engine without FantasyPros dependencies."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from .capture_normalize import (
    projection_evidence_from_artifact,
    projection_provider_records,
)
from .capture_schema import GenericTableArtifact, RankingHorizon
from .engine_bundle import EngineBundle
from .ensemble import EnsembleConfig
from .identity import IdentityRegistry
from .identity_match import ProviderPlayerRecord, reconcile_player_identities
from .independent_power_disclosure import IndependentPowerDisclosure
from .independent_strength import (
    INDEPENDENT_STRENGTH_POLICY_ID,
    build_independent_strength_model,
)
from .independent_waiver_pool import (
    IndependentWaiverCandidate,
    select_independent_waiver_pool,
)
from .league_ingest import (
    NormalizedLeagueInputs,
    host_player_records,
    normalize_host_league_snapshot,
)
from .league_source import VerifiedHostLeagueSnapshot
from .nfl_schedule import NflSchedule
from .positions import normalize_player_position
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
from .scenario_config import CorrelatedScenarioConfig, FactorLoadings, PlayerEligibility
from .trade_space import TeamRoster
from .waiver_pool import required_waiver_positions, waiver_eligible_slots
from .weekly_engine import prepare_projection_ensemble


@dataclass(frozen=True, slots=True)
class IndependentWeeklyEngine:
    bundle: EngineBundle
    identities: IdentityRegistry
    league_inputs: NormalizedLeagueInputs

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, EngineBundle):
            raise ValueError("bundle must be an EngineBundle")
        if self.bundle.methodology_mode != "independent":
            raise ValueError("bundle must use independent methodology")
        if not isinstance(self.identities, IdentityRegistry):
            raise ValueError("identities must be an IdentityRegistry")
        if not isinstance(self.league_inputs, NormalizedLeagueInputs):
            raise ValueError("league_inputs must be NormalizedLeagueInputs")


def assemble_independent_weekly_engine(
    *,
    host_snapshot: VerifiedHostLeagueSnapshot,
    projection_artifacts: Iterable[GenericTableArtifact],
    nfl_schedule: NflSchedule,
    scoring: str,
    expected_team_count: int,
    previous_identities: IdentityRegistry | None = None,
    ensemble_config: EnsembleConfig | None = None,
    scenario_config: CorrelatedScenarioConfig | None = None,
    broad_consensus: bool = False,
) -> IndependentWeeklyEngine:
    """Normalize public projections and seal a transparently independent bundle."""

    if not isinstance(host_snapshot, VerifiedHostLeagueSnapshot):
        raise ValueError("host_snapshot must be a VerifiedHostLeagueSnapshot")
    if not isinstance(nfl_schedule, NflSchedule):
        raise ValueError("nfl_schedule must be an NflSchedule")
    if nfl_schedule.season != host_snapshot.season:
        raise ValueError("NFL schedule does not describe the host season")
    if type(expected_team_count) is not int or not 2 <= expected_team_count <= 32:
        raise ValueError("expected_team_count must be an integer from 2 through 32")
    if host_snapshot.expected_team_count != expected_team_count:
        raise ValueError("host snapshot does not match expected_team_count")
    if previous_identities is not None and not isinstance(
        previous_identities, IdentityRegistry
    ):
        raise ValueError("previous_identities must be an IdentityRegistry or None")
    projections = _typed_artifacts(projection_artifacts)
    captured_providers = _validate_dimensions(host_snapshot, projections, scoring)
    selection = select_projection_sources(
        captured_providers,
        broad_consensus=broad_consensus,
        fantasypros_available=False,
    )
    providers = selection.providers
    ensemble = ensemble_config or selection.ensemble_config()
    configured = {row.provider for row in ensemble.provider_weights}
    validate_selectable_projection_providers(configured)
    validate_no_composite_double_count(configured)
    if configured != set(providers):
        raise ValueError("projection ensemble providers do not match selected sources")

    identity_records = _dedupe_records(
        (
            *host_player_records(host_snapshot),
            *(
                record
                for artifact in projections
                for record in projection_provider_records(
                    artifact, known_registry=previous_identities
                )
            ),
        )
    )
    identities = reconcile_player_identities(
        identity_records,
        previous_identities,
        anchor_provider="espn",
    )
    league = normalize_host_league_snapshot(host_snapshot, identities)
    state = league.league_state
    players = {row.canonical_player_id: row for row in identities.players}
    owned = frozenset(
        player_id for roster in league.rosters for player_id in roster.player_ids
    )
    all_evidence = _projection_evidence(
        projections,
        identities,
        nfl_schedule,
        snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        applicable_weeks=state.remaining_regular_season_weeks,
    )
    candidate_ids = _waiver_candidate_ids(
        all_evidence,
        players,
        owned,
        state.remaining_regular_season_weeks,
        nfl_schedule,
    )
    preliminary_ids = owned | candidate_ids
    complete_evidence = _complete_provider_evidence(
        all_evidence,
        preliminary_ids,
        providers,
        projections,
        identities,
        snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        season=state.season,
        applicable_weeks=state.remaining_regular_season_weeks,
    )
    positions = {
        player_id: normalize_player_position(
            players[player_id].position, require_supported=True
        )
        for player_id in sorted(preliminary_ids)
    }
    nfl_teams = {
        player_id: players[player_id].nfl_team_id
        for player_id in sorted(preliminary_ids)
    }
    eligibility = _eligibilities(
        league.eligibilities,
        candidate_ids,
        positions,
        state.roster_rules.starting_lineup_slots,
    )
    preliminary_ensemble = prepare_projection_ensemble(
        state,
        complete_evidence,
        player_positions=positions,
        player_nfl_team_ids=nfl_teams,
        nfl_schedule=nfl_schedule,
        ensemble_config=ensemble,
    )
    projected_points = _remaining_points(preliminary_ensemble)
    required_positions = required_waiver_positions(
        state.roster_rules.starting_lineup_slots,
        (players[player_id].position for player_id in owned),
    )
    waiver_pool = select_independent_waiver_pool(
        snapshot_id=state.snapshot_id,
        scoring_profile_id=state.scoring_profile_id,
        candidates=tuple(
            IndependentWaiverCandidate(
                player_id,
                players[player_id].display_name,
                positions[player_id],
                nfl_teams[player_id],
                eligibility[player_id].eligible_slots,
                _provider_ids(players[player_id], providers),
                projected_points[player_id],
            )
            for player_id in sorted(candidate_ids)
            if positions[player_id] in required_positions
        ),
        required_positions=required_positions,
        minimum_pool_size=state.roster_rules.roster_cap,
    )
    calculation_ids = owned | frozenset(waiver_pool.player_ids)
    final_eligibilities = tuple(
        eligibility[player_id] for player_id in sorted(calculation_ids)
    )
    final_positions = {
        player_id: positions[player_id] for player_id in sorted(calculation_ids)
    }
    final_nfl_teams = {
        player_id: nfl_teams[player_id] for player_id in sorted(calculation_ids)
    }
    final_projections = tuple(
        row
        for row in preliminary_ensemble
        if row.canonical_player_id in calculation_ids
    )
    final_evidence = tuple(
        row
        for row in complete_evidence
        if row.canonical_player_id in calculation_ids
    )
    roles = build_calibration_roles(
        state.roster_rules,
        league.rosters,
        final_positions,
        final_eligibilities,
    )
    captured_at = max(row.captured_at for row in final_evidence)
    strength_model = build_independent_strength_model(
        snapshot_id=state.snapshot_id,
        season=state.season,
        scoring_profile_id=state.scoring_profile_id,
        role_definitions=roles,
        projections=final_projections,
        eligibilities=final_eligibilities,
        rosters=league.rosters,
        captured_at=captured_at,
    )
    disclosure = IndependentPowerDisclosure.from_strength_model(
        strength_model,
        policy_id=INDEPENDENT_STRENGTH_POLICY_ID,
        provider_names=providers,
        captured_at=captured_at,
    )
    scenarios = scenario_config or CorrelatedScenarioConfig(
        10_000,
        20_260_901,
        FactorLoadings(0.0, 0.0, 0.0, 1.0),
    )
    bundle = EngineBundle(
        state=state,
        scoring_profile=host_snapshot.scoring_profile,
        rosters=league.rosters,
        projections=final_projections,
        eligibilities=final_eligibilities,
        scenario_config=scenarios,
        strength_model=strength_model,
        ecr_snapshots=(),
        projection_evidence=final_evidence,
        player_names={
            player_id: players[player_id].display_name
            for player_id in sorted(calculation_ids)
        },
        waiver_pool=waiver_pool,
        methodology_attestation=None,
        independent_power_disclosure=disclosure,
    )
    return IndependentWeeklyEngine(bundle, identities, league)


def _validate_dimensions(host, artifacts, scoring) -> tuple[str, ...]:
    if scoring not in {"STD", "HALF", "PPR"}:
        raise ValueError("scoring must be STD, HALF, or PPR")
    for row in artifacts:
        if row.season != host.season or row.scoring != scoring:
            raise ValueError("projection artifacts do not share host season and scoring")
        if row.week < host.first_remaining_week:
            raise ValueError("projection artifacts cannot predate the current week")
    providers = validate_selectable_projection_providers(
        {row.provider.value for row in artifacts}
    )
    if (
        "espn" not in providers
        or not set(providers) <= set(INDEPENDENT_PROJECTION_PROVIDERS)
    ):
        raise ValueError(
            "independent projections require ESPN and only supported public sources"
        )
    for provider in providers:
        horizons = {
            row.horizon for row in artifacts if row.provider.value == provider
        }
        if provider == "cbs":
            complete = horizons == {RankingHorizon.ROS}
        elif provider == "fftoday":
            # FFToday's public weekly IDP tables do not expose stable player
            # identities.  A ROS-only capture is therefore complete for an
            # IDP-only league and can be allocated across remaining games.
            complete = (
                RankingHorizon.ROS in horizons
                and horizons <= {RankingHorizon.WEEKLY, RankingHorizon.ROS}
            )
        else:
            complete = horizons == {
                RankingHorizon.WEEKLY,
                RankingHorizon.ROS,
            }
        if not complete:
            raise ValueError(f"{provider} projection period coverage is incomplete")
    return providers


def _projection_evidence(
    artifacts,
    identities,
    nfl_schedule,
    *,
    snapshot_id,
    scoring_profile_id,
    applicable_weeks,
):
    result = []
    for artifact in artifacts:
        result.extend(
            row
            for row in projection_evidence_from_artifact(
                artifact,
                identities,
                snapshot_id=snapshot_id,
                scoring_profile_id=scoring_profile_id,
                applicable_weeks=applicable_weeks,
                nfl_schedule=nfl_schedule,
            )
            if row.canonical_player_id is not None
        )
    keys = [
        (
            row.provider,
            row.canonical_player_id,
            row.week if hasattr(row, "week") else "ros",
        )
        for row in result
    ]
    if not result or len(set(keys)) != len(keys):
        raise ValueError("projection artifacts have missing or overlapping coverage")
    return tuple(result)


def _waiver_candidate_ids(evidence, players, owned, weeks, schedule):
    periods = defaultdict(set)
    observed = defaultdict(bool)
    for row in evidence:
        if row.provider != "espn":
            continue
        period = row.week if hasattr(row, "week") else "ros"
        periods[row.canonical_player_id].add(period)
        observed[row.canonical_player_id] |= row.status is ProjectionStatus.OBSERVED
    required_weeks = set(weeks)
    result = set()
    for player_id, coverage in periods.items():
        if (
            player_id in owned
            or player_id not in players
            or not observed[player_id]
            or not ("ros" in coverage or required_weeks <= coverage)
        ):
            continue
        player = players[player_id]
        try:
            normalize_player_position(player.position, require_supported=True)
            for week in weeks:
                schedule.team_week(player.nfl_team_id, week)
        except ValueError:
            continue
        result.add(player_id)
    return frozenset(result)


def _complete_provider_evidence(
    evidence,
    player_ids,
    providers,
    artifacts,
    identities,
    *,
    snapshot_id,
    scoring_profile_id,
    season,
    applicable_weeks,
):
    rows = [row for row in evidence if row.canonical_player_id in player_ids]
    coverage = {(row.provider, row.canonical_player_id) for row in rows}
    captured = {
        provider: max(
            _capture_time(row.captured_at)
            for row in artifacts
            if row.provider.value == provider
        )
        for provider in providers
    }
    by_player = {row.canonical_player_id: row for row in identities.players}
    for provider in providers:
        for player_id in sorted(player_ids):
            if (provider, player_id) in coverage:
                continue
            references = tuple(
                ref.provider_player_id
                for ref in by_player[player_id].provider_references
                if ref.provider == provider
            )
            provider_id = references[0] if len(references) == 1 else f"not-published:{player_id}"
            rows.append(
                RemainingSeasonProjection(
                    player_id,
                    snapshot_id,
                    scoring_profile_id,
                    provider,
                    provider_id,
                    season,
                    applicable_weeks,
                    ProjectionStatus.NOT_PUBLISHED,
                    RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                    captured[provider],
                )
            )
    return tuple(rows)


def _eligibilities(owned_rows, candidate_ids, positions, starting_slots):
    result = {row.canonical_player_id: row for row in owned_rows}
    result.update(
        {
            player_id: PlayerEligibility(
                player_id,
                waiver_eligible_slots(positions[player_id], starting_slots),
            )
            for player_id in candidate_ids
        }
    )
    return result


def _remaining_points(projections):
    result = defaultdict(float)
    for row in projections:
        if row.status is ProjectionStatus.OBSERVED:
            result[row.canonical_player_id] += row.projected_fantasy_points
        else:
            result.setdefault(row.canonical_player_id, 0.0)
    return result


def _provider_ids(player, providers) -> Mapping[str, str]:
    result = {
        row.provider: row.provider_player_id
        for row in player.provider_references
        if row.provider in providers
    }
    if "espn" not in result:
        raise ValueError("independent waiver candidate lacks an ESPN identity")
    return MappingProxyType(dict(sorted(result.items())))


def _typed_artifacts(values):
    if isinstance(values, (str, bytes)):
        raise ValueError("projection_artifacts must be an iterable")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError("projection_artifacts must be an iterable") from None
    if not rows or any(not isinstance(row, GenericTableArtifact) for row in rows):
        raise ValueError("projection_artifacts must contain GenericTableArtifact values")
    return rows


def _dedupe_records(values) -> tuple[ProviderPlayerRecord, ...]:
    result = {}
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


def _capture_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("captured_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("captured_at must include a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = (
    "IndependentWeeklyEngine",
    "assemble_independent_weekly_engine",
)
