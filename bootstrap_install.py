"""Install a source checkout into an isolated, user-scoped runtime.

Native releases remain the preferred installation path.  This bootstrap is a
portable fallback for supported desktops that already have Python 3.11 or
newer.  It never installs into the invoking interpreter or writes outside the
selected directory.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parent
INSTALL_MARKER = ".fantasy-trade-evaluator-source-install"
RUNTIME_MARKER = ".fantasy-trade-evaluator-runtime"
MARKER_CONTENT = "fantasy-trade-evaluator source install v1\n"
RUNTIME_MARKER_CONTENT = "fantasy-trade-evaluator isolated runtime v1\n"
SUPPORTED_SYSTEMS = frozenset({"win32", "darwin", "linux"})
SUPPORTED_ARCHITECTURES = frozenset({"x64", "arm64"})


def supported_host(
    platform_name: str | None = None,
    machine: str | None = None,
) -> tuple[str, str]:
    system = (platform_name or sys.platform).lower()
    if system.startswith("linux"):
        system = "linux"
    architecture = {
        "amd64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get((machine or platform.machine()).lower())
    if system not in SUPPORTED_SYSTEMS or architecture not in SUPPORTED_ARCHITECTURES:
        raise ValueError(
            f"unsupported source-install host: {system}/{machine or platform.machine()}"
        )
    if system == "win32" and architecture != "x64":
        raise ValueError("Windows source installation is currently validated only on x64")
    return system, architecture


def default_install_root(
    *,
    platform_name: str | None = None,
    machine: str | None = None,
    environment: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return a user-owned runtime location without consulting shell syntax."""

    system, _ = supported_host(platform_name, machine)
    values = os.environ if environment is None else environment
    home_root = Path.home() if home is None else Path(home)
    if not home_root.is_absolute():
        raise ValueError("the user home directory must be absolute")
    if system == "win32":
        base = Path(values.get("LOCALAPPDATA", home_root / "AppData" / "Local"))
        base = base / "Programs"
    elif system == "darwin":
        base = home_root / "Library" / "Application Support"
    else:
        base = Path(values.get("XDG_DATA_HOME", home_root / ".local" / "share"))
    if not base.is_absolute():
        raise ValueError("the user application-data directory must be absolute")
    return base / "FantasyTradeEvaluatorSource"


