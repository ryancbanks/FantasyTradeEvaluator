import unittest

from trade_snapshot.positions import (
    normalize_lineup_slot,
    normalize_player_position,
)


class PositionNormalizationTests(unittest.TestCase):
    def test_cross_provider_idp_and_offense_aliases_are_canonical(self):
        expected = {
            "D/ST": "DST",
            "DEF": "DST",
            "PK": "K",
            "FB": "RB",
            "DE": "DL",
            "DT": "DL",
            "NT": "DL",
            "EDGE": "DL",
            "ILB": "LB",
            "OLB": "LB",
            "CB": "DB",
            "S": "DB",
            "FS": "DB",
            "SS": "DB",
        }
        for raw, canonical in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_player_position(raw), canonical)

    def test_lineup_flex_slots_remain_slots_not_player_positions(self):
        self.assertEqual(normalize_lineup_slot("FLX"), "FLEX")
        self.assertEqual(normalize_lineup_slot("superflex"), "SFLX")
        self.assertEqual(normalize_lineup_slot("DT"), "DL")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalize_player_position("PUNTER", require_supported=True)


if __name__ == "__main__":
    unittest.main()
