"""Shared bounded-retention policy for in-memory background job records."""

ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset({"complete", "cancelled", "failed"})
DEFAULT_TERMINAL_JOB_LIMIT = 24


def has_active_jobs(jobs) -> bool:
    return any(job.status in ACTIVE_JOB_STATUSES for job in jobs.values())


def prune_terminal_jobs(
    jobs,
    maximum: int = DEFAULT_TERMINAL_JOB_LIMIT,
) -> None:
    """Delete oldest terminal records while retaining active work unconditionally."""

    if type(maximum) is not int or maximum < 1:
        raise ValueError("terminal job limit must be a positive integer")
    terminal_ids = [
        job_id
        for job_id, job in jobs.items()
        if job.status in TERMINAL_JOB_STATUSES
    ]
    for job_id in terminal_ids[:-maximum]:
        del jobs[job_id]
