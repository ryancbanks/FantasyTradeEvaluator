from copy import deepcopy
from datetime import datetime, timezone
import unittest

from trade_snapshot.espn_league import espn_host_league_snapshot
from trade_snapshot.identity_match import reconcile_player_identities
from trade_snapshot.league_ingest import host_player_records, normalize_host_league_snapshot
from trade_snapshot.league_state import Tiebreaker
from trade_snapshot.trade_space import TradeConstraints, TradeSpace


NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def scoring_settings():
    return {
        "allowOutOfPositionScoring": False,
        "homeTeamBonus": 0,
        "matchupTieRule": "NONE",
        "matchupTieRuleBy": 0,
        "playerRankType": "PPR",
        "playoffHomeTeamBonus": 0,
        "playoffMatchupTieRule": "NONE",
        "playoffMatchupTieRuleBy": 0,
        "scoringItems": [{"statId": 53, "points": 1}],
        "scoringType": "H2H_POINTS",
    }


def player(player_id, name, pro_team_id):
    return {
        "id": player_id,
        "fullName": name,
        "defaultPositionId": 1,
        "proTeamId": pro_team_id,
        "eligibleSlots": [0, 20, 21],
    }


def team(team_id, name, player_id, player_name, pro_team_id, *, won):
    return {
        "id": team_id,
        "name": name,
        "abbrev": name[:3].upper(),
        "divisionId": team_id,
        "record": {"overall": {
            "wins": 1 if won else 0,
            "losses": 0 if won else 1,
            "ties": 0,
            "pointsFor": 100 if won else 90,
            "pointsAgainst": 90 if won else 100,
        }},
        "roster": {"entries": [{
            "playerId": player_id,
            "lineupSlotId": 0,
            "playerPoolEntry": {"player": player(player_id, player_name, pro_team_id)},
        }]},
        "owners": ["private-member-id"],
    }


def league_payload():
    return {
        "id": 77,
        "seasonId": 2026,
        "scoringPeriodId": 2,
        "members": [{"displayName": "private"}],
        "status": {"currentMatchupPeriod": 2, "finalScoringPeriod": 3},
        "settings": {
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "20": 0, "21": 1}},
            "scheduleSettings": {
                "divisions": [
                    {"id": 1, "name": "One", "size": 1},
                    {"id": 2, "name": "Two", "size": 1},
                ],
                "matchupPeriodCount": 2,
                "playoffTeamCount": 2,
                "playoffReseed": False,
                "playoffSeedingRule": "TOTAL_POINTS_SCORED",
            },
            "scoringSettings": scoring_settings(),
        },
        "teams": [
            team(1, "Alpha", 101, "Player One", 1, won=True),
            team(2, "Bravo", 102, "Player Two", 2, won=False),
        ],
        "schedule": [
            {"id": 1, "matchupPeriodId": 1, "winner": "HOME",
             "home": {"teamId": 1, "totalPoints": 100},
             "away": {"teamId": 2, "totalPoints": 90}},
            {"id": 2, "matchupPeriodId": 2, "winner": "UNDECIDED",
             "home": {"teamId": 2, "totalPoints": 0},
             "away": {"teamId": 1, "totalPoints": 0}},
        ],
    }


def pro_team_payload():
    return {"settings": {"proTeams": [
        {"id": index, "abbrev": "FA" if index == 0 else "ARI" if index == 1 else "ATL"}
        for index in range(33)
    ]}}


