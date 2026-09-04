from dataclasses import replace
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.test_search_runner import PLAYER_POINTS, components
from tests.ecr_fixtures import ecr_source_provenance
from tests.source_fixtures import (
    fantasypros_league_benchmark,
    projection_source_manifest,
    weekly_source_manifest,
)
from trade_snapshot.analyzer_contract import BundleFingerprint
from trade_snapshot.ecr import EcrExpertPanel, EcrPeriod, EcrPlayerRanking, EcrSnapshot
from trade_snapshot.engine_bundle import (
    EngineBundle,
    UnsupportedEngineBundleSchema,
    load_engine_bundle,
    save_engine_bundle,
)
from trade_snapshot.ensemble import (
    EnsembleConfig,
    EnsembleProjection,
    ProviderObservation,
    ProviderWeight,
)
from trade_snapshot.feature_engineering import build_strength_features
from trade_snapshot.methodology import PowerMethodology
from trade_snapshot.methodology_attestation import MethodologyAttestation
from trade_snapshot.methodology_reuse import (
    FormulaAction,
    FormulaReuseDecision,
    MethodologyFingerprint,
)
from trade_snapshot.league_state import RosterRules
from trade_snapshot.nfl_schedule import (
    NflSchedule,
    NflTeamWeek,
    NflTeamWeekStatus,
)
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
)
from trade_snapshot.scenario_config import PlayerEligibility
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.strength import CalibrationStatus, PlayerStrength, StrengthModel
from trade_snapshot.strength_calibration import CalibrationMetadata
from trade_snapshot.strength_formula import StrengthFormula
from trade_snapshot.trade_space import TeamRoster
from trade_snapshot.waiver_pool import WaiverPool, WaiverPoolPlayer, WaiverPoolSource


NOW = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
SCORING_PROFILE = ScoringProfile("espn", {"reception": 1, "passing_td": 4})
WAIVER_PLAYER_IDS = ("w1", "w2")
ALL_POINTS = {**PLAYER_POINTS, "w1": 4.0, "w2": 3.0}
URL = "https://cdn.fantasypros.com/assets/js/trade-analyzer.js"
SHA = "1" * 64
SCHEMA = "2" * 64


def ecr_snapshot(period):
    return EcrSnapshot(
        snapshot_id="snapshot-1",
        scoring_profile_id=SCORING_PROFILE.scoring_profile_id,
        season=2026,
        as_of_week=1,
        period=period,
        captured_at=NOW,
        source_updated_at=NOW,
        expert_ids=("9", "22"),
        total_experts=2,
        rankings=tuple(
            EcrPlayerRanking(
                player_id,
                str(index),
                "FLEX",
                index,
                index,
                index,
                index + 2,
                index + 1,
                1,
            )
            for index, player_id in enumerate(ALL_POINTS, start=1)
        ),
        expert_panels=(EcrExpertPanel(
            "FLEX",
            ("9", "22"),
            2,
            ecr_source_provenance(
                captured_at=NOW,
                source_updated_at=NOW,
                horizon=("weekly" if period is EcrPeriod.WEEKLY else "ros"),
                position="FLEX",
                source_player_count=len(ALL_POINTS),
            ),
        ),),
    )


def exact_formula_model_and_attestation(
    role_definitions,
    projections,
    eligibilities,
    ecr_snapshots,
    evidence,
    rosters,
    ensemble_config,
):
    calibration = CalibrationMetadata(
        URL,
        SHA,
        SCHEMA,
        NOW,
        CalibrationStatus.EXACT,
        100,
        0,
        1,
    )
    methodology = PowerMethodology(
        ("projection_fantasypros_full_ros_points",),
        ("presence",),
    )
    formula = StrengthFormula(
        "strength-fit-test",
        "snapshot-1",
        2026,
        SCORING_PROFILE.scoring_profile_id,
        role_definitions,
        {"projection_fantasypros_full_ros_points": 1},
        {
            role.role_id: {"presence": 0}
            for role in role_definitions
        },
        calibration,
        tuple(f"calibration-holdout-{index}" for index in range(100)),
        (1, 2, 3, 4),
    )
    features = build_strength_features(
        ecr_snapshots,
        projections,
        eligibilities,
        provider_names=("fantasypros",),
        projection_evidence=evidence,
        remaining_week_scopes={
            player_id: tuple(range(1, 19)) for player_id in ALL_POINTS
        },
    )
    exact_model = formula.build_model(features, rosters)
    fingerprint = MethodologyFingerprint(
        BundleFingerprint(URL, SHA),
        SCHEMA,
        methodology,
        exact_model.role_definitions,
    )
    decision = FormulaReuseDecision(
        FormulaAction.RECALIBRATE,
        ("test calibration",),
        fingerprint.fingerprint_id,
    )
    return formula, exact_model, MethodologyAttestation.from_refresh(
        formula=formula,
        strength_model=exact_model,
        methodology_fingerprint=fingerprint,
        formula_decision=decision,
        reuse_verification=None,
    )


