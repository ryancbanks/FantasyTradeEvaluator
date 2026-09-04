import copy
from dataclasses import FrozenInstanceError
import math
import unittest

from trade_snapshot.analyzer_contract import (
    CURRENT_BUNDLE_FINGERPRINT,
    AnalyzerContractError,
    AnalyzerPeriod,
    AnalyzerTradeRequest,
    BundleFingerprint,
    PlayoffOddsChange,
    PowerRankingChange,
    observation_from_record,
    observation_to_record,
    parse_analyzer_observation,
    parse_playoff_response,
    parse_power_response,
)


def request(period=AnalyzerPeriod.ROS):
    return AnalyzerTradeRequest(
        period=period,
        team1_id=1,
        team2_id="2",
        team1_gets=(1001, "1002"),
        team2_gets=(2001,),
        team1_adds=(3001,),
        team2_drops=(3002,),
    )


def power_response(period_key="ros"):
    return {
        period_key: {
            "powerRankings": {
                "before": [
                    {"teamId": 99, "score_decimal": 88.8},
                    {"teamId": 1, "score_decimal": 1.24},
                    {"teamId": 2, "score_decimal": 100.04},
                ],
                "after": [
                    {"teamId": "2", "score_decimal": 99.94},
                    {"teamId": "1", "score_decimal": 1.25},
                    {"teamId": 99, "score_decimal": 90.0},
                ],
            }
        }
    }


def full_response():
    return {
        "playoffs": {
            "oddsBefore_team1": 20.516000000000002,
            "oddsAfter_team1": 51.7,
            "oddsBefore_team2": 34.812,
            "oddsAfter_team2": 9.9,
        }
    }


