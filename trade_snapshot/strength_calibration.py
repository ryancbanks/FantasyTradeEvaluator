"""Typed, immutable evidence and coefficients for roster-strength calibration."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from numbers import Real
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import urlsplit


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RoleKind(str, Enum):
    STARTER = "starter"
    DEPTH = "depth"


class CalibrationStatus(str, Enum):
    UNVALIDATED = "unvalidated"
    SURROGATE = "surrogate"
    EXACT = "exact"


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """One distinct scored role and the positions eligible to occupy it."""

    role_id: str
    kind: RoleKind
    source_slot: str
    eligible_positions: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_id", _nonempty_string("role_id", self.role_id))
        if not isinstance(self.kind, RoleKind):
            raise ValueError("kind must be a RoleKind")
        object.__setattr__(
            self,
            "source_slot",
            _nonempty_string("source_slot", self.source_slot),
        )
        positions = _normalized_names("eligible_positions", self.eligible_positions)
        object.__setattr__(self, "eligible_positions", frozenset(positions))

    def to_record(self) -> dict[str, object]:
        return {
            "role_id": self.role_id,
            "kind": self.kind.value,
            "source_slot": self.source_slot,
            "eligible_positions": sorted(self.eligible_positions),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "RoleDefinition":
        _require_exact_fields(
            "role definition",
            record,
            {"role_id", "kind", "source_slot", "eligible_positions"},
        )
        try:
            kind = RoleKind(record["kind"])
        except (TypeError, ValueError):
            raise ValueError("role definition kind is invalid") from None
        return cls(
            role_id=record["role_id"],
            kind=kind,
            source_slot=record["source_slot"],
            eligible_positions=_record_string_set(
                "eligible_positions",
                record["eligible_positions"],
            ),
        )


@dataclass(frozen=True, slots=True)
class CalibrationMetadata:
    """Public source fingerprint and held-out evidence for one calibration."""

    analyzer_bundle_url: str
    analyzer_bundle_sha256: str
    response_schema_sha256: str
    captured_at: datetime
    status: CalibrationStatus = CalibrationStatus.UNVALIDATED
    held_out_trade_count: int = 0
    max_absolute_score_error: float | None = None
    display_match_rate: float | None = None
    evidence_id: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_public_url(self.analyzer_bundle_url)
        _validate_sha256("analyzer_bundle_sha256", self.analyzer_bundle_sha256)
        _validate_sha256("response_schema_sha256", self.response_schema_sha256)
        if (
            not isinstance(self.captured_at, datetime)
            or self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() is None
        ):
            raise ValueError("captured_at must be a timezone-aware datetime")
        if not isinstance(self.status, CalibrationStatus):
            raise ValueError("status must be a CalibrationStatus")
        if (
            isinstance(self.held_out_trade_count, bool)
            or not isinstance(self.held_out_trade_count, int)
            or self.held_out_trade_count < 0
        ):
            raise ValueError("held_out_trade_count must be a non-negative integer")

        error = _optional_nonnegative(
            "max_absolute_score_error",
            self.max_absolute_score_error,
        )
        match_rate = _optional_nonnegative("display_match_rate", self.display_match_rate)
        if match_rate is not None and match_rate > 1:
            raise ValueError("display_match_rate must be between 0 and 1")
        object.__setattr__(self, "max_absolute_score_error", error)
        object.__setattr__(self, "display_match_rate", match_rate)

        has_metrics = error is not None or match_rate is not None
        if self.held_out_trade_count == 0 and has_metrics:
            raise ValueError("held-out metrics require at least one held-out trade")
        if self.held_out_trade_count > 0 and (error is None or match_rate is None):
            raise ValueError("held-out trade evidence requires both validation metrics")
        if self.status is CalibrationStatus.UNVALIDATED and self.held_out_trade_count != 0:
            raise ValueError("unvalidated calibration cannot claim held-out evidence")
        if self.status is CalibrationStatus.EXACT and (
            self.held_out_trade_count == 0 or error > 1e-6 or match_rate != 1.0
        ):
            raise ValueError(
                "exact calibration requires held-out error <= 1e-6 and 100% display matches"
            )

        record = {
            "analyzer_bundle_sha256": self.analyzer_bundle_sha256,
            "analyzer_bundle_url": self.analyzer_bundle_url,
            "captured_at": self.captured_at.isoformat(),
            "display_match_rate": match_rate,
            "held_out_trade_count": self.held_out_trade_count,
            "max_absolute_score_error": error,
            "response_schema_sha256": self.response_schema_sha256,
            "status": self.status.value,
        }
        object.__setattr__(self, "evidence_id", _content_id("calibration", record))

    def to_record(self) -> dict[str, object]:
        return {
            "analyzer_bundle_url": self.analyzer_bundle_url,
            "analyzer_bundle_sha256": self.analyzer_bundle_sha256,
            "response_schema_sha256": self.response_schema_sha256,
            "captured_at": self.captured_at.isoformat(),
            "status": self.status.value,
            "held_out_trade_count": self.held_out_trade_count,
            "max_absolute_score_error": self.max_absolute_score_error,
            "display_match_rate": self.display_match_rate,
            "evidence_id": self.evidence_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "CalibrationMetadata":
        _require_exact_fields(
            "calibration metadata",
            record,
            {
                "analyzer_bundle_url",
                "analyzer_bundle_sha256",
                "response_schema_sha256",
                "captured_at",
                "status",
                "held_out_trade_count",
                "max_absolute_score_error",
                "display_match_rate",
                "evidence_id",
            },
        )
        try:
            captured_at = datetime.fromisoformat(record["captured_at"])
        except (TypeError, ValueError):
            raise ValueError("calibration captured_at must be an ISO datetime") from None
        try:
            status = CalibrationStatus(record["status"])
        except (TypeError, ValueError):
            raise ValueError("calibration status is invalid") from None
        metadata = cls(
            analyzer_bundle_url=record["analyzer_bundle_url"],
            analyzer_bundle_sha256=record["analyzer_bundle_sha256"],
            response_schema_sha256=record["response_schema_sha256"],
            captured_at=captured_at,
            status=status,
            held_out_trade_count=record["held_out_trade_count"],
            max_absolute_score_error=record["max_absolute_score_error"],
            display_match_rate=record["display_match_rate"],
        )
        if record["evidence_id"] != metadata.evidence_id:
            raise ValueError("calibration metadata does not match evidence_id")
        return metadata


@dataclass(frozen=True, slots=True)
class PlayerStrength:
    """One player's residual and complete scores for every eligible model role."""

    player_id: str
    residual_score: float
    eligible_positions: frozenset[str]
    assignment_score_by_role: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "player_id", _nonempty_string("player_id", self.player_id))
        object.__setattr__(
            self,
            "residual_score",
            _finite_number("residual_score", self.residual_score),
        )
        positions = _normalized_names("eligible_positions", self.eligible_positions)
        object.__setattr__(self, "eligible_positions", frozenset(positions))
        if not isinstance(self.assignment_score_by_role, Mapping):
            raise ValueError("assignment_score_by_role must be a mapping")
        role_scores: dict[str, float] = {}
        for role, value in self.assignment_score_by_role.items():
            role_id = _nonempty_string("roster role", role)
            score = _finite_number("role assignment score", value)
            if score < 0:
                raise ValueError("role assignment scores must be non-negative")
            role_scores[role_id] = score
        object.__setattr__(
            self,
            "assignment_score_by_role",
            MappingProxyType(role_scores),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "residual_score": self.residual_score,
            "eligible_positions": sorted(self.eligible_positions),
            "assignment_score_by_role": dict(sorted(self.assignment_score_by_role.items())),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PlayerStrength":
        _require_exact_fields(
            "player strength",
            record,
            {
                "player_id",
                "residual_score",
                "eligible_positions",
                "assignment_score_by_role",
            },
        )
        scores = record["assignment_score_by_role"]
        if not isinstance(scores, Mapping):
            raise ValueError("assignment_score_by_role must be a mapping")
        return cls(
            player_id=record["player_id"],
            residual_score=record["residual_score"],
            eligible_positions=_record_string_set(
                "eligible_positions",
                record["eligible_positions"],
            ),
            assignment_score_by_role=scores,
        )


def _normalized_names(name: str, values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection of non-empty strings")
    try:
        normalized = tuple(values)
    except TypeError:
        raise ValueError(f"{name} must be a collection of non-empty strings") from None
    if not normalized or any(not isinstance(value, str) or not value for value in normalized):
        raise ValueError(f"{name} must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} cannot contain duplicates")
    return normalized


def _nonempty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(normalized):
        raise ValueError(f"{name} must be a finite number")
    return normalized


def _optional_nonnegative(name: str, value: object) -> float | None:
    if value is None:
        return None
    normalized = _finite_number(name, value)
    if normalized < 0:
        raise ValueError(f"{name} must be non-negative")
    return normalized


def _validate_public_url(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("analyzer_bundle_url must be a public HTTPS URL")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("analyzer_bundle_url must be a public HTTPS URL without query data")


def _validate_sha256(name: str, value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _content_id(prefix: str, record: Mapping[str, object]) -> str:
    encoded = json.dumps(
        record,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{prefix}-v1-{sha256(encoded).hexdigest()}"


def _require_exact_fields(name: str, record: object, expected: set[str]) -> None:
    if not isinstance(record, Mapping):
        raise ValueError(f"{name} must be a mapping")
    if set(record) != expected:
        raise ValueError(f"{name} has missing or unknown fields")


def _record_string_set(name: str, value: object) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a list of strings")
    try:
        values = tuple(value)
    except TypeError:
        raise ValueError(f"{name} must be a list of strings") from None
    return frozenset(_normalized_names(name, values))
