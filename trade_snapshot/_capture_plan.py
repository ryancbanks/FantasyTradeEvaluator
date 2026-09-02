"""Immutable weekly browser capture plans."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from ._capture_common import content_id, require_json_int, require_safe_https_url, schema_fingerprint
from ._capture_dimensions import AnalyzerTradeSpec, ProjectionTableSpec, RankingHorizon
from ._capture_task_policy import capture_plan_fingerprint, page_task_fingerprint
from ._capture_task_policy import validate_league_source_task, validate_visible_table_task
from ._capture_validation import enum_value as _enum_value, exact_fields as _exact_fields
from ._capture_validation import optional_text_set as _optional_text_set
from ._capture_validation import text_set as _text_set


class CaptureProvider(str, Enum):
    FANTASYPROS = "fantasypros"
    ESPN = "espn"
    YAHOO = "yahoo"


class CaptureKind(str, Enum):
    VISIBLE_TABLE = "visible_table"
    ANALYZER_RESPONSE = "analyzer_response"
    ECR_RANKINGS = "ecr_rankings"
    LEAGUE_SOURCE = "league_source"


class AnalyzerCapturePhase(str, Enum):
    ORDINARY_POWER = "ordinary_power"
    FULL_PLAYOFFS = "full_playoffs"


class ECRCaptureMethod(str, Enum):
    VISIBLE_PAGE = "visible_page"
    OFFICIAL_API = "official_api"


PROVIDER_HOST_ALLOWLISTS: Mapping[CaptureProvider, frozenset[str]] = MappingProxyType({
    CaptureProvider.FANTASYPROS: frozenset(
        {
            "fantasypros.com",
            "www.fantasypros.com",
            "api.fantasypros.com",
            "cdn.fantasypros.com",
            "mpbnfl.fantasypros.com",
        }
    ),
    CaptureProvider.ESPN: frozenset(
        {"espn.com", "www.espn.com", "fantasy.espn.com"}
    ),
    CaptureProvider.YAHOO: frozenset(
        {
            "fantasy.yahoo.com",
            "football.fantasysports.yahoo.com",
            "sports.yahoo.com",
        }
    ),
})

TASK_SCHEMA_FINGERPRINT = page_task_fingerprint(
    CaptureKind, AnalyzerCapturePhase, PROVIDER_HOST_ALLOWLISTS
)
ECR_TASK_SCHEMA_FINGERPRINT = schema_fingerprint(
    "fantasypros_ecr_capture_task",
    {
        "fields": [
            "provider", "season", "week", "kind", "horizon", "scoring",
            "position_scope", "expert_ids", "expert_count", "capture_method",
            "url", "task_id",
        ],
        "horizons": [horizon.value for horizon in RankingHorizon],
        "capture_methods": [method.value for method in ECRCaptureMethod],
        "provider_hosts": sorted(PROVIDER_HOST_ALLOWLISTS[CaptureProvider.FANTASYPROS]),
        "policy_version": "queryless-bootstrap-provenance-v3",
    },
)
CAPTURE_PLAN_SCHEMA_FINGERPRINT = capture_plan_fingerprint(TASK_SCHEMA_FINGERPRINT, ECR_TASK_SCHEMA_FINGERPRINT)


@dataclass(frozen=True, slots=True)
class PageCaptureTask:
    provider: CaptureProvider | str
    season: int
    week: int
    kind: CaptureKind | str
    url: str
    analyzer_phase: AnalyzerCapturePhase | str | None = None
    analyzer_trade: AnalyzerTradeSpec | None = None
    projection: ProjectionTableSpec | None = None
    task_id: str = field(init=False)

    def __post_init__(self) -> None:
        provider = _enum_value(CaptureProvider, "provider", self.provider)
        kind = _enum_value(CaptureKind, "kind", self.kind)
        if kind is CaptureKind.ECR_RANKINGS:
            raise ValueError("ecr_rankings requires FantasyProsECRTask")
        season = require_json_int("season", self.season, minimum=2000, maximum=2200)
        week = require_json_int("week", self.week, minimum=1, maximum=25)
        url = require_safe_https_url(
            self.url,
            allowed_hosts=PROVIDER_HOST_ALLOWLISTS[provider],
        )
        if kind is CaptureKind.ANALYZER_RESPONSE:
            if provider is not CaptureProvider.FANTASYPROS:
                raise ValueError("analyzer_response tasks must use FantasyPros")
            parsed = urlsplit(url)
            if (
                (parsed.hostname or "").casefold() not in {"fantasypros.com", "www.fantasypros.com"}
                or parsed.path != "/nfl/myplaybook/trade-analyzer.php"
            ):
                raise ValueError("analyzer_response tasks require the FantasyPros analyzer page")
            analyzer_phase = _enum_value(
                AnalyzerCapturePhase, "analyzer_phase", self.analyzer_phase
            )
            if not isinstance(self.analyzer_trade, AnalyzerTradeSpec):
                raise ValueError("analyzer_response tasks require AnalyzerTradeSpec")
            if self.projection is not None:
                raise ValueError("projection is only valid for visible_table tasks")
            trade = self.analyzer_trade
            projection = None
        elif kind is CaptureKind.VISIBLE_TABLE:
            if self.analyzer_phase is not None:
                raise ValueError("analyzer_phase is only valid for analyzer_response tasks")
            if self.analyzer_trade is not None:
                raise ValueError("analyzer_trade is only valid for analyzer_response tasks")
            if not isinstance(self.projection, ProjectionTableSpec):
                raise ValueError("visible_table tasks require ProjectionTableSpec")
            validate_visible_table_task(provider, url)
            analyzer_phase = None
            trade = None
            projection = self.projection
        else:
            validate_league_source_task(
                provider, url,
                (self.analyzer_phase, self.analyzer_trade, self.projection),
            )
            analyzer_phase = trade = projection = None
        values = {"provider": provider, "kind": kind, "season": season, "week": week, "url": url, "analyzer_phase": analyzer_phase, "analyzer_trade": trade, "projection": projection}
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "task_id", content_id("captask", self._content_record()))

    def _content_record(self) -> dict[str, object]:
        record = {
            "kind": self.kind.value,
            "provider": self.provider.value,
            "season": self.season,
            "url": self.url,
            "week": self.week,
        }
        if self.analyzer_phase is not None:
            record["analyzer_phase"] = self.analyzer_phase.value
            record["analyzer_trade"] = self.analyzer_trade.to_record()
        elif self.projection is not None:
            record["projection"] = self.projection.to_record()
        return record

    def to_record(self) -> dict[str, object]:
        return {
            "schema_fingerprint": TASK_SCHEMA_FINGERPRINT,
            **self._content_record(),
            "task_id": self.task_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "PageCaptureTask":
        if not isinstance(record, Mapping):
            raise ValueError("page capture task fields do not match the schema")
        kind_value = record.get("kind")
        phase_present = kind_value == CaptureKind.ANALYZER_RESPONSE.value
        table_present = kind_value == CaptureKind.VISIBLE_TABLE.value
        expected = {
            "schema_fingerprint", "provider", "season", "week", "kind", "url", "task_id"
        }
        if phase_present:
            expected.update(("analyzer_phase", "analyzer_trade"))
        elif table_present:
            expected.add("projection")
        _exact_fields(
            record,
            expected,
            "page capture task",
        )
        if record["schema_fingerprint"] != TASK_SCHEMA_FINGERPRINT:
            raise ValueError("page capture task schema fingerprint is invalid")
        task = cls(
            record["provider"], record["season"], record["week"], record["kind"],
            record["url"], record.get("analyzer_phase"),
            AnalyzerTradeSpec.from_record(record["analyzer_trade"])
            if phase_present else None,
            ProjectionTableSpec.from_record(record["projection"])
            if table_present else None,
        )
        if record["task_id"] != task.task_id:
            raise ValueError("page capture task content does not match task_id")
        return task


@dataclass(frozen=True, slots=True)
class FantasyProsECRTask:
    season: int
    week: int
    horizon: RankingHorizon | str
    scoring: str
    position_scope: tuple[str, ...]
    expert_ids: tuple[str, ...]
    expert_count: int | None
    url: str
    capture_method: ECRCaptureMethod | str = ECRCaptureMethod.VISIBLE_PAGE
    task_id: str = field(init=False)

    def __post_init__(self) -> None:
        season = require_json_int("season", self.season, minimum=2000, maximum=2200)
        week = require_json_int("week", self.week, minimum=1, maximum=25)
        horizon = _enum_value(RankingHorizon, "horizon", self.horizon)
        dimensions = ProjectionTableSpec(horizon, self.scoring, self.position_scope)
        scoring = dimensions.scoring
        positions = dimensions.position_scope
        experts = _optional_text_set("expert_ids", self.expert_ids)
        count = (
            None
            if self.expert_count is None
            else require_json_int("expert_count", self.expert_count, minimum=1, maximum=10000)
        )
        if experts and count is not None and count != len(experts):
            raise ValueError("expert_count must equal the number of unique expert_ids")
        method = _enum_value(ECRCaptureMethod, "capture_method", self.capture_method)
        url = require_safe_https_url(
            self.url,
            allowed_hosts=PROVIDER_HOST_ALLOWLISTS[CaptureProvider.FANTASYPROS],
        )
        parsed = urlsplit(url)
        page_hosts = {"fantasypros.com", "www.fantasypros.com"}
        if method is ECRCaptureMethod.VISIBLE_PAGE and (
            (parsed.hostname or "").casefold() not in page_hosts
            or not re.fullmatch(r"/nfl/(?:fantasy-football-)?rankings/[^/]+\.php", parsed.path)
        ):
            raise ValueError("visible ECR tasks require a FantasyPros rankings page")
        if method is ECRCaptureMethod.OFFICIAL_API and (
            (parsed.hostname or "").casefold() != "api.fantasypros.com"
            or not parsed.path.startswith("/v2/")
        ):
            raise ValueError("official API ECR tasks require the FantasyPros v2 API origin")
        for name, value in (
            ("season", season), ("week", week), ("horizon", horizon),
            ("scoring", scoring), ("position_scope", positions),
            ("expert_ids", experts), ("expert_count", count),
            ("capture_method", method), ("url", url),
        ):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "task_id", content_id("captask", self._content_record()))

    @property
    def provider(self) -> CaptureProvider:
        return CaptureProvider.FANTASYPROS

    @property
    def kind(self) -> CaptureKind:
        return CaptureKind.ECR_RANKINGS

    def _content_record(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "season": self.season,
            "week": self.week,
            "kind": self.kind.value,
            "horizon": self.horizon.value,
            "scoring": self.scoring,
            "position_scope": list(self.position_scope),
            "expert_ids": list(self.expert_ids),
            "expert_count": self.expert_count,
            "capture_method": self.capture_method.value,
            "url": self.url,
        }

    def to_record(self) -> dict[str, object]:
        return {
            "schema_fingerprint": ECR_TASK_SCHEMA_FINGERPRINT,
            **self._content_record(),
            "task_id": self.task_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "FantasyProsECRTask":
        expected = {
            "schema_fingerprint", "provider", "season", "week", "kind", "horizon",
            "scoring", "position_scope", "expert_ids", "expert_count", "capture_method",
            "url", "task_id",
        }
        _exact_fields(record, expected, "FantasyPros ECR capture task")
        if (
            record["schema_fingerprint"] != ECR_TASK_SCHEMA_FINGERPRINT
            or record["provider"] != CaptureProvider.FANTASYPROS.value
            or record["kind"] != CaptureKind.ECR_RANKINGS.value
            or not isinstance(record["position_scope"], list)
            or not isinstance(record["expert_ids"], list)
        ):
            raise ValueError("FantasyPros ECR capture task header is invalid")
        task = cls(
            record["season"], record["week"], record["horizon"], record["scoring"],
            tuple(record["position_scope"]), tuple(record["expert_ids"]),
            record["expert_count"], record["url"], record["capture_method"],
        )
        if record["task_id"] != task.task_id:
            raise ValueError("FantasyPros ECR capture task content does not match task_id")
        return task


CaptureTask = PageCaptureTask | FantasyProsECRTask


@dataclass(frozen=True, slots=True)
class CapturePlan:
    tasks: tuple[CaptureTask, ...]
    plan_id: str = field(init=False)

    def __init__(self, tasks: Iterable[CaptureTask]):
        try:
            normalized = tuple(tasks)
        except TypeError:
            raise ValueError("tasks must be an iterable of supported capture tasks") from None
        if not normalized or any(
            not isinstance(task, (PageCaptureTask, FantasyProsECRTask)) for task in normalized
        ):
            raise ValueError("tasks must contain at least one supported capture task")
        if len({task.task_id for task in normalized}) != len(normalized):
            raise ValueError("capture plan cannot contain duplicate tasks")
        if len({task.season for task in normalized}) != 1:
            raise ValueError("all capture plan tasks must target one season")
        object.__setattr__(self, "tasks", normalized)
        content = {"tasks": [task.to_record() for task in normalized]}
        object.__setattr__(self, "plan_id", content_id("capplan", content))

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema_fingerprint": CAPTURE_PLAN_SCHEMA_FINGERPRINT,
            "tasks": [task.to_record() for task in self.tasks],
            "plan_id": self.plan_id,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "CapturePlan":
        _exact_fields(
            record,
            {"schema_version", "schema_fingerprint", "tasks", "plan_id"},
            "capture plan",
        )
        if type(record["schema_version"]) is not int or record["schema_version"] != 1:
            raise ValueError("capture plan schema_version is invalid")
        if record["schema_fingerprint"] != CAPTURE_PLAN_SCHEMA_FINGERPRINT:
            raise ValueError("capture plan schema fingerprint is invalid")
        raw_tasks = record["tasks"]
        if not isinstance(raw_tasks, list):
            raise ValueError("capture plan tasks must be a list")
        plan = cls(_task_from_record(task) for task in raw_tasks)
        if record["plan_id"] != plan.plan_id:
            raise ValueError("capture plan content does not match plan_id")
        return plan


def _task_from_record(record: object) -> CaptureTask:
    if not isinstance(record, Mapping):
        raise ValueError("capture task record must be a mapping")
    if record.get("kind") == CaptureKind.ECR_RANKINGS.value:
        return FantasyProsECRTask.from_record(record)
    return PageCaptureTask.from_record(record)
