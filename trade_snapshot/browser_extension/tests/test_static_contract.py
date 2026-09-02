from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = [
    "session.open",
    "session.navigate",
    "analyzer.begin",
    "analyzer.finish",
    "analyzer.abort",
    "analyzer.bundle",
    "analyzer.activate_full",
    "page.provenance",
    "projection.capture",
    "ecr.capture",
    "league.capture",
    "espn.authenticated_json",
    "yahoo.scoring",
    "session.wait",
    "session.close",
]
HOST_PERMISSIONS = [
    "http://127.0.0.1/*",
    "http://localhost/*",
    "https://fantasypros.com/*",
    "https://www.fantasypros.com/*",
    "https://fantasy.espn.com/*",
    "https://lm-api-reads.fantasy.espn.com/*",
    "https://football.fantasysports.yahoo.com/*",
]


class ExtensionStaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        cls.javascript = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in ROOT.rglob("*.js")
        }

    def test_manifest_has_minimal_fixed_surface(self) -> None:
        self.assertEqual(self.manifest["manifest_version"], 3)
        self.assertEqual(self.manifest["permissions"], ["storage"])
        self.assertEqual(self.manifest["host_permissions"], HOST_PERMISSIONS)
        self.assertNotIn("<all_urls>", json.dumps(self.manifest))
        main = self.manifest["content_scripts"][1]
        self.assertEqual(main["world"], "MAIN")
        self.assertEqual(main["run_at"], "document_start")
        self.assertEqual(main["js"][0], "single_page_main.js")

    def test_every_manifest_asset_exists(self) -> None:
        references = [
            self.manifest["background"]["service_worker"],
            self.manifest["action"]["default_popup"],
        ]
        for entry in self.manifest["content_scripts"]:
            references.extend(entry["js"])
        missing = sorted(path for path in set(references) if not (ROOT / path).is_file())
        self.assertEqual(missing, [])

    def test_protocol_advertises_exact_operation_list(self) -> None:
        source = self.javascript["protocol.js"]
        match = re.search(
            r"const OPERATIONS = Object\.freeze\(\[(.*?)\]\);", source, re.DOTALL
        )
        self.assertIsNotNone(match)
        advertised = re.findall(r'"([a-z_.]+)"', match.group(1))
        self.assertEqual(advertised, OPERATIONS)
        self.assertIn("validateOperationEnvelope", source)

    def test_dynamic_code_and_credential_reads_are_absent(self) -> None:
        forbidden = {
            "eval": re.compile(r"\beval\s*\("),
            "Function constructor": re.compile(r"\bnew\s+Function\b"),
            "cookie API": re.compile(r"\bchrome\.cookies\b"),
            "document cookie": re.compile(r"\bdocument\.cookie\b"),
            "local storage": re.compile(r"\blocalStorage\b"),
            "session storage": re.compile(r"\bsessionStorage\b"),
            "dynamic script execution": re.compile(r"\bexecuteScript\b"),
        }
        findings = []
        for name, source in self.javascript.items():
            for label, pattern in forbidden.items():
                if pattern.search(source):
                    findings.append(f"{name}: {label}")
        self.assertEqual(findings, [])

    def test_session_token_is_confined_to_trusted_worker_storage(self) -> None:
        worker = self.javascript["service_worker.js"]
        self.assertIn('setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})', worker)
        self.assertIn('headers["X-FTE-Extension-Token"] = token', worker)
        self.assertIn('credentials: "omit"', worker)
        scan_sources = "\n".join(
            source for name, source in self.javascript.items()
            if name.startswith("collectors/") or name in {
                "scan_agent.js", "main_dispatcher.js", "single_page_main.js"
            }
        )
        self.assertNotIn("session_token", scan_sources)
        self.assertNotIn("pair_code", scan_sources)
        self.assertNotIn("X-FTE-Extension-Token", scan_sources)

    def test_worker_lifecycle_edges_are_durable_and_bounded(self) -> None:
        worker = self.javascript["service_worker.js"]
        self.assertIn('const PENDING_KEY = "fte_pending_pair_v1"', worker)
        self.assertIn("expires_at: pair.expiresAt", worker)
        self.assertIn("validStoredPendingShape", worker)
        self.assertIn("storedPending.expires_at <= Date.now()", worker)
        self.assertIn("pendingPair?.appTabId === tabId", worker)
        self.assertIn("chrome.runtime.getPlatformInfo()", worker)
        self.assertIn("response.body.getReader()", worker)
        self.assertNotIn("await response.text()", worker)

    def test_explicit_rejection_is_returned_to_the_requesting_app_tab(self) -> None:
        worker = self.javascript["service_worker.js"]
        self.assertIn("const rejectedPair = pendingPair", worker)
        self.assertIn(
            'sendLocalEvent(rejectedPair.appTabId, "pair.rejected")', worker
        )

    def test_popup_hides_actions_that_do_not_match_the_connection_state(self) -> None:
        stylesheet = (ROOT / "popup" / "popup.css").read_text(encoding="utf-8")
        popup = self.javascript["popup/popup.js"]
        self.assertRegex(stylesheet, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important")
        self.assertIn("pairActions.hidden = !pending", popup)
        self.assertIn("connectedActions.hidden = !", popup)
        self.assertIn('kind: "fte.popup.status"', popup)

    def test_scan_code_is_marker_gated_and_one_tab_is_enforced(self) -> None:
        gated = [
            name for name in self.javascript
            if name.startswith("collectors/") or name in {"scan_agent.js", "main_dispatcher.js"}
        ]
        for name in gated:
            self.assertIn('location.hash !== "#fte-scan-v1"', self.javascript[name], name)
        worker = self.javascript["service_worker.js"]
        self.assertIn("parsed.hash = protocol.SCAN_MARKER", worker)
        self.assertIn("chrome.tabs.onCreated.addListener", worker)
        self.assertIn("chrome.tabs.onRemoved.addListener", worker)
        self.assertIn("window.location.assign", self.javascript["single_page_main.js"])

    def test_packaged_collectors_retain_existing_safety_checks(self) -> None:
        analyzer = self.javascript["collectors/analyzer_main.js"]
        projection = self.javascript["collectors/projection.js"]
        league = self.javascript["collectors/league_main.js"]
        espn = self.javascript["collectors/espn_main.js"]
        self.assertIn("/v2/ajax/myplaybook.php", analyzer)
        self.assertIn("mpbnfl.fantasypros.com", analyzer)
        self.assertIn("/api/tradeAnalyzer", analyzer)
        self.assertIn("team1drops", analyzer)
        self.assertIn("response.redirected", analyzer)
        self.assertIn("response_too_large", analyzer)
        self.assertIn("privateColumn", projection)
        self.assertIn("Yahoo All Players", projection)
        self.assertIn("projected_standings", league)
        self.assertIn("roster_positions", league)
        self.assertIn("proTeamSchedules_wl", espn)
        self.assertIn('credentials: "include"', espn)

    def test_no_patch_directives_were_embedded(self) -> None:
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".css", ".html", ".js", ".json", ".md", ".py"
            }:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"^\*\*\* (?:Add|Delete|Update) File", source, re.MULTILINE),
                path,
            )


if __name__ == "__main__":
    unittest.main()
