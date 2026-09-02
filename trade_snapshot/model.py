from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping


_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class SnapshotRequest:
    """The NFL period and scoring rules represented by one snapshot."""

    season: int
    week: int
    scoring: str

    def __post_init__(self) -> None:
        if isinstance(self.season, bool) or not isinstance(self.season, int) or self.season < 2012:
            raise ValueError("season must be an integer of 2012 or later")
        if isinstance(self.week, bool) or not isinstance(self.week, int) or not 0 <= self.week <= 25:
            raise ValueError("week must be an integer from 0 through 25")
        if self.scoring not in {"STD", "HALF", "PPR"}:
            raise ValueError("scoring must be STD, HALF, or PPR")


@dataclass(frozen=True)
class DatasetPayload:
    """A JSON dataset plus non-secret provenance supplied by one source."""

    name: str
    payload: Any
    source_metadata: Mapping[str, Any] = field(default_factory=dict)
    source_as_of: str | None = None

    def __post_init__(self) -> None:
        if not _DATASET_NAME.fullmatch(self.name):
            raise ValueError("dataset name must contain only lowercase letters, numbers, '-' or '_'")


@dataclass(frozen=True)
class SnapshotResult:
    path: Path
    failed_sources: tuple[str, ...]
    ready_for_offline_compute: bool
