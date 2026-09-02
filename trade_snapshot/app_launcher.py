"""Stable application entry point used by source and frozen releases."""

import argparse
from html import escape
import http.client
from importlib import resources
import json
from multiprocessing import freeze_support
from pathlib import Path, PurePosixPath
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import quote
import webbrowser

from .local_server import create_local_server, serve_local_app
from .production_collection import create_production_weekly_collection_workflow
from .extension_bridge import ExtensionCommandBridge


_EXTENSION_COMPANION_ASSETS = (
    "manifest.json",
    "popup/popup.css",
    "popup/popup.js",
    "README.md",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the local Fantasy Trade Evaluator.")
    parser.add_argument("--data-directory", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    freeze_support()
    args = build_parser().parse_args(argv)
    if args.self_check:
        try:
            result = runtime_self_check()
        except Exception as error:
            print(f"Packaged runtime check failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        extension_bridge = ExtensionCommandBridge()
        collection_workflow = create_production_weekly_collection_workflow(extension_bridge)
        serve_local_app(
            args.data_directory,
            port=args.port,
            open_browser=not args.no_browser,
            weekly_collection_workflow=collection_workflow,
            extension_bridge=extension_bridge,
        )
    except (OSError, RuntimeError, ValueError) as error:
        message = f"Fantasy Trade Evaluator could not start: {error}"
        print(message, file=sys.stderr)
        if getattr(sys, "frozen", False):
            _show_frozen_error(message)
        return 2
    return 0


def runtime_self_check() -> dict[str, object]:
    """Verify the local interface, extension package, and server lifecycle."""

    page = resources.files("trade_snapshot.web_assets").joinpath("index.html")
    if not page.read_bytes().startswith(b"<!doctype html>"):
        raise RuntimeError("local interface assets are missing")
    extension = resources.files("trade_snapshot.browser_extension")
    try:
        manifest = json.loads(extension.joinpath("manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise RuntimeError("browser extension manifest is invalid")
        required_extension_files = _required_extension_assets(manifest)
        assets_complete = all(
            extension.joinpath(*PurePosixPath(name).parts).read_bytes()
            for name in required_extension_files
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("browser extension assets are missing") from None
    if manifest.get("manifest_version") != 3 or not assets_complete:
        raise RuntimeError("browser extension assets are incomplete")
    _lifecycle_self_check()
    return {
        "status": "ready",
        "web_assets": True,
        "browser_extension": manifest.get("version"),
    }


def _required_extension_assets(manifest: dict[str, object]) -> tuple[str, ...]:
    """Return every packaged file needed by the extension manifest and popup."""

    background = manifest.get("background")
    action = manifest.get("action")
    content_scripts = manifest.get("content_scripts")
    if not isinstance(background, dict) or not isinstance(action, dict):
        raise RuntimeError("browser extension manifest is incomplete")
    if not isinstance(content_scripts, list):
        raise RuntimeError("browser extension manifest is incomplete")
    declared = [background.get("service_worker"), action.get("default_popup")]
    for entry in content_scripts:
        if not isinstance(entry, dict) or not isinstance(entry.get("js"), list):
            raise RuntimeError("browser extension manifest is incomplete")
        declared.extend(entry["js"])
        stylesheets = entry.get("css", [])
        if not isinstance(stylesheets, list):
            raise RuntimeError("browser extension manifest is incomplete")
        declared.extend(stylesheets)
    paths = set(_EXTENSION_COMPANION_ASSETS)
    for value in declared:
        if not isinstance(value, str) or not value:
            raise RuntimeError("browser extension manifest contains an invalid asset path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise RuntimeError("browser extension manifest contains an invalid asset path")
        paths.add(value)
    return tuple(sorted(paths))


def _lifecycle_self_check() -> None:
    with TemporaryDirectory(prefix="fantasy-trade-evaluator-check-") as directory:
        server = create_local_server(directory)
        serving = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        serving.start()
        server.start_browser_lifecycle(
            launch_timeout=5.0, idle_timeout=5.0, close_grace=0.05
        )
        try:
            for path in ("/api/session/ping", "/api/session/close"):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=2
                )
                connection.request(
                    "POST", path, body="", headers={"X-FTE-Token": server.app_token}
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status != 200:
                    raise RuntimeError(f"local lifecycle endpoint failed: {path}")
            serving.join(timeout=2)
            if serving.is_alive():
                raise RuntimeError("local browser-close lifecycle did not stop the server")
        finally:
            if serving.is_alive():
                server.shutdown()
                serving.join(timeout=2)
            server.server_close()


def _show_frozen_error(message: str) -> None:
    page = (
        "<!doctype html><meta charset=utf-8><meta name=color-scheme content=dark>"
        "<meta name=theme-color content=#071014><title>Fantasy Trade Evaluator</title>"
        "<style>html{background:#071014;color-scheme:dark}body{font:16px system-ui;"
        "max-width:42rem;margin:4rem auto;padding:0 1rem;background:#071014;color:#eef6f7}"
        "h1{color:#ff9aa1}</style><h1>The app could not start</h1><p>"
        f"{escape(message)}</p><p>Close this tab and try opening the app again.</p>"
    )
    webbrowser.open("data:text/html;charset=utf-8," + quote(page))


if __name__ == "__main__":
    raise SystemExit(main())