def waiver_projection(template, player_id):
    return EnsembleProjection(
        player_id,
        template.snapshot_id,
        template.scoring_profile_id,
        template.season,
        template.week,
        "RB",
        ProjectionStatus.OBSERVED,
        (
            ProviderObservation(
                "fantasypros",
                f"fantasypros-{player_id}",
                ProjectionStatus.OBSERVED,
                ALL_POINTS[player_id],
                1,
            ),
        ),
        1,
        0,
        ALL_POINTS[player_id],
        0,
        0,
        f"NFL-{player_id}".upper(),
        f"G1-{player_id}",
        f"OPP-{player_id}",
        True,
    )


def waiver_pool():
    return WaiverPool(
        "snapshot-1",
        SCORING_PROFILE.scoring_profile_id,
        ("RB",),
        2,
        tuple(
            WaiverPoolPlayer(
                player_id,
                str(index),
                player_id.upper(),
                "RB",
                f"NFL-{player_id}".upper(),
                ("RB", "FLEX"),
                index,
                WaiverPoolSource.FANTASYPROS_BEST,
                source_order,
            )
            for source_order, (player_id, index) in enumerate(
                (("w1", 5), ("w2", 6)), start=1
            )
        ),
    )


def nfl_schedule_for(projections, weeks=tuple(range(1, 19))):
    rows = []
    by_player = {}
    for projection in projections:
        by_player.setdefault(projection.canonical_player_id, projection)
    for projection in by_player.values():
        for week in weeks:
            game_id = f"G{week}-{projection.canonical_player_id}"
            rows.extend(
                (
                    NflTeamWeek(
                        projection.nfl_team_id,
                        week,
                        NflTeamWeekStatus.SCHEDULED,
                        game_id,
                        projection.opponent_team_id,
                        projection.is_home,
                    ),
                    NflTeamWeek(
                        projection.opponent_team_id,
                        week,
                        NflTeamWeekStatus.SCHEDULED,
                        game_id,
                        projection.nfl_team_id,
                        not projection.is_home,
                    ),
                )
            )
    return NflSchedule(2026, NOW, "espn", tuple(rows))


