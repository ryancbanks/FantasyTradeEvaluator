from copy import deepcopy
from io import BytesIO
import json
import unittest
from unittest.mock import ANY, Mock, patch

from trade_snapshot._projection_parse import projection_artifact_rows
from trade_snapshot._projection_tables import projection_capture
from trade_snapshot.capture_schema import (
    CaptureKind,
    CaptureProvider,
    GenericTableArtifact,
    PageCaptureTask,
    ProjectionTableSpec,
)
from trade_snapshot.espn_projection_read import (
    EspnProjectionReadError,
    EspnSeasonProjectionClient,
    espn_season_projection_filter,
    espn_season_projection_segment,
    espn_season_projection_url,
)


class EspnProjectionReadTests(unittest.TestCase):
    def test_exact_scoring_urls_and_page_backed_filter(self):
        self.assertEqual(
            espn_season_projection_url(2026, "STD"),
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
            "2026/segments/0/leaguedefaults/1?view=kona_player_info",
        )
        self.assertIn("/leaguedefaults/3?", espn_season_projection_url(2026, "PPR"))
        self.assertIn("/leaguedefaults/8?", espn_season_projection_url(2026, "HALF"))
        value = json.loads(espn_season_projection_filter(2026))["players"]
        self.assertEqual(value["limit"], 5000)
        self.assertEqual(value["filterStatsForExternalIds"]["value"], [2026])
        self.assertEqual(value["filterStatsForSourceIds"]["value"], [1])
        self.assertTrue(value["useFullProjectionTable"]["value"])
        self.assertEqual(value["sortAppliedStatTotal"]["value"], "102026")
        self.assertNotIn("filterStatsForTopScoringPeriodIds", value)

    def test_sanitized_segment_is_accepted_by_existing_archive_parser(self):
        task = espn_task()
        private = payload()
        private["players"][0]["privateWrapper"] = "never archive"
        private["players"][0]["player"]["ownership"] = {"private": "never archive"}
        segment = espn_season_projection_segment(
            private,
            season=2026,
            scoring="HALF",
            league_format_id=8,
        )

        self.assertEqual(segment["source"]["week"], None)
        self.assertEqual(segment["source"]["horizon"], "ros")
        self.assertEqual(segment["source"]["scoring"], "HALF")
        self.assertEqual(segment["source"]["positions"], ["ALL"])
        self.assertIn("2 of 3 returned players projected", segment["source"]["period_text"])
        serialized = json.dumps(segment, sort_keys=True)
        self.assertNotIn("never archive", serialized)
        self.assertNotIn("No 2026 projection", serialized)
        self.assertNotIn("102025", serialized)
        headers = [cell["text"] for cell in segment["tables"][0]["rows"][0]]
        self.assertEqual(headers[:6], ["PLAYER", "TEAM", "POS", "GP", "FPTS", "FPPG"])
        self.assertEqual(
            headers[6:],
            [
                "CMP", "PASS ATT", "PASS YDS", "PASS TD", "INT", "RUSH ATT",
                "RUSH YDS", "RUSH TD", "REC", "TGT", "REC YDS", "REC TD",
                "FUM", "FUM LOST",
            ],
        )
        capture = projection_capture([segment], task)
        artifact = GenericTableArtifact(
            task_id=task.task_id,
            provider=task.provider,
            season=task.season,
            week=task.week,
            kind=task.kind,
            captured_at="2026-09-04T12:00:00Z",
            horizon=task.projection.horizon,
            scoring=task.projection.scoring,
            position_scope=task.projection.position_scope,
            source_period_text=capture.source_period_text,
            segments_captured=capture.segments_captured,
            complete=True,
            tables=capture.tables,
        )
        parsed = projection_artifact_rows(artifact)
        gibbs = next(row for row in parsed if row.display_name == "Jahmyr Gibbs")
        self.assertEqual(gibbs.provider_player_id, "4429795")
        self.assertEqual(gibbs.nfl_team_id, "DET")
        self.assertEqual(gibbs.position, "RB")
        self.assertAlmostEqual(gibbs.projected_fantasy_points, 331.69259203)
        self.assertEqual(dict(gibbs.raw_projected_stats)["rush_yds"], 1389.485562)
        defense = next(row for row in parsed if row.position == "DST")
        self.assertEqual(defense.provider_player_id, "-16034")
        self.assertEqual(defense.nfl_team_id, "HOU")

    def test_position_scope_filters_without_weakening_source_proof(self):
        segment = espn_season_projection_segment(
            payload(),
            season=2026,
            scoring="HALF",
            league_format_id=8,
            position_scope=("RB",),
        )
        self.assertEqual(segment["source"]["positions"], ["RB"])
        self.assertEqual(len(segment["tables"][0]["rows"]), 2)
        self.assertEqual(segment["tables"][0]["rows"][1][0]["text"], "Jahmyr Gibbs")

    def test_wrong_scoring_season_and_stat_provenance_are_rejected(self):
        with self.assertRaisesRegex(EspnProjectionReadError, "scoring"):
            espn_season_projection_segment(
                payload(), season=2026, scoring="HALF", league_format_id=3
            )
        wrong_season = payload()
        for wrapper in wrong_season["players"]:
            for stat in wrapper["player"]["stats"]:
                stat["seasonId"] = 2025
        with self.assertRaisesRegex(EspnProjectionReadError, "no exact"):
            espn_season_projection_segment(
                wrong_season, season=2026, scoring="HALF", league_format_id=8
            )
        wrong_id = payload()
        wrong_id["players"][0]["player"]["stats"][0]["id"] = "102025"
        with self.assertRaisesRegex(EspnProjectionReadError, "provenance"):
            espn_season_projection_segment(
                wrong_id, season=2026, scoring="HALF", league_format_id=8
            )

    def test_duplicate_truncated_nonfinite_and_inconsistent_rows_are_rejected(self):
        duplicate = payload()
        duplicate["players"].append(deepcopy(duplicate["players"][0]))
        with self.assertRaisesRegex(EspnProjectionReadError, "repeated a player"):
            project(duplicate)
        with self.assertRaisesRegex(EspnProjectionReadError, "complete player"):
            project({"players": [deepcopy(payload()["players"][0])] * 5000})
        nonfinite = payload()
        nonfinite["players"][0]["player"]["stats"][0]["stats"]["24"] = float("nan")
        with self.assertRaisesRegex(EspnProjectionReadError, "24"):
            project(nonfinite)
        inconsistent = payload()
        inconsistent["players"][0]["player"]["stats"][0]["appliedAverage"] = 99
        with self.assertRaisesRegex(EspnProjectionReadError, "inconsistent"):
            project(inconsistent)

    def test_client_makes_one_exact_bounded_credential_free_request(self):
        body = json.dumps(payload(), separators=(",", ":")).encode("utf-8")
        expected_url = espn_season_projection_url(2026, "HALF")
        response = FakeResponse(body, expected_url)
        calls = []

        def opener(request, *, timeout):
            calls.append((request, timeout))
            return response

        segment = EspnSeasonProjectionClient(opener=opener)(espn_task(), lambda: False)
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, expected_url)
        self.assertEqual(request.method, "GET")
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("X-fantasy-platform"), "espn-fantasy-web")
        self.assertEqual(request.get_header("X-fantasy-source"), "kona")
        self.assertEqual(
            json.loads(request.get_header("X-fantasy-filter")),
            json.loads(espn_season_projection_filter(2026)),
        )
        self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(segment["availability"], "available")

    def test_client_rejects_redirect_and_weekly_task(self):
        body = json.dumps(payload()).encode("utf-8")
        redirected = FakeResponse(body, espn_season_projection_url(2026, "PPR"))
        client = EspnSeasonProjectionClient(opener=lambda request, *, timeout: redirected)
        with self.assertRaisesRegex(EspnProjectionReadError, "unexpected"):
            client(espn_task(), lambda: False)
        with self.assertRaisesRegex(EspnProjectionReadError, "rest-of-season"):
            client(espn_task(horizon="weekly"), lambda: False)

    def test_playwright_ros_capture_uses_season_endpoint_not_dom_traversal(self):
        from trade_snapshot._playwright_capture import _PlaywrightSession

        task = espn_task()
        session = object.__new__(_PlaywrightSession)
        client = Mock(return_value=project(payload()))
        with patch(
            "trade_snapshot._playwright_capture.EspnSeasonProjectionClient",
            return_value=client,
        ) as factory:
            captured = session.capture_visible_tables(task, 5000, 200, lambda: False)

        factory.assert_called_once_with(timeout_seconds=5.0)
        client.assert_called_once_with(task, ANY)
        self.assertEqual(captured.segments_captured, 1)
        self.assertEqual(
            {row[0].text for row in captured.tables[0].rows[1:]},
            {"Jahmyr Gibbs", "Texans D/ST"},
        )


