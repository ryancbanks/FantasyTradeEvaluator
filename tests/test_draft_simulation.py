from dataclasses import replace
import unittest

from tests.draft_fixtures import (
    draft_player,
    small_draft_config,
    small_historical_corpus,
)
from trade_snapshot.draft_config import DraftStrategy, default_slot_eligibility
from trade_snapshot.draft_features import build_baseline_brain
from trade_snapshot.draft_simulation import rank_draft_candidates, simulate_snake_draft


class DraftSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = small_historical_corpus()
        cls.season = cls.corpus.seasons[0]
        cls.config = small_draft_config()
        cls.brain = build_baseline_brain(cls.corpus, cls.config, (2025,))

    def test_completes_a_deterministic_legal_snake_draft(self):
        first = simulate_snake_draft(
            self.season, self.config, (self.brain,) * 4, seed=17
        )
        second = simulate_snake_draft(
            self.season, self.config, (self.brain,) * 4, seed=17
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first.picks), 20)
        self.assertEqual([row.drafter_number for row in first.picks[:8]], [
            1, 2, 3, 4, 4, 3, 2, 1
        ])
        self.assertEqual(len({row.player_id for row in first.picks}), 20)
        for roster in first.rosters:
            positions = {
                next(player.position for player in self.season.players if player.player_id == player_id)
                for player_id in roster
            }
            self.assertEqual(positions, {"QB", "RB", "WR", "TE"})

    def test_strategy_gates_are_hard_and_roster_context_is_reported(self):
        config = small_draft_config(strategies={
            DraftStrategy.STREAMING_QB: 1,
            DraftStrategy.STREAMING_TE: 1,
            DraftStrategy.LATE_ROUND_QB: 1,
            DraftStrategy.NONE: 1,
        })
        brain = build_baseline_brain(self.corpus, config, (2025,))
        available = self.season.players
        ranked = rank_draft_candidates(
            self.season, config, brain, DraftStrategy.STREAMING_QB,
            roster_player_ids=(), available_players=available,
            round_number=1, overall_pick=1, drafter_number=1,
        )
        by_id = {player.player_id: player for player in available}
        self.assertTrue(ranked)
        self.assertTrue(all(by_id[row.player_id].position != "QB" for row in ranked))
        self.assertTrue(any(row.starter_need == 1 for row in ranked))

    def test_display_names_do_not_change_draft_decisions(self):
        renamed_players = tuple(
            replace(player, display_name=f"Anonymous {index}")
            for index, player in enumerate(self.season.players)
        )
        renamed_season = replace(self.season, players=renamed_players)
        original = simulate_snake_draft(
            self.season, self.config, (self.brain,) * 4, seed=8
        )
        renamed = simulate_snake_draft(
            renamed_season, self.config, (self.brain,) * 4, seed=8
        )
        self.assertEqual(original.rosters, renamed.rosters)
        self.assertEqual(original.picks, renamed.picks)

    def test_incompatible_model_and_impossible_strategy_fail_clearly(self):
        other = small_draft_config(strategies={DraftStrategy.STREAMING_DST: 4})
        with self.assertRaisesRegex(ValueError, "not compatible"):
            rank_draft_candidates(
                self.season, other, self.brain, DraftStrategy.NONE,
                roster_player_ids=(), available_players=self.season.players,
                round_number=1, overall_pick=1, drafter_number=1,
            )

    def test_global_player_supply_must_fill_every_team_not_just_one(self):
        season = replace(
            self.season,
            players=(
                next(row for row in self.season.players if row.position == "QB"),
                next(row for row in self.season.players if row.position == "RB"),
            ),
        )
        corpus = replace(self.corpus, seasons=(season,))
        config = replace(
            self.config,
            team_count=2,
            starting_slots=("QB",),
            bench_slots=0,
            slot_eligibility=default_slot_eligibility(("QB",)),
            position_limits={"QB": 1},
            playoff_team_count=2,
            playoff_weeks=(3,),
            strategy_counts={DraftStrategy.NONE: 2},
        )
        brain = build_baseline_brain(corpus, config, (2025,))
        with self.assertRaisesRegex(ValueError, "every team's"):
            simulate_snake_draft(season, config, (brain, brain))

    def test_global_reservation_keeps_qb_for_late_round_strategy(self):
        season = replace(
            self.season,
            players=tuple(
                [draft_player("QB", rank) for rank in range(1, 3)]
                + [draft_player("RB", rank) for rank in range(1, 19)]
            ),
        )
        corpus = replace(self.corpus, seasons=(season,))
        config = replace(
            self.config,
            team_count=2,
            starting_slots=("QB",),
            bench_slots=9,
            slot_eligibility=default_slot_eligibility(("QB",)),
            position_limits={"QB": 2, "RB": 10},
            playoff_team_count=2,
            playoff_weeks=(3,),
            strategy_counts={
                DraftStrategy.NONE: 1,
                DraftStrategy.LATE_ROUND_QB: 1,
            },
        )
        brain = build_baseline_brain(corpus, config, (2025,))
        result = simulate_snake_draft(season, config, (brain, brain), seed=0)
        late_qb_pick = next(
            row for row in result.picks
            if row.drafter_number == 2 and row.player_id.startswith("2025-qb")
        )
        self.assertEqual(late_qb_pick.round_number, 10)


if __name__ == "__main__":
    unittest.main()
