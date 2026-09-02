"""Best-effort collection and quarantine for public projection publishers."""

from itertools import combinations

from .browser_capture import (
    BrowserCaptureCancelled,
    BrowserCaptureDependencyError,
    BrowserCaptureError,
)
from .capture_schema import (
    CapturePlan,
    CaptureProvider,
    validate_artifact_for_task,
)
from .weekly_collection import (
    WeeklyCollectionError,
    WeeklyCollectionProgress,
    WeeklyCollectionStage,
)


PUBLIC_PROVIDERS = frozenset({
    CaptureProvider.CBS,
    CaptureProvider.FFTODAY,
    CaptureProvider.FANTASYSHARKS,
})


def split_optional_public(plan: CapturePlan) -> tuple[CapturePlan, CapturePlan | None]:
    """Separate fail-closed sources from best-effort public publishers."""

    if not isinstance(plan, CapturePlan):
        raise ValueError("source plan must be a CapturePlan")
    public = tuple(task for task in plan.tasks if task.provider in PUBLIC_PROVIDERS)
    required = tuple(task for task in plan.tasks if task.provider not in PUBLIC_PROVIDERS)
    if not required:
        raise ValueError("source plan has no required projection tasks")
    return CapturePlan(required), None if not public else CapturePlan(public)


def capture_optional_public(
    collector,
    plan: CapturePlan | None,
    *,
    options,
    token,
    gate,
    cancelled,
    progress,
):
    """Capture public tables independently and retain only complete providers."""

    if plan is None:
        return (), ()
    artifacts = []
    captured_tasks = []
    total = len(plan.tasks)
    for index, task in enumerate(plan.tasks, start=1):
        _check_cancelled(cancelled)
        progress(
            WeeklyCollectionProgress(
                WeeklyCollectionStage.COLLECTING_PUBLIC,
                .57 + .06 * (index - 1) / max(total, 1),
                f"Trying public {task.provider.value} projections ({index} of {total})",
            )
        )
        try:
            rows = collector.collect(
                CapturePlan((task,)),
                options,
                cancellation=token,
                sign_in_gate=gate,
            )
        except (BrowserCaptureCancelled, BrowserCaptureDependencyError):
            raise
        except BrowserCaptureError:
            continue
        if not isinstance(rows, tuple) or len(rows) != 1:
            raise ValueError("public projection collector returned invalid task coverage")
        try:
            validate_artifact_for_task(rows[0], task)
        except ValueError:
            # A best-effort publisher page that no longer proves the requested
            # dimensions is rejected just like a failed navigation.
            continue
        artifacts.append(rows[0])
        captured_tasks.append(task)
    return retain_complete_public_providers(
        tuple(artifacts), tuple(captured_tasks), plan.tasks
    )


def retain_complete_public_providers(artifacts, captured_tasks, planned_tasks):
    """Drop a provider unless every planned page/dimension task succeeded."""

    planned = {
        provider: {
            task.task_id
            for task in planned_tasks
            if task.provider is provider
        }
        for provider in PUBLIC_PROVIDERS
    }
    captured = {
        provider: {
            task.task_id
            for task in captured_tasks
            if task.provider is provider
        }
        for provider in PUBLIC_PROVIDERS
    }
    complete = {
        provider
        for provider in PUBLIC_PROVIDERS
        if planned[provider] and captured[provider] == planned[provider]
    }
    kept_tasks = tuple(task for task in captured_tasks if task.provider in complete)
    kept_ids = {task.task_id for task in kept_tasks}
    return (
        tuple(row for row in artifacts if row.task_id in kept_ids),
        kept_tasks,
    )


def assemble_with_public_fallback(projections, assemble, *, broad_consensus):
    """Retry local assembly without a public publisher that fails semantically."""

    rows = tuple(projections)
    if not callable(assemble) or not isinstance(broad_consensus, bool):
        raise ValueError("public projection assembly inputs are invalid")
    if not broad_consensus:
        return assemble(rows, False), rows

    provider_order = (
        CaptureProvider.CBS,
        CaptureProvider.FFTODAY,
        CaptureProvider.FANTASYSHARKS,
    )
    available = tuple(
        provider
        for provider in provider_order
        if any(row.provider is provider for row in rows)
    )
    required = tuple(row for row in rows if row.provider not in PUBLIC_PROVIDERS)
    if not available:
        return assemble(required, True), required

    try:
        return assemble(rows, True), rows
    except ValueError:
        # Prove the required ESPN/FantasyPros evidence is healthy before
        # treating the failure as optional-publisher drift.
        assemble(required, False)

    for size in range(len(available) - 1, 0, -1):
        for retained in combinations(available, size):
            candidate = required + tuple(
                row for row in rows if row.provider in retained
            )
            try:
                return assemble(candidate, True), candidate
            except ValueError:
                continue
    try:
        return assemble(required, True), required
    except ValueError as exc:
        raise WeeklyCollectionError(
            "The captured projection publishers could not form a valid broad "
            "consensus. Optional publisher data was rejected; turn Broad "
            "projection consensus off or retry later. No weekly bundle was "
            "published."
        ) from exc


def _check_cancelled(check) -> None:
    if not callable(check):
        raise ValueError("cancelled must be callable")
    value = check()
    if not isinstance(value, bool):
        raise ValueError("cancelled must return a boolean")
    if value:
        raise WeeklyCollectionError("Weekly collection was cancelled.")


__all__ = (
    "PUBLIC_PROVIDERS",
    "assemble_with_public_fallback",
    "capture_optional_public",
    "retain_complete_public_providers",
    "split_optional_public",
)
