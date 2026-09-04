"""Runtime-only options, pacing, navigation, and deadline helpers."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ._capture_common import is_forbidden_capture_key
from ._capture_errors import BrowserCaptureCancelled, BrowserCaptureError, BrowserCaptureTimeout
from ._capture_task_policy import runtime_path_matches_task
from .capture_schema import (
    CaptureKind, CapturePlan, CaptureTask, ECRRankingRow, LeagueSource, VisibleTable,
)
from .ecr_source import EcrSourceDetails


@dataclass(frozen=True, slots=True)
class BrowserCaptureOptions:
    profile_directory: Path
    headed: bool = True
    navigation_timeout_ms: int = 45_000
    capture_timeout_ms: int = 30_000
    sign_in_timeout_ms: int = 300_000
    overall_timeout_ms: int | None = None
    action_delay_ms: int = 200

    def __post_init__(self) -> None:
        if not isinstance(self.profile_directory, Path) or not self.profile_directory.name:
            raise ValueError("profile_directory must be a non-root Path")
        if not isinstance(self.headed, bool):
            raise ValueError("headed must be a boolean")
        milliseconds("navigation_timeout_ms", self.navigation_timeout_ms)
        milliseconds("capture_timeout_ms", self.capture_timeout_ms)
        milliseconds("sign_in_timeout_ms", self.sign_in_timeout_ms)
        if self.overall_timeout_ms is not None:
            milliseconds("overall_timeout_ms", self.overall_timeout_ms)
        milliseconds("action_delay_ms", self.action_delay_ms, minimum=200)


@dataclass(frozen=True, slots=True)
class ECRCaptureData:
    expert_ids: tuple[str, ...]
    expert_count: int
    source_scoring: str
    last_updated_text: str
    last_updated_at: str | None
    source_details: EcrSourceDetails
    rankings: tuple[ECRRankingRow, ...]

    def __init__(
        self, expert_ids: Iterable[str], expert_count: int, source_scoring: str,
        last_updated_text: str, last_updated_at: str | None,
        source_details: EcrSourceDetails,
        rankings: Iterable[ECRRankingRow],
    ) -> None:
        try:
            experts, rows = tuple(expert_ids), tuple(rankings)
        except TypeError:
            raise ValueError("ECR capture collections must be iterable") from None
        if not experts or any(not isinstance(value, str) or not value for value in experts):
            raise ValueError("expert_ids must contain non-empty strings")
        if (
            type(expert_count) is not int or expert_count != len(experts)
            or len(experts) != len(set(experts))
        ):
            raise ValueError("expert_count must equal unique expert_ids")
        if source_scoring not in {"STD", "HALF", "PPR"}:
            raise ValueError("source_scoring must be STD, HALF, or PPR")
        if not isinstance(last_updated_text, str) or not last_updated_text.strip():
            raise ValueError("last_updated_text must be non-empty")
        if last_updated_at is not None and not isinstance(last_updated_at, str):
            raise ValueError("last_updated_at must be an RFC3339 string or None")
        if not isinstance(source_details, EcrSourceDetails):
            raise ValueError("source_details must be EcrSourceDetails")
        if not rows or any(not isinstance(row, ECRRankingRow) for row in rows):
            raise ValueError("rankings must contain ECRRankingRow values")
        object.__setattr__(self, "expert_ids", tuple(sorted(experts)))
        object.__setattr__(self, "expert_count", expert_count)
        object.__setattr__(self, "source_scoring", source_scoring)
        object.__setattr__(self, "last_updated_text", last_updated_text)
        object.__setattr__(self, "last_updated_at", last_updated_at)
        object.__setattr__(self, "source_details", source_details)
        object.__setattr__(self, "rankings", rows)


@dataclass(frozen=True, slots=True)
class ProjectionCaptureData:
    tables: tuple[VisibleTable, ...]
    source_period_text: str
    segments_captured: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tables, tuple) or not self.tables
            or any(not isinstance(table, VisibleTable) for table in self.tables)
        ):
            raise ValueError("tables must contain VisibleTable values")
        if not isinstance(self.source_period_text, str) or not self.source_period_text:
            raise ValueError("source_period_text must be non-empty")
        if type(self.segments_captured) is not int or not 1 <= self.segments_captured <= 10000:
            raise ValueError("segments_captured must be from 1 through 10000")


@dataclass(frozen=True, slots=True)
class LeagueCaptureData:
    team_count: int
    sources: tuple[LeagueSource, ...]

    def __init__(self, team_count: int, sources: Iterable[LeagueSource]) -> None:
        if type(team_count) is not int or not 2 <= team_count <= 100:
            raise ValueError("team_count must be from 2 through 100")
        if isinstance(sources, (str, bytes)):
            raise ValueError("sources must contain LeagueSource values")
        try:
            rows = tuple(sources)
        except TypeError:
            raise ValueError("sources must contain LeagueSource values") from None
        if not rows or any(not isinstance(row, LeagueSource) for row in rows):
            raise ValueError("sources must contain LeagueSource values")
        object.__setattr__(self, "team_count", team_count)
        object.__setattr__(self, "sources", rows)


class ActionPacer:
    def __init__(self, delay: float, clock: Callable[[], float]) -> None:
        self._delay, self._clock, self._last_action = delay, clock, None

    def before_action(self, cancelled, deadline, event_waiter) -> None:
        if self._last_action is not None:
            target = self._last_action + self._delay
            while self._clock() < target:
                check(cancelled, deadline, self._clock)
                chunk = min(0.05, target - self._clock())
                if deadline is not None:
                    chunk = min(chunk, deadline - self._clock())
                if chunk <= 0:
                    check(cancelled, deadline, self._clock)
                event_waiter(max(1, round(chunk * 1000)))
        check(cancelled, deadline, self._clock)
        self._last_action = self._clock()


def navigation_bindings(plan: CapturePlan, value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("navigation_bindings must map task IDs to runtime-only URLs")
    tasks = {task.task_id: task for task in plan.tasks}
    if any(not isinstance(key, str) or key not in tasks for key in value):
        raise ValueError("navigation_bindings contains an unknown task ID")
    result = {}
    for task_id, runtime_url in value.items():
        task = tasks[task_id]
        if task.kind is CaptureKind.ANALYZER_RESPONSE:
            raise ValueError("analyzer navigation is constructed from AnalyzerTradeSpec")
        if not isinstance(runtime_url, str) or len(runtime_url) > 8192:
            raise ValueError("runtime navigation URL is invalid")
        try:
            runtime, canonical, port = urlsplit(runtime_url), urlsplit(task.url), urlsplit(runtime_url).port
        except ValueError:
            raise ValueError("runtime navigation URL is invalid") from None
        if (
            runtime.scheme.casefold() != "https" or runtime.username or runtime.password
            or port not in (None, 443) or runtime.fragment
            or (runtime.hostname or "").casefold().rstrip(".")
            != (canonical.hostname or "").casefold().rstrip(".")
            or not runtime_path_matches_task(task, runtime.path)
        ):
            raise ValueError("runtime navigation must keep the task's HTTPS origin and path")
        if any(is_forbidden_capture_key(key) for key, _ in parse_qsl(runtime.query)):
            raise ValueError("runtime navigation query contains a secret-like field")
        result[task_id] = runtime_url
    return result


def navigation_url(task: CaptureTask, bindings: Mapping[str, str]) -> str:
    if task.kind is not CaptureKind.ANALYZER_RESPONSE:
        return bindings.get(task.task_id, task.url)
    trade = task.analyzer_trade
    query = [("team2Id", trade.team2_id)]
    for name, values in (
        ("team1Gets", trade.team1_gets), ("team2Gets", trade.team2_gets),
        ("team1Adds", trade.team1_adds), ("team2Adds", trade.team2_adds),
    ):
        if values:
            query.append((name, ",".join(values)))
    parsed = urlsplit(task.url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def cancellation_check(value: object | None) -> Callable[[], bool]:
    if value is None:
        return lambda: False
    checker = getattr(value, "is_set", None)
    if not callable(checker):
        raise ValueError("cancellation must expose an is_set() method")
    return checker


def check(cancelled, deadline, clock) -> None:
    if cancelled():
        raise BrowserCaptureCancelled("browser capture was cancelled")
    if deadline is not None and clock() >= deadline:
        raise BrowserCaptureTimeout("browser capture exceeded its overall timeout")


def remaining(configured_ms: int, deadline, clock) -> int:
    if deadline is None:
        return configured_ms
    value = int((deadline - clock()) * 1000)
    if value <= 0:
        raise BrowserCaptureTimeout("browser capture exceeded its overall timeout")
    return min(configured_ms, value)


def capture_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BrowserCaptureError("capture clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def milliseconds(name: str, value: object, minimum: int = 1) -> None:
    if type(value) is not int or not minimum <= value <= 86_400_000:
        raise ValueError(f"{name} must be an integer from {minimum} through 86400000")


__all__ = (
    "ActionPacer", "BrowserCaptureOptions", "ECRCaptureData", "LeagueCaptureData",
    "ProjectionCaptureData",
    "cancellation_check", "capture_time", "check", "navigation_bindings", "navigation_url",
    "remaining",
)
