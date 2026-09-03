from dataclasses import replace
import unittest

from trade_snapshot.draft_history import (
    ActualPlayerWeek,
    ActualWeekStatus,
    DataProvenance,
    DraftPlayerBoard,
    HistoricalCorpus,
    HistoricalSeason,
    PreseasonPlayer,
)


def historical_player(player_id="p1", *, position="RB", name="Player One"):
    return PreseasonPlayer(
        player_id=player_id,
        display_name=name,
        position=position,
        eligible_positions=(position,),
        nfl_team_id="NFL1",
        bye_week=2,
        nfl_experience_years=2,
        rookie=False,
        first_year_on_team=False,
        preseason_features={
            "projected_points": 200,
            "projected_stat.tds": 8,
            "projected_stat.optional": None,
        },
        actual_weeks=(
            ActualPlayerWeek(1, ActualWeekStatus.PLAYED, {"rushing_yards": 80}),
            ActualPlayerWeek(2, ActualWeekStatus.BYE, {}),
            ActualPlayerWeek(3, ActualWeekStatus.INACTIVE, {}),
        ),
    )


def historical_corpus():
    season = HistoricalSeason(
        2025,
        "2025-08-20T12:00:00+00:00",
        "2025-09-04T00:00:00+00:00",
        (1, 2, 3),
        (historical_player(), historical_player("p2", position="WR", name="Player Two")),
    )
    return HistoricalCorpus(
        (season,),
        (
            DataProvenance(
                "fixture",
                "2026-01-01T00:00:00Z",
                "test only",
                "CC0",
                preseason_feature_names=(
                    "projected_points",
                    "projected_stat.optional",
                    "projected_stat.tds",
                ),
                preseason_source_as_of={
                    2025: "2025-08-20T12:00:00+00:00",
                },
            ),
        ),
    )


