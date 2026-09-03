from dataclasses import replace
import inspect
import json
import unittest

from tests.test_engine_bundle import engine_bundle, exact_model_and_attestation
from trade_snapshot.roster_compatibility import build_roster_compatibility
from trade_snapshot.strength import PlayerStrength, RoleDefinition, RoleKind, StrengthModel
from trade_snapshot.trade_space import TeamRoster


def mutual_fit_bundle():
    bundle = engine_bundle()
    model = bundle.strength_model
    roles = (
        model.role_definitions[0],
        RoleDefinition("QB", RoleKind.STARTER, "QB", frozenset({"QB"})),
    )
    players = (
        PlayerStrength("p1", 1, frozenset({"FLEX"}), {"FLEX": 10}),
        PlayerStrength("p2", 1, frozenset({"FLEX"}), {"FLEX": 8}),
        PlayerStrength("q1", 1, frozenset({"QB"}), {"QB": 10}),
        PlayerStrength("q2", 1, frozenset({"QB"}), {"QB": 8}),
    )
    seed = StrengthModel(
        roles,
        players,
        model.normalization_denominator,
        snapshot_id=model.snapshot_id,
        season=model.season,
        scoring_profile_id=model.scoring_profile_id,
        calibration=model.calibration,
    )
    exact_model, attestation = exact_model_and_attestation(seed)
    return replace(
        bundle,
        strength_model=exact_model,
        methodology_attestation=attestation,
    )


def partner(result, team_id, partner_id):
    team = next(row for row in result["teams"] if row["team_id"] == team_id)
    return next(
        row for row in team["partners"] if row["partner_team_id"] == partner_id
    )


class RosterCompatibilityTests(unittest.TestCase):
    def test_mutual_fit_is_symmetric_and_best_example_is_directional(self):
        result = build_roster_compatibility(mutual_fit_bundle())
        primary = partner(result, "primary", "other")
        other = partner(result, "other", "primary")

        self.assertEqual(primary["evidence_tier"], "verified_mutual_positive_fit")
        self.assertEqual(primary["evaluated_swap_count"], 4)
        self.assertGreater(primary["mutually_positive_swap_count"], 0)
        self.assertEqual(
            primary["mutually_positive_swap_count"],
            other["mutually_positive_swap_count"],
        )
        self.assertEqual(
            primary["mutually_nondecreasing_swap_count"],
            other["mutually_nondecreasing_swap_count"],
        )
        primary_example = primary["best_mutually_positive_example"]
        other_example = other["best_mutually_positive_example"]
        self.assertEqual(
            primary_example["team_sends"], other_example["team_receives"]
        )
        self.assertEqual(
            primary_example["team_receives"], other_example["team_sends"]
        )
        self.assertEqual(
            primary_example["team_power_delta"],
            other_example["partner_power_delta"],
        )
        self.assertEqual(primary["power_methodology_status"], "exact")

    def test_api_and_payload_explicitly_exclude_behavior_or_acceptance_inputs(self):
        signature = inspect.signature(build_roster_compatibility)
        self.assertEqual(tuple(signature.parameters), (
            "bundle",
            "physically_injured_player_ids",
        ))
        self.assertEqual(
            signature.parameters["physically_injured_player_ids"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )
        result = build_roster_compatibility(engine_bundle())
        self.assertFalse(result["scope"]["behavioral_history_used"])
        self.assertFalse(result["scope"]["manager_acceptance_modeled"])
        self.assertEqual(result["scope"]["trade_shape"], "1_for_1")
        self.assertIn("1-for-1 discovery only", result["scope"]["limitation"])
        fit = partner(result, "primary", "other")["positional_fit"]
        self.assertEqual(fit["status"], "one_way")
        self.assertEqual(fit["team_needs_met_by_partner_surplus"], [])
        self.assertEqual(
            fit["partner_needs_met_by_team_surplus"][0]["position"],
            "FLEX",
        )

    def test_explicit_injury_and_capacity_exemption_remove_only_candidates(self):
        bundle = engine_bundle()
        rosters = tuple(
            TeamRoster(
                row.team_id,
                row.player_ids,
                row.current_size,
                row.roster_cap,
                {"q2"} if row.team_id == "other" else frozenset(),
            )
            for row in bundle.rosters
        )
        bundle = replace(bundle, rosters=rosters)
        result = build_roster_compatibility(
            bundle,
            physically_injured_player_ids=("p1",),
        )
        row = partner(result, "primary", "other")

        self.assertEqual(row["evaluated_swap_count"], 1)
        exclusions = {
            item["player_id"]: item["reasons"]
            for item in result["excluded_candidate_players"]
        }
        self.assertEqual(exclusions["p1"], ["explicit_physical_injury"])
        self.assertEqual(exclusions["q2"], ["capacity_exempt"])
        with self.assertRaisesRegex(ValueError, "not on a current roster"):
            build_roster_compatibility(
                bundle,
                physically_injured_player_ids=("w1",),
            )

    def test_result_is_deterministic_and_json_safe(self):
        bundle = mutual_fit_bundle()
        first = build_roster_compatibility(bundle)
        second = build_roster_compatibility(bundle)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, allow_nan=False, sort_keys=True),
            json.dumps(second, allow_nan=False, sort_keys=True),
        )
        self.assertEqual(first["unordered_team_pair_count"], 1)
        self.assertEqual(first["directed_partner_record_count"], 2)

    def test_no_tradeable_players_reports_limited_without_claiming_no_package_works(self):
        bundle = engine_bundle()
        rosters = tuple(
            TeamRoster(
                row.team_id,
                row.player_ids,
                row.current_size,
                row.roster_cap,
                frozenset(row.player_ids),
            )
            for row in bundle.rosters
        )
        result = build_roster_compatibility(replace(bundle, rosters=rosters))
        row = partner(result, "primary", "other")

        self.assertEqual(row["evidence_tier"], "limited")
        self.assertEqual(row["evaluated_swap_count"], 0)
        self.assertEqual(row["mutually_positive_swap_count"], 0)
        self.assertEqual(row["mutually_nondecreasing_swap_count"], 0)
        self.assertIsNone(row["best_mutually_positive_example"])
        self.assertEqual(row["positional_fit"]["status"], "none")
        self.assertIn("larger or differently shaped", row["scope_limitation"])


if __name__ == "__main__":
    unittest.main()
