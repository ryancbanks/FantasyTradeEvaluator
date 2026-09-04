import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from trade_snapshot.espn_free_read import (
    EspnFreeReadClient,
    EspnFreeReadError,
    EspnUnauthorizedError,
)


TRANSACTION_FILTER = {
    "transactions": {
        "limit": 1000,
        "sortProcessDate": {"sortPriority": 1, "sortAsc": False},
    }
}


def transaction(transaction_id, process_date, period, *, proposed_date=None):
    return {
        "id": transaction_id,
        "processDate": process_date,
        "proposedDate": process_date if proposed_date is None else proposed_date,
        "scoringPeriodId": period,
    }


class EspnTransactionReadTests(unittest.TestCase):
    def test_reads_preseason_and_current_snapshots_and_merges_exact_duplicates(self):
        shared = transaction("shared", 20, 1)
        snapshot_rows = {
            0: [
                shared,
                transaction("dated", 30, 0),
                transaction("null-b", None, 0, proposed_date=10),
            ],
            None: [
                dict(shared),
                transaction("null-a", None, 1, proposed_date=10),
            ],
        }
        opener = _LeagueOpener(snapshot_rows)

        league, pro_teams = EspnFreeReadClient(opener=opener)(
            2026, "77", lambda: False
        )

        self.assertEqual(
            [row["id"] for row in league["transactions"]],
            ["dated", "shared", "null-a", "null-b"],
        )
        self.assertTrue(pro_teams["display"])
        transaction_requests = _transaction_requests(opener.requests)
        self.assertEqual(
            [_requested_period(request) for request in transaction_requests],
            [0, None],
        )
        for request in transaction_requests:
            with self.subTest(url=request.full_url):
                self.assertEqual(
                    json.loads(_header(request, "x-fantasy-filter")),
                    TRANSACTION_FILTER,
                )
                self.assertEqual(
                    parse_qs(urlsplit(request.full_url).query)["view"],
                    ["mTransactions2"],
                )
                self.assertNotIn("cookie", _headers(request))
                self.assertNotIn("authorization", _headers(request))
        self.assertEqual(len(_pro_team_requests(opener.requests)), 1)
        self.assertIsNone(
            _header(_pro_team_requests(opener.requests)[0], "x-fantasy-filter")
        )

    def test_applies_one_global_limit_after_cross_snapshot_ordering(self):
        snapshot_rows = {
            0: [
                transaction(f"tx-{process_date:04d}", process_date, 0)
                for process_date in range(1, 502)
            ],
            None: [
                transaction(f"tx-{process_date:04d}", process_date, 1)
                for process_date in range(502, 1002)
            ],
        }

        league, _ = EspnFreeReadClient(opener=_LeagueOpener(snapshot_rows))(
            2026, "77", lambda: False
        )

        rows = league["transactions"]
        self.assertEqual(len(rows), 1000)
        self.assertEqual(
            [row["processDate"] for row in rows], list(range(1001, 1, -1))
        )
        self.assertNotIn("tx-0001", {row["id"] for row in rows})

    def test_rejects_conflicts_and_duplicates_within_one_snapshot(self):
        snapshot_rows = {
            0: [transaction("duplicate", 10, 0)],
            None: [transaction("duplicate", 11, 1)],
        }

        with self.assertRaises(EspnFreeReadError):
            EspnFreeReadClient(opener=_LeagueOpener(snapshot_rows))(
                2026, "77", lambda: False
            )
        duplicate = transaction("same-source", 10, 0)
        with self.assertRaises(EspnFreeReadError):
            EspnFreeReadClient(
                opener=_LeagueOpener(
                    {0: [duplicate, dict(duplicate)], None: []}
                )
            )(2026, "77", lambda: False)

        typed_left = transaction("typed-conflict", 10, 0)
        typed_left["skipTransactionCounters"] = False
        typed_right = transaction("typed-conflict", 10, 0)
        typed_right["skipTransactionCounters"] = 0
        with self.assertRaises(EspnFreeReadError):
            EspnFreeReadClient(
                opener=_LeagueOpener({0: [typed_left], None: [typed_right]})
            )(2026, "77", lambda: False)

    def test_boolean_period_does_not_satisfy_period_zero_provenance(self):
        class BooleanPeriodOpener(_LeagueOpener):
            def __call__(self, request, *, timeout):
                response = super().__call__(request, timeout=timeout)
                if _requested_period(request) == 0:
                    payload = json.loads(response.body)
                    payload["scoringPeriodId"] = False
                    return _Response(request.full_url, payload)
                return response

        with self.assertRaises(EspnFreeReadError):
            EspnFreeReadClient(opener=BooleanPeriodOpener({}))(
                2026, "77", lambda: False
            )

    def test_opaque_hyphenated_id_is_not_parsed_as_a_negative_integer(self):
        opaque = transaction("--1", 10, 0)

        league, _ = EspnFreeReadClient(
            opener=_LeagueOpener({0: [opaque], None: [dict(opaque)]})
        )(2026, "77", lambda: False)

        self.assertEqual([row["id"] for row in league["transactions"]], ["--1"])

    def test_numeric_ids_must_fit_the_shared_json_integer_range(self):
        too_large = 1 << 53
        with self.assertRaises(EspnFreeReadError):
            EspnFreeReadClient(
                opener=_LeagueOpener(
                    {0: [transaction(too_large, 10, 0)], None: []}
                )
            )(2026, "77", lambda: False)

        with self.assertRaises(EspnFreeReadError):
            EspnFreeReadClient(
                opener=_LeagueOpener({}, league_id=too_large)
            )(2026, str(too_large), lambda: False)

    def test_requires_both_snapshot_arrays_before_publishing_transactions(self):
        partial, _ = EspnFreeReadClient(
            opener=_LeagueOpener({0: [], None: _OMITTED})
        )(2026, "77", lambda: False)
        complete, _ = EspnFreeReadClient(
            opener=_LeagueOpener({0: [], None: []})
        )(2026, "77", lambda: False)

        self.assertNotIn("transactions", partial)
        self.assertEqual(complete["transactions"], [])

    def test_snapshot_at_requested_limit_is_not_published_as_complete_history(self):
        capped = [
            transaction(f"tx-{value:04d}", value, 0)
            for value in range(1, 1001)
        ]
        for capped_period in (0, None):
            snapshots = {0: [], None: []}
            snapshots[capped_period] = capped
            with self.subTest(capped_period=capped_period):
                league, _ = EspnFreeReadClient(
                    opener=_LeagueOpener(snapshots)
                )(2026, "77", lambda: False)

                self.assertNotIn("transactions", league)

    def test_required_snapshot_failures_are_fatal_and_keep_access_denial_distinct(self):
        for status, error_type in (
            (401, EspnUnauthorizedError),
            (500, EspnFreeReadError),
        ):
            with self.subTest(status=status), self.assertRaises(error_type):
                EspnFreeReadClient(opener=_LeagueOpener({}, failure=(None, status)))(
                    2026, "77", lambda: False
                )

    def test_one_absolute_deadline_covers_opening_and_reading(self):
        with patch(
            "trade_snapshot.espn_free_read.monotonic",
            side_effect=(0.0, 0.0, 21.0),
        ), self.assertRaisesRegex(EspnFreeReadError, "time limit"):
            EspnFreeReadClient(opener=_LeagueOpener({}))(2026, "77", lambda: False)

    def test_response_size_budget_is_cumulative_across_snapshots(self):
        delegate = _LeagueOpener({0: [], None: []})

        def padded_opener(request, *, timeout):
            response = delegate(request, timeout=timeout)
            payload = json.loads(response.body)
            payload["ignored"] = "x" * 600
            padded = _Response(request.full_url, payload)
            self.assertLess(len(padded.body), 1024)
            return padded

        with self.assertRaisesRegex(EspnFreeReadError, "size limit"):
            EspnFreeReadClient(maximum_bytes=1024, opener=padded_opener)(
                2026, "77", lambda: False
            )


