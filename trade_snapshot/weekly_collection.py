"""Cancellable background boundary for publishing one weekly engine bundle."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from threading import Event, RLock, Thread
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from .engine_bundle import EngineBundle, save_engine_bundle
from .operation_timing import OperationTiming


class WeeklyCollectionStage(str, Enum):
    PREPARING = "preparing"
    COLLECTING_LEAGUE = "collecting_league"
    COLLECTING_FANTASYPROS = "collecting_fantasypros"
    COLLECTING_ESPN = "collecting_espn"
    COLLECTING_YAHOO = "collecting_yahoo"
    NORMALIZING = "normalizing"
    CALIBRATING = "calibrating"
    BUILDING = "building"
    PUBLISHING = "publishing"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class WeeklyCollectionRequest:
    """Non-secret inputs; team count and rosters are discovered during capture."""

    season: int
    week: int
    scoring: str
    # Retained as an optional compatibility assertion for older API clients.
    expected_team_count: int | None = None
    host_league_url: str | None = None
    yahoo_projection_league_url: str | None = None
    include_future_weekly: bool = False
    allow_surrogate_power: bool = False

    def __post_init__(self) -> None:
        if type(self.season) is not int or not 2012 <= self.season <= 9999:
            raise ValueError("season must be an integer from 2012 through 9999")
        if type(self.week) is not int or not 1 <= self.week <= 25:
            raise ValueError("week must be an integer from 1 through 25")
        if self.scoring not in {"STD", "HALF", "PPR"}:
            raise ValueError("scoring must be STD, HALF, or PPR")
        if self.expected_team_count is not None and (
            type(self.expected_team_count) is not int
            or not 2 <= self.expected_team_count <= 32
        ):
            raise ValueError(
                "expected_team_count must be null or an integer from 2 through 32"
            )
        object.__setattr__(
            self,
            "host_league_url",
            _host_league_url(self.host_league_url, self.season),
        )
        object.__setattr__(
            self,
            "yahoo_projection_league_url",
            _yahoo_projection_url(self.yahoo_projection_league_url, self.season),
        )
        if not isinstance(self.include_future_weekly, bool):
            raise ValueError("include_future_weekly must be a boolean")
        if not isinstance(self.allow_surrogate_power, bool):
            raise ValueError("allow_surrogate_power must be a boolean")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "WeeklyCollectionRequest":
        required = {
            "season",
            "week",
            "scoring",
            "include_future_weekly",
        }
        optional = {
            "expected_team_count",
            "host_league_url",
            "yahoo_projection_league_url",
            "allow_surrogate_power",
        }
        if (
            not isinstance(payload, Mapping)
            or not required <= set(payload)
            or not set(payload) <= required | optional
        ):
            raise ValueError("weekly collection request fields are invalid")
        return cls(
            season=payload["season"],
            week=payload["week"],
            scoring=payload["scoring"],
            expected_team_count=payload.get("expected_team_count"),
            host_league_url=payload.get("host_league_url"),
            yahoo_projection_league_url=payload.get("yahoo_projection_league_url"),
            include_future_weekly=payload["include_future_weekly"],
            allow_surrogate_power=payload.get("allow_surrogate_power", False),
        )


@dataclass(frozen=True, slots=True)
class WeeklyCollectionProgress:
    stage: WeeklyCollectionStage
    fraction: float
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.stage, WeeklyCollectionStage):
            raise ValueError("stage must be a WeeklyCollectionStage")
        if (
            isinstance(self.fraction, bool)
            or not isinstance(self.fraction, (int, float))
            or not 0 <= self.fraction <= 1
        ):
            raise ValueError("fraction must be between zero and one")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")


class WeeklyCollectionError(RuntimeError):
    """Expected collection failure whose message is safe to show locally."""


class WeeklyCollectionWorkflow(Protocol):
    """External-data workflow; ordinary trade search never calls this seam."""

    def __call__(
        self,
        request: WeeklyCollectionRequest,
        *,
        data_directory: Path,
        progress: Callable[[WeeklyCollectionProgress], None],
        cancelled: Callable[[], bool],
    ) -> EngineBundle: ...


@dataclass(slots=True)
class _CollectionJob:
    job_id: str
    request: WeeklyCollectionRequest
    status: str = "queued"
    progress: WeeklyCollectionProgress | None = None
    error: str | None = None
    bundle_id: str | None = None
    cancel: Event = field(default_factory=Event)
    data_directory: Path | None = None
    on_published: Callable[[EngineBundle], None] | None = None
    timing: OperationTiming = field(default_factory=OperationTiming)


class WeeklyCollectionJobs:
    """Own collection thread state and atomically publish only complete bundles."""

    def __init__(
        self,
        data_directory: str | Path,
        bundle_directory: str | Path,
        workflow: WeeklyCollectionWorkflow | None,
        *,
        timing_factory: Callable[[], OperationTiming] = OperationTiming,
    ) -> None:
        if not callable(timing_factory):
            raise ValueError("timing_factory must be callable")
        self._data_directory = Path(data_directory).resolve()
        self._bundle_directory = Path(bundle_directory).resolve()
        self._workflow = workflow
        self._timing_factory = timing_factory
        self._lock = RLock()
        self._jobs: dict[str, _CollectionJob] = {}

    @property
    def available(self) -> bool:
        return self._workflow is not None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return any(job.status in {"queued", "running"} for job in self._jobs.values())

    def start(
        self,
        request: WeeklyCollectionRequest,
        *,
        data_directory: str | Path | None = None,
        on_published: Callable[[EngineBundle], None] | None = None,
    ) -> dict[str, object]:
        if not isinstance(request, WeeklyCollectionRequest):
            raise ValueError("request must be a WeeklyCollectionRequest")
        if self._workflow is None:
            raise RuntimeError(
                "Weekly collection is not available in this build. "
                "Import a complete weekly bundle instead."
            )
        workspace = (
            self._data_directory
            if data_directory is None
            else Path(data_directory).resolve()
        )
        if workspace != self._data_directory and self._data_directory not in workspace.parents:
            raise ValueError("collection data_directory must stay inside application data")
        if on_published is not None and not callable(on_published):
            raise ValueError("on_published must be callable")
        with self._lock:
            if self.is_running:
                raise RuntimeError("another weekly collection is already running")
            timing = self._timing_factory()
            if not isinstance(timing, OperationTiming):
                raise TypeError("timing_factory must return an OperationTiming")
            job = _CollectionJob(
                uuid4().hex,
                request,
                data_directory=workspace,
                on_published=on_published,
                timing=timing,
            )
            self._jobs[job.job_id] = job
            Thread(target=self._run, args=(job,), name="weekly-collection", daemon=True).start()
            return self._record(job)

    def job(self, job_id: str) -> dict[str, object]:
        with self._lock:
            return self._record(self._require(job_id))

    def confirm_sign_in(self, job_id: str) -> dict[str, object]:
        """Release the current provider gate without accepting credentials."""

        with self._lock:
            job = self._require(job_id)
            if job.status not in {"queued", "running"}:
                raise RuntimeError("weekly collection is not waiting for sign-in")
            gate = getattr(self._workflow, "sign_in_gate", None)
            confirm = getattr(gate, "confirm", None)
            status = getattr(gate, "status", None)
            if not callable(confirm) or not callable(status):
                raise RuntimeError("this collection workflow has no interactive sign-in")
        confirmed_provider = confirm()
        current = status()
        return {
            "job_id": job_id,
            "confirmed_provider": confirmed_provider,
            "sign_in": _sign_in_record(current),
        }

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._require(job_id)
            if job.status in {"queued", "running"}:
                job.cancel.set()
                job.timing.request_cancel()
            return self._record(job)

    def _run(self, job: _CollectionJob) -> None:
        unsubscribe_wait_state: Callable[[], None] | None = None
        try:
            job.timing.start(WeeklyCollectionStage.PREPARING.value)
            self._update(
                job,
                status="running",
                progress=WeeklyCollectionProgress(
                    WeeklyCollectionStage.PREPARING,
                    0,
                    "Preparing the connected browser extension",
                ),
            )
            unsubscribe_wait_state = self._subscribe_sign_in_timing(job)
            if job.cancel.is_set():
                job.timing.cancel()
                self._update(job, status="cancelled")
                return
            bundle = self._workflow(
                job.request,
                data_directory=job.data_directory or self._data_directory,
                progress=lambda value: self._progress(job, value),
                cancelled=job.cancel.is_set,
            )
            if not isinstance(bundle, EngineBundle):
                raise TypeError("weekly collection workflow did not return an EngineBundle")
            if job.cancel.is_set():
                job.timing.cancel()
                self._update(job, status="cancelled")
                return
            job.timing.begin_phase(WeeklyCollectionStage.PUBLISHING.value)
            self._update(
                job,
                progress=WeeklyCollectionProgress(
                    WeeklyCollectionStage.PUBLISHING,
                    0.99,
                    "Saving the complete weekly engine for offline searches",
                ),
            )
            save_engine_bundle(
                bundle,
                self._bundle_directory / f"{bundle.bundle_id}.json",
            )
            self._update(job, bundle_id=bundle.bundle_id)
            if job.on_published is not None:
                try:
                    job.on_published(bundle)
                except Exception:
                    job.timing.fail()
                    self._update(
                        job,
                        status="failed",
                        error=(
                            "The weekly engine was saved under Unassigned imports because "
                            "its league workspace could not be updated."
                        ),
                    )
                    return
            job.timing.finish()
            self._update(
                job,
                status="complete",
                progress=WeeklyCollectionProgress(
                    WeeklyCollectionStage.READY,
                    1,
                    "This week is ready for local trade searches",
                ),
            )
        except WeeklyCollectionError as error:
            self._finish_error(job, str(error))
        except Exception:
            self._finish_error(
                job,
                "Weekly collection stopped unexpectedly. No new weekly bundle was published.",
            )
        finally:
            if unsubscribe_wait_state is not None:
                unsubscribe_wait_state()

    def _progress(self, job: _CollectionJob, value: WeeklyCollectionProgress) -> None:
        if job.cancel.is_set():
            raise WeeklyCollectionError("Weekly collection was cancelled")
        if not isinstance(value, WeeklyCollectionProgress):
            raise TypeError("collection progress must be WeeklyCollectionProgress")
        if value.stage in {
            WeeklyCollectionStage.PUBLISHING,
            WeeklyCollectionStage.READY,
        }:
            raise ValueError("publishing stages are owned by the collection job")
        with self._lock:
            previous = job.progress
            if previous is not None and value.fraction < previous.fraction:
                raise ValueError("collection progress cannot move backwards")
            if value.fraction >= 0.99:
                raise ValueError("workflow progress must leave room for atomic publishing")
            if previous is None or value.stage != previous.stage:
                job.timing.begin_phase(value.stage.value)
            job.progress = value

    def _finish_error(self, job: _CollectionJob, message: str) -> None:
        if job.cancel.is_set():
            job.timing.cancel()
            self._update(job, status="cancelled", error=None)
        else:
            job.timing.fail()
            visible = message.strip() if isinstance(message, str) else ""
            self._update(
                job,
                status="failed",
                error=(
                    visible
                    or "Weekly collection could not be completed. No new bundle was published."
                ),
            )

    def _update(self, job: _CollectionJob, **changes: object) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)

    def _subscribe_sign_in_timing(
        self, job: _CollectionJob
    ) -> Callable[[], None] | None:
        gate = getattr(self._workflow, "sign_in_gate", None)
        subscribe = getattr(gate, "subscribe_wait_state", None)
        if not callable(subscribe):
            return None

        def waiting_changed(waiting: bool) -> None:
            if not isinstance(waiting, bool):
                raise ValueError("sign-in wait state must be a boolean")
            if job.timing.snapshot()["status"] != "running":
                return
            try:
                if waiting:
                    job.timing.pause()
                else:
                    job.timing.resume()
            except RuntimeError:
                # Terminalization can win a race with an already-dispatched
                # gate notification. Such a stale notification is harmless.
                if job.timing.snapshot()["status"] != "running":
                    return
                raise

        unsubscribe = subscribe(waiting_changed)
        if not callable(unsubscribe):
            raise TypeError("sign-in wait subscription must be removable")
        return unsubscribe

    def _require(self, job_id: str) -> _CollectionJob:
        try:
            return self._jobs[job_id]
        except (KeyError, TypeError):
            raise KeyError("unknown weekly collection job") from None

    def _record(self, job: _CollectionJob) -> dict[str, object]:
        progress = job.progress
        sign_in = None
        gate = getattr(self._workflow, "sign_in_gate", None)
        status = getattr(gate, "status", None)
        if callable(status) and job.status in {"queued", "running"}:
            sign_in = _sign_in_record(status())
        return {
            "job_id": job.job_id,
            "status": job.status,
            "error": job.error,
            "bundle_id": job.bundle_id,
            "cancel_requested": job.cancel.is_set(),
            "sign_in": sign_in,
            "request": {
                "season": job.request.season,
                "week": job.request.week,
                "scoring": job.request.scoring,
                "expected_team_count": job.request.expected_team_count,
                "host_league_configured": job.request.host_league_url is not None,
                "yahoo_projection_league_configured": (
                    job.request.yahoo_projection_league_url is not None
                ),
                "include_future_weekly": job.request.include_future_weekly,
                "allow_surrogate_power": job.request.allow_surrogate_power,
            },
            "progress": None
            if progress is None
            else {
                "stage": progress.stage.value,
                "fraction": progress.fraction,
                "message": progress.message,
            },
            "operation": job.timing.snapshot(),
        }


def _sign_in_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "pending_provider", "confirmed_providers"
    }:
        raise ValueError("interactive sign-in status is invalid")
    pending = value["pending_provider"]
    if pending is not None and pending not in {"fantasypros", "espn", "yahoo"}:
        raise ValueError("interactive sign-in provider is invalid")
    confirmed = value["confirmed_providers"]
    if not isinstance(confirmed, list) or any(
        provider not in {"fantasypros", "espn", "yahoo"} for provider in confirmed
    ) or len(set(confirmed)) != len(confirmed):
        raise ValueError("confirmed sign-in providers are invalid")
    return {
        "pending_provider": pending,
        "confirmed_providers": list(confirmed),
    }


_YAHOO_NUMERIC_LEAGUE_PATH = re.compile(
    r"^/(?:(?P<season>20[0-9]{2})/)?f1/"
    r"(?P<league>[1-9][0-9]{0,19})"
    r"(?:/(?P<page>players|playersearch|[1-9][0-9]{0,19}))?/?$"
)
_ESPN_ROUTE = r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,3}"
# Pasted ESPN links are identifiers, not navigation targets.  Accept ESPN's
# changing league-page routes while keeping the exact host/sport boundary and
# retaining only the one numeric leagueId used to build our own allowlisted URLs.
_ESPN_FANTASY_PATH = re.compile(rf"^/football(?:/{_ESPN_ROUTE})?/?$")
_ESPN_WWW_PATH = re.compile(rf"^/fantasy/football(?:/{_ESPN_ROUTE})?/?$")
_ESPN_LEGACY_PATH = re.compile(r"^/football/?$")
_ESPN_LEGACY_ROUTE = re.compile(rf"^/?{_ESPN_ROUTE}(?:\?|$)")


def _host_league_url(value: object, requested_season: int) -> str | None:
    parsed = _optional_https_url("host_league_url", value)
    if parsed is None:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    legacy_route = bool(
        host == "fantasy.espn.com"
        and _ESPN_LEGACY_PATH.fullmatch(parsed.path)
        and _ESPN_LEGACY_ROUTE.match(parsed.fragment)
    )
    valid_path = (
        host == "fantasy.espn.com"
        and (bool(_ESPN_FANTASY_PATH.fullmatch(parsed.path)) or legacy_route)
    ) or (
        host in {"espn.com", "www.espn.com"}
        and bool(_ESPN_WWW_PATH.fullmatch(parsed.path))
    )
    league_id = _query_value(parsed, "leagueid", include_fragment=legacy_route)
    season_id = _query_value(
        parsed, "seasonid", required=False, include_fragment=legacy_route
    )
    if season_id is not None and season_id != str(requested_season):
        raise ValueError(
            f"The ESPN link is for {season_id or 'an unknown season'}, but the "
            f"selected season is {requested_season}."
        )
    if valid_path and _league_number(league_id):
        suffix = f"&seasonId={season_id}" if season_id is not None else ""
        return (
            "https://fantasy.espn.com/football/league?"
            f"leagueId={league_id}{suffix}"
        )
    raise ValueError(
        "Paste an ESPN Fantasy Football league page whose address contains "
        "one numeric leagueId (League Home works)."
    )


def _yahoo_projection_url(value: object, requested_season: int) -> str | None:
    parsed = _optional_https_url("yahoo_projection_league_url", value)
    if parsed is None:
        return None
    if (parsed.hostname or "").casefold().rstrip(".") == (
        "football.fantasysports.yahoo.com"
    ):
        match = _YAHOO_NUMERIC_LEAGUE_PATH.fullmatch(parsed.path)
        if match is not None:
            season = match.group("season")
            if season is not None and season != str(requested_season):
                raise ValueError(
                    f"The Yahoo link is for {season}, but the selected season is "
                    f"{requested_season}."
                )
            prefix = f"/{season}" if season is not None else ""
            return (
                "https://football.fantasysports.yahoo.com"
                f"{prefix}/f1/{match.group('league')}/players?status=ALL"
            )
    raise ValueError(
        "Use a numeric Yahoo league home, team, Players, or Player List address "
        "that contains /f1/ followed by the league ID"
    )


def _optional_https_url(name: str, value: object):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip():
        raise ValueError(f"{name} must be a web address or empty")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name} is invalid") from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise ValueError(f"{name} must be a safe HTTPS web address")
    return parsed


def _query_value(
    parsed, key: str, *, required: bool = True, include_fragment: bool = False
) -> str | None:
    """Read one case-insensitive value from normal or legacy hash routing."""

    pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
    if include_fragment:
        fragment = parsed.fragment.lstrip("#/")
        if "?" in fragment:
            pairs.extend(parse_qsl(fragment.split("?", 1)[1], keep_blank_values=True))
    values = [value for name, value in pairs if name.casefold() == key]
    if not values:
        return "" if required else None
    if len(values) != 1:
        raise ValueError(f"The ESPN link must contain only one {key} value.")
    return values[0]


def _league_number(value: str) -> bool:
    return (
        value.isascii()
        and value.isdigit()
        and not value.startswith("0")
        and len(value) <= 20
    )


__all__ = (
    "WeeklyCollectionError",
    "WeeklyCollectionJobs",
    "WeeklyCollectionProgress",
    "WeeklyCollectionRequest",
    "WeeklyCollectionStage",
    "WeeklyCollectionWorkflow",
)
