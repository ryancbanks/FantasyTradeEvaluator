from pathlib import Path
import unittest

from trade_snapshot.player_outlook import (
    build_player_outlook_catalog,
    select_player_outlook_detail,
)


def full_outlook():
    return {
        "schema_version": 2,
        "bundle_id": f"engine_{'1' * 64}",
        "snapshot_id": "snapshot-1",
        "scoring_mode": "PPR",
        "providers": [],
        "players": [
            {
                "player_id": "sleeper:123",
                "name": "Example Player",
                "position": "WR",
                "weekly_ecr": {
                    "rank": 10,
                    "position_rank": 4,
                    "rank_min": 2,
                    "rank_max": 20,
                },
                "average_predictive_uncertainty": 4.2,
                "overall_rank": 10,
                "overall_rank_basis": "local remaining projection",
                "weeks": [{"week": 1, "provider_values": [{"provider": "espn"}]}],
                "provider_remaining_season": [{"provider": "espn"}],
                "profile": {
                    "provider_references": [
                        {"provider": "sleeper", "provider_player_id": "123"}
                    ],
                    "active": True,
                    "status": "Active",
                    "injury_status": "questionable",
                    "depth_chart": {"position": "WR", "order": 2},
                    "market_trend": {
                        "status": "unknown",
                        "adds": 14,
                        "drops": None,
                        "net_adds": None,
                        "direction": "unknown",
                        "method": "detail-only market methodology",
                    },
                    "performance_trend": {
                        "status": "observed",
                        "direction": "rising",
                        "change": 2.0,
                        "sample_size": 6,
                        "method": "detail-only trend methodology",
                    },
                    "current_season": {
                        "season": 2026,
                        "recorded_stat_lines": 1,
                        "weeks": [{"week": 1, "stat_values": {"targets": 0}}],
                    },
                    "previous_season": {
                        "season": 2025,
                        "recorded_stat_lines": 1,
                        "weeks": [{"week": 2}],
                    },
                    "historical_availability": {
                        "status": "observed",
                        "risk_score": 20.0,
                        "risk_tier": "lower",
                        "distinct_report_weeks": 1,
                        "exposure_status": "report_evidence",
                        "method": "detail-only repeated methodology",
                        "recorded_stat_line_exposure": [{"season": 2025, "week": 2}],
                        "out_weeks": [{"season": 2025, "week": 2}],
                        "doubtful_weeks": [],
                        "questionable_weeks": [],
                        "out_report_without_recorded_stat_line": [],
                        "weekly_evidence": [{"season": 2025, "week": 2}],
                    },
                },
            }
        ],
    }


class PlayerOutlookViewTests(unittest.TestCase):
    def test_player_lab_read_models_remain_split_by_responsibility(self):
        root = Path(__file__).resolve().parents[1] / "trade_snapshot"
        maximums = {
            "player_outlook.py": 800,
            "player_outlook_evidence.py": 400,
            "player_outlook_views.py": 300,
            "player_outlook_lazy.py": 500,
            "player_outlook_detail.py": 250,
        }
        for name, maximum in maximums.items():
            with self.subTest(name=name):
                self.assertLessEqual(
                    len((root / name).read_text(encoding="utf-8").splitlines()),
                    maximum,
                )

    def test_catalog_strips_only_heavy_detail_rows_without_mutating_full_view(self):
        full = full_outlook()
        catalog = build_player_outlook_catalog(full)
        player = catalog["players"][0]

        self.assertEqual(catalog["view"], "catalog")
        self.assertNotIn("weeks", player)
        self.assertNotIn("provider_remaining_season", player)
        self.assertNotIn("average_predictive_uncertainty", player)
        self.assertEqual(player["overall_rank"], player["weekly_ecr"]["rank"])
        self.assertEqual(player["overall_rank_basis"], "local remaining projection")
        self.assertNotIn("rank_min", player["weekly_ecr"])
        self.assertNotIn("current_season", player["profile"])
        self.assertNotIn("previous_season", player["profile"])
        self.assertNotIn("provider_references", player["profile"])
        self.assertNotIn("active", player["profile"])
        self.assertNotIn("injury_status", player["profile"])
        self.assertEqual(player["profile"]["depth_chart"]["order"], 2)
        for field in (
            "method",
            "recorded_stat_line_exposure",
            "out_weeks",
            "doubtful_weeks",
            "questionable_weeks",
            "out_report_without_recorded_stat_line",
            "weekly_evidence",
        ):
            self.assertNotIn(field, player["profile"]["historical_availability"])
        self.assertEqual(
            player["profile"]["historical_availability"],
            {
                "status": "observed",
                "burden_index": 20.0,
                "burden_tier": "lower",
            },
        )
        self.assertEqual(player["profile"]["market_trend"]["adds"], 14)
        self.assertNotIn("method", player["profile"]["market_trend"])
        self.assertEqual(player["profile"]["performance_trend"]["change"], 2.0)
        self.assertNotIn("sample_size", player["profile"]["performance_trend"])
        self.assertIn("weeks", full["players"][0])
        self.assertIn("weeks", full["players"][0]["profile"]["current_season"])

    def test_detail_selects_an_exact_id_and_rejects_unknown_ids(self):
        full = full_outlook()
        detail = select_player_outlook_detail(full, "sleeper:123")

        self.assertEqual(detail["view"], "player_detail")
        self.assertEqual(detail["player"]["player_id"], "sleeper:123")
        self.assertIn("weeks", detail["player"])
        with self.assertRaises(KeyError):
            select_player_outlook_detail(full, "sleeper:12")
        with self.assertRaises(ValueError):
            select_player_outlook_detail(full, "")


if __name__ == "__main__":
    unittest.main()
