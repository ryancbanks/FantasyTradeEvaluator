"""Loopback-only HTTP server for the non-technical local application."""

import json
import re
import shutil
import webbrowser
from functools import lru_cache
from hmac import compare_digest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from secrets import token_urlsafe
from threading import Event, Lock, Thread, Timer
from time import monotonic
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from ._app_support import default_data_directory
from .app_service import LocalAppService, LocalSearchRequest
from .extension_bridge import (
    COMMAND_RESULT_MAX_BYTES,
    PAIR_REQUEST_MAX_BYTES,
    SESSION_TOKEN_HEADER,
    BridgeAuthenticationError,
    BridgePayloadError,
    BridgeProtocolError,
    BridgeStaleCommandError,
    BridgeStateError,
    ExtensionCommandBridge,
)
from .source_catalog import weekly_source_catalog
from .web_asset_manifest import WEB_ASSET_ROUTES
from .weekly_collection import WeeklyCollectionRequest, WeeklyCollectionWorkflow

_JOB_PATH = re.compile(r"^/api/searches/([0-9a-f]{32})$")
_JOB_ACTION_PATH = re.compile(
    r"^/api/searches/([0-9a-f]{32})/(activity-ack|cancel|export|results)$"
)
_DASHBOARD_PATH = re.compile(r"^/api/bundles/(engine_[0-9a-f]{64})/dashboard$")
_PLAYER_OUTLOOK_PATH = re.compile(
    r"^/api/bundles/(engine_[0-9a-f]{64})/player-outlook$"
)
_PLAYER_OUTLOOK_DETAIL_PATH = re.compile(
    r"^/api/bundles/(engine_[0-9a-f]{64})/player-outlook/players/([^/]+)$"
)
_GM_INSIGHTS_PATH = re.compile(
    r"^/api/bundles/(engine_[0-9a-f]{64})/gm-insights$"
)
_TRADE_TIMING_PATH = re.compile(
    r"^/api/bundles/(engine_[0-9a-f]{64})/trade-timing$"
)
_COLLECTION_PATH = re.compile(r"^/api/weekly-collections/([0-9a-f]{32})$")
_COLLECTION_CANCEL_PATH = re.compile(
    r"^/api/weekly-collections/([0-9a-f]{32})/cancel$"
)
_COLLECTION_SIGN_IN_PATH = re.compile(
    r"^/api/weekly-collections/([0-9a-f]{32})/sign-in$"
)
_COLLECTION_ACTIVITY_ACK_PATH = re.compile(
    r"^/api/weekly-collections/([0-9a-f]{32})/activity-ack$"
)
_PROFILE_ID = r"league_[0-9a-f]{32}"
_BUNDLE_ID = r"engine_[0-9a-f]{64}"
_LEAGUE_PATH = re.compile(rf"^/api/leagues/({_PROFILE_ID})$")
_LEAGUE_ACTION_PATH = re.compile(
    rf"^/api/leagues/({_PROFILE_ID})/(archive|restore|team)$"
)
_LEAGUE_BUNDLES_PATH = re.compile(
    rf"^/api/leagues/({_PROFILE_ID}|unassigned)/bundles$"
)
_LEAGUE_BUNDLE_IMPORT_PATH = re.compile(
    rf"^/api/leagues/({_PROFILE_ID}|unassigned)/bundles/import$"
)
_LEAGUE_BUNDLE_ASSIGN_PATH = re.compile(
    rf"^/api/leagues/({_PROFILE_ID})/bundles/({_BUNDLE_ID})/assign$"
)
_BUNDLE_PATH = re.compile(rf"^/api/bundles/({_BUNDLE_ID})$")
_EXPORT_PATH = re.compile(r"^/api/exports/([^/]+\.xlsx)$")
_DRAFT_JOB_PATH = re.compile(r"^/api/draft/jobs/([0-9a-f]{32})$")
_DRAFT_JOB_ACTION_PATH = re.compile(
    r"^/api/draft/jobs/([0-9a-f]{32})/(activity-ack|cancel|result)$"
)
_DRAFT_CHECKPOINT_ACTION_PATH = re.compile(
    r"^/api/draft/checkpoints/([0-9a-f]{32})/promote$"
)
_DRAFT_ASSISTANT_PATH = re.compile(r"^/api/draft/assistants/([0-9a-f]{32})$")
_DRAFT_ASSISTANT_ACTION_PATH = re.compile(
    r"^/api/draft/assistants/([0-9a-f]{32})/(players|picks|undo|espn-sync)$"
)
_DRAFT_MODEL_PATH = re.compile(r"^/api/draft/models/(draft_model_[0-9a-f]{64})/export$")
_STATIC = WEB_ASSET_ROUTES
_MAX_JSON_BYTES = 256 * 1024 * 1024
_MAX_DRAFT_DATA_BYTES = 128 * 1024 * 1024
_MAX_PLAYER_ID_LENGTH = 256
_EXTENSION_ROOT = "/api/browser-extension/v1"
_CLIENT_ID_HEADER = "X-FTE-Client"
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class LocalAppHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address,
        service: LocalAppService,
        extension_bridge: ExtensionCommandBridge | None = None,
    ):
        self.app_service = service
        self.extension_bridge = extension_bridge or ExtensionCommandBridge()
        self.app_token = token_urlsafe(32)
        self._lifecycle_lock = Lock()
        self._lifecycle_changed = Event()
        self._created_at = monotonic()
        self._browser_clients: dict[str, float] = {}
        self._last_client_departed: float | None = None
        self._browser_connected = False
        self._lifecycle_started = False
        super().__init__(address, LocalAppRequestHandler)

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def browser_ping(self, client_id: str = "legacy") -> None:
        if not _CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ValueError("browser client ID is invalid")
        with self._lifecycle_lock:
            self._browser_clients[client_id] = monotonic()
            self._last_client_departed = None
            self._browser_connected = True
        self._lifecycle_changed.set()

    def browser_close(self, client_id: str = "legacy") -> None:
        if not _CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ValueError("browser client ID is invalid")
        with self._lifecycle_lock:
            self._browser_clients.pop(client_id, None)
            if self._browser_connected and not self._browser_clients:
                self._last_client_departed = monotonic()
        self._lifecycle_changed.set()

    def start_browser_lifecycle(
        self,
        *,
        launch_timeout: float = 120.0,
        idle_timeout: float = 180.0,
        close_grace: float = 30.0,
    ) -> Thread:
        if min(launch_timeout, idle_timeout, close_grace) <= 0:
            raise ValueError("browser lifecycle timeouts must be positive")
        with self._lifecycle_lock:
            if self._lifecycle_started:
                raise RuntimeError("browser lifecycle is already running")
            self._lifecycle_started = True
        monitor = Thread(
            target=self._monitor_browser,
            args=(launch_timeout, idle_timeout, close_grace),
            name="browser-lifecycle",
            daemon=True,
        )
        monitor.start()
        return monitor

    def _monitor_browser(
        self, launch_timeout: float, idle_timeout: float, close_grace: float
    ) -> None:
        while True:
            now = monotonic()
            with self._lifecycle_lock:
                stale = [
                    client_id
                    for client_id, ping in self._browser_clients.items()
                    if now - ping >= idle_timeout
                ]
                for client_id in stale:
                    del self._browser_clients[client_id]
                if stale and not self._browser_clients:
                    self._last_client_departed = now
                connected = self._browser_connected
                clients_remain = bool(self._browser_clients)
                departed = self._last_client_departed
            expired = not connected and now - self._created_at >= launch_timeout
            expired = expired or (
                connected
                and not clients_remain
                and departed is not None
                and now - departed >= close_grace
                and not self.app_service.is_busy
            )
            if expired:
                self.shutdown()
                return
            self._lifecycle_changed.wait(timeout=min(close_grace, 1.0))
            self._lifecycle_changed.clear()


