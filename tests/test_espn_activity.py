from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest

from trade_snapshot.espn_activity import (
    ESPN_TRANSACTION_LIMIT,
    EspnActivityKind,
    EspnActivitySkipCount,
    EspnActivitySkipReason,
    EspnTransactionAssetKind,
    espn_activity_capture,
)


NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def item(
    player_id,
    from_team,
    to_team,
    *,
    from_slot=20,
    to_slot=20,
    item_type=None,
    overall_pick=None,
):
    if item_type is None:
        item_type = (
            "ADD"
            if from_team in (None, 0)
            else "DROP"
            if to_team in (None, 0)
            else "TRADE"
        )
    return {
        "fromLineupSlotId": from_slot,
        "fromTeamId": from_team,
        "isKeeper": False,
        "overallPickNumber": overall_pick,
        "playerId": player_id,
        "toLineupSlotId": to_slot,
        "toTeamId": to_team,
        "type": item_type,
    }


def transaction(
    transaction_id,
    transaction_type,
    items,
    *,
    status="EXECUTED",
    pending=False,
    date=1_788_800_400_000,
    bid=None,
    team_id=1,
):
    return {
        "bidAmount": bid,
        "executionType": "EXECUTE",
        "id": transaction_id,
        "isActingAsTeamOwner": True,
        "isLeagueManager": False,
        "isPending": pending,
        "items": items,
        "memberId": "private-member-id",
        "proposedDate": date,
        "rating": 0,
        "scoringPeriodId": 2,
        "status": status,
        "teamId": team_id,
        "type": transaction_type,
    }


def league_payload(transactions=None):
    return {
        "id": 77,
        "seasonId": 2026,
        "scoringPeriodId": 2,
        "members": [{"id": "private-member-id", "displayName": "Private Person"}],
        "teams": [
            {
                "id": 1,
                "name": "Alpha",
                "owners": ["private-member-id"],
                "roster": {
                    "entries": [
                        {
                            "playerId": 101,
                            "lineupSlotId": 0,
                            "playerPoolEntry": {
                                "player": {"injuryStatus": "ACTIVE"}
                            },
                        },
                        {"playerId": -1, "lineupSlotId": 20},
                    ]
                },
            },
            {
                "id": 2,
                "name": "Bravo",
                "owners": ["another-private-member-id"],
                "roster": {
                    "entries": [
                        {
                            "playerId": 202,
                            "lineupSlotId": 2,
                            "playerPoolEntry": {
                                "player": {"injuryStatus": "questionable"}
                            },
                        }
                    ]
                },
            },
        ],
        "transactions": list(transactions or ()),
    }