class FakeResponse(BytesIO):
    status = 200

    def __init__(self, body, url):
        super().__init__(body)
        self._url = url
        self.headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
        }

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def project(value):
    return espn_season_projection_segment(
        value, season=2026, scoring="HALF", league_format_id=8
    )


def espn_task(*, horizon="ros"):
    return PageCaptureTask(
        CaptureProvider.ESPN,
        2026,
        2,
        CaptureKind.VISIBLE_TABLE,
        "https://fantasy.espn.com/football/players/projections",
        projection=ProjectionTableSpec(horizon, "HALF", ("ALL",)),
    )


def payload():
    return {
        "players": [
            wrapper(
                4429795,
                "Jahmyr Gibbs",
                2,
                8,
                331.69259203,
                19.511328942647058,
                {
                    "23": 286.2066446,
                    "24": 1389.485562,
                    "25": 14.27355551,
                    "42": 546.4294512,
                    "43": 3.378463538,
                    "53": 67.94936963,
                    "58": 85.92577601,
                    "72": 1.271654266,
                    "73": 1.271654266,
                    "210": 17,
                    "999": 123456,
                },
            ),
            wrapper(-16034, "Texans D/ST", 16, 34, 122.4, 7.2, {"210": 17}),
            {
                "player": {
                    "id": 999,
                    "fullName": "No 2026 projection",
                    "defaultPositionId": 3,
                    "proTeamId": 0,
                    "stats": [{
                        "id": "102025",
                        "externalId": "2025",
                        "seasonId": 2025,
                        "statSourceId": 1,
                        "scoringPeriodId": 0,
                        "statSplitTypeId": 0,
                        "appliedTotal": 1,
                        "appliedAverage": 1,
                        "stats": {"210": 1},
                    }],
                }
            },
        ]
    }


def wrapper(player_id, name, position, team, total, average, stats):
    return {
        "player": {
            "id": player_id,
            "fullName": name,
            "defaultPositionId": position,
            "proTeamId": team,
            "stats": [{
                "id": "102026",
                "externalId": "2026",
                "seasonId": 2026,
                "statSourceId": 1,
                "scoringPeriodId": 0,
                "statSplitTypeId": 0,
                "appliedTotal": total,
                "appliedAverage": average,
                "stats": stats,
            }],
        }
    }


if __name__ == "__main__":
    unittest.main()
