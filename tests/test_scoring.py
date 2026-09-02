from dataclasses import FrozenInstanceError
import math
import unittest

from trade_snapshot.scoring import ScoringProfile


class ScoringProfileTests(unittest.TestCase):
    def test_identifier_is_content_addressed_and_order_independent(self):
        first = ScoringProfile(
            "espn",
            {
                "passing": {"touchdown": 4.0, "yards_per_point": 25},
                "reception": 1,
            },
        )
        same = ScoringProfile(
            "espn",
            {
                "reception": 1.0,
                "passing": {"yards_per_point": 25.0, "touchdown": 4},
            },
        )
        changed = ScoringProfile(
            "espn",
            {
                "reception": 0.5,
                "passing": {"yards_per_point": 25, "touchdown": 4},
            },
        )

        self.assertEqual(first.scoring_profile_id, same.scoring_profile_id)
        self.assertNotEqual(first.scoring_profile_id, changed.scoring_profile_id)
        self.assertNotEqual(
            first.scoring_profile_id,
            ScoringProfile("yahoo", first.to_record()["settings"]).scoring_profile_id,
        )

    def test_settings_are_defensively_copied_and_deeply_immutable(self):
        supplied = {"bonuses": [{"threshold": 100, "points": 2}]}
        profile = ScoringProfile("custom", supplied)
        supplied["bonuses"][0]["points"] = 99

        self.assertEqual(profile.settings["bonuses"][0]["points"], 2)
        with self.assertRaises(TypeError):
            profile.settings["new"] = 1
        with self.assertRaises(TypeError):
            profile.settings["bonuses"][0]["points"] = 3
        with self.assertRaises(FrozenInstanceError):
            profile.platform = "other"

    def test_record_round_trip_recomputes_and_verifies_the_hash(self):
        profile = ScoringProfile("espn", {"reception": 1, "return_yards": 0.1})
        record = profile.to_record()

        self.assertEqual(ScoringProfile.from_record(record), profile)
        record["settings"]["reception"] = 0
        with self.assertRaisesRegex(ValueError, "does not match"):
            ScoringProfile.from_record(record)

    def test_rejects_incomplete_or_nonportable_settings(self):
        for settings in (
            {},
            {1: "bad key"},
            {"bad": math.nan},
            {"bad": math.inf},
            {"bad": object()},
            {"bad": 10**100},
            {"bad": float(2**53)},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    ScoringProfile("espn", settings)

        for version in (True, 1.0, "1"):
            with self.subTest(schema_version=version):
                with self.assertRaisesRegex(ValueError, "schema_version"):
                    ScoringProfile("espn", {"reception": 1}, schema_version=version)

        profile = ScoringProfile("espn", {"reception": 1})
        record = profile.to_record()
        record["unknown"] = True
        with self.assertRaisesRegex(ValueError, "missing or unknown"):
            ScoringProfile.from_record(record)


if __name__ == "__main__":
    unittest.main()
