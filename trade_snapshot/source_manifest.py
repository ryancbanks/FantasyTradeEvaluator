"""Privacy-safe provenance for the private league inputs in an engine bundle."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from collections.abc import Mapping
import re


_OPAQUE_BINDING = re.compile(r"^league_[0-9a-f]{32}(?:[0-9a-f]{32})?$")


class LeagueBindingScope(str, Enum):
    WORKSPACE = "workspace"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True, slots=True)
class WeeklySourceManifest:
    """Bind standings, rosters, rules, and matchups to their captured source."""

    league_binding_id: str
    league_binding_scope: LeagueBindingScope | str
    host_provider: str
    host_snapshot_id: str
    host_captured_at: datetime
    fantasypros_league_artifact_id: str
    fantasypros_captured_at: datetime
    completed_history_available: bool

    def __post_init__(self) -> None:
        for name in (
            "league_binding_id",
            "host_provider",
            "host_snapshot_id",
            "fantasypros_league_artifact_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
            object.__setattr__(self, name, value.strip())
        if not _OPAQUE_BINDING.fullmatch(self.league_binding_id):
            raise ValueError("league_binding_id must be an opaque local binding")
        try:
            scope = LeagueBindingScope(self.league_binding_scope)
        except (TypeError, ValueError):
            raise ValueError("league_binding_scope is invalid") from None
        object.__setattr__(self, "league_binding_scope", scope)
        for name in ("host_captured_at", "fantasypros_captured_at"):
            value = getattr(self, name)
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be a timezone-aware datetime")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if not isinstance(self.completed_history_available, bool):
            raise ValueError("completed_history_available must be a boolean")

    @classmethod
    def from_captures(
        cls,
        host_snapshot,
        fantasypros_league,
        *,
        league_binding_id: str | None = None,
    ) -> "WeeklySourceManifest":
        if league_binding_id is None:
            digest = sha256(host_snapshot.snapshot_id.encode("utf-8")).hexdigest()
            league_binding_id = f"league_{digest}"
            scope = LeagueBindingScope.SNAPSHOT
        else:
            scope = LeagueBindingScope.WORKSPACE
        return cls(
            league_binding_id,
            scope,
            host_snapshot.source_provider,
            host_snapshot.snapshot_id,
            host_snapshot.captured_at,
            fantasypros_league.artifact_id,
            _time(fantasypros_league.captured_at, "fantasypros captured_at"),
            host_snapshot.completed_matchups is not None,
        )

    def to_record(self) -> dict[str, object]:
        return {
            "completed_history_available": self.completed_history_available,
            "fantasypros_captured_at": self.fantasypros_captured_at.isoformat(
                timespec="microseconds"
            ),
            "fantasypros_league_artifact_id": self.fantasypros_league_artifact_id,
            "host_captured_at": self.host_captured_at.isoformat(timespec="microseconds"),
            "host_provider": self.host_provider,
            "host_snapshot_id": self.host_snapshot_id,
            "league_binding_id": self.league_binding_id,
            "league_binding_scope": self.league_binding_scope.value,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "WeeklySourceManifest":
        fields = {
            "completed_history_available",
            "fantasypros_captured_at",
            "fantasypros_league_artifact_id",
            "host_captured_at",
            "host_provider",
            "host_snapshot_id",
            "league_binding_id",
            "league_binding_scope",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("weekly source manifest fields are invalid")
        return cls(
            record["league_binding_id"],
            record["league_binding_scope"],
            record["host_provider"],
            record["host_snapshot_id"],
            _time(record["host_captured_at"], "host_captured_at"),
            record["fantasypros_league_artifact_id"],
            _time(record["fantasypros_captured_at"], "fantasypros_captured_at"),
            record["completed_history_available"],
        )


def _time(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = ("LeagueBindingScope", "WeeklySourceManifest")
