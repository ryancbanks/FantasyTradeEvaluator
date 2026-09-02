from datetime import datetime, timedelta, timezone
import copy
import json
import unittest

from trade_snapshot.ecr import EcrPeriod, EcrPlayerRanking, EcrSnapshot


NOW = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)


def ranking(player_id="p1", fantasypros_id="101", rank=1):
    return EcrPlayerRanking(
        canonical_player_id=player_id,
        fantasypros_player_id=fantasypros_id,
        position="rb",
        rank_ecr=rank,
        position_rank=rank,
        rank_min=rank,
        rank_max=rank + 2,
        rank_average=rank + 1,
        rank_stddev=0.75,
    )


def snapshot(rankings=None):
    return EcrSnapshot(
        snapshot_id="snapshot-1",
        scoring_profile_id="profile-1",
        season=2026,
        as_of_week=1,
        period=EcrPeriod.WEEKLY,
        captured_at=NOW,
        source_updated_at=NOW - timedelta(hours=2),
        expert_ids=("22", "9"),
        total_experts=3,
        rankings=tuple(rankings or (ranking(), ranking("p2", "102", 2))),
    )


class EcrSnapshotTests(unittest.TestCase):
    def test_is_order_independent_content_addressed_and_json_round_trips(self):
        first = snapshot()
        second = snapshot(tuple(reversed(first.rankings)))

        self.assertEqual(first, second)
        self.assertEqual(first.expert_ids, ("22", "9"))
        self.assertEqual(first.rankings[0].canonical_player_id, "p1")
        self.assertEqual(first.rankings[0].position, "RB")
        record = first.to_record()
        json.dumps(record, allow_nan=False)
        self.assertEqual(EcrSnapshot.from_record(record), first)
        self.assertTrue(first.ecr_id.startswith("ecr_"))

    def test_rejects_tampering_unknown_fields_and_bad_time_order(self):
        record = snapshot().to_record()
        tampered = copy.deepcopy(record)
        tampered["rankings"][0]["rank_ecr"] = 99
        with self.assertRaisesRegex(ValueError, "does not match ecr_id"):
            EcrSnapshot.from_record(tampered)

        unknown = copy.deepcopy(record)
        unknown["cookie"] = "secret"
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            EcrSnapshot.from_record(unknown)

        with self.assertRaisesRegex(ValueError, "cannot be after"):
            EcrSnapshot(
                "snapshot-1",
                "profile-1",
                2026,
                1,
                EcrPeriod.REST_OF_SEASON,
                NOW,
                NOW + timedelta(seconds=1),
                (),
                0,
                (ranking(),),
            )

    def test_rejects_duplicate_identities_and_invalid_rank_statistics(self):
        with self.assertRaisesRegex(ValueError, "duplicate player identity"):
            snapshot((ranking(), ranking("p2", "101", 2)))
        with self.assertRaisesRegex(ValueError, "between"):
            EcrPlayerRanking("p", "1", "RB", 1, 1, 2, 3, 1.5, 0)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            EcrSnapshot(
                "snapshot-1",
                "profile-1",
                2026,
                26,
                EcrPeriod.WEEKLY,
                NOW,
                None,
                (),
                0,
                (ranking(),),
            )


if __name__ == "__main__":
    unittest.main()
