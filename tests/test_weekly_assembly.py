from datetime import datetime, timezone
from dataclasses import replace
import unittest

from trade_snapshot._scenario_random import content_id

from tests.ecr_fixtures import ecr_source_details, preseason_ros_source_details
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
from trade_snapshot.capture_normalize import ecr_provider_records
from trade_snapshot.ensemble import EnsembleConfig, ProviderWeight
from trade_snapshot.feature_engineering import (
    ProjectionAvailabilityRequirements,
    projection_availability_requirements,
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
from trade_snapshot.identity_match import reconcile_player_identities
from trade_snapshot.league_ingest import host_player_records
from trade_snapshot.nfl_schedule import (
    NflSchedule,
    NflTeamWeek,
    NflTeamWeekStatus,
    canonical_nfl_game_id,
)
from trade_snapshot.scoring import ScoringProfile
from trade_snapshot.projections import (
    ProjectionStatus,
    RemainingSeasonOrigin,
    RemainingSeasonProjection,
    WeeklyProjection,
)
from trade_snapshot.projection_source import (
    ProjectionAttemptReason,
    ProjectionAttemptStatus,
    ProjectionInputPresence,
    ProjectionSourceAttempt,
)
from trade_snapshot.weekly_assembly import (
    _validate_capture_before_first_remaining_kickoff,
    _dedupe_player_records,
    _fantasypros_bootstrap_identity_evidence,
    _merge_ecr_artifacts,
    _materializable_projection_player_ids,
    _validate_preseason_ros_capture_times,
    assemble_weekly_refresh_evidence,
)
from trade_snapshot.weekly_engine import prepare_weekly_model_inputs


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def nfl_schedule(final_week=18):
    rows = []
    for team, opponent in (("ARI", "BUF"), ("ATL", "CAR")):
        for week in range(1, final_week + 1):
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
            {"player_id": "103", "name": "Player Three", "position_id": "RB",
             "team_id": "CAR"},
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
        source_scoring="PPR",
        position_scope=("RB",), expert_ids=("expert-1",), expert_count=1,
        capture_method=ECRCaptureMethod.VISIBLE_PAGE,
        last_updated_text="today", last_updated_at="2026-09-01T00:00:00Z",
        captured_at="2026-09-01T01:00:00Z",
        source_details=replace(
            ecr_source_details(horizon=horizon.value),
            source_player_count=3,
            source_position_counts={"RB": 3},
        ),
        rankings=(
            ECRRankingRow("101", "Player One", "ARI", "RB", 1, 1, 2, 1.2, .2, "RB1", {"ECR": "1"}),
            ECRRankingRow("102", "Player Two", "ATL", "RB", 2, 1, 3, 2.1, .3, "RB2", {"ECR": "2"}),
            ECRRankingRow("103", "Player Three", "CAR", "RB", 3, 2, 4, 3.1, .3, "RB3", {"ECR": "3"}),
        ),
    )


def ecr_artifact_with_outside_player(horizon):
    artifact = ecr_artifact(horizon)
    return replace(
        artifact,
        source_details=replace(
            artifact.source_details,
            source_player_count=4,
            source_position_counts={"RB": 4},
        ),
        rankings=(
            *artifact.rankings,
            ECRRankingRow(
                "104", "Player Four", "BUF", "RB", 4, 3, 5, 4.1, .3,
                "RB4", {"ECR": "4"},
            ),
        ),
    )


