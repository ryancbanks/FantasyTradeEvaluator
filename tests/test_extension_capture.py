import unittest
from pathlib import Path
import re
from unittest.mock import patch

from tests.capture_fixtures import league_capture_value
from tests.test_browser_capture import (
    PLAYOFF_BODY,
    POWER_BODY,
    ecr_raw,
    ecr_task,
    league_task,
    projection_raw,
    projection_task,
)
from trade_snapshot._extension_capture import ExtensionCaptureBackend
from trade_snapshot.analyzer_contract import CURRENT_BUNDLE_FINGERPRINT
from trade_snapshot.browser_capture import (
    BrowserCaptureOptions,
    BrowserCaptureDependencyError,
)
from trade_snapshot.capture_schema import AnalyzerCapturePhase
from trade_snapshot.extension_bridge import BridgeStateError, V1_CAPABILITIES


EXTENSION_ROOT = Path(__file__).resolve().parents[1] / "trade_snapshot" / "browser_extension"


class _Bridge:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def execute(self, op, payload, timeout, cancelled):
        self.calls.append((op, payload, timeout))
        value = self.values.get(op)
        return value(payload) if callable(value) else value


class ExtensionCaptureTests(unittest.TestCase):
    def open(self, values=None):
        bridge = _Bridge({"session.open": {"opened": True}, **(values or {})})
        session = ExtensionCaptureBackend(bridge).open(
            BrowserCaptureOptions(Path("unused-profile")), 5000, lambda: False
        )
        return bridge, session

    def test_session_uses_only_named_operations_and_validates_common_results(self):
        task = projection_task("espn")
        raw = projection_raw("espn", "Player A", "123", "12.4")
        bridge, session = self.open({
            "session.navigate": {"loaded": True},
            "page.provenance": {
                "protocol": "https:",
                "hostname": "fantasy.espn.com",
                "port": "",
                "pathname": "/football/players/projections",
            },
            "projection.capture": {"segments": [raw]},
            "session.close": {"closed": True},
        })

        session.navigate(task.url, 5000, lambda: False)
        session.assert_page_provenance(task, task.url, 5000, lambda: False)
        data = session.capture_visible_tables(task, 5000, 200, lambda: False)
        self.assertEqual(data.segments_captured, 1)
        self.assertEqual(data.tables[0].rows[1][0].text, "Player A")
        session.close()

        self.assertEqual(
            [row[0] for row in bridge.calls],
            [
                "session.open",
                "session.navigate",
                "page.provenance",
                "projection.capture",
                "session.close",
            ],
        )
        self.assertEqual(bridge.calls[3][1]["request"]["provider"], "espn")
        self.assertEqual(bridge.calls[0][1], {"action_delay_ms": 200})
        self.assertEqual(bridge.calls[1][1]["timeout_ms"], 5000)
        self.assertEqual(bridge.calls[3][1]["timeout_ms"], 5000)

    def test_analyzer_phase_bundle_and_raw_body_are_revalidated_in_python(self):
        bridge, session = self.open({
            "analyzer.begin": None,
            "analyzer.finish": POWER_BODY,
            "analyzer.abort": None,
            "analyzer.bundle": {"url": CURRENT_BUNDLE_FINGERPRINT.url},
            "session.close": None,
        })
        session.begin_analyzer_response_capture(AnalyzerCapturePhase.ORDINARY_POWER)
        self.assertEqual(
            session.finish_analyzer_response_capture(5000, lambda: False), POWER_BODY
        )
        with patch(
            "trade_snapshot.bundle_provenance.fetch_analyzer_bundle_fingerprint",
            return_value=CURRENT_BUNDLE_FINGERPRINT,
        ):
            self.assertEqual(
                session.capture_analyzer_bundle(5000, lambda: False),
                CURRENT_BUNDLE_FINGERPRINT,
            )
        session.abort_analyzer_response_capture()
        session.close()
        self.assertEqual(bridge.calls[1][1], {"phase": "ordinary_power"})

        _, wrong = self.open({
            "analyzer.begin": None,
            "analyzer.finish": PLAYOFF_BODY,
        })
        wrong.begin_analyzer_response_capture(AnalyzerCapturePhase.ORDINARY_POWER)
        with self.assertRaisesRegex(Exception, "invalid"):
            wrong.finish_analyzer_response_capture(5000, lambda: False)

    def test_league_ecr_espn_and_yahoo_results_keep_existing_strict_parsers(self):
        yahoo = projection_task("yahoo")
        settings = "https://football.fantasysports.yahoo.com/2026/f1/12345/settings"
        bridge, session = self.open({
            "league.capture": league_capture_value(),
            "ecr.capture": ecr_raw(expert_count=19),
            "espn.authenticated_json": {
                "league": {"league": True},
                "pro_teams": {"schedule": True},
            },
            "session.navigate": {"loaded": True},
            "page.provenance": {
                "protocol": "https:",
                "hostname": "football.fantasysports.yahoo.com",
                "port": "",
                "pathname": "/2026/f1/12345/settings",
            },
            "yahoo.scoring": {"scoring": "PPR"},
        })

        self.assertEqual(
            session.capture_league_sources(league_task(), 5000, lambda: False).team_count,
            2,
        )
        self.assertEqual(
            session.capture_ecr_rankings(ecr_task(expected=False), 5000, lambda: False).expert_count,
            19,
        )
        self.assertEqual(
            session.read_authenticated_espn_json(2026, "123", 5000, 1024, lambda: False),
            ({"league": True}, {"schedule": True}),
        )
        self.assertEqual(
            session.read_yahoo_scoring(yahoo, settings, 5000, lambda: False), "PPR"
        )

    def test_league_failure_code_becomes_actionable_without_private_data(self):
        _, session = self.open({
            "league.capture": {"error": "analyzer_init_incomplete"},
        })
        with self.assertRaisesRegex(
            Exception, "Trade Analyzer initialization response was not captured"
        ) as raised:
            session.capture_league_sources(league_task(), 5000, lambda: False)
        self.assertNotIn("key", str(raised.exception).casefold())
        self.assertNotIn("url", str(raised.exception).casefold())

    def test_unpaired_bridge_fails_with_an_actionable_extension_message(self):
        class Unpaired:
            def execute(self, *_args):
                raise BridgeStateError("no extension is paired")

        with self.assertRaisesRegex(BrowserCaptureDependencyError, "Connect.*extension"):
            ExtensionCaptureBackend(Unpaired()).open(
                BrowserCaptureOptions(Path("unused-profile")), 5000, lambda: False
            )

    def test_python_bridge_and_packaged_extension_share_the_exact_operation_contract(self):
        source = (EXTENSION_ROOT / "protocol.js").read_text(encoding="utf-8")
        match = re.search(
            r"const OPERATIONS = Object\.freeze\(\[(.*?)\]\);", source, re.DOTALL
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            tuple(re.findall(r'"([a-z_.]+)"', match.group(1))),
            V1_CAPABILITIES,
        )


if __name__ == "__main__":
    unittest.main()
