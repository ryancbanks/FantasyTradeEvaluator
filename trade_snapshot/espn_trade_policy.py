"""Normalize ESPN mSettings/mRoster fields into an optional precheck sidecar."""

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
import re

from ._scenario_random import SAFE_INTEGER, content_id
from .host_transaction_policy import (
    HostAssetField,
    HostAssetTransactionStatus,
    HostPolicyField,
    HostTransactionPolicy,
    HostTransactionPolicyAttempt,
    HostTransactionPolicyReason,
    HostTransactionPolicyStatus,
)
from .league_source import VerifiedHostLeagueSnapshot


ESPN_HOST_TRANSACTION_POLICY_ADAPTER_VERSION = "espn-ffl-host-policy-v1"
_MODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_MAX_PENDING_REFERENCES_PER_PLAYER = 64
_MAX_SOURCE_REFERENCE_LENGTH = 128


class _SourceSchemaError(ValueError):
    pass


class _IdentityMismatch(ValueError):
    pass


class _SourceFieldState(str, Enum):
    OBSERVED = "observed"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"


def acquire_espn_host_transaction_policy(
    league_payload: object,
    *,
    host_snapshot: VerifiedHostLeagueSnapshot,
    league_binding_id: str,
    canonical_player_ids: Mapping[str, str],
) -> HostTransactionPolicyAttempt:
    """Capture optional leaf fields from the payload that produced ``host_snapshot``.

    The core ESPN adapter has already proved the settings and roster roots.
    Missing or changed precheck leaves become per-field gaps; a changed root
    means this is no longer the same validated host-capture contract.
    """

    _context(host_snapshot, league_binding_id, canonical_player_ids)
    try:
        policy = _policy(
            league_payload,
            host_snapshot=host_snapshot,
            league_binding_id=league_binding_id,
            canonical_player_ids=canonical_player_ids,
        )
    except _IdentityMismatch:
        return _unavailable(
            host_snapshot,
            league_binding_id,
            HostTransactionPolicyReason.IDENTITY_MISMATCH,
        )
    except _SourceSchemaError:
        return _unavailable(
            host_snapshot,
            league_binding_id,
            HostTransactionPolicyReason.SOURCE_SCHEMA_UNSUPPORTED,
        )
    complete = policy.field_coverage_complete
    return HostTransactionPolicyAttempt(
        league_binding_id=league_binding_id,
        season=host_snapshot.season,
        source_provider="espn",
        source_adapter_version=ESPN_HOST_TRANSACTION_POLICY_ADAPTER_VERSION,
        host_snapshot_id=host_snapshot.snapshot_id,
        attempted_at=host_snapshot.captured_at,
        status=(
            HostTransactionPolicyStatus.FIELD_COVERAGE_COMPLETE
            if complete
            else HostTransactionPolicyStatus.PARTIAL
        ),
        reason_code=(
            HostTransactionPolicyReason.FIELD_COVERAGE_COMPLETE
            if complete
            else HostTransactionPolicyReason.SOURCE_FIELDS_PARTIAL
        ),
        policy=policy,
    )


def _policy(
    payload,
    *,
    host_snapshot,
    league_binding_id,
    canonical_player_ids,
):
    league = _object(payload)
    if _source_id(league.get("id")) != host_snapshot.source_league_id:
        raise _IdentityMismatch("ESPN payload does not match the host league")
    if _integer(league.get("seasonId"), minimum=2012) != host_snapshot.season:
        raise _IdentityMismatch("ESPN payload does not match the host season")
    settings = _required_object(league, "settings")
    roster_settings = _required_object(settings, "rosterSettings")
    trade_settings, trade_group_state = _optional_object(settings, "tradeSettings")

    values = {}
    absent_fields = set()
    unsupported_fields = set()
    specs = (
        (HostPolicyField.TRADE_DEADLINE, trade_settings, trade_group_state,
         "deadlineDate", _epoch_milliseconds),
        (HostPolicyField.REVISION_HOURS_SOURCE_VALUE, trade_settings, trade_group_state,
         "revisionHours", lambda value: _integer(value, minimum=0)),
        (HostPolicyField.VETO_VOTES_REQUIRED, trade_settings, trade_group_state,
         "vetoVotesRequired", lambda value: _integer(value, minimum=0)),
        (HostPolicyField.UNDROPPABLE_LIST_ENABLED, roster_settings,
         _SourceFieldState.OBSERVED, "isUsingUndroppableList", _boolean),
        (HostPolicyField.LINEUP_LOCKTIME_TYPE_SOURCE_VALUE, roster_settings,
         _SourceFieldState.OBSERVED, "lineupLocktimeType", _mode),
        (HostPolicyField.ROSTER_LOCKTIME_TYPE_SOURCE_VALUE, roster_settings,
         _SourceFieldState.OBSERVED, "rosterLocktimeType", _mode),
    )
    for source_field, source, group_state, key, parser in specs:
        value, state = _optional_value(source, group_state, key, parser)
        values[source_field] = value
        _record_gap(source_field, state, absent_fields, unsupported_fields)

    asset_statuses = _asset_statuses(
        league,
        host_snapshot=host_snapshot,
        league_binding_id=league_binding_id,
        canonical_player_ids=canonical_player_ids,
    )
    return HostTransactionPolicy(
        league_binding_id=league_binding_id,
        season=host_snapshot.season,
        source_provider="espn",
        source_adapter_version=ESPN_HOST_TRANSACTION_POLICY_ADAPTER_VERSION,
        host_snapshot_id=host_snapshot.snapshot_id,
        captured_at=host_snapshot.captured_at,
        trade_deadline_at=values[HostPolicyField.TRADE_DEADLINE],
        revision_hours_source_value=values[
            HostPolicyField.REVISION_HOURS_SOURCE_VALUE
        ],
        veto_votes_required=values[HostPolicyField.VETO_VOTES_REQUIRED],
        undroppable_list_enabled=values[HostPolicyField.UNDROPPABLE_LIST_ENABLED],
        lineup_locktime_type_source_value=values[
            HostPolicyField.LINEUP_LOCKTIME_TYPE_SOURCE_VALUE
        ],
        roster_locktime_type_source_value=values[
            HostPolicyField.ROSTER_LOCKTIME_TYPE_SOURCE_VALUE
        ],
        asset_statuses=asset_statuses,
        absent_fields=frozenset(absent_fields),
        unsupported_fields=frozenset(unsupported_fields),
    )