class AnalyzerTradeRequestTests(unittest.TestCase):
    def test_is_immutable_and_maps_semantic_periods_to_response_fields(self):
        expected = {
            AnalyzerPeriod.ROS: "ros",
            AnalyzerPeriod.PRE: "ros",
            AnalyzerPeriod.DYN: "dynasty",
        }
        for period, response_key in expected.items():
            with self.subTest(period=period):
                trade = request(period.value)
                self.assertIs(trade.period, period)
                self.assertEqual(trade.response_period_key, response_key)
                self.assertEqual(trade.team1_id, "1")
                self.assertEqual(trade.team1_gets, ("1001", "1002"))

        with self.assertRaises(FrozenInstanceError):
            request().team1_id = "changed"

    def test_rejects_missing_teams_empty_packages_and_ambiguous_asset_ids(self):
        valid = {
            "period": "ros",
            "team1_id": "1",
            "team2_id": "2",
            "team1_gets": ("a",),
            "team2_gets": ("b",),
        }
        cases = (
            ({"team1_id": ""}, "team1_id"),
            ({"team2_id": "1"}, "different teams"),
            ({"team1_gets": ()}, "trade packages"),
            ({"team2_gets": (" ",)}, "non-empty"),
            ({"team1_adds": ("a",)}, "more than once"),
            ({"team2_drops": ("https://example.test/request",)}, "URL"),
            ({"team2_drops": ("//example.test/request",)}, "URL"),
            ({"team2_drops": ("example.test/request",)}, "URL"),
            ({"team2_drops": ("key=secret",)}, "URL"),
            ({"team2_drops": ("sessionid=secret",)}, "URL"),
            ({"team2_drops": ("cookie: secret",)}, "URL"),
            ({"period": "weekly"}, "period"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(AnalyzerContractError, message):
                    AnalyzerTradeRequest(**{**valid, **changes})

    def test_local_change_records_accept_namespaced_team_ids_without_weakening_requests(self):
        self.assertEqual(
            PowerRankingChange("espn:team:6", 50, 51).team_id,
            "espn:team:6",
        )
        self.assertEqual(
            PlayoffOddsChange("espn:team:6", 40, 45).team_id,
            "espn:team:6",
        )
        with self.assertRaisesRegex(AnalyzerContractError, "portable provider-ID"):
            AnalyzerTradeRequest(
                period="ros",
                team1_id="espn:team:6",
                team2_id="2",
                team1_gets=("a",),
                team2_gets=("b",),
            )


class ResponseParserTests(unittest.TestCase):
    def test_preserves_raw_power_and_matches_fantasypros_one_decimal_logic(self):
        parsed = parse_power_response(request(), power_response())

        self.assertEqual(parsed.semantic_period, AnalyzerPeriod.ROS)
        self.assertEqual(parsed.response_period_key, "ros")
        self.assertEqual(parsed.team1.raw_before, 1.24)
        self.assertEqual(parsed.team1.raw_after, 1.25)
        self.assertEqual(parsed.team1.display_before_text, "1.2")
        self.assertEqual(parsed.team1.display_after_text, "1.3")
        self.assertEqual(parsed.team1.display_delta_text, "0.1")
        self.assertAlmostEqual(parsed.team1.display_delta, 0.1)
        self.assertEqual(parsed.team2.display_before_text, "100.0")
        self.assertEqual(parsed.team2.display_after_text, "99.9")
        self.assertEqual(parsed.team2.display_delta_text, "-0.1")

        negative = power_response()
        negative["ros"]["powerRankings"]["before"][1]["score_decimal"] = -0.04
        negative["ros"]["powerRankings"]["after"][1]["score_decimal"] = -1.25
        negative_change = parse_power_response(request(), negative).team1
        self.assertEqual(negative_change.display_before_text, "-0.0")
        self.assertEqual(negative_change.display_after_text, "-1.3")
        self.assertEqual(negative_change.display_delta_text, "-1.3")

    def test_uses_the_period_response_mapping_and_rejects_the_wrong_period(self):
        preseason = parse_power_response(request("pre"), power_response("ros"))
        dynasty = parse_power_response(request("dyn"), power_response("dynasty"))
        self.assertEqual(preseason.response_period_key, "ros")
        self.assertEqual(dynasty.response_period_key, "dynasty")

        with self.assertRaisesRegex(AnalyzerContractError, "expected response period"):
            parse_power_response(request("dyn"), power_response("ros"))

    def test_rejects_missing_duplicate_and_nonfinite_power_rows(self):
        cases = []

        missing_team = power_response()
        missing_team["ros"]["powerRankings"]["after"] = [
            {"teamId": 1, "score_decimal": 2.0}
        ]
        cases.append((missing_team, "team 2"))

        missing_score = power_response()
        del missing_score["ros"]["powerRankings"]["before"][1]["score_decimal"]
        cases.append((missing_score, "score_decimal"))

        duplicate_team = power_response()
        duplicate_team["ros"]["powerRankings"]["before"].append(
            {"teamId": "1", "score_decimal": 3.0}
        )
        cases.append((duplicate_team, "duplicate"))

        for value in (math.nan, math.inf, -math.inf, True, "100"):
            nonfinite = power_response()
            nonfinite["ros"]["powerRankings"]["after"][1]["score_decimal"] = value
            cases.append((nonfinite, "finite number"))

        for response, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AnalyzerContractError, message):
                    parse_power_response(request(), response)

    def test_parses_both_teams_playoff_odds_from_one_full_response(self):
        parsed = parse_playoff_response(request(), full_response())

        self.assertEqual(parsed.team1.raw_before, 20.516000000000002)
        self.assertEqual(parsed.team1.raw_after, 51.7)
        self.assertEqual(parsed.team1.display_before_text, "20.5")
        self.assertEqual(parsed.team1.display_after_text, "51.7")
        self.assertEqual(parsed.team1.display_delta_text, "31.2")
        self.assertEqual(parsed.team2.display_delta_text, "-24.9")

    def test_rejects_missing_or_out_of_range_playoff_odds(self):
        missing = full_response()
        del missing["playoffs"]["oddsAfter_team2"]
        with self.assertRaisesRegex(AnalyzerContractError, "oddsAfter_team2"):
            parse_playoff_response(request(), missing)

        for value in (-0.1, 100.1, math.nan, math.inf, True, "20.5"):
            response = full_response()
            response["playoffs"]["oddsBefore_team1"] = value
            with self.subTest(value=value):
                with self.assertRaises(AnalyzerContractError):
                    parse_playoff_response(request(), response)


class SanitizedObservationRecordTests(unittest.TestCase):
    def test_round_trips_strict_json_safe_record_with_bundle_fingerprint(self):
        observation = parse_analyzer_observation(
            request(),
            power_response(),
            full_response(),
        )

        record = observation_to_record(observation)

        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(
            record["bundle"],
            {
                "url": CURRENT_BUNDLE_FINGERPRINT.url,
                "sha256": CURRENT_BUNDLE_FINGERPRINT.sha256,
            },
        )
        self.assertNotIn("key", record["request"])
        self.assertNotIn("request_url", record)
        self.assertEqual(observation_from_record(record), observation)

    def test_rejects_secrets_request_urls_unknown_fields_and_tampered_derivations(self):
        record = observation_to_record(
            parse_analyzer_observation(request(), power_response(), full_response())
        )
        mutations = []

        with_secret = copy.deepcopy(record)
        with_secret["request"]["headers"] = {"Authorization": "Bearer secret"}
        mutations.append((with_secret, "secret-like"))

        with_request_url = copy.deepcopy(record)
        with_request_url["request_url"] = "https://mpbnfl.fantasypros.com/api/tradeAnalyzer?key=secret"
        mutations.append((with_request_url, "request URL"))

        with_unknown = copy.deepcopy(record)
        with_unknown["power"]["unexpected"] = True
        mutations.append((with_unknown, "fields"))

        with_bool_version = copy.deepcopy(record)
        with_bool_version["schema_version"] = True
        mutations.append((with_bool_version, "schema_version"))

        with_bad_display = copy.deepcopy(record)
        with_bad_display["power"]["team1"]["display_delta"] = "9.9"
        mutations.append((with_bad_display, "derived display"))

        with_bad_team = copy.deepcopy(record)
        with_bad_team["power"]["team1"]["team_id"] = "999"
        mutations.append((with_bad_team, "teams do not match"))

        with_bad_period = copy.deepcopy(record)
        with_bad_period["power"]["response_period"] = "dynasty"
        mutations.append((with_bad_period, "wrong response period"))

        for candidate, message in mutations:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AnalyzerContractError, message):
                    observation_from_record(candidate)

    def test_bundle_metadata_accepts_only_a_public_bundle_fingerprint(self):
        self.assertEqual(
            BundleFingerprint(
                CURRENT_BUNDLE_FINGERPRINT.url,
                CURRENT_BUNDLE_FINGERPRINT.sha256.upper(),
            ),
            CURRENT_BUNDLE_FINGERPRINT,
        )
        with self.assertRaisesRegex(AnalyzerContractError, "public FantasyPros bundle"):
            BundleFingerprint(
                "https://mpbnfl.fantasypros.com/api/tradeAnalyzer?key=secret",
                "0" * 64,
            )
        with self.assertRaisesRegex(AnalyzerContractError, "public FantasyPros bundle"):
            BundleFingerprint(
                "https://cdn.fantasypros.com:invalid/assets/js/bundle.js",
                "0" * 64,
            )

    def test_record_must_contain_only_json_values(self):
        record = observation_to_record(
            parse_analyzer_observation(request(), power_response(), full_response())
        )
        record["power"]["team1"]["raw_before"] = {1, 2}
        with self.assertRaisesRegex(AnalyzerContractError, "JSON-safe"):
            observation_from_record(record)

        record = observation_to_record(
            parse_analyzer_observation(request(), power_response(), full_response())
        )
        record["request"]["team1_gets"] = tuple(record["request"]["team1_gets"])
        with self.assertRaisesRegex(AnalyzerContractError, "JSON-safe"):
            observation_from_record(record)


if __name__ == "__main__":
    unittest.main()