def engine_bundle():
    runner = components(scoring_profile_id=SCORING_PROFILE.scoring_profile_id)
    baseline = runner.season_baseline
    bundle_state = replace(
        baseline.state,
        roster_rules=RosterRules(2, ("FLEX", "RB")),
    )
    projections = (
        *(
            replace(
                row,
                provider_observations=tuple(
                    replace(
                        observation,
                        provider="espn",
                        provider_player_id=f"espn-{row.canonical_player_id}",
                    )
                    for observation in row.provider_observations
                ),
            )
            for row in baseline.scenarios.projections
        ),
        *(waiver_projection(baseline.scenarios.projections[0], player_id)
          for player_id in WAIVER_PLAYER_IDS),
    )
    eligibilities = (
        *baseline.scenarios.eligibilities,
        *(PlayerEligibility(player_id, ("RB", "FLEX"))
          for player_id in WAIVER_PLAYER_IDS),
    )
    projections = tuple(
        replace(
            row,
            provider_observations=(
                replace(
                    row.provider_observations[0],
                    provider="fantasypros",
                    provider_player_id=f"fantasypros-{row.canonical_player_id}",
                ),
            ),
            nfl_game_id=f"G1-{row.canonical_player_id}",
        )
        for row in projections
    )
    projection_by_player = {
        row.canonical_player_id: row for row in projections
    }
    evidence = tuple(
        WeeklyProjection(
            canonical_player_id=player_id,
            snapshot_id="snapshot-1",
            scoring_profile_id=SCORING_PROFILE.scoring_profile_id,
            provider="fantasypros",
            provider_player_id=f"fantasypros-{player_id}",
            season=2026,
            week=1,
            status=ProjectionStatus.OBSERVED,
            captured_at=NOW,
            projected_fantasy_points=points,
            raw_projected_stats={"points": points, "rush_yards": points * 5},
            nfl_team_id=projection_by_player[player_id].nfl_team_id,
            nfl_game_id=projection_by_player[player_id].nfl_game_id,
            opponent_team_id=projection_by_player[player_id].opponent_team_id,
            is_home=projection_by_player[player_id].is_home,
        )
        for player_id, points in ALL_POINTS.items()
    ) + tuple(
        RemainingSeasonProjection(
            canonical_player_id=player_id,
            snapshot_id="snapshot-1",
            scoring_profile_id=SCORING_PROFILE.scoring_profile_id,
            provider="fantasypros",
            provider_player_id=f"fantasypros-{player_id}",
            season=2026,
            applicable_weeks=tuple(range(1, 19)),
            status=ProjectionStatus.OBSERVED,
            origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
            captured_at=NOW,
            projected_fantasy_points=points,
        )
        for player_id, points in ALL_POINTS.items()
    )
    ecr_snapshots = (
        ecr_snapshot(EcrPeriod.WEEKLY),
        ecr_snapshot(EcrPeriod.REST_OF_SEASON),
    )
    ensemble_config = EnsembleConfig(
        (ProviderWeight("fantasypros", 1),),
        1,
        {"FLEX": 0, "RB": 0},
    )
    nfl_schedule = nfl_schedule_for(projections)
    formula, strength_model, attestation = exact_formula_model_and_attestation(
        runner.prepared_strength.model.role_definitions,
        projections,
        eligibilities,
        ecr_snapshots,
        evidence,
        baseline.scenarios.rosters,
        ensemble_config,
    )
    return EngineBundle(
        state=bundle_state,
        scoring_profile=SCORING_PROFILE,
        rosters=baseline.scenarios.rosters,
        projections=projections,
        eligibilities=eligibilities,
        nfl_schedule=nfl_schedule,
        source_manifest=weekly_source_manifest(),
        projection_source_manifest=projection_source_manifest(evidence),
        fantasypros_benchmark=fantasypros_league_benchmark(
            captured_at=NOW,
            team_ids=("primary", "other"),
        ),
        ensemble_config=ensemble_config,
        scenario_config=baseline.scenarios.config,
        strength_formula=formula,
        strength_model=strength_model,
        ecr_snapshots=ecr_snapshots,
        projection_evidence=evidence,
        player_names={player_id: player_id.upper() for player_id in ALL_POINTS},
        waiver_pool=waiver_pool(),
        methodology_attestation=attestation,
    )


def rebuild_bundle_inputs(
    bundle,
    *,
    state=None,
    projections=None,
    projection_evidence=None,
    nfl_schedule=None,
    ensemble_config=None,
    ecr_snapshots=None,
):
    state = state or bundle.state
    projections = tuple(projections or bundle.projections)
    evidence = tuple(projection_evidence or bundle.projection_evidence)
    schedule = nfl_schedule or bundle.nfl_schedule
    config = ensemble_config or bundle.ensemble_config
    ecr = tuple(ecr_snapshots or bundle.ecr_snapshots)
    player_teams = {}
    for row in projections:
        player_teams.setdefault(row.canonical_player_id, row.nfl_team_id)
    features = build_strength_features(
        ecr,
        projections,
        bundle.eligibilities,
        provider_names=tuple(row.provider for row in config.provider_weights),
        projection_evidence=evidence,
        remaining_week_scopes={
            player_id: tuple(
                row.week
                for row in schedule.team_weeks
                if row.nfl_team_id == nfl_team_id
                and row.status is NflTeamWeekStatus.SCHEDULED
                and row.week >= state.first_remaining_week
            )
            for player_id, nfl_team_id in player_teams.items()
        },
    )
    model = bundle.strength_formula.build_model(features, bundle.rosters)
    attestation = replace(
        bundle.methodology_attestation,
        strength_model_id=model.model_id,
    )
    return replace(
        bundle,
        state=state,
        projections=projections,
        projection_evidence=evidence,
        projection_source_manifest=projection_source_manifest(evidence),
        nfl_schedule=schedule,
        ensemble_config=config,
        ecr_snapshots=ecr,
        strength_model=model,
        methodology_attestation=attestation,
    )


