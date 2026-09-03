from dataclasses import replace
import unittest

from trade_snapshot.draft_config import (
    DraftLeagueConfig,
    DraftStrategy,
    default_slot_eligibility,
)
from trade_snapshot.draft_features import (
    build_baseline_brain,
    candidate_feature_values,
    fit_feature_schema,
    fit_regression_baseline,
    resolve_preseason_projection,
)
from trade_snapshot.draft_history import (
    ActualPlayerWeek,
    ActualWeekStatus,
    DataProvenance,
    HistoricalCorpus,
    HistoricalSeason,
    PreseasonPlayer,
)


def league_config():
    slots = ("QB", "RB")
    return DraftLeagueConfig(
        name="Feature test league",
        team_count=2,
        starting_slots=slots,
        bench_slots=1,
        slot_eligibility=default_slot_eligibility(slots),
        position_limits={},
        scoring_weights={"yards": 0.1, "touchdowns": 6.0},
        regular_season_weeks=(1, 2),
        playoff_team_count=1,
        playoff_weeks=(3,),
        strategy_counts={DraftStrategy.NONE: 2},
    )


def player(
    player_id,
    position,
    projected,
    yards,
    *,
    name=None,
    team="NFL-A",
    extra_features=None,
    experience=2,
):
    features = {
        "projected_points": projected,
        "projected_stat.touchdowns": projected / 30,
    }
    features.update(extra_features or {})
    return PreseasonPlayer(
        player_id=player_id,
        display_name=name or f"Player {player_id}",
        position=position,
        eligible_positions=(position,),
        nfl_team_id=team,
        bye_week=4,
        nfl_experience_years=experience,
        rookie=experience == 0,
        first_year_on_team=experience == 0,
        preseason_features=features,
        actual_weeks=tuple(
            ActualPlayerWeek(
                week,
                ActualWeekStatus.PLAYED,
                {"yards": yards + week, "touchdowns": float(week == 1)},
            )
            for week in (1, 2, 3)
        ),
    )


def season(year, players):
    return HistoricalSeason(
        season=year,
        preseason_as_of=f"{year}-08-20T00:00:00Z",
        season_kickoff_at=f"{year}-09-05T00:00:00Z",
        available_weeks=(1, 2, 3),
        players=tuple(players),
    )


def corpus():
    seasons = (
        season(
            2024,
            (
                player("a", "QB", 300, 220),
                player(
                    "b", "RB", 220, 110,
                    extra_features={"projected_stat.targets": None},
                ),
                player("c", "WR", 190, 90, experience=0),
            ),
        ),
        season(
            2025,
            (
                player(
                    "d", "QB", 320, 240,
                    extra_features={"projected_stat.holdout_metric": 4},
                ),
                player(
                    "e", "RB", 240, 130,
                    extra_features={"projected_stat.targets": 50},
                ),
                player("f", "TE", 160, 70),
            ),
        ),
    )
    return HistoricalCorpus(
        seasons,
        (
            DataProvenance(
                "fixture",
                "2026-01-01T00:00:00Z",
                "feature tests",
                "CC0",
                preseason_feature_names=(
                    "projected_points",
                    "projected_stat.holdout_metric",
                    "projected_stat.targets",
                    "projected_stat.touchdowns",
                ),
                preseason_source_as_of={
                    2024: "2024-08-20T00:00:00Z",
                    2025: "2025-08-20T00:00:00Z",
                },
            ),
        ),
    )


def anonymized_copy(value):
    seasons = []
    for season_index, source in enumerate(value.seasons):
        players = tuple(
            replace(
                row,
                player_id=f"anonymous-{season_index}-{len(source.players) - index}",
                display_name=f"Renamed {index}",
                nfl_team_id=f"CHANGED-{index}",
            )
            for index, row in enumerate(source.players)
        )
        seasons.append(replace(source, players=players))
    return HistoricalCorpus(tuple(seasons), value.provenance)