def projection_artifact(
    provider,
    horizon,
    week,
    *,
    missing_player_three=False,
    omit_player_one=False,
    omit_player_three=False,
):
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
    points = ("100", "90", "80") if horizon is RankingHorizon.ROS else ("10", "9", "8")
    player_rows = (
        (
            VisibleTableCell("Player One", (links[0],)),
            VisibleTableCell("ARI"),
            VisibleTableCell("RB"),
            VisibleTableCell(points[0]),
        ),
        (
            VisibleTableCell("Player Two", (links[1],)),
            VisibleTableCell("ATL"),
            VisibleTableCell("RB"),
            VisibleTableCell(points[1]),
        ),
        (
            VisibleTableCell("Player Three", (links[2],)),
            VisibleTableCell("CAR"),
            VisibleTableCell("RB"),
            VisibleTableCell("" if missing_player_three else points[2]),
        ),
    )
    retained_rows = tuple(
        row
        for index, row in enumerate(player_rows, 1)
        if not (index == 1 and omit_player_one)
        and not (index == 3 and omit_player_three)
    )
    table = VisibleTable((
        tuple(
            VisibleTableCell(value)
            for value in ("PLAYER", "TEAM", "POS", "FPTS")
        ),
        *retained_rows,
    ))
    return GenericTableArtifact(
        content_id(
            "captask",
            {
                "provider": provider.value,
                "horizon": horizon.value,
                "week": week,
                "missing_player_three": missing_player_three,
                "omit_player_one": omit_player_one,
                "omit_player_three": omit_player_three,
            },
        ),
        provider, 2026, week, CaptureKind.VISIBLE_TABLE,
        "2026-09-01T01:00:00Z", horizon, "PPR", ("RB",),
        "2026 PPR", 1, True, (table,),
    )


