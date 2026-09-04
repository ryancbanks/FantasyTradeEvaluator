import copy
import json
import unittest

from tests.capture_fixtures import league_sources
from trade_snapshot.analyzer_contract import CURRENT_BUNDLE_FINGERPRINT

from trade_snapshot.capture_schema import (
    ANALYZER_RESPONSE_SCHEMA_FINGERPRINT,
    CAPTURE_PLAN_SCHEMA_FINGERPRINT,
    ECR_TASK_SCHEMA_FINGERPRINT,
    FANTASYPROS_ECR_SCHEMA_FINGERPRINT,
    GENERIC_TABLE_SCHEMA_FINGERPRINT,
    LEAGUE_SOURCE_SCHEMA_FINGERPRINT,
    TASK_SCHEMA_FINGERPRINT,
    AnalyzerCapturePhase,
    AnalyzerResponseArtifact,
    AnalyzerTradeSpec,
    CapturePlan,
    CaptureProvider,
    ECRRankingRow,
    FantasyProsECRArtifact,
    FantasyProsECRTask,
    FantasyProsLeagueArtifact,
    GenericTableArtifact,
    PageCaptureTask,
    LeagueSource,
    LeagueSourceKind,
    ProjectionTableSpec,
    VisibleTable,
    VisibleTableCell,
    analyzer_body_matches_phase,
    artifact_from_record,
    artifact_to_record,
    capture_plan_from_record,
    public_player_link,
    sanitize_capture_body,
)


CAPTURED_AT = "2026-09-01T14:15:16Z"