def _asset_statuses(
    league,
    *,
    host_snapshot,
    league_binding_id,
    canonical_player_ids,
):
    teams = league.get("teams")
    if not isinstance(teams, list) or not teams:
        raise _SourceSchemaError("ESPN teams are missing")
    source_entries = {}
    for raw_team in teams:
        team = _object(raw_team)
        roster = _required_object(team, "roster")
        entries = roster.get("entries")
        if not isinstance(entries, list) or not entries:
            raise _SourceSchemaError("ESPN roster entries are missing")
        for raw_entry in entries:
            entry = _object(raw_entry)
            source_player_id = _source_id(entry.get("playerId"), signed=True)
            if source_player_id in source_entries:
                raise _IdentityMismatch("ESPN roster repeats one player")
            source_entries[source_player_id] = entry

    expected_source_ids = {row.source_player_id for row in host_snapshot.players}
    normalized_mapping = _canonical_mapping(canonical_player_ids)
    if (
        set(source_entries) != expected_source_ids
        or set(normalized_mapping) != expected_source_ids
        or len(set(normalized_mapping.values())) != len(normalized_mapping)
    ):
        raise _IdentityMismatch("ESPN roster identity coverage does not match")

    result = []
    for source_player_id, entry in source_entries.items():
        pool, pool_state = _optional_object(entry, "playerPoolEntry")
        if pool is not None and "id" in pool:
            try:
                pool_id = _source_id(pool["id"], signed=True)
            except _SourceSchemaError:
                raise _IdentityMismatch("ESPN player-pool identity is invalid") from None
            if pool_id != source_player_id:
                raise _IdentityMismatch("ESPN player-pool identity conflicts")
        player, player_state = (
            _optional_object(pool, "player")
            if pool is not None
            else (None, pool_state)
        )
        if player is not None:
            if "id" not in player:
                player, player_state = None, _SourceFieldState.UNSUPPORTED
            else:
                try:
                    nested_player_id = _source_id(player["id"], signed=True)
                except _SourceSchemaError:
                    raise _IdentityMismatch(
                        "ESPN nested player identity is invalid"
                    ) from None
                if nested_player_id != source_player_id:
                    raise _IdentityMismatch("ESPN nested player identity conflicts")
        fields = (
            (HostAssetField.LINEUP_LOCKED, pool, pool_state,
             "lineupLocked", _boolean),
            (HostAssetField.ROSTER_LOCKED, pool, pool_state,
             "rosterLocked", _boolean),
            (HostAssetField.TRADE_LOCKED, pool, pool_state,
             "tradeLocked", _boolean),
            (HostAssetField.DROPPABLE, player, player_state,
             "droppable", _boolean),
            (HostAssetField.PENDING_TRANSACTION_REFERENCES, entry,
             _SourceFieldState.OBSERVED, "pendingTransactionIds",
             lambda value: _pending_references(value, league_binding_id)),
        )
        values, absent_fields, unsupported_fields = {}, set(), set()
        for source_field, source, group_state, key, parser in fields:
            value, state = _optional_value(source, group_state, key, parser)
            values[source_field] = value
            _record_gap(source_field, state, absent_fields, unsupported_fields)
        result.append(
            HostAssetTransactionStatus(
                player_id=normalized_mapping[source_player_id],
                lineup_locked=values[HostAssetField.LINEUP_LOCKED],
                roster_locked=values[HostAssetField.ROSTER_LOCKED],
                trade_locked=values[HostAssetField.TRADE_LOCKED],
                droppable=values[HostAssetField.DROPPABLE],
                pending_transaction_reference_ids=values[
                    HostAssetField.PENDING_TRANSACTION_REFERENCES
                ],
                absent_fields=frozenset(absent_fields),
                unsupported_fields=frozenset(unsupported_fields),
            )
        )
    return tuple(result)


