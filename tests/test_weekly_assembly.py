from datetime import datetime, timezone
from dataclasses import replace
import unittest

from trade_snapshot.analyzer_contract import BundleFingerprint
from trade_snapshot.capture_schema import (
    CaptureKind,
    CaptureProvider,
    ECRCaptureMethod,
    ECRRankingRow,
    FantasyProsECRArtifact,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    LeagueSource,
    LeagueSourceKind,
    RankingHorizon,
    VisibleTable,
    VisibleTableCell,
)
from trade_snapshot.league_source import (
    ProviderPlayerId,
    ProviderTeamId,
    SourceLeaguePlayer,
    SourceLeagueTeam,
    SourceMatchup,
    SourceTeamRoster,
    SourceTeamStanding,
    VerifiedHostLeagueSnapshot,
)
from trade_snapshot.league_state import PlayoffRules, RosterRules, Tiebreaker
from trade_snapshot.nfl_schedule import (
    NflSchedule,
    NflTeamWeek,
    NflTeamWeekStatus,
    canonical_nfl_game_id,
)
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.weekly_assembly import assemble_weekly_refresh_evidence
from trade_snapshot.weekly_engine import prepare_weekly_model_inputs


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def nfl_schedule():
    rows = []
    for team, opponent in (("ARI", "BUF"), ("ATL", "CAR")):
        for week in (1, 2):
            game_id = canonical_nfl_game_id(2026, week, team, opponent)
            rows.extend(
                (
                    NflTeamWeek(
                        team,
                        week,
                        NflTeamWeekStatus.SCHEDULED,
                        game_id,
                        opponent,
                        True,
                    ),
                    NflTeamWeek(
                        opponent,
                        week,
                        NflTeamWeekStatus.SCHEDULED,
                        game_id,
                        team,
                        False,
                    ),
                )
            )
    return NflSchedule(2026, NOW, "espn", tuple(rows))


def host_snapshot():
    return VerifiedHostLeagueSnapshot(
        snapshot_id="week-1",
        captured_at=NOW,
        source_provider="espn",
        source_league_id="league-77",
        season=2026,
        scoring_profile=ScoringProfile("espn", {"receiving": {"reception": 1}}),
        first_remaining_week=1,
        expected_team_count=2,
        teams=(
            SourceLeagueTeam("e1", "Alpha", (ProviderTeamId("espn", "e1"),)),
            SourceLeagueTeam("e2", "Bravo", (ProviderTeamId("espn", "e2"),)),
        ),
        players=(
            SourceLeaguePlayer(
                "201", "Player One", "RB", "ARI", ("RB", "FLEX"),
                (ProviderPlayerId("espn", "201"), ProviderPlayerId("yahoo", "301"),
                 ProviderPlayerId("fantasypros", "101")),
            ),
            SourceLeaguePlayer(
                "202", "Player Two", "RB", "ATL", ("RB", "FLEX"),
                (ProviderPlayerId("espn", "202"), ProviderPlayerId("yahoo", "302"),
                 ProviderPlayerId("fantasypros", "102")),
            ),
        ),
        rosters=(SourceTeamRoster("e1", ("201",)), SourceTeamRoster("e2", ("202",))),
        standings=(
            SourceTeamStanding("e1", 0, 0, 0, 0, 0),
            SourceTeamStanding("e2", 0, 0, 0, 0, 0),
        ),
        remaining_matchups=(SourceMatchup(1, "e1", "e2"), SourceMatchup(2, "e1", "e2")),
        roster_rules=RosterRules(1, ("RB",)),
        playoff_rules=PlayoffRules(
            1, 2, (3,), False, 0,
            (Tiebreaker.WIN_PERCENTAGE, Tiebreaker.RANDOM_DRAW),
        ),
    )


