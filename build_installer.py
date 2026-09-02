"""Create a clean, locked environment and build native installer artifacts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parent
BUILD_ENVIRONMENT_NAME = ".installer-build-venv"
BUILD_ENVIRONMENT = PROJECT_ROOT / BUILD_ENVIRONMENT_NAME
BUILD_REQUIREMENTS = PROJECT_ROOT / "packaging" / "build-requirements.txt"
REQUIRED_PYTHON = (3, 12, 13)
REQUIRED_POINTER_BITS = 64
_ENVIRONMENT_MARKER = ".fantasy-trade-evaluator-installer-builder"
_ENVIRONMENT_MARKER_CONTENT = "fantasy-trade-evaluator installer builder v1\n"


@dataclass(frozen=True, slots=True)
class InterpreterIdentity:
    implementation: str
    version: tuple[int, int, int]
    pointer_bits: int


def _environment_python(environment: Path, *, platform_name: str | None = None) -> Path:
    if (platform_name or os.name) == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _current_identity() -> InterpreterIdentity:
    return InterpreterIdentity(
        implementation=platform.python_implementation(),
        version=sys.version_info[:3],
        pointer_bits=struct.calcsize("P") * 8,
    )


def _interpreter_identity(executable: Path, project_root: Path) -> InterpreterIdentity:
    script = (
        "import json,platform,struct,sys;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':list(sys.version_info[:3]),'pointer_bits':struct.calcsize('P')*8}))"
    )
    result = subprocess.run(
        [str(executable), "-I", "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
        if set(payload) != {"implementation", "version", "pointer_bits"}:
            raise ValueError
        version = payload["version"]
        if (
            not isinstance(payload["implementation"], str)
            or not isinstance(version, list)
            or len(version) != 3
            or any(not isinstance(part, int) or isinstance(part, bool) for part in version)
            or not isinstance(payload["pointer_bits"], int)
            or isinstance(payload["pointer_bits"], bool)
        ):
            raise ValueError
        return InterpreterIdentity(
            payload["implementation"], tuple(version), payload["pointer_bits"]
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("build environment returned an invalid Python identity") from error


def _validate_identity(identity: InterpreterIdentity, label: str) -> None:
    required = ".".join(map(str, REQUIRED_PYTHON))
    if identity.implementation != "CPython":
        raise RuntimeError(f"{label} must use CPython, not {identity.implementation}")
    if identity.version != REQUIRED_PYTHON:
        raise RuntimeError(f"{label} must use CPython {required}")
    if identity.pointer_bits != REQUIRED_POINTER_BITS:
        raise RuntimeError(f"{label} must use a 64-bit CPython runtime")


def _is_owned_environment(environment: Path) -> bool:
    marker = environment / _ENVIRONMENT_MARKER
    return (
        marker.is_file()
        and not marker.is_symlink()
        and marker.read_text(encoding="utf-8") == _ENVIRONMENT_MARKER_CONTENT
    )


def _validate_environment(environment: Path, project_root: Path) -> Path:
    if environment.is_symlink() or not environment.is_dir():
        raise RuntimeError("build environment must be a regular directory")
    if not _is_owned_environment(environment):
        raise RuntimeError("build environment is not owned by this installer builder")
    configuration = environment / "pyvenv.cfg"
    if configuration.is_symlink() or not configuration.is_file():
        raise RuntimeError("build environment is missing its regular pyvenv.cfg")
    settings = {}
    for line in configuration.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            settings[key.strip().casefold()] = value.strip().casefold()
    if settings.get("include-system-site-packages") != "false":
        raise RuntimeError("build environment must exclude system site packages")
    executable = _environment_python(environment)
    if not executable.is_file():
        raise RuntimeError("build environment is incomplete: Python was not found")
    _validate_identity(
        _interpreter_identity(executable, project_root), "build environment"
    )
    return executable


def _create_environment(project_root: Path) -> Path:
    temporary = Path(
        tempfile.mkdtemp(prefix=f"{BUILD_ENVIRONMENT_NAME}-new-", dir=project_root)
    )
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(temporary)],
            cwd=project_root,
            check=True,
            timeout=300,
        )
        (temporary / _ENVIRONMENT_MARKER).write_text(
            _ENVIRONMENT_MARKER_CONTENT, encoding="utf-8", newline="\n"
        )
        _validate_environment(temporary, project_root)
        return temporary
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def prepare_build_environment(
    project_root: Path = PROJECT_ROOT,
    environment: Path = BUILD_ENVIRONMENT,
) -> Path:
    _validate_identity(_current_identity(), "installer builder")
    if environment.is_symlink():
        raise ValueError("build environment cannot be a symbolic link")
    if environment.exists() and not environment.is_dir():
        raise ValueError("build environment path must be a directory")
    if environment.exists() and not _is_owned_environment(environment):
        raise ValueError("refusing to replace an unowned build environment")
    if environment.parent.resolve() != project_root.resolve():
        raise ValueError("build environment must be directly inside the project")

    replacement = _create_environment(project_root)
    try:
        if environment.exists():
            if environment.is_symlink() or not _is_owned_environment(environment):
                raise ValueError("build environment ownership changed during preparation")
            shutil.rmtree(environment)
        replacement.replace(environment)
    except BaseException:
        shutil.rmtree(replacement, ignore_errors=True)
        raise
    return _validate_environment(environment, project_root)


def install_build_dependencies(executable: Path, project_root: Path = PROJECT_ROOT) -> None:
    requirements = project_root / "packaging" / BUILD_REQUIREMENTS.name
    subprocess.run(
        [
            str(executable),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "--only-binary=:all:",
            "--requirement",
            str(requirements),
        ],
        cwd=project_root,
        check=True,
        timeout=1_200,
    )


def invoke_release_builder(
    executable: Path,
    *,
    output: Path,
    portable_only: bool,
    project_root: Path = PROJECT_ROOT,
) -> None:
    command = [
        str(executable),
        str(project_root / "release_build.py"),
        "--output",
        str(output),
    ]
    if portable_only:
        command.append("--portable-only")
    subprocess.run(command, cwd=project_root, check=True, timeout=1_800)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("release"),
        help="dedicated output directory inside this checkout (default: release)",
    )
    parser.add_argument(
        "--portable-only",
        action="store_true",
        help="on Windows, build the portable ZIP without requiring Inno Setup",
    )
    args = parser.parse_args(argv)

    try:
        executable = prepare_build_environment()
        print("Installing the repository's hash-locked build dependencies...", flush=True)
        install_build_dependencies(executable)
        print("Building and validating native release artifacts...", flush=True)
        invoke_release_builder(
            executable,
            output=args.output,
            portable_only=args.portable_only,
        )
    except subprocess.CalledProcessError as error:
        return error.returncode or 2
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as error:
        print(f"Installer build failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