class EspnActivityCaptureTests(unittest.TestCase):
    def test_projects_executed_activity_and_current_lineups_without_member_data(self):
        payload = league_payload(
            [
                transaction(
                    10,
                    "TRADE_ACCEPT",
                    [item(101, 1, 2), item(202, 2, 1)],
                ),
                transaction(
                    11,
                    "WAIVER",
                    [item(303, 0, 1), item(-1, 1, 0)],
                    bid=17,
                ),
                transaction(12, "FREEAGENT", [item(404, None, 2)]),
                transaction(
                    13,
                    "TRADE_UPHOLD",
                    [item(505, 0, 1)],
                ),
                transaction(
                    14,
                    "TRADE_ACCEPT",
                    [item(101, 1, 2), item(202, 2, 1)],
                    pending=True,
                ),
                transaction(
                    15,
                    "WAIVER",
                    [item(606, 0, 1)],
                    status="CANCELED",
                ),
                transaction(
                    16,
                    "FREEAGENT",
                    [item(606, 0, 1)],
                    status="FAILED",
                ),
                transaction(
                    17,
                    "TRADE_ACCEPT",
                    [item(101, 1, 2, item_type="MYSTERY")],
                    status=None,
                ),
                transaction(18, "ROSTER", [item(101, 1, 1, to_slot=20)]),
            ]
        )

        capture = espn_activity_capture(payload, captured_at=NOW)

        self.assertEqual(capture.source_league_id, "77")
        self.assertEqual(capture.season, 2026)
        self.assertEqual(capture.scoring_period_id, 2)
        self.assertTrue(capture.transactions_complete)
        self.assertEqual(capture.returned_transaction_count, 9)
        self.assertEqual(capture.transaction_limit, ESPN_TRANSACTION_LIMIT)
        self.assertEqual(
            {row.reason: row.count for row in capture.skipped_transactions},
            {
                EspnActivitySkipReason.NOT_EXECUTED: 3,
                EspnActivitySkipReason.PENDING: 1,
                EspnActivitySkipReason.TRADE_WITHOUT_BILATERAL_ASSETS: 1,
                EspnActivitySkipReason.UNSUPPORTED_ACTIVITY_KIND: 1,
            },
        )
        self.assertEqual(
            [row.kind for row in capture.transactions],
            [
                EspnActivityKind.TRADE,
                EspnActivityKind.WAIVER,
                EspnActivityKind.FREE_AGENT,
            ],
        )
        trade, waiver, free_agent = capture.transactions
        self.assertEqual(trade.source_transaction_id, "10")
        self.assertEqual(
            {(row.from_source_team_id, row.to_source_team_id) for row in trade.items},
            {("1", "2"), ("2", "1")},
        )
        self.assertEqual(waiver.bid_amount, 17.0)
        self.assertEqual(waiver.source_type, "WAIVER")
        waiver_add = next(
            row for row in waiver.items if row.source_player_id == "303"
        )
        self.assertIsNone(waiver_add.from_source_team_id)
        self.assertIsNone(free_agent.items[0].from_source_team_id)
        self.assertEqual(trade.proposed_at.tzinfo, timezone.utc)
        self.assertEqual(
            [(row.source_team_id, [(entry.source_player_id, entry.lineup_slot_id,
                                    entry.injury_status)
                                   for entry in row.entries]) for row in capture.rosters],
            [
                ("1", [("101", 0, "ACTIVE"), ("-1", 20, None)]),
                ("2", [("202", 2, "QUESTIONABLE")]),
            ],
        )
        serialized = repr(capture)
        self.assertNotIn("private-member-id", serialized)
        self.assertNotIn("Private Person", serialized)

    def test_accepts_live_transaction_ids_fields_and_player_sentinels(self):
        free_agent = transaction(
            "9614947a-0c45-bd5",
            "FREEAGENT",
            [
                item(
                    303,
                    0,
                    1,
                    from_slot=-1,
                    to_slot=-1,
                    overall_pick=0,
                ),
                item(
                    -1,
                    1,
                    0,
                    from_slot=-1,
                    to_slot=-1,
                    overall_pick=0,
                ),
            ],
        )
        free_agent.pop("memberId")
        free_agent.update(skipTransactionCounters=False, subOrder=0)
        waiver = transaction(
            "12305a93-4917-48",
            "WAIVER",
            [
                item(
                    404,
                    0,
                    2,
                    from_slot=-1,
                    to_slot=-1,
                    overall_pick=0,
                )
            ],
        )
        waiver.update(
            processDate=1_788_800_400_100,
            relatedTransactionId="b59caa44-ff2d-43bf",
            skipTransactionCounters=False,
            subOrder=5,
        )
        accepted_trade = transaction(
            "a97ddebc-2b48-411",
            "TRADE_ACCEPT",
            [
                item(
                    101,
                    1,
                    2,
                    from_slot=-1,
                    to_slot=-1,
                    overall_pick=0,
                ),
                item(
                    202,
                    2,
                    1,
                    from_slot=-1,
                    to_slot=-1,
                    overall_pick=0,
                ),
            ],
        )
        accepted_trade.update(
            acceptedDate=1_788_800_399_000,
            expirationDate=1_788_900_400_000,
            processDate=1_788_800_400_100,
            relatedTransactionId="d6b20dba-03a1-4621-9e3a-ee927830b736",
            skipTransactionCounters=False,
            subOrder=0,
            teamActions={"1": "APPROVED", "2": "ACCEPTED"},
        )

        capture = espn_activity_capture(
            league_payload([free_agent, waiver, accepted_trade]),
            captured_at=NOW,
        )
        by_id = {row.source_transaction_id: row for row in capture.transactions}

        self.assertEqual(
            set(by_id),
            {free_agent["id"], waiver["id"], accepted_trade["id"]},
        )
        self.assertEqual(by_id[free_agent["id"]].kind, EspnActivityKind.FREE_AGENT)
        self.assertEqual(by_id[waiver["id"]].kind, EspnActivityKind.WAIVER)
        self.assertEqual(by_id[accepted_trade["id"]].kind, EspnActivityKind.TRADE)
        accepted = by_id[accepted_trade["id"]]
        self.assertEqual(
            accepted.accepted_at,
            datetime.fromtimestamp(accepted_trade["acceptedDate"] / 1000, timezone.utc),
        )
        self.assertEqual(
            accepted.processed_at,
            datetime.fromtimestamp(accepted_trade["processDate"] / 1000, timezone.utc),
        )
        self.assertEqual(
            accepted.expires_at,
            datetime.fromtimestamp(accepted_trade["expirationDate"] / 1000, timezone.utc),
        )
        self.assertEqual(accepted.completion_observed_at, NOW)
        self.assertNotEqual(accepted.accepted_at, accepted.proposed_at)
        self.assertNotEqual(accepted.processed_at, accepted.proposed_at)
        self.assertIsNone(by_id[free_agent["id"]].accepted_at)
        self.assertIsNone(by_id[free_agent["id"]].processed_at)
        self.assertIsNone(by_id[free_agent["id"]].expires_at)
        self.assertEqual(by_id[free_agent["id"]].completion_observed_at, NOW)
        without_process = deepcopy(accepted_trade)
        without_process.pop("processDate")
        self.assertNotEqual(
            capture.capture_id,
            espn_activity_capture(
                league_payload([free_agent, waiver, without_process]),
                captured_at=NOW,
            ).capture_id,
        )
        player_items = [
            row
            for event in by_id.values()
            for row in event.items
        ]
        self.assertEqual(
            {row.source_player_id for row in player_items},
            {"-1", "101", "202", "303", "404"},
        )
        self.assertTrue(
            all(
                row.asset_kind is EspnTransactionAssetKind.PLAYER
                for row in player_items
            )
        )
        free_agent_add = next(
            row
            for row in by_id[free_agent["id"]].items
            if row.source_player_id == "303"
        )
        self.assertEqual(
            (free_agent_add.from_source_team_id, free_agent_add.to_source_team_id),
            (None, "1"),
        )
        self.assertEqual(
            (free_agent_add.from_lineup_slot_id, free_agent_add.to_lineup_slot_id),
            (None, None),
        )

    def test_requires_a_real_multi_team_asset_transfer_before_classifying_trade(self):
        payload = league_payload(
            [
                transaction(1, "TRADE_ACCEPT", [item(101, 1, 2)]),
                transaction(2, "TRADE_UPHOLD", [item(303, 0, 1)]),
                transaction(3, "TRADE_ACCEPT", [item(101, 1, 1, to_slot=20)]),
                transaction(
                    4,
                    "TRADE_ACCEPT",
                    [item(0, 1, 2, item_type="DRAFT", overall_pick=5)],
                ),
                transaction(
                    5,
                    "TRADE_ACCEPT",
                    [item(101, 1, 2), item(202, 2, 1), item(303, 0, 1)],
                ),
            ]
        )

        capture = espn_activity_capture(payload, captured_at=NOW)

        self.assertEqual(
            [row.source_transaction_id for row in capture.transactions], ["1", "4"]
        )
        self.assertTrue(
            all(row.kind is EspnActivityKind.TRADE for row in capture.transactions)
        )
        draft_asset = capture.transactions[1].items[0]
        self.assertEqual(
            draft_asset.asset_kind,
            EspnTransactionAssetKind.UNSUPPORTED_NON_PLAYER,
        )
        self.assertEqual(draft_asset.source_player_id, "0")

    def test_reports_one_stable_reason_for_every_omitted_source_row(self):
        first = 1_788_800_300_000
        last = 1_788_800_600_000
        payload = league_payload(
            [
                transaction(1, "WAIVER", [item(303, 0, 1)], date=first + 100_000),
                transaction(
                    2,
                    "TRADE_ACCEPT",
                    [item(101, 1, 2), item(202, 2, 1)],
                    pending=True,
                    date=first,
                ),
                transaction(
                    3,
                    "WAIVER",
                    [item(404, 0, 1)],
                    status="CANCELED",
                    date=first + 50_000,
                ),
                transaction(
                    4,
                    "ROSTER",
                    [item(101, 1, 1)],
                    date=first + 150_000,
                ),
                transaction(
                    5,
                    "FREEAGENT",
                    [item(101, 1, 1)],
                    date=first + 200_000,
                ),
                transaction(
                    6,
                    "TRADE_UPHOLD",
                    [item(505, 0, 1)],
                    date=last,
                ),
            ]
        )

        capture = espn_activity_capture(payload, captured_at=NOW)

        self.assertEqual(capture.returned_transaction_count, 6)
        self.assertEqual(len(capture.transactions), 1)
        self.assertEqual(
            sum(row.count for row in capture.skipped_transactions), 5
        )
        self.assertEqual(
            {row.reason.value: row.count for row in capture.skipped_transactions},
            {
                "no_ownership_changes": 1,
                "not_executed": 1,
                "pending": 1,
                "trade_without_bilateral_assets": 1,
                "unsupported_activity_kind": 1,
            },
        )
        self.assertEqual(
            capture.earliest_returned_proposed_at,
            datetime.fromtimestamp(first / 1000, timezone.utc),
        )
        self.assertEqual(
            capture.latest_returned_proposed_at,
            datetime.fromtimestamp(last / 1000, timezone.utc),
        )
        reordered = deepcopy(payload)
        reordered["transactions"].reverse()
        reordered["teams"].reverse()
        self.assertEqual(
            capture.capture_id,
            espn_activity_capture(reordered, captured_at=NOW).capture_id,
        )

        with self.assertRaisesRegex(ValueError, "skip counts"):
            replace(capture, skipped_transactions=())
        with self.assertRaisesRegex(ValueError, "duplicate skip reason"):
            replace(
                capture,
                skipped_transactions=(
                    EspnActivitySkipCount(EspnActivitySkipReason.PENDING, 2),
                    EspnActivitySkipCount(EspnActivitySkipReason.PENDING, 3),
                ),
            )

    def test_mixed_player_and_draft_pick_trade_retains_the_unsupported_asset(self):
        payload = league_payload(
            [
                transaction(
                    5,
                    "TRADE_ACCEPT",
                    [
                        item(101, 1, 2),
                        item(202, 2, 1),
                        item(0, 2, 1, item_type="DRAFT", overall_pick=7),
                    ],
                )
            ]
        )

        trade = espn_activity_capture(payload, captured_at=NOW).transactions[0]

        self.assertEqual(len(trade.items), 3)
        self.assertEqual(
            [row.asset_kind for row in trade.items].count(
                EspnTransactionAssetKind.UNSUPPORTED_NON_PLAYER
            ),
            1,
        )
        serialized = repr(trade)
        self.assertNotIn("overall_pick", serialized)

    def test_zero_pick_does_not_turn_an_unknown_item_type_into_a_player(self):
        payload = league_payload(
            [
                transaction(
                    "draft-zero",
                    "FREEAGENT",
                    [item(303, 0, 1, item_type="DRAFT", overall_pick=0)],
                )
            ]
        )

        with self.assertRaisesRegex(
            ValueError, "unsupported executed transaction item type"
        ):
            espn_activity_capture(payload, captured_at=NOW)

    def test_marks_the_provider_limit_as_incomplete_and_canonicalizes_order(self):
        transactions = [
            transaction(2, "FREEAGENT", [item(404, 0, 2)], date=1_788_800_500_000),
            transaction(1, "WAIVER", [item(303, 0, 1)], date=1_788_800_400_000),
        ]
        left = espn_activity_capture(
            league_payload(transactions), captured_at=NOW, transaction_limit=2
        )
        reordered = league_payload(list(reversed(transactions)))
        reordered["teams"].reverse()
        right = espn_activity_capture(
            reordered, captured_at=NOW, transaction_limit=2
        )

        self.assertFalse(left.transactions_complete)
        self.assertEqual(left.returned_transaction_count, 2)
        self.assertEqual(len(left.transactions), 2)
        self.assertEqual(left.skipped_transactions, ())
        self.assertEqual(
            [row.source_transaction_id for row in left.transactions], ["1", "2"]
        )
        self.assertEqual(left.capture_id, right.capture_id)

        filtered_at_cap = espn_activity_capture(
            league_payload(
                [
                    transaction(
                        3,
                        "WAIVER",
                        [item(303, 0, 1)],
                        status="CANCELED",
                    ),
                    transaction(
                        4,
                        "TRADE_ACCEPT",
                        [item(101, 1, 2), item(202, 2, 1)],
                        pending=True,
                    ),
                ]
            ),
            captured_at=NOW,
            transaction_limit=2,
        )
        self.assertFalse(filtered_at_cap.transactions_complete)
        self.assertEqual(filtered_at_cap.transactions, ())
        self.assertEqual(
            sum(row.count for row in filtered_at_cap.skipped_transactions), 2
        )

        complete = espn_activity_capture(
            league_payload(transactions[:1]), captured_at=NOW, transaction_limit=2
        )
        self.assertTrue(complete.transactions_complete)

    def test_rejects_source_action_times_after_the_completion_observation(self):
        after_capture = int(NOW.timestamp() * 1000) + 1
        for field in ("acceptedDate", "processDate"):
            row = transaction(1, "WAIVER", [item(303, 0, 1)])
            row[field] = after_capture
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "cannot follow captured_at"
            ):
                espn_activity_capture(league_payload([row]), captured_at=NOW)

    def test_rejects_schema_drift_duplicate_ids_and_invalid_roster_ownership(self):
        valid = transaction(1, "WAIVER", [item(303, 0, 1)])
        mutations = []
        missing = deepcopy(valid)
        missing.pop("type")
        mutations.append(missing)
        extra = deepcopy(valid)
        extra["unexpected"] = True
        mutations.append(extra)
        bad_item = deepcopy(valid)
        bad_item["items"][0]["unexpected"] = True
        mutations.append(bad_item)
        bad_date = deepcopy(valid)
        bad_date["proposedDate"] = "yesterday"
        mutations.append(bad_date)
        bad_optional = deepcopy(valid)
        bad_optional["subOrder"] = "first"
        mutations.append(bad_optional)
        unknown_item_type = deepcopy(valid)
        unknown_item_type["items"][0]["type"] = "MYSTERY"
        mutations.append(unknown_item_type)
        for row in mutations:
            with self.subTest(row=row), self.assertRaises(ValueError):
                espn_activity_capture(league_payload([row]), captured_at=NOW)

        duplicate_transactions = league_payload([valid, deepcopy(valid)])
        with self.assertRaisesRegex(ValueError, "duplicate transaction"):
            espn_activity_capture(duplicate_transactions, captured_at=NOW)

        unknown_team = league_payload(
            [transaction(2, "FREEAGENT", [item(303, 0, 99)])]
        )
        with self.assertRaisesRegex(ValueError, "unknown league team"):
            espn_activity_capture(unknown_team, captured_at=NOW)

        duplicate_player = league_payload()
        duplicate_player["teams"][1]["roster"]["entries"].append(
            {"playerId": 101, "lineupSlotId": 20}
        )
        with self.assertRaisesRegex(ValueError, "multiple current rosters"):
            espn_activity_capture(duplicate_player, captured_at=NOW)

    def test_missing_status_is_retained_as_a_nonexecuted_activity_attempt(self):
        row = transaction(19, "TRADE_ACCEPT", [])
        row.pop("status")

        capture = espn_activity_capture(league_payload([row]), captured_at=NOW)

        self.assertEqual(capture.transactions, ())
        self.assertEqual(
            capture.skipped_transactions,
            (EspnActivitySkipCount(EspnActivitySkipReason.NOT_EXECUTED, 1),),
        )

    def test_packaged_authenticated_reader_requests_transactions_in_existing_read(self):
        source = (
            ROOT / "trade_snapshot" / "browser_extension" / "collectors" / "espn_main.js"
        ).read_text(encoding="utf-8")

        self.assertIn('"mTransactions2"', source)
        self.assertIn('"X-Fantasy-Filter"', source)
        self.assertIn("limit: 1000", source)
        self.assertIn(
            "sortProcessDate: {sortPriority: 1, sortAsc: false}", source
        )
        self.assertIn("for (const expectedPeriod of [0, null])", source)
        self.assertIn("if (merged.completeEvidence)", source)
        self.assertIn("response.body.getReader()", source)
        self.assertIn("const budget = {remaining: options.maximum_bytes}", source)
        self.assertEqual(source.count("await readJson("), 3)


if __name__ == "__main__":
    unittest.main()