class HistoricalCorpusTests(unittest.TestCase):
    def test_round_trip_is_content_addressed_and_sorted(self):
        corpus = historical_corpus()
        restored = HistoricalCorpus.from_record(corpus.to_record())
        self.assertEqual(restored.corpus_id, corpus.corpus_id)
        self.assertEqual(corpus.to_record()["preseason_feature_policy_version"], 1)
        self.assertEqual(restored.summary()["seasons"], [2025])
        self.assertEqual(restored.summary()["feature_names"], [
            "projected_points", "projected_stat.optional", "projected_stat.tds"
        ])
        self.assertEqual(restored.summary()["preseason_feature_policy_version"], 1)

    def test_tampering_and_unknown_fields_are_rejected(self):
        record = historical_corpus().to_record()
        record["seasons"][0]["players"][0]["display_name"] = "Changed"
        with self.assertRaisesRegex(ValueError, "corpus_id"):
            HistoricalCorpus.from_record(record)
        record = historical_corpus().to_record()
        record["surprise"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            HistoricalCorpus.from_record(record)
        record = historical_corpus().to_record()
        record["preseason_feature_policy_version"] = 2
        with self.assertRaisesRegex(ValueError, "feature policy version"):
            HistoricalCorpus.from_record(record)

    def test_every_preseason_feature_has_one_explicit_provenance_binding(self):
        corpus = historical_corpus()
        source = corpus.provenance[0]
        names = source.preseason_feature_names
        self.assertEqual(
            corpus.to_record()["provenance"][0]["preseason_feature_names"],
            list(names),
        )
        self.assertEqual(
            corpus.to_record()["provenance"][0]["preseason_source_as_of"],
            {"2025": "2025-08-20T12:00:00+00:00"},
        )
        with self.assertRaisesRegex(ValueError, "no provenance binding"):
            HistoricalCorpus(
                corpus.seasons,
                (replace(source, preseason_feature_names=names[1:]),),
            )
        with self.assertRaisesRegex(ValueError, "unknown preseason feature"):
            HistoricalCorpus(
                corpus.seasons,
                (
                    replace(
                        source,
                        preseason_feature_names=(
                            *names,
                            "projected_stat.unused_metric",
                        ),
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "duplicate provenance"):
            HistoricalCorpus(
                corpus.seasons,
                (
                    source,
                    DataProvenance(
                        "duplicate source",
                        "2026-01-02T00:00:00Z",
                        "invalid duplicate binding",
                        preseason_feature_names=(names[0],),
                        preseason_source_as_of={
                            2025: "2025-08-20T12:00:00+00:00",
                        },
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "not available by"):
            HistoricalCorpus(
                corpus.seasons,
                (
                    replace(
                        source,
                        preseason_source_as_of={
                            2025: "2025-08-21T12:00:00+00:00",
                        },
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "source-as-of seasons"):
            HistoricalCorpus(
                corpus.seasons,
                (
                    replace(
                        source,
                        preseason_source_as_of={
                            2024: "2024-08-20T12:00:00+00:00",
                            2025: "2025-08-20T12:00:00+00:00",
                        },
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "after captured_at"):
            replace(
                source,
                captured_at="2025-08-19T12:00:00+00:00",
            )

    def test_requires_same_season_preseason_data_and_explicit_week_statuses(self):
        with self.assertRaisesRegex(ValueError, "before that season"):
            HistoricalSeason(
                2015, "2016-08-01T00:00:00Z", "2015-09-01T00:00:00Z", (1,),
                (PreseasonPlayer(
                    "p", "P", "RB", ("RB",), "NFL", 2, 0, True, True,
                    {"projected_points": 1}, (ActualPlayerWeek(1, ActualWeekStatus.PLAYED, {"yards": 1}),),
                ),),
            )
        with self.assertRaisesRegex(ValueError, "before that season"):
            HistoricalSeason(
                2015, "2014-08-01T00:00:00Z", "2015-09-01T00:00:00Z", (1,),
                (PreseasonPlayer(
                    "p", "P", "RB", ("RB",), "NFL", 2, 0, True, True,
                    {"projected_points": 1},
                    (ActualPlayerWeek(1, ActualWeekStatus.PLAYED, {"yards": 1}),),
                ),),
            )
        player = historical_player()
        with self.assertRaisesRegex(ValueError, "explicitly cover"):
            HistoricalSeason(
                2025, "2025-08-01T00:00:00Z", "2025-09-01T00:00:00Z", (1, 2),
                (replace(player, actual_weeks=player.actual_weeks[:1]),),
            )

    def test_2020_is_explicitly_excluded(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            HistoricalSeason(
                2020, "2020-08-01T00:00:00Z", "2020-09-01T00:00:00Z", (1,),
                (PreseasonPlayer(
                    "p", "P", "RB", ("RB",), "NFL", 1, 0, True, True,
                    {"projected_points": 1}, (ActualPlayerWeek(1, ActualWeekStatus.PLAYED, {"yards": 1}),),
                ),),
            )

    def test_current_board_has_no_future_outcomes_and_can_use_a_new_season(self):
        players = tuple(
            replace(player, actual_weeks=())
            for player in historical_corpus().seasons[0].players
        )
        board = DraftPlayerBoard(
            2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", players
        )
        self.assertEqual(DraftPlayerBoard.from_record(board.to_record()), board)
        self.assertEqual(board.summary()["player_count"], 2)
        self.assertEqual(board.summary()["espn_mapped_player_count"], 0)
        self.assertEqual(board.to_record()["preseason_feature_policy_version"], 1)
        self.assertEqual(board.to_record()["schema_version"], 1)
        with self.assertRaisesRegex(ValueError, "future actual"):
            DraftPlayerBoard(
                2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
                historical_corpus().seasons[0].players,
            )

    def test_current_board_can_content_address_espn_identity_metadata(self):
        players = tuple(
            replace(player, actual_weeks=())
            for player in historical_corpus().seasons[0].players
        )
        plain = DraftPlayerBoard(
            2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", players
        )
        mapped = DraftPlayerBoard(
            2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", players,
            {"p1": "-16001", "p2": "202"},
        )

        self.assertEqual(plain.to_record()["schema_version"], 1)
        self.assertEqual(mapped.to_record()["schema_version"], 2)
        self.assertNotEqual(mapped.board_id, plain.board_id)
        self.assertEqual(DraftPlayerBoard.from_record(mapped.to_record()), mapped)
        self.assertEqual(mapped.summary()["espn_mapped_player_count"], 2)
        self.assertNotIn("espn_player_ids", mapped.players[0].preseason_features)

        tampered = mapped.to_record()
        tampered["espn_player_ids"]["p1"] = "303"
        with self.assertRaisesRegex(ValueError, "board_id"):
            DraftPlayerBoard.from_record(tampered)

    def test_current_board_rejects_ambiguous_espn_identity_metadata(self):
        players = tuple(
            replace(player, actual_weeks=())
            for player in historical_corpus().seasons[0].players
        )
        for mapping, message in (
            ({"missing": "101"}, "outside"),
            ({"p1": "101", "p2": "101"}, "unique"),
            ({"p1": "not-numeric"}, "non-zero decimal"),
        ):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(ValueError, message):
                DraftPlayerBoard(
                    2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
                    players, mapping,
                )

        noncanonical = DraftPlayerBoard(
            2026, "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", players
        ).to_record()
        noncanonical["schema_version"] = 2
        noncanonical["espn_player_ids"] = {}
        with self.assertRaisesRegex(ValueError, "not canonical"):
            DraftPlayerBoard.from_record(noncanonical)

    def test_feature_policy_accepts_projection_sources_and_rejects_outcome_columns(self):
        accepted = {
            "projected_fantasy_points": 200,
            "projected_stat.pass_yds": 4_000,
            "projected_stat.espn_stat_3": 32,
            "fantasypros.ecr_rank": 12,
            "espn.projected_stat.espn_stat_3": 30,
            "yahoo.adp": 15,
            "ensemble.projected_points": 210,
        }
        self.assertEqual(
            dict(replace(historical_player(), preseason_features=accepted).preseason_features),
            accepted,
        )
        for name in (
            "final_points",
            "postseason_points",
            "future_score",
            "year_end_total",
            "targets",
            "espn.actual_points",
            "espn.unknown_metric",
            "projected_stat",
            "projection_error",
            "projection_accuracy",
            "projected_residual",
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "forbidden|not allowed"
            ):
                replace(historical_player(), preseason_features={name: 1})

    def test_feature_union_limit_applies_to_each_season_and_the_corpus(self):
        def feature_rows(start, stop):
            return {
                f"projected_stat.metric_{index:03d}": float(index)
                for index in range(start, stop)
            }

        first = replace(
            historical_player("wide-1"),
            preseason_features=feature_rows(0, 100),
        )
        second = replace(
            historical_player("wide-2"),
            preseason_features=feature_rows(100, 170),
        )
        with self.assertRaisesRegex(ValueError, "season preseason feature union"):
            HistoricalSeason(
                2025,
                "2025-08-20T12:00:00+00:00",
                "2025-09-04T00:00:00+00:00",
                (1, 2, 3),
                (first, second),
            )
        with self.assertRaisesRegex(ValueError, "board preseason feature union"):
            DraftPlayerBoard(
                2026,
                "2026-08-20T12:00:00+00:00",
                "2026-09-04T00:00:00+00:00",
                (
                    replace(first, actual_weeks=()),
                    replace(second, actual_weeks=()),
                ),
            )

        base = historical_corpus().seasons[0]
        older = HistoricalSeason(
            2024,
            "2024-08-20T12:00:00+00:00",
            "2024-09-04T00:00:00+00:00",
            base.available_weeks,
            (replace(first, preseason_features=feature_rows(0, 100)),),
        )
        newer = replace(
            base,
            players=(replace(second, preseason_features=feature_rows(70, 170)),),
        )
        with self.assertRaisesRegex(ValueError, "corpus preseason feature union"):
            HistoricalCorpus((older, newer), historical_corpus().provenance)


if __name__ == "__main__":
    unittest.main()