def _context(host_snapshot, league_binding_id, canonical_player_ids):
    if not isinstance(host_snapshot, VerifiedHostLeagueSnapshot):
        raise ValueError("host_snapshot must be a VerifiedHostLeagueSnapshot")
    if host_snapshot.source_provider != "espn":
        raise ValueError("host_snapshot must come from ESPN")
    if not isinstance(league_binding_id, str):
        raise ValueError("league_binding_id must be text")
    if not isinstance(canonical_player_ids, Mapping) or not canonical_player_ids:
        raise ValueError("canonical_player_ids must be a non-empty mapping")


def _unavailable(host_snapshot, league_binding_id, reason):
    return HostTransactionPolicyAttempt(
        league_binding_id=league_binding_id,
        season=host_snapshot.season,
        source_provider="espn",
        source_adapter_version=ESPN_HOST_TRANSACTION_POLICY_ADAPTER_VERSION,
        host_snapshot_id=host_snapshot.snapshot_id,
        attempted_at=host_snapshot.captured_at,
        status=HostTransactionPolicyStatus.UNAVAILABLE,
        reason_code=reason,
    )


def _optional_object(parent, key):
    if key not in parent:
        return None, _SourceFieldState.ABSENT
    value = parent[key]
    if not isinstance(value, Mapping):
        return None, _SourceFieldState.UNSUPPORTED
    return value, _SourceFieldState.OBSERVED


def _optional_value(source, group_state, key, parser):
    if source is None:
        return None, group_state
    if key not in source:
        return None, _SourceFieldState.ABSENT
    try:
        return parser(source[key]), _SourceFieldState.OBSERVED
    except _SourceSchemaError:
        return None, _SourceFieldState.UNSUPPORTED


def _record_gap(source_field, state, absent_fields, unsupported_fields):
    if state is _SourceFieldState.ABSENT:
        absent_fields.add(source_field)
    elif state is _SourceFieldState.UNSUPPORTED:
        unsupported_fields.add(source_field)


def _pending_references(value, league_binding_id):
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_PENDING_REFERENCES_PER_PLAYER:
        raise _SourceSchemaError("pending transaction references are invalid")
    source_ids = tuple(_reference_id(row) for row in value)
    if len(set(source_ids)) != len(source_ids):
        raise _SourceSchemaError("pending transaction references contain duplicates")
    return tuple(
        content_id(
            "pendingtx",
            {
                "league_binding_id": league_binding_id,
                "source_transaction_id": source_id,
            },
        )
        for source_id in source_ids
    )


def _canonical_mapping(value):
    result = {}
    for source_id, canonical_id in value.items():
        try:
            source = _source_id(source_id, signed=True)
        except _SourceSchemaError:
            raise _IdentityMismatch("canonical player mapping source ID is invalid") from None
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise _IdentityMismatch("canonical player mapping target is invalid")
        if source in result:
            raise _IdentityMismatch("canonical player mapping repeats a source ID")
        result[source] = canonical_id.strip()
    return result


def _object(value):
    if not isinstance(value, Mapping):
        raise _SourceSchemaError("ESPN source object is invalid")
    return value


def _required_object(parent, key):
    if key not in parent or not isinstance(parent[key], Mapping):
        raise _SourceSchemaError(f"ESPN {key} object is missing")
    return parent[key]


def _source_id(value, *, signed=False):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _SourceSchemaError("ESPN source ID is invalid")
    text = str(value)
    digits = text.lstrip("-") if signed else text
    if not text.isascii() or not digits.isdigit() or int(text) == 0:
        raise _SourceSchemaError("ESPN source ID is invalid")
    if not signed and int(text) < 0:
        raise _SourceSchemaError("ESPN source ID is invalid")
    return text


def _reference_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _SourceSchemaError("ESPN pending transaction reference is invalid")
    text = str(value).strip()
    if not text or len(text) > _MAX_SOURCE_REFERENCE_LENGTH or not text.isascii():
        raise _SourceSchemaError("ESPN pending transaction reference is invalid")
    return text


def _integer(value, *, minimum):
    if type(value) is not int or not minimum <= value <= SAFE_INTEGER:
        raise _SourceSchemaError("ESPN integer field is invalid")
    return value


def _boolean(value):
    if not isinstance(value, bool):
        raise _SourceSchemaError("ESPN boolean field is invalid")
    return value


def _mode(value):
    if not isinstance(value, str) or not _MODE.fullmatch(value):
        raise _SourceSchemaError("ESPN locktime mode is invalid")
    return value


def _epoch_milliseconds(value):
    milliseconds = _integer(value, minimum=1)
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    except (OSError, OverflowError, ValueError):
        raise _SourceSchemaError("ESPN deadline is outside the timestamp range") from None


__all__ = (
    "ESPN_HOST_TRANSACTION_POLICY_ADAPTER_VERSION",
    "acquire_espn_host_transaction_policy",
)