def projection_artifact_with_outside_player(provider, horizon, week):
    artifact = projection_artifact(provider, horizon, week)
    links = {
        CaptureProvider.FANTASYPROS:
            "https://www.fantasypros.com/nfl/players/player-four.php",
        CaptureProvider.ESPN:
            "https://www.espn.com/nfl/player/_/id/204/player-four",
        CaptureProvider.YAHOO:
            "https://sports.yahoo.com/nfl/players/304/",
        CaptureProvider.CBS:
            "https://www.cbssports.com/nfl/players/404/player-four/fantasy/",
        CaptureProvider.FFTODAY:
            "https://www.fftoday.com/stats/players/504/Player_Four",
        CaptureProvider.FANTASYSHARKS:
            "https://www.fantasysharks.com/apps/bert/players/playerpage.php?id=604",
    }
    row = (
        VisibleTableCell("Player Four", (links[provider],)),
        VisibleTableCell("BUF"),
        VisibleTableCell("RB"),
        VisibleTableCell("20" if horizon is RankingHorizon.ROS else "2"),
    )
    return replace(
        artifact,
        tables=(VisibleTable((*artifact.tables[0].rows, row)),),
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


def projection_attempt(artifact):
    return ProjectionSourceAttempt(
        task_id=artifact.task_id,
        provider=artifact.provider,
        season=artifact.season,
        week=artifact.week,
        horizon=artifact.horizon,
        scoring=artifact.scoring,
        position_scope=artifact.position_scope,
        attempted_at=datetime.fromisoformat(
            artifact.captured_at.replace("Z", "+00:00")
        ),
        status=ProjectionAttemptStatus.CAPTURED,
        reason_code=ProjectionAttemptReason.CAPTURED,
        artifact_id=artifact.artifact_id,
    )


class WeeklyAssemblyTests(unittest.TestCase):
    def test_retains_projected_players_outside_the_bounded_calculation_pool(self):
        projections = tuple(
            projection_artifact_with_outside_player(provider, horizon, week)
            for provider in (
                CaptureProvider.FANTASYPROS,
                CaptureProvider.ESPN,
                CaptureProvider.YAHOO,
            )
            for horizon, weeks in (
                (RankingHorizon.WEEKLY, (1, 2)),
                (RankingHorizon.ROS, (1,)),
            )
            for week in weeks
        )
        result = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=projections,
            ecr_artifacts=(
                ecr_artifact_with_outside_player(RankingHorizon.WEEKLY),
                ecr_artifact_with_outside_player(RankingHorizon.ROS),
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

        self.assertEqual(
            result.player_lab_projections.player_ids,
            ("fantasypros:104",),
        )
        self.assertNotIn(
            "fantasypros:104",
            {row.canonical_player_id for row in result.evidence.projection_evidence},
        )

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
        self.assertNotIn(
            "fantasypros",
            {row.provider for row in result.evidence.projection_evidence},
        )
        self.assertIn(
            CaptureProvider.FANTASYPROS,
            {
                row.provider
                for row in result.evidence.projection_source_manifest.attempts
            },
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

    def test_unpublished_future_fantasypros_attempt_keeps_two_source_projection_quorum(self):
        artifacts = (
            projection_artifact(CaptureProvider.FANTASYPROS, RankingHorizon.WEEKLY, 1),
            projection_artifact(CaptureProvider.ESPN, RankingHorizon.WEEKLY, 1),
            projection_artifact(CaptureProvider.ESPN, RankingHorizon.ROS, 1),
            projection_artifact(CaptureProvider.YAHOO, RankingHorizon.WEEKLY, 1),
            projection_artifact(CaptureProvider.YAHOO, RankingHorizon.ROS, 1),
        )
        attempts = tuple(projection_attempt(artifact) for artifact in artifacts) + (
            ProjectionSourceAttempt(
                task_id=content_id("captask", {
                    "provider": "fantasypros", "horizon": "weekly", "week": 2,
                }),
                provider=CaptureProvider.FANTASYPROS,
                season=2026,
                week=2,
                horizon=RankingHorizon.WEEKLY,
                scoring="PPR",
                position_scope=("RB",),
                attempted_at=NOW,
                status=ProjectionAttemptStatus.NOT_PUBLISHED,
                reason_code=ProjectionAttemptReason.SOURCE_NOT_PUBLISHED,
            ),
        )
        result = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=artifacts,
            projection_source_attempts=attempts,
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
        missing = tuple(
            row for row in evidence.projection_source_manifest.attempts
            if row.status is ProjectionAttemptStatus.NOT_PUBLISHED
        )
        self.assertEqual([(row.provider.value, row.week) for row in missing], [
            ("fantasypros", 2)
        ])

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
        week_two = tuple(row for row in prepared.projections if row.week == 2)
        self.assertTrue(week_two)
        self.assertTrue(all(
            sum(
                observation.status is ProjectionStatus.OBSERVED
                for observation in row.provider_observations
            ) >= 2
            for row in week_two
        ))

    def test_bootstrap_crosswalk_resolves_rostered_player_missing_from_public_tables(self):
        host = host_snapshot()
        host = replace(
            host,
            players=tuple(
                replace(
                    player,
                    provider_ids=(ProviderPlayerId("espn", player.source_player_id),),
                )
                for player in host.players
            ),
        )
        artifact = league_artifact()
        sources = []
        for source in artifact.sources:
            record = source.to_record()
            if source.source is LeagueSourceKind.BOOTSTRAP:
                for index, player in enumerate(record["body"]["payload"]["players"], 1):
                    player["espn_id"] = str(200 + index)
            sources.append(LeagueSource.from_record(record))
        artifact = FantasyProsLeagueArtifact(
            artifact.task_id,
            artifact.provider,
            artifact.season,
            artifact.week,
            artifact.kind,
            artifact.captured_at,
            artifact.team_count,
            True,
            artifact.bundle_url,
            artifact.bundle_sha256,
            tuple(sources),
        )

        host_records = _dedupe_player_records(host_player_records(host))
        bootstrap_records, links = _fantasypros_bootstrap_identity_evidence(
            artifact, host_records
        )
        identities = reconcile_player_identities(
            _dedupe_player_records((*host_records, *bootstrap_records)),
            anchor_provider="fantasypros",
            verified_links=links,
        )

        self.assertEqual(
            identities.lookup("espn", "201").canonical_player_id,
            "fantasypros:101",
        )
        self.assertEqual(
            identities.lookup("fantasypros", "102").canonical_player_id,
            "fantasypros:102",
        )
        self.assertEqual(identities.unresolved, ())

    def test_preseason_ros_fallback_expires_at_first_week_one_kickoff(self):
        kickoff = datetime(2026, 9, 3, 18, tzinfo=timezone.utc)
        schedule = nfl_schedule()
        schedule = NflSchedule(
            schedule.season,
            schedule.captured_at,
            schedule.source_provider,
            tuple(replace(row, kickoff_at=kickoff) for row in schedule.team_weeks),
        )
        fallback = replace(
            ecr_artifact(RankingHorizon.ROS),
            source_details=preseason_ros_source_details(source_player_count=3),
        )

        _validate_preseason_ros_capture_times((fallback,), schedule)

        with self.assertRaisesRegex(ValueError, "at or after"):
            _validate_preseason_ros_capture_times(
                (replace(fallback, captured_at="2026-09-03T18:00:00Z"),),
                schedule,
            )
        with self.assertRaisesRegex(ValueError, "kickoff times"):
            _validate_preseason_ros_capture_times((fallback,), nfl_schedule())

    def test_capture_window_rejects_a_partially_played_current_week(self):
        kickoff = datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
        schedule = nfl_schedule()
        schedule = NflSchedule(
            schedule.season,
            schedule.captured_at,
            schedule.source_provider,
            tuple(replace(row, kickoff_at=kickoff) for row in schedule.team_weeks),
        )
        with self.assertRaisesRegex(ValueError, "first kickoff"):
            _validate_capture_before_first_remaining_kickoff(
                host_snapshot(),
                league_artifact(),
                all_projection_artifacts(),
                (
                    ecr_artifact(RankingHorizon.WEEKLY),
                    ecr_artifact(RankingHorizon.ROS),
                ),
                schedule,
            )

    def test_materializable_players_require_weekly_quorum_and_formula_horizons(self):
        providers = ("fantasypros", "espn", "yahoo")
        rows = []
        for player_id in ("complete-ros", "complete-weekly", "incomplete"):
            for provider in providers:
                rows.append(
                    RemainingSeasonProjection(
                        player_id,
                        "week-1",
                        "profile-1",
                        provider,
                        f"{provider}-{player_id}",
                        2026,
                        (1, 2),
                        (
                            ProjectionStatus.OBSERVED
                            if player_id == "complete-ros"
                            else ProjectionStatus.NOT_PUBLISHED
                        ),
                        RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                        NOW,
                        20.0 if player_id == "complete-ros" else None,
                    )
                )
                for week in (1, 2):
                    if player_id == "incomplete" and provider == "yahoo" and week == 2:
                        continue
                    rows.append(
                        WeeklyProjection(
                            player_id,
                            "week-1",
                            "profile-1",
                            provider,
                            f"{provider}-{player_id}",
                            2026,
                            week,
                            ProjectionStatus.OBSERVED,
                            NOW,
                            10.0,
                        )
                    )

        self.assertEqual(
            _materializable_projection_player_ids(
                rows,
                {
                    "complete-ros": (1, 2),
                    "complete-weekly": (1, 2),
                    "incomplete": (1, 2),
                },
                calculation_weeks=(1, 2),
                current_week=1,
                provider_names=providers,
                minimum_observed_sources=3,
                requirements=projection_availability_requirements(
                    ("projection_fantasypros_full_ros_points",),
                    providers,
                ),
            ),
            {"complete-ros", "complete-weekly"},
        )

        self.assertEqual(
            _materializable_projection_player_ids(
                rows,
                {
                    "complete-ros": (1, 2),
                    "complete-weekly": (1, 2),
                    "incomplete": (1, 2),
                },
                calculation_weeks=(1, 2),
                current_week=1,
                provider_names=providers,
                minimum_observed_sources=2,
                requirements=projection_availability_requirements(
                    ("projection_fantasypros_full_ros_points",),
                    providers,
                ),
            ),
            {"complete-ros", "complete-weekly", "incomplete"},
        )

        self.assertEqual(
            _materializable_projection_player_ids(
                rows,
                {
                    "complete-ros": (1, 2),
                    "complete-weekly": (1, 2),
                    "incomplete": (1, 2),
                },
                calculation_weeks=(1, 2),
                current_week=1,
                provider_names=("fantasypros", "espn"),
                minimum_observed_sources=2,
                requirements=projection_availability_requirements(
                    ("projection_fantasypros_full_ros_points",),
                    ("fantasypros", "espn"),
                ),
            ),
            {"complete-ros", "complete-weekly", "incomplete"},
        )

    def test_materializable_players_separate_weekly_and_full_horizon_coverage(self):
        providers = ("fantasypros", "espn", "yahoo")
        rows = []
        for provider in providers:
            observed_ros = provider == "fantasypros"
            rows.append(
                RemainingSeasonProjection(
                    "player",
                    "week-1",
                    "profile-1",
                    provider,
                    f"{provider}-player",
                    2026,
                    (1, 2, 3),
                    (
                        ProjectionStatus.OBSERVED
                        if observed_ros
                        else ProjectionStatus.NOT_PUBLISHED
                    ),
                    RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                    NOW,
                    30.0 if observed_ros else None,
                )
            )
        for week in (1, 2):
            rows.append(
                WeeklyProjection(
                    "player",
                    "week-1",
                    "profile-1",
                    "espn",
                    "espn-player",
                    2026,
                    week,
                    ProjectionStatus.OBSERVED,
                    NOW,
                    10.0,
                )
            )
        common = {
            "rows": rows,
            "active_weeks_by_player": {"player": (1, 2, 3)},
            "calculation_weeks": (1, 2),
            "current_week": 1,
            "provider_names": providers,
            "minimum_observed_sources": 2,
        }

        self.assertEqual(
            _materializable_projection_player_ids(
                **common,
                requirements=projection_availability_requirements(
                    ("projection_fantasypros_full_ros_points",),
                    providers,
                ),
            ),
            {"player"},
        )
        self.assertEqual(
            _materializable_projection_player_ids(
                **common,
                requirements=projection_availability_requirements(
                    (
                        "projection_fantasypros_full_ros_points",
                        "projection_ensemble_full_ros_points",
                    ),
                    providers,
                ),
            ),
            set(),
        )

    def test_materializable_players_apply_quorum_per_week_and_preserve_horizon(self):
        providers = ("fantasypros", "espn", "yahoo")
        rows = [
            RemainingSeasonProjection(
                "player",
                "week-1",
                "profile-1",
                provider,
                f"{provider}-player",
                2026,
                (1, 2),
                ProjectionStatus.NOT_PUBLISHED,
                RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                NOW,
            )
            for provider in providers
        ]
        for provider, weeks in (
            ("fantasypros", (1, 2)),
            ("espn", (1,)),
            ("yahoo", (2,)),
        ):
            rows.extend(
                WeeklyProjection(
                    "player",
                    "week-1",
                    "profile-1",
                    provider,
                    f"{provider}-player",
                    2026,
                    week,
                    ProjectionStatus.OBSERVED,
                    NOW,
                    10.0,
                )
                for week in weeks
            )
        common = {
            "rows": rows,
            "active_weeks_by_player": {"player": (1, 2)},
            "calculation_weeks": (1, 2),
            "current_week": 1,
            "provider_names": providers,
            "minimum_observed_sources": 2,
        }

        self.assertEqual(
            _materializable_projection_player_ids(
                **common,
                requirements=ProjectionAvailabilityRequirements(
                    frozenset({"fantasypros"}),
                    frozenset(),
                    False,
                    False,
                ),
            ),
            {"player"},
        )
        self.assertEqual(
            _materializable_projection_player_ids(
                **common,
                requirements=ProjectionAvailabilityRequirements(
                    frozenset(),
                    frozenset({"espn"}),
                    False,
                    False,
                ),
            ),
            set(),
        )

    def test_materializable_players_match_capture_and_bye_validation(self):
        providers = ("fantasypros", "espn", "yahoo")
        requirements = ProjectionAvailabilityRequirements(
            frozenset(),
            frozenset(),
            False,
            False,
        )
        two_source_ros = [
            RemainingSeasonProjection(
                "player",
                "week-1",
                "profile-1",
                provider,
                f"{provider}-player",
                2026,
                (1, 2, 3),
                ProjectionStatus.OBSERVED,
                RemainingSeasonOrigin.PROVIDER_PUBLISHED,
                NOW,
                30.0,
            )
            for provider in ("fantasypros", "espn")
        ]
        outside_scope = [
            *two_source_ros,
            WeeklyProjection(
                "player",
                "week-1",
                "profile-1",
                "yahoo",
                "yahoo-player",
                2026,
                3,
                ProjectionStatus.OBSERVED,
                NOW,
                10.0,
            ),
        ]
        self.assertEqual(
            _materializable_projection_player_ids(
                outside_scope,
                {"player": (1, 2, 3)},
                calculation_weeks=(1, 2),
                current_week=1,
                provider_names=providers,
                minimum_observed_sources=2,
                requirements=requirements,
            ),
            set(),
        )

        bye_conflict = [
            *(
                replace(row, applicable_weeks=(1,), projected_fantasy_points=10.0)
                for row in two_source_ros
            ),
            WeeklyProjection(
                "player",
                "week-1",
                "profile-1",
                "yahoo",
                "yahoo-player",
                2026,
                2,
                ProjectionStatus.OBSERVED,
                NOW,
                10.0,
            ),
        ]
        self.assertEqual(
            _materializable_projection_player_ids(
                bye_conflict,
                {"player": (1,)},
                calculation_weeks=(1, 2),
                current_week=1,
                provider_names=providers,
                minimum_observed_sources=2,
                requirements=requirements,
            ),
            set(),
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
        projection_manifest = result.evidence.projection_source_manifest
        self.assertEqual(
            {row.artifact_id for row in projection_manifest.sources},
            {row.artifact_id for row in projections},
        )
        self.assertEqual(
            {row.artifact_id for row in projection_manifest.attempts},
            {row.artifact_id for row in projections},
        )
        projection_manifest.validate_projection_evidence(
            result.evidence.projection_evidence
        )
        self.assertEqual(
            {
                row.team_id: row.playoff_probability
                for row in result.evidence.fantasypros_benchmark.teams
            },
            {"espn:team:e1": 0.6, "espn:team:e2": 0.4},
        )
        self.assertEqual(
            result.evidence.fantasypros_benchmark.source_artifact_id,
            league_artifact().artifact_id,
        )
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

    def test_waiver_selection_uses_configured_quorum_and_required_power_source(self):
        artifacts = tuple(
            projection_artifact(
                provider,
                horizon,
                week,
                missing_player_three=provider is CaptureProvider.YAHOO,
            )
            for provider in CaptureProvider
            for horizon, weeks in (
                (RankingHorizon.WEEKLY, (1, 2)),
                (RankingHorizon.ROS, (1,)),
            )
            for week in weeks
        )
        common = {
            "host_snapshot": host_snapshot(),
            "fantasypros_league": league_artifact(),
            "projection_artifacts": artifacts,
            "ecr_artifacts": (
                ecr_artifact(RankingHorizon.WEEKLY),
                ecr_artifact(RankingHorizon.ROS),
            ),
            "nfl_schedule": nfl_schedule(),
            "analyzer_bundle": BundleFingerprint(
                "https://cdn.fantasypros.com/assets/js/min/pages/myplaybook/"
                "trade-analyzer/bundle-1234567890abcdef.js",
                "a" * 64,
            ),
            "response_schema_sha256": "b" * 64,
            "scoring": "PPR",
            "expected_team_count": 2,
        }

        default = assemble_weekly_refresh_evidence(**common)
        custom = assemble_weekly_refresh_evidence(
            **{
                **common,
                "projection_artifacts": tuple(
                    row
                    for row in artifacts
                    if row.provider
                    in {CaptureProvider.ESPN, CaptureProvider.YAHOO}
                ),
            },
            ensemble_config=EnsembleConfig(
                (
                    ProviderWeight("espn", 1),
                    ProviderWeight("yahoo", 1),
                ),
                1,
                {"RB": 0},
            ),
        )

        self.assertEqual(default.evidence.waiver_pool.player_ids, ("fantasypros:103",))
        self.assertEqual(custom.evidence.waiver_pool.player_ids, ("fantasypros:103",))
        self.assertEqual(
            tuple(
                row.provider
                for row in custom.evidence.ensemble_config.provider_weights
            ),
            ("espn", "yahoo"),
        )

    def test_complete_table_omissions_preserve_roster_and_waiver_quorum(self):
        artifacts = tuple(
            projection_artifact(
                provider,
                horizon,
                week,
                omit_player_one=(
                    provider is CaptureProvider.ESPN
                    and horizon is RankingHorizon.WEEKLY
                    and week == 1
                ),
                omit_player_three=(
                    provider is CaptureProvider.YAHOO
                    and horizon is RankingHorizon.WEEKLY
                    and week == 1
                ),
                missing_player_three=(
                    provider is CaptureProvider.YAHOO
                    and horizon is RankingHorizon.ROS
                ),
            )
            for provider in CaptureProvider
            for horizon, weeks in (
                (RankingHorizon.WEEKLY, (1, 2)),
                (RankingHorizon.ROS, (1,)),
            )
            for week in weeks
        )
        assembled = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=artifacts,
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
        evidence = assembled.evidence
        omitted = {
            (binding.canonical_player_id, source.provider.value): binding
            for source in evidence.projection_source_manifest.sources
            if source.horizon is RankingHorizon.WEEKLY and source.week == 1
            for binding in source.inputs
            if binding.presence
            is ProjectionInputPresence.OMITTED_FROM_COMPLETE_CAPTURE
        }

        self.assertEqual(evidence.waiver_pool.player_ids, ("fantasypros:103",))
        self.assertEqual(
            set(omitted),
            {
                ("fantasypros:101", "espn"),
                ("fantasypros:103", "yahoo"),
            },
        )
        self.assertEqual(
            omitted[("fantasypros:101", "espn")].provider_player_id, "201"
        )
        self.assertEqual(
            omitted[("fantasypros:103", "yahoo")].provider_player_id, "303"
        )

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
        week_one = {
            row.canonical_player_id: row
            for row in prepared.projections
            if row.week == 1
        }
        self.assertGreaterEqual(
            week_one["fantasypros:101"].observed_source_count, 2
        )
        waiver_projection = week_one["fantasypros:103"]
        self.assertEqual(waiver_projection.observed_source_count, 2)
        observation = next(
            row
            for row in waiver_projection.provider_observations
            if row.provider == "yahoo"
        )
        self.assertIs(observation.status, ProjectionStatus.NOT_PUBLISHED)

    def test_waiver_can_use_weekly_quorum_when_optional_ros_sources_are_absent(self):
        artifacts = tuple(
            projection_artifact(
                provider,
                horizon,
                week,
                missing_player_three=(
                    provider is CaptureProvider.YAHOO
                    or (
                        provider is CaptureProvider.ESPN
                        and horizon is RankingHorizon.ROS
                    )
                ),
            )
            for provider in CaptureProvider
            for horizon, weeks in (
                (RankingHorizon.WEEKLY, (1, 2)),
                (RankingHorizon.ROS, (1,)),
            )
            for week in weeks
        )
        assembled = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=artifacts,
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
        evidence = assembled.evidence

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
        waiver_id = "fantasypros:103"
        waiver_features = next(
            row.values
            for row in prepared.features.player_features
            if row.player_id == waiver_id
        )

        self.assertEqual(evidence.waiver_pool.player_ids, (waiver_id,))
        self.assertEqual(waiver_features["projection_fantasypros_full_ros_available"], 1)
        self.assertEqual(waiver_features["projection_espn_full_ros_available"], 0)
        self.assertEqual(waiver_features["projection_yahoo_full_ros_available"], 0)
        self.assertEqual(waiver_features["projection_ensemble_full_ros_available"], 0)

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
        self.assertEqual(
            {
                row.applicable_weeks
                for row in evidence.projection_evidence
                if not hasattr(row, "week")
            },
            {tuple(range(1, 19))},
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

    def test_ecr_merge_preserves_different_position_expert_panels(self):
        weekly = ecr_artifact(RankingHorizon.WEEKLY)
        ros = ecr_artifact(RankingHorizon.ROS)
        receiver = (
            ECRRankingRow(
                "104", "Player Four", "BUF", "WR", 1, 1, 2, 1.2, .2,
                "WR1", {"ECR": "1"},
            ),
        )
        weekly_receiver = replace(
            weekly,
            task_id="captask_" + "9" * 64,
            position_scope=("WR",),
            expert_ids=("expert-2", "expert-3"),
            expert_count=2,
            source_details=ecr_source_details(position="WR"),
            rankings=receiver,
        )
        ros_receiver = replace(
            weekly_receiver,
            task_id="captask_" + "8" * 64,
            horizon=RankingHorizon.ROS,
            source_details=ecr_source_details(horizon="ros", position="WR"),
        )
        registry = reconcile_player_identities((
            *ecr_provider_records(weekly),
            *ecr_provider_records(weekly_receiver),
        ))

        merged = _merge_ecr_artifacts(
            (weekly, weekly_receiver, ros, ros_receiver),
            registry,
            snapshot_id="week-1",
            scoring_profile_id="ppr",
        )
        for snapshot in merged:
            panels = {panel.position: panel for panel in snapshot.expert_panels}
            self.assertEqual(panels["RB"].expert_ids, ("expert-1",))
            self.assertEqual(
                panels["WR"].expert_ids,
                ("expert-2", "expert-3"),
            )
            self.assertEqual(
                snapshot.expert_ids,
                ("expert-1", "expert-2", "expert-3"),
            )
            self.assertEqual(snapshot.total_experts, 3)

    def test_assembly_accepts_standard_source_pages_for_ppr_qb_ecr(self):
        weekly = ecr_artifact(RankingHorizon.WEEKLY)
        ros = ecr_artifact(RankingHorizon.ROS)
        quarterback = (
            ECRRankingRow(
                "104", "Player Four", "BUF", "QB", 1, 1, 2, 1.2, .2,
                "QB1", {"ECR": "1"},
            ),
        )
        weekly_qb = replace(
            weekly,
            task_id="captask_" + "7" * 64,
            source_scoring="STD",
            position_scope=("QB",),
            expert_ids=("expert-2",),
            source_details=ecr_source_details(position="QB", source_scoring="STD"),
            rankings=quarterback,
        )
        ros_qb = replace(
            weekly_qb,
            task_id="captask_" + "8" * 64,
            horizon=RankingHorizon.ROS,
            source_details=ecr_source_details(
                horizon="ros", position="QB", source_scoring="STD"
            ),
        )

        assembled = assemble_weekly_refresh_evidence(
            host_snapshot=host_snapshot(),
            fantasypros_league=league_artifact(),
            projection_artifacts=all_projection_artifacts(),
            ecr_artifacts=(weekly, weekly_qb, ros, ros_qb),
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

        self.assertTrue(
            all(
                {panel.position for panel in snapshot.expert_panels}
                == {"QB", "RB"}
                for snapshot in assembled.evidence.ecr_snapshots
            )
        )


if __name__ == "__main__":
    unittest.main()
