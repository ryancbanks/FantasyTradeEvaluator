from pathlib import Path
import unittest


ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "trade_snapshot"
    / "web_assets"
    / "results_workbench.js"
)
ASSET_SOURCE = ASSET_PATH.read_text(encoding="utf-8")


class ResultsWorkbenchStaticContractTests(unittest.TestCase):
    def test_asset_exports_a_pure_module_without_html_sinks(self):
        self.assertIn("window.ResultsWorkbench", ASSET_SOURCE)
        self.assertIn("filterAndSort", ASSET_SOURCE)
        self.assertIn("readControlValues", ASSET_SOURCE)
        for unsafe_source in (
            "document.",
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            "new Function",
        ):
            with self.subTest(unsafe_source=unsafe_source):
                self.assertNotIn(unsafe_source, ASSET_SOURCE)


class ResultsWorkbenchRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is optional for source-only test runs")
        cls._playwright_context = sync_playwright()
        cls._playwright = cls._playwright_context.start()
        cls._browser = None
        for options in (
            {"channel": "chromium", "headless": True},
            {"channel": "msedge", "headless": True},
            {"headless": True},
        ):
            try:
                cls._browser = cls._playwright.chromium.launch(**options)
                break
            except Exception:
                continue
        if cls._browser is None:
            cls._playwright.stop()
            raise unittest.SkipTest(
                "No Playwright-compatible Chromium or Edge browser is installed"
            )

    @classmethod
    def tearDownClass(cls):
        if cls._browser is not None:
            cls._browser.close()
        cls._playwright.stop()

    def evaluate(self, expression):
        page = self._browser.new_page()
        try:
            page.add_script_tag(content=ASSET_SOURCE)
            return page.evaluate(expression)
        finally:
            page.close()

    def test_control_values_are_typed_allowlisted_and_dom_independent(self):
        result = self.evaluate(
            """() => {
              const api = window.ResultsWorkbench;
              const parsed = api.readControlValues({
                onlyAllParticipantsImprove: true,
                minimumPlayoffGainPoints: "2.5",
                sortBy: "weakest_participant_gain"
              });
              const defaults = api.readControlValues({});
              const errors = [];
              for (const controls of [
                {onlyAllParticipantsImprove: "true"},
                {minimumPlayoffGainPoints: -0.1},
                {minimumPlayoffGainPoints: "2 points"},
                {sortBy: "arbitrary_field"},
                {unexpected: true}
              ]) {
                try { api.readControlValues(controls); }
                catch (error) { errors.push(error instanceof TypeError); }
              }
              return {
                exports: Object.keys(api),
                apiFrozen: Object.isFrozen(api),
                sortKeysFrozen: Object.isFrozen(api.SORT_KEYS),
                parsed,
                parsedFrozen: Object.isFrozen(parsed),
                defaults,
                errors
              };
            }"""
        )
        self.assertEqual(
            result["exports"], ["SORT_KEYS", "filterAndSort", "readControlValues"]
        )
        self.assertTrue(result["apiFrozen"])
        self.assertTrue(result["sortKeysFrozen"])
        self.assertTrue(result["parsedFrozen"])
        self.assertEqual(
            result["parsed"],
            {
                "onlyAllParticipantsImprove": True,
                "minimumPlayoffGainPoints": 2.5,
                "sortBy": "weakest_participant_gain",
            },
        )
        self.assertEqual(
            result["defaults"],
            {
                "onlyAllParticipantsImprove": False,
                "minimumPlayoffGainPoints": None,
                "sortBy": "combined_playoff_gain",
            },
        )
        self.assertEqual(result["errors"], [True] * 5)

    def test_two_team_rows_filter_sort_stably_and_keep_original_objects(self):
        result = self.evaluate(
            """() => {
              const rows = [
                {id: "A", other_team: "Alpha", give: ["a1", "a2"], receive: ["b1"],
                 your_playoff_delta: .03, their_playoff_delta: .02,
                 your_power_delta: 1, their_power_delta: 2},
                {id: "B", other_team: "Beta", give: ["a1"], receive: ["b1"],
                 your_playoff_delta: .01, their_playoff_delta: .04,
                 your_power_delta: 4, their_power_delta: 3},
                {id: "C", other_team: "Gamma", give: ["a1"], receive: ["b1", "b2"],
                 your_playoff_delta: -.01, their_playoff_delta: .08,
                 your_power_delta: 1, their_power_delta: 1},
                {id: "D", other_team: "<img src=x onerror=alert(1)>",
                 give: ["a1"], receive: ["b1"],
                 your_playoff_delta: .03, their_playoff_delta: .02,
                 your_power_delta: 1, their_power_delta: 2}
              ];
              for (const row of rows) {
                Object.freeze(row.give); Object.freeze(row.receive); Object.freeze(row);
              }
              const input = Object.freeze(rows.slice());
              const context = {tradeFormat: "two_team"};
              const order = sortBy => ResultsWorkbench.filterAndSort(
                input, context, {sortBy}
              ).map(row => row.id);
              const allImprove = ResultsWorkbench.filterAndSort(input, context, {
                onlyAllParticipantsImprove: true
              });
              const minimumTwo = ResultsWorkbench.filterAndSort(input, context, {
                minimumPlayoffGainPoints: "2"
              });
              const minimumZero = ResultsWorkbench.filterAndSort(input, context, {
                minimumPlayoffGainPoints: 0
              });
              const unfiltered = ResultsWorkbench.filterAndSort(input, context, {});
              const minimalAccepted = ResultsWorkbench.filterAndSort([{
                give: ["a1"], receive: ["b1"],
                your_playoff_delta: 0, their_playoff_delta: 0,
                your_power_delta: 0, their_power_delta: 0
              }], context).length;
              const sparse = new Array(1);
              let sparseRejected = false;
              try { ResultsWorkbench.filterAndSort(sparse, context); }
              catch (error) { sparseRejected = error instanceof TypeError; }
              return {
                combined: order("combined_playoff_gain"),
                mine: order("my_playoff_gain"),
                weakest: order("weakest_participant_gain"),
                power: order("combined_power_gain"),
                fewest: order("fewest_moved_players"),
                allImprove: allImprove.map(row => row.id),
                minimumTwo: minimumTwo.map(row => row.id),
                minimumZero: minimumZero.map(row => row.id),
                minimalAccepted,
                sparseRejected,
                sameReferences: unfiltered.every(row => input.includes(row)),
                inputOrder: input.map(row => row.id),
                unsafeTextUntouched: unfiltered.find(row => row.id === "D").other_team
              };
            }"""
        )
        self.assertEqual(result["combined"], ["C", "A", "B", "D"])
        self.assertEqual(result["mine"], ["A", "D", "B", "C"])
        self.assertEqual(result["weakest"], ["A", "D", "B", "C"])
        self.assertEqual(result["power"], ["B", "A", "D", "C"])
        self.assertEqual(result["fewest"], ["B", "D", "A", "C"])
        self.assertEqual(result["allImprove"], ["A", "B", "D"])
        self.assertEqual(result["minimumTwo"], ["A", "D"])
        self.assertEqual(result["minimumZero"], ["A", "B", "D"])
        self.assertEqual(result["minimalAccepted"], 1)
        self.assertTrue(result["sparseRejected"])
        self.assertTrue(result["sameReferences"])
        self.assertEqual(result["inputOrder"], ["A", "B", "C", "D"])
        self.assertEqual(
            result["unsafeTextUntouched"], "<img src=x onerror=alert(1)>"
        )

    def test_three_team_rows_use_primary_id_and_transfer_player_count(self):
        result = self.evaluate(
            """() => {
              const impact = (team_id, playoff_delta, power_delta) =>
                ({team_id, playoff_delta, power_delta});
              const transfer = (from_team_id, to_team_id, ...playerIds) => ({
                from_team_id, to_team_id,
                players: playerIds.map(player_id => ({player_id, name: player_id}))
              });
              const rows = [
                {id: "X", all_teams_gain: false,
                 team_impacts: [impact("a", .03, 1), impact("me", .01, 1), impact("b", .02, 1)],
                 transfers: [transfer("me", "a", "x1", "x2")]},
                {id: "Y", all_teams_gain: true,
                 team_impacts: [impact("me", .04, 3), impact("a", .01, 2), impact("b", .01, 2)],
                 transfers: [transfer("a", "me", "y1"), transfer("b", "a", "y2", "y3")]},
                {id: "Z", all_teams_gain: true,
                 team_impacts: [impact("a", .05, 1), impact("b", .05, 1), impact("me", -.01, 0)],
                 transfers: [transfer("me", "b", "z1")]}
              ];
              const context = {tradeFormat: "three_team", primaryTeamId: "me"};
              const order = sortBy => ResultsWorkbench.filterAndSort(
                rows, context, {sortBy}
              ).map(row => row.id);
              const allImprove = ResultsWorkbench.filterAndSort(rows, context, {
                onlyAllParticipantsImprove: true
              });
              const minimumOne = ResultsWorkbench.filterAndSort(rows, context, {
                minimumPlayoffGainPoints: 1
              });
              const validationErrors = [];
              for (const action of [
                () => ResultsWorkbench.filterAndSort(rows, {
                  tradeFormat: "three_team", primaryTeamId: "missing"
                }),
                () => ResultsWorkbench.filterAndSort([{...rows[0], team_impacts: [
                  impact("me", .01, 1), impact("me", .02, 1), impact("b", .03, 1)
                ]}], context),
                () => ResultsWorkbench.filterAndSort([{...rows[0], transfers: [
                  transfer("me", "a", "same"), transfer("a", "b", "same")
                ]}], context),
                () => ResultsWorkbench.filterAndSort([{...rows[0], team_impacts: [
                  impact("me", NaN, 1), impact("a", .02, 1), impact("b", .03, 1)
                ]}], context)
              ]) {
                try { action(); }
                catch (error) { validationErrors.push(error instanceof TypeError); }
              }
              return {
                combined: order("combined_playoff_gain"),
                mine: order("my_playoff_gain"),
                weakest: order("weakest_participant_gain"),
                power: order("combined_power_gain"),
                fewest: order("fewest_moved_players"),
                allImprove: allImprove.map(row => row.id),
                minimumOne: minimumOne.map(row => row.id),
                sameReferences: allImprove.every(row => rows.includes(row)),
                validationErrors
              };
            }"""
        )
        self.assertEqual(result["combined"], ["Z", "X", "Y"])
        self.assertEqual(result["mine"], ["Y", "X", "Z"])
        self.assertEqual(result["weakest"], ["X", "Y", "Z"])
        self.assertEqual(result["power"], ["Y", "X", "Z"])
        self.assertEqual(result["fewest"], ["Z", "X", "Y"])
        self.assertEqual(result["allImprove"], ["X", "Y"])
        self.assertEqual(result["minimumOne"], ["X", "Y"])
        self.assertTrue(result["sameReferences"])
        self.assertEqual(result["validationErrors"], [True] * 4)


if __name__ == "__main__":
    unittest.main()
