import copy
from dataclasses import FrozenInstanceError
import json
import math
import unittest

from trade_snapshot.draft_brain import (
    DraftBrain,
    FeatureSchema,
    RegressionBaseline,
    crossover_and_mutate,
    initialize_genome,
)


def components():
    schema = FeatureSchema(
        names=("projected_points", "bye_week", "rookie"),
        means=(150.0, 9.0, 0.2),
        scales=(40.0, 3.0, 0.4),
        missing_indicators=("bye_week",),
    )
    baseline = RegressionBaseline(
        schema.feature_schema_id,
        coefficients=(2.0, -0.25, 0.5, -1.5),
        intercept=7.0,
    )
    return schema, baseline


def candidate(points=190.0, bye=12.0, rookie=1.0):
    return {"projected_points": points, "bye_week": bye, "rookie": rookie}


class FeatureSchemaTests(unittest.TestCase):
    def test_encodes_ordered_normalized_values_and_missing_indicators(self):
        schema, _ = components()

        self.assertEqual(schema.encode(candidate()), (1.0, 1.0, 2.0, 0.0))
        self.assertEqual(
            schema.encode({"projected_points": 150, "bye_week": None, "rookie": 0.2}),
            (0.0, 0.0, 0.0, 1.0),
        )
        with self.assertRaisesRegex(ValueError, "projected_points.*required"):
            schema.encode({"bye_week": 8, "rookie": 0})
        with self.assertRaisesRegex(ValueError, "unknown field"):
            schema.encode({**candidate(), "player_name": "Hidden Name"})

    def test_has_strict_content_addressed_round_trip(self):
        schema, _ = components()
        record = json.loads(json.dumps(schema.to_record()))

        self.assertEqual(FeatureSchema.from_record(record), schema)
        changed = copy.deepcopy(record)
        changed["means"][0] += 1
        with self.assertRaisesRegex(ValueError, "does not match"):
            FeatureSchema.from_record(changed)
        extra = copy.deepcopy(record)
        extra["future"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            FeatureSchema.from_record(extra)

    def test_rejects_identity_nonfinite_and_unbounded_schemas(self):
        for forbidden in ("player_name", "canonical_player_id", "nfl_team_id"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(ValueError, "identity feature"):
                    FeatureSchema((forbidden,), (0,), (1,))
        for means, scales in (((math.nan,), (1,)), ((0,), (0,)), ((0,), (math.inf,))):
            with self.subTest(means=means, scales=scales):
                with self.assertRaises(ValueError):
                    FeatureSchema(("value",), means, scales)
        with self.assertRaisesRegex(ValueError, "unique"):
            FeatureSchema(("value", "value"), (0, 0), (1, 1))
        with self.assertRaisesRegex(ValueError, "feature order"):
            FeatureSchema(("a", "b"), (0, 0), (1, 1), ("b", "a"))
        with self.assertRaisesRegex(ValueError, "supported size"):
            FeatureSchema(tuple(f"f{i}" for i in range(257)), (0,) * 257, (1,) * 257)


class RegressionBaselineTests(unittest.TestCase):
    def test_scores_and_round_trips_without_mutable_state(self):
        schema, baseline = components()
        vector = schema.encode(candidate())
        expected = 7 + 2 - 0.25 + 1

        self.assertEqual(baseline.score(vector), expected)
        self.assertEqual(RegressionBaseline.from_record(baseline.to_record()), baseline)
        with self.assertRaises(FrozenInstanceError):
            baseline.intercept = 9

    def test_rejects_tampering_bad_dimensions_and_unsafe_values(self):
        schema, baseline = components()
        changed = copy.deepcopy(baseline.to_record())
        changed["intercept"] += 1
        with self.assertRaisesRegex(ValueError, "does not match"):
            RegressionBaseline.from_record(changed)
        with self.assertRaisesRegex(ValueError, "exactly"):
            baseline.score((1, 2))
        with self.assertRaises(ValueError):
            RegressionBaseline(schema.feature_schema_id, (math.inf,))
        with self.assertRaises(ValueError):
            baseline.score((1, 2, 3, math.nan))


class DraftBrainTests(unittest.TestCase):
    def test_zero_residual_reproduces_baseline_exactly_in_batch(self):
        schema, baseline = components()
        brain = DraftBrain.zero_residual(schema, baseline, "league-config-test")
        candidates = (candidate(), candidate(points=150, bye=None, rookie=0.2))

        expected = tuple(baseline.score(schema.encode(row)) for row in candidates)
        self.assertEqual(brain.score_candidates(candidates), expected)
        self.assertEqual(brain.parameter_count, 16 * 4 + 16 + 16 * 8 + 8 + 8 + 1)

    def test_strict_standalone_round_trip_recomputes_every_identity(self):
        schema, baseline = components()
        brain = initialize_genome(
            schema, baseline, "league-config-test", seed=24, genome_index=3
        )
        record = json.loads(json.dumps(brain.to_record()))

        restored = DraftBrain.from_record(record)
        self.assertEqual(restored, brain)
        self.assertEqual(restored.score(candidate()), brain.score(candidate()))
        changed = copy.deepcopy(record)
        changed["output_bias"] += 1
        with self.assertRaisesRegex(ValueError, "does not match"):
            DraftBrain.from_record(changed)
        nested = copy.deepcopy(record)
        nested["baseline"]["intercept"] += 1
        with self.assertRaisesRegex(ValueError, "does not match"):
            DraftBrain.from_record(nested)

    def test_initialization_and_reproduction_are_deterministic_and_stateless(self):
        schema, baseline = components()
        make = lambda index: initialize_genome(
            schema, baseline, "league-config-test", seed=99, genome_index=index
        )
        first = make(1)
        self.assertEqual(first, make(1))
        self.assertNotEqual(first.brain_id, make(2).brain_id)

        child = crossover_and_mutate(
            first, make(2), seed=88, generation=4, offspring_index=7,
            mutation_rate=1.0, mutation_magnitude=1000,
        )
        repeated = crossover_and_mutate(
            first, make(2), seed=88, generation=4, offspring_index=7,
            mutation_rate=1.0, mutation_magnitude=1000,
        )
        self.assertEqual(child, repeated)
        self.assertNotEqual(child.brain_id, first.brain_id)
        self.assertNotEqual(
            child.brain_id,
            crossover_and_mutate(
                first, make(2), seed=88, generation=4, offspring_index=8,
                mutation_rate=1.0, mutation_magnitude=1000,
            ).brain_id,
        )

    def test_schema_and_config_are_hard_compatibility_boundaries(self):
        schema, baseline = components()
        first = initialize_genome(schema, baseline, "league-a", seed=1, genome_index=1)
        other_config = initialize_genome(schema, baseline, "league-b", seed=1, genome_index=2)
        with self.assertRaisesRegex(ValueError, "share schema, baseline, and league"):
            crossover_and_mutate(first, other_config, seed=1, generation=1, offspring_index=1)

        narrow_schema = FeatureSchema(("only",), (0,), (1,))
        wrong_baseline = RegressionBaseline(narrow_schema.feature_schema_id, (0, 0))
        with self.assertRaisesRegex(ValueError, "coefficient count"):
            DraftBrain.zero_residual(narrow_schema, wrong_baseline, "league-a")

    def test_identity_metadata_cannot_affect_a_decision(self):
        schema, baseline = components()
        brain = initialize_genome(schema, baseline, "league", seed=7, genome_index=0)
        public_a = {"player_id": "one", "player_name": "Alpha", "features": candidate()}
        public_b = {"player_id": "two", "player_name": "Beta", "features": candidate()}

        self.assertEqual(brain.score(public_a["features"]), brain.score(public_b["features"]))
        for identity in ("player_id", "player_name"):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(ValueError, "unknown field"):
                    brain.score({**candidate(), identity: public_a[identity]})

    def test_rejects_malformed_architecture_weights_and_unsafe_options(self):
        schema, baseline = components()
        brain = DraftBrain.zero_residual(schema, baseline, "league")
        record = brain.to_record()
        bad_architecture = copy.deepcopy(record)
        bad_architecture["architecture"] = [4, 32, 8, 1]
        with self.assertRaisesRegex(ValueError, "architecture"):
            DraftBrain.from_record(bad_architecture)
        bad_weights = copy.deepcopy(record)
        bad_weights["input_weights"].append(0)
        with self.assertRaisesRegex(ValueError, "exactly"):
            DraftBrain.from_record(bad_weights)
        bad_weight = copy.deepcopy(record)
        bad_weight["output_bias"] = 10_000_001
        with self.assertRaisesRegex(ValueError, "bounded"):
            DraftBrain.from_record(bad_weight)
        for kwargs in (
            {"seed": -1, "genome_index": 0},
            {"seed": 0, "genome_index": -1},
            {"seed": 0, "genome_index": 0, "magnitude": 10_000_001},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    initialize_genome(schema, baseline, "league", **kwargs)


if __name__ == "__main__":
    unittest.main()
