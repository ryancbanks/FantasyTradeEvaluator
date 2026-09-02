import unittest

from trade_snapshot.league_state import RosterRules
from trade_snapshot.role_design import build_calibration_roles
from trade_snapshot.scenario_config import PlayerEligibility
from trade_snapshot.strength import RoleKind
from trade_snapshot.trade_space import TeamRoster


class RoleDesignTests(unittest.TestCase):
    def test_builds_distinct_starters_and_bounded_observed_depth(self):
        rules = RosterRules(6, ("QB", "RB", "RB", "FLEX"))
        rosters = (
            TeamRoster("a", ("q1", "r1", "r2", "r3", "w1", "w2"), 6, 6),
            TeamRoster("b", ("q2", "r4", "r5", "w3", "w4", "w5"), 6, 6),
        )
        positions = {
            "q1": "QB", "q2": "QB",
            "r1": "RB", "r2": "RB", "r3": "RB", "r4": "RB", "r5": "RB",
            "w1": "WR", "w2": "WR", "w3": "WR", "w4": "WR", "w5": "WR",
        }
        eligibilities = tuple(
            PlayerEligibility(player, (position, "FLEX") if position in {"RB", "WR"} else (position,))
            for player, position in positions.items()
        )
        roles = build_calibration_roles(
            rules, rosters, positions, eligibilities, maximum_depth_per_position=2
        )
        starters = tuple(role for role in roles if role.kind is RoleKind.STARTER)
        depth = tuple(role for role in roles if role.kind is RoleKind.DEPTH)
        self.assertEqual(len(starters), 4)
        self.assertEqual(
            tuple(role.role_id for role in starters),
            ("START__QB__1", "START__RB__1", "START__RB__2", "START__FLEX__1"),
        )
        self.assertEqual(len(depth), 5)
        self.assertEqual({role.source_slot for role in depth}, {"QB", "RB", "WR"})

    def test_rejects_incomplete_rosters_or_missing_player_metadata(self):
        rules = RosterRules(2, ("QB",))
        with self.assertRaisesRegex(ValueError, "complete"):
            build_calibration_roles(
                rules,
                (TeamRoster("a", ("q1",), 2, 2),),
                {"q1": "QB"},
                (PlayerEligibility("q1", ("QB",)),),
            )
        with self.assertRaisesRegex(ValueError, "missing player metadata"):
            build_calibration_roles(
                rules,
                (TeamRoster("a", ("q1", "q2"), 2, 2),),
                {"q1": "QB"},
                (PlayerEligibility("q1", ("QB",)),),
            )


if __name__ == "__main__":
    unittest.main()
