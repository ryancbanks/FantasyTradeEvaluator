from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import copy
import json
import math
import unittest

from trade_snapshot.strength import (
    CalibrationMetadata,
    CalibrationStatus,
    PlayerStrength,
    RoleDefinition,
    RoleKind,
    StrengthModel,
)


BUNDLE_URL = "https://cdn.fantasypros.com/assets/trade-analyzer.js"
BUNDLE_SHA = "1" * 64
SCHEMA_SHA = "2" * 64
CAPTURED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def metadata(**changes):
    values = {
        "analyzer_bundle_url": BUNDLE_URL,
        "analyzer_bundle_sha256": BUNDLE_SHA,
        "response_schema_sha256": SCHEMA_SHA,
        "captured_at": CAPTURED_AT,
    }
    values.update(changes)
    return CalibrationMetadata(**values)


def role(role_id, *, kind=RoleKind.STARTER, source_slot=None, positions=None):
    return RoleDefinition(
        role_id,
        kind,
        source_slot or role_id,
        frozenset(positions or {role_id}),
    )


def player(player_id, residual, role_scores, *, positions=None):
    return PlayerStrength(
        player_id,
        residual,
        frozenset(positions or set(role_scores) or {"UNSCORED"}),
        role_scores,
    )


def make_model(roles, players, normalization_denominator, **identity_changes):
    role_definitions = tuple(
        value if isinstance(value, RoleDefinition) else role(value)
        for value in roles
    )
    identity = {
        "snapshot_id": "snapshot-1",
        "season": 2026,
        "scoring_profile_id": "profile-1",
        "calibration": metadata(),
    }
    identity.update(identity_changes)
    return StrengthModel(
        role_definitions,
        players,
        normalization_denominator,
        **identity,
    )


