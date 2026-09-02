from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import unittest

from league_ingest_fixtures import complete_snapshot
from trade_snapshot.league_source import (
    ProviderPlayerId,
    ProviderTeamId,
    SourceLeaguePlayer,
    SourceLeagueTeam,
    SourceMatchup,
)
from trade_snapshot.league_state import RosterRules


class VerifiedHostLeagueSnapshotTests(unittest.TestCase):
    def test_accepts_complete_18_team_source_and_preserves_exact_scoring(self):
        snapshot = complete_snapshot()

        self.assertEqual(snapshot.expected_team_count, 18)
        self.assertEqual(len(snapshot.teams), 18)
        self.assertEqual(len(snapshot.rosters), 18)
        self.assertEqual(len(snapshot.standings), 18)
        self.assertEqual(len(snapshot.remaining_matchups), 18)
        self.assertEqual(snapshot.scoring_profile.platform, "espn")
        self.assertEqual(snapshot.scoring_profile.settings["receiving"]["reception"], 1)
        self.assertIsInstance(snapshot.rosters, tuple)
        with self.assertRaises(FrozenInstanceError):
            snapshot.expected_team_count = 17
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            replace(snapshot, captured_at=datetime(2026, 9, 1, 12))

    def test_configurable_expected_team_count_is_enforced_exactly(self):
        snapshot = complete_snapshot()
        with self.assertRaisesRegex(ValueError, "team coverage"):
            replace(snapshot, expected_team_count=17)

        four_team = complete_snapshot(4)
        self.assertEqual(four_team.expected_team_count, 4)

    def test_rosters_and_standings_must_each_exactly_cover_teams(self):
        snapshot = complete_snapshot()
        for field, rows, message in (
            ("rosters", snapshot.rosters[:-1], "rosters must contain exactly"),
            ("standings", snapshot.standings[:-1], "standings must contain exactly"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                replace(snapshot, **{field: rows})

        duplicate = (*snapshot.rosters[:-1], snapshot.rosters[0])
        with self.assertRaisesRegex(ValueError, "roster team IDs must be unique"):
            replace(snapshot, rosters=duplicate)

    def test_remaining_schedule_must_cover_every_team_in_every_week(self):
        snapshot = complete_snapshot()
        with self.assertRaisesRegex(ValueError, "exactly one matchup in week 3"):
            replace(snapshot, remaining_matchups=snapshot.remaining_matchups[:-1])

        unknown = replace(snapshot.remaining_matchups[0], source_team1_id="unknown")
        with self.assertRaisesRegex(ValueError, "unknown team"):
            replace(snapshot, remaining_matchups=(unknown, *snapshot.remaining_matchups[1:]))

        adjusted = replace(
            snapshot.remaining_matchups[0],
            team1_score_adjustment=1,
        )
        self.assertEqual(adjusted.team1_score_adjustment, 1)
        for invalid in (True, "1", float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    SourceMatchup(2, "1", "2", invalid)

    def test_rosters_reject_unknown_players_duplicate_ownership_and_over_cap(self):
        snapshot = complete_snapshot()
        unknown = replace(snapshot.rosters[0], source_player_ids=("unknown",))
        with self.assertRaisesRegex(ValueError, "without metadata"):
            replace(snapshot, rosters=(unknown, *snapshot.rosters[1:]))

        shared = replace(snapshot.rosters[1], source_player_ids=("p1",))
        with self.assertRaisesRegex(ValueError, "more than one team"):
            replace(snapshot, rosters=(snapshot.rosters[0], shared, *snapshot.rosters[2:]))

        too_many = replace(
            snapshot.rosters[0],
            source_player_ids=tuple(row.source_player_id for row in snapshot.players[:15]),
        )
        with self.assertRaisesRegex(ValueError, "exceeds.*roster cap"):
            replace(snapshot, rosters=(too_many, *snapshot.rosters[1:]))

    def test_full_standings_require_elapsed_games_and_balanced_pf_pa(self):
        snapshot = complete_snapshot()
        wrong_record = replace(snapshot.standings[0], wins=0)
        with self.assertRaisesRegex(ValueError, "one result per elapsed week"):
            replace(snapshot, standings=(wrong_record, *snapshot.standings[1:]))

        wrong_points = replace(snapshot.standings[0], points_for=999)
        with self.assertRaisesRegex(ValueError, "points-for and points-against"):
            replace(snapshot, standings=(wrong_points, *snapshot.standings[1:]))

    def test_completed_history_is_explicitly_unavailable_or_exact(self):
        unavailable = complete_snapshot(completed_history=False)
        self.assertIsNone(unavailable.completed_matchups)

        snapshot = complete_snapshot()
        with self.assertRaisesRegex(ValueError, "cover every elapsed team-week"):
            replace(snapshot, completed_matchups=snapshot.completed_matchups[:-1])
        changed = replace(snapshot.completed_matchups[0], team1_score=777)
        with self.assertRaisesRegex(ValueError, "do not reproduce the standings"):
            replace(snapshot, completed_matchups=(changed, *snapshot.completed_matchups[1:]))

    def test_exact_source_provider_ids_are_required_and_globally_unique(self):
        snapshot = complete_snapshot()
        missing_source = replace(
            snapshot.teams[0],
            provider_ids=(ProviderTeamId("espn", "espn-1"),),
        )
        with self.assertRaisesRegex(ValueError, "exact source-provider ID"):
            replace(snapshot, teams=(missing_source, *snapshot.teams[1:]))

        collided = replace(
            snapshot.players[1],
            provider_ids=(
                ProviderPlayerId("fantasypros", "p2"),
                ProviderPlayerId("espn", "e1"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "identifies multiple rows"):
            replace(snapshot, players=(snapshot.players[0], collided, *snapshot.players[2:]))

    def test_source_entities_normalize_positions_and_reject_ambiguous_provider_ids(self):
        player = SourceLeaguePlayer(
            "1", "Defender", "DE", "DAL", ("DE",),
            (ProviderPlayerId("fantasypros", "1"),),
        )
        self.assertEqual((player.position, player.eligible_slots), ("DL", ("DL",)))
        with self.assertRaisesRegex(ValueError, "one ID per provider"):
            SourceLeagueTeam(
                "1", "Team", (
                    ProviderTeamId("espn", "a"), ProviderTeamId("espn", "b"),
                )
            )

        with self.assertRaisesRegex(ValueError, "unsupported lineup slot"):
            SourceLeaguePlayer(
                "2", "Invalid", "RB", "DAL", ("BENCH",),
                (ProviderPlayerId("fantasypros", "2"),),
            )

    def test_starting_lineup_rules_must_already_be_provider_neutral(self):
        snapshot = complete_snapshot()
        with self.assertRaisesRegex(ValueError, "canonical supported"):
            replace(snapshot, roster_rules=RosterRules(14, ("DE",)))

    def test_scoring_profile_object_is_mandatory_not_a_label(self):
        snapshot = complete_snapshot()
        with self.assertRaisesRegex(ValueError, "exact captured scoring settings"):
            replace(snapshot, scoring_profile="PPR")


if __name__ == "__main__":
    unittest.main()
