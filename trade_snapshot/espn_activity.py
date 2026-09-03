"""Strict, privacy-minimal ESPN transaction and current-lineup evidence."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from numbers import Real

from ._scenario_random import content_id


ESPN_TRANSACTION_LIMIT = 1_000

_TRANSACTION_REQUIRED_FIELDS = {
    "bidAmount",
    "executionType",
    "id",
    "isActingAsTeamOwner",
    "isLeagueManager",
    "isPending",
    "items",
    "proposedDate",
    "rating",
    "scoringPeriodId",
    "status",
    "teamId",
    "type",
}
_TRANSACTION_OPTIONAL_FIELDS = {
    "acceptedDate",
    "expirationDate",
    "memberId",
    "processDate",
    "relatedTransactionId",
    "skipTransactionCounters",
    "subOrder",
    "teamActions",
}
_ITEM_FIELDS = {
    "fromLineupSlotId",
    "fromTeamId",
    "isKeeper",
    "overallPickNumber",
    "playerId",
    "toLineupSlotId",
    "toTeamId",
    "type",
}
_SOURCE_TYPES = {
    "TRADE_ACCEPT": "trade",
    "TRADE_UPHOLD": "trade",
    "WAIVER": "waiver",
    "FREEAGENT": "free_agent",
}
_PLAYER_ITEM_TYPES = frozenset({"ADD", "DROP", "TRADE"})
_UNSUPPORTED_SOURCE_ASSET_ID = "0"


class EspnActivityKind(str, Enum):
    TRADE = "trade"
    WAIVER = "waiver"
    FREE_AGENT = "free_agent"


class EspnTransactionAssetKind(str, Enum):
    PLAYER = "player"
    UNSUPPORTED_NON_PLAYER = "unsupported_non_player"


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
            tuple(sorted(rows, key=lambda row: (row.lineup_slot_id, _id_key(row.source_player_id)))),
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
        _integer("returned_transaction_count", self.returned_transaction_count, minimum=0)
        if self.returned_transaction_count > self.transaction_limit:
            raise ValueError("returned transaction count exceeds the requested limit")
        if self.transactions_complete != (
            self.returned_transaction_count < self.transaction_limit
        ):
            raise ValueError("transaction completeness does not match the provider limit")
        transactions = _typed("transactions", self.transactions, EspnTransaction)
        rosters = _typed(
            "rosters", self.rosters, EspnTeamRosterSnapshot, allow_empty=False
        )
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
            if transaction.scoring_period_id > self.scoring_period_id:
                raise ValueError("transaction scoring period cannot be in the future")
        player_ids = [
            entry.source_player_id for roster in rosters for entry in roster.entries
        ]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("a player appears on multiple current rosters")
        transactions = tuple(sorted(transactions, key=_transaction_key))
        rosters = tuple(sorted(rosters, key=lambda row: _id_key(row.source_team_id)))
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "transactions", transactions)
        object.__setattr__(self, "rosters", rosters)
        object.__setattr__(
            self, "capture_id", content_id("espn-activity", self._content_record())
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "captured_at": self.captured_at.isoformat(),
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
            "source_league_id": self.source_league_id,
            "transaction_limit": self.transaction_limit,
            "transactions": [_transaction_record(row) for row in self.transactions],
            "transactions_complete": self.transactions_complete,
        }


def espn_activity_capture(
    league_payload: Mapping[str, object],
    *,
    captured_at: datetime,
    transaction_limit: int = ESPN_TRANSACTION_LIMIT,
) -> EspnActivityCapture:
    """Project ESPN's full league response into bounded activity evidence."""

    league = _object("league_payload", league_payload)
    _integer("transaction_limit", transaction_limit, minimum=1)
    if transaction_limit > ESPN_TRANSACTION_LIMIT:
        raise ValueError("transaction_limit exceeds the supported ESPN maximum")
    raw_transactions = _array(
        "transactions", league.get("transactions"), allow_empty=True
    )
    if len(raw_transactions) > transaction_limit:
        raise ValueError("ESPN returned more transactions than requested")
    transactions = []
    seen_transaction_ids = set()
    for raw in raw_transactions:
        transaction_id, parsed = _parse_transaction(raw)
        if transaction_id in seen_transaction_ids:
            raise ValueError("ESPN returned a duplicate transaction ID")
        seen_transaction_ids.add(transaction_id)
        if parsed is not None:
            transactions.append(parsed)
    return EspnActivityCapture(
        source_league_id=_positive_identifier("league id", league.get("id")),
        season=_integer("seasonId", league.get("seasonId"), minimum=2012),
        scoring_period_id=_integer(
            "scoringPeriodId", league.get("scoringPeriodId"), minimum=1
        ),
        captured_at=captured_at,
        transactions_complete=len(raw_transactions) < transaction_limit,
        returned_transaction_count=len(raw_transactions),
        transaction_limit=transaction_limit,
        transactions=tuple(transactions),
        rosters=_parse_rosters(league.get("teams")),
    )


