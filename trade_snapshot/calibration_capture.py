"""Bind a bounded calibration session to queryless browser-capture tasks."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ._analyzer_parsing import parse_analyzer_observation
from ._analyzer_types import AnalyzerObservation, AnalyzerTradeRequest, BundleFingerprint
from ._capture_artifacts import AnalyzerResponseArtifact
from ._capture_dimensions import AnalyzerTradeSpec
from ._capture_plan import (
    AnalyzerCapturePhase,
    CaptureKind,
    CapturePlan,
    PageCaptureTask,
)
from .capture_schema import validate_artifact_for_task
from .calibration_workflow import CalibrationSession


_ANALYZER_PAGE = "https://www.fantasypros.com/nfl/myplaybook/trade-analyzer.php"


@dataclass(frozen=True, slots=True)
class CalibrationCaptureBatch:
    """A capture plan plus its immutable task-to-experiment meaning."""

    plan: CapturePlan
    experiment_by_task_id: Mapping[str, str]
    request_by_task_id: Mapping[str, AnalyzerTradeRequest]
    _task_by_id: Mapping[str, PageCaptureTask] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.plan, CapturePlan):
            raise ValueError("plan must be a CapturePlan")
        tasks = tuple(self.plan.tasks)
        if not tasks or any(not isinstance(task, PageCaptureTask) for task in tasks):
            raise ValueError("calibration plan must contain page capture tasks")
        if any(
            task.kind is not CaptureKind.ANALYZER_RESPONSE
            or task.analyzer_phase is not AnalyzerCapturePhase.ORDINARY_POWER
            for task in tasks
        ):
            raise ValueError("calibration may capture ordinary power responses only")
        task_ids = {task.task_id for task in tasks}
        experiments = _text_mapping("experiment_by_task_id", self.experiment_by_task_id)
        requests = _request_mapping(self.request_by_task_id)
        if set(experiments) != task_ids or set(requests) != task_ids:
            raise ValueError("calibration task mappings must exactly cover the capture plan")
        if len(set(experiments.values())) != len(experiments):
            raise ValueError("each calibration experiment must map to one task")
        for task in tasks:
            if task.analyzer_trade != _trade_spec(requests[task.task_id]):
                raise ValueError("calibration task does not match its analyzer request")
        object.__setattr__(self, "experiment_by_task_id", experiments)
        object.__setattr__(self, "request_by_task_id", requests)
        object.__setattr__(
            self,
            "_task_by_id",
            MappingProxyType({task.task_id: task for task in tasks}),
        )

    def task(self, task_id: str) -> PageCaptureTask:
        try:
            return self._task_by_id[task_id]
        except (KeyError, TypeError):
            raise KeyError("unknown calibration capture task") from None


def build_calibration_capture_batch(
    session: CalibrationSession,
    *,
    season: int,
    week: int,
    analyzer_page: str = _ANALYZER_PAGE,
) -> CalibrationCaptureBatch:
    """Create one ordinary-power task for each designed calibration experiment."""

    if not isinstance(session, CalibrationSession):
        raise ValueError("session must be a CalibrationSession")
    tasks = []
    experiment_by_task_id = {}
    request_by_task_id = {}
    by_experiment = {row.experiment_id: row for row in session.plan.experiments}
    if set(by_experiment) != set(session.requests):
        raise ValueError("calibration session request coverage is incomplete")
    for experiment_id in sorted(session.requests):
        request = session.requests[experiment_id]
        task = PageCaptureTask(
            "fantasypros",
            season,
            week,
            "analyzer_response",
            analyzer_page,
            analyzer_phase="ordinary_power",
            analyzer_trade=_trade_spec(request),
        )
        tasks.append(task)
        experiment_by_task_id[task.task_id] = experiment_id
        request_by_task_id[task.task_id] = request
    return CalibrationCaptureBatch(
        CapturePlan(tasks),
        experiment_by_task_id,
        request_by_task_id,
    )


def observations_from_calibration_artifacts(
    batch: CalibrationCaptureBatch,
    artifacts: Iterable[AnalyzerResponseArtifact],
    *,
    bundle: BundleFingerprint | None = None,
) -> Mapping[str, AnalyzerObservation]:
    """Parse exact ordinary responses without retaining request transport data."""

    if not isinstance(batch, CalibrationCaptureBatch):
        raise ValueError("batch must be a CalibrationCaptureBatch")
    if bundle is not None and not isinstance(bundle, BundleFingerprint):
        raise ValueError("bundle must be a BundleFingerprint or None")
    if isinstance(artifacts, (str, bytes)):
        raise ValueError("artifacts must contain analyzer response artifacts")
    try:
        rows = tuple(artifacts)
    except TypeError:
        raise ValueError("artifacts must contain analyzer response artifacts") from None
    if any(not isinstance(row, AnalyzerResponseArtifact) for row in rows):
        raise ValueError("artifacts must contain analyzer response artifacts")
    by_task = {row.task_id: row for row in rows}
    if len(by_task) != len(rows) or set(by_task) != set(batch.request_by_task_id):
        raise ValueError("artifacts must exactly cover the calibration capture batch")
    captured_bundles = {
        BundleFingerprint(row.bundle_url, row.bundle_sha256) for row in rows
    }
    if len(captured_bundles) != 1:
        raise ValueError("calibration artifacts use different analyzer bundles")
    captured_bundle = next(iter(captured_bundles))
    if bundle is not None and bundle != captured_bundle:
        raise ValueError("supplied analyzer bundle does not match captured artifacts")
    observations = {}
    for task_id in sorted(by_task):
        artifact = by_task[task_id]
        task = batch.task(task_id)
        validate_artifact_for_task(artifact, task)
        if artifact.analyzer_phase is not AnalyzerCapturePhase.ORDINARY_POWER:
            raise ValueError("calibration artifacts must contain ordinary power only")
        experiment_id = batch.experiment_by_task_id[task_id]
        observations[experiment_id] = parse_analyzer_observation(
            batch.request_by_task_id[task_id],
            artifact.body,
            bundle=captured_bundle,
        )
    return MappingProxyType(dict(sorted(observations.items())))


def _trade_spec(request: AnalyzerTradeRequest) -> AnalyzerTradeSpec:
    if not isinstance(request, AnalyzerTradeRequest):
        raise ValueError("calibration request must be an AnalyzerTradeRequest")
    if request.team1_drops or request.team2_drops:
        raise ValueError("calibration capture does not support drop selections")
    return AnalyzerTradeSpec(
        request.team2_id,
        request.team1_gets,
        request.team2_gets,
        request.team1_adds,
        request.team2_adds,
    )


def _text_mapping(name: str, value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise ValueError(f"{name} must map non-empty strings to non-empty strings")
        result[key] = item
    return MappingProxyType(dict(sorted(result.items())))


def _request_mapping(value: object) -> Mapping[str, AnalyzerTradeRequest]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("request_by_task_id must be a non-empty mapping")
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(request, AnalyzerTradeRequest)
        for key, request in value.items()
    ):
        raise ValueError("request_by_task_id contains an invalid request")
    return MappingProxyType(dict(sorted(value.items())))


__all__ = (
    "CalibrationCaptureBatch",
    "build_calibration_capture_batch",
    "observations_from_calibration_artifacts",
)
