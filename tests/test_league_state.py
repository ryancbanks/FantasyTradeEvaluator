from dataclasses import FrozenInstanceError, replace
import copy
import unittest

from trade_snapshot.league_io import league_state_from_record, league_state_to_record
from trade_snapshot.league_state import (
    CompletedFantasyMatchup,
    FantasyMatchup,
    HeadToHeadPolicy,
    LeagueState,
    LeagueTeam,
    PlayoffRules,
    RosterRules,
    TeamStanding,
    Tiebreaker,
)


class LeagueStateTests(unittest.TestCase):
    def test_accepts_complete_immutable_league_state(self):
        state = make_state()

        self.assertEqual(state.remaining_regular_season_weeks, (3, 4))
        self.assertEqual(state.roster_rules.roster_cap, 14)
        self.assertEqual(
            state.roster_rules.starting_lineup_slots,
            ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DST"),
        )
        with self.assertRaises(FrozenInstanceError):
            state.first_remaining_week = 4

    def test_rejects_duplicate_team_ids(self):
        state = make_state()
        duplicate = replace(state.teams[1], team_id="a")

        with self.assertRaisesRegex(ValueError, "team_id values must be unique"):
            replace(state, teams=(state.teams[0], duplicate, *state.teams[2:]))

        for invalid_id in (None, 1, True, ""):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(ValueError, "non-empty string"):
                    replace(state.teams[0], team_id=invalid_id)

    def test_standings_must_exactly_cover_teams(self):
        state = make_state()

        with self.subTest("missing"):
            with self.assertRaisesRegex(ValueError, "exactly one row for every league team"):
                replace(state, standings=state.standings[:-1])

        with self.subTest("duplicate"):
            with self.assertRaisesRegex(ValueError, "standing team_id values must be unique"):
                replace(state, standings=(*state.standings[:-1], state.standings[0]))

        with self.subTest("unknown"):
            unknown = replace(state.standings[-1], team_id="unknown")
            with self.assertRaisesRegex(ValueError, "exactly one row for every league team"):
                replace(state, standings=(*state.standings[:-1], unknown))

    def test_rejects_self_unknown_and_duplicate_matchups(self):
        state = make_state()

        invalid_matchups = {
            "self": FantasyMatchup(3, "a", "a"),
            "unknown team": FantasyMatchup(3, "a", "unknown"),
        }
        for label, matchup in invalid_matchups.items():
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, label):
                    replace(state, remaining_matchups=(matchup, *state.remaining_matchups[1:]))

        with self.subTest("duplicate pair in either order"):
            reversed_duplicate = FantasyMatchup(3, "b", "a")
            with self.assertRaisesRegex(ValueError, "duplicate matchup for week 3"):
                replace(state, remaining_matchups=(
                    state.remaining_matchups[0],
                    reversed_duplicate,
                    *state.remaining_matchups[2:],
                ))

    def test_every_team_plays_exactly_once_in_each_remaining_week(self):
        state = make_state()

        with self.subTest("missing team"):
            with self.assertRaisesRegex(ValueError, "exactly one matchup in week 4"):
                replace(state, remaining_matchups=state.remaining_matchups[:-1])

        with self.subTest("team appears twice"):
            duplicate_team = FantasyMatchup(4, "a", "d")
            with self.assertRaisesRegex(ValueError, "exactly one matchup in week 4"):
                replace(state, remaining_matchups=(*state.remaining_matchups[:-1], duplicate_team))

    def test_rejects_matchups_outside_remaining_regular_season(self):
        state = make_state()
        playoff_week_matchup = replace(state.remaining_matchups[0], week=5)

        with self.assertRaisesRegex(ValueError, "outside the remaining regular season"):
            replace(
                state,
                remaining_matchups=(playoff_week_matchup, *state.remaining_matchups[1:]),
            )

    def test_matchup_adjustment_is_finite_immutable_and_strictly_serialized(self):
        state = make_state()
        adjusted = replace(state.remaining_matchups[0], team1_score_adjustment=1.25)
        state = replace(
            state,
            remaining_matchups=(adjusted, *state.remaining_matchups[1:]),
        )
        record = league_state_to_record(state)

        self.assertEqual(record["schema_version"], 3)
        self.assertEqual(
            record["remaining_matchups"][0]["team1_score_adjustment"],
            1.25,
        )
        self.assertEqual(league_state_from_record(record), state)
        with self.assertRaises(FrozenInstanceError):
            adjusted.team1_score_adjustment = 0

        for invalid in (True, "1", float("nan"), float("inf"), -float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "finite number"):
                    FantasyMatchup(3, "a", "b", invalid)

        legacy = copy.deepcopy(record)
        legacy["schema_version"] = 1
        for row in legacy["remaining_matchups"]:
            row.pop("team1_score_adjustment")
        with self.assertRaisesRegex(ValueError, "schema version"):
            league_state_from_record(legacy)

        prior_roster_schema = copy.deepcopy(record)
        prior_roster_schema["schema_version"] = 2
        prior_roster_schema["roster_rules"].pop("reserve_slot_counts")
        with self.assertRaisesRegex(ValueError, "schema version"):
            league_state_from_record(prior_roster_schema)

        missing = copy.deepcopy(record)
        missing["remaining_matchups"][0].pop("team1_score_adjustment")
        with self.assertRaisesRegex(ValueError, "row fields"):
            league_state_from_record(missing)

    def test_validates_playoff_qualifiers_and_division_berths_against_league(self):
        state = make_state()

        with self.subTest("too many qualifiers"):
            rules = replace(state.playoff_rules, qualifier_count=5)
            with self.assertRaisesRegex(ValueError, "qualifier_count cannot exceed"):
                replace(state, playoff_rules=rules)

        with self.subTest("too many division berths"):
            rules = replace(
                state.playoff_rules,
                qualifier_count=4,
                division_winner_qualifier_count=3,
            )
            with self.assertRaisesRegex(ValueError, "division winner berths"):
                replace(state, playoff_rules=rules)

        with self.subTest("division assignments required"):
            teams = tuple(replace(team, division_id=None) for team in state.teams)
            with self.assertRaisesRegex(ValueError, "division_id"):
                replace(state, teams=teams)

    def test_validates_roster_and_playoff_settings(self):
        rules = RosterRules(2, ("QB",), {"IR": 1})
        self.assertIsInstance(hash(rules), int)

        with self.subTest("lineup larger than roster"):
            with self.assertRaisesRegex(ValueError, "cannot exceed roster_cap"):
                RosterRules(roster_cap=1, starting_lineup_slots=("QB", "RB"))

        with self.subTest("duplicate tiebreaker"):
            with self.assertRaisesRegex(ValueError, "tiebreaker_order cannot contain duplicates"):
                PlayoffRules(
                    qualifier_count=2,
                    regular_season_end_week=14,
                    playoff_weeks=(15, 16, 17),
                    reseed_each_round=False,
                    division_winner_qualifier_count=0,
                    tiebreaker_order=(Tiebreaker.POINTS_FOR, Tiebreaker.POINTS_FOR),
                )

        with self.subTest("head-to-head policy type"):
            state = make_state()
            with self.assertRaisesRegex(ValueError, "head_to_head_policy"):
                replace(state.playoff_rules, head_to_head_policy="unknown")

    def test_completed_history_exposes_completeness_and_standings_consistency(self):
        state = make_history_state()

        self.assertTrue(state.completed_history_is_complete)
        self.assertTrue(state.completed_history_matches_standings)
        self.assertTrue(state.completed_history_is_usable)
        copied = replace(state, completed_matchups=list(state.completed_matchups))
        self.assertIsInstance(copied.completed_matchups, tuple)
        self.assertIs(
            state.playoff_rules.head_to_head_policy,
            HeadToHeadPolicy.BALANCED_GROUP_WIN_PERCENTAGE,
        )
        with self.assertRaises(FrozenInstanceError):
            state.completed_matchups[0].team1_score = 0

        partial = replace(
            state,
            completed_matchups=state.completed_matchups[:2],
            standings=(
                TeamStanding("a", 1, 0, 0, 100, 90),
                TeamStanding("b", 0, 1, 0, 90, 100),
                TeamStanding("c", 1, 0, 0, 80, 70),
                TeamStanding("d", 0, 1, 0, 70, 80),
            ),
        )
        self.assertFalse(partial.completed_history_is_complete)
        self.assertTrue(partial.completed_history_matches_standings)
        self.assertFalse(partial.completed_history_is_usable)

        inconsistent = replace(
            state,
            standings=(replace(state.standings[0], points_for=999), *state.standings[1:]),
        )
        self.assertTrue(inconsistent.completed_history_is_complete)
        self.assertFalse(inconsistent.completed_history_matches_standings)
        self.assertFalse(inconsistent.completed_history_is_usable)

        extreme = replace(
            state,
            completed_matchups=tuple(
                replace(row, team1_score=1e308, team2_score=1e308)
                for row in state.completed_matchups
            ),
            standings=tuple(
                TeamStanding(team.team_id, 0, 0, 2, 1e308, 1e308)
                for team in state.teams
            ),
        )
        self.assertTrue(extreme.completed_history_is_complete)
        self.assertFalse(extreme.completed_history_matches_standings)

    def test_validates_completed_matchup_rows_and_schedule_bounds(self):
        for field, value in (
            ("team1_id", " "),
            ("team2_id", 3),
            ("team1_score", float("inf")),
            ("team2_score", True),
        ):
            with self.subTest(field=field):
                values = {
                    "week": 1,
                    "team1_id": "a",
                    "team2_id": "b",
                    "team1_score": 1,
                    "team2_score": 2,
                }
                values[field] = value
                with self.assertRaises(ValueError):
                    CompletedFantasyMatchup(**values)

        state = make_history_state()
        with self.assertRaisesRegex(ValueError, "CompletedFantasyMatchup values"):
            replace(state, completed_matchups=("not-a-matchup",))
        invalid_rows = (
            ("before first_remaining_week", replace(state.completed_matchups[0], week=3)),
            ("unknown team", replace(state.completed_matchups[0], team1_id="unknown")),
            ("self completed matchup", replace(state.completed_matchups[0], team2_id="a")),
        )
        for message, row in invalid_rows:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    replace(state, completed_matchups=(row, *state.completed_matchups[1:]))

        with self.subTest("duplicate pair"):
            duplicate = replace(state.completed_matchups[0], team1_id="b", team2_id="a")
            with self.assertRaisesRegex(ValueError, "duplicate completed matchup"):
                replace(state, completed_matchups=(state.completed_matchups[0], duplicate))

        with self.subTest("team double-booked"):
            double_booked = CompletedFantasyMatchup(1, "a", "c", 1, 2)
            with self.assertRaisesRegex(ValueError, "appears twice"):
                replace(
                    state,
                    completed_matchups=(*state.completed_matchups, double_booked),
                )


