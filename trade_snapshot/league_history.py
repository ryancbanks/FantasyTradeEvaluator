"""Append-only, privacy-bounded league history for General Manager Insights.

This store deliberately has no dependency on ``EngineBundle``.  Collection
adapters provide canonical team/player IDs and retain provider league/member
identifiers only long enough to derive pseudonymous keys outside this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from math import isfinite
from numbers import Real
import re
from typing import Iterable, Mapping

from ._scenario_random import content_id
from ._league_history_acquisition import (
    HistoryAcquisitionEvidence,
    HistoryAcquisitionOutcome,
    HistorySkipCount,
)
from ._league_history_capture_record import roster_ownership_id
from ._league_history_schema import (
    LEAGUE_HISTORY_APPLICATION_ID,
    LEAGUE_HISTORY_SCHEMA_VERSION,
)


HISTORY_CAPTURE_BINDING_TOLERANCE = timedelta(hours=1)

_LEAGUE_KEY = re.compile(r"^league_[0-9a-f]{32}(?:[0-9a-f]{32})?$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_UNSAFE_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|\bwww\.)", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?:authorization|cookie|password|secret|sessionid|token)\s*[:=]",
    re.IGNORECASE,
)
_SOURCE_ASSET_KEY = re.compile(r"^source_asset_[0-9a-f]{64}$")


class LeagueHistoryStoreError(RuntimeError):
    """A corrupt, incompatible, or inaccessible league-history database."""


class LeagueHistoryConflictError(LeagueHistoryStoreError):
    """An immutable history identity was reused with different content."""


class HistoryTransactionKind(str, Enum):
    TRADE = "trade"
    WAIVER = "waiver"
    FREE_AGENT = "free_agent"
    DROP = "drop"
    COMMISSIONER = "commissioner"


class HistoryTimestampBasis(str, Enum):
    """What the provider timestamp on an executed transaction represents."""

    EXECUTED_AT = "executed_at"
    ESPN_PROPOSED_DATE = "espn_proposed_date"


class HistoryTransactionAssetKind(str, Enum):
    PLAYER = "player"
    UNSUPPORTED_NON_PLAYER = "unsupported_non_player"


@dataclass(frozen=True, slots=True)
class HistoryTeam:
    team_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _identifier("team_id", self.team_id))
        object.__setattr__(self, "name", _label("team name", self.name))

    def to_record(self) -> dict[str, object]:
        return {"team_id": self.team_id, "name": self.name}

    @classmethod
    def from_record(cls, value: object) -> "HistoryTeam":
        row = _record(value, {"team_id", "name"}, "history team")
        return cls(row["team_id"], row["name"])


@dataclass(frozen=True, slots=True)
class HistoryRosterPlayer:
    canonical_player_id: str
    lineup_slot: str | None = None
    injury_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_player_id",
            _identifier("canonical_player_id", self.canonical_player_id),
        )
        if self.lineup_slot is not None:
            object.__setattr__(
                self, "lineup_slot", _identifier("lineup_slot", self.lineup_slot)
            )
        if self.injury_status is not None:
            object.__setattr__(
                self,
                "injury_status",
                _identifier("injury_status", self.injury_status).upper(),
            )

    def to_record(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "injury_status": self.injury_status,
            "lineup_slot": self.lineup_slot,
        }

    @classmethod
    def from_record(cls, value: object) -> "HistoryRosterPlayer":
        row = _record(
            value,
            {"canonical_player_id", "injury_status", "lineup_slot"},
            "history roster player",
        )
        return cls(
            row["canonical_player_id"],
            row["lineup_slot"],
            row["injury_status"],
        )


@dataclass(frozen=True, slots=True)
class HistoryTeamRoster:
    team_id: str
    players: tuple[HistoryRosterPlayer, ...]

    def __post_init__(self) -> None:
        team_id = _identifier("roster team_id", self.team_id)
        players = _typed_tuple("roster players", self.players, HistoryRosterPlayer)
        players = tuple(sorted(players, key=lambda row: row.canonical_player_id))
        player_ids = tuple(row.canonical_player_id for row in players)
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("history roster contains a duplicate player")
        object.__setattr__(self, "team_id", team_id)
        object.__setattr__(self, "players", players)

    def to_record(self) -> dict[str, object]:
        return {
            "team_id": self.team_id,
            "players": [row.to_record() for row in self.players],
        }

    @classmethod
    def from_record(cls, value: object) -> "HistoryTeamRoster":
        row = _record(value, {"team_id", "players"}, "history team roster")
        return cls(
            row["team_id"],
            tuple(
                HistoryRosterPlayer.from_record(item)
                for item in _array("roster players", row["players"])
            ),
        )


@dataclass(frozen=True, slots=True)
class HistoryTransactionAsset:
    asset_index: int
    canonical_player_id: str | None
    from_team_id: str | None
    to_team_id: str | None
    source_asset_key: str | None = None
    asset_kind: HistoryTransactionAssetKind = HistoryTransactionAssetKind.PLAYER

    def __post_init__(self) -> None:
        if type(self.asset_index) is not int or self.asset_index < 0:
            raise ValueError("asset_index must be a non-negative integer")
        for name in ("canonical_player_id", "from_team_id", "to_team_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _identifier(name, value))
        if self.source_asset_key is not None:
            if not isinstance(self.source_asset_key, str) or not _SOURCE_ASSET_KEY.fullmatch(
                self.source_asset_key
            ):
                raise ValueError(
                    "source_asset_key must be a privacy-safe source asset SHA-256 key"
                )
        try:
            asset_kind = HistoryTransactionAssetKind(self.asset_kind)
        except (TypeError, ValueError):
            raise ValueError("transaction asset_kind is unsupported") from None
        if (
            asset_kind is HistoryTransactionAssetKind.UNSUPPORTED_NON_PLAYER
            and self.canonical_player_id is not None
        ):
            raise ValueError("an unsupported non-player asset cannot resolve to a player")
        if self.from_team_id is None and self.to_team_id is None:
            raise ValueError("a transaction asset must have a source or destination team")
        object.__setattr__(self, "asset_kind", asset_kind)

    def to_record(self) -> dict[str, object]:
        record = {
            "asset_index": self.asset_index,
            "canonical_player_id": self.canonical_player_id,
            "from_team_id": self.from_team_id,
            "to_team_id": self.to_team_id,
        }
        # Preserve the exact legacy record shape when both additions are at
        # their defaults, so captures written by schema v1 still recompute to
        # the same content ID when reopened.
        if self.source_asset_key is not None:
            record["source_asset_key"] = self.source_asset_key
        if (
            self.source_asset_key is not None
            or self.asset_kind is not HistoryTransactionAssetKind.PLAYER
        ):
            record["asset_kind"] = self.asset_kind.value
        return record

    @classmethod
    def from_record(cls, value: object) -> "HistoryTransactionAsset":
        legacy_fields = {
            "asset_index",
            "canonical_player_id",
            "from_team_id",
            "to_team_id",
        }
        if not isinstance(value, Mapping):
            raise ValueError("history transaction asset fields are invalid")
        fields = set(value)
        if fields == legacy_fields:
            row = value
            source_asset_key = None
            asset_kind = HistoryTransactionAssetKind.PLAYER
        elif fields == legacy_fields | {"asset_kind"}:
            row = value
            source_asset_key = None
            asset_kind = row["asset_kind"]
        elif fields == legacy_fields | {"source_asset_key", "asset_kind"}:
            row = value
            source_asset_key = row["source_asset_key"]
            asset_kind = row["asset_kind"]
        else:
            raise ValueError("history transaction asset fields are invalid")
        return cls(
            row["asset_index"],
            row["canonical_player_id"],
            row["from_team_id"],
            row["to_team_id"],
            source_asset_key,
            asset_kind,
        )


@dataclass(frozen=True, slots=True)
class HistoryTransaction:
    transaction_id: str
    recorded_at: datetime
    timestamp_basis: HistoryTimestampBasis
    effective_week: int
    kind: HistoryTransactionKind
    assets: tuple[HistoryTransactionAsset, ...]
    bid_amount: float | None = None
    accepted_at: datetime | None = None
    processed_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        transaction_id = _identifier("transaction_id", self.transaction_id)
        recorded_at = _aware_datetime("recorded_at", self.recorded_at)
        try:
            timestamp_basis = HistoryTimestampBasis(self.timestamp_basis)
        except (TypeError, ValueError):
            raise ValueError("transaction timestamp_basis is unsupported") from None
        if type(self.effective_week) is not int or self.effective_week < 0:
            raise ValueError("effective_week must be a non-negative integer")
        try:
            kind = HistoryTransactionKind(self.kind)
        except (TypeError, ValueError):
            raise ValueError("transaction kind is unsupported") from None
        assets = _typed_tuple("transaction assets", self.assets, HistoryTransactionAsset)
        if not assets:
            raise ValueError("an executed transaction must contain at least one asset")
        assets = tuple(sorted(assets, key=lambda row: row.asset_index))
        if tuple(row.asset_index for row in assets) != tuple(range(len(assets))):
            raise ValueError("transaction asset indexes must be contiguous from zero")
        known_players = tuple(
            row.canonical_player_id
            for row in assets
            if row.canonical_player_id is not None
        )
        if len(set(known_players)) != len(known_players):
            raise ValueError("a transaction contains a duplicate canonical player")
        source_asset_keys = tuple(
            row.source_asset_key
            for row in assets
            if row.source_asset_key is not None
        )
        if len(set(source_asset_keys)) != len(source_asset_keys):
            raise ValueError("a transaction contains a duplicate source asset key")
        bid_amount = _optional_nonnegative_number("bid_amount", self.bid_amount)
        accepted_at = _optional_aware_datetime("accepted_at", self.accepted_at)
        processed_at = _optional_aware_datetime("processed_at", self.processed_at)
        expires_at = _optional_aware_datetime("expires_at", self.expires_at)
        _validate_asset_movements(kind, assets)
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "timestamp_basis", timestamp_basis)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "assets", assets)
        object.__setattr__(self, "bid_amount", bid_amount)
        object.__setattr__(self, "accepted_at", accepted_at)
        object.__setattr__(self, "processed_at", processed_at)
        object.__setattr__(self, "expires_at", expires_at)

    @property
    def participant_team_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    team_id
                    for asset in self.assets
                    for team_id in (asset.from_team_id, asset.to_team_id)
                    if team_id is not None
                }
            )
        )

    @property
    def source_event_at(self) -> datetime:
        """Provider time with semantics named by ``timestamp_basis``."""

        return self.recorded_at

    def to_record(self) -> dict[str, object]:
        record = {
            "transaction_id": self.transaction_id,
            "execution_status": "executed",
            "recorded_at": _timestamp(self.recorded_at),
            "timestamp_basis": self.timestamp_basis.value,
            "effective_week": self.effective_week,
            "kind": self.kind.value,
            "assets": [row.to_record() for row in self.assets],
            "bid_amount": self.bid_amount,
        }
        # Retain the legacy shape when no source supplied any of these fields;
        # this keeps existing content-addressed capture identities stable.
        if any(
            value is not None
            for value in (self.accepted_at, self.processed_at, self.expires_at)
        ):
            record.update(
                {
                    "accepted_at": _optional_timestamp(self.accepted_at),
                    "processed_at": _optional_timestamp(self.processed_at),
                    "expires_at": _optional_timestamp(self.expires_at),
                }
            )
        return record

    @classmethod
    def from_record(cls, value: object) -> "HistoryTransaction":
        legacy_fields = {
            "transaction_id",
            "execution_status",
            "recorded_at",
            "timestamp_basis",
            "effective_week",
            "kind",
            "assets",
            "bid_amount",
        }
        extended_fields = legacy_fields | {
            "accepted_at",
            "processed_at",
            "expires_at",
        }
        if not isinstance(value, Mapping):
            raise ValueError("history transaction fields are invalid")
        fields = set(value)
        if fields != legacy_fields and fields != extended_fields:
            raise ValueError("history transaction fields are invalid")
        row = value
        if row["execution_status"] != "executed":
            raise ValueError("league history accepts executed transactions only")
        return cls(
            row["transaction_id"],
            _datetime_from_record("recorded_at", row["recorded_at"]),
            row["timestamp_basis"],
            row["effective_week"],
            row["kind"],
            tuple(
                HistoryTransactionAsset.from_record(item)
                for item in _array("transaction assets", row["assets"])
            ),
            row["bid_amount"],
            _optional_datetime_from_record("accepted_at", row.get("accepted_at")),
            _optional_datetime_from_record("processed_at", row.get("processed_at")),
            _optional_datetime_from_record("expires_at", row.get("expires_at")),
        )


@dataclass(frozen=True, slots=True)
class LeagueHistoryCapture:
    league_key: str
    season: int
    captured_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    transaction_history_complete: bool
    roster_complete: bool
    lineup_complete: bool
    teams: tuple[HistoryTeam, ...]
    transactions: tuple[HistoryTransaction, ...]
    rosters: tuple[HistoryTeamRoster, ...]
    host_snapshot_id: str | None = None
    acquisition_evidence: HistoryAcquisitionEvidence | None = None
    identity_schema_version: int = field(
        default=LEAGUE_HISTORY_SCHEMA_VERSION, repr=False
    )
    capture_id: str = field(init=False)
    roster_ownership_id: str = field(init=False)

    def __post_init__(self) -> None:
        league_key = _league_key(self.league_key)
        season = _season(self.season)
        captured_at = _aware_datetime("captured_at", self.captured_at)
        coverage_start = _aware_datetime("coverage_start", self.coverage_start)
        coverage_end = _aware_datetime("coverage_end", self.coverage_end)
        if coverage_start > coverage_end or coverage_end > captured_at:
            raise ValueError(
                "history coverage must be ordered and cannot end after capture"
            )
        for name in (
            "transaction_history_complete",
            "roster_complete",
            "lineup_complete",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.lineup_complete and not self.roster_complete:
            raise ValueError("lineup_complete requires roster_complete")
        host_snapshot_id = (
            None
            if self.host_snapshot_id is None
            else _identifier("host_snapshot_id", self.host_snapshot_id)
        )
        if self.identity_schema_version not in (1, LEAGUE_HISTORY_SCHEMA_VERSION):
            raise ValueError("history capture identity schema version is unsupported")

        teams = _typed_tuple("history teams", self.teams, HistoryTeam)
        teams = tuple(sorted(teams, key=lambda row: row.team_id))
        team_ids = tuple(row.team_id for row in teams)
        if len(teams) < 2 or len(set(team_ids)) != len(team_ids):
            raise ValueError("history teams must contain at least two unique teams")
        transactions = _typed_tuple(
            "history transactions", self.transactions, HistoryTransaction
        )
        transactions = tuple(
            sorted(transactions, key=lambda row: (row.recorded_at, row.transaction_id))
        )
        transaction_ids = tuple(row.transaction_id for row in transactions)
        if len(set(transaction_ids)) != len(transaction_ids):
            raise ValueError("history capture contains a duplicate transaction_id")
        known_teams = set(team_ids)
        for transaction in transactions:
            if not coverage_start <= transaction.recorded_at <= coverage_end:
                raise ValueError("transaction is outside the stated history coverage")
            if not set(transaction.participant_team_ids).issubset(known_teams):
                raise ValueError("transaction references an unknown team")

        rosters = _typed_tuple("history rosters", self.rosters, HistoryTeamRoster)
        rosters = tuple(sorted(rosters, key=lambda row: row.team_id))
        roster_team_ids = tuple(row.team_id for row in rosters)
        if len(set(roster_team_ids)) != len(roster_team_ids):
            raise ValueError("history capture contains duplicate team rosters")
        if not set(roster_team_ids).issubset(known_teams):
            raise ValueError("history roster references an unknown team")
        if self.roster_complete and set(roster_team_ids) != known_teams:
            raise ValueError("a complete history roster must contain every team")
        roster_players = tuple(
            player.canonical_player_id for roster in rosters for player in roster.players
        )
        if len(set(roster_players)) != len(roster_players):
            raise ValueError("a history player cannot belong to multiple teams")
        if self.lineup_complete and any(
            player.lineup_slot is None for roster in rosters for player in roster.players
        ):
            raise ValueError("a complete lineup must assign every rostered player a slot")
        event_times = tuple(row.recorded_at for row in transactions)
        acquisition = self.acquisition_evidence or (
            HistoryAcquisitionEvidence.legacy_unknown(
                captured_at,
                len(transactions),
                earliest_source_event_at=min(event_times, default=None),
                latest_source_event_at=max(event_times, default=None),
            )
        )
        if not isinstance(acquisition, HistoryAcquisitionEvidence):
            raise ValueError(
                "acquisition_evidence must be HistoryAcquisitionEvidence or None"
            )
        if acquisition.attempted_at != captured_at:
            raise ValueError("history acquisition does not match captured_at")
        if acquisition.normalized_transaction_count != len(transactions):
            raise ValueError("history acquisition does not match normalized transactions")
        if (
            acquisition.history_complete is not None
            and acquisition.history_complete != self.transaction_history_complete
        ):
            raise ValueError("history acquisition does not match coverage completeness")
        if acquisition.outcome is not HistoryAcquisitionOutcome.LEGACY_UNKNOWN:
            earliest = acquisition.earliest_source_event_at
            latest = acquisition.latest_source_event_at
            if event_times and (
                earliest is None
                or latest is None
                or min(event_times) < earliest
                or max(event_times) > latest
            ):
                raise ValueError(
                    "normalized history events fall outside the returned source bounds"
                )
        roster_id = roster_ownership_id(rosters)

        object.__setattr__(self, "league_key", league_key)
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "captured_at", captured_at)
        object.__setattr__(self, "coverage_start", coverage_start)
        object.__setattr__(self, "coverage_end", coverage_end)
        object.__setattr__(self, "teams", teams)
        object.__setattr__(self, "transactions", transactions)
        object.__setattr__(self, "rosters", rosters)
        object.__setattr__(self, "host_snapshot_id", host_snapshot_id)
        object.__setattr__(self, "acquisition_evidence", acquisition)
        object.__setattr__(
            self, "identity_schema_version", self.identity_schema_version
        )
        object.__setattr__(self, "roster_ownership_id", roster_id)
        object.__setattr__(
            self, "capture_id", content_id("history-capture", self._identity_record())
        )

    def _identity_record(self) -> dict[str, object]:
        """Use v2 provenance for new IDs while preserving migrated v1 IDs."""

        from ._league_history_capture_record import capture_identity_record

        return capture_identity_record(self)

    def _content_record(self) -> dict[str, object]:
        from ._league_history_capture_record import capture_content_record

        return capture_content_record(self)

    def to_record(self) -> dict[str, object]:
        return {**self._content_record(), "capture_id": self.capture_id}

    @classmethod
    def from_record(cls, value: object) -> "LeagueHistoryCapture":
        from ._league_history_capture_record import capture_from_record

        return capture_from_record(cls, value)


@dataclass(frozen=True, slots=True)
class HistoryBundleBinding:
    league_key: str
    season: int
    bundle_id: str
    captured_at: datetime
    host_snapshot_id: str | None = None
    host_captured_at: datetime | None = None
    history_capture_id: str | None = None
    roster_ownership_id: str | None = None

    def __post_init__(self) -> None:
        from ._league_history_binding import normalized_binding_fields

        for name, value in normalized_binding_fields(self).items():
            object.__setattr__(self, name, value)

    def to_record(self) -> dict[str, object]:
        from ._league_history_binding import binding_record

        return binding_record(self)

    @classmethod
    def from_record(cls, value: object) -> "HistoryBundleBinding":
        from ._league_history_binding import binding_from_record

        return binding_from_record(cls, value)


@dataclass(frozen=True, slots=True)
class LeagueHistorySnapshot:
    requested_binding: HistoryBundleBinding
    bundle_bindings: tuple[HistoryBundleBinding, ...]
    captures: tuple[LeagueHistoryCapture, ...]
    transactions: tuple[HistoryTransaction, ...] = field(init=False)
    latest_teams: tuple[HistoryTeam, ...] = field(init=False)
    history_revision: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.requested_binding, HistoryBundleBinding):
            raise ValueError("requested_binding must be a HistoryBundleBinding")
        identity = self.requested_binding.league_key, self.requested_binding.season
        bindings = _typed_tuple(
            "history bundle bindings", self.bundle_bindings, HistoryBundleBinding
        )
        bindings = tuple(sorted(bindings, key=lambda row: (row.captured_at, row.bundle_id)))
        if not bindings or self.requested_binding not in bindings:
            raise ValueError("history bindings must include the requested bundle")
        if any((row.league_key, row.season) != identity for row in bindings):
            raise ValueError("history bundle bindings do not share one league season")
        captures = _typed_tuple("history captures", self.captures, LeagueHistoryCapture)
        captures = tuple(sorted(captures, key=lambda row: (row.captured_at, row.capture_id)))
        if any((row.league_key, row.season) != identity for row in captures):
            raise ValueError("history captures do not share the requested league season")
        capture_by_id = {row.capture_id: row for row in captures}
        for binding in bindings:
            if binding.history_capture_id is None:
                continue
            bound_capture = capture_by_id.get(binding.history_capture_id)
            if bound_capture is None or (
                binding.host_snapshot_id != bound_capture.host_snapshot_id
                or binding.host_captured_at != bound_capture.captured_at
                or binding.roster_ownership_id
                != bound_capture.roster_ownership_id
            ):
                raise ValueError("exact history binding does not match its capture")
        from ._league_history_evidence import merge_transaction_versions

        by_transaction: dict[str, HistoryTransaction] = {}
        for capture in captures:
            for transaction in capture.transactions:
                previous = by_transaction.get(transaction.transaction_id)
                by_transaction[transaction.transaction_id] = (
                    transaction
                    if previous is None
                    else merge_transaction_versions(previous, transaction)
                )
        transactions = tuple(
            sorted(
                by_transaction.values(),
                key=lambda row: (row.recorded_at, row.transaction_id),
            )
        )
        latest_teams = captures[-1].teams if captures else ()
        revision_record = {
            "schema_version": LEAGUE_HISTORY_SCHEMA_VERSION,
            "league_key": identity[0],
            "season": identity[1],
            "bundle_bindings": [row.to_record() for row in bindings],
            "captures": [row.to_record() for row in captures],
        }
        object.__setattr__(self, "bundle_bindings", bindings)
        object.__setattr__(self, "captures", captures)
        object.__setattr__(self, "transactions", transactions)
        object.__setattr__(self, "latest_teams", latest_teams)
        object.__setattr__(
            self, "history_revision", content_id("history", revision_record)
        )

    @property
    def bundle_id(self) -> str:
        return self.requested_binding.bundle_id

    @property
    def league_key(self) -> str:
        return self.requested_binding.league_key

    @property
    def season(self) -> int:
        return self.requested_binding.season

    @property
    def bundle_captured_at(self) -> datetime:
        return self.requested_binding.captured_at

    @property
    def evidence_as_of(self) -> datetime:
        """Latest time this bundle is allowed to observe from mutable history."""

        return self.requested_binding.captured_at

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": LEAGUE_HISTORY_SCHEMA_VERSION,
            "bundle_id": self.bundle_id,
            "league_key": self.league_key,
            "season": self.season,
            "bundle_captured_at": _timestamp(self.bundle_captured_at),
            "evidence_as_of": _timestamp(self.evidence_as_of),
            "history_revision": self.history_revision,
            "bundle_bindings": [row.to_record() for row in self.bundle_bindings],
            "captures": [_snapshot_capture_record(row) for row in self.captures],
            "transactions": [row.to_record() for row in self.transactions],
            "latest_teams": [row.to_record() for row in self.latest_teams],
        }


def make_league_key(provider: str, source_league_id: str) -> str:
    """Return the legacy deterministic key used by pre-v2 local stores.

    New captures must use ``WeeklySourceManifest.league_binding_id`` so the
    history identity cannot be correlated from a known provider league ID.
    """

    if not isinstance(provider, str):
        raise ValueError("provider must be a lowercase identifier")
    normalized_provider = provider.strip().casefold()
    if not _PROVIDER.fullmatch(normalized_provider):
        raise ValueError("provider must be a lowercase identifier")
    source_id = _identifier("source_league_id", source_league_id)
    payload = f"{normalized_provider}\0{source_id}".encode("utf-8")
    return f"league_{sha256(payload).hexdigest()}"


def _validate_asset_movements(
    kind: HistoryTransactionKind, assets: tuple[HistoryTransactionAsset, ...]
) -> None:
    participants = {
        team_id
        for row in assets
        for team_id in (row.from_team_id, row.to_team_id)
        if team_id is not None
    }
    if kind is HistoryTransactionKind.TRADE:
        if len(participants) < 2 or any(
            row.from_team_id is None
            or row.to_team_id is None
            or row.from_team_id == row.to_team_id
            for row in assets
        ):
            raise ValueError("trade assets must move between at least two different teams")
    elif kind in {HistoryTransactionKind.WAIVER, HistoryTransactionKind.FREE_AGENT}:
        if len(participants) != 1 or any(
            (row.from_team_id is None) == (row.to_team_id is None) for row in assets
        ):
            raise ValueError(
                "waiver and free-agent assets must add or drop for exactly one team"
            )
    elif kind is HistoryTransactionKind.DROP:
        if len(participants) != 1 or any(
            row.from_team_id is None or row.to_team_id is not None for row in assets
        ):
            raise ValueError("drop assets must leave exactly one team")


def _snapshot_capture_record(capture: LeagueHistoryCapture) -> dict[str, object]:
    return {
        "acquisition_evidence": capture.acquisition_evidence.to_record(),
        "capture_id": capture.capture_id,
        "captured_at": _timestamp(capture.captured_at),
        "coverage_start": _timestamp(capture.coverage_start),
        "coverage_end": _timestamp(capture.coverage_end),
        "transaction_history_complete": capture.transaction_history_complete,
        "roster_complete": capture.roster_complete,
        "lineup_complete": capture.lineup_complete,
        "host_snapshot_id": capture.host_snapshot_id,
        "identity_schema_version": capture.identity_schema_version,
        "roster_ownership_id": capture.roster_ownership_id,
        "transaction_ids": [row.transaction_id for row in capture.transactions],
        "teams": [row.to_record() for row in capture.teams],
        "rosters": [row.to_record() for row in capture.rosters],
    }


def _league_key(value: object) -> str:
    if not isinstance(value, str) or not _LEAGUE_KEY.fullmatch(value):
        raise ValueError("league_key must be an opaque local league binding")
    return value


def _bundle_id(value: object) -> str:
    return _identifier("bundle_id", value)


def _season(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("season must be a positive integer")
    return value


def _optional_nonnegative_number(name: str, value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite non-negative number or None")
    result = float(value)
    if result < 0:
        raise ValueError(f"{name} must be a finite non-negative number or None")
    return result


def _identifier(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 for character in normalized)
        or _UNSAFE_URL.search(normalized)
        or _SECRET_ASSIGNMENT.search(normalized)
    ):
        raise ValueError(f"{name} contains unsafe or invalid text")
    return normalized


def _label(name: str, value: object) -> str:
    normalized = _identifier(name, value)
    if len(normalized) > 200:
        raise ValueError(f"{name} is too long")
    return normalized


def _aware_datetime(name: str, value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _optional_aware_datetime(name: str, value: object) -> datetime | None:
    return None if value is None else _aware_datetime(name, value)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _datetime_from_record(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from None
    result = _aware_datetime(name, parsed)
    if _timestamp(result) != value:
        raise ValueError(f"{name} must use the canonical UTC timestamp format")
    return result


def _optional_datetime_from_record(name: str, value: object) -> datetime | None:
    return None if value is None else _datetime_from_record(name, value)


def _typed_tuple(name: str, values: Iterable[object], item_type: type) -> tuple:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable")
    try:
        rows = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be an iterable") from None
    if any(not isinstance(row, item_type) for row in rows):
        raise ValueError(f"{name} must contain {item_type.__name__} values")
    return rows


def _record(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields are invalid")
    return value


def _array(name: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


from ._league_history_store import LeagueHistoryStore


__all__ = (
    "HISTORY_CAPTURE_BINDING_TOLERANCE",
    "LEAGUE_HISTORY_APPLICATION_ID",
    "LEAGUE_HISTORY_SCHEMA_VERSION",
    "HistoryAcquisitionEvidence",
    "HistoryAcquisitionOutcome",
    "HistoryBundleBinding",
    "HistoryRosterPlayer",
    "HistoryTeam",
    "HistoryTeamRoster",
    "HistoryTransaction",
    "HistoryTransactionAsset",
    "HistoryTransactionAssetKind",
    "HistoryTransactionKind",
    "HistoryTimestampBasis",
    "HistorySkipCount",
    "LeagueHistoryCapture",
    "LeagueHistoryConflictError",
    "LeagueHistorySnapshot",
    "LeagueHistoryStore",
    "LeagueHistoryStoreError",
    "make_league_key",
)