def _parse_transaction(value):
    row = _known_object(
        "transaction",
        value,
        _TRANSACTION_REQUIRED_FIELDS,
        _TRANSACTION_OPTIONAL_FIELDS,
    )
    source_id = _source_transaction_id("transaction id", row["id"])
    source_type = _text("transaction type", row["type"])
    status = _optional_text("transaction status", row["status"])
    proposed_at = _epoch_milliseconds("proposedDate", row["proposedDate"])
    scoring_period = _integer(
        "transaction scoringPeriodId", row["scoringPeriodId"], minimum=0
    )
    pending = _boolean("isPending", row["isPending"])
    _boolean("isActingAsTeamOwner", row["isActingAsTeamOwner"])
    _boolean("isLeagueManager", row["isLeagueManager"])
    _optional_text("executionType", row["executionType"])
    _optional_text("memberId", row.get("memberId"))
    _optional_number("rating", row["rating"])
    _validate_optional_transaction_fields(row)
    bid = _optional_number("bidAmount", row["bidAmount"], minimum=0)
    initiating_team = _source_team_id("teamId", row["teamId"])
    kind_value = _SOURCE_TYPES.get(source_type)
    if status != "EXECUTED" or pending or kind_value is None:
        return source_id, None
    kind = EspnActivityKind(kind_value)
    items = tuple(
        filter(
            None,
            (
                _parse_item(raw)
                for raw in _array(
                    "transaction items", row["items"], allow_empty=True
                )
            ),
        )
    )
    if kind is EspnActivityKind.TRADE:
        items = tuple(
            item
            for item in items
            if item.from_source_team_id is not None
            and item.to_source_team_id is not None
        )
        teams = {
            team_id
            for item in items
            for team_id in (item.from_source_team_id, item.to_source_team_id)
        }
        if len(teams) < 2:
            return source_id, None
    if not items:
        return source_id, None
    return source_id, EspnTransaction(
        source_id,
        kind,
        source_type,
        proposed_at,
        scoring_period,
        initiating_team,
        bid,
        items,
    )


def _validate_optional_transaction_fields(row):
    for name in ("acceptedDate", "expirationDate", "processDate"):
        if name in row and row[name] is not None:
            _epoch_milliseconds(name, row[name])
    if row.get("relatedTransactionId") is not None:
        _source_transaction_id(
            "relatedTransactionId", row["relatedTransactionId"]
        )
    if "skipTransactionCounters" in row:
        _boolean("skipTransactionCounters", row["skipTransactionCounters"])
    if "subOrder" in row:
        _integer("subOrder", row["subOrder"], minimum=0)
    if "teamActions" in row:
        actions = _object("teamActions", row["teamActions"])
        for team_id, action in actions.items():
            _positive_identifier("teamActions team ID", team_id)
            _text("teamActions action", action)


def _parse_item(value):
    row = _exact_object("transaction item", value, _ITEM_FIELDS)
    item_type = _text("transaction item type", row["type"])
    _boolean("transaction item isKeeper", row["isKeeper"])
    overall_pick = _optional_integer(
        "overallPickNumber", row["overallPickNumber"], minimum=0
    )
    from_team = _source_team_id("fromTeamId", row["fromTeamId"])
    to_team = _source_team_id("toTeamId", row["toTeamId"])
    from_slot = _optional_integer(
        "fromLineupSlotId", row["fromLineupSlotId"], minimum=-1
    )
    to_slot = _optional_integer(
        "toLineupSlotId", row["toLineupSlotId"], minimum=-1
    )
    if from_team == to_team:
        return None
    if overall_pick is not None and overall_pick > 0:
        return EspnTransactionItem(
            _UNSUPPORTED_SOURCE_ASSET_ID,
            from_team,
            to_team,
            from_slot,
            to_slot,
            EspnTransactionAssetKind.UNSUPPORTED_NON_PLAYER,
        )
    if item_type not in _PLAYER_ITEM_TYPES:
        raise ValueError(f"unsupported executed transaction item type: {item_type}")
    player_id = _signed_identifier("playerId", row["playerId"])
    return EspnTransactionItem(player_id, from_team, to_team, from_slot, to_slot)