class StrengthModelTests(unittest.TestCase):
    def test_counts_every_residual_and_only_the_optimal_role_assignments(self):
        model = make_model(
            ("QB", "FLEX"),
            (
                player("qb", 20, {"QB": 8}),
                player("starter", 15, {"FLEX": 6}),
                player("bench", 5, {"FLEX": 2}),
            ),
            normalization_denominator=54,
        )

        result = model.score_roster(("qb", "starter", "bench"))

        self.assertEqual(result.residual_score, 40)
        self.assertEqual(result.assignment_score, 14)
        self.assertEqual(result.absolute_score, 54)
        self.assertEqual(result.power_score, 100)
        self.assertEqual(
            tuple(
                assignment.player_id
                for assignment in result.role_assignment.assignments
            ),
            ("qb", "starter"),
        )

    def test_role_specific_score_uses_the_global_assignment_optimum(self):
        model = make_model(
            ("RB", "FLEX"),
            (
                player("dual", 1, {"RB": 10, "FLEX": 10}),
                player("rb", 1, {"RB": 9}),
                player("wr", 1, {"FLEX": 8}),
            ),
            normalization_denominator=22,
        )

        result = model.score_roster(("dual", "rb", "wr"))

        self.assertEqual(result.absolute_score, 22)
        self.assertEqual(
            tuple(
                assignment.player_id
                for assignment in result.role_assignment.assignments
            ),
            ("rb", "dual"),
        )

    def test_trade_uses_one_fixed_pre_trade_normalization_denominator(self):
        model = make_model(
            ("QB",),
            (
                player("primary-qb", 40, {"QB": 10}),
                player("primary-bench", 20, {"QB": 1}),
                player("other-qb", 50, {"QB": 20}),
                player("other-bench", 10, {"QB": 1}),
            ),
            normalization_denominator=80,
        )

        result = model.evaluate_trade(
            primary_roster=("primary-qb", "primary-bench"),
            counterparty_roster=("other-qb", "other-bench"),
            outgoing_player_ids=("primary-qb",),
            incoming_player_ids=("other-qb",),
        )

        self.assertAlmostEqual(result.primary.before.absolute_score, 70)
        self.assertAlmostEqual(result.primary.after.absolute_score, 90)
        self.assertAlmostEqual(result.primary.after.power_score, 112.5)
        self.assertAlmostEqual(result.primary.absolute_delta, 20)
        self.assertAlmostEqual(result.primary.power_delta, 25)
        self.assertAlmostEqual(result.counterparty.absolute_delta, -20)
        self.assertAlmostEqual(result.counterparty.power_delta, -25)

    def test_depth_roles_can_make_a_bench_swap_non_antisymmetric(self):
        roles = (
            role("RB_START", source_slot="RB", positions={"RB"}),
            role("RB_DEPTH_1", kind=RoleKind.DEPTH, source_slot="RB", positions={"RB"}),
            role("RB_DEPTH_2", kind=RoleKind.DEPTH, source_slot="RB", positions={"RB"}),
        )

        def rb(player_id, start, depth1, depth2):
            return player(
                player_id,
                0,
                {
                    "RB_START": start,
                    "RB_DEPTH_1": depth1,
                    "RB_DEPTH_2": depth2,
                },
                positions={"RB"},
            )

        model = make_model(
            roles,
            (
                rb("starter-a", 20, 0, 0),
                rb("depth-a", 0, 10, 9),
                rb("context-a", 0, 9, 0),
                rb("starter-b", 20, 0, 0),
                rb("depth-b", 0, 8, 1),
            ),
            normalization_denominator=50,
        )

        result = model.evaluate_trade(
            primary_roster=("starter-a", "depth-a", "context-a"),
            counterparty_roster=("starter-b", "depth-b"),
            outgoing_player_ids=("depth-a",),
            incoming_player_ids=("depth-b",),
        )

        self.assertEqual(result.primary.absolute_delta, -8)
        self.assertEqual(result.counterparty.absolute_delta, 2)

    def test_model_and_nested_calibration_are_immutable_and_content_addressed(self):
        model = make_model(("QB",), (player("p", 1, {"QB": 2}),), 3)
        same = make_model(("QB",), (player("p", 1, {"QB": 2}),), 3)
        changed = make_model(("QB",), (player("p", 1, {"QB": 3}),), 3)

        self.assertEqual(model.model_id, same.model_id)
        self.assertNotEqual(model.model_id, changed.model_id)
        for field, value in (
            ("snapshot_id", "changed"),
            ("season", 2027),
            ("scoring_profile_id", "changed"),
            ("normalization_denominator", 1),
            ("role_definitions", ()),
            ("players", {}),
        ):
            with self.subTest(field=field):
                with self.assertRaises((FrozenInstanceError, AttributeError)):
                    setattr(model, field, value)
        with self.assertRaises(TypeError):
            model.players["new"] = player("new", 0, {"QB": 0})

    def test_strict_model_record_round_trip_rejects_tampering(self):
        model = make_model(("QB",), (player("p", 1, {"QB": 2}),), 3)
        record = model.to_record()

        json.dumps(record, allow_nan=False)
        self.assertEqual(StrengthModel.from_record(record), model)

        tampered = copy.deepcopy(record)
        tampered["players"][0]["residual_score"] = 2
        with self.assertRaisesRegex(ValueError, "does not match model_id"):
            StrengthModel.from_record(tampered)

        unknown = copy.deepcopy(record)
        unknown["role_definitions"][0]["unknown"] = True
        with self.assertRaisesRegex(ValueError, "missing or unknown"):
            StrengthModel.from_record(unknown)

        bad_evidence = copy.deepcopy(record)
        bad_evidence["calibration"]["evidence_id"] = "calibration-v1-" + "0" * 64
        with self.assertRaisesRegex(ValueError, "evidence_id"):
            StrengthModel.from_record(bad_evidence)

    def test_role_schema_rejects_unknown_missing_ineligible_and_negative_scores(self):
        with self.assertRaisesRegex(ValueError, "unknown role 'QBB'"):
            make_model(
                ("QB",),
                (player("p", 1, {"QBB": 1}, positions={"QB"}),),
                10,
            )
        with self.assertRaisesRegex(ValueError, r"missing \['QB'\]"):
            make_model(
                ("QB",),
                (player("p", 1, {}, positions={"QB"}),),
                10,
            )
        with self.assertRaisesRegex(ValueError, r"ineligible \['QB'\]"):
            make_model(
                ("QB",),
                (player("p", 1, {"QB": 1}, positions={"WR"}),),
                10,
            )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            player("p", 1, {"QB": -1})

    def test_exact_label_requires_strict_held_out_evidence(self):
        with self.assertRaisesRegex(ValueError, "exact calibration"):
            metadata(
                status=CalibrationStatus.EXACT,
                held_out_trade_count=10,
                max_absolute_score_error=0.01,
                display_match_rate=1,
            )
        exact = metadata(
            status=CalibrationStatus.EXACT,
            held_out_trade_count=10,
            max_absolute_score_error=1e-6,
            display_match_rate=1,
        )
        self.assertEqual(exact.status, CalibrationStatus.EXACT)

    def test_rejects_invalid_calibration_and_unknown_players(self):
        for value in (math.inf, -math.inf, math.nan, True, "5"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    player("p", value, {"QB": 1})

        with self.assertRaisesRegex(ValueError, "duplicate player_id"):
            make_model(
                ("QB",),
                (player("p", 1, {"QB": 1}), player("p", 2, {"QB": 2})),
                10,
            )
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            make_model(("QB",), (), 0)

        model = make_model(("QB",), (player("p", 1, {"QB": 1}),), 10)
        with self.assertRaisesRegex(ValueError, "missing strength calibration"):
            model.score_roster(("unknown",))

    def test_rejects_invalid_trade_ownership_and_rosters(self):
        model = make_model(
            ("QB",),
            tuple(player(player_id, 1, {}, positions={"UNSCORED"}) for player_id in "abcd"),
            10,
        )
        valid = {
            "primary_roster": ("a", "b"),
            "counterparty_roster": ("c", "d"),
            "outgoing_player_ids": ("a",),
            "incoming_player_ids": ("c",),
        }

        cases = (
            ({"primary_roster": ("a", "a")}, "duplicate player_id"),
            ({"counterparty_roster": ("a", "c")}, "owned by both"),
            ({"outgoing_player_ids": ("c",)}, "not on the primary"),
            ({"incoming_player_ids": ("a",)}, "not on the counterparty"),
            ({"outgoing_player_ids": ()}, "at least one player"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                arguments = {**valid, **changes}
                with self.assertRaisesRegex(ValueError, message):
                    model.evaluate_trade(**arguments)


if __name__ == "__main__":
    unittest.main()
