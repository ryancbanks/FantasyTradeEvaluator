from datetime import datetime, timedelta, timezone
import copy
import json
import unittest

from tests.ecr_fixtures import ecr_source_provenance
from trade_snapshot.ecr import (
    EcrExpertPanel,
    EcrPeriod,
    EcrPlayerRanking,
    EcrSnapshot,
)


NOW = datetime(2026, 9, 1, 18, tzinfo=timezone.utc)


def ranking(player_id="p1", fantasypros_id="101", rank=1, *, position="rb"):
    return EcrPlayerRanking(
        canonical_player_id=player_id,
        fantasypros_player_id=fantasypros_id,
        position=position,
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
        total_experts=2,
        rankings=tuple(rankings or (ranking(), ranking("p2", "102", 2))),
        expert_panels=(EcrExpertPanel(
            "RB",
            ("22", "9"),
            2,
            ecr_source_provenance(
                captured_at=NOW,
                source_updated_at=NOW - timedelta(hours=2),
                source_player_count=2,
            ),
        ),),
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
        self.assertEqual(record["schema_version"], 4)
        self.assertEqual(record["expert_panels"][0]["position"], "RB")
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
                (EcrExpertPanel(
                    "RB",
                    ("9",),
                    1,
                    ecr_source_provenance(captured_at=NOW),
                ),),
            )

    def test_position_panels_preserve_exact_provenance_and_aggregate_union(self):
        result = EcrSnapshot(
            snapshot_id="snapshot-1",
            scoring_profile_id="profile-1",
            season=2026,
            as_of_week=1,
            period=EcrPeriod.WEEKLY,
            captured_at=NOW,
            source_updated_at=None,
            expert_ids=("wr-expert", "shared", "rb-expert"),
            total_experts=3,
            rankings=(
                ranking(),
                ranking("p2", "102", 2, position="WR"),
            ),
            expert_panels=(
                EcrExpertPanel(
                    "WR",
                    ("wr-expert", "shared"),
                    2,
                    ecr_source_provenance(captured_at=NOW, position="WR"),
                ),
                EcrExpertPanel(
                    "RB",
                    ("rb-expert", "shared"),
                    2,
                    ecr_source_provenance(captured_at=NOW),
                ),
            ),
        )

        self.assertEqual(
            tuple(panel.position for panel in result.expert_panels),
            ("RB", "WR"),
        )
        self.assertEqual(result.expert_ids, ("rb-expert", "shared", "wr-expert"))
        self.assertEqual(EcrSnapshot.from_record(result.to_record()), result)

        with self.assertRaisesRegex(ValueError, "position-panel union"):
            EcrSnapshot(
                snapshot_id="snapshot-1",
                scoring_profile_id="profile-1",
                season=2026,
                as_of_week=1,
                period=EcrPeriod.WEEKLY,
                captured_at=NOW,
                source_updated_at=None,
                expert_ids=("shared",),
                total_experts=1,
                rankings=result.rankings,
                expert_panels=result.expert_panels,
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
                (EcrExpertPanel(
                    "RB",
                    ("9",),
                    1,
                    ecr_source_provenance(captured_at=NOW),
                ),),
            )


if __name__ == "__main__":
    unittest.main()