class EspnLeagueAdapterTests(unittest.TestCase):
    def test_rejects_rostered_unassigned_player_before_schedule_assembly(self):
        payload = league_payload()
        payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"][
            "proTeamId"
        ] = 0

        with self.assertRaisesRegex(
            ValueError,
            r"Player One.*101.*unassigned NFL free agent.*proTeamId=0.*"
            r"weekly projections and playoff odds cannot be calculated safely",
        ):
            espn_host_league_snapshot(
                payload,
                pro_team_payload(),
                captured_at=NOW,
                expected_team_count=2,
            )

    def test_builds_complete_provider_neutral_snapshot_and_drops_private_members(self):
        snapshot = espn_host_league_snapshot(
            league_payload(), pro_team_payload(), captured_at=NOW, expected_team_count=2
        )
        self.assertEqual(snapshot.source_provider, "espn")
        self.assertEqual(snapshot.source_league_id, "77")
        self.assertEqual(snapshot.first_remaining_week, 2)
        self.assertEqual(snapshot.roster_rules.roster_cap, 1)
        self.assertEqual(snapshot.roster_rules.starting_lineup_slots, ("QB",))
        self.assertEqual(dict(snapshot.roster_rules.reserve_slot_counts), {"IR": 1})
        self.assertEqual(len(snapshot.completed_matchups), 1)
        self.assertEqual(len(snapshot.remaining_matchups), 1)
        self.assertTrue(snapshot.playoff_rules.division_winner_qualifier_count, 2)
        self.assertEqual(
            snapshot.playoff_rules.tiebreaker_order,
            (
                Tiebreaker.WIN_PERCENTAGE,
                Tiebreaker.POINTS_FOR,
                Tiebreaker.HEAD_TO_HEAD,
                Tiebreaker.DIVISION_RECORD,
                Tiebreaker.POINTS_AGAINST,
                Tiebreaker.RANDOM_DRAW,
            ),
        )
        self.assertNotIn("private", snapshot.scoring_profile.canonical_json)

    def test_carries_regular_season_home_bonus_without_reapplying_completed_score(self):
        without_bonus = espn_host_league_snapshot(
            league_payload(),
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )
        payload = league_payload()
        payload["settings"]["scoringSettings"]["homeTeamBonus"] = 1

        snapshot = espn_host_league_snapshot(
            payload,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )

        self.assertEqual(snapshot.remaining_matchups[0].source_team1_id, "2")
        self.assertEqual(snapshot.remaining_matchups[0].team1_score_adjustment, 1.0)
        self.assertEqual(snapshot.completed_matchups[0].team1_score, 100.0)
        self.assertEqual(snapshot.completed_matchups[0].team2_score, 90.0)
        self.assertNotEqual(snapshot.snapshot_id, without_bonus.snapshot_id)
        normalized = normalize_host_league_snapshot(
            snapshot,
            reconcile_player_identities(
                host_player_records(snapshot),
                anchor_provider="espn",
            ),
        )
        self.assertEqual(
            normalized.league_state.remaining_matchups[0].team1_score_adjustment,
            1.0,
        )
        self.assertEqual(
            normalized.league_state.remaining_matchups[0].team1_id,
            "espn:team:2",
        )

    def test_dynamic_lineup_and_reserve_rules_preserve_distinct_espn_slots(self):
        payload = league_payload()
        payload["settings"]["rosterSettings"]["lineupSlotCounts"] = {
            "0": 1,
            "2": 2,
            "3": 1,
            "4": 2,
            "5": 1,
            "6": 1,
            "7": 1,
            "20": 4,
            "21": 2,
            "23": 2,
            "25": 1,
        }
        payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"][
            "eligibleSlots"
        ] = [3, 5, 7, 23, 20, 21]

        snapshot = espn_host_league_snapshot(
            payload,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )

        self.assertEqual(snapshot.roster_rules.roster_cap, 15)
        self.assertEqual(
            snapshot.roster_rules.starting_lineup_slots,
            (
                "QB",
                "RB",
                "RB",
                "RB_WR",
                "WR",
                "WR",
                "WR_TE",
                "TE",
                "OP",
                "FLEX",
                "FLEX",
            ),
        )
        self.assertEqual(
            dict(snapshot.roster_rules.reserve_slot_counts),
            {"IR": 2, "ROOKIE_RESERVE": 1},
        )
        first_player = next(
            row for row in snapshot.players if row.source_player_id == "101"
        )
        self.assertIn("RB_WR", first_player.eligible_slots)
        self.assertIn("WR_TE", first_player.eligible_slots)
        self.assertIn("FLEX", first_player.eligible_slots)
        self.assertIn("OP", first_player.eligible_slots)

    def test_unconfigured_eligible_slots_are_ignored_without_losing_base_position(self):
        payload = league_payload()
        payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"][
            "eligibleSlots"
        ] = [0, 7, 20, 21]

        snapshot = espn_host_league_snapshot(
            payload,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )
        first_player = next(
            row for row in snapshot.players if row.source_player_id == "101"
        )

        self.assertEqual(first_player.eligible_slots, ("QB",))
        identities = reconcile_player_identities(
            host_player_records(snapshot),
            anchor_provider="espn",
        )
        normalized = normalize_host_league_snapshot(snapshot, identities)
        normalized_player = next(
            row
            for row in normalized.eligibilities
            if row.canonical_player_id == "espn:101"
        )
        self.assertEqual(normalized_player.eligible_slots, ("QB",))

    def test_rejects_unmodeled_regular_season_scoring_rules(self):
        for field, value, message in (
            ("allowOutOfPositionScoring", True, "out-of-position"),
            ("matchupTieRule", "MOST_BENCH_POINTS", "matchup tie rules"),
            ("matchupTieRuleBy", 1, "matchup tie rules"),
        ):
            with self.subTest(field=field):
                payload = league_payload()
                payload["settings"]["scoringSettings"][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    espn_host_league_snapshot(
                        payload,
                        pro_team_payload(),
                        captured_at=NOW,
                        expected_team_count=2,
                    )

    def test_maps_defensive_player_positions_and_rejects_coaches(self):
        for position_id, expected, eligible_slots in (
            (9, "DL", [8, 11, 15, 20]),
            (10, "DL", [9, 11, 15, 20]),
            (11, "LB", [10, 15, 20]),
            (12, "DB", [12, 14, 15, 20]),
            (13, "DB", [13, 14, 15, 20]),
        ):
            with self.subTest(position_id=position_id):
                payload = league_payload()
                payload["settings"]["rosterSettings"]["lineupSlotCounts"]["20"] = 1
                entry = payload["teams"][0]["roster"]["entries"][0]
                entry["lineupSlotId"] = 20
                entry["playerPoolEntry"]["player"].update(
                    defaultPositionId=position_id,
                    eligibleSlots=eligible_slots,
                )

                snapshot = espn_host_league_snapshot(
                    payload,
                    pro_team_payload(),
                    captured_at=NOW,
                    expected_team_count=2,
                )
                imported = next(
                    row for row in snapshot.players if row.source_player_id == "101"
                )

                self.assertEqual(imported.position, expected)
                self.assertIn(expected, imported.eligible_slots)

        coach = league_payload()
        coach["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]["player"][
            "defaultPositionId"
        ] = 14
        with self.assertRaisesRegex(ValueError, "default position 14"):
            espn_host_league_snapshot(
                coach,
                pro_team_payload(),
                captured_at=NOW,
                expected_team_count=2,
            )

    def test_rejects_fine_grained_idp_lineup_slots_until_modeled_exactly(self):
        for slot_id in (8, 9, 12, 13):
            with self.subTest(slot_id=slot_id):
                payload = league_payload()
                payload["settings"]["rosterSettings"]["lineupSlotCounts"][
                    str(slot_id)
                ] = 1
                with self.assertRaisesRegex(ValueError, "fine-grained IDP"):
                    espn_host_league_snapshot(
                        payload,
                        pro_team_payload(),
                        captured_at=NOW,
                        expected_team_count=2,
                    )

    def test_ir_and_rookie_reserve_placements_remain_typed_through_ingest(self):
        payload = league_payload()
        payload["settings"]["rosterSettings"]["lineupSlotCounts"]["25"] = 1
        ir_player = player(103, "Injured Player", 1)
        reserve_player = player(104, "Reserve Player", 1)
        reserve_player["eligibleSlots"].append(25)
        payload["teams"][0]["roster"]["entries"].extend(
            (
                {
                    "playerId": 103,
                    "lineupSlotId": 21,
                    "playerPoolEntry": {"player": ir_player},
                },
                {
                    "playerId": 104,
                    "lineupSlotId": 25,
                    "playerPoolEntry": {"player": reserve_player},
                },
            )
        )
        snapshot = espn_host_league_snapshot(
            payload,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )
        source = next(
            row for row in snapshot.rosters if row.source_team_id == "1"
        )

        self.assertEqual(snapshot.roster_rules.roster_cap, 1)
        self.assertEqual(
            dict(snapshot.roster_rules.reserve_slot_counts),
            {"IR": 1, "ROOKIE_RESERVE": 1},
        )
        self.assertEqual(len(source.source_player_ids), 3)
        self.assertEqual(
            dict(source.reserve_slot_by_player),
            {"103": "IR", "104": "ROOKIE_RESERVE"},
        )

        identities = reconcile_player_identities(
            host_player_records(snapshot),
            anchor_provider="espn",
        )
        normalized = normalize_host_league_snapshot(snapshot, identities)
        primary = next(row for row in normalized.rosters if row.team_id == "espn:team:1")
        counterparty = next(
            row for row in normalized.rosters if row.team_id == "espn:team:2"
        )
        space = TradeSpace(
            primary,
            counterparty,
            TradeConstraints(require_no_drops=True),
        )

        self.assertEqual(primary.current_size, 3)
        self.assertEqual(primary.active_size, 1)
        self.assertEqual(
            dict(primary.reserve_slot_by_player),
            {
                "espn:103": "IR",
                "espn:104": "ROOKIE_RESERVE",
            },
        )
        self.assertEqual(
            dict(primary.reserve_slot_counts),
            {"IR": 1, "ROOKIE_RESERVE": 1},
        )
        self.assertEqual(space.candidate_count, 1)
        candidate = next(iter(space))
        self.assertTrue(
            set(candidate.outgoing_player_ids).isdisjoint(
                primary.reserve_slot_by_player
            )
        )

    def test_snapshot_identity_changes_with_reserve_capacity_or_current_placement(self):
        payload = league_payload()
        payload["settings"]["rosterSettings"]["lineupSlotCounts"]["20"] = 1
        injured = player(103, "Injured Player", 1)
        payload["teams"][0]["roster"]["entries"].append(
            {
                "playerId": 103,
                "lineupSlotId": 21,
                "playerPoolEntry": {"player": injured},
            }
        )
        baseline = espn_host_league_snapshot(
            payload,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )

        changed_capacity = deepcopy(payload)
        changed_capacity["settings"]["rosterSettings"]["lineupSlotCounts"]["21"] = 2
        capacity_snapshot = espn_host_league_snapshot(
            changed_capacity,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )
        changed_placement = deepcopy(payload)
        changed_placement["teams"][0]["roster"]["entries"][1][
            "lineupSlotId"
        ] = 20
        placement_snapshot = espn_host_league_snapshot(
            changed_placement,
            pro_team_payload(),
            captured_at=NOW,
            expected_team_count=2,
        )

        self.assertNotEqual(capacity_snapshot.snapshot_id, baseline.snapshot_id)
        self.assertNotEqual(placement_snapshot.snapshot_id, baseline.snapshot_id)

    def test_is_content_addressed_and_rejects_unsupported_rules_or_slots(self):
        left = espn_host_league_snapshot(
            league_payload(), pro_team_payload(), captured_at=NOW, expected_team_count=2
        )
        right = espn_host_league_snapshot(
            league_payload(), pro_team_payload(), captured_at=NOW, expected_team_count=2
        )
        self.assertEqual(left.snapshot_id, right.snapshot_id)

        bad_rule = deepcopy(league_payload())
        bad_rule["settings"]["scheduleSettings"]["playoffSeedingRule"] = "HEAD_TO_HEAD"
        with self.assertRaisesRegex(ValueError, "seeding rule"):
            espn_host_league_snapshot(
                bad_rule, pro_team_payload(), captured_at=NOW, expected_team_count=2
            )
        bad_slot = deepcopy(league_payload())
        bad_slot["settings"]["rosterSettings"]["lineupSlotCounts"]["99"] = 1
        with self.assertRaisesRegex(ValueError, "slot 99"):
            espn_host_league_snapshot(
                bad_slot, pro_team_payload(), captured_at=NOW, expected_team_count=2
            )

        missing_entry_slot = deepcopy(league_payload())
        missing_entry_slot["teams"][0]["roster"]["entries"][0].pop("lineupSlotId")
        with self.assertRaisesRegex(ValueError, "lineupSlotId"):
            espn_host_league_snapshot(
                missing_entry_slot,
                pro_team_payload(),
                captured_at=NOW,
                expected_team_count=2,
            )

        invalid_bonus = deepcopy(league_payload())
        invalid_bonus["settings"]["scoringSettings"]["homeTeamBonus"] = float("inf")
        with self.assertRaisesRegex(ValueError, "homeTeamBonus must be a finite number"):
            espn_host_league_snapshot(
                invalid_bonus,
                pro_team_payload(),
                captured_at=NOW,
                expected_team_count=2,
            )


if __name__ == "__main__":
    unittest.main()