_OMITTED = object()


class _LeagueOpener:
    def __init__(self, snapshot_rows, failure=None, *, league_id=77):
        self.snapshot_rows = snapshot_rows
        self.failure = failure
        self.league_id = league_id
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        query = parse_qs(urlsplit(request.full_url).query)
        if "proTeamSchedules_wl" in query.get("view", ()):
            return _Response(
                request.full_url,
                {"display": True, "settings": {"proTeams": []}},
            )
        if "mTransactions2" not in query.get("view", ()):
            return _Response(
                request.full_url,
                {
                    "id": self.league_id,
                    "seasonId": 2026,
                    "scoringPeriodId": 2,
                    "status": {
                        "currentMatchupPeriod": 2,
                        "finalScoringPeriod": 18,
                    },
                    "settings": {},
                    "teams": [],
                    "schedule": [],
                },
            )
        period = _requested_period(request)
        if self.failure is not None and self.failure[0] == period:
            raise HTTPError(request.full_url, self.failure[1], "failure", {}, None)
        rows = self.snapshot_rows.get(period, [])
        payload = {
            "id": self.league_id,
            "seasonId": 2026,
            "scoringPeriodId": 2 if period is None else period,
        }
        if rows is not _OMITTED:
            payload["transactions"] = rows
        return _Response(request.full_url, payload)


class _Response:
    status = 200

    def __init__(self, url, payload):
        self.url = url
        self.body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.body)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, maximum):
        return self.body[:maximum]


def _transaction_requests(requests):
    return [
        request
        for request in requests
        if "mTransactions2"
        in parse_qs(urlsplit(request.full_url).query).get("view", ())
    ]


def _requested_period(request):
    values = parse_qs(urlsplit(request.full_url).query).get("scoringPeriodId")
    return None if values is None else int(values[0])


def _pro_team_requests(requests):
    return [
        request
        for request in requests
        if "proTeamSchedules_wl"
        in parse_qs(urlsplit(request.full_url).query).get("view", ())
    ]


def _headers(request):
    return {key.casefold(): value for key, value in request.header_items()}


def _header(request, name):
    return _headers(request).get(name.casefold())


if __name__ == "__main__":
    unittest.main()
