from dataclasses import replace
import copy
import unittest

from tests.test_weekly_assembly import league_artifact
from trade_snapshot.fantasypros_benchmark import FantasyProsLeagueBenchmark


class FantasyProsLeagueBenchmarkTests(unittest.TestCase):
    def test_maps_team_ids_and_round_trips_strictly(self):
        artifact = league_artifact()
        benchmark = FantasyProsLeagueBenchmark.from_capture(
            artifact,
            "snapshot-1",
            {"host-a": "1", "host-b": "2"},
        )

        self.assertEqual(
            {row.team_id for row in benchmark.teams}, {"host-a", "host-b"}
        )
        self.assertEqual(benchmark.teams[0].playoff_probability, 0.6)
        self.assertEqual(
            FantasyProsLeagueBenchmark.from_record(benchmark.to_record()),
            benchmark,
        )

    def test_rejects_incomplete_or_ambiguous_team_mapping(self):
        artifact = league_artifact()
        for mapping in (
            {"host-a": "1"},
            {"host-a": "1", "host-b": "1"},
        ):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                FantasyProsLeagueBenchmark.from_capture(
                    artifact, "snapshot-1", mapping
                )

    def test_rejects_tampering_and_boolean_schema_version(self):
        benchmark = FantasyProsLeagueBenchmark.from_capture(
            league_artifact(),
            "snapshot-1",
            {"host-a": "1", "host-b": "2"},
        )
        tampered = copy.deepcopy(benchmark.to_record())
        tampered["teams"][0]["playoff_probability"] = 0.9
        with self.assertRaisesRegex(ValueError, "does not match benchmark_id"):
            FantasyProsLeagueBenchmark.from_record(tampered)

        invalid_header = copy.deepcopy(benchmark.to_record())
        invalid_header["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "header is invalid"):
            FantasyProsLeagueBenchmark.from_record(invalid_header)

        with self.assertRaisesRegex(ValueError, "probabilities"):
            replace(benchmark.teams[0], playoff_probability=1.1)


if __name__ == "__main__":
    unittest.main()
