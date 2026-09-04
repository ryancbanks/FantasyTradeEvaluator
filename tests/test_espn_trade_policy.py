from copy import deepcopy
from datetime import datetime, timezone
import unittest

from tests.test_espn_league import (
    NOW,
    league_payload,
    pro_team_payload,
)
from trade_snapshot.espn_league import espn_host_league_snapshot
from trade_snapshot.espn_trade_policy import acquire_espn_host_transaction_policy
from trade_snapshot.host_transaction_policy import (
    HostAssetField,
    HostPolicyField,
    HostTransactionPolicy,
    HostTransactionPolicyAttempt,
    HostTransactionPolicyReason,
    HostTransactionPolicyStatus,
    TradeDeadlineState,
)


LEAGUE_BINDING_ID = "league_0123456789abcdef0123456789abcdef"
DEADLINE_MS = 1_789_000_000_000


def payload_with_policy():
    payload = league_payload()
    payload["settings"].update(
        tradeSettings={
            "allowOutOfUniverse": False,
            "deadlineDate": DEADLINE_MS,
            "max": -1,
            "revisionHours": 48,
            "vetoVotesRequired": 4,
        }
    )
    payload["settings"]["rosterSettings"].update(
        isUsingUndroppableList=True,
        lineupLocktimeType="INDIVIDUAL_GAME",
        rosterLocktimeType="INDIVIDUAL_GAME",
    )
    payload["status"].update(isActive=True, transactionScoringPeriod=2)
    for index, team in enumerate(payload["teams"]):
        entry = team["roster"]["entries"][0]
        source_id = entry["playerId"]
        entry["pendingTransactionIds"] = (
            ["pending-source-id"] if index == 0 else None
        )
        entry["playerPoolEntry"].update(
            id=source_id,
            lineupLocked=index == 0,
            rosterLocked=False,
            tradeLocked=index == 1,
        )
        entry["playerPoolEntry"]["player"]["droppable"] = index == 0
    return payload


def host_snapshot(payload):
    return espn_host_league_snapshot(
        payload,
        pro_team_payload(),
        captured_at=NOW,
        expected_team_count=2,
    )


def acquire(payload=None, *, snapshot=None, player_ids=None):
    payload = payload_with_policy() if payload is None else payload
    snapshot = host_snapshot(payload) if snapshot is None else snapshot
    player_ids = (
        {"101": "espn:101", "102": "espn:102"}
        if player_ids is None
        else player_ids
    )
    return acquire_espn_host_transaction_policy(
        payload,
        host_snapshot=snapshot,
        league_binding_id=LEAGUE_BINDING_ID,
        canonical_player_ids=player_ids,
    )


