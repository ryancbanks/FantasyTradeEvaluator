"""Auxiliary FantasyPros standings benchmark retained for model-drift checks."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite

from ._scenario_random import content_id
from .capture_schema import FantasyProsLeagueArtifact, LeagueSourceKind


@dataclass(frozen=True, slots=True)
class FantasyProsTeamBenchmark:
    team_id: str
    team_name: str
    current_rank: float
    projected_rank: float
    current_wins: float
    current_losses: float
    projected_wins: float
    projected_losses: float
    playoff_probability: float
    championship_probability: float

    def __post_init__(self) -> None:
        for name in ("team_id", "team_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
            object.__setattr__(self, name, value.strip())
        for name in (
            "current_rank", "projected_rank", "current_wins", "current_losses",
            "projected_wins", "projected_losses", "playoff_probability",
            "championship_probability",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be finite numeric data")
            value = float(value)
            if not isfinite(value):
                raise ValueError(f"{name} must be finite numeric data")
            object.__setattr__(self, name, value)
        if self.current_rank < 1 or self.projected_rank < 1:
            raise ValueError("benchmark ranks must be positive")
        if min(
            self.current_wins,
            self.current_losses,
            self.projected_wins,
            self.projected_losses,
        ) < 0:
            raise ValueError("benchmark records cannot be negative")
        if not 0 <= self.playoff_probability <= 1 or not (
            0 <= self.championship_probability <= 1
        ):
            raise ValueError("benchmark probabilities must be between zero and one")

    def to_record(self):
        return {
            name: getattr(self, name)
            for name in (
                "championship_probability", "current_losses", "current_rank",
                "current_wins", "playoff_probability", "projected_losses",
                "projected_rank", "projected_wins", "team_id", "team_name",
            )
        }

    @classmethod
    def from_record(cls, record):
        fields = {
            "championship_probability", "current_losses", "current_rank",
            "current_wins", "playoff_probability", "projected_losses",
            "projected_rank", "projected_wins", "team_id", "team_name",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("FantasyPros team benchmark fields are invalid")
        return cls(**{name: record[name] for name in fields})


@dataclass(frozen=True, slots=True)
class FantasyProsLeagueBenchmark:
    snapshot_id: str
    source_artifact_id: str
    captured_at: datetime
    teams: tuple[FantasyProsTeamBenchmark, ...]
    benchmark_id: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "source_artifact_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
            object.__setattr__(self, name, value.strip())
        if (
            not isinstance(self.captured_at, datetime)
            or self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() is None
        ):
            raise ValueError("captured_at must be a timezone-aware datetime")
        captured = self.captured_at.astimezone(timezone.utc)
        if isinstance(self.teams, (str, bytes)):
            raise ValueError("teams must contain FantasyProsTeamBenchmark values")
        try:
            teams = tuple(self.teams)
        except TypeError:
            raise ValueError(
                "teams must contain FantasyProsTeamBenchmark values"
            ) from None
        if not teams or any(
            not isinstance(row, FantasyProsTeamBenchmark) for row in teams
        ):
            raise ValueError("teams must contain FantasyProsTeamBenchmark values")
        if len({row.team_id for row in teams}) != len(teams):
            raise ValueError("FantasyPros benchmark contains duplicate teams")
        teams = tuple(sorted(teams, key=lambda row: row.team_id))
        object.__setattr__(self, "captured_at", captured)
        object.__setattr__(self, "teams", teams)
        object.__setattr__(
            self, "benchmark_id", content_id("fpbenchmark", self._content_record())
        )

    @classmethod
    def from_capture(
        cls,
        artifact: FantasyProsLeagueArtifact,
        snapshot_id: str,
        fantasypros_team_ids: Mapping[str, str],
    ) -> "FantasyProsLeagueBenchmark":
        if not isinstance(artifact, FantasyProsLeagueArtifact):
            raise ValueError("artifact must be a FantasyProsLeagueArtifact")
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            raise ValueError("snapshot_id must be non-empty text")
        if not isinstance(fantasypros_team_ids, Mapping) or not fantasypros_team_ids:
            raise ValueError("fantasypros_team_ids must be a non-empty mapping")
        mapped_ids: dict[str, str] = {}
        for team_id, source_team_id in fantasypros_team_ids.items():
            if not isinstance(team_id, str) or not team_id.strip():
                raise ValueError("FantasyPros team mapping keys must be non-empty text")
            if not isinstance(source_team_id, str) or not source_team_id.strip():
                raise ValueError("FantasyPros team mapping values must be non-empty text")
            mapped_ids[team_id.strip()] = source_team_id.strip()
        if len(set(mapped_ids.values())) != len(mapped_ids):
            raise ValueError("FantasyPros team mapping must be one-to-one")
        projected = next(
            source
            for source in artifact.sources
            if source.source is LeagueSourceKind.PROJECTED_STANDINGS
        )
        payload = projected.to_record()["body"]["payload"]
        by_source = {row["teamId"]: row for row in payload["standings"]}
        if set(by_source) != set(mapped_ids.values()):
            raise ValueError("FantasyPros benchmark does not cover mapped league teams")
        rows = []
        for team_id, source_team_id in mapped_ids.items():
            row = by_source[source_team_id]
            rows.append(
                FantasyProsTeamBenchmark(
                    team_id,
                    row["teamName"],
                    row["rank_current"],
                    row["rank_proj"],
                    row["wins_current"],
                    row["losses_current"],
                    row["wins_proj"],
                    row["losses_proj"],
                    row["playoffs_odds"] / 100,
                    row["championship_odds"] / 100,
                )
            )
        return cls(
            snapshot_id,
            artifact.artifact_id,
            _time(artifact.captured_at),
            tuple(rows),
        )

    def _content_record(self):
        return {
            "captured_at": self.captured_at.isoformat(timespec="microseconds"),
            "snapshot_id": self.snapshot_id,
            "source_artifact_id": self.source_artifact_id,
            "teams": [row.to_record() for row in self.teams],
        }

    def to_record(self):
        return {
            "kind": "fantasypros_league_benchmark",
            "schema_version": 1,
            **self._content_record(),
            "benchmark_id": self.benchmark_id,
        }

    @classmethod
    def from_record(cls, record):
        fields = {
            "benchmark_id", "captured_at", "kind", "schema_version",
            "snapshot_id", "source_artifact_id", "teams",
        }
        if not isinstance(record, Mapping) or set(record) != fields:
            raise ValueError("FantasyPros league benchmark fields are invalid")
        if (
            record["kind"] != "fantasypros_league_benchmark"
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
        ):
            raise ValueError("FantasyPros league benchmark header is invalid")
        if not isinstance(record["teams"], list):
            raise ValueError("FantasyPros league benchmark teams must be an array")
        value = cls(
            record["snapshot_id"],
            record["source_artifact_id"],
            _time(record["captured_at"]),
            tuple(FantasyProsTeamBenchmark.from_record(row) for row in record["teams"]),
        )
        if value.benchmark_id != record["benchmark_id"]:
            raise ValueError("FantasyPros benchmark content does not match benchmark_id")
        return value


def _time(value):
    if not isinstance(value, str):
        raise ValueError("captured_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("captured_at must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("captured_at must include a timezone")
    return parsed.astimezone(timezone.utc)


__all__ = ("FantasyProsLeagueBenchmark", "FantasyProsTeamBenchmark")