def _parse_rosters(value):
    rows = _array("teams", value)
    result = []
    for raw in rows:
        team = _object("team", raw)
        source_team_id = _positive_identifier("team.id", team.get("id"))
        roster = _object("team.roster", team.get("roster"))
        entries = []
        for raw_entry in _array("team.roster.entries", roster.get("entries")):
            entry = _object("roster entry", raw_entry)
            player_pool = entry.get("playerPoolEntry")
            injury_status = None
            if player_pool is not None:
                player = _object(
                    "roster entry playerPoolEntry.player",
                    _object("roster entry playerPoolEntry", player_pool).get("player"),
                )
                injury_status = _optional_text(
                    "roster player injuryStatus", player.get("injuryStatus")
                )
            entries.append(
                EspnRosterEntry(
                    _signed_identifier("roster playerId", entry.get("playerId")),
                    _integer(
                        "roster lineupSlotId", entry.get("lineupSlotId"), minimum=0
                    ),
                    injury_status,
                )
            )
        result.append(EspnTeamRosterSnapshot(source_team_id, tuple(entries)))
    return tuple(result)


def _transaction_record(row):
    return {
        "bid_amount": row.bid_amount,
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


def _object(name, value):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _exact_object(name, value, fields):
    row = _object(name, value)
    if set(row) != fields:
        raise ValueError(f"{name} fields do not match the ESPN schema")
    return row


def _known_object(name, value, required_fields, optional_fields):
    row = _object(name, value)
    fields = set(row)
    if not required_fields.issubset(fields) or not fields.issubset(
        required_fields | optional_fields
    ):
        raise ValueError(f"{name} fields do not match the ESPN schema")
    return row


def _array(name, value, *, allow_empty=False):
    if not isinstance(value, list) or (not allow_empty and not value):
        label = "JSON array" if allow_empty else "non-empty JSON array"
        raise ValueError(f"{name} must be a {label}")
    return value


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(name, value):
    if value is None:
        return None
    return _text(name, value)


def _boolean(name, value):
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _integer(name, value, *, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _optional_integer(name, value, *, minimum):
    if value is None:
        return None
    return _integer(name, value, minimum=minimum)


def _optional_number(name, value, *, minimum=None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number or null")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


def _positive_identifier(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a positive decimal ID")
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{name} must be a positive decimal ID")
    return text


def _source_transaction_id(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a bounded ESPN source ID")
    text = str(value)
    if (
        not 1 <= len(text) <= 128
        or not text.isascii()
        or not any(character.isalnum() for character in text)
        or any(
            not (character.isalnum() or character in "-_")
            for character in text
        )
        or (text.lstrip("-").isdigit() and int(text) <= 0)
    ):
        raise ValueError(f"{name} must be a bounded ESPN source ID")
    return text


def _signed_identifier(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a nonzero decimal ID")
    text = str(value)
    if not text.isascii() or not text.lstrip("-").isdigit() or int(text) == 0:
        raise ValueError(f"{name} must be a nonzero decimal ID")
    return text


def _source_team_id(name, value):
    if value in (None, 0, "0"):
        return None
    return _positive_identifier(name, value)


def _team_id(name, value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{name} must be a positive decimal string ID")


def _player_id(name, value):
    if not isinstance(value, str) or not value.isascii() or not value.lstrip("-").isdigit() or int(value) == 0:
        raise ValueError(f"{name} must be a nonzero decimal string ID")


def _optional_team_id(name, value):
    if value is not None:
        _team_id(name, value)


def _aware(name, value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _epoch_milliseconds(name, value):
    _integer(name, value, minimum=0)
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"{name} is outside the supported timestamp range") from None


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
    "EspnRosterEntry",
    "EspnTeamRosterSnapshot",
    "EspnTransaction",
    "EspnTransactionAssetKind",
    "EspnTransactionItem",
    "espn_activity_capture",
)