class EspnHostTransactionPolicyTests(unittest.TestCase):
    def test_captures_policy_and_current_asset_status_without_private_ids(self):
        attempt = acquire()

        self.assertIs(
            attempt.status,
            HostTransactionPolicyStatus.FIELD_COVERAGE_COMPLETE,
        )
        self.assertIs(
            attempt.reason_code,
            HostTransactionPolicyReason.FIELD_COVERAGE_COMPLETE,
        )
        self.assertEqual(attempt.season, 2026)
        policy = attempt.policy
        self.assertIsNotNone(policy)
        self.assertEqual(
            policy.trade_deadline_at,
            datetime.fromtimestamp(DEADLINE_MS / 1000, timezone.utc),
        )
        self.assertIs(
            policy.deadline_state_at_capture, TradeDeadlineState.BEFORE_DEADLINE
        )
        self.assertEqual(policy.revision_hours_source_value, 48)
        self.assertEqual(policy.veto_votes_required, 4)
        self.assertTrue(policy.undroppable_list_enabled)
        self.assertEqual(
            policy.lineup_locktime_type_source_value, "INDIVIDUAL_GAME"
        )
        self.assertEqual(
            policy.roster_locktime_type_source_value, "INDIVIDUAL_GAME"
        )
        self.assertEqual(
            [row.player_id for row in policy.asset_statuses],
            ["espn:101", "espn:102"],
        )
        first, second = policy.asset_statuses
        self.assertTrue(first.lineup_locked)
        self.assertFalse(first.roster_locked)
        self.assertFalse(first.trade_locked)
        self.assertTrue(first.droppable)
        self.assertEqual(len(first.pending_transaction_reference_ids), 1)
        self.assertTrue(
            first.pending_transaction_reference_ids[0].startswith("pendingtx_")
        )
        self.assertTrue(second.trade_locked)
        self.assertFalse(second.droppable)
        self.assertEqual(second.pending_transaction_reference_ids, ())
        serialized = repr(attempt.to_record())
        self.assertNotIn("pending-source-id", serialized)
        self.assertNotIn("private-member-id", serialized)
        self.assertNotIn("'77'", serialized)

    def test_round_trips_strictly_and_is_order_independent(self):
        payload = payload_with_policy()
        left = acquire(payload)
        reordered = deepcopy(payload)
        reordered["teams"].reverse()
        right = acquire(reordered, snapshot=host_snapshot(payload))

        self.assertEqual(left.attempt_id, right.attempt_id)
        self.assertEqual(left.policy.policy_id, right.policy.policy_id)
        self.assertEqual(
            HostTransactionPolicyAttempt.from_record(left.to_record()), left
        )
        self.assertEqual(
            HostTransactionPolicy.from_record(left.policy.to_record()), left.policy
        )

    def test_deadline_state_can_be_recomputed_after_the_capture(self):
        payload = payload_with_policy()
        payload["settings"]["tradeSettings"]["deadlineDate"] = int(
            NOW.timestamp() * 1000
        )

        policy = acquire(payload).policy

        self.assertIs(
            policy.deadline_state_at_capture, TradeDeadlineState.DEADLINE_REACHED
        )
        payload = payload_with_policy()
        policy = acquire(payload).policy
        self.assertIs(
            policy.deadline_state_at(
                datetime.fromtimestamp(DEADLINE_MS / 1000 + 1, timezone.utc)
            ),
            TradeDeadlineState.DEADLINE_REACHED,
        )

    def test_missing_policy_field_preserves_independently_observed_locks(self):
        payload = payload_with_policy()
        payload["settings"]["tradeSettings"].pop("deadlineDate")

        attempt = acquire(payload, snapshot=host_snapshot(payload))

        self.assertIs(attempt.status, HostTransactionPolicyStatus.PARTIAL)
        self.assertIs(
            attempt.reason_code,
            HostTransactionPolicyReason.SOURCE_FIELDS_PARTIAL,
        )
        self.assertIsNone(attempt.policy.trade_deadline_at)
        self.assertIsNone(attempt.policy.deadline_state_at_capture)
        self.assertIn(
            HostPolicyField.TRADE_DEADLINE,
            attempt.policy.absent_fields,
        )
        self.assertTrue(attempt.policy.asset_statuses[1].trade_locked)
        self.assertEqual(
            HostTransactionPolicyAttempt.from_record(attempt.to_record()), attempt
        )

    def test_missing_optional_trade_settings_preserves_roster_policy_and_locks(self):
        payload = payload_with_policy()
        payload["settings"].pop("tradeSettings")

        attempt = acquire(payload, snapshot=host_snapshot(payload))

        self.assertIs(attempt.status, HostTransactionPolicyStatus.PARTIAL)
        self.assertEqual(
            attempt.policy.absent_fields,
            {
                HostPolicyField.TRADE_DEADLINE,
                HostPolicyField.REVISION_HOURS_SOURCE_VALUE,
                HostPolicyField.VETO_VOTES_REQUIRED,
            },
        )
        self.assertTrue(attempt.policy.undroppable_list_enabled)
        self.assertTrue(attempt.policy.asset_statuses[1].trade_locked)

    def test_unknown_locktime_tokens_are_retained_only_as_raw_source_values(self):
        payload = payload_with_policy()
        payload["settings"]["rosterSettings"]["lineupLocktimeType"] = (
            "FUTURE_ESPN_MODE"
        )

        attempt = acquire(payload, snapshot=host_snapshot(payload))

        self.assertIs(
            attempt.status,
            HostTransactionPolicyStatus.FIELD_COVERAGE_COMPLETE,
        )
        self.assertEqual(
            attempt.policy.lineup_locktime_type_source_value,
            "FUTURE_ESPN_MODE",
        )

    def test_invalid_asset_field_degrades_only_that_field(self):
        payload = payload_with_policy()
        entry = payload["teams"][0]["roster"]["entries"][0]
        entry["playerPoolEntry"]["tradeLocked"] = 0
        entry["pendingTransactionIds"] = "one-id"

        attempt = acquire(payload, snapshot=host_snapshot(payload))

        self.assertIs(attempt.status, HostTransactionPolicyStatus.PARTIAL)
        first = attempt.policy.asset_statuses[0]
        self.assertIsNone(first.trade_locked)
        self.assertIsNone(first.pending_transaction_reference_ids)
        self.assertTrue(first.lineup_locked)
        self.assertIn(HostAssetField.TRADE_LOCKED, first.unsupported_fields)
        self.assertIn(
            HostAssetField.PENDING_TRANSACTION_REFERENCES,
            first.unsupported_fields,
        )

    def test_payload_and_canonical_identity_mismatches_are_explicit(self):
        payload = payload_with_policy()
        snapshot = host_snapshot(payload)
        wrong_league = deepcopy(payload)
        wrong_league["id"] = 88

        for current, player_ids in (
            (wrong_league, {"101": "espn:101", "102": "espn:102"}),
            (payload, {"101": "espn:101"}),
            (payload, {"101": "espn:101", "102": "espn:101"}),
            (
                payload,
                {101: "espn:101", "101": "other:101", "102": "espn:102"},
            ),
        ):
            with self.subTest(current=current, player_ids=player_ids):
                attempt = acquire(
                    current,
                    snapshot=snapshot,
                    player_ids=player_ids,
                )
                self.assertIs(
                    attempt.reason_code,
                    HostTransactionPolicyReason.IDENTITY_MISMATCH,
                )
                self.assertIsNone(attempt.policy)

    def test_conflicting_nested_player_ids_fail_closed(self):
        payload = payload_with_policy()
        snapshot = host_snapshot(payload)
        payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"][
            "id"
        ] = 999

        attempt = acquire(payload, snapshot=snapshot)

        self.assertIs(
            attempt.reason_code,
            HostTransactionPolicyReason.IDENTITY_MISMATCH,
        )

        payload = payload_with_policy()
        snapshot = host_snapshot(payload)
        payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"][
            "player"
        ]["id"] = 999
        nested = acquire(payload, snapshot=snapshot)
        self.assertIs(
            nested.reason_code,
            HostTransactionPolicyReason.IDENTITY_MISMATCH,
        )

    def test_drop_and_lineup_flags_remain_distinct_from_trade_lock(self):
        payload = payload_with_policy()
        pool = payload["teams"][0]["roster"]["entries"][0]["playerPoolEntry"]
        pool.update(lineupLocked=True, rosterLocked=True, tradeLocked=False)
        pool["player"]["droppable"] = False

        status = acquire(
            payload, snapshot=host_snapshot(payload)
        ).policy.asset_statuses[0]

        self.assertTrue(status.lineup_locked)
        self.assertTrue(status.roster_locked)
        self.assertFalse(status.trade_locked)
        self.assertFalse(status.droppable)

    def test_tampered_sidecar_and_attempt_records_are_rejected(self):
        attempt = acquire()
        policy_record = deepcopy(attempt.policy.to_record())
        policy_record["revision_hours_source_value"] = 24
        with self.assertRaisesRegex(ValueError, "policy_id"):
            HostTransactionPolicy.from_record(policy_record)

        attempt_record = deepcopy(attempt.to_record())
        attempt_record["reason_code"] = "source_schema_unsupported"
        with self.assertRaises(ValueError):
            HostTransactionPolicyAttempt.from_record(attempt_record)

        invalid_header = deepcopy(attempt.to_record())
        invalid_header["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "header"):
            HostTransactionPolicyAttempt.from_record(invalid_header)

        missing_coverage = deepcopy(attempt.policy.to_record())
        missing_coverage["trade_deadline_at"] = None
        with self.assertRaisesRegex(ValueError, "field gaps conflict"):
            HostTransactionPolicy.from_record(missing_coverage)

        duplicate_gap = deepcopy(attempt.policy.to_record())
        duplicate_gap["trade_deadline_at"] = None
        duplicate_gap["absent_fields"] = ["trade_deadline", "trade_deadline"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            HostTransactionPolicy.from_record(duplicate_gap)

    def test_duplicate_pending_ids_on_one_asset_are_rejected(self):
        payload = payload_with_policy()
        payload["teams"][0]["roster"]["entries"][0][
            "pendingTransactionIds"
        ] = ["same", "same"]

        attempt = acquire(payload, snapshot=host_snapshot(payload))

        self.assertIs(attempt.status, HostTransactionPolicyStatus.PARTIAL)
        self.assertIsNone(
            attempt.policy.asset_statuses[0].pending_transaction_reference_ids
        )

    def test_pending_references_are_partitioned_by_opaque_league_binding(self):
        left = acquire().policy.asset_statuses[0].pending_transaction_reference_ids[0]
        payload = payload_with_policy()
        right_attempt = acquire_espn_host_transaction_policy(
            payload,
            host_snapshot=host_snapshot(payload),
            league_binding_id="league_fedcba9876543210fedcba9876543210",
            canonical_player_ids={"101": "espn:101", "102": "espn:102"},
        )
        right = right_attempt.policy.asset_statuses[
            0
        ].pending_transaction_reference_ids[0]

        self.assertNotEqual(left, right)

    def test_missing_source_root_is_explicitly_unavailable(self):
        payload = payload_with_policy()

        attempt = acquire_espn_host_transaction_policy(
            None,
            host_snapshot=host_snapshot(payload),
            league_binding_id=LEAGUE_BINDING_ID,
            canonical_player_ids={"101": "espn:101", "102": "espn:102"},
        )

        self.assertIs(attempt.status, HostTransactionPolicyStatus.UNAVAILABLE)
        self.assertIs(
            attempt.reason_code,
            HostTransactionPolicyReason.SOURCE_SCHEMA_UNSUPPORTED,
        )
        self.assertEqual(attempt.season, 2026)
        self.assertEqual(
            HostTransactionPolicyAttempt.from_record(attempt.to_record()), attempt
        )

    def test_zero_deadline_is_not_interpreted_as_a_closed_1970_deadline(self):
        payload = payload_with_policy()
        payload["settings"]["tradeSettings"]["deadlineDate"] = 0

        attempt = acquire(payload, snapshot=host_snapshot(payload))

        self.assertIs(attempt.status, HostTransactionPolicyStatus.PARTIAL)
        self.assertIsNone(attempt.policy.trade_deadline_at)
        self.assertIn(
            HostPolicyField.TRADE_DEADLINE,
            attempt.policy.unsupported_fields,
        )


if __name__ == "__main__":
    unittest.main()