def league_artifact():
    bootstrap = {
        "current_week": 1,
        "league": {
            "id": "77", "name": "League", "season": 2026, "team_count": 2,
            "playoff_teams": 1, "roster_size": 1, "scoring": "PPR",
        },
        "players": [
            {"player_id": "101", "name": "Player One", "position_id": "RB"},
            {"player_id": "102", "name": "Player Two", "position_id": "RB"},
        ],
        "teams": [
            {"team_id": "1", "team_name": "Alpha"},
            {"team_id": "2", "team_name": "Bravo"},
        ],
        "rosters": [
            {"team_id": "1", "player_ids": ["101"]},
            {"team_id": "2", "player_ids": ["102"]},
        ],
    }
    current = {
        "best_free_agent_ids": ["103"],
        "standings": [
            {"teamId": "1", "wins": 0, "losses": 0, "ties": 0},
            {"teamId": "2", "wins": 0, "losses": 0, "ties": 0},
        ],
    }
    projected = {"playoffsTeam": 1, "standings": [
        {"teamId": "1", "teamName": "Alpha", "rank_proj": 1, "rank_current": 1,
         "wins_current": 0, "losses_current": 0, "wins_proj": 1,
         "losses_proj": 1, "playoffs_odds": 60, "championship_odds": 30},
        {"teamId": "2", "teamName": "Bravo", "rank_proj": 2, "rank_current": 2,
         "wins_current": 0, "losses_current": 0, "wins_proj": 1,
         "losses_proj": 1, "playoffs_odds": 40, "championship_odds": 20},
    ]}
    sources = (
        LeagueSource(LeagueSourceKind.BOOTSTRAP, {"payload": bootstrap}),
        LeagueSource(LeagueSourceKind.ANALYZER_INIT, {"payload": current}),
        LeagueSource(LeagueSourceKind.PROJECTED_STANDINGS, {"payload": projected}),
    )
    return FantasyProsLeagueArtifact(
        "captask_" + "1" * 64,
        "fantasypros", 2026, 1, "league_source",
        "2026-09-01T00:00:00Z", 2, True,
        "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
        "trade-analyzer/bundle-1234567890abcdef.js",
        "a" * 64,
        sources,
    )


def ecr_artifact(horizon):
    return FantasyProsECRArtifact(
        task_id="captask_" + ("2" if horizon is RankingHorizon.WEEKLY else "3") * 64,
        season=2026, week=1, horizon=horizon, scoring="PPR",
        position_scope=("RB",), expert_ids=("expert-1",), expert_count=1,
        capture_method=ECRCaptureMethod.VISIBLE_PAGE,
        last_updated_text="today", last_updated_at="2026-09-01T00:00:00Z",
        captured_at="2026-09-01T01:00:00Z",
        rankings=(
            ECRRankingRow("101", "Player One", "ARI", "RB", 1, 1, 2, 1.2, .2, "RB1", {"ECR": "1"}),
            ECRRankingRow("102", "Player Two", "ATL", "RB", 2, 1, 3, 2.1, .3, "RB2", {"ECR": "2"}),
            ECRRankingRow("103", "Player Three", "CAR", "RB", 3, 2, 4, 3.1, .3, "RB3", {"ECR": "3"}),
        ),
    )


def projection_artifact(provider, horizon, week):
    links = {
        CaptureProvider.FANTASYPROS: (
            "https://www.fantasypros.com/nfl/players/player-one.php",
            "https://www.fantasypros.com/nfl/players/player-two.php",
            "https://www.fantasypros.com/nfl/players/player-three.php",
        ),
        CaptureProvider.ESPN: (
            "https://www.espn.com/nfl/player/_/id/201/player-one",
            "https://www.espn.com/nfl/player/_/id/202/player-two",
            "https://www.espn.com/nfl/player/_/id/203/player-three",
        ),
        CaptureProvider.YAHOO: (
            "https://sports.yahoo.com/nfl/players/301/",
            "https://sports.yahoo.com/nfl/players/302/",
            "https://sports.yahoo.com/nfl/players/303/",
        ),
        CaptureProvider.CBS: (
            "https://www.cbssports.com/nfl/players/401/player-one/fantasy/",
            "https://www.cbssports.com/nfl/players/402/player-two/fantasy/",
            "https://www.cbssports.com/nfl/players/403/player-three/fantasy/",
        ),
        CaptureProvider.FFTODAY: (
            "https://www.fftoday.com/stats/players/501/Player_One",
            "https://www.fftoday.com/stats/players/502/Player_Two",
            "https://www.fftoday.com/stats/players/503/Player_Three",
        ),
        CaptureProvider.FANTASYSHARKS: (
            "https://www.fantasysharks.com/apps/bert/players/playerpage.php?id=601",
            "https://www.fantasysharks.com/apps/bert/players/playerpage.php?id=602",
            "https://www.fantasysharks.com/apps/bert/players/playerpage.php?id=603",
        ),
    }[provider]
    table = VisibleTable((
        tuple(
            VisibleTableCell(value)
            for value in ("PLAYER", "TEAM", "POS", "FPTS", "FPPG", "GP")
        ),
        (VisibleTableCell("Player One", (links[0],)), VisibleTableCell("ARI"),
         VisibleTableCell("RB"), VisibleTableCell("10"), VisibleTableCell("10"),
         VisibleTableCell("17")),
        (VisibleTableCell("Player Two", (links[1],)), VisibleTableCell("ATL"),
         VisibleTableCell("RB"), VisibleTableCell("9"), VisibleTableCell("9"),
         VisibleTableCell("17")),
        (VisibleTableCell("Player Three", (links[2],)), VisibleTableCell("CAR"),
         VisibleTableCell("RB"), VisibleTableCell("8"), VisibleTableCell("8"),
         VisibleTableCell("17")),
    ))
    return GenericTableArtifact(
        "captask_" + str(4 + week) * 64,
        provider, 2026, week, CaptureKind.VISIBLE_TABLE,
        "2026-09-01T01:00:00Z", horizon, "PPR", ("RB",),
        "2026 PPR", 1, True, (table,),
    )


