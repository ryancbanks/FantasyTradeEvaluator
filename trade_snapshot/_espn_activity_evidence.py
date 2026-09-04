"""Immutable normalized evidence from one bounded ESPN activity read."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from numbers import Real

from ._scenario_random import content_id


ESPN_TRANSACTION_LIMIT = 1_000
_MAXIMUM_SAFE_JSON_INTEGER = (1 << 53) - 1

_SOURCE_TYPES = {
    "TRADE_ACCEPT": "trade",
    "TRADE_UPHOLD": "trade",
    "WAIVER": "waiver",
    "FREEAGENT": "free_agent",
}
_UNSUPPORTED_SOURCE_ASSET_ID = "0"


class EspnActivityKind(str, Enum):
    TRADE = "trade"
    WAIVER = "waiver"
    FREE_AGENT = "free_agent"


class EspnTransactionAssetKind(str, Enum):
    PLAYER = "player"
    UNSUPPORTED_NON_PLAYER = "unsupported_non_player"


class EspnActivitySkipReason(str, Enum):
    PENDING = "pending"
    NOT_EXECUTED = "not_executed"
    UNSUPPORTED_ACTIVITY_KIND = "unsupported_activity_kind"
    NO_OWNERSHIP_CHANGES = "no_ownership_changes"
    TRADE_WITHOUT_BILATERAL_ASSETS = "trade_without_bilateral_assets"


@dataclass(frozen=True, slots=True)
class EspnActivitySkipCount:
    reason: EspnActivitySkipReason
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.reason, EspnActivitySkipReason):
            raise ValueError("reason must be an EspnActivitySkipReason")
        _integer("skip count", self.count, minimum=1)


@dataclass(frozen=True, slots=True)
class EspnTransactionItem:
    source_player_id: str
    from_source_team_id: str | None
    to_source_team_id: str | None
    from_lineup_slot_id: int | None
    to_lineup_slot_id: int | None
    asset_kind: EspnTransactionAssetKind = EspnTransactionAssetKind.PLAYER

    def __post_init__(self) -> None:
        if not isinstance(self.asset_kind, EspnTransactionAssetKind):
            raise ValueError("asset_kind must be an EspnTransactionAssetKind")
        if self.asset_kind is EspnTransactionAssetKind.PLAYER:
            _player_id("source_player_id", self.source_player_id)
        elif self.source_player_id != _UNSUPPORTED_SOURCE_ASSET_ID:
            raise ValueError("unsupported assets must use the bounded source marker")
        _optional_team_id("from_source_team_id", self.from_source_team_id)
        _optional_team_id("to_source_team_id", self.to_source_team_id)
        from_slot = _optional_integer(
            "from_lineup_slot_id", self.from_lineup_slot_id, minimum=-1
        )
        to_slot = _optional_integer(
            "to_lineup_slot_id", self.to_lineup_slot_id, minimum=-1
        )
        if self.from_source_team_id == self.to_source_team_id:
            raise ValueError("transaction item must change player ownership")
        object.__setattr__(
            self, "from_lineup_slot_id", None if from_slot == -1 else from_slot
        )
        object.__setattr__(
            self, "to_lineup_slot_id", None if to_slot == -1 else to_slot
        )


@dataclass(frozen=True, slots=True)
class EspnTransaction:
    source_transaction_id: str
    kind: EspnActivityKind
    source_type: str
    proposed_at: datetime
    scoring_period_id: int
    initiating_source_team_id: str | None
    bid_amount: float | None
    items: tuple[EspnTransactionItem, ...]
    accepted_at: datetime | None = None
    processed_at: datetime | None = None
    expires_at: datetime | None = None
    completion_observed_at: datetime | None = None

    def __post_init__(self) -> None:
        source_transaction_id = _source_transaction_id(
            "source_transaction_id", self.source_transaction_id
        )
        if not isinstance(self.kind, EspnActivityKind):
            raise ValueError("kind must be an EspnActivityKind")
        expected = _SOURCE_TYPES.get(self.source_type)
        if expected != self.kind.value:
            raise ValueError("source_type does not match the normalized activity kind")
        proposed = _aware("proposed_at", self.proposed_at)
        accepted = _optional_aware("accepted_at", self.accepted_at)
        processed = _optional_aware("processed_at", self.processed_at)
        expires = _optional_aware("expires_at", self.expires_at)
        completion_observed = _optional_aware(
            "completion_observed_at", self.completion_observed_at
        )
        _integer("scoring_period_id", self.scoring_period_id, minimum=0)
        _optional_team_id(
            "initiating_source_team_id", self.initiating_source_team_id
        )
        bid = _optional_number("bid_amount", self.bid_amount, minimum=0)
        rows = _typed("items", self.items, EspnTransactionItem, allow_empty=False)
        if self.kind is EspnActivityKind.TRADE:
            teams = {
                team_id
                for row in rows
                for team_id in (row.from_source_team_id, row.to_source_team_id)
                if team_id is not None
            }
            if len(teams) < 2 or any(
                row.from_source_team_id is None or row.to_source_team_id is None
                for row in rows
            ):
                raise ValueError("trade must transfer assets between multiple teams")
        object.__setattr__(self, "proposed_at", proposed)
        object.__setattr__(self, "accepted_at", accepted)
        object.__setattr__(self, "processed_at", processed)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "completion_observed_at", completion_observed)
        object.__setattr__(self, "bid_amount", bid)
        object.__setattr__(self, "source_transaction_id", source_transaction_id)
        object.__setattr__(self, "items", tuple(sorted(rows, key=_item_key)))


@dataclass(frozen=True, slots=True)
class EspnRosterEntry:
    source_player_id: str
    lineup_slot_id: int
    injury_status: str | None = None

    def __post_init__(self) -> None:
        _player_id("source_player_id", self.source_player_id)
        _integer("lineup_slot_id", self.lineup_slot_id, minimum=0)
        status = _optional_text("injury_status", self.injury_status)
        object.__setattr__(
            self,
            "injury_status",
            None if status is None else status.upper(),
        )


@dataclass(frozen=True, slots=True)
class EspnTeamRosterSnapshot:
    source_team_id: str
    entries: tuple[EspnRosterEntry, ...]

    def __post_init__(self) -> None:
        _team_id("source_team_id", self.source_team_id)
        try:
            rows = tuple(self.entries)
        except TypeError:
            raise ValueError("entries must contain EspnRosterEntry values") from None
        if not rows or any(not isinstance(row, EspnRosterEntry) for row in rows):
            raise ValueError("entries must contain EspnRosterEntry values")
        player_ids = [row.source_player_id for row in rows]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("current roster contains a duplicate player")
        object.__setattr__(
            self,
            "entries",
            tuple(
                sorted(
                    rows,
                    key=lambda row: (
                        row.lineup_slot_id,
                        _id_key(row.source_player_id),
                    ),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class EspnActivityCapture:
    source_league_id: str
    season: int
    scoring_period_id: int
    captured_at: datetime
    transactions_complete: bool
    returned_transaction_count: int
    transaction_limit: int
    transactions: tuple[EspnTransaction, ...]
    rosters: tuple[EspnTeamRosterSnapshot, ...]
    skipped_transactions: tuple[EspnActivitySkipCount, ...] = ()
    earliest_returned_proposed_at: datetime | None = None
    latest_returned_proposed_at: datetime | None = None
    capture_id: str = field(init=False)

    def __post_init__(self) -> None:
        _team_id("source_league_id", self.source_league_id)
        _integer("season", self.season, minimum=2012)
        _integer("scoring_period_id", self.scoring_period_id, minimum=1)
        captured = _aware("captured_at", self.captured_at)
        if not isinstance(self.transactions_complete, bool):
            raise ValueError("transactions_complete must be a boolean")
        _integer("transaction_limit", self.transaction_limit, minimum=1)
        if self.transaction_limit > ESPN_TRANSACTION_LIMIT:
            raise ValueError("transaction_limit exceeds the supported ESPN maximum")
        _integer(
            "returned_transaction_count", self.returned_transaction_count, minimum=0
        )
        if self.returned_transaction_count > self.transaction_limit:
            raise ValueError("returned transaction count exceeds the requested limit")
        if self.transactions_complete != (
            self.returned_transaction_count < self.transaction_limit
        ):
            raise ValueError("transaction completeness does not match the provider limit")
        transactions = _typed("transactions", self.transactions, EspnTransaction)
        skipped = _typed(
            "skipped_transactions", self.skipped_transactions, EspnActivitySkipCount
        )
        skipped = tuple(sorted(skipped, key=lambda row: row.reason.value))
        if len({row.reason for row in skipped}) != len(skipped):
            raise ValueError("activity contains a duplicate skip reason")
        if sum(row.count for row in skipped) != (
            self.returned_transaction_count - len(transactions)
        ):
            raise ValueError("activity skip counts do not explain omitted transactions")
        earliest = _optional_aware(
            "earliest_returned_proposed_at", self.earliest_returned_proposed_at
        )
        latest = _optional_aware(
            "latest_returned_proposed_at", self.latest_returned_proposed_at
        )
        if (earliest is None) != (latest is None):
            raise ValueError("returned proposal bounds must both be known or both be null")
        if self.returned_transaction_count == 0:
            if earliest is not None:
                raise ValueError("empty activity cannot have returned proposal bounds")
        elif earliest is None:
            raise ValueError("returned activity requires proposal bounds")
        elif earliest > latest or latest > captured:
            raise ValueError("returned proposal bounds are not ordered")
        rosters = _typed(
            "rosters", self.rosters, EspnTeamRosterSnapshot, allow_empty=False
        )
        self._validate_identifiers_and_times(transactions, rosters, captured, earliest, latest)
        transactions = tuple(sorted(transactions, key=_transaction_key))
        rosters = tuple(sorted(rosters, key=lambda row: _id_key(row.source_team_id)))
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "transactions", transactions)
        object.__setattr__(self, "rosters", rosters)
        object.__setattr__(self, "skipped_transactions", skipped)
        object.__setattr__(self, "earliest_returned_proposed_at", earliest)
        object.__setattr__(self, "latest_returned_proposed_at", latest)
        object.__setattr__(
            self, "capture_id", content_id("espn-activity", self._content_record())
        )

    def _validate_identifiers_and_times(
        self, transactions, rosters, captured, earliest, latest
    ) -> None:
        transaction_ids = [row.source_transaction_id for row in transactions]
        if len(set(transaction_ids)) != len(transaction_ids):
            raise ValueError("activity contains a duplicate transaction ID")
        team_ids = [row.source_team_id for row in rosters]
        if len(set(team_ids)) != len(team_ids):
            raise ValueError("activity contains a duplicate current roster")
        known_teams = set(team_ids)
        for transaction in transactions:
            referenced_teams = {
                team_id
                for item in transaction.items
                for team_id in (
                    item.from_source_team_id,
                    item.to_source_team_id,
                    transaction.initiating_source_team_id,
                )
                if team_id is not None
            }
            if not referenced_teams.issubset(known_teams):
                raise ValueError("transaction references an unknown league team")
            if transaction.proposed_at > captured:
                raise ValueError("transaction proposed_at cannot follow captured_at")
            if not earliest <= transaction.proposed_at <= latest:
                raise ValueError("transaction falls outside returned proposal bounds")
            if transaction.completion_observed_at != captured:
                raise ValueError("completion observation must match the activity capture")
            if any(
                timestamp is not None and timestamp > captured
                for timestamp in (transaction.accepted_at, transaction.processed_at)
            ):
                raise ValueError("accepted_at and processed_at cannot follow captured_at")
            if transaction.scoring_period_id > self.scoring_period_id:
                raise ValueError("transaction scoring period cannot be in the future")
        player_ids = [
            entry.source_player_id for roster in rosters for entry in roster.entries
        ]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("a player appears on multiple current rosters")

    def _content_record(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at.isoformat(),
            "earliest_returned_proposed_at": _optional_timestamp(
                self.earliest_returned_proposed_at
            ),
            "latest_returned_proposed_at": _optional_timestamp(
                self.latest_returned_proposed_at
            ),
            "returned_transaction_count": self.returned_transaction_count,
            "rosters": [
                {
                    "source_team_id": roster.source_team_id,
                    "entries": [
                        {
                            "injury_status": entry.injury_status,
                            "lineup_slot_id": entry.lineup_slot_id,
                            "source_player_id": entry.source_player_id,
                        }
                        for entry in roster.entries
                    ],
                }
                for roster in self.rosters
            ],
            "scoring_period_id": self.scoring_period_id,
            "season": self.season,
            "skipped_transactions": [
                {"count": row.count, "reason": row.reason.value}
                for row in self.skipped_transactions
            ],
            "source_league_id": self.source_league_id,
            "transaction_limit": self.transaction_limit,
            "transactions": [_transaction_record(row) for row in self.transactions],
            "transactions_complete": self.transactions_complete,
        }


def _transaction_record(row):
    return {
        "accepted_at": _optional_timestamp(row.accepted_at),
        "bid_amount": row.bid_amount,
        "completion_observed_at": _optional_timestamp(row.completion_observed_at),
        "expires_at": _optional_timestamp(row.expires_at),
        "initiating_source_team_id": row.initiating_source_team_id,
        "items": [
            {
                "from_lineup_slot_id": item.from_lineup_slot_id,
                "from_source_team_id": item.from_source_team_id,
                "asset_kind": item.asset_kind.value,
                "source_player_id": item.source_player_id,
                "to_lineup_slot_id": item.to_lineup_slot_id,
                "to_source_team_id": item.to_source_team_id,
            }
            for item in row.items
        ],
        "kind": row.kind.value,
        "proposed_at": row.proposed_at.isoformat(),
        "processed_at": _optional_timestamp(row.processed_at),
        "scoring_period_id": row.scoring_period_id,
        "source_transaction_id": row.source_transaction_id,
        "source_type": row.source_type,
    }


def _typed(name, values, item_type, *, allow_empty=True):
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must contain {item_type.__name__} values") from None
    if (not allow_empty and not rows) or any(
        not isinstance(row, item_type) for row in rows
    ):
        raise ValueError(f"{name} must contain {item_type.__name__} values")
    return rows


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(name, value):
    return None if value is None else _text(name, value)


def _integer(name, value, *, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _optional_integer(name, value, *, minimum):
    return None if value is None else _integer(name, value, minimum=minimum)


def _optional_number(name, value, *, minimum=None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number or null")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


def _source_transaction_id(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a bounded ESPN source ID")
    if isinstance(value, int) and abs(value) > _MAXIMUM_SAFE_JSON_INTEGER:
        raise ValueError(f"{name} must be a bounded ESPN source ID")
    text = str(value)
    if (
        not 1 <= len(text) <= 128
        or not text.isascii()
        or not any(character.isalnum() for character in text)
        or any(not (character.isalnum() or character in "-_") for character in text)
        or (
            (text.isdigit() or (text.startswith("-") and text[1:].isdigit()))
            and int(text) <= 0
        )
    ):
        raise ValueError(f"{name} must be a bounded ESPN source ID")
    return text


def _team_id(name, value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or int(value) <= 0
    ):
        raise ValueError(f"{name} must be a positive decimal string ID")


def _player_id(name, value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.lstrip("-").isdigit()
        or int(value) == 0
    ):
        raise ValueError(f"{name} must be a nonzero decimal string ID")


def _optional_team_id(name, value):
    if value is not None:
        _team_id(name, value)


def _aware(name, value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _optional_aware(name, value):
    return None if value is None else _aware(name, value)


def _optional_timestamp(value):
    return None if value is None else value.isoformat()


def _id_key(value):
    return int(value), value


def _optional_id_key(value):
    return (-1, "") if value is None else _id_key(value)


def _item_key(row):
    return (
        row.asset_kind.value,
        (
            _id_key(row.source_player_id)
            if row.asset_kind is EspnTransactionAssetKind.PLAYER
            else (0, row.source_player_id)
        ),
        _optional_id_key(row.from_source_team_id),
        _optional_id_key(row.to_source_team_id),
    )


def _transaction_key(row):
    source_id = row.source_transaction_id
    source_key = (
        (0, int(source_id), source_id)
        if source_id.isdigit()
        else (1, 0, source_id)
    )
    return row.proposed_at, source_key


__all__ = (
    "ESPN_TRANSACTION_LIMIT",
    "EspnActivityCapture",
    "EspnActivityKind",
    "EspnActivitySkipCount",
    "EspnActivitySkipReason",
    "EspnRosterEntry",
    "EspnTeamRosterSnapshot",
    "EspnTransaction",
    "EspnTransactionAssetKind",
    "EspnTransactionItem",
)
