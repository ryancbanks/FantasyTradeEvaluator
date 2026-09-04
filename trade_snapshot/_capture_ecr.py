"""Strict FantasyPros expert-consensus rankings capture artifacts."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from numbers import Real
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from ._capture_common import (
    content_id,
    is_forbidden_capture_key,
    looks_like_url,
    require_captured_at,
    require_content_id,
    require_json_int,
    require_text,
    schema_fingerprint,
)
from ._capture_dimensions import _scoring, fantasypros_ecr_source_scoring
from ._capture_plan import (
    CaptureKind,
    CaptureProvider,
    ECRCaptureMethod,
    FantasyProsECRTask,
    RankingHorizon,
    _enum_value,
    _exact_fields,
    _text_set,
)
from .ecr_source import (
    EcrHorizonEvidence,
    EcrSourceDetails,
    FANTASYPROS_LATEST_ECR_POLICY,
)


FANTASYPROS_ECR_SCHEMA_FINGERPRINT = schema_fingerprint(
    "fantasypros_ecr_rankings_artifact",
    {
        "fields": [
            "task_metadata", "horizon", "scoring", "source_scoring",
            "position_scope", "expert_ids", "expert_count", "capture_method",
            "last_updated_text", "last_updated_at", "captured_at", "rankings",
            "source_details",
        ],
        "ranking_fields": [
            "provider_player_id", "player_name", "nfl_team_id", "position", "rank_ecr",
            "rank_min", "rank_max", "rank_avg", "rank_std", "position_rank",
            "source_position", "visible_values",
        ],
        "privacy": {
            "forbidden_key_classes": ["authentication", "cookies", "credentials", "transport"],
            "url_like_field_values_rejected": True,
            "visible_value_headers_and_cells_only": True,
        },
        "horizons": [horizon.value for horizon in RankingHorizon],
        "capture_methods": [method.value for method in ECRCaptureMethod],
        "expert_selection_policy": FANTASYPROS_LATEST_ECR_POLICY,
    },
)


@dataclass(frozen=True, slots=True)
class ECRRankingRow:
    provider_player_id: str
    player_name: str
    nfl_team_id: str | None
    position: str
    rank_ecr: float
    rank_min: float | None
    rank_max: float | None
    rank_avg: float | None
    rank_std: float | None
    position_rank: str
    visible_values: Mapping[str, str]
    source_position: str | None = None

    def __post_init__(self) -> None:
        provider_player_id = _safe_text("provider_player_id", self.provider_player_id)
        player_name = _safe_text("player_name", self.player_name)
        nfl_team_id = (
            None if self.nfl_team_id is None else _safe_text("nfl_team_id", self.nfl_team_id)
        )
        position = _safe_text("position", self.position)
        source_position = _safe_text(
            "source_position",
            self.position if self.source_position is None else self.source_position,
        ).upper()
        rank_ecr = _rank("rank_ecr", self.rank_ecr, required=True)
        rank_min = _rank("rank_min", self.rank_min)
        rank_max = _rank("rank_max", self.rank_max)
        rank_avg = _rank("rank_avg", self.rank_avg)
        rank_std = _rank("rank_std", self.rank_std, allow_zero=True)
        if rank_min is not None and rank_max is not None and rank_min > rank_max:
            raise ValueError("rank_min cannot exceed rank_max")
        if (
            rank_avg is not None
            and rank_min is not None
            and rank_avg < rank_min
        ) or (
            rank_avg is not None
            and rank_max is not None
            and rank_avg > rank_max
        ):
            raise ValueError("rank_avg must fall within rank_min and rank_max")
        position_rank = _safe_text("position_rank", self.position_rank)
        visible_values = _visible_values(self.visible_values)
        for name, value in (
            ("provider_player_id", provider_player_id), ("player_name", player_name),
            ("nfl_team_id", nfl_team_id), ("position", position), ("rank_ecr", rank_ecr),
            ("rank_min", rank_min), ("rank_max", rank_max), ("rank_avg", rank_avg),
            ("rank_std", rank_std), ("position_rank", position_rank),
            ("visible_values", visible_values), ("source_position", source_position),
        ):
            object.__setattr__(self, name, value)

    def to_record(self) -> dict[str, object]:
        return {
            "provider_player_id": self.provider_player_id,
            "player_name": self.player_name,
            "nfl_team_id": self.nfl_team_id,
            "position": self.position,
            "rank_ecr": self.rank_ecr,
            "rank_min": self.rank_min,
            "rank_max": self.rank_max,
            "rank_avg": self.rank_avg,
            "rank_std": self.rank_std,
            "position_rank": self.position_rank,
            "source_position": self.source_position,
            "visible_values": dict(self.visible_values),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "ECRRankingRow":
        fields = {
            "provider_player_id", "player_name", "nfl_team_id", "position", "rank_ecr",
            "rank_min", "rank_max", "rank_avg", "rank_std", "position_rank",
            "source_position", "visible_values",
        }
        _exact_fields(record, fields, "ECR ranking row")
        return cls(**{field: record[field] for field in fields})


@dataclass(frozen=True, slots=True)
class FantasyProsECRArtifact:
    task_id: str
    season: int
    week: int
    horizon: RankingHorizon | str
    scoring: str
    source_scoring: str
    position_scope: tuple[str, ...]
    expert_ids: tuple[str, ...]
    expert_count: int
    capture_method: ECRCaptureMethod | str
    last_updated_text: str
    last_updated_at: str | None
    captured_at: str
    source_details: EcrSourceDetails
    rankings: tuple[ECRRankingRow, ...]
    artifact_id: str = field(init=False)

    def __post_init__(self) -> None:
        task_id = require_content_id("task_id", self.task_id, "captask")
        season = require_json_int("season", self.season, minimum=2000, maximum=2200)
        week = require_json_int("week", self.week, minimum=1, maximum=25)
        horizon = _enum_value(RankingHorizon, "horizon", self.horizon)
        scoring = _scoring(self.scoring)
        positions = _text_set("position_scope", self.position_scope, uppercase=True)
        source_scoring = _scoring(self.source_scoring)
        if source_scoring != fantasypros_ecr_source_scoring(scoring, positions):
            raise ValueError(
                "source_scoring does not match the FantasyPros position page"
            )
        experts = _text_set("expert_ids", self.expert_ids)
        count = require_json_int("expert_count", self.expert_count, minimum=1, maximum=10000)
        if count != len(experts):
            raise ValueError("expert_count must equal the number of unique expert_ids")
        method = _enum_value(ECRCaptureMethod, "capture_method", self.capture_method)
        last_updated_text = _safe_text("last_updated_text", self.last_updated_text)
        last_updated_at = (
            None if self.last_updated_at is None else require_captured_at(self.last_updated_at)
        )
        captured_at = require_captured_at(self.captured_at)
        if last_updated_at is not None and _time(last_updated_at) > _time(captured_at):
            raise ValueError("last_updated_at cannot be after captured_at")
        if not isinstance(self.source_details, EcrSourceDetails):
            raise ValueError("source_details must be EcrSourceDetails")
        source_details = self.source_details
        _source_page_path(source_details.page_path, method)
        if len(positions) == 1 and positions[0] not in {"ALL", "FLX"} and (
            source_details.page_position != positions[0]
        ):
            raise ValueError("source page position does not match position_scope")
        _validate_horizon_evidence(
            season,
            horizon,
            week,
            source_scoring,
            positions,
            method,
            last_updated_at,
            source_details,
        )
        if isinstance(self.rankings, (str, bytes)):
            raise ValueError("rankings must be an iterable of ECRRankingRow values")
        try:
            rankings = tuple(self.rankings)
        except TypeError:
            raise ValueError("rankings must be an iterable of ECRRankingRow values") from None
        if not rankings or any(not isinstance(row, ECRRankingRow) for row in rankings):
            raise ValueError("rankings must contain at least one ECRRankingRow")
        player_ids = [row.provider_player_id for row in rankings]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("rankings cannot repeat provider_player_id")
        rankings = tuple(sorted(rankings, key=lambda row: (row.rank_ecr, row.provider_player_id)))
        for name, value in (
            ("task_id", task_id), ("season", season), ("week", week),
            ("horizon", horizon), ("scoring", scoring),
            ("source_scoring", source_scoring), ("position_scope", positions),
            ("expert_ids", experts), ("expert_count", count), ("capture_method", method),
            ("last_updated_text", last_updated_text),
            ("last_updated_at", last_updated_at), ("captured_at", captured_at),
            ("source_details", source_details),
            ("rankings", rankings),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "artifact_id", content_id("capecr", self._content_record()))

    @property
    def provider(self) -> CaptureProvider:
        return CaptureProvider.FANTASYPROS

    @property
    def kind(self) -> CaptureKind:
        return CaptureKind.ECR_RANKINGS

    def _content_record(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "provider": self.provider.value,
            "season": self.season,
            "week": self.week,
            "kind": self.kind.value,
            "horizon": self.horizon.value,
            "scoring": self.scoring,
            "source_scoring": self.source_scoring,
            "position_scope": list(self.position_scope),
            "expert_ids": list(self.expert_ids),
            "expert_count": self.expert_count,
            "capture_method": self.capture_method.value,
            "last_updated_text": self.last_updated_text,
            "last_updated_at": self.last_updated_at,
            "captured_at": self.captured_at,
            "source_details": self.source_details.to_record(),
            "rankings": [row.to_record() for row in self.rankings],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 4,
            "schema_fingerprint": FANTASYPROS_ECR_SCHEMA_FINGERPRINT,
            **self._content_record(),
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "FantasyProsECRArtifact":
        fields = {
            "schema_version", "schema_fingerprint", "artifact_id", "task_id", "provider",
            "season", "week", "kind", "horizon", "scoring", "position_scope",
            "source_scoring",
            "expert_ids", "expert_count", "capture_method", "last_updated_text",
            "last_updated_at", "captured_at", "rankings",
            "source_details",
        }
        _exact_fields(record, fields, "FantasyPros ECR artifact")
        if (
            type(record["schema_version"]) is not int
            or record["schema_version"] != 4
            or record["schema_fingerprint"] != FANTASYPROS_ECR_SCHEMA_FINGERPRINT
            or record["provider"] != CaptureProvider.FANTASYPROS.value
            or record["kind"] != CaptureKind.ECR_RANKINGS.value
            or not isinstance(record["position_scope"], list)
            or not isinstance(record["expert_ids"], list)
            or not isinstance(record["source_details"], Mapping)
            or not isinstance(record["rankings"], list)
        ):
            raise ValueError("FantasyPros ECR artifact header is invalid")
        artifact = cls(
            task_id=record["task_id"], season=record["season"], week=record["week"],
            horizon=record["horizon"], scoring=record["scoring"],
            source_scoring=record["source_scoring"],
            position_scope=tuple(record["position_scope"]),
            expert_ids=tuple(record["expert_ids"]), expert_count=record["expert_count"],
            capture_method=record["capture_method"],
            last_updated_text=record["last_updated_text"],
            last_updated_at=record["last_updated_at"], captured_at=record["captured_at"],
            source_details=EcrSourceDetails.from_record(record["source_details"]),
            rankings=tuple(ECRRankingRow.from_record(row) for row in record["rankings"]),
        )
        if record["artifact_id"] != artifact.artifact_id:
            raise ValueError("FantasyPros ECR artifact content does not match artifact_id")
        return artifact

    @classmethod
    def from_task(
        cls,
        task: FantasyProsECRTask,
        *,
        expert_ids: Iterable[str] | None = None,
        expert_count: int | None = None,
        source_scoring: str,
        last_updated_text: str,
        last_updated_at: str | None,
        captured_at: str,
        source_details: EcrSourceDetails,
        rankings: Iterable[ECRRankingRow],
    ) -> "FantasyProsECRArtifact":
        if not isinstance(task, FantasyProsECRTask):
            raise ValueError("task must be FantasyProsECRTask")
        if (
            not isinstance(source_details, EcrSourceDetails)
            or source_details.page_path != urlsplit(task.url).path
            or source_details.expert_selection_policy
            != task.expert_selection_policy
        ):
            raise ValueError("captured ECR page provenance does not match the task")
        actual_experts = task.expert_ids if expert_ids is None else tuple(expert_ids)
        if not actual_experts:
            raise ValueError("capture must report the actual consensus expert_ids")
        actual_count = (
            expert_count
            if expert_count is not None
            else task.expert_count
            if task.expert_count is not None
            else len(actual_experts)
        )
        if task.expert_ids and tuple(sorted(actual_experts)) != task.expert_ids:
            raise ValueError("captured expert_ids do not match the requested expert selection")
        if task.expert_count is not None and actual_count != task.expert_count:
            raise ValueError("captured expert_count does not match the expected count")
        return cls(
            task.task_id, task.season, task.week, task.horizon, task.scoring,
            source_scoring, task.position_scope, actual_experts, actual_count,
            task.capture_method, last_updated_text, last_updated_at, captured_at,
            source_details,
            tuple(rankings),
        )


def _source_page_path(value: object, method: ECRCaptureMethod) -> str:
    path = require_text("source_page_path", value)
    if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
        raise ValueError("source_page_path must be a safe absolute path")
    if method is ECRCaptureMethod.VISIBLE_PAGE and not re.fullmatch(
        r"/nfl/(?:fantasy-football-)?rankings/[^/]+\.php", path
    ):
        raise ValueError("source_page_path must identify a FantasyPros rankings page")
    return path


def _validate_horizon_evidence(
    season,
    horizon,
    week,
    source_scoring,
    positions,
    capture_method,
    source_updated_at,
    details,
) -> None:
    normalized = re.sub(r"[^a-z]", "", details.ranking_type.casefold())
    if details.horizon_evidence is EcrHorizonEvidence.PRESEASON_REST_OF_SEASON_PAGE:
        expected_draft_type = {
            "STD": "Draft", "HALF": "Draft Half PPR", "PPR": "Draft PPR",
        }[source_scoring]
        prefix = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}[
            source_scoring
        ]
        expected_path = f"/nfl/rankings/ros-{prefix}{positions[0].casefold()}.php"
        expected_heading = f"Fantasy Football ROS Rankings ({season})"
        expected_note = (
            f"We are currently displaying {season} Draft Rankings. "
            "Updated ROS Rankings will be available after the first week."
        )
        if (
            capture_method is not ECRCaptureMethod.VISIBLE_PAGE
            or horizon is not RankingHorizon.ROS
            or week != 1
            or len(positions) != 1
            or normalized != "draft"
            or details.type_text != expected_draft_type
            or details.source_week != 0
            or source_updated_at is None
            or details.page_position != positions[0]
            or details.page_path != expected_path
            or details.page_path != details.canonical_path
            or details.canonical_link_count != 1
            or details.settings_ranking_type != "ros"
            or details.settings_position != positions[0]
            or details.settings_page_heading != expected_heading
            or details.settings_fallback_note != expected_note
            or details.visible_page_heading != expected_heading
            or details.visible_page_heading_count != 1
            or details.visible_ranking_period != "Rest of Season"
            or details.visible_ranking_period_count != 1
            or details.visible_fallback_note != expected_note
            or details.visible_fallback_note_count != 1
            or not details.document_title.startswith("Rest of Season ")
            or " Rankings" not in details.document_title
            or not details.document_title.endswith(" | FantasyPros")
        ):
            raise ValueError("preseason ROS page evidence is inconsistent")
        return
    expected = "weekly" if horizon is RankingHorizon.WEEKLY else "ros"
    aliases = {"weekly": "weekly", "ros": "ros", "restofseason": "ros"}
    if aliases.get(normalized) != expected:
        raise ValueError("direct ECR horizon metadata is inconsistent")
    if horizon is RankingHorizon.WEEKLY and (
        details.source_week != week or "week" not in details.type_text.casefold()
    ):
        raise ValueError("direct weekly ECR metadata is inconsistent")
    if horizon is RankingHorizon.ROS and not (
        "rest" in details.type_text.casefold() or "ros" in details.type_text.casefold()
    ):
        raise ValueError("direct ROS ECR metadata is inconsistent")
    expected_settings = "weekly" if horizon is RankingHorizon.WEEKLY else "ros"
    period = str(details.visible_ranking_period or "").casefold()
    if (
        details.canonical_link_count != 1
        or details.page_path != details.canonical_path
        or details.visible_page_heading_count != 1
        or details.visible_ranking_period_count != 1
        or details.settings_ranking_type != expected_settings
        or details.settings_position != details.page_position
        or details.settings_page_heading != details.visible_page_heading
        or (horizon is RankingHorizon.WEEKLY and "week" not in period)
        or (horizon is RankingHorizon.ROS and period != "rest of season")
    ):
        raise ValueError("direct ECR page evidence is inconsistent")


def _rank(name: str, value: object, *, required: bool = False, allow_zero: bool = False):
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite rank")
    result = float(value)
    if not isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be a finite rank")
    return result


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe_text(name: str, value: object) -> str:
    text = require_text(name, value)
    if looks_like_url(text):
        raise ValueError(f"{name} cannot contain a URL")
    return text


def _visible_values(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("visible_values must be a mapping of source labels to text")
    normalized: dict[str, str] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key or is_forbidden_capture_key(key):
            raise ValueError("visible_values contains a forbidden field name")
        normalized[key] = _safe_text(f"visible_values.{key}", child)
    if not normalized:
        raise ValueError("visible_values must preserve at least one source value")
    return MappingProxyType(dict(sorted(normalized.items())))