class CapturePlanTests(unittest.TestCase):
    def test_round_trip_is_strict_content_addressed_and_uses_only_public_dimensions(self):
        tasks = (
            projection_task("fantasypros"), projection_task("espn"),
            projection_task("yahoo"), analyzer_task(), ecr_task("weekly"), ecr_task("ros"),
            league_task(),
        )
        plan = CapturePlan(tasks)
        record = plan.to_record()

        json.dumps(record, allow_nan=False)
        self.assertEqual(capture_plan_from_record(record), plan)
        self.assertEqual(len({task.task_id for task in tasks}), len(tasks))
        self.assertEqual(
            record["tasks"][0]["url"],
            "https://www.fantasypros.com/nfl/projections/rb.php?week=1&scoring=PPR",
        )
        self.assertNotIn("secret", json.dumps(record).casefold())

        changed = copy.deepcopy(record)
        changed["tasks"][0]["week"] = 2
        with self.assertRaises(ValueError):
            CapturePlan.from_record(changed)
        with self.assertRaises(ValueError):
            CapturePlan((*tasks, tasks[0]))

    def test_one_weekly_run_can_collect_each_remaining_projection_week(self):
        week_one = projection_task("espn")
        week_two = PageCaptureTask(
            "espn", 2026, 2, "visible_table", week_one.url,
            projection=week_one.projection,
        )
        plan = CapturePlan((week_one, week_two, league_task()))
        self.assertEqual(tuple(task.week for task in plan.tasks), (1, 2, 1))
        with self.assertRaisesRegex(ValueError, "one season"):
            CapturePlan((
                week_one,
                PageCaptureTask(
                    "espn", 2027, 1, "visible_table", week_one.url,
                    projection=week_one.projection,
                ),
            ))

    def test_urls_reject_every_query_fragment_credential_and_wrong_origin(self):
        urls = (
            "http://fantasy.espn.com/football/players",
            "https://evil.example/football/players",
            "https://user:password@fantasy.espn.com/football/players",
            "https://fantasy.espn.com/football/players#private",
            "https://fantasy.espn.com/football/players?teamId=1",
            "https://fantasy.espn.com/football/players?innocent=value",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                PageCaptureTask(
                    "espn", 2026, 1, "visible_table", url,
                    projection=ProjectionTableSpec("weekly", "PPR", ("RB",)),
                )
        with self.assertRaisesRegex(ValueError, "analyzer page"):
            PageCaptureTask(
                "fantasypros", 2026, 1, "analyzer_response",
                "https://www.fantasypros.com/nfl/myplaybook/trade-finder.php",
                "ordinary_power", AnalyzerTradeSpec(13, (1,), (2,)),
            )

    def test_typed_trade_and_projection_dimensions_are_required_and_distinct(self):
        with self.assertRaisesRegex(ValueError, "AnalyzerTradeSpec"):
            PageCaptureTask(
                "fantasypros", 2026, 1, "analyzer_response",
                "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
                "ordinary_power",
            )
        with self.assertRaisesRegex(ValueError, "ProjectionTableSpec"):
            PageCaptureTask("espn", 2026, 1, "visible_table", "https://fantasy.espn.com/football/players")
        with self.assertRaises(ValueError):
            AnalyzerTradeSpec(13, (1, 2), (2, 3))
        self.assertNotEqual(
            analyzer_task(AnalyzerTradeSpec(13, (1,), (2,))).task_id,
            analyzer_task(AnalyzerTradeSpec(13, (1,), (3,))).task_id,
        )
        self.assertEqual(projection_task("fantasypros").projection.scoring, "PPR")

    def test_fingerprints_are_golden_behavior_versions(self):
        self.assertEqual(
            {
                "task": TASK_SCHEMA_FINGERPRINT,
                "plan": CAPTURE_PLAN_SCHEMA_FINGERPRINT,
                "table": GENERIC_TABLE_SCHEMA_FINGERPRINT,
                "analyzer": ANALYZER_RESPONSE_SCHEMA_FINGERPRINT,
                "ecr_task": ECR_TASK_SCHEMA_FINGERPRINT,
                "ecr": FANTASYPROS_ECR_SCHEMA_FINGERPRINT,
                "league": LEAGUE_SOURCE_SCHEMA_FINGERPRINT,
            },
            {
                "task": "capschema_af16dfc09b6db286820a7c025004d1bd2dab20eb6c32ff0e13aecd537662a83d",
                "plan": "capschema_971f6395a83a25ed94a80ffdc4ee8b3cda1de01f08538170fb5cda45ff33ab5a",
                "table": "capschema_b188bdea5c549ed3992750e5663d2b4c32a956b760174df2b5f3d1fbf5b83265",
                "analyzer": "capschema_bcf693343ec0769715115324a1c4e1bd210d418eb52abca72cca5d820f73092e",
                "ecr_task": "capschema_62035209fe09acc94aece7e364f3af99fa5c9ba622decd4213dfd5af7ecb7175",
                "ecr": "capschema_4c36763ac799eb4a267a836e532eecbd2e7582ab0f379272e753a31828569092",
                "league": "capschema_5de2665e9d4c2e5de7ffa7ff467e13cb8b2a0bea85e8a96be30f6dfcf585a202",
            },
        )

    def test_league_task_is_queryless_fantasypros_only(self):
        task = league_task()
        self.assertEqual(PageCaptureTask.from_record(task.to_record()), task)
        for provider, url in (
            ("espn", task.url),
            ("fantasypros", "https://www.fantasypros.com/nfl/myplaybook/league-analyzer.php"),
            ("fantasypros", task.url + "?key=secret"),
        ):
            with self.subTest(provider=provider, url=url), self.assertRaises(ValueError):
                PageCaptureTask(provider, 2026, 1, "league_source", url)


class ProjectionArtifactTests(unittest.TestCase):
    def test_all_three_providers_keep_only_public_identity_links_and_evidence(self):
        for provider, link in (
            ("fantasypros", "https://www.fantasypros.com/nfl/players/player-a.php?x=1"),
            ("espn", "https://www.espn.com/nfl/player/_/id/123/player-a?x=1"),
            ("yahoo", "https://sports.yahoo.com/nfl/players/456/?x=1"),
        ):
            task = projection_task(provider)
            table = VisibleTable((
                (VisibleTableCell("PLAYER"), VisibleTableCell("FPTS")),
                (VisibleTableCell("Player A", (link,)), VisibleTableCell("12.4")),
            ))
            artifact = GenericTableArtifact(
                task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                "weekly", "PPR", ("RB",), "2026 | Week 1 | PPR | RB", 2, True,
                (table,),
            )
            record = artifact_to_record(artifact, task)
            self.assertEqual(artifact_from_record(record, task), artifact)
            self.assertTrue(record["complete"])
            self.assertEqual(record["segments_captured"], 2)
            self.assertNotIn("?", json.dumps(record))

    def test_private_captions_external_links_and_incomplete_capture_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "captions"):
            VisibleTable(((VisibleTableCell("PLAYER"),),), caption="My Private League")
        with self.assertRaisesRegex(ValueError, "transport URLs"):
            VisibleTableCell("https://private.example/?token=secret")
        task = projection_task("espn")
        table = VisibleTable(((VisibleTableCell("PLAYER", ("https://example.com/player",)),),))
        with self.assertRaisesRegex(ValueError, "public identity"):
            GenericTableArtifact(
                task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                "weekly", "PPR", ("RB",), "Week 1", 1, True, (table,),
            )
        valid = VisibleTable(((VisibleTableCell("PLAYER"),),))
        with self.assertRaisesRegex(ValueError, "complete"):
            GenericTableArtifact(
                task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                "weekly", "PPR", ("RB",), "Week 1", 1, False, (valid,),
            )

    def test_public_source_identity_links_are_minimized_without_losing_identity(self):
        self.assertEqual(
            public_player_link(
                "fftoday",
                "https://www.fftoday.com/stats/players/501/A.J._Brown"
                "?LeagueID=107644#top",
            ),
            "https://www.fftoday.com/stats/players/501/A.J._Brown",
        )
        self.assertEqual(
            public_player_link(
                "fantasysharks",
                "https://www.fantasysharks.com/apps/bert/players/"
                "playerpage.php?id=13589",
            ),
            "https://www.fantasysharks.com/apps/bert/players/"
            "playerpage.php?id=13589",
        )
        for invalid in (
            "https://www.fantasysharks.com/apps/bert/players/"
            "playerpage.php?id=13589&league=private",
            "https://www.fftoday.com/stats/players/not-an-id/Josh_Allen",
            "https://evil.example/stats/players/501/Josh_Allen",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(
                    public_player_link(
                        "fantasysharks" if "fantasysharks" in invalid else "fftoday",
                        invalid,
                    )
                )


class AnalyzerArtifactTests(unittest.TestCase):
    def test_phase_allowlist_discards_every_non_result_field(self):
        task = analyzer_task()
        raw = {
            **playoff_body(),
            "request": {"url": "https://example.test/?token=x", "headers": {"jwt": "x"}},
            "signature": "abc", "nonce": "n", "access_ticket": "ticket",
            "embedded": "prefix https%3A%2F%2Fevil.example%2Fsecret suffix",
            "nested": [{"safe": 1, "cookie": "secret"}],
        }
        artifact = AnalyzerResponseArtifact(
            task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
            "full_playoffs", CURRENT_BUNDLE_FINGERPRINT.url,
            CURRENT_BUNDLE_FINGERPRINT.sha256, raw,
        )
        self.assertEqual(artifact.to_record()["body"], playoff_body())
        self.assertEqual(artifact_from_record(artifact.to_record(), task), artifact)

    def test_persisted_extra_field_and_wrong_phase_are_rejected(self):
        task = analyzer_task()
        artifact = AnalyzerResponseArtifact(
            task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
            "full_playoffs", CURRENT_BUNDLE_FINGERPRINT.url,
            CURRENT_BUNDLE_FINGERPRINT.sha256, playoff_body(),
        )
        record = artifact.to_record()
        record["body"]["nonce"] = "x"
        with self.assertRaisesRegex(ValueError, "non-allowlisted"):
            artifact_from_record(record, task)
        self.assertFalse(analyzer_body_matches_phase(playoff_body(), "ordinary_power"))
        self.assertTrue(analyzer_body_matches_phase(power_body(), "ordinary_power"))

    def test_bundle_provenance_is_required_and_content_addressed(self):
        task = analyzer_task()
        with self.assertRaisesRegex(ValueError, "bundle fingerprint"):
            AnalyzerResponseArtifact(
                task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                "full_playoffs", "https://cdn.fantasypros.com/assets/js/vendor.js",
                "0" * 64, playoff_body(),
            )
        first = AnalyzerResponseArtifact(
            task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
            "full_playoffs", CURRENT_BUNDLE_FINGERPRINT.url,
            CURRENT_BUNDLE_FINGERPRINT.sha256, playoff_body(),
        )
        changed = AnalyzerResponseArtifact(
            task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
            "full_playoffs", CURRENT_BUNDLE_FINGERPRINT.url,
            "0" * 64, playoff_body(),
        )
        self.assertNotEqual(first.artifact_id, changed.artifact_id)


class LeagueArtifactTests(unittest.TestCase):
    def test_optional_league_identifiers_are_normalized_decimal_text(self):
        bootstrap = next(
            source for source in league_sources()
            if source.source is LeagueSourceKind.BOOTSTRAP
        ).to_record()["body"]["payload"]
        for field, invalid in (("id", 77), ("team_id", 1), ("id", "0")):
            changed = copy.deepcopy(bootstrap)
            changed["league"][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(
                ValueError, "positive decimal"
            ):
                LeagueSource("bootstrap", {"payload": changed})

    def test_complete_sources_round_trip_and_remove_secrets_urls(self):
        task = league_task()
        records = [source.to_record() for source in league_sources()]
        records[0]["body"]["payload"].update({
            "leagueKey": "secret", "requestUrl": "https://example.test/?token=x",
        })
        sources = tuple(LeagueSource(record["source"], record["body"]) for record in records)
        artifact = FantasyProsLeagueArtifact(
            task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT, 2, True,
            CURRENT_BUNDLE_FINGERPRINT.url, CURRENT_BUNDLE_FINGERPRINT.sha256, sources,
        )
        record = artifact_to_record(artifact, task)
        self.assertEqual(artifact_from_record(record, task), artifact)
        serialized = json.dumps(record)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("example.test", serialized)

    def test_projected_standings_preserve_censored_odds_and_reject_bad_percentages(self):
        projected = next(
            source for source in league_sources()
            if source.source is LeagueSourceKind.PROJECTED_STANDINGS
        ).to_record()["body"]["payload"]
        projected["standings"][0]["championship_odds"] = "<1%"
        source = LeagueSource("projected_standings", {"payload": projected})
        self.assertEqual(
            source.to_record()["body"]["payload"]["standings"][0]["championship_odds"],
            "<1%",
        )
        for invalid in ("1-ish%", "101%", 101, -1, True, [], {}):
            changed = copy.deepcopy(projected)
            changed["standings"][0]["playoffs_odds"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "percentage"
            ):
                LeagueSource("projected_standings", {"payload": changed})

    def test_missing_source_and_injected_persisted_secret_fail_closed(self):
        task = league_task()
        sources = league_sources()
        with self.assertRaisesRegex(ValueError, "every required source"):
            FantasyProsLeagueArtifact(
                task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                2, True, CURRENT_BUNDLE_FINGERPRINT.url,
                CURRENT_BUNDLE_FINGERPRINT.sha256, sources[:-1],
            )
        artifact = FantasyProsLeagueArtifact(
            task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT, 2, True,
            CURRENT_BUNDLE_FINGERPRINT.url, CURRENT_BUNDLE_FINGERPRINT.sha256, sources,
        )
        record = artifact.to_record()
        record["sources"][0]["body"]["payload"]["oauthToken"] = "secret"
        with self.assertRaisesRegex(ValueError, "secret, transport, or URL"):
            artifact_from_record(record, task)

    def test_best_free_agent_ids_are_exact_and_cannot_be_owned(self):
        task = league_task()
        sources = list(league_sources())
        analyzer_index = next(
            index
            for index, source in enumerate(sources)
            if source.source is LeagueSourceKind.ANALYZER_INIT
        )
        analyzer_payload = sources[analyzer_index].to_record()["body"]["payload"]
        self.assertEqual(analyzer_payload["best_free_agent_ids"], ["9001"])

        for invalid in ([], ["0"], ["9001", "9001"], [9001]):
            changed = copy.deepcopy(analyzer_payload)
            changed["best_free_agent_ids"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "best[_ -]free"
            ):
                LeagueSource("analyzer_init", {"payload": changed})

        changed_sources = list(sources)
        owned = copy.deepcopy(analyzer_payload)
        owned["best_free_agent_ids"] = ["1001"]
        changed_sources[analyzer_index] = LeagueSource(
            "analyzer_init", {"payload": owned}
        )
        with self.assertRaisesRegex(ValueError, "cannot already belong"):
            FantasyProsLeagueArtifact(
                task.task_id,
                task.provider,
                2026,
                1,
                task.kind,
                CAPTURED_AT,
                2,
                True,
                CURRENT_BUNDLE_FINGERPRINT.url,
                CURRENT_BUNDLE_FINGERPRINT.sha256,
                changed_sources,
            )

    def test_bootstrap_player_crosswalk_ids_are_unique_and_typed(self):
        source = league_sources()[0].to_record()
        players = source["body"]["payload"]["players"]
        players[0]["espn_id"] = "2001"
        players[1]["espn_id"] = "2001"
        with self.assertRaisesRegex(ValueError, "unique espn_id"):
            LeagueSource.from_record(source)

        source = league_sources()[0].to_record()
        source["body"]["payload"]["players"][0]["yahoo_id"] = 3001
        with self.assertRaisesRegex(ValueError, "positive decimal"):
            LeagueSource.from_record(source)

    def test_semantically_empty_error_and_unrelated_league_payloads_fail_closed(self):
        for payload in ({"ok": True}, {"error": "signed out"}, {"leagues": []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                LeagueSource("bootstrap", {"payload": payload})

    def test_roster_team_count_mismatch_and_missing_standings_fail_closed(self):
        task = league_task()
        sources = list(league_sources())
        bootstrap = next(
            index for index, source in enumerate(sources)
            if source.source is LeagueSourceKind.BOOTSTRAP
        )
        body = sources[bootstrap].to_record()["body"]
        body["payload"]["rosters"].pop()
        sources[bootstrap] = LeagueSource("bootstrap", body)
        with self.assertRaisesRegex(ValueError, "roster team coverage"):
            FantasyProsLeagueArtifact(
                task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                2, True, CURRENT_BUNDLE_FINGERPRINT.url,
                CURRENT_BUNDLE_FINGERPRINT.sha256, sources,
            )
        with self.assertRaisesRegex(ValueError, "payload|fields"):
            LeagueSource("analyzer_init", {"payload": {}})
        with self.assertRaisesRegex(ValueError, "fields"):
            LeagueSource("projected_standings", {"payload": {"playoffsTeam": 2}})
        self.assertNotIn("schedule", {source.value for source in LeagueSourceKind})

    def test_bootstrap_dimensions_must_match_artifact_task(self):
        task = league_task()
        sources = list(league_sources())
        bootstrap = next(
            index for index, source in enumerate(sources)
            if source.source is LeagueSourceKind.BOOTSTRAP
        )
        for field, value in (("current_week", 2), ("league.season", 2025)):
            changed = list(sources)
            body = changed[bootstrap].to_record()["body"]
            if field == "current_week":
                body["payload"]["current_week"] = value
            else:
                body["payload"]["league"]["season"] = value
            changed[bootstrap] = LeagueSource("bootstrap", body)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "season and week"):
                FantasyProsLeagueArtifact(
                    task.task_id, task.provider, 2026, 1, task.kind, CAPTURED_AT,
                    2, True, CURRENT_BUNDLE_FINGERPRINT.url,
                    CURRENT_BUNDLE_FINGERPRINT.sha256, changed,
                )

    def test_public_recursive_sanitizer_rejects_mixed_encoded_auth_material(self):
        raw = {
            "safe": 1, "SiGnAtUrE": "x", "JWT": "x", "access-ticket": "x",
            "nonce_value": "x", "message": "go to https%3A%2F%2Fevil.test%2Flogin",
            "nested": [
                "kept", "prefix blob:https://evil.test/id", "wss://evil.test/socket",
                "mailto:user@example.test", "custom-protocol://evil.test/path",
            ],
        }
        self.assertEqual(sanitize_capture_body(raw), {"safe": 1, "nested": ["kept"]})


class ECRArtifactTests(unittest.TestCase):
    def test_weekly_and_ros_are_distinct_and_export_is_not_falsely_exposed(self):
        weekly, ros = ecr_task("weekly"), ecr_task("ros")
        self.assertNotEqual(weekly.task_id, ros.task_id)
        self.assertEqual(
            weekly.to_record()["expert_selection_policy"],
            "fantasypros_latest_ecr_v1",
        )
        with self.assertRaisesRegex(ValueError, "expert_selection_policy"):
            FantasyProsECRTask(
                2026,
                1,
                "weekly",
                "PPR",
                ("RB",),
                (),
                None,
                "https://www.fantasypros.com/nfl/rankings/ppr-rb.php",
                expert_selection_policy="signed_in_preference",
            )
        with self.assertRaises(ValueError):
            FantasyProsECRTask(
                2026, 1, "weekly", "PPR", ("RB",), (), None,
                "https://www.fantasypros.com/nfl/rankings/ppr-rb.php", "export",
            )
        with self.assertRaisesRegex(ValueError, "rankings page"):
            FantasyProsECRTask(
                2026, 1, "weekly", "PPR", ("RB",), (), None,
                "https://www.fantasypros.com/nfl/rankings/experts/",
            )

    def test_ecr_preserves_exact_rank_fields_experts_and_update_evidence(self):
        task = ecr_task("weekly", expected=True)
        artifact = FantasyProsECRArtifact.from_task(
            task, source_scoring="PPR", last_updated_text="9/01", last_updated_at=None,
            captured_at=CAPTURED_AT,
            source_details=ecr_source_details(),
            rankings=(ranking_row(),),
        )
        record = artifact_to_record(artifact, task)
        self.assertEqual(record["expert_ids"], ["1204", "7639"])
        self.assertEqual(record["rankings"][0]["rank_std"], 0.8)
        self.assertEqual(record["last_updated_text"], "9/01")
        self.assertEqual(record["source_scoring"], "PPR")
        self.assertEqual(record["schema_version"], 4)
        self.assertEqual(artifact_from_record(record, task), artifact)


def projection_task(provider="espn"):
    urls = {
        "fantasypros": "https://www.fantasypros.com/nfl/projections/rb.php?week=1&scoring=PPR",
        "espn": "https://fantasy.espn.com/football/players/projections",
        "yahoo": "https://football.fantasysports.yahoo.com/f1/players",
    }
    return PageCaptureTask(
        provider, 2026, 1, "visible_table", urls[provider],
        projection=ProjectionTableSpec("weekly", "PPR", ("RB",)),
    )


def analyzer_task(trade=None, phase=AnalyzerCapturePhase.FULL_PLAYOFFS):
    return PageCaptureTask(
        "fantasypros", 2026, 1, "analyzer_response",
        "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
        phase, trade or AnalyzerTradeSpec(2, (1001, 1002), (2001, 2002)),
    )


def league_task():
    return PageCaptureTask(
        "fantasypros", 2026, 1, "league_source",
        "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php",
    )


def ecr_task(horizon, expected=False):
    return FantasyProsECRTask(
        2026, 1, horizon, "PPR", ("RB",),
        ("1204", "7639") if expected else (), 2 if expected else None,
        "https://www.fantasypros.com/nfl/rankings/ppr-rb.php"
        if horizon == "weekly"
        else "https://www.fantasypros.com/nfl/rankings/ros-ppr-rb.php",
    )


def ranking_row():
    return ECRRankingRow(
        "22968", "Player A", "DET", "RB", 1, 1, 3, 2, 0.8, "RB1",
        {"ECR": "1", "BEST": "1", "WORST": "3", "AVG": "2", "STD DEV": "0.8"},
    )


def playoff_body():
    return {"playoffs": {
        "oddsBefore_team1": 20.0, "oddsAfter_team1": 30.0,
        "oddsBefore_team2": 40.0, "oddsAfter_team2": 35.0,
    }}


def power_body():
    rows = [{"teamId": 1, "score_decimal": 101.2}, {"teamId": 13, "score_decimal": 99.4}]
    return {"ros": {"powerRankings": {"before": rows, "after": list(reversed(rows))}}}


if __name__ == "__main__":
    unittest.main()
from tests.ecr_fixtures import ecr_source_details
