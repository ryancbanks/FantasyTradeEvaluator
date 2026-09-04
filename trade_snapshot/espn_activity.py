"""Strict, privacy-minimal ESPN transaction and current-lineup evidence."""

from collections.abc import Mapping
from datetime import datetime, timezone

from ._espn_activity_evidence import (
    ESPN_TRANSACTION_LIMIT,
    EspnActivityCapture,
    EspnActivityKind,
    EspnActivitySkipCount,
    EspnActivitySkipReason,
    EspnRosterEntry,
    EspnTeamRosterSnapshot,
    EspnTransaction,
    EspnTransactionAssetKind,
    EspnTransactionItem,
    _SOURCE_TYPES,
    _UNSUPPORTED_SOURCE_ASSET_ID,
    _integer,
    _optional_integer,
    _optional_number,
    _source_transaction_id,
)


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
    "status",
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
_PLAYER_ITEM_TYPES = frozenset({"ADD", "DROP", "TRADE"})


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
    transactions, proposed_times = [], []
    skip_counts = {reason: 0 for reason in EspnActivitySkipReason}
    seen_transaction_ids = set()
    for raw in raw_transactions:
        transaction_id, parsed, skipped, proposed_at = _parse_transaction(
            raw, completion_observed_at=captured_at
        )
        if transaction_id in seen_transaction_ids:
            raise ValueError("ESPN returned a duplicate transaction ID")
        seen_transaction_ids.add(transaction_id)
        proposed_times.append(proposed_at)
        if parsed is not None:
            transactions.append(parsed)
        else:
            skip_counts[skipped] += 1
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
        skipped_transactions=tuple(
            EspnActivitySkipCount(reason, count)
            for reason, count in skip_counts.items()
            if count
        ),
        earliest_returned_proposed_at=min(proposed_times, default=None),
        latest_returned_proposed_at=max(proposed_times, default=None),
    )


def _parse_transaction(value, *, completion_observed_at):
    row = _known_object(
        "transaction",
        value,
        _TRANSACTION_REQUIRED_FIELDS,
        _TRANSACTION_OPTIONAL_FIELDS,
    )
    source_id = _source_transaction_id("transaction id", row["id"])
    source_type = _text("transaction type", row["type"])
    status = _optional_text("transaction status", row.get("status"))
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
    accepted_at, processed_at, expires_at = _parse_optional_transaction_fields(row)
    bid = _optional_number("bidAmount", row["bidAmount"], minimum=0)
    initiating_team = _source_team_id("teamId", row["teamId"])
    kind_value = _SOURCE_TYPES.get(source_type)
    if pending:
        return source_id, None, EspnActivitySkipReason.PENDING, proposed_at
    if status != "EXECUTED":
        return source_id, None, EspnActivitySkipReason.NOT_EXECUTED, proposed_at
    if kind_value is None:
        return (
            source_id,
            None,
            EspnActivitySkipReason.UNSUPPORTED_ACTIVITY_KIND,
            proposed_at,
        )
    kind = EspnActivityKind(kind_value)
    raw_items = _array("transaction items", row["items"], allow_empty=True)
    items = tuple(
        filter(
            None,
            (_parse_item(raw) for raw in raw_items),
        )
    )
    if kind is EspnActivityKind.TRADE:
        bilateral = tuple(
            item for item in items
            if item.from_source_team_id is not None and item.to_source_team_id is not None
        )
        teams = {
            team_id
            for item in bilateral
            for team_id in (item.from_source_team_id, item.to_source_team_id)
        }
        if len(bilateral) != len(raw_items) or len(teams) < 2:
            return (
                source_id,
                None,
                EspnActivitySkipReason.TRADE_WITHOUT_BILATERAL_ASSETS,
                proposed_at,
            )
    if not items:
        return source_id, None, EspnActivitySkipReason.NO_OWNERSHIP_CHANGES, proposed_at
    return (
        source_id,
        EspnTransaction(
            source_transaction_id=source_id,
            kind=kind,
            source_type=source_type,
            proposed_at=proposed_at,
            scoring_period_id=scoring_period,
            initiating_source_team_id=initiating_team,
            bid_amount=bid,
            items=items,
            accepted_at=accepted_at,
            processed_at=processed_at,
            expires_at=expires_at,
            completion_observed_at=completion_observed_at,
        ),
        None,
        proposed_at,
    )


def _parse_optional_transaction_fields(row):
    accepted_at = _optional_epoch_milliseconds("acceptedDate", row.get("acceptedDate"))
    processed_at = _optional_epoch_milliseconds("processDate", row.get("processDate"))
    expires_at = _optional_epoch_milliseconds("expirationDate", row.get("expirationDate"))
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
    return accepted_at, processed_at, expires_at


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


def _positive_identifier(name, value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{name} must be a positive decimal ID")
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{name} must be a positive decimal ID")
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


def _epoch_milliseconds(name, value):
    _integer(name, value, minimum=0)
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"{name} is outside the supported timestamp range") from None


def _optional_epoch_milliseconds(name, value):
    return None if value is None else _epoch_milliseconds(name, value)


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
    "espn_activity_capture",
)
