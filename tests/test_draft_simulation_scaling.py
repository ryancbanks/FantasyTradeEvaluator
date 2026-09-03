from collections import Counter
from dataclasses import replace
from unittest.mock import patch
import unittest

from tests.draft_fixtures import (
    draft_player,
    small_draft_config,
    small_historical_corpus,
)
from trade_snapshot.draft_brain import (
    FeatureSchema,
    RegressionBaseline,
    initialize_genome,
)
from trade_snapshot.draft_config import (
    DraftLeagueConfig,
    DraftStrategy,
    default_slot_eligibility,
)
from trade_snapshot.draft_features import build_baseline_brain, candidate_feature_values
from trade_snapshot.draft_matching import maximum_group_slot_fill
from trade_snapshot.draft_simulation import rank_draft_candidates, simulate_snake_draft


def _config(slots, *, limits=None, strategies=None, bench=0):
    return DraftLeagueConfig(
        name="Scaling fixture",
        team_count=2,
        starting_slots=tuple(slots),
        bench_slots=bench,
        slot_eligibility=default_slot_eligibility(tuple(slots)),
        position_limits=limits or {},
        scoring_weights={"points": 1.0},
        regular_season_weeks=(1, 2),
        playoff_team_count=2,
        playoff_weeks=(3,),
        strategy_counts=strategies or {DraftStrategy.NONE: 2},
    )


def _corpus_with(players):
    corpus = small_historical_corpus()
    players = tuple(players)
    feature_names = tuple(sorted({
        name for player in players for name in player.preseason_features
    }))
    provenance = tuple(
        replace(row, preseason_feature_names=feature_names)
        for row in corpus.provenance
    )
    return replace(
        corpus,
        seasons=(replace(corpus.seasons[0], players=players),),
        provenance=provenance,
    )


