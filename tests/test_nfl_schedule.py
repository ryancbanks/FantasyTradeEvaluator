from copy import deepcopy
from datetime import datetime, timezone
import unittest

from trade_snapshot.nfl_schedule import (
    NflTeamWeekStatus,
    canonical_nfl_game_id,
    parse_espn_pro_team_schedule,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def pro_team_payload():
    schedules = {team_id: {} for team_id in range(1, 33)}
    order = list(range(1, 33))
    for week in range(1, 18):
        for pair_index in range(16):
            left = order[pair_index]
            right = order[-pair_index - 1]
            away, home = (left, right) if (week + pair_index) % 2 else (right, left)
            game = {
                "awayProTeamId": away,
                "date": 1788267600000 + (week - 1) * 7 * 24 * 60 * 60 * 1000,
                "homeProTeamId": home,
                "id": 401900000 + (week - 1) * 16 + pair_index,
                "scoringPeriodId": week,
                "startTimeTBD": False,
                "statsOfficial": False,
                "validForLocking": True,
            }
            schedules[left][str(week)] = [deepcopy(game)]
            schedules[right][str(week)] = [deepcopy(game)]
        order = [order[0], order[-1], *order[1:-1]]
    teams = [
        {
            "abbrev": f"T{team_id:02d}",
            "byeWeek": 18,
            "id": team_id,
            "location": f"Location {team_id}",
            "name": f"Team {team_id}",
            "proGamesByScoringPeriod": schedules[team_id],
            "teamPlayersByPosition": {},
            "universeId": team_id,
        }
        for team_id in range(1, 33)
    ]
    teams.append(
        {
            "abbrev": "FA",
            "byeWeek": 0,
            "id": 0,
            "location": "",
            "name": "FA",
            "proGamesByScoringPeriod": {},
            "universeId": 0,
        }
    )
    return {
        "display": True,
        "settings": {
            "defaultDraftPosition": 1,
            "draftLobbyMinimumLeagueCount": 1,
            "gameNotificationSettings": {},
            "gated": False,
            "playerOwnershipSettings": {},
            "proTeams": teams,
            "readOnly": False,
            "statIdToOverridePosition": {},
            "teamActivityEnabled": True,
            "typeNames": {},
        },
    }


class NflScheduleTests(unittest.TestCase):
    def test_parses_complete_reciprocal_games_and_keeps_byes_explicit(self):
        result = parse_espn_pro_team_schedule(
            pro_team_payload(), season=2026, captured_at=NOW
        )

        self.assertEqual(result.season, 2026)
        self.assertEqual(result.source_provider, "espn")
        self.assertTrue(result.schedule_id.startswith("nfl-schedule_"))
        self.assertEqual(len(result.team_weeks), 32 * 18)
        first = result.team_week("T01", 1)
        self.assertEqual(first.status, NflTeamWeekStatus.SCHEDULED)
        self.assertEqual(
            first.nfl_game_id,
            canonical_nfl_game_id(2026, 1, "T01", first.opponent_team_id),
        )
        self.assertIsNotNone(first.source_game_id)
        self.assertIsNotNone(first.kickoff_at)
        self.assertEqual(
            result.team_week("T01", 18).status,
            NflTeamWeekStatus.BYE,
        )
        self.assertEqual(
            len(
                {
                    row.source_game_id
                    for row in result.team_weeks
                    if row.source_game_id is not None
                }
            ),
            272,
        )

    def test_rejects_consumed_schema_drift(self):
        payload = pro_team_payload()
        payload["settings"]["proTeams"][0]["proGamesByScoringPeriod"]["1"][0][
            "newField"
        ] = "unknown"
        with self.assertRaisesRegex(ValueError, "missing or unknown fields"):
            parse_espn_pro_team_schedule(payload, season=2026, captured_at=NOW)

    def test_rejects_conflicting_duplicate_game_appearances(self):
        payload = pro_team_payload()
        first = payload["settings"]["proTeams"][0]["proGamesByScoringPeriod"]["1"][0]
        game_id = first["id"]
        changed = False
        for team in payload["settings"]["proTeams"]:
            for games in team["proGamesByScoringPeriod"].values():
                if games[0]["id"] == game_id and games[0] is not first:
                    games[0]["date"] += 1
                    changed = True
                    break
            if changed:
                break
        self.assertTrue(changed)
        with self.assertRaisesRegex(ValueError, "identical team appearances"):
            parse_espn_pro_team_schedule(payload, season=2026, captured_at=NOW)

    def test_rejects_a_game_in_the_declared_bye_week(self):
        payload = pro_team_payload()
        team = payload["settings"]["proTeams"][0]
        team["proGamesByScoringPeriod"]["18"] = [
            deepcopy(team["proGamesByScoringPeriod"]["17"][0])
        ]
        with self.assertRaisesRegex(ValueError, "explicit bye"):
            parse_espn_pro_team_schedule(payload, season=2026, captured_at=NOW)


if __name__ == "__main__":
    unittest.main()