def install(
    source: Path = PROJECT_ROOT,
    target: Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Build a fresh runtime, publish its launcher, then retire older runtimes."""

    _require_python()
    supported_host()
    source = _validated_source(source)
    home_root = _resolved_home(home)
    target = _validated_target(
        target or default_install_root(home=home_root),
        home_root,
    )
    created_root = not target.exists()
    versions = target / "versions"
    runtime = versions / f"runtime-{uuid4().hex}"
    launcher: Path | None = None
    try:
        _prepare_install_root(target)
        _require_plain_directory(versions, create=True)
        _build_runtime(runtime, source)
        (runtime / RUNTIME_MARKER).write_text(
            RUNTIME_MARKER_CONTENT,
            encoding="utf-8",
            newline="\n",
        )
        launcher = _publish_launcher(target, runtime)
    except BaseException:
        _remove_runtime(runtime, versions, allow_incomplete=True)
        if created_root:
            _remove_empty_install_root(target)
        raise
    _remove_old_runtimes(versions, keep=runtime)
    return launcher


def uninstall(target: Path | None = None, *, home: Path | None = None) -> bool:
    """Remove only a bootstrap-owned runtime; weekly application data is separate."""

    home_root = _resolved_home(home)
    target = _validated_target(
        target or default_install_root(home=home_root),
        home_root,
    )
    if not target.exists() and not target.is_symlink():
        return False
    _require_owned_install_root(target)
    shutil.rmtree(target)
    return True


def _build_runtime(runtime: Path, source: Path) -> None:
    if runtime.exists() or runtime.is_symlink():
        raise ValueError("new runtime path unexpectedly exists")
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(runtime)
    python = _runtime_python(runtime)
    environment = os.environ.copy()
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            str(source),
        ],
        env=environment,
        timeout=1_200,
    )
    _run(
        [
            str(python),
            "-m",
            "trade_snapshot.app_launcher",
            "--self-check",
        ],
        env=environment,
        timeout=180,
    )


def _publish_launcher(target: Path, runtime: Path) -> Path:
    executable = _runtime_scripts(runtime) / (
        "fantasy-trade-evaluator.exe" if os.name == "nt" else "fantasy-trade-evaluator"
    )
    if executable.is_symlink() or not executable.is_file():
        raise RuntimeError("installed runtime did not provide the application launcher")
    relative_executable = executable.relative_to(target)
    if os.name == "nt":
        launcher = target / "Fantasy Trade Evaluator.cmd"
        relative = str(relative_executable).replace("/", "\\")
        body = f'@echo off\r\n"%~dp0{relative}" %*\r\n'
    else:
        launcher = target / "fantasy-trade-evaluator"
        relative = relative_executable.as_posix()
        body = (
            "#!/bin/sh\n"
            'script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            f'exec "$script_dir/{relative}" "$@"\n'
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".launcher-",
        suffix=".tmp",
        dir=target,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(body)
        if os.name != "nt":
            temporary.chmod(0o755)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)
    return launcher


def _prepare_install_root(target: Path) -> None:
    if target.exists() or target.is_symlink():
        _require_owned_install_root(target)
        return
    target.mkdir(parents=True)
    (target / INSTALL_MARKER).write_text(
        MARKER_CONTENT,
        encoding="utf-8",
        newline="\n",
    )


def _require_owned_install_root(target: Path) -> None:
    if target.is_symlink() or not target.is_dir():
        raise ValueError("install target must be a plain directory")
    marker = target / INSTALL_MARKER
    if (
        marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != MARKER_CONTENT
    ):
        raise ValueError("refusing to modify an install target not owned by this bootstrap")


def _require_plain_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symbolic-link directory: {path}")
    if create:
        path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"required directory is unavailable: {path}")


def _remove_old_runtimes(versions: Path, *, keep: Path) -> None:
    for candidate in tuple(versions.iterdir()):
        if candidate == keep:
            continue
        try:
            marker = candidate / RUNTIME_MARKER
            owned = (
                not candidate.is_symlink()
                and candidate.is_dir()
                and candidate.name.startswith("runtime-")
                and not marker.is_symlink()
                and marker.is_file()
                and marker.read_text(encoding="utf-8") == RUNTIME_MARKER_CONTENT
            )
        except (OSError, UnicodeError):
            owned = False
        if not owned:
            continue
        try:
            _remove_runtime(candidate, versions)
        except (OSError, ValueError):
            # A still-running Windows process can hold its prior runtime open.
            # The published launcher already points only at the verified new one.
            continue


def _remove_runtime(
    runtime: Path,
    versions: Path,
    *,
    allow_incomplete: bool = False,
) -> None:
    if not runtime.exists() and not runtime.is_symlink():
        return
    if runtime.is_symlink() or runtime.parent.resolve() != versions.resolve():
        raise ValueError("refusing to remove an unsafe runtime path")
    marker = runtime / RUNTIME_MARKER
    if not marker.is_file() or marker.is_symlink():
        if allow_incomplete and runtime.name.startswith("runtime-") and runtime.is_dir():
            shutil.rmtree(runtime)
            return
        raise ValueError("refusing to remove an unowned runtime")
    if marker.read_text(encoding="utf-8") != RUNTIME_MARKER_CONTENT:
        raise ValueError("refusing to remove a runtime with an invalid marker")
    shutil.rmtree(runtime)


def _remove_empty_install_root(target: Path) -> None:
    versions = target / "versions"
    if versions.is_dir() and not any(versions.iterdir()):
        versions.rmdir()
    marker = target / INSTALL_MARKER
    if marker.is_file() and marker.read_text(encoding="utf-8") == MARKER_CONTENT:
        marker.unlink()
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()


def _validated_source(value: Path) -> Path:
    source = Path(value).expanduser().resolve()
    if source.is_dir():
        if not (source / "pyproject.toml").is_file() or not (
            source / "trade_snapshot"
        ).is_dir():
            raise ValueError("source directory is not a Fantasy Trade Evaluator checkout")
        return source
    if source.is_file() and source.suffix.casefold() == ".whl":
        return source
    raise ValueError("source must be a project checkout or local wheel")


def _validated_target(value: Path, home: Path) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ValueError("install target cannot be a symbolic link")
    target = raw.resolve()
    if target == home or not target.is_relative_to(home):
        raise ValueError("install target must be a dedicated directory inside the user home")
    return target


def _resolved_home(value: Path | None) -> Path:
    home = Path.home() if value is None else Path(value)
    if not home.is_absolute():
        raise ValueError("the user home directory must be absolute")
    home = home.resolve()
    if not home.is_dir():
        raise ValueError("the user home directory does not exist")
    return home


def _runtime_scripts(runtime: Path) -> Path:
    return runtime / ("Scripts" if os.name == "nt" else "bin")


def _runtime_python(runtime: Path) -> Path:
    return _runtime_scripts(runtime) / ("python.exe" if os.name == "nt" else "python")


def _require_python() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("source installation requires Python 3.11 or newer")


def _run(command: list[str], *, env: dict[str, str], timeout: int) -> None:
    subprocess.run(command, check=True, env=env, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.uninstall:
            removed = uninstall(args.target)
            print(
                "Source runtime removed; weekly application data was kept."
                if removed
                else "No source runtime was installed."
            )
        else:
            launcher = install(args.source, args.target)
            print(f"Fantasy Trade Evaluator is ready. Open: {launcher}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