class LocalAppRequestHandler(BaseHTTPRequestHandler):
    server: LocalAppHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "FantasyTradeEvaluator"
    sys_version = ""

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Cross-origin requests are not allowed."})

    def log_message(self, _format: str, *_args) -> None:
        return

    def _dispatch(self, operation) -> None:
        try:
            if not self._valid_host():
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid local host header."})
                return
            operation()
        except (ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (FileNotFoundError, KeyError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "The requested local item was not found."})
        except BridgeAuthenticationError as error:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(error)})
        except (BridgeProtocolError, BridgePayloadError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (BridgeStateError, BridgeStaleCommandError) as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})
        except RuntimeError as error:
            self._json(HTTPStatus.CONFLICT, {"error": str(error)})
        except PermissionError as error:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(error)})
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Unexpected local application error."})

    def _get(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        if path == "/":
            template = _asset("index.html").decode("utf-8")
            body = template.replace("__APP_TOKEN__", self.server.app_token).encode("utf-8")
            self._bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return
        if path in _STATIC:
            filename, content_type = _STATIC[path]
            self._bytes(HTTPStatus.OK, _asset(filename), content_type)
            return
        if path == "/browser-extension.zip":
            self._bytes(
                HTTPStatus.OK,
                _extension_archive(),
                "application/zip",
                disposition='attachment; filename="FantasyTradeEvaluator-Browser-Extension.zip"',
            )
            return
        self._require_token()
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ready", "version": "0.2.0"})
            return
        if path == "/api/activity":
            self._json(
                HTTPStatus.OK,
                self.server.app_service.active_job_catalog(),
            )
            return
        if path == "/api/bundles":
            self._json(HTTPStatus.OK, self.server.app_service.bundle_catalog())
            return
        if self._league_get(path, split.query):
            return
        if path == "/api/draft/catalog":
            self._json(HTTPStatus.OK, self.server.app_service.draft_lab.catalog())
            return
        matched = _DRAFT_JOB_PATH.fullmatch(path)
        if matched:
            self._json(HTTPStatus.OK, self.server.app_service.draft_lab.job(matched.group(1)))
            return
        matched = _DRAFT_JOB_ACTION_PATH.fullmatch(path)
        if matched and matched.group(2) == "result":
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.job_result(matched.group(1)),
            )
            return
        matched = _DRAFT_ASSISTANT_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.assistant(matched.group(1)),
            )
            return
        matched = _DRAFT_ASSISTANT_ACTION_PATH.fullmatch(path)
        if matched and matched.group(2) == "players":
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.assistant_players(matched.group(1)),
            )
            return
        matched = _DRAFT_MODEL_PATH.fullmatch(path)
        if matched:
            model_path = self.server.app_service.draft_lab.model_path(matched.group(1))
            self._bytes(
                HTTPStatus.OK,
                model_path.read_bytes(),
                "application/json; charset=utf-8",
                disposition=f'attachment; filename="{model_path.name}"',
            )
            return
        matched = _DASHBOARD_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.league_dashboard(matched.group(1)),
            )
            return
        matched = _PLAYER_OUTLOOK_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.player_outlook_catalog(matched.group(1)),
            )
            return
        matched = _PLAYER_OUTLOOK_DETAIL_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.player_outlook_detail(
                    matched.group(1), _decode_player_id(matched.group(2))
                ),
            )
            return
        matched = _GM_INSIGHTS_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.gm_insights(matched.group(1)),
            )
            return
        matched = _TRADE_TIMING_PATH.fullmatch(path)
        if matched:
            query = parse_qs(split.query, keep_blank_values=True)
            if set(query) != {"primaryTeamId"} or len(query["primaryTeamId"]) != 1:
                raise ValueError("trade timing requires exactly one primaryTeamId")
            primary_team_id = query["primaryTeamId"][0]
            if not primary_team_id:
                raise ValueError("primaryTeamId must be a non-empty string")
            self._json(
                HTTPStatus.OK,
                self.server.app_service.trade_timing(
                    matched.group(1), primary_team_id
                ),
            )
            return
        if path == "/api/browser-extension/status":
            self._json(HTTPStatus.OK, self.server.extension_bridge.public_status())
            return
        matched = _COLLECTION_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.weekly_collection(matched.group(1)),
            )
            return
        matched = _JOB_PATH.fullmatch(path)
        if matched:
            self._json(HTTPStatus.OK, self.server.app_service.job(matched.group(1)))
            return
        matched = _JOB_ACTION_PATH.fullmatch(path)
        if matched and matched.group(2) == "results":
            self._json(HTTPStatus.OK, self.server.app_service.job_results(matched.group(1)))
            return
        matched = _EXPORT_PATH.fullmatch(path)
        if matched:
            self._download(unquote(matched.group(1)))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def _post(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith(_EXTENSION_ROOT + "/"):
            self._extension_post(path)
            return
        self._require_token()
        if path == "/api/browser-extension/pairing":
            self._require_empty_body()
            self._json(
                HTTPStatus.CREATED,
                self.server.extension_bridge.create_pairing(),
            )
            return
        if path == "/api/bundles/import":
            summary = self.server.app_service.import_bundle(self._read_json())
            self._json(HTTPStatus.CREATED, summary)
            return
        if self._league_post(path):
            return
        if path == "/api/draft/corpus/install":
            self._json(
                HTTPStatus.ACCEPTED,
                self.server.app_service.draft_lab.start_corpus_install(
                    self._read_json(max_bytes=1024)
                ),
            )
            return
        if path == "/api/draft/corpora/import":
            self._json(
                HTTPStatus.CREATED,
                self.server.app_service.draft_lab.import_corpus(
                    self._read_json(max_bytes=_MAX_DRAFT_DATA_BYTES)
                ),
            )
            return
        if path == "/api/draft/boards/import":
            self._json(
                HTTPStatus.CREATED,
                self.server.app_service.draft_lab.import_board(
                    self._read_json(max_bytes=_MAX_DRAFT_DATA_BYTES)
                ),
            )
            return
        if path == "/api/draft/models/import":
            self._json(
                HTTPStatus.CREATED,
                self.server.app_service.draft_lab.import_model(self._read_json(max_bytes=16 * 1024 * 1024)),
            )
            return
        if path in {"/api/draft/trainings", "/api/draft/trainings/estimate"}:
            payload = self._read_json()
            if path.endswith("/estimate"):
                self._json(
                    HTTPStatus.OK,
                    self.server.app_service.draft_lab.estimate_training(payload),
                )
            else:
                self._json(
                    HTTPStatus.ACCEPTED,
                    self.server.app_service.draft_lab.start_training(payload),
                )
            return
        if path == "/api/draft/trainings/resume":
            payload = self._read_json(max_bytes=1024)
            if (
                not isinstance(payload, dict)
                or not {"checkpoint_job_id"}.issubset(payload)
                or not set(payload).issubset({"checkpoint_job_id", "generations"})
            ):
                raise ValueError("training resume fields are invalid")
            self._json(
                HTTPStatus.ACCEPTED,
                self.server.app_service.draft_lab.resume_training(
                    payload["checkpoint_job_id"], payload.get("generations")
                ),
            )
            return
        matched = _DRAFT_CHECKPOINT_ACTION_PATH.fullmatch(path)
        if matched:
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.promote_checkpoint(
                    matched.group(1)
                ),
            )
            return
        if path == "/api/draft/benchmarks":
            self._json(
                HTTPStatus.ACCEPTED,
                self.server.app_service.draft_lab.start_benchmark(
                    self._read_json(max_bytes=64 * 1024)
                ),
            )
            return
        if path == "/api/draft/assistants":
            self._json(
                HTTPStatus.CREATED,
                self.server.app_service.draft_lab.create_assistant(
                    self._read_json(max_bytes=64 * 1024)
                ),
            )
            return
        if path == "/api/weekly-sources":
            request = WeeklyCollectionRequest.from_payload(
                self._read_json(max_bytes=32 * 1024)
            )
            self._json(HTTPStatus.OK, weekly_source_catalog(request))
            return
        if path == "/api/weekly-collections":
            self._json(
                HTTPStatus.ACCEPTED,
                self._start_weekly_collection(),
            )
            return
        if path in {"/api/searches", "/api/searches/estimate"}:
            request = LocalSearchRequest.from_payload(self._read_json(max_bytes=1024 * 1024))
            if path.endswith("/estimate"):
                self._json(HTTPStatus.OK, self.server.app_service.estimate_search(request))
            else:
                self._json(HTTPStatus.ACCEPTED, self.server.app_service.start_search(request))
            return
        if path == "/api/session/ping":
            self._require_empty_body()
            self.server.browser_ping(self._browser_client_id())
            self._json(HTTPStatus.OK, {"status": "active"})
            return
        if path == "/api/session/close":
            self._require_empty_body()
            self.server.browser_close(self._browser_client_id())
            self._json(HTTPStatus.OK, {"status": "closing"})
            return
        matched = _JOB_ACTION_PATH.fullmatch(path)
        if matched and matched.group(2) == "activity-ack":
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.acknowledge_search_activity(matched.group(1)),
            )
            return
        if matched and matched.group(2) == "cancel":
            self._require_empty_body()
            self._json(HTTPStatus.OK, self.server.app_service.cancel_job(matched.group(1)))
            return
        if matched and matched.group(2) == "export":
            self._require_empty_body()
            self._json(HTTPStatus.CREATED, self.server.app_service.export_job(matched.group(1)))
            return
        matched = _DRAFT_JOB_ACTION_PATH.fullmatch(path)
        if matched and matched.group(2) == "activity-ack":
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.acknowledge_job_activity(
                    matched.group(1)
                ),
            )
            return
        if matched and matched.group(2) == "cancel":
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.cancel_job(matched.group(1)),
            )
            return
        matched = _DRAFT_ASSISTANT_ACTION_PATH.fullmatch(path)
        if matched and matched.group(2) == "picks":
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.record_pick(
                    matched.group(1), self._read_json(max_bytes=16 * 1024)
                ),
            )
            return
        if matched and matched.group(2) == "espn-sync":
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.sync_espn_draft(
                    matched.group(1), self._read_json(max_bytes=1024)
                ),
            )
            return
        if matched and matched.group(2) == "undo":
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.draft_lab.undo_pick(matched.group(1)),
            )
            return
        matched = _COLLECTION_CANCEL_PATH.fullmatch(path)
        if matched:
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.cancel_weekly_collection(matched.group(1)),
            )
            return
        matched = _COLLECTION_ACTIVITY_ACK_PATH.fullmatch(path)
        if matched:
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.acknowledge_weekly_collection_activity(
                    matched.group(1)
                ),
            )
            return
        matched = _COLLECTION_SIGN_IN_PATH.fullmatch(path)
        if matched:
            self._require_empty_body()
            self._json(
                HTTPStatus.OK,
                self.server.app_service.confirm_weekly_collection_sign_in(
                    matched.group(1)
                ),
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def _league_get(self, path: str, query: str) -> bool:
        if path == "/api/leagues":
            self._json(
                HTTPStatus.OK,
                self.server.app_service.league_profiles(
                    **_league_page_options(query)
                ),
            )
            return True
        matched = _LEAGUE_BUNDLES_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.league_bundle_catalog(matched.group(1)),
            )
            return True
        matched = _BUNDLE_PATH.fullmatch(path)
        if matched:
            self._json(
                HTTPStatus.OK,
                self.server.app_service.bundle(matched.group(1)),
            )
            return True
        return False

    def _league_post(self, path: str) -> bool:
        if path == "/api/leagues":
            response = self.server.app_service.create_league_profile(
                self._read_json(max_bytes=32 * 1024)
            )
            self._json(HTTPStatus.CREATED, response)
            return True
        matched = _LEAGUE_PATH.fullmatch(path)
        if matched:
            response = self.server.app_service.update_league_profile(
                matched.group(1), self._read_json(max_bytes=32 * 1024)
            )
            self._json(HTTPStatus.OK, response)
            return True
        matched = _LEAGUE_ACTION_PATH.fullmatch(path)
        if matched:
            profile_id, action = matched.groups()
            if action == "team":
                response = self.server.app_service.save_league_team(
                    profile_id, self._read_json(max_bytes=4 * 1024)
                )
            else:
                self._require_empty_body()
                operation = (
                    self.server.app_service.archive_league_profile
                    if action == "archive"
                    else self.server.app_service.restore_league_profile
                )
                response = operation(profile_id)
            self._json(HTTPStatus.OK, response)
            return True
        matched = _LEAGUE_BUNDLE_IMPORT_PATH.fullmatch(path)
        if matched:
            profile_id = matched.group(1)
            response = self.server.app_service.import_bundle(
                self._read_json(),
                league_profile_id=(
                    None if profile_id == "unassigned" else profile_id
                ),
            )
            self._json(HTTPStatus.CREATED, response)
            return True
        matched = _LEAGUE_BUNDLE_ASSIGN_PATH.fullmatch(path)
        if matched:
            self._require_empty_body()
            response = self.server.app_service.assign_bundle_to_league(
                *matched.groups()
            )
            self._json(HTTPStatus.OK, response)
            return True
        return False

    def _start_weekly_collection(self) -> dict[str, object]:
        payload = self._read_json(max_bytes=32 * 1024)
        if isinstance(payload, dict) and "league_profile_id" in payload:
            profile_id = payload.pop("league_profile_id")
            return self.server.app_service.start_profile_weekly_collection(
                profile_id, payload
            )
        request = WeeklyCollectionRequest.from_payload(payload)
        return self.server.app_service.start_weekly_collection(request)

    def _extension_post(self, path: str) -> None:
        bridge = self.server.extension_bridge
        if path == _EXTENSION_ROOT + "/pair":
            value = self._read_json(max_bytes=PAIR_REQUEST_MAX_BYTES)
            if not isinstance(value, dict) or set(value) != {
                "pair_code", "protocol_version", "capabilities", "extension_version"
            }:
                raise ValueError("extension pairing fields are invalid")
            self._json(
                HTTPStatus.OK,
                bridge.connect(
                    value["pair_code"],
                    value["protocol_version"],
                    value["capabilities"],
                    value["extension_version"],
                ),
            )
            return

        token = self.headers.get(SESSION_TOKEN_HEADER, "")
        if path == _EXTENSION_ROOT + "/poll":
            value = self._read_json(max_bytes=1024)
            if not isinstance(value, dict) or set(value) != {"wait_seconds"}:
                raise ValueError("extension poll fields are invalid")
            self._json(HTTPStatus.OK, bridge.poll(token, value["wait_seconds"]))
            return
        if path == _EXTENSION_ROOT + "/result":
            value = self._read_json(max_bytes=COMMAND_RESULT_MAX_BYTES + 4096)
            if not isinstance(value, dict) or "command_id" not in value:
                raise ValueError("extension result fields are invalid")
            keys = set(value)
            if keys == {"command_id", "result"}:
                response = bridge.complete(
                    token, value["command_id"], result=value["result"]
                )
            elif keys == {"command_id", "error"}:
                response = bridge.complete(
                    token, value["command_id"], error=value["error"]
                )
            else:
                raise ValueError("extension result fields are invalid")
            self._json(HTTPStatus.OK, response)
            return
        if path == _EXTENSION_ROOT + "/disconnect":
            value = self._read_json(max_bytes=64)
            if value != {}:
                raise ValueError("extension disconnect body must be an empty object")
            self._json(HTTPStatus.OK, bridge.disconnect(token))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "")
        expected_port = str(self.server.server_address[1])
        if host.count(":") > 1:
            return False
        hostname, separator, port = host.partition(":")
        return hostname in {"127.0.0.1", "localhost"} and (
            not separator or port == expected_port
        )

    def _require_token(self) -> None:
        supplied = self.headers.get("X-FTE-Token", "")
        if not compare_digest(supplied, self.server.app_token):
            raise PermissionError("missing local application token")

    def _browser_client_id(self) -> str:
        client_id = self.headers.get(_CLIENT_ID_HEADER, "legacy")
        if not _CLIENT_ID_PATTERN.fullmatch(client_id):
            raise ValueError("browser client ID is invalid")
        return client_id

    def _read_json(self, *, max_bytes: int = _MAX_JSON_BYTES):
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = self._content_length(max_bytes)
        if length == 0:
            raise ValueError("JSON request body cannot be empty")
        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("JSON request body must be UTF-8") from None
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)

    def _require_empty_body(self) -> None:
        if self._content_length(0) != 0:
            raise ValueError("request body must be empty")

    def _content_length(self, maximum: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw)
        except ValueError:
            raise ValueError("Content-Length is invalid") from None
        if length < 0 or length > maximum:
            raise ValueError("request body is too large")
        return length

    def _json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._bytes(status, body, "application/json; charset=utf-8")

    def _bytes(self, status, body: bytes, content_type: str, *, disposition=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if disposition is not None:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def _download(self, filename: str) -> None:
        path = self.server.app_service.export_path(filename)
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)