class DraftCompletionScalingTests(unittest.TestCase):
    def test_slot_compression_preserves_owned_edge_identity(self):
        filled = maximum_group_slot_fill(
            Counter({(0,): 2}),
            Counter(),
            ((0, ("A",)), (0, ("A",))),
            {},
        )

        self.assertEqual(filled, 1)

    def test_direct_group_flow_respects_zero_strategy_round_capacity(self):
        available = Counter({
            ("C", ("B",), (1,)): 1,
            ("C", ("B", "C"), (0,)): 1,
        })
        slots = ((0, ("B",)), (0, ("C",)))

        filled = maximum_group_slot_fill(
            Counter(), available, slots, {(0, "C"): 2}
        )

        self.assertEqual(filled, 1)

    def test_dual_eligible_primary_players_can_fill_secondary_slots(self):
        players = tuple(
            replace(
                draft_player("RB", index),
                eligible_positions=("RB", "WR"),
            )
            for index in (1, 2)
        )
        corpus = _corpus_with(players)
        config = _config(("WR",), limits={"RB": 1, "WR": 1})
        brain = build_baseline_brain(corpus, config, (2025,))

        result = simulate_snake_draft(
            corpus.seasons[0], config, (brain, brain), seed=3
        )

        self.assertEqual(len(result.picks), 2)
        self.assertTrue(all(len(roster) == 1 for roster in result.rosters))

    def test_primary_position_caps_are_enforced_across_flexible_slots(self):
        players = tuple(
            replace(
                draft_player("RB", index),
                eligible_positions=("RB", "WR"),
            )
            for index in range(1, 5)
        )
        corpus = _corpus_with(players)
        config = _config(("RB", "WR"), limits={"RB": 1, "WR": 2})
        brain = build_baseline_brain(corpus, config, (2025,))

        with self.assertRaisesRegex(ValueError, "position limits"):
            simulate_snake_draft(corpus.seasons[0], config, (brain, brain))

    def test_full_roster_caps_are_proved_before_the_first_pick(self):
        players = tuple(
            draft_player(position, index)
            for position in ("QB", "RB", "WR", "TE")
            for index in range(1, 4)
        )
        corpus = _corpus_with(players)
        config = _config(
            ("QB", "RB", "WR", "TE"),
            limits={"QB": 1, "RB": 1, "WR": 1, "TE": 1},
            bench=1,
        )
        brain = build_baseline_brain(corpus, config, (2025,))

        with self.assertRaisesRegex(ValueError, "complete configured roster"):
            simulate_snake_draft(corpus.seasons[0], config, (brain, brain))

    def test_mixed_flex_groups_keep_their_own_slot_edges_behind_caps(self):
        players = (
            replace(
                draft_player("RB", 1),
                eligible_positions=("RB", "WR"),
            ),
            replace(
                draft_player("RB", 2),
                eligible_positions=("RB", "WR"),
            ),
            replace(
                draft_player("RB", 3),
                eligible_positions=("RB",),
            ),
            replace(
                draft_player("TE", 1),
                eligible_positions=("RB", "TE"),
            ),
            replace(
                draft_player("TE", 2),
                eligible_positions=("RB", "TE"),
            ),
        )
        corpus = _corpus_with(players)
        config = _config(("RB", "WR"), limits={"RB": 1, "TE": 1})
        brain = build_baseline_brain(corpus, config, (2025,))

        result = simulate_snake_draft(
            corpus.seasons[0], config, (brain, brain), seed=7
        )

        self.assertEqual(len(result.picks), 4)
        self.assertTrue(all(len(roster) == 2 for roster in result.rosters))

    def test_impossible_late_round_strategy_fails_before_drafting(self):
        corpus = small_historical_corpus()
        config = small_draft_config(
            strategies={DraftStrategy.LATE_ROUND_QB: 4}
        )
        brain = build_baseline_brain(corpus, config, (2025,))

        with self.assertRaisesRegex(ValueError, "eligible draft rounds"):
            simulate_snake_draft(corpus.seasons[0], config, (brain,) * 4)

    def test_streaming_dst_cannot_fill_two_dst_slots_in_one_round(self):
        players = tuple(
            replace(
                draft_player("RB", index),
                player_id=f"2025-dst-{index:02d}",
                display_name=f"DST {index}",
                position="DST",
                eligible_positions=("DST",),
            )
            for index in range(1, 5)
        )
        corpus = _corpus_with(players)
        config = _config(
            ("DST", "DST"),
            limits={"DST": 2},
            strategies={DraftStrategy.STREAMING_DST: 2},
        )
        brain = build_baseline_brain(corpus, config, (2025,))

        with self.assertRaisesRegex(ValueError, "only 1 eligible draft rounds"):
            simulate_snake_draft(corpus.seasons[0], config, (brain, brain))

    def test_feature_context_is_computed_once_per_candidate_signature(self):
        corpus = small_historical_corpus()
        config = _config(("RB",))
        brain = build_baseline_brain(corpus, config, (2025,))
        running = tuple(
            player for player in corpus.seasons[0].players
            if player.position == "RB"
        )

        with patch(
            "trade_snapshot.draft_simulation.candidate_feature_values",
            wraps=candidate_feature_values,
        ) as feature_builder:
            ranked = rank_draft_candidates(
                corpus.seasons[0], config, brain, DraftStrategy.NONE,
                roster_player_ids=(), available_players=running,
                round_number=1, overall_pick=1, drafter_number=1,
            )

        self.assertEqual(len(ranked), len(running))
        self.assertEqual(feature_builder.call_count, 1)

    def test_namespaced_projection_shortlist_uses_an_ensemble_then_provider_mean(self):
        first = replace(
            draft_player("RB", 1),
            preseason_features={
                "espn.projected_fantasy_points": 100.0,
                "yahoo.projected_fantasy_points": 100.0,
            },
        )
        second = replace(
            draft_player("RB", 2),
            preseason_features={"ensemble.projected_fantasy_points": 150.0},
        )
        corpus = _corpus_with((first, second))
        config = _config(("RB",))
        brain = build_baseline_brain(corpus, config, (2025,))

        ranked = rank_draft_candidates(
            corpus.seasons[0], config, brain, DraftStrategy.NONE,
            roster_player_ids=(), available_players=corpus.seasons[0].players,
            round_number=1, overall_pick=1, drafter_number=1,
            candidate_window=1,
        )

        self.assertEqual([row.player_id for row in ranked], [second.player_id])

    def test_empty_position_limits_do_not_invent_a_roster_cap(self):
        corpus = small_historical_corpus()
        config = _config(("RB",), bench=3)
        brain = build_baseline_brain(corpus, config, (2025,))
        quarterbacks = tuple(
            player for player in corpus.seasons[0].players
            if player.position == "QB"
        )
        running_back = next(
            player for player in corpus.seasons[0].players
            if player.position == "RB"
        )

        ranked = rank_draft_candidates(
            corpus.seasons[0], config, brain, DraftStrategy.NONE,
            roster_player_ids=tuple(player.player_id for player in quarterbacks[:2]),
            available_players=(*quarterbacks[2:3], running_back),
            round_number=3, overall_pick=5, drafter_number=1,
        )

        self.assertIn(quarterbacks[2].player_id, {row.player_id for row in ranked})


class DraftBrainCalibrationTests(unittest.TestCase):
    def test_default_population_can_reverse_close_baseline_rankings(self):
        size = 64
        schema = FeatureSchema(
            tuple(f"f{index}" for index in range(size)),
            (0.0,) * size,
            (1.0,) * size,
        )
        baseline = RegressionBaseline(
            schema.feature_schema_id,
            (1.0, *((0.0,) * (size - 1))),
        )
        first = (0.05, *(((-1.0) ** index * 0.8 for index in range(1, size))))
        second = (0.0, *(((-1.0) ** (index + 1) * 0.8 for index in range(1, size))))
        choices = set()
        largest_adjustment = 0.0
        for index in range(32):
            brain = initialize_genome(
                schema, baseline, "league", seed=77, genome_index=index
            )
            first_score = brain.score_vector(first)
            second_score = brain.score_vector(second)
            choices.add(first_score > second_score)
            largest_adjustment = max(
                largest_adjustment,
                abs(first_score - baseline.score(first)),
                abs(second_score - baseline.score(second)),
            )

        self.assertEqual(choices, {False, True})
        self.assertLess(largest_adjustment, 1.0)


if __name__ == "__main__":
    unittest.main()
