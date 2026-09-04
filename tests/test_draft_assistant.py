from dataclasses import replace
import unittest
from unittest.mock import patch

import trade_snapshot.draft_assistant as draft_assistant_module
from tests.draft_fixtures import small_draft_config, small_historical_corpus
from trade_snapshot.draft_assistant import (
    AssistantDraftBinding,
    DraftAssistantSession,
    assistant_board_coverage,
    assistant_status,
    bind_assistant_draft,
    create_assistant_session,
    drafter_for_pick,
    reconcile_assistant_picks,
    record_assistant_pick,
    undo_assistant_pick,
)
from trade_snapshot.draft_features import build_baseline_brain
from trade_snapshot.draft_brain import FeatureSchema
from trade_snapshot.draft_history import DraftPlayerBoard
from trade_snapshot.draft_persistence import DraftModelArtifact


class DraftAssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = small_historical_corpus()
        cls.config = small_draft_config()
        cls.brain = build_baseline_brain(cls.corpus, cls.config, (2025,))
        cls.model = DraftModelArtifact(
            cls.brain, cls.config, cls.corpus.corpus_id, (2025,), 1,
            {"fitness": 10}, "2026-09-02T12:00:00+00:00",
        )
        season = cls.corpus.seasons[0]
        cls.board = DraftPlayerBoard(
            2026, "2026-08-15T12:00:00+00:00", "2026-09-03T00:00:00+00:00",
            tuple(replace(player, player_id=f"new-{player.player_id}", actual_weeks=())
                  for player in season.players),
        )

    def test_manual_picks_drive_turn_aware_recommendations_and_undo(self):
        session = create_assistant_session(
            self.model, self.board, user_drafter_number=1, session_id="a" * 32
        )
        first = assistant_status(session, self.model, self.board, recommendation_limit=5)
        self.assertTrue(first["your_turn"])
        self.assertEqual(len(first["recommendations"]), 5)
        self.assertEqual(first["board_coverage"], {
            "status": "ready",
            "board_player_count": 24,
            "usable_player_count": 24,
            "required_usable_player_count": 20,
            "model_preseason_feature_count": 3,
            "feasibility": {
                "status": "ready",
                "scope": "all_teams",
                "starting_slots_checked": 16,
                "roster_slots_checked": 20,
            },
        })
        chosen = first["recommendations"][0]["player_id"]
        session = record_assistant_pick(
            session, self.model, self.board, player_id=chosen, drafter_number=1
        )
        waiting = assistant_status(session, self.model, self.board)
        self.assertFalse(waiting["your_turn"])
        self.assertEqual(waiting["recommendations"], [])
        self.assertEqual(waiting["live_sync"]["status"], "manual")
        self.assertIsNone(waiting["draft_binding"])
        restored = DraftAssistantSession.from_record(session.to_record())
        self.assertEqual(restored, session)
        self.assertEqual(session.to_record()["schema_version"], 2)
        self.assertEqual(undo_assistant_pick(session).picks, ())

    def test_board_coverage_cache_is_copy_safe(self):
        with draft_assistant_module._BOARD_COVERAGE_CACHE_LOCK:
            draft_assistant_module._BOARD_COVERAGE_CACHE.clear()
        original = draft_assistant_module.validate_player_supply
        with patch.object(
            draft_assistant_module,
            "validate_player_supply",
            wraps=original,
        ) as validate:
            first = assistant_board_coverage(
                self.model,
                self.board,
                user_drafter_number=1,
                strategy=draft_assistant_module.DraftStrategy.NONE,
            )
            first["feasibility"]["status"] = "changed by caller"
            second = assistant_board_coverage(
                self.model,
                self.board,
                user_drafter_number=1,
                strategy=draft_assistant_module.DraftStrategy.NONE,
            )

        self.assertEqual(validate.call_count, 1)
        self.assertEqual(second["feasibility"]["status"], "ready")

    def test_recommendations_reuse_the_bounded_board_encoding_cache(self):
        session = create_assistant_session(
            self.model, self.board, user_drafter_number=1,
            session_id="9" * 32,
        )
        with draft_assistant_module._ASSISTANT_RANK_CACHE_LOCK:
            draft_assistant_module._ASSISTANT_RANK_CACHE.clear()
        original_encode = FeatureSchema.encode

        with patch.object(
            FeatureSchema,
            "encode",
            autospec=True,
            side_effect=original_encode,
        ) as encode:
            first = assistant_status(session, self.model, self.board)
            first_encode_count = encode.call_count
            second = assistant_status(session, self.model, self.board)
            second_encode_count = encode.call_count - first_encode_count
            replacement_board = replace(self.board)
            replacement = assistant_status(session, self.model, replacement_board)

        self.assertEqual(first["recommendations"], second["recommendations"])
        self.assertEqual(first["recommendations"], replacement["recommendations"])
        self.assertLess(second_encode_count, first_encode_count)
        with draft_assistant_module._ASSISTANT_RANK_CACHE_LOCK:
            self.assertLessEqual(
                len(draft_assistant_module._ASSISTANT_RANK_CACHE),
                draft_assistant_module._MAX_ASSISTANT_RANK_CACHE_SIZE,
            )
            entry = next(iter(draft_assistant_module._ASSISTANT_RANK_CACHE.values()))
            self.assertIs(entry[0].season, replacement_board)

    def test_reconciliation_is_idempotent_and_conflicts_fail(self):
        session = create_assistant_session(
            self.model, self.board, user_drafter_number=2, session_id="b" * 32
        )
        players = [row.player_id for row in self.board.players]
        observed = [(1, players[0]), (2, players[1])]
        once = reconcile_assistant_picks(session, self.model, self.board, observed)
        twice = reconcile_assistant_picks(once, self.model, self.board, observed)
        self.assertIs(once, twice)
        with self.assertRaisesRegex(ValueError, "conflicts at pick 1"):
            reconcile_assistant_picks(once, self.model, self.board, [(1, players[2])])

    def test_public_draft_binding_is_persistent_and_cannot_be_reassigned(self):
        session = create_assistant_session(
            self.model, self.board, user_drafter_number=2, session_id="d" * 32
        )
        binding = AssistantDraftBinding(
            "espn", "123", 2026, ("40", "20", "10", "30")
        )

        bound = bind_assistant_draft(session, binding)
        self.assertIs(bind_assistant_draft(bound, binding), bound)
        self.assertEqual(
            assistant_status(bound, self.model, self.board)["draft_binding"],
            binding.to_record(),
        )
        with self.assertRaisesRegex(ValueError, "different public draft"):
            bind_assistant_draft(
                bound,
                AssistantDraftBinding(
                    "espn", "456", 2026, ("40", "20", "10", "30")
                ),
            )

    def test_legacy_v1_session_reads_unbound_and_serializes_as_v2(self):
        legacy = {
            "kind": "draft_assistant_session",
            "schema_version": 1,
            "session_id": "e" * 32,
            "model_id": self.model.model_id,
            "board_id": self.board.board_id,
            "user_drafter_number": 1,
            "strategy": "none",
            "picks": [],
        }

        restored = DraftAssistantSession.from_record(legacy)

        self.assertIsNone(restored.draft_binding)
        self.assertEqual(restored.to_record()["schema_version"], 2)
        self.assertIsNone(restored.to_record()["draft_binding"])

    def test_board_requires_roster_capacity_of_players_with_real_model_inputs(self):
        empty_players = tuple(
            replace(
                player,
                preseason_features={name: None for name in player.preseason_features},
            )
            for player in self.board.players
        )
        one_usable_player = replace(
            empty_players[0],
            preseason_features={
                **empty_players[0].preseason_features,
                "projected_points": 100.0,
            },
        )
        for expected, players in (
            (0, empty_players),
            (1, (one_usable_player, *empty_players[1:])),
        ):
            with self.subTest(usable_players=expected):
                board = DraftPlayerBoard(
                    2026,
                    "2026-08-15T12:00:00+00:00",
                    "2026-09-03T00:00:00+00:00",
                    players,
                )
                with self.assertRaisesRegex(ValueError, f"found {expected}"):
                    create_assistant_session(
                        self.model, board, user_drafter_number=1,
                        session_id="f" * 32,
                    )

    def test_board_declares_every_model_feature_but_allows_explicit_nulls(self):
        omitted_players = tuple(
            replace(
                player,
                preseason_features={
                    name: value
                    for name, value in player.preseason_features.items()
                    if name != "projected_stat.touchdowns"
                },
            )
            for player in self.board.players
        )
        omitted = DraftPlayerBoard(
            2026,
            "2026-08-15T12:00:00+00:00",
            "2026-09-03T00:00:00+00:00",
            omitted_players,
        )
        with self.assertRaisesRegex(
            ValueError,
            "draft board omits model preseason feature "
            "'projected_stat.touchdowns'",
        ):
            create_assistant_session(
                self.model, omitted, user_drafter_number=1,
                session_id="1" * 32,
            )

        nullable = DraftPlayerBoard(
            2026,
            "2026-08-15T12:00:00+00:00",
            "2026-09-03T00:00:00+00:00",
            tuple(
                replace(
                    player,
                    preseason_features={
                        **player.preseason_features,
                        "projected_stat.optional_metric": None,
                    },
                )
                for player in self.board.players
            ),
        )
        session = create_assistant_session(
            self.model, nullable, user_drafter_number=1,
            session_id="2" * 32,
        )
        self.assertEqual(session.board_id, nullable.board_id)

    def test_snake_order_and_wrong_manual_team_are_rejected(self):
        self.assertEqual([drafter_for_pick(i, 4) for i in range(1, 9)], [
            1, 2, 3, 4, 4, 3, 2, 1
        ])
        session = create_assistant_session(
            self.model, self.board, user_drafter_number=1, session_id="c" * 32
        )
        with self.assertRaisesRegex(ValueError, "belongs to Drafter #1"):
            record_assistant_pick(
                session, self.model, self.board,
                player_id=self.board.players[0].player_id, drafter_number=2,
            )


if __name__ == "__main__":
    unittest.main()
