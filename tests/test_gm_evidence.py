from datetime import datetime, timedelta, timezone
import unittest

from trade_snapshot._gm_evidence import build_trade_evidence
from trade_snapshot.league_history import (
    HistoryTimestampBasis,
    HistoryTransaction,
    HistoryTransactionAsset,
    HistoryTransactionKind,
)


START = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)


def completed_trade(index):
    return HistoryTransaction(
        f"trade-{index:02d}",
        START + timedelta(hours=index),
        HistoryTimestampBasis.EXECUTED_AT,
        index + 1,
        HistoryTransactionKind.TRADE,
        (
            HistoryTransactionAsset(0, "player-a", "team-a", "team-b"),
            HistoryTransactionAsset(1, "player-b", "team-b", "team-a"),
        ),
    )


class GeneralManagerEvidenceTests(unittest.TestCase):
    def test_every_completed_trade_is_returned_in_newest_first_order(self):
        trades = tuple(completed_trade(index) for index in range(12))
        arguments = (
            "team-a",
            trades,
            (),
            {},
            {"team-a": "Alpha", "team-b": "Bravo"},
            {"player-a": "Ada", "player-b": "Bert"},
            {
                event.transaction_id: event.recorded_at + timedelta(minutes=5)
                for event in trades
            },
        )

        all_rows = build_trade_evidence(*arguments)

        self.assertEqual(len(all_rows), 12)
        self.assertEqual(
            all_rows[0]["first_observed_completed_at"],
            (trades[-1].recorded_at + timedelta(minutes=5))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        )
        self.assertEqual(
            [row["source_event_at"] for row in all_rows],
            [
                (START + timedelta(hours=index))
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                for index in reversed(range(12))
            ],
        )


if __name__ == "__main__":
    unittest.main()
