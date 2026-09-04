"""Evidence-safe reconciliation and observation bounds for captured transactions."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .league_history import HistoryTransaction


def merge_transaction_versions(
    previous: HistoryTransaction,
    current: HistoryTransaction,
) -> HistoryTransaction:
    """Allow only keyed ``None -> canonical player`` evidence enrichment.

    Provider timestamps and asset movements are immutable. A later capture may
    add a previously unavailable source action timestamp or canonical player
    mapping; it may never replace conflicting evidence.
    """

    # Keep this module importable while ``league_history`` attaches its SQLite
    # store at module teardown; runtime types are imported only when called.
    from .league_history import HistoryTransaction

    if not isinstance(previous, HistoryTransaction) or not isinstance(
        current, HistoryTransaction
    ):
        raise ValueError("transaction versions must be HistoryTransaction values")
    if (
        previous.transaction_id != current.transaction_id
        or previous.recorded_at != current.recorded_at
        or previous.timestamp_basis != current.timestamp_basis
        or previous.effective_week != current.effective_week
        or previous.kind != current.kind
        or previous.bid_amount != current.bid_amount
        or len(previous.assets) != len(current.assets)
    ):
        raise ValueError("history contains conflicting immutable transactions")

    optional_times = {}
    changed = False
    for name in ("accepted_at", "processed_at", "expires_at"):
        old = getattr(previous, name)
        new = getattr(current, name)
        if old is not None and new is not None and old != new:
            raise ValueError("history contains conflicting source action timestamps")
        optional_times[name] = old or new
        changed = changed or optional_times[name] != old

    assets = []
    for old, new in zip(previous.assets, current.assets):
        if (
            old.asset_index != new.asset_index
            or old.from_team_id != new.from_team_id
            or old.to_team_id != new.to_team_id
            or old.asset_kind != new.asset_kind
            or old.source_asset_key != new.source_asset_key
        ):
            raise ValueError("history contains conflicting immutable transaction assets")
        if old.source_asset_key is None:
            if old != new:
                raise ValueError(
                    "legacy transaction assets cannot be enriched without a stable source key"
                )
            assets.append(old)
            continue
        if (
            old.canonical_player_id is not None
            and new.canonical_player_id is not None
            and old.canonical_player_id != new.canonical_player_id
        ):
            raise ValueError("source asset key has conflicting canonical player mappings")
        canonical = old.canonical_player_id or new.canonical_player_id
        merged = old if canonical == old.canonical_player_id else replace(
            old, canonical_player_id=canonical
        )
        changed = changed or merged != old
        assets.append(merged)
    return previous if not changed else replace(
        previous,
        assets=tuple(assets),
        **optional_times,
    )


def captured_transaction_evidence(captures):
    """Return captured transaction versions and their conservative executed-by bounds."""

    merged = {}
    first_observed_at = {}
    for capture in sorted(
        captures, key=lambda row: (row.captured_at, row.capture_id)
    ):
        for transaction in capture.transactions:
            transaction_id = transaction.transaction_id
            first_observed_at.setdefault(transaction_id, capture.captured_at)
            previous = merged.get(transaction_id)
            merged[transaction_id] = (
                transaction
                if previous is None
                else merge_transaction_versions(previous, transaction)
            )
    transactions = tuple(
        sorted(
            merged.values(),
            key=lambda row: (row.recorded_at, row.transaction_id),
        )
    )
    return transactions, dict(sorted(first_observed_at.items()))


def transaction_executed_by(transaction, first_observed_at):
    """Return exact execution time or the first conservative capture bound."""

    from .league_history import HistoryTimestampBasis

    if transaction.timestamp_basis is HistoryTimestampBasis.EXECUTED_AT:
        return transaction.recorded_at
    return first_observed_at.get(transaction.transaction_id)


__all__ = (
    "captured_transaction_evidence",
    "merge_transaction_versions",
    "transaction_executed_by",
)