def all_projection_artifacts():
    providers = (
        CaptureProvider.FANTASYPROS,
        CaptureProvider.ESPN,
        CaptureProvider.YAHOO,
    )
    return tuple(
        projection_artifact(provider, horizon, week)
        for provider in providers
        for horizon, weeks in (
            (RankingHorizon.WEEKLY, (1, 2)),
            (RankingHorizon.ROS, (1,)),
        )
        for week in weeks
    )


def broad_projection_artifacts():
    provider_periods = (
        (CaptureProvider.FANTASYPROS, (RankingHorizon.WEEKLY, RankingHorizon.ROS)),
        (CaptureProvider.ESPN, (RankingHorizon.WEEKLY, RankingHorizon.ROS)),
        (CaptureProvider.YAHOO, (RankingHorizon.WEEKLY, RankingHorizon.ROS)),
        (CaptureProvider.CBS, (RankingHorizon.ROS,)),
        (CaptureProvider.FFTODAY, (RankingHorizon.WEEKLY, RankingHorizon.ROS)),
        (CaptureProvider.FANTASYSHARKS, (RankingHorizon.WEEKLY, RankingHorizon.ROS)),
    )
    return tuple(
        projection_artifact(provider, horizon, week)
        for provider, horizons in provider_periods
        for horizon in horizons
        for week in ((1, 2) if horizon is RankingHorizon.WEEKLY else (1,))
    )