def ros_derived_bundle(*, explicit_not_published=False):
    """Build a bundle whose current value depends on a future direct source row."""

    bundle = engine_bundle()
    target = next(
        row for row in bundle.projections if row.canonical_player_id == "p1"
    )
    observation = target.provider_observations[0]
    derived_points = 5.0
    derived_projection = replace(
        target,
        provider_observations=(
            replace(
                observation,
                projected_fantasy_points=derived_points,
            ),
        ),
        projected_fantasy_points=derived_points,
    )
    source = next(
        row
        for row in bundle.projection_evidence
        if isinstance(row, WeeklyProjection) and row.canonical_player_id == "p1"
    )
    future_direct = replace(
        source,
        week=2,
        projected_fantasy_points=4.0,
        raw_projected_stats={"points": 4.0},
        nfl_game_id="G2-p1",
    )
    remaining = RemainingSeasonProjection(
        canonical_player_id=source.canonical_player_id,
        snapshot_id=source.snapshot_id,
        scoring_profile_id=source.scoring_profile_id,
        provider=source.provider,
        provider_player_id=source.provider_player_id,
        season=source.season,
        applicable_weeks=tuple(range(1, 19)),
        status=ProjectionStatus.OBSERVED,
        origin=RemainingSeasonOrigin.PROVIDER_PUBLISHED,
        captured_at=NOW,
        projected_fantasy_points=89.0,
        raw_projected_stats={"points": 89.0},
    )
    projections = tuple(
        derived_projection if row is target else row for row in bundle.projections
    )
    source_rows = ()
    if explicit_not_published:
        source_rows = (
            replace(
                source,
                status=ProjectionStatus.NOT_PUBLISHED,
                projected_fantasy_points=None,
                raw_projected_stats={},
            ),
        )
    evidence = tuple(
        replace(row, applicable_weeks=tuple(range(1, 19)))
        if isinstance(row, RemainingSeasonProjection)
        and row.canonical_player_id != "p1"
        else row
        for row in bundle.projection_evidence
        if row is not source
        and not (
            isinstance(row, RemainingSeasonProjection)
            and row.canonical_player_id == "p1"
        )
    ) + source_rows + (future_direct, remaining)
    return rebuild_bundle_inputs(
        bundle,
        projections=projections,
        projection_evidence=evidence,
        nfl_schedule=nfl_schedule_for(projections),
    )


