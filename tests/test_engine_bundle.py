from dataclasses import replace
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.test_search_runner import PLAYER_POINTS, components
from trade_snapshot.analyzer_contract import BundleFingerprint
from trade_snapshot.ecr import EcrPeriod, EcrPlayerRanking, EcrSnapshot
from trade_snapshot.engine_bundle import EngineBundle, load_engine_bundle, save_engine_bundle
from trade_snapshot.ensemble import EnsembleProjection, ProviderObservation
from trade_snapshot.methodology import PowerMethodology
from trade_snapshot.methodology_attestation import MethodologyAttestation
from trade_snapshot.methodology_reuse import (
    FormulaAction,
    FormulaReuseDecision,
    MethodologyFingerprint,
)
from trade_snapshot.league_state import RosterRules
from trade_snapshot.projections import ProjectionStatus, WeeklyProjection
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
    )


def exact_model_and_attestation(model):
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
    waiver_strengths = tuple(
        PlayerStrength(
            player_id,
            ALL_POINTS[player_id],
            frozenset({"RB", "FLEX"}),
            {model.role_definitions[0].role_id: 0},
        )
        for player_id in WAIVER_PLAYER_IDS
    )
    exact_model = StrengthModel(
        model.role_definitions,
        (*model.players.values(), *waiver_strengths),
        model.normalization_denominator,
        snapshot_id=model.snapshot_id,
        season=model.season,
        scoring_profile_id=model.scoring_profile_id,
        calibration=calibration,
    )
    methodology = PowerMethodology(("presence",), ("ecr_ros_inverse_rank",))
    fingerprint = MethodologyFingerprint(
        BundleFingerprint(URL, SHA),
        SCHEMA,
        methodology,
        exact_model.role_definitions,
    )
    formula = StrengthFormula(
        "strength-fit-test",
        exact_model.snapshot_id,
        exact_model.season,
        exact_model.scoring_profile_id,
        exact_model.role_definitions,
        {"presence": 1},
        {
            role.role_id: {"ecr_ros_inverse_rank": 1}
            for role in exact_model.role_definitions
        },
        calibration,
        tuple(f"calibration-holdout-{index}" for index in range(100)),
        (1, 2, 3, 4),
    )
    decision = FormulaReuseDecision(
        FormulaAction.RECALIBRATE,
        ("test calibration",),
        fingerprint.fingerprint_id,
    )
    return exact_model, MethodologyAttestation.from_refresh(
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
                "source",
                f"source-{player_id}",
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
        "G1",
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


def engine_bundle():
    runner = components(scoring_profile_id=SCORING_PROFILE.scoring_profile_id)
    baseline = runner.season_baseline
    bundle_state = replace(
        baseline.state,
        roster_rules=RosterRules(2, ("FLEX", "RB")),
    )
    strength_model, attestation = exact_model_and_attestation(
        runner.prepared_strength.model
    )
    projections = (
        *baseline.scenarios.projections,
        *(waiver_projection(baseline.scenarios.projections[0], player_id)
          for player_id in WAIVER_PLAYER_IDS),
    )
    eligibilities = (
        *baseline.scenarios.eligibilities,
        *(PlayerEligibility(player_id, ("RB", "FLEX"))
          for player_id in WAIVER_PLAYER_IDS),
    )
    evidence = tuple(
        WeeklyProjection(
            canonical_player_id=player_id,
            snapshot_id="snapshot-1",
            scoring_profile_id=SCORING_PROFILE.scoring_profile_id,
            provider="fantasypros",
            provider_player_id=f"fp-{player_id}",
            season=2026,
            week=1,
            status=ProjectionStatus.OBSERVED,
            captured_at=NOW,
            projected_fantasy_points=points,
            raw_projected_stats={"points": points, "rush_yards": points * 5},
            nfl_team_id=f"NFL-{player_id}",
            nfl_game_id="G1",
            opponent_team_id=f"OPP-{player_id}",
            is_home=True,
        )
        for player_id, points in ALL_POINTS.items()
    )
    return EngineBundle(
        state=bundle_state,
        scoring_profile=SCORING_PROFILE,
        rosters=baseline.scenarios.rosters,
        projections=projections,
        eligibilities=eligibilities,
        scenario_config=baseline.scenarios.config,
        strength_model=strength_model,
        ecr_snapshots=(
            ecr_snapshot(EcrPeriod.WEEKLY),
            ecr_snapshot(EcrPeriod.REST_OF_SEASON),
        ),
        projection_evidence=evidence,
        player_names={player_id: player_id.upper() for player_id in ALL_POINTS},
        waiver_pool=waiver_pool(),
        methodology_attestation=attestation,
    )


class EngineBundleTests(unittest.TestCase):
    def test_methodology_status_is_exact_only_inside_attested_trade_scope(self):
        attestation = engine_bundle().methodology_attestation
        self.assertEqual(
            attestation.power_result_status(
                outgoing_count=1,
                incoming_count=1,
                has_roster_adjustment=False,
            ),
            "exact",
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
        self.assertEqual(record["schema_version"], 6)
        self.assertIsNone(record["surrogate_disclosure"])
        self.assertEqual(
            record["methodology_attestation"]["attestation_id"],
            bundle.methodology_attestation.attestation_id,
        )
        self.assertEqual(record["league_state"]["schema_version"], 2)
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

    def test_round_trip_preserves_capacity_exempt_ownership_and_identity(self):
        baseline = engine_bundle()
        rosters = tuple(
            TeamRoster(
                row.team_id,
                row.player_ids,
                row.current_size,
                row.roster_cap,
                {"p2"} if row.team_id == "primary" else frozenset(),
            )
            for row in baseline.rosters
        )
        with_ir = EngineBundle(
            state=baseline.state,
            scoring_profile=baseline.scoring_profile,
            rosters=rosters,
            projections=baseline.projections,
            eligibilities=baseline.eligibilities,
            scenario_config=baseline.scenario_config,
            strength_model=baseline.strength_model,
            ecr_snapshots=baseline.ecr_snapshots,
            projection_evidence=baseline.projection_evidence,
            player_names=baseline.player_names,
            waiver_pool=baseline.waiver_pool,
            methodology_attestation=baseline.methodology_attestation,
        )

        restored = EngineBundle.from_record(with_ir.to_record())
        primary = next(row for row in restored.rosters if row.team_id == "primary")

        self.assertEqual(primary.capacity_exempt_player_ids, frozenset({"p2"}))
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
            scenario_config=bundle.scenario_config,
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
        for old_schema in (1, 2, 3, 4):
            with self.subTest(old_schema=old_schema):
                legacy["schema_version"] = old_schema
                with self.assertRaisesRegex(ValueError, "fields are invalid"):
                    EngineBundle.from_record(legacy)

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
                scenario_config=bundle.scenario_config,
                strength_model=bundle.strength_model,
                ecr_snapshots=bundle.ecr_snapshots,
                projection_evidence=bundle.projection_evidence,
                player_names=bundle.player_names,
                waiver_pool=bundle.waiver_pool,
                methodology_attestation=bundle.methodology_attestation,
            )
        with self.assertRaisesRegex(ValueError, "normalized source projections"):
            EngineBundle(
                state=bundle.state,
                scoring_profile=bundle.scoring_profile,
                rosters=bundle.rosters,
                projections=bundle.projections,
                eligibilities=bundle.eligibilities,
                scenario_config=bundle.scenario_config,
                strength_model=bundle.strength_model,
                ecr_snapshots=bundle.ecr_snapshots,
                projection_evidence=(),
                player_names=bundle.player_names,
                waiver_pool=bundle.waiver_pool,
                methodology_attestation=bundle.methodology_attestation,
            )

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