class DraftFeatureTests(unittest.TestCase):
    def test_candidate_features_cover_preseason_bio_position_and_draft_context(self):
        config = league_config()
        candidate = player("secret-id", "RB", 220, 100, name="Secret Name", team="SECRET")
        values = candidate_feature_values(
            candidate,
            config=config,
            round_number=2,
            overall_pick=3,
            roster_player_positions=("QB",),
            available_position_counts={"QB": 2, "RB": 5, "WR": 8},
            picks_until_next=2,
        )

        self.assertEqual(values["preseason.projected_points"], 220)
        self.assertEqual(values["position.rb"], 1)
        self.assertEqual(values["eligible.rb"], 1)
        self.assertEqual(values["bio.experience_years"], 2)
        self.assertEqual(values["context.roster.qb"], 1)
        self.assertEqual(values["context.candidate_supply"], 5)
        self.assertEqual(values["context.starter_need"], 1)
        self.assertFalse(any("name" in key or key.endswith("_id") for key in values))
        self.assertTrue(all(value is None or isinstance(value, float) for value in values.values()))

    def test_roster_need_uses_full_multi_position_eligibility(self):
        config = replace(
            league_config(),
            starting_slots=("WR",),
            slot_eligibility=default_slot_eligibility(("WR",)),
        )
        values = candidate_feature_values(
            player("candidate", "QB", 220, 100),
            config=config,
            round_number=1,
            overall_pick=1,
            roster_player_positions=("RB",),
            roster_player_eligibilities=(("RB", "WR"),),
            available_position_counts={"QB": 2, "RB": 2, "WR": 2},
            picks_until_next=2,
        )
        self.assertEqual(values["context.unfilled_starters"], 0)

    def test_projection_resolver_prefers_ensemble_then_avoids_provider_summing(self):
        source = player(
            "p", "RB", 200, 100,
            extra_features={
                "ensemble.projected_fantasy_points": 240,
                "espn.projected_fantasy_points": 180,
                "fantasypros.projected_fantasy_points": 210,
                "yahoo.projected_fantasy_points": 150,
                "ensemble.projected_stat.yards": 1_200,
                "espn.projected_stat.yards": 900,
                "yahoo.projected_stat.yards": 700,
            },
        )
        self.assertEqual(
            resolve_preseason_projection(source, "projected_fantasy_points"),
            240,
        )
        self.assertEqual(
            resolve_preseason_projection(source, "projected_stat.yards"),
            1_200,
        )

        providers_only = replace(
            source,
            preseason_features={
                "espn.projected_fantasy_points": 180,
                "fantasypros.projected_fantasy_points": 210,
                "yahoo.projected_fantasy_points": 150,
            },
        )
        self.assertEqual(
            resolve_preseason_projection(
                providers_only, "projected_fantasy_points"
            ),
            180,
        )
        with self.assertRaisesRegex(ValueError, "unnamespaced"):
            resolve_preseason_projection(
                source, "ensemble.projected_fantasy_points"
            )

    def test_schema_uses_only_training_year_feature_names_and_statistics(self):
        history = corpus()
        config = league_config()
        schema = fit_feature_schema(history, config, training_years=(2024,))

        self.assertIn("preseason.projected_points", schema.names)
        self.assertIn("preseason.projected_stat.targets", schema.names)
        self.assertNotIn("preseason.projected_stat.holdout_metric", schema.names)
        self.assertEqual(
            schema.missing_indicators,
            (
                "preseason.projected_points",
                "preseason.projected_stat.targets",
                "preseason.projected_stat.touchdowns",
            ),
        )
        self.assertEqual(schema, fit_feature_schema(history, config, (2024,)))

    def test_names_player_ids_and_team_ids_cannot_change_features_or_model(self):
        history = corpus()
        renamed = anonymized_copy(history)
        config = league_config()
        original = history.seasons[0].players[0]
        changed = next(
            row
            for row in renamed.seasons[0].players
            if row.preseason_features["projected_points"]
            == original.preseason_features["projected_points"]
        )

        kwargs = dict(
            config=config,
            round_number=1,
            overall_pick=1,
            roster_player_positions=(),
            available_position_counts={"QB": 2, "RB": 2, "WR": 1, "TE": 1},
            picks_until_next=2,
        )
        self.assertEqual(
            candidate_feature_values(original, **kwargs),
            candidate_feature_values(changed, **kwargs),
        )
        self.assertEqual(
            build_baseline_brain(history, config, (2024, 2025)),
            build_baseline_brain(renamed, config, (2024, 2025)),
        )

    def test_future_outcomes_cannot_change_schema_but_do_change_regression(self):
        history = corpus()
        config = league_config()
        source_season = history.seasons[1]
        source_player = source_season.players[0]
        changed_weeks = tuple(
            replace(row, stats={"yards": row.stats["yards"] + 1000, "touchdowns": 5})
            for row in source_player.actual_weeks
        )
        changed_player = replace(source_player, actual_weeks=changed_weeks)
        changed_season = replace(
            source_season,
            players=(changed_player, *source_season.players[1:]),
        )
        changed_history = HistoricalCorpus(
            (history.seasons[0], changed_season), history.provenance
        )

        schema = fit_feature_schema(history, config)
        changed_schema = fit_feature_schema(changed_history, config)
        self.assertEqual(changed_schema, schema)
        baseline = fit_regression_baseline(schema, history, config)
        changed_baseline = fit_regression_baseline(changed_schema, changed_history, config)
        self.assertNotEqual(changed_baseline.baseline_id, baseline.baseline_id)

    def test_baseline_is_deterministic_context_neutral_and_zero_residual(self):
        history = corpus()
        config = league_config()
        brain = build_baseline_brain(history, config, (2024, 2025))
        repeated = build_baseline_brain(history, config, (2025, 2024))

        self.assertEqual(brain, repeated)
        self.assertEqual(
            brain, build_baseline_brain(history, config, iter((2024, 2025)))
        )
        for index, name in enumerate(brain.schema.names):
            if name.startswith("context."):
                self.assertEqual(brain.baseline.coefficients[index], 0)
        candidate = history.seasons[0].players[0]
        values = candidate_feature_values(
            candidate,
            config=config,
            round_number=1,
            overall_pick=1,
            roster_player_positions=(),
            available_position_counts={"QB": 2, "RB": 2, "WR": 1, "TE": 1},
            picks_until_next=2,
        )
        encoded = brain.schema.encode(values)
        self.assertEqual(brain.score_vector(encoded), brain.baseline.score(encoded))
        self.assertEqual(brain.league_config_fingerprint, config.config_id)

    def test_rejects_identity_fields_missing_outcomes_and_bad_context(self):
        config = league_config()
        with self.assertRaisesRegex(ValueError, "identity preseason"):
            player(
                "p", "RB", 200, 100, extra_features={"player_id": 123}
            )
        for forbidden in (
            "actual_points",
            "nfl_team.kc",
            "points_scored",
            "final_points",
            "postseason_points",
            "future_score",
            "year_end_total",
        ):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(
                ValueError, "identity preseason|outcome-derived|not allowed"
            ):
                player(
                    "p", "RB", 200, 100, extra_features={forbidden: 1}
                )
        with self.assertRaisesRegex(ValueError, "round_number"):
            candidate_feature_values(
                player("p", "RB", 200, 100),
                config=config,
                round_number=0,
                overall_pick=1,
                roster_player_positions=(),
                available_position_counts={"RB": 1},
                picks_until_next=1,
            )

        history = corpus()
        schema = fit_feature_schema(history, config)
        missing = ActualPlayerWeek(1, ActualWeekStatus.MISSING, {})
        source = history.seasons[0].players[0]
        broken = replace(source, actual_weeks=(missing, *source.actual_weeks[1:]))
        broken_season = replace(history.seasons[0], players=(broken, *history.seasons[0].players[1:]))
        with self.assertRaisesRegex(ValueError, "outcome is missing"):
            fit_regression_baseline(schema, (broken_season, history.seasons[1]), config)


if __name__ == "__main__":
    unittest.main()