class EngineBundleTests(unittest.TestCase):
    def test_methodology_status_is_holdout_validated_only_inside_attested_shape(self):
        attestation = engine_bundle().methodology_attestation
        self.assertEqual(
            attestation.power_result_status(
                outgoing_count=1,
                incoming_count=1,
                has_roster_adjustment=False,
            ),
            "holdout_validated",
        )
        for outgoing, incoming, adjusted in (
            (1, 2, False),
            (2, 1, False),
            (1, 1, True),
            (99, 99, False),
        ):
            with self.subTest(
                outgoing=outgoing, incoming=incoming, adjusted=adjusted
            ):
                self.assertEqual(
                    attestation.power_result_status(
                        outgoing_count=outgoing,
                        incoming_count=incoming,
                        has_roster_adjustment=adjusted,
                    ),
                    "extrapolated",
                )

    def test_strict_json_round_trip_and_atomic_file_persistence(self):
        bundle = engine_bundle()
        record = bundle.to_record()
        self.assertEqual(record["schema_version"], 10)
        self.assertIsNone(record["player_profiles"])
        self.assertIsNone(record["player_lab_projections"])
        self.assertEqual(
            record["projection_source_manifest"],
            bundle.projection_source_manifest.to_record(),
        )
        self.assertIsNone(record["surrogate_disclosure"])
        self.assertEqual(
            record["methodology_attestation"]["attestation_id"],
            bundle.methodology_attestation.attestation_id,
        )
        self.assertEqual(record["league_state"]["schema_version"], 3)
        self.assertEqual(
            record["league_state"]["remaining_matchups"][0][
                "team1_score_adjustment"
            ],
            0.0,
        )
        self.assertEqual(record["scoring_profile"], SCORING_PROFILE.to_record())
        json.dumps(record, allow_nan=False)
        self.assertEqual(EngineBundle.from_record(record), bundle)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "week-1.json"
            resolved = save_engine_bundle(bundle, path)
            loaded = load_engine_bundle(path)
            leftovers = tuple(path.parent.glob(".*.tmp.json"))
        self.assertEqual(resolved, path.resolve())
        self.assertEqual(loaded, bundle)
        self.assertEqual(leftovers, ())

    def test_rejects_oversized_bundle_before_reading_it(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, "oversized.json")
            path.write_text("{}", encoding="utf-8")
            with patch("trade_snapshot.engine_bundle._MAX_ENGINE_BUNDLE_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    load_engine_bundle(path)

    def test_loaded_bundle_rejects_reference_only_calculation_provider(self):
        bundle = engine_bundle()
        record = bundle.to_record()
        record["projections"][0]["provider_observations"][0]["provider"] = "ffa"

        with self.assertRaisesRegex(ValueError, "reference-only"):
            EngineBundle.from_record(record)

    def test_round_trip_preserves_typed_reserve_capacity_and_placement(self):
        baseline = engine_bundle()
        reserve_counts = {"IR": 1, "ROOKIE_RESERVE": 1}
        state = replace(
            baseline.state,
            roster_rules=RosterRules(
                baseline.state.roster_rules.roster_cap,
                baseline.state.roster_rules.starting_lineup_slots,
                reserve_counts,
            ),
        )
        capacity_only_rosters = tuple(
            TeamRoster(
                team_id=row.team_id,
                player_ids=row.player_ids,
                current_size=row.current_size,
                roster_cap=row.roster_cap,
                reserve_slot_counts=reserve_counts,
            )
            for row in baseline.rosters
        )
        capacity_only = replace(
            baseline,
            state=state,
            rosters=capacity_only_rosters,
        )
        placed_rosters = tuple(
            TeamRoster(
                team_id=row.team_id,
                player_ids=row.player_ids,
                current_size=row.current_size,
                roster_cap=row.roster_cap,
                reserve_slot_by_player=(
                    {"p2": "IR"}
                    if row.team_id == "primary"
                    else {"q2": "ROOKIE_RESERVE"}
                ),
                reserve_slot_counts=reserve_counts,
            )
            for row in baseline.rosters
        )
        with_reserves = replace(
            capacity_only,
            rosters=placed_rosters,
        )

        record = with_reserves.to_record()
        restored = EngineBundle.from_record(record)
        primary = next(row for row in restored.rosters if row.team_id == "primary")
        other = next(row for row in restored.rosters if row.team_id == "other")

        self.assertEqual(
            record["league_state"]["roster_rules"]["reserve_slot_counts"],
            reserve_counts,
        )
        primary_record = next(
            row for row in record["rosters"] if row["team_id"] == "primary"
        )
        self.assertEqual(primary_record["reserve_slot_by_player"], {"p2": "IR"})
        self.assertEqual(primary_record["reserve_slot_counts"], reserve_counts)
        self.assertEqual(dict(primary.reserve_slot_by_player), {"p2": "IR"})
        self.assertEqual(
            dict(other.reserve_slot_by_player), {"q2": "ROOKIE_RESERVE"}
        )
        self.assertEqual(dict(primary.reserve_slot_counts), reserve_counts)
        self.assertEqual(primary.current_size, 2)
        self.assertEqual(primary.active_size, 1)
        self.assertNotEqual(capacity_only.bundle_id, baseline.bundle_id)
        self.assertNotEqual(with_reserves.bundle_id, capacity_only.bundle_id)

    def test_round_trip_preserves_legacy_capacity_exempt_roster_input(self):
        baseline = engine_bundle()
        reserve_counts = {"IR": 1}
        state = replace(
            baseline.state,
            roster_rules=replace(
                baseline.state.roster_rules,
                reserve_slot_counts=reserve_counts,
            ),
        )
        rosters = tuple(
            TeamRoster(
                row.team_id,
                row.player_ids,
                row.current_size,
                row.roster_cap,
                {"p2"} if row.team_id == "primary" else frozenset(),
                reserve_counts,
            )
            for row in baseline.rosters
        )
        with_ir = replace(baseline, state=state, rosters=rosters)

        restored = EngineBundle.from_record(with_ir.to_record())
        primary = next(row for row in restored.rosters if row.team_id == "primary")

        self.assertEqual(primary.capacity_exempt_player_ids, frozenset({"p2"}))
        self.assertEqual(dict(primary.reserve_slot_by_player), {"p2": "IR"})
        self.assertEqual(primary.current_size, 2)
        self.assertEqual(primary.active_size, 1)
        self.assertNotEqual(with_ir.bundle_id, baseline.bundle_id)

    def test_input_order_is_canonical_but_tampering_changes_identity(self):
        bundle = engine_bundle()
        reordered = EngineBundle(
            state=bundle.state,
            scoring_profile=bundle.scoring_profile,
            rosters=tuple(reversed(bundle.rosters)),
            projections=tuple(reversed(bundle.projections)),
            eligibilities=tuple(reversed(bundle.eligibilities)),
            nfl_schedule=bundle.nfl_schedule,
            source_manifest=bundle.source_manifest,
            projection_source_manifest=bundle.projection_source_manifest,
            fantasypros_benchmark=bundle.fantasypros_benchmark,
            ensemble_config=bundle.ensemble_config,
            scenario_config=bundle.scenario_config,
            strength_formula=bundle.strength_formula,
            strength_model=bundle.strength_model,
            ecr_snapshots=tuple(reversed(bundle.ecr_snapshots)),
            projection_evidence=tuple(reversed(bundle.projection_evidence)),
            player_names=dict(reversed(tuple(bundle.player_names.items()))),
            waiver_pool=bundle.waiver_pool,
            methodology_attestation=bundle.methodology_attestation,
        )
        self.assertEqual(bundle, reordered)

        tampered = copy.deepcopy(bundle.to_record())
        tampered["player_names"]["p1"] = "Changed"
        with self.assertRaisesRegex(ValueError, "does not match bundle_id"):
            EngineBundle.from_record(tampered)
        unknown = copy.deepcopy(bundle.to_record())
        unknown["cookie"] = "secret"
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            EngineBundle.from_record(unknown)

    def test_old_or_tampered_scoring_profile_records_fail_closed(self):
        bundle = engine_bundle()
        legacy = copy.deepcopy(bundle.to_record())
        legacy.pop("scoring_profile")
        for old_schema in range(1, 10):
            with self.subTest(old_schema=old_schema):
                legacy["schema_version"] = old_schema
                with self.assertRaises(UnsupportedEngineBundleSchema) as raised:
                    EngineBundle.from_record(legacy)
                self.assertEqual(raised.exception.schema_version, old_schema)
                self.assertIn("collect the league again", str(raised.exception))

        tampered = copy.deepcopy(bundle.to_record())
        tampered["scoring_profile"]["settings"]["reception"] = 0
        with self.assertRaisesRegex(ValueError, "does not match scoring_profile_id"):
            EngineBundle.from_record(tampered)

        with self.assertRaisesRegex(ValueError, "scoring profile"):
            replace(
                bundle,
                scoring_profile=ScoringProfile("espn", {"reception": 0}),
            )

    def test_rejects_incomplete_projection_universe_and_missing_raw_evidence(self):
        bundle = engine_bundle()
        with self.assertRaisesRegex(ValueError, "player universes differ"):
            EngineBundle(
                state=bundle.state,
                scoring_profile=bundle.scoring_profile,
                rosters=bundle.rosters,
                projections=bundle.projections[:-1],
                eligibilities=bundle.eligibilities,
                nfl_schedule=bundle.nfl_schedule,
                source_manifest=bundle.source_manifest,
                projection_source_manifest=bundle.projection_source_manifest,
                fantasypros_benchmark=bundle.fantasypros_benchmark,
                ensemble_config=bundle.ensemble_config,
                scenario_config=bundle.scenario_config,
                strength_formula=bundle.strength_formula,
                strength_model=bundle.strength_model,
                ecr_snapshots=bundle.ecr_snapshots,
                projection_evidence=bundle.projection_evidence,
                player_names=bundle.player_names,
                waiver_pool=bundle.waiver_pool,
                methodology_attestation=bundle.methodology_attestation,
            )

    def test_rejects_split_simulation_and_strength_eligibility(self):
        bundle = engine_bundle()
        first, *rest = bundle.eligibilities
        changed = PlayerEligibility(first.canonical_player_id, ("K",))

        with self.assertRaisesRegex(ValueError, "simulation eligibility"):
            replace(bundle, eligibilities=(changed, *rest))

    def test_rejects_ensemble_provider_identity_without_raw_evidence(self):
        bundle = engine_bundle()
        evidence = tuple(
            replace(row, provider_player_id="detached-provider-player")
            if row.canonical_player_id == "p1"
            and row.provider == "fantasypros"
            else row
            for row in bundle.projection_evidence
        )

        with self.assertRaisesRegex(ValueError, "matching projection evidence"):
            replace(bundle, projection_evidence=evidence)
        with self.assertRaisesRegex(ValueError, "normalized source projections"):
            EngineBundle(
                state=bundle.state,
                scoring_profile=bundle.scoring_profile,
                rosters=bundle.rosters,
                projections=bundle.projections,
                eligibilities=bundle.eligibilities,
                nfl_schedule=bundle.nfl_schedule,
                source_manifest=bundle.source_manifest,
                projection_source_manifest=bundle.projection_source_manifest,
                fantasypros_benchmark=bundle.fantasypros_benchmark,
                ensemble_config=bundle.ensemble_config,
                scenario_config=bundle.scenario_config,
                strength_formula=bundle.strength_formula,
                strength_model=bundle.strength_model,
                ecr_snapshots=bundle.ecr_snapshots,
                projection_evidence=(),
                player_names=bundle.player_names,
                waiver_pool=bundle.waiver_pool,
                methodology_attestation=bundle.methodology_attestation,
            )

    def test_rejects_projection_evidence_outside_the_calculation_universe(self):
        bundle = engine_bundle()
        source = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, RemainingSeasonProjection)
        )
        ghost = replace(
            source,
            canonical_player_id="ghost",
            provider_player_id="ghost-provider-id",
        )

        with self.assertRaisesRegex(ValueError, "outside the calculation universe"):
            replace(bundle, projection_evidence=(*bundle.projection_evidence, ghost))

        unconfigured = replace(
            source,
            provider="unconfigured",
            provider_player_id="unconfigured-provider-id",
        )
        with self.assertRaisesRegex(ValueError, "outside the ensemble configuration"):
            replace(
                bundle,
                projection_evidence=(*bundle.projection_evidence, unconfigured),
            )

        identity_collision = replace(source, canonical_player_id="p2")
        with self.assertRaisesRegex(ValueError, "maps to multiple calculation players"):
            replace(
                bundle,
                projection_evidence=(*bundle.projection_evidence, identity_collision),
            )

    def test_accepts_exact_ros_residual_after_future_direct_rows(self):
        bundle = ros_derived_bundle()

        restored = EngineBundle.from_record(bundle.to_record())
        target = next(
            row for row in restored.projections if row.canonical_player_id == "p1"
        )

        self.assertEqual(
            target.provider_observations[0].projected_fantasy_points,
            5.0,
        )

    def test_ros_residual_replaces_an_explicit_not_published_row(self):
        bundle = ros_derived_bundle(explicit_not_published=True)

        target = next(
            row for row in bundle.projections if row.canonical_player_id == "p1"
        )

        self.assertEqual(
            target.provider_observations[0].projected_fantasy_points,
            5.0,
        )

    def test_rejects_tampered_ros_derived_provider_value(self):
        bundle = ros_derived_bundle()
        target = next(
            row for row in bundle.projections if row.canonical_player_id == "p1"
        )
        observation = target.provider_observations[0]
        tampered = replace(
            target,
            provider_observations=(
                replace(observation, projected_fantasy_points=5.1),
            ),
            projected_fantasy_points=5.1,
        )
        projections = tuple(
            tampered if row is target else row for row in bundle.projections
        )

        with self.assertRaisesRegex(ValueError, "does not reconcile to ROS"):
            replace(bundle, projections=projections)

    def test_rejects_full_horizon_evidence_detached_from_the_strength_model(self):
        bundle = engine_bundle()
        target = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, RemainingSeasonProjection)
            and row.canonical_player_id == "p1"
        )
        evidence = tuple(
            replace(
                row,
                projected_fantasy_points=row.projected_fantasy_points + 1,
            )
            if row is target
            else row
            for row in bundle.projection_evidence
        )

        with self.assertRaisesRegex(ValueError, "strength model does not match"):
            replace(bundle, projection_evidence=evidence)

    def test_rejects_detached_ensemble_configuration_and_nfl_schedule(self):
        bundle = engine_bundle()
        changed_config = EnsembleConfig(
            (ProviderWeight("fantasypros", 2),),
            bundle.ensemble_config.minimum_observed_sources,
            bundle.ensemble_config.position_stddev_floors,
        )
        with self.assertRaisesRegex(ValueError, "provider weight"):
            replace(bundle, ensemble_config=changed_config)

        target_game = bundle.projections[0].nfl_game_id
        changed_schedule = NflSchedule(
            bundle.nfl_schedule.season,
            bundle.nfl_schedule.captured_at,
            bundle.nfl_schedule.source_provider,
            tuple(
                replace(row, nfl_game_id="different-game")
                if row.nfl_game_id == target_game
                else row
                for row in bundle.nfl_schedule.team_weeks
            ),
        )
        with self.assertRaisesRegex(ValueError, "NFL schedule"):
            replace(bundle, nfl_schedule=changed_schedule)

        truncated_schedule = nfl_schedule_for(bundle.projections, weeks=(1,))
        with self.assertRaisesRegex(ValueError, "through week 18"):
            replace(bundle, nfl_schedule=truncated_schedule)

    def test_rejects_projection_position_outside_player_eligibility(self):
        bundle = engine_bundle()
        projections = tuple(
            replace(row, position="WR")
            if row.canonical_player_id == "p1"
            else row
            for row in bundle.projections
        )

        with self.assertRaisesRegex(ValueError, "primary position"):
            replace(bundle, projections=projections)

    def test_rejects_raw_weekly_team_or_game_context_contradictions(self):
        bundle = engine_bundle()
        target = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, WeeklyProjection)
            and row.canonical_player_id == "p1"
        )
        wrong_team = tuple(
            replace(row, nfl_team_id="WRONG") if row is target else row
            for row in bundle.projection_evidence
        )
        with self.assertRaisesRegex(ValueError, "NFL team"):
            replace(bundle, projection_evidence=wrong_team)

        tampered = copy.deepcopy(bundle.to_record())
        raw = next(
            row
            for row in tampered["projection_evidence"]
            if row["kind"] == "weekly" and row["canonical_player_id"] == "p1"
        )
        raw["nfl_game_id"] = "WRONG-GAME"
        with self.assertRaisesRegex(ValueError, "game context"):
            EngineBundle.from_record(tampered)

    def test_rejects_a_strength_formula_detached_from_methodology_evidence(self):
        bundle = engine_bundle()
        formula = replace(bundle.strength_formula, source_fit_id="different-fit")

        with self.assertRaisesRegex(ValueError, "strength formula"):
            replace(bundle, strength_formula=formula)

    def test_rejects_a_formula_with_a_different_declared_feature_policy(self):
        bundle = engine_bundle()
        current = bundle.methodology_attestation.methodology_fingerprint
        fingerprint = MethodologyFingerprint(
            current.analyzer_bundle,
            current.response_schema_sha256,
            PowerMethodology(("ecr_ros_inverse_rank",), ("presence",)),
            current.role_definitions,
        )
        decision = FormulaReuseDecision(
            FormulaAction.RECALIBRATE,
            ("test feature-policy mismatch",),
            fingerprint.fingerprint_id,
        )
        with self.assertRaisesRegex(ValueError, "feature policy changed"):
            MethodologyAttestation.from_refresh(
                formula=bundle.strength_formula,
                strength_model=bundle.strength_model,
                methodology_fingerprint=fingerprint,
                formula_decision=decision,
                reuse_verification=None,
            )

        # The bundle boundary independently rejects a detached record loaded from
        # storage, even if it bypassed the normal refresh constructor.
        attestation = replace(
            bundle.methodology_attestation,
            methodology_fingerprint=fingerprint,
            formula_decision=decision,
        )

        with self.assertRaisesRegex(ValueError, "feature policy changed"):
            replace(bundle, methodology_attestation=attestation)

    def test_rejects_observed_value_without_weekly_or_ros_support(self):
        bundle = engine_bundle()
        target = next(
            row
            for row in bundle.projection_evidence
            if isinstance(row, WeeklyProjection)
            and row.canonical_player_id == "p1"
        )
        future_only = replace(target, week=2, nfl_game_id="G2")
        evidence = tuple(
            future_only if row is target else row
            for row in bundle.projection_evidence
            if not (
                isinstance(row, RemainingSeasonProjection)
                and row.canonical_player_id == "p1"
            )
        )

        with self.assertRaisesRegex(ValueError, "lacks matching projection evidence"):
            replace(bundle, projection_evidence=evidence)

    def test_rejects_detached_or_tampered_methodology_attestation(self):
        bundle = engine_bundle()
        detached = replace(
            bundle.methodology_attestation,
            strength_model_id="different-model",
        )
        with self.assertRaisesRegex(ValueError, "strength model"):
            replace(bundle, methodology_attestation=detached)

        tampered = copy.deepcopy(bundle.to_record())
        tampered["methodology_attestation"]["formula_id"] = "changed"
        with self.assertRaisesRegex(ValueError, "attestation_id"):
            EngineBundle.from_record(tampered)


if __name__ == "__main__":
    unittest.main()
