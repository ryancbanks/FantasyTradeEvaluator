import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from trade_snapshot.espn_payload_projection import (
    project_espn_league_payload,
    project_espn_pro_team_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    PROJECT_ROOT
    / "trade_snapshot"
    / "browser_extension"
    / "collectors"
    / "espn_main.js"
).read_text(encoding="utf-8")
ESPN_PAGE = "https://fantasy.espn.com/football/players/projections#fte-scan-v1"
_NO_FAILURE = object()
_OMITTED = object()
_NO_OVERRIDE = object()


def launch_test_browser(playwright, test_case):
    for options in (
        {"channel": "chromium", "headless": True},
        {"channel": "msedge", "headless": True},
        {"headless": True},
    ):
        try:
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    test_case.skipTest("No Playwright-compatible Chromium or Edge browser is installed")


class AuthenticatedEspnScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("Playwright is optional for source-only test runs")
        cls._playwright_context = sync_playwright()
        cls._playwright = cls._playwright_context.start()
        cls._browser = launch_test_browser(cls._playwright, cls)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_browser"):
            cls._browser.close()
        if hasattr(cls, "_playwright"):
            cls._playwright.stop()

    def test_private_member_and_owner_fields_never_cross_extension_boundary(self):
        league = {
            "id": 77,
            "seasonId": 2026,
            "scoringPeriodId": 2,
            "members": [{"displayName": "PRIVATE MEMBER"}],
            "status": {"currentMatchupPeriod": 2, "finalScoringPeriod": 3},
            "settings": {
                "rosterSettings": {"lineupSlotCounts": {"0": 1, "21": 1}},
                "scheduleSettings": {
                    "divisions": [{"id": 1, "name": "One", "size": 1}],
                    "matchupPeriodCount": 2,
                    "playoffTeamCount": 1,
                    "playoffReseed": False,
                    "playoffSeedingRule": "TOTAL_POINTS_SCORED",
                },
                "scoringSettings": {
                    "allowOutOfPositionScoring": False,
                    "homeTeamBonus": 0,
                    "matchupTieRule": "NONE",
                    "matchupTieRuleBy": 0,
                    "playerRankType": "PPR",
                    "playoffHomeTeamBonus": 0,
                    "playoffMatchupTieRule": "NONE",
                    "playoffMatchupTieRuleBy": 0,
                    "scoringType": "H2H_POINTS",
                    "scoringItems": [{
                        "statId": 53,
                        "points": 1,
                        "pointsOverrides": {"1": 0.5},
                        "privateNote": "PRIVATE SCORING NOTE",
                    }],
                },
            },
            "teams": [{
                "id": 1,
                "name": "Alpha",
                "abbrev": "ALP",
                "divisionId": 1,
                "owners": ["PRIVATE OWNER"],
                "record": {"overall": {
                    "wins": 1,
                    "losses": 0,
                    "ties": 0,
                    "pointsFor": 100,
                    "pointsAgainst": 90,
                    "privateRecord": "PRIVATE RECORD",
                }},
                "roster": {"entries": [{
                    "playerId": 101,
                    "lineupSlotId": 0,
                    "privateEntry": "PRIVATE ENTRY",
                    "playerPoolEntry": {"player": {
                        "id": 101,
                        "fullName": "Player One",
                        "defaultPositionId": 1,
                        "proTeamId": 1,
                        "eligibleSlots": [0, 20, 21],
                        "privatePlayer": "PRIVATE PLAYER",
                    }},
                }]},
            }],
            "schedule": [{
                "id": 1,
                "matchupPeriodId": 2,
                "winner": "UNDECIDED",
                "home": {"teamId": 1, "totalPoints": 0, "private": "PRIVATE HOME"},
                "away": {"teamId": 2, "totalPoints": 0, "private": "PRIVATE AWAY"},
            }],
            "privateTopLevel": "PRIVATE TOP LEVEL",
        }
        pro_teams = {
            "settings": {
                "proTeams": [{
                    "id": 1,
                    "abbrev": "ARI",
                    "byeWeek": 8,
                    "location": "Arizona",
                    "name": "Cardinals",
                    "universeId": 1,
                    "proGamesByScoringPeriod": {
                        "1": [{
                            "id": 1001,
                            "scoringPeriodId": 1,
                            "awayProTeamId": 1,
                            "homeProTeamId": 2,
                            "date": 1788000000000,
                            "startTimeTBD": False,
                            "statsOfficial": False,
                            "validForLocking": True,
                            "privateGameData": "PRIVATE GAME",
                        }]
                    },
                    "teamPlayersByPosition": {"1": ["PRIVATE PLAYER ID"]},
                    "privateTeamData": "PRIVATE PRO TEAM",
                }],
                "privateSettings": "PRIVATE SETTINGS",
            },
            "privateTopLevel": "PRIVATE PRO ROOT",
        }
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.route(
            "https://fantasy.espn.com/**",
            lambda route: route.fulfill(content_type="text/html", body="<main></main>"),
        )

        def api(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if "proTeamSchedules_wl" in query.get("view", ()):
                body = pro_teams
            elif "mTransactions2" in query.get("view", ()):
                period_values = query.get("scoringPeriodId")
                body = {
                    "id": 77,
                    "seasonId": 2026,
                    "scoringPeriodId": (
                        2 if period_values is None else int(period_values[0])
                    ),
                }
            else:
                body = league
            route.fulfill(content_type="application/json", body=json.dumps(body))

        page.route("https://lm-api-reads.fantasy.espn.com/**", api)
        page.goto(ESPN_PAGE)
        page.evaluate("globalThis.__FTE_MAIN_HANDLERS = {}")
        page.evaluate(SCRIPT)

        captured = page.evaluate(
            """async (options) =>
              await globalThis.__FTE_MAIN_HANDLERS['espn.authenticated_json'](options)
            """,
            {
                "season": 2026,
                "league_id": "77",
                "timeout_ms": 5000,
                "maximum_bytes": 1024 * 1024,
            },
        )

        self.assertEqual(captured["league"], project_espn_league_payload(league))
        self.assertEqual(
            captured["pro_teams"], project_espn_pro_team_payload(pro_teams)
        )
        serialized = json.dumps(captured, sort_keys=True)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("members", captured["league"])
        self.assertNotIn("owners", captured["league"]["teams"][0])
        self.assertEqual(
            captured["league"]["teams"][0]["roster"]["entries"][0][
                "playerPoolEntry"
            ]["player"]["fullName"],
            "Player One",
        )
        self.assertEqual(
            captured["league"]["settings"]["scoringSettings"]["scoringItems"],
            [{"statId": 53, "points": 1, "pointsOverrides": {"1": 0.5}}],
        )
        projected_pro_team = captured["pro_teams"]["settings"]["proTeams"][0]
        self.assertEqual(projected_pro_team["id"], 1)
        self.assertEqual(projected_pro_team["abbrev"], "ARI")
        self.assertEqual(projected_pro_team["teamPlayersByPosition"], {})
        self.assertEqual(
            projected_pro_team["proGamesByScoringPeriod"]["1"][0]["id"],
            1001,
        )

    def test_transaction_requests_merge_dedupe_sort_and_cap_in_the_browser(self):
        shared = transaction("tx-0500", 500, 1)
        shared["teamActions"] = {"2": "ACCEPT", "1": "PROPOSE"}
        reordered_shared = dict(shared)
        reordered_shared["teamActions"] = {"1": "PROPOSE", "2": "ACCEPT"}
        rows = {
            0: [
                transaction(f"tx-{value:04d}", value, 0)
                for value in range(1, 499)
            ] + [shared],
            None: [
                transaction(f"tx-{value:04d}", value, 1)
                for value in range(499, 998)
                if value != 500
            ] + [
                reordered_shared,
                transaction("dated-top", 2000, 2),
                transaction("null-c", None, 2, proposed_date=10),
                transaction("null-a", None, 2, proposed_date=10),
                transaction("null-b", None, 2, proposed_date=10),
            ],
        }

        captured, requests = self._run_authenticated_reader(rows)

        transactions = captured["league"]["transactions"]
        self.assertEqual(len(transactions), 1000)
        self.assertEqual(transactions[0]["id"], "dated-top")
        self.assertEqual(
            [row["id"] for row in transactions[-2:]], ["null-a", "null-b"]
        )
        self.assertEqual(
            sum(row["id"] == "tx-0500" for row in transactions), 1
        )
        self.assertNotIn("null-c", {row["id"] for row in transactions})

        transaction_requests = [
            (url, headers)
            for url, headers in requests
            if "mTransactions2"
            in parse_qs(urlsplit(url).query).get("view", ())
        ]
        self.assertEqual(
            [
                parse_qs(urlsplit(url).query).get("scoringPeriodId")
                for url, _ in transaction_requests
            ],
            [["0"], None],
        )
        expected_filter = {
            "transactions": {
                "limit": 1000,
                "sortProcessDate": {"sortPriority": 1, "sortAsc": False},
            }
        }
        for url, headers in transaction_requests:
            with self.subTest(url=url):
                self.assertEqual(
                    parse_qs(urlsplit(url).query)["view"], ["mTransactions2"]
                )
                self.assertEqual(
                    json.loads(headers["x-fantasy-filter"]), expected_filter
                )
        pro_team_requests = [
            (url, headers)
            for url, headers in requests
            if "proTeamSchedules_wl" in parse_qs(urlsplit(url).query).get("view", ())
        ]
        self.assertEqual(len(pro_team_requests), 1)
        self.assertNotIn("x-fantasy-filter", pro_team_requests[0][1])

    def test_conflict_duplicate_and_required_snapshot_failure_reject_the_read(self):
        same_source = transaction("same-source", 10, 0)
        typed_left = transaction("typed-conflict", 10, 0)
        typed_left["skipTransactionCounters"] = False
        typed_right = transaction("typed-conflict", 10, 0)
        typed_right["skipTransactionCounters"] = 0
        cases = (
            (
                {
                    0: [transaction("duplicate", 10, 0)],
                    None: [transaction("duplicate", 11, 1)],
                },
                _NO_FAILURE,
            ),
            ({0: [same_source, dict(same_source)], None: []}, _NO_FAILURE),
            ({0: [typed_left], None: [typed_right]}, _NO_FAILURE),
            ({0: [transaction(1 << 53, 10, 0)], None: []}, _NO_FAILURE),
            ({}, None),
        )
        for rows, failure_period in cases:
            with self.subTest(failure_period=failure_period), self.assertRaises(Exception):
                self._run_authenticated_reader(rows, failure_period=failure_period)

        with self.assertRaises(Exception):
            self._run_authenticated_reader({}, period_zero_override=False)
        with self.assertRaises(Exception):
            self._run_authenticated_reader({}, league_id_override=[77])

    def test_partial_transaction_evidence_is_omitted_but_empty_arrays_publish(self):
        partial, _ = self._run_authenticated_reader(
            {0: [], None: _OMITTED}
        )
        complete, _ = self._run_authenticated_reader({0: [], None: []})

        self.assertNotIn("transactions", partial["league"])
        self.assertEqual(complete["league"]["transactions"], [])

    def test_capped_snapshot_is_not_published_as_complete_history_in_browser(self):
        capped = [
            transaction(f"tx-{value:04d}", value, 0)
            for value in range(1, 1001)
        ]
        for capped_period in (0, None):
            snapshots = {0: [], None: []}
            snapshots[capped_period] = capped
            with self.subTest(capped_period=capped_period):
                captured, _ = self._run_authenticated_reader(snapshots)

                self.assertNotIn("transactions", captured["league"])

    def test_response_size_budget_is_shared_across_all_browser_reads(self):
        with self.assertRaises(Exception):
            self._run_authenticated_reader(
                {0: [], None: []},
                maximum_bytes=200,
            )

    def _run_authenticated_reader(
        self,
        snapshot_rows,
        *,
        failure_period=_NO_FAILURE,
        period_zero_override=_NO_OVERRIDE,
        league_id_override=_NO_OVERRIDE,
        maximum_bytes=1024 * 1024,
    ):
        page = self._browser.new_page()
        self.addCleanup(page.close)
        requests = []
        page.route(
            "https://fantasy.espn.com/**",
            lambda route: route.fulfill(content_type="text/html", body="<main></main>"),
        )

        def api(route):
            request = route.request
            requests.append((request.url, request.all_headers()))
            query = parse_qs(urlsplit(request.url).query)
            if "proTeamSchedules_wl" in query.get("view", ()):
                payload = {"display": True, "settings": {"proTeams": []}}
            elif "mTransactions2" not in query.get("view", ()):
                payload = {
                    "id": 77,
                    "seasonId": 2026,
                    "scoringPeriodId": 2,
                    "status": {
                        "currentMatchupPeriod": 2,
                        "finalScoringPeriod": 18,
                    },
                    "settings": {},
                    "teams": [],
                    "schedule": [],
                }
            else:
                period_values = query.get("scoringPeriodId")
                period = None if period_values is None else int(period_values[0])
                if period == failure_period:
                    route.fulfill(status=503, content_type="application/json", body="{}")
                    return
                rows = snapshot_rows.get(period, [])
                payload = {
                    "id": (
                        77
                        if league_id_override is _NO_OVERRIDE
                        else league_id_override
                    ),
                    "seasonId": 2026,
                    "scoringPeriodId": (
                        period_zero_override
                        if period == 0 and period_zero_override is not _NO_OVERRIDE
                        else 2 if period is None else period
                    ),
                }
                if rows is not _OMITTED:
                    payload["transactions"] = rows
            route.fulfill(content_type="application/json", body=json.dumps(payload))

        page.route("https://lm-api-reads.fantasy.espn.com/**", api)
        page.goto(ESPN_PAGE)
        page.evaluate("globalThis.__FTE_MAIN_HANDLERS = {}")
        page.evaluate(SCRIPT)
        captured = page.evaluate(
            """async (options) =>
              await globalThis.__FTE_MAIN_HANDLERS['espn.authenticated_json'](options)
            """,
            {
                "season": 2026,
                "league_id": "77",
                "timeout_ms": 5000,
                "maximum_bytes": maximum_bytes,
            },
        )
        return captured, requests


def transaction(transaction_id, process_date, period, *, proposed_date=None):
    return {
        "id": transaction_id,
        "processDate": process_date,
        "proposedDate": process_date if proposed_date is None else proposed_date,
        "scoringPeriodId": period,
    }


if __name__ == "__main__":
    unittest.main()