def make_state() -> LeagueState:
    teams = (
        LeagueTeam("a", "Alpha", "east"),
        LeagueTeam("b", "Bravo", "east"),
        LeagueTeam("c", "Charlie", "west"),
        LeagueTeam("d", "Delta", "west"),
    )
    standings = tuple(
        TeamStanding(
            team_id=team.team_id,
            wins=index + 1,
            losses=2,
            ties=0,
            points_for=300.0 + index,
            points_against=290.0 + index,
        )
        for index, team in enumerate(teams)
    )
    matchups = (
        FantasyMatchup(3, "a", "b"),
        FantasyMatchup(3, "c", "d"),
        FantasyMatchup(4, "a", "c"),
        FantasyMatchup(4, "b", "d"),
    )
    return LeagueState(
        snapshot_id="snapshot-1",
        season=2026,
        scoring_profile_id="ppr-default-v1",
        first_remaining_week=3,
        teams=teams,
        standings=standings,
        remaining_matchups=matchups,
        roster_rules=RosterRules(
            roster_cap=14,
            starting_lineup_slots=(
                "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DST",
            ),
        ),
        playoff_rules=PlayoffRules(
            qualifier_count=2,
            regular_season_end_week=4,
            playoff_weeks=(5, 6),
            reseed_each_round=False,
            division_winner_qualifier_count=2,
            tiebreaker_order=(
                Tiebreaker.WIN_PERCENTAGE,
                Tiebreaker.POINTS_FOR,
                Tiebreaker.HEAD_TO_HEAD,
            ),
        ),
    )


def make_history_state() -> LeagueState:
    state = make_state()
    completed = (
        CompletedFantasyMatchup(1, "a", "b", 100, 90),
        CompletedFantasyMatchup(1, "c", "d", 80, 70),
        CompletedFantasyMatchup(2, "a", "c", 110, 105),
        CompletedFantasyMatchup(2, "d", "b", 95, 85),
    )
    standings = (
        TeamStanding("a", 2, 0, 0, 210, 195),
        TeamStanding("b", 0, 2, 0, 175, 195),
        TeamStanding("c", 1, 1, 0, 185, 180),
        TeamStanding("d", 1, 1, 0, 165, 165),
    )
    return replace(
        state,
        standings=standings,
        completed_matchups=completed,
        playoff_rules=replace(
            state.playoff_rules,
            head_to_head_policy=HeadToHeadPolicy.BALANCED_GROUP_WIN_PERCENTAGE,
        ),
    )


if __name__ == "__main__":
    unittest.main()
