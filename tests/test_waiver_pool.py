import copy
import unittest

from trade_snapshot.waiver_pool import (
    WaiverCandidate,
    WaiverPool,
    WaiverPoolSource,
    required_waiver_positions,
    select_waiver_pool,
    waiver_eligible_slots,
)


def candidate(player_id, position, rank):
    flex = ("FLEX",) if position in {"RB", "WR", "TE"} else ()
    return WaiverCandidate(
        f"fantasypros:{player_id}",
        player_id,
        f"Player {player_id}",
        position,
        "ARI",
        (position, *flex),
        rank,
    )


class WaiverPoolTests(unittest.TestCase):
    def test_position_completion_and_eligibility_follow_captured_slots(self):
        starting = ("QB", "RB", "WR", "TE", "FLEX", "OP", "K", "DST")
        self.assertEqual(
            required_waiver_positions(
                starting,
                ("QB", "RB", "WR", "TE", "K", "DST"),
            ),
            ("DST", "K", "QB", "RB", "TE", "WR"),
        )
        self.assertEqual(
            waiver_eligible_slots("RB", starting),
            ("FLEX", "OP", "RB"),
        )

    def test_composite_flex_slots_keep_their_position_boundaries(self):
        starting = ("RB_WR", "WR_TE", "FLEX", "OP")

        self.assertEqual(
            waiver_eligible_slots("RB", starting),
            ("FLEX", "OP", "RB", "RB_WR"),
        )
        self.assertEqual(
            waiver_eligible_slots("WR", starting),
            ("FLEX", "OP", "RB_WR", "WR", "WR_TE"),
        )
        self.assertEqual(
            waiver_eligible_slots("TE", starting),
            ("FLEX", "OP", "TE", "WR_TE"),
        )
        self.assertEqual(waiver_eligible_slots("QB", starting), ("OP", "QB"))

    def test_preserves_exact_best_ids_and_labels_deterministic_ecr_fill(self):
        pool = select_waiver_pool(
            snapshot_id="week-1",
            scoring_profile_id="scoring-1",
            candidates=(
                candidate("104", "WR", 4),
                candidate("103", "RB", 3),
                candidate("105", "TE", 5),
                candidate("101", "RB", 1),
                candidate("102", "WR", 2),
            ),
            fantasypros_best_player_ids=("103",),
            required_positions=("RB", "WR", "TE"),
            minimum_pool_size=4,
        )

        self.assertEqual(
            pool.player_ids,
            (
                "fantasypros:103",
                "fantasypros:102",
                "fantasypros:105",
                "fantasypros:101",
            ),
        )
        self.assertIs(pool.players[0].source, WaiverPoolSource.FANTASYPROS_BEST)
        self.assertEqual(pool.fantasypros_best_player_ids, ("103",))
        self.assertEqual(
            pool.fantasypros_best_canonical_player_ids,
            ("fantasypros:103",),
        )
        self.assertTrue(
            all(
                row.source is WaiverPoolSource.ECR_AUGMENTATION
                for row in pool.players[1:]
            )
        )
        self.assertEqual(WaiverPool.from_record(pool.to_record()), pool)

    def test_missing_exact_best_or_position_coverage_fails_closed(self):
        rows = (candidate("101", "RB", 1), candidate("102", "RB", 2))
        with self.assertRaisesRegex(ValueError, "best free agent.*lacks complete"):
            select_waiver_pool(
                snapshot_id="week-1",
                scoring_profile_id="scoring-1",
                candidates=rows,
                fantasypros_best_player_ids=("999",),
                required_positions=("RB",),
                minimum_pool_size=1,
            )
        with self.assertRaisesRegex(ValueError, "required position"):
            select_waiver_pool(
                snapshot_id="week-1",
                scoring_profile_id="scoring-1",
                candidates=rows,
                fantasypros_best_player_ids=("101",),
                required_positions=("RB", "WR"),
                minimum_pool_size=1,
            )

    def test_pool_identity_rejects_tampering(self):
        pool = select_waiver_pool(
            snapshot_id="week-1",
            scoring_profile_id="scoring-1",
            candidates=(candidate("101", "RB", 1),),
            fantasypros_best_player_ids=("101",),
            required_positions=("RB",),
            minimum_pool_size=1,
        )
        record = copy.deepcopy(pool.to_record())
        record["players"][0]["display_name"] = "Changed"
        with self.assertRaisesRegex(ValueError, "waiver_pool_id"):
            WaiverPool.from_record(record)

    def test_provider_ids_source_order_and_pool_bound_are_intrinsic(self):
        with self.assertRaisesRegex(ValueError, "iterable of IDs"):
            select_waiver_pool(
                snapshot_id="week-1",
                scoring_profile_id="scoring-1",
                candidates=(candidate("101", "RB", 1),),
                fantasypros_best_player_ids="101",
                required_positions=("RB",),
                minimum_pool_size=1,
            )

        pool = select_waiver_pool(
            snapshot_id="week-1",
            scoring_profile_id="scoring-1",
            candidates=(
                candidate(str(player_id), "RB", rank)
                for rank, player_id in enumerate(range(101, 118), 1)
            ),
            fantasypros_best_player_ids=("101",),
            required_positions=("RB",),
            minimum_pool_size=16,
        )
        reversed_sources = (*pool.players[1:], pool.players[0])
        with self.assertRaisesRegex(ValueError, "source order"):
            WaiverPool(
                pool.snapshot_id,
                pool.scoring_profile_id,
                pool.required_positions,
                pool.minimum_pool_size,
                reversed_sources,
            )

        oversized = tuple(
            type(pool.players[0])(
                f"fantasypros:{player_id}",
                str(player_id),
                f"Player {player_id}",
                "RB",
                "ARI",
                ("RB",),
                rank,
                WaiverPoolSource.FANTASYPROS_BEST
                if rank == 1
                else WaiverPoolSource.ECR_AUGMENTATION,
                1 if rank == 1 else rank - 1,
            )
            for rank, player_id in enumerate(range(1001, 1066), 1)
        )
        with self.assertRaisesRegex(ValueError, "deterministic bound"):
            WaiverPool("week-1", "scoring-1", ("RB",), 16, oversized)


if __name__ == "__main__":
    unittest.main()
