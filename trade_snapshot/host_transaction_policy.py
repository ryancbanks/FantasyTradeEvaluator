"""Versioned, privacy-safe host transaction-policy sidecar evidence."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re

from ._scenario_random import SAFE_INTEGER, content_id


HOST_TRANSACTION_POLICY_SCHEMA_VERSION = 1
HOST_TRANSACTION_POLICY_SEMANTICS_VERSION = "host-transaction-policy-v1"
HOST_TRANSACTION_POLICY_SCOPE = "analytical_precheck_only"
HOST_PENDING_STATUS_SCOPE = "rostered_player_references_unjoined"

_LEAGUE_BINDING_ID = re.compile(r"^league_[0-9a-f]{32}(?:[0-9a-f]{32})?$")
_PENDING_REFERENCE_ID = re.compile(r"^pendingtx_[0-9a-f]{64}$")


class HostPolicyField(str, Enum):
    TRADE_DEADLINE = "trade_deadline"
    REVISION_HOURS_SOURCE_VALUE = "revision_hours_source_value"
    VETO_VOTES_REQUIRED = "veto_votes_required"
    UNDROPPABLE_LIST_ENABLED = "undroppable_list_enabled"
    LINEUP_LOCKTIME_TYPE_SOURCE_VALUE = "lineup_locktime_type_source_value"
    ROSTER_LOCKTIME_TYPE_SOURCE_VALUE = "roster_locktime_type_source_value"


class HostAssetField(str, Enum):
    LINEUP_LOCKED = "lineup_locked"
    ROSTER_LOCKED = "roster_locked"
    TRADE_LOCKED = "trade_locked"
    DROPPABLE = "droppable"
    PENDING_TRANSACTION_REFERENCES = "pending_transaction_references"


class TradeDeadlineState(str, Enum):
    BEFORE_DEADLINE = "before_deadline"
    DEADLINE_REACHED = "deadline_reached"


class HostTransactionPolicyStatus(str, Enum):
    FIELD_COVERAGE_COMPLETE = "field_coverage_complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class HostTransactionPolicyReason(str, Enum):
    FIELD_COVERAGE_COMPLETE = "field_coverage_complete"
    SOURCE_FIELDS_PARTIAL = "source_fields_partial"
    SOURCE_SCHEMA_UNSUPPORTED = "source_schema_unsupported"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True, slots=True)
class HostAssetTransactionStatus:
    """Distinct as-of flags for one canonical rostered player."""

    player_id: str
    lineup_locked: bool | None
    roster_locked: bool | None
    trade_locked: bool | None
    droppable: bool | None
    pending_transaction_reference_ids: tuple[str, ...] | None
    absent_fields: frozenset[HostAssetField] = frozenset()
    unsupported_fields: frozenset[HostAssetField] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "player_id", _text("player_id", self.player_id))
        for name in ("lineup_locked", "roster_locked", "trade_locked", "droppable"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean or None")
        pending = self.pending_transaction_reference_ids
        if pending is not None:
            pending = _tuple("pending_transaction_reference_ids", pending)
            if any(
                not isinstance(value, str)
                or not _PENDING_REFERENCE_ID.fullmatch(value)
                for value in pending
            ):
                raise ValueError("pending transaction reference ID is invalid")
            if len(set(pending)) != len(pending):
                raise ValueError("pending transaction reference IDs must be unique")
            object.__setattr__(
                self, "pending_transaction_reference_ids", tuple(sorted(pending))
            )
        absent = _enum_set("absent_fields", self.absent_fields, HostAssetField)
        unsupported = _enum_set(
            "unsupported_fields", self.unsupported_fields, HostAssetField
        )
        _validate_gaps(
            HostAssetField,
            absent,
            unsupported,
            {
                HostAssetField.LINEUP_LOCKED: self.lineup_locked,
                HostAssetField.ROSTER_LOCKED: self.roster_locked,
                HostAssetField.TRADE_LOCKED: self.trade_locked,
                HostAssetField.DROPPABLE: self.droppable,
                HostAssetField.PENDING_TRANSACTION_REFERENCES: pending,
            },
        )
        object.__setattr__(self, "absent_fields", absent)
        object.__setattr__(self, "unsupported_fields", unsupported)

    @property
    def field_coverage_complete(self) -> bool:
        return not self.absent_fields and not self.unsupported_fields

    def to_record(self) -> dict[str, object]:
        return {
            "absent_fields": _enum_values(self.absent_fields),
            "droppable": self.droppable,
            "lineup_locked": self.lineup_locked,
            "pending_transaction_reference_ids": (
                None
                if self.pending_transaction_reference_ids is None
                else list(self.pending_transaction_reference_ids)
            ),
            "player_id": self.player_id,
            "roster_locked": self.roster_locked,
            "trade_locked": self.trade_locked,
            "unsupported_fields": _enum_values(self.unsupported_fields),
        }

    @classmethod
    def from_record(cls, record: object) -> "HostAssetTransactionStatus":
        fields = {
            "absent_fields", "droppable", "lineup_locked",
            "pending_transaction_reference_ids", "player_id", "roster_locked",
            "trade_locked", "unsupported_fields",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("host asset transaction status fields are invalid")
        pending = record["pending_transaction_reference_ids"]
        if pending is not None and not isinstance(pending, list):
            raise ValueError("pending transaction references must be an array or null")
        return cls(
            player_id=record["player_id"],
            lineup_locked=record["lineup_locked"],
            roster_locked=record["roster_locked"],
            trade_locked=record["trade_locked"],
            droppable=record["droppable"],
            pending_transaction_reference_ids=(
                None if pending is None else tuple(pending)
            ),
            absent_fields=_enum_record(record["absent_fields"], HostAssetField),
            unsupported_fields=_enum_record(
                record["unsupported_fields"], HostAssetField
            ),
        )


@dataclass(frozen=True, slots=True)
class HostTransactionPolicy:
    """One as-of precheck bound to an exact host snapshot, never legal approval.

    Names ending in ``source_value`` preserve raw ESPN meaning without
    assigning stronger platform-independent semantics.
    """

    league_binding_id: str
    season: int
    source_provider: str
    source_adapter_version: str
    host_snapshot_id: str
    captured_at: datetime
    trade_deadline_at: datetime | None
    revision_hours_source_value: int | None
    veto_votes_required: int | None
    undroppable_list_enabled: bool | None
    lineup_locktime_type_source_value: str | None
    roster_locktime_type_source_value: str | None
    asset_statuses: tuple[HostAssetTransactionStatus, ...]
    absent_fields: frozenset[HostPolicyField] = frozenset()
    unsupported_fields: frozenset[HostPolicyField] = frozenset()
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        binding = _text("league_binding_id", self.league_binding_id)
        if not _LEAGUE_BINDING_ID.fullmatch(binding):
            raise ValueError("league_binding_id must be an opaque local binding")
        object.__setattr__(self, "league_binding_id", binding)
        _optional_int("season", self.season, minimum=2012, required=True)
        provider = _text("source_provider", self.source_provider).casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", provider):
            raise ValueError("source_provider is invalid")
        object.__setattr__(self, "source_provider", provider)
        for name in ("source_adapter_version", "host_snapshot_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(self, "captured_at", _aware("captured_at", self.captured_at))
        if self.trade_deadline_at is not None:
            object.__setattr__(
                self,
                "trade_deadline_at",
                _aware("trade_deadline_at", self.trade_deadline_at),
            )
        _optional_int(
            "revision_hours_source_value",
            self.revision_hours_source_value,
            minimum=0,
        )
        _optional_int("veto_votes_required", self.veto_votes_required, minimum=0)
        if self.undroppable_list_enabled is not None and not isinstance(
            self.undroppable_list_enabled, bool
        ):
            raise ValueError("undroppable_list_enabled must be a boolean or None")
        for name in (
            "lineup_locktime_type_source_value",
            "roster_locktime_type_source_value",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _source_mode(name, value))
        statuses = _typed_tuple(
            "asset_statuses", self.asset_statuses, HostAssetTransactionStatus
        )
        if not statuses or len({row.player_id for row in statuses}) != len(statuses):
            raise ValueError("asset_statuses must contain unique rostered players")
        object.__setattr__(
            self, "asset_statuses", tuple(sorted(statuses, key=lambda row: row.player_id))
        )
        absent = _enum_set("absent_fields", self.absent_fields, HostPolicyField)
        unsupported = _enum_set(
            "unsupported_fields", self.unsupported_fields, HostPolicyField
        )
        _validate_gaps(
            HostPolicyField,
            absent,
            unsupported,
            {
                HostPolicyField.TRADE_DEADLINE: self.trade_deadline_at,
                HostPolicyField.REVISION_HOURS_SOURCE_VALUE: (
                    self.revision_hours_source_value
                ),
                HostPolicyField.VETO_VOTES_REQUIRED: self.veto_votes_required,
                HostPolicyField.UNDROPPABLE_LIST_ENABLED: (
                    self.undroppable_list_enabled
                ),
                HostPolicyField.LINEUP_LOCKTIME_TYPE_SOURCE_VALUE: (
                    self.lineup_locktime_type_source_value
                ),
                HostPolicyField.ROSTER_LOCKTIME_TYPE_SOURCE_VALUE: (
                    self.roster_locktime_type_source_value
                ),
            },
        )
        object.__setattr__(self, "absent_fields", absent)
        object.__setattr__(self, "unsupported_fields", unsupported)
        object.__setattr__(
            self, "policy_id", content_id("hostpolicy", self._content_record())
        )

    @property
    def field_coverage_complete(self) -> bool:
        return (
            not self.absent_fields
            and not self.unsupported_fields
            and all(row.field_coverage_complete for row in self.asset_statuses)
        )

    @property
    def deadline_state_at_capture(self) -> TradeDeadlineState | None:
        return self.deadline_state_at(self.captured_at)

    def deadline_state_at(self, evaluated_at: datetime) -> TradeDeadlineState | None:
        """Compare time to the observed deadline without claiming trades are open."""

        evaluated = _aware("evaluated_at", evaluated_at)
        if self.trade_deadline_at is None:
            return None
        return (
            TradeDeadlineState.BEFORE_DEADLINE
            if evaluated < self.trade_deadline_at
            else TradeDeadlineState.DEADLINE_REACHED
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "absent_fields": _enum_values(self.absent_fields),
            "asset_statuses": [row.to_record() for row in self.asset_statuses],
            "captured_at": _timestamp(self.captured_at),
            "host_snapshot_id": self.host_snapshot_id,
            "league_binding_id": self.league_binding_id,
            "legality_scope": HOST_TRANSACTION_POLICY_SCOPE,
            "lineup_locktime_type_source_value": (
                self.lineup_locktime_type_source_value
            ),
            "pending_status_scope": HOST_PENDING_STATUS_SCOPE,
            "revision_hours_source_value": self.revision_hours_source_value,
            "roster_locktime_type_source_value": (
                self.roster_locktime_type_source_value
            ),
            "season": self.season,
            "semantics_version": HOST_TRANSACTION_POLICY_SEMANTICS_VERSION,
            "source_adapter_version": self.source_adapter_version,
            "source_provider": self.source_provider,
            "trade_deadline_at": (
                None if self.trade_deadline_at is None else _timestamp(self.trade_deadline_at)
            ),
            "undroppable_list_enabled": self.undroppable_list_enabled,
            "unsupported_fields": _enum_values(self.unsupported_fields),
            "veto_votes_required": self.veto_votes_required,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "host_transaction_policy",
            "schema_version": HOST_TRANSACTION_POLICY_SCHEMA_VERSION,
            **self._content_record(),
            "policy_id": self.policy_id,
        }

    @classmethod
    def from_record(cls, record: object) -> "HostTransactionPolicy":
        fields = {
            "absent_fields", "asset_statuses", "captured_at", "host_snapshot_id",
            "kind", "league_binding_id", "legality_scope",
            "lineup_locktime_type_source_value", "pending_status_scope", "policy_id",
            "revision_hours_source_value", "roster_locktime_type_source_value",
            "schema_version", "season", "semantics_version", "source_adapter_version",
            "source_provider", "trade_deadline_at", "undroppable_list_enabled",
            "unsupported_fields", "veto_votes_required",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("host transaction policy fields are invalid")
        if (
            record["kind"] != "host_transaction_policy"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != HOST_TRANSACTION_POLICY_SCHEMA_VERSION
            or record["semantics_version"] != HOST_TRANSACTION_POLICY_SEMANTICS_VERSION
            or record["legality_scope"] != HOST_TRANSACTION_POLICY_SCOPE
            or record["pending_status_scope"] != HOST_PENDING_STATUS_SCOPE
            or not isinstance(record["asset_statuses"], list)
        ):
            raise ValueError("host transaction policy header is invalid")
        deadline = record["trade_deadline_at"]
        value = cls(
            league_binding_id=record["league_binding_id"],
            season=record["season"],
            source_provider=record["source_provider"],
            source_adapter_version=record["source_adapter_version"],
            host_snapshot_id=record["host_snapshot_id"],
            captured_at=_parse_time("captured_at", record["captured_at"]),
            trade_deadline_at=(
                None if deadline is None else _parse_time("trade_deadline_at", deadline)
            ),
            revision_hours_source_value=record["revision_hours_source_value"],
            veto_votes_required=record["veto_votes_required"],
            undroppable_list_enabled=record["undroppable_list_enabled"],
            lineup_locktime_type_source_value=record[
                "lineup_locktime_type_source_value"
            ],
            roster_locktime_type_source_value=record[
                "roster_locktime_type_source_value"
            ],
            asset_statuses=tuple(
                HostAssetTransactionStatus.from_record(row)
                for row in record["asset_statuses"]
            ),
            absent_fields=_enum_record(record["absent_fields"], HostPolicyField),
            unsupported_fields=_enum_record(
                record["unsupported_fields"], HostPolicyField
            ),
        )
        if value.policy_id != record["policy_id"]:
            raise ValueError("host transaction policy content does not match policy_id")
        return value


@dataclass(frozen=True, slots=True)
class HostTransactionPolicyAttempt:
    """Typed result of one optional host-policy acquisition attempt."""

    league_binding_id: str
    season: int
    source_provider: str
    source_adapter_version: str
    host_snapshot_id: str
    attempted_at: datetime
    status: HostTransactionPolicyStatus | str
    reason_code: HostTransactionPolicyReason | str
    policy: HostTransactionPolicy | None = None
    attempt_id: str = field(init=False)

    def __post_init__(self) -> None:
        binding = _text("league_binding_id", self.league_binding_id)
        if not _LEAGUE_BINDING_ID.fullmatch(binding):
            raise ValueError("league_binding_id must be an opaque local binding")
        object.__setattr__(self, "league_binding_id", binding)
        _optional_int("season", self.season, minimum=2012, required=True)
        provider = _text("source_provider", self.source_provider).casefold()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", provider):
            raise ValueError("source_provider is invalid")
        object.__setattr__(self, "source_provider", provider)
        for name in ("source_adapter_version", "host_snapshot_id"):
            object.__setattr__(self, name, _text(name, getattr(self, name)))
        object.__setattr__(
            self, "attempted_at", _aware("attempted_at", self.attempted_at)
        )
        try:
            status = HostTransactionPolicyStatus(self.status)
            reason = HostTransactionPolicyReason(self.reason_code)
        except (TypeError, ValueError):
            raise ValueError("host transaction policy attempt status is invalid") from None
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", reason)
        has_policy = isinstance(self.policy, HostTransactionPolicy)
        if status is HostTransactionPolicyStatus.FIELD_COVERAGE_COMPLETE:
            valid = (
                reason is HostTransactionPolicyReason.FIELD_COVERAGE_COMPLETE
                and has_policy
                and self.policy.field_coverage_complete
            )
        elif status is HostTransactionPolicyStatus.PARTIAL:
            valid = (
                reason is HostTransactionPolicyReason.SOURCE_FIELDS_PARTIAL
                and has_policy
                and not self.policy.field_coverage_complete
            )
        else:
            valid = (
                reason
                in {
                    HostTransactionPolicyReason.SOURCE_SCHEMA_UNSUPPORTED,
                    HostTransactionPolicyReason.IDENTITY_MISMATCH,
                }
                and self.policy is None
            )
        if not valid:
            raise ValueError("host transaction policy attempt evidence is inconsistent")
        if has_policy and (
            self.policy.league_binding_id != binding
            or self.policy.season != self.season
            or self.policy.source_provider != provider
            or self.policy.source_adapter_version != self.source_adapter_version
            or self.policy.host_snapshot_id != self.host_snapshot_id
            or self.policy.captured_at != self.attempted_at
        ):
            raise ValueError("host policy attempt does not match policy evidence")
        object.__setattr__(
            self, "attempt_id", content_id("hostpolicyattempt", self._content_record())
        )

    def _content_record(self) -> dict[str, object]:
        return {
            "attempted_at": _timestamp(self.attempted_at),
            "host_snapshot_id": self.host_snapshot_id,
            "league_binding_id": self.league_binding_id,
            "policy": None if self.policy is None else self.policy.to_record(),
            "reason_code": self.reason_code.value,
            "season": self.season,
            "source_adapter_version": self.source_adapter_version,
            "source_provider": self.source_provider,
            "status": self.status.value,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "kind": "host_transaction_policy_attempt",
            "schema_version": HOST_TRANSACTION_POLICY_SCHEMA_VERSION,
            **self._content_record(),
            "attempt_id": self.attempt_id,
        }

    @classmethod
    def from_record(cls, record: object) -> "HostTransactionPolicyAttempt":
        fields = {
            "attempt_id", "attempted_at", "host_snapshot_id", "kind",
            "league_binding_id", "policy", "reason_code", "schema_version", "season",
            "source_adapter_version", "source_provider", "status",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("host transaction policy attempt fields are invalid")
        if (
            record["kind"] != "host_transaction_policy_attempt"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != HOST_TRANSACTION_POLICY_SCHEMA_VERSION
        ):
            raise ValueError("host transaction policy attempt header is invalid")
        policy_record = record["policy"]
        value = cls(
            league_binding_id=record["league_binding_id"],
            season=record["season"],
            source_provider=record["source_provider"],
            source_adapter_version=record["source_adapter_version"],
            host_snapshot_id=record["host_snapshot_id"],
            attempted_at=_parse_time("attempted_at", record["attempted_at"]),
            status=record["status"],
            reason_code=record["reason_code"],
            policy=(
                None
                if policy_record is None
                else HostTransactionPolicy.from_record(policy_record)
            ),
        )
        if value.attempt_id != record["attempt_id"]:
            raise ValueError("host policy attempt content does not match attempt_id")
        return value


def _validate_gaps(field_type, absent, unsupported, values) -> None:
    if absent & unsupported:
        raise ValueError("absent and unsupported fields must be disjoint")
    if not (absent | unsupported) <= set(field_type):
        raise ValueError("field gaps contain an unknown field")
    missing = {source_field for source_field, value in values.items() if value is None}
    if missing != absent | unsupported:
        raise ValueError("field gaps conflict with observed values")


def _enum_set(name, value, enum_type):
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must contain field names")
    try:
        return frozenset(enum_type(item) for item in value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} contains an invalid field") from None


def _enum_record(value, enum_type):
    if not isinstance(value, list):
        raise ValueError("field gaps must be arrays")
    try:
        parsed = tuple(enum_type(item) for item in value)
    except (TypeError, ValueError):
        raise ValueError("field gaps contain an invalid field") from None
    if len(parsed) != len(set(parsed)):
        raise ValueError("field gaps must not contain duplicate fields")
    return frozenset(parsed)


def _enum_values(values) -> list[str]:
    return sorted(value.value for value in values)


def _text(name, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _source_mode(name, value):
    value = _text(name, value)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        raise ValueError(f"{name} is invalid")
    return value


def _optional_int(name, value, *, minimum, required=False):
    if value is None and not required:
        return None
    if type(value) is not int or not minimum <= value <= SAFE_INTEGER:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _tuple(name, value):
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        return tuple(value)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None


def _typed_tuple(name, value, item_type):
    rows = _tuple(name, value)
    if any(not isinstance(row, item_type) for row in rows):
        raise ValueError(f"{name} contains invalid values")
    return rows


def _aware(name, value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return value.isoformat(timespec="microseconds")


def _parse_time(name, value):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from None
    return _aware(name, parsed)


__all__ = (
    "HOST_PENDING_STATUS_SCOPE",
    "HOST_TRANSACTION_POLICY_SCHEMA_VERSION",
    "HOST_TRANSACTION_POLICY_SEMANTICS_VERSION",
    "HOST_TRANSACTION_POLICY_SCOPE",
    "HostAssetField",
    "HostAssetTransactionStatus",
    "HostPolicyField",
    "HostTransactionPolicy",
    "HostTransactionPolicyAttempt",
    "HostTransactionPolicyReason",
    "HostTransactionPolicyStatus",
    "TradeDeadlineState",
)