def create_local_server(
    data_directory: str | Path | None = None,
    *,
    port: int = 0,
    weekly_collection_workflow: WeeklyCollectionWorkflow | None = None,
    extension_bridge: ExtensionCommandBridge | None = None,
) -> LocalAppHTTPServer:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer between 0 and 65535")
    service = LocalAppService(
        data_directory or default_data_directory(),
        weekly_collection_workflow=weekly_collection_workflow,
    )
    return LocalAppHTTPServer(
        ("127.0.0.1", port), service, extension_bridge=extension_bridge
    )


def serve_local_app(
    data_directory: str | Path | None = None,
    *,
    port: int = 0,
    open_browser: bool = True,
    weekly_collection_workflow: WeeklyCollectionWorkflow | None = None,
    extension_bridge: ExtensionCommandBridge | None = None,
) -> None:
    server = create_local_server(
        data_directory,
        port=port,
        weekly_collection_workflow=weekly_collection_workflow,
        extension_bridge=extension_bridge,
    )
    if open_browser:
        server.start_browser_lifecycle()
        Timer(0.25, lambda: webbrowser.open(server.app_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.extension_bridge.close()
        server.server_close()


def _asset(name: str) -> bytes:
    return files("trade_snapshot.web_assets").joinpath(name).read_bytes()


def _decode_player_id(value: str) -> str:
    try:
        player_id = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("player ID path must be valid UTF-8") from None
    if not player_id or len(player_id) > _MAX_PLAYER_ID_LENGTH:
        raise ValueError("player ID path length is invalid")
    if any(character in "/\\" or ord(character) < 32 or ord(character) == 127 for character in player_id):
        raise ValueError("player ID path contains a forbidden character")
    return player_id


@lru_cache(maxsize=1)
def _extension_archive() -> bytes:
    """Build a deterministic load-unpacked extension archive from packaged resources."""

    root = files("trade_snapshot.browser_extension")
    entries = []
    allowed_suffixes = {".css", ".html", ".js", ".json", ".md"}

    def collect(directory, prefix=""):
        for child in directory.iterdir():
            name = f"{prefix}{child.name}"
            if child.is_dir():
                if child.name != "__pycache__":
                    collect(child, name + "/")
            elif Path(child.name).suffix.casefold() in allowed_suffixes:
                entries.append((name, child.read_bytes()))

    collect(root)
    if not any(name == "manifest.json" for name, _ in entries):
        raise RuntimeError("packaged browser extension is incomplete")
    target = BytesIO()
    with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(entries):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, body)
    return target.getvalue()


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value}")


def _league_page_options(query: str) -> dict[str, object]:
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    allowed = {"include_archived", "limit", "cursor"}
    if any(key not in allowed for key, _ in pairs):
        raise ValueError("league list query fields are invalid")
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("league list query fields cannot be repeated")
    values = dict(pairs)

    include_archived = values.get("include_archived", "false")
    if include_archived not in {"true", "false"}:
        raise ValueError("include_archived must be true or false")
    raw_limit = values.get("limit", "100")
    if not raw_limit.isascii() or not raw_limit.isdigit():
        raise ValueError("limit must be an integer from 1 through 250")
    limit = int(raw_limit)
    if not 1 <= limit <= 250:
        raise ValueError("limit must be an integer from 1 through 250")
    cursor = values.get("cursor")
    if cursor == "":
        raise ValueError("cursor cannot be empty")
    return {
        "include_archived": include_archived == "true",
        "limit": limit,
        "cursor": cursor,
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result