class WeeklyAssemblyTests(unittest.TestCase):
    def test_broad_consensus_excludes_fantasypros_composite_from_forecast_votes(self):
        result = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=broad_projection_artifacts(),
            ecr_artifacts=(
                ecr_artifact(RankingHorizon.WEEKLY),
                ecr_artifact(RankingHorizon.ROS),
            ),
            nfl_schedule=nfl_schedule(),
            analyzer_bundle=BundleFingerprint(
                "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                "trade-analyzer/bundle-1234567890abcdef.js",
                "a" * 64,
            ),
            response_schema_sha256="b" * 64,
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=True,
        )

        forecast = tuple(
            row.provider for row in result.evidence.ensemble_config.provider_weights
        )
        self.assertEqual(
            forecast, ("espn", "yahoo", "cbs", "fftoday", "fantasysharks")
        )
        self.assertNotIn("fantasypros", forecast)
        self.assertIn(
            "fantasypros",
            {row.provider for row in result.evidence.projection_evidence},
        )

    def test_broad_consensus_accepts_ros_only_fftoday_evidence(self):
        projections = tuple(
            row
            for row in broad_projection_artifacts()
            if not (
                row.provider is CaptureProvider.FFTODAY
                and row.horizon is RankingHorizon.WEEKLY
            )
        )

        result = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=projections,
            ecr_artifacts=(
                ecr_artifact(RankingHorizon.WEEKLY),
                ecr_artifact(RankingHorizon.ROS),
            ),
            nfl_schedule=nfl_schedule(),
            analyzer_bundle=BundleFingerprint(
                "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                "trade-analyzer/bundle-1234567890abcdef.js",
                "a" * 64,
            ),
            response_schema_sha256="b" * 64,
            scoring="PPR",
            expected_team_count=2,
            broad_consensus=True,
        )

        self.assertIn(
            "fftoday",
            tuple(
                row.provider
                for row in result.evidence.ensemble_config.provider_weights
            ),
        )

    def test_assembles_complete_exact_provider_mappings_and_refresh_evidence(self):
        host = host_snapshot()
        projections = all_projection_artifacts()
        result = assemble_weekly_refresh_evidence(
            host_snapshot=host,
            fantasypros_league=league_artifact(),
            projection_artifacts=projections,
            ecr_artifacts=(
                ecr_artifact(RankingHorizon.WEEKLY),
                ecr_artifact(RankingHorizon.ROS),
            ),
            nfl_schedule=nfl_schedule(),
            analyzer_bundle=BundleFingerprint(
                "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                "trade-analyzer/bundle-1234567890abcdef.js",
                "a" * 64,
            ),
            response_schema_sha256="b" * 64,
            scoring="PPR",
            expected_team_count=2,
        )
        self.assertEqual(set(result.fantasypros_team_ids.values()), {"1", "2"})
        self.assertEqual(
            set(result.fantasypros_player_ids.values()), {"101", "102", "103"}
        )
        self.assertEqual(len(result.evidence.ecr_snapshots), 2)
        self.assertEqual(len(result.evidence.projection_evidence), 27)
        self.assertIs(result.evidence.scoring_profile, host.scoring_profile)
        self.assertIsInstance(result.evidence.nfl_schedule, NflSchedule)
        self.assertEqual(
            dict(result.evidence.player_nfl_team_ids),
            {
                "fantasypros:101": "ARI",
                "fantasypros:102": "ATL",
                "fantasypros:103": "CAR",
            },
        )
        self.assertEqual(result.evidence.waiver_pool.player_ids, ("fantasypros:103",))
        prepared = prepare_weekly_model_inputs(
            state=result.evidence.state,
            projection_evidence=result.evidence.projection_evidence,
            ecr_snapshots=result.evidence.ecr_snapshots,
            eligibilities=result.evidence.eligibilities,
            player_positions=result.evidence.player_positions,
            player_nfl_team_ids=result.evidence.player_nfl_team_ids,
            nfl_schedule=result.evidence.nfl_schedule,
            ensemble_config=result.evidence.ensemble_config,
        )
        self.assertEqual(len(prepared.projections), 6)
        self.assertNotIn(
            "fantasypros:103",
            {
                player_id
                for roster in result.evidence.rosters
                for player_id in roster.player_ids
            },
        )
        self.assertEqual(result.evidence.state.first_remaining_week, 1)
        self.assertEqual(
            tuple(row.provider for row in result.evidence.ensemble_config.provider_weights),
            ("fantasypros", "espn", "yahoo"),
        )

    def test_current_week_plus_ros_materializes_future_projection_grid(self):
        projections = tuple(
            projection_artifact(provider, horizon, 1)
            for provider in (
                CaptureProvider.FANTASYPROS,
                CaptureProvider.ESPN,
                CaptureProvider.YAHOO,
            )
            for horizon in (RankingHorizon.WEEKLY, RankingHorizon.ROS)
        )
        result = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=projections,
            ecr_artifacts=(
                ecr_artifact(RankingHorizon.WEEKLY),
                ecr_artifact(RankingHorizon.ROS),
            ),
            nfl_schedule=nfl_schedule(),
            analyzer_bundle=BundleFingerprint(
                "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                "trade-analyzer/bundle-1234567890abcdef.js",
                "a" * 64,
            ),
            response_schema_sha256="b" * 64,
            scoring="PPR",
            expected_team_count=2,
        )
        evidence = result.evidence
        prepared = prepare_weekly_model_inputs(
            state=evidence.state,
            projection_evidence=evidence.projection_evidence,
            ecr_snapshots=evidence.ecr_snapshots,
            eligibilities=evidence.eligibilities,
            player_positions=evidence.player_positions,
            player_nfl_team_ids=evidence.player_nfl_team_ids,
            nfl_schedule=evidence.nfl_schedule,
            ensemble_config=evidence.ensemble_config,
        )

        self.assertEqual(evidence.waiver_pool.player_ids, ("fantasypros:103",))
        self.assertEqual(len(evidence.projection_evidence), 18)
        self.assertEqual(len(prepared.projections), 6)
        self.assertEqual(
            {row.week for row in prepared.projections},
            {1, 2},
        )

    def test_core_ensemble_requires_yahoo_projection_evidence(self):
        projections = tuple(
            row
            for row in all_projection_artifacts()
            if row.provider is not CaptureProvider.YAHOO
        )
        with self.assertRaisesRegex(ValueError, "ESPN and Yahoo"):
            assemble_weekly_refresh_evidence(
                host_snapshot=host_snapshot(),
                fantasypros_league=league_artifact(),
                projection_artifacts=projections,
                ecr_artifacts=(
                    ecr_artifact(RankingHorizon.WEEKLY),
                    ecr_artifact(RankingHorizon.ROS),
                ),
                nfl_schedule=nfl_schedule(),
                analyzer_bundle=BundleFingerprint(
                    "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                    "trade-analyzer/bundle-1234567890abcdef.js",
                    "a" * 64,
                ),
                response_schema_sha256="b" * 64,
                scoring="PPR",
                expected_team_count=2,
            )

    def test_rejects_a_fantasypros_roster_player_without_exact_identity(self):
        broken = league_artifact()
        sources = []
        for source in broken.sources:
            record = source.to_record()
            if source.source is LeagueSourceKind.BOOTSTRAP:
                record["body"]["payload"]["players"][0]["player_id"] = "999"
                record["body"]["payload"]["rosters"][0]["player_ids"] = ["999"]
            sources.append(LeagueSource.from_record(record))
        broken = FantasyProsLeagueArtifact(
            broken.task_id, broken.provider, broken.season, broken.week, broken.kind,
            broken.captured_at, broken.team_count, True,
            broken.bundle_url, broken.bundle_sha256, tuple(sources),
        )
        projections = all_projection_artifacts()
        with self.assertRaisesRegex(ValueError, "without an exact identity"):
            assemble_weekly_refresh_evidence(
                host_snapshot=host_snapshot(), fantasypros_league=broken,
                projection_artifacts=projections,
                ecr_artifacts=(ecr_artifact(RankingHorizon.WEEKLY), ecr_artifact(RankingHorizon.ROS)),
                nfl_schedule=nfl_schedule(),
                analyzer_bundle=BundleFingerprint(
                    "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                    "trade-analyzer/bundle-1234567890abcdef.js", "a" * 64,
                ),
                response_schema_sha256="b" * 64, scoring="PPR", expected_team_count=2,
            )

    def test_rejects_cross_source_roster_disagreement(self):
        host = host_snapshot()
        third = SourceLeaguePlayer(
            "203", "Player Three", "RB", "CAR", ("RB", "FLEX"),
            (
                ProviderPlayerId("espn", "203"),
                ProviderPlayerId("yahoo", "303"),
                ProviderPlayerId("fantasypros", "103"),
            ),
        )
        host = replace(
            host,
            players=(*host.players, third),
            rosters=(host.rosters[0], SourceTeamRoster("e2", ("203",))),
        )
        with self.assertRaisesRegex(ValueError, "one-to-one team mapping"):
            assemble_weekly_refresh_evidence(
                host_snapshot=host,
                fantasypros_league=league_artifact(),
                projection_artifacts=all_projection_artifacts(),
                ecr_artifacts=(
                    ecr_artifact(RankingHorizon.WEEKLY),
                    ecr_artifact(RankingHorizon.ROS),
                ),
                nfl_schedule=nfl_schedule(),
                analyzer_bundle=BundleFingerprint(
                    "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                    "trade-analyzer/bundle-1234567890abcdef.js",
                    "a" * 64,
                ),
                response_schema_sha256="b" * 64,
                scoring="PPR",
                expected_team_count=2,
            )

    def test_rejects_bundle_provenance_from_a_different_capture(self):
        with self.assertRaisesRegex(ValueError, "must match the bundle captured"):
            assemble_weekly_refresh_evidence(
                host_snapshot=host_snapshot(),
                fantasypros_league=league_artifact(),
                projection_artifacts=all_projection_artifacts(),
                ecr_artifacts=(
                    ecr_artifact(RankingHorizon.WEEKLY),
                    ecr_artifact(RankingHorizon.ROS),
                ),
                nfl_schedule=nfl_schedule(),
                analyzer_bundle=BundleFingerprint(
                    "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                    "trade-analyzer/bundle-1234567890abcdef.js",
                    "c" * 64,
                ),
                response_schema_sha256="b" * 64,
                scoring="PPR",
                expected_team_count=2,
            )


if __name__ == "__main__":
    unittest.main()
