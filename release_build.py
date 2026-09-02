"""Build and package one native Fantasy Trade Evaluator release."""

import argparse
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import distribution
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parent
APP_BASENAME = "FantasyTradeEvaluator"
APP_DISPLAY_NAME = "Fantasy Trade Evaluator"
_OUTPUT_MARKER = ".fantasy-trade-evaluator-release"
_OUTPUT_MARKER_CONTENT = "fantasy-trade-evaluator release output v1\n"
SOURCE_NOTICE_FILENAMES = (
    "README.txt",
    "CPython-LICENSE.txt",
    "CPython-THIRD-PARTY-LICENSES.rst",
    "XlsxWriter-LICENSE.txt",
    "PyInstaller-LICENSE.txt",
    "SQLite-LICENSE.md",
    "XZ-Utils-COPYING.txt",
)
NOTICE_FILENAMES = SOURCE_NOTICE_FILENAMES + ("NATIVE-DEPENDENCY-INVENTORY.txt",)
_REMOTE_NOTICES = {
    "CPython-THIRD-PARTY-LICENSES.rst": (
        "https://raw.githubusercontent.com/python/cpython/v3.12.13/Doc/license.rst",
        "341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5",
    ),
    "SQLite-LICENSE.md": (
        "https://raw.githubusercontent.com/sqlite/sqlite/version-3.53.1/LICENSE.md",
        "ee6af51062b30d532991face5164136ae6f84e265ecf8abe89dc69dac45ca1e7",
    ),
    "XZ-Utils-COPYING.txt": (
        "https://raw.githubusercontent.com/tukaani-project/xz/v5.8.1/COPYING",
        "616a3ad264ce29b8f1cb97e53037b139d406899ca8d1f799651e17bfa09830b8",
    ),
}


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    system: str
    architecture: str

    @property
    def tag(self) -> str:
        return f"{self.system}-{self.architecture}"


def current_target(
    platform_name: str | None = None, machine: str | None = None
) -> ReleaseTarget:
    system = (platform_name or sys.platform).lower()
    system = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(system, system)
    processor = (machine or platform.machine()).lower()
    architecture = {
        "amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64",
    }.get(processor)
    if system not in {"windows", "macos", "linux"} or architecture is None:
        raise ValueError(f"unsupported release platform: {system}/{processor}")
    if system == "windows" and architecture != "x64":
        raise ValueError("Windows releases currently require an x64 build host")
    return ReleaseTarget(system, architecture)


def project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as source:
        value = tomllib.load(source)["project"]["version"]
    if not isinstance(value, str) or not value or any(ch not in "0123456789." for ch in value):
        raise ValueError("project version must contain only digits and periods")
    return value


def validate_release_tag(tag: str) -> None:
    expected = f"v{project_version()}"
    if tag != expected:
        raise ValueError(f"release tag must be exactly {expected}, not {tag}")


def build_release(output: Path, *, portable_only: bool = False) -> tuple[Path, ...]:
    output = _safe_output(output)
    target = current_target()
    version = project_version()
    work, dist = output / "build", output / "dist"
    for directory in (work, dist):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

    notices = output / "legal-notices"
    _prepare_third_party_notices(notices)

    environment = os.environ.copy()
    environment["FTE_NOTICES_DIR"] = str(notices)
    _run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--workpath", str(work), "--distpath", str(dist),
        str(PROJECT_ROOT / "packaging" / "fantasy_trade_evaluator.spec"),
    ], env=environment)

    application = _application_path(dist, target)
    _write_native_inventory(application)
    _assert_application_notices(application)
    _run(
        [str(_application_executable(application, target)), "--self-check"],
        env=environment,
        timeout=120,
    )
    artifacts: list[Path]
    if target.system == "windows":
        artifacts = [_windows_zip(application, output, version, target)]
        if portable_only:
            _remove_windows_packages(output, version)
        else:
            artifacts.extend(_windows_packages(application, output, version))
    elif target.system == "macos":
        artifacts = [_macos_dmg(application, output, version, target)]
    else:
        artifacts = [_linux_archive(application, output, version, target)]
    checksum = _write_checksums(output, artifacts)
    return tuple(artifacts + [checksum])


def _prepare_third_party_notices(destination: Path) -> None:
    """Collect license texts from the exact runtime used for this native build."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    python_license = _first_file(
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    )
    shutil.copyfile(python_license, destination / "CPython-LICENSE.txt")
    if sys.version_info[:3] != (3, 12, 13):
        raise RuntimeError("native releases must use the reviewed CPython 3.12.13 runtime")
    for name, (url, digest) in _REMOTE_NOTICES.items():
        _download_pinned_notice(url, digest, destination / name)
    shutil.copyfile(
        _distribution_file("XlsxWriter", "LICENSE.txt"),
        destination / "XlsxWriter-LICENSE.txt",
    )
    shutil.copyfile(
        _distribution_file("PyInstaller", "licenses/COPYING.txt"),
        destination / "PyInstaller-LICENSE.txt",
    )
    (destination / "README.txt").write_text(
        "Fantasy Trade Evaluator — third-party notices\n\n"
        "These files were collected from the exact dependencies included in this build.\n",
        encoding="utf-8",
        newline="\n",
    )
    _validate_notice_directory(destination, SOURCE_NOTICE_FILENAMES)


def _first_file(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"required license file was not found: {candidates[0].name}")


def _download_pinned_notice(url: str, expected_digest: str, destination: Path) -> None:
    with urlopen(url, timeout=30) as response:
        content = response.read()
    if sha256(content).hexdigest() != expected_digest:
        raise RuntimeError(f"downloaded license failed its pinned SHA-256 check: {url}")
    destination.write_bytes(content)


def _distribution_file(project: str, suffix: str) -> Path:
    normalized = suffix.replace("\\", "/").lower()
    package = distribution(project)
    for relative in package.files or ():
        if str(relative).replace("\\", "/").lower().endswith(normalized):
            path = Path(package.locate_file(relative))
            if path.is_file():
                return path
    raise RuntimeError(f"{project} distribution is missing {suffix}")


def _validate_notice_directory(
    directory: Path, filenames: tuple[str, ...] = NOTICE_FILENAMES
) -> None:
    missing = [name for name in filenames if not (directory / name).is_file()]
    empty = [name for name in filenames if (directory / name).is_file() and not (directory / name).stat().st_size]
    if missing or empty:
        raise RuntimeError(f"third-party notice bundle is incomplete: {missing + empty}")


def validated_notice_source(setting: str | None) -> Path:
    """Fail closed unless a spec received this builder's exact allowlisted notice tree."""

    if not setting:
        raise ValueError("FTE_NOTICES_DIR is required")
    supplied = Path(setting)
    if supplied.is_symlink():
        raise ValueError("FTE_NOTICES_DIR cannot be a symbolic link")
    directory = supplied.resolve()
    marker = directory.parent / _OUTPUT_MARKER
    if (
        directory.name != "legal-notices"
        or not marker.is_file()
        or marker.is_symlink()
        or marker.read_text(encoding="utf-8") != _OUTPUT_MARKER_CONTENT
    ):
        raise ValueError("FTE_NOTICES_DIR is not owned by this release builder")
    entries = tuple(directory.iterdir()) if directory.is_dir() else ()
    if (
        {entry.name for entry in entries} != set(SOURCE_NOTICE_FILENAMES)
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise ValueError("FTE_NOTICES_DIR does not match the notice allowlist")
    _validate_notice_directory(directory, SOURCE_NOTICE_FILENAMES)
    return directory


def _write_native_inventory(application: Path) -> Path:
    matches = tuple(application.rglob("THIRD_PARTY_NOTICES"))
    if len(matches) != 1 or not matches[0].is_dir():
        raise RuntimeError("frozen application is missing THIRD_PARTY_NOTICES")
    inventory: list[str] = []
    for item in sorted(application.rglob("*")):
        if not item.is_file() or not _is_native_runtime_file(item):
            continue
        relative = item.relative_to(application).as_posix()
        notice = _native_notice_for(relative)
        if notice is None:
            raise RuntimeError(f"native dependency has no reviewed license mapping: {relative}")
        inventory.append(f"{relative}\t{item.stat().st_size}\t{notice}\n")
    if not inventory:
        raise RuntimeError("native dependency inventory is empty")
    path = matches[0] / "NATIVE-DEPENDENCY-INVENTORY.txt"
    path.write_text(
        "Relative path\tBytes\tLicense/notice coverage\n" + "".join(inventory),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _is_native_runtime_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        path.name.startswith(APP_BASENAME)
        or any(token in name for token in (".so", ".dylib"))
        or path.suffix.lower() in {".dll", ".pyd", ".exe"}
    )


def _native_notice_for(relative: str) -> str | None:
    path = "/" + relative.lower()
    name = Path(relative).name.lower()
    if name.startswith(APP_BASENAME.lower()):
        return "PyInstaller-LICENSE.txt; CPython-LICENSE.txt"
    if "lzma" in name:
        return "XZ-Utils-COPYING.txt"
    if "sqlite" in name:
        return "SQLite-LICENSE.md"
    if any(part in name for part in ("crypto", "ssl", "ffi", "expat", "decimal", "bz2", "zlib")):
        return "CPython-THIRD-PARTY-LICENSES.rst"
    if name.startswith(("python", "libpython", "api-ms-", "vcruntime", "ucrtbase", "msvcp")):
        return "CPython-LICENSE.txt"
    if path.endswith(".pyd") or "/lib-dynload/" in path or ".cpython-" in name:
        return "CPython-LICENSE.txt; CPython-THIRD-PARTY-LICENSES.rst"
    return None


def _assert_application_notices(application: Path) -> Path:
    matches = tuple(application.rglob("THIRD_PARTY_NOTICES"))
    if len(matches) != 1 or not matches[0].is_dir():
        raise RuntimeError("frozen application is missing THIRD_PARTY_NOTICES")
    _validate_notice_directory(matches[0])
    return matches[0]


def _application_path(dist: Path, target: ReleaseTarget) -> Path:
    name = f"{APP_DISPLAY_NAME}.app" if target.system == "macos" else APP_BASENAME
    path = dist / name
    if not path.exists():
        raise RuntimeError(f"PyInstaller did not create {path.name}")
    return path


def _application_executable(application: Path, target: ReleaseTarget) -> Path:
    if target.system == "macos":
        return application / "Contents" / "MacOS" / APP_BASENAME
    suffix = ".exe" if target.system == "windows" else ""
    return application / f"{APP_BASENAME}{suffix}"


def _windows_zip(
    application: Path, output: Path, version: str, target: ReleaseTarget
) -> Path:
    stem = output / f"{APP_BASENAME}-{version}-{target.tag}-portable"
    archive = shutil.make_archive(
        str(stem), "zip", root_dir=application.parent, base_dir=application.name
    )
    return Path(archive)


def _windows_packages(
    application: Path, output: Path, version: str
) -> tuple[Path, Path]:
    compiler = _find_iscc()
    common = [str(compiler), f"/DAppVersion={version}", f"/DOutputDir={output}"]
    packages = (
        (
            "installer.iss",
            output / f"{APP_BASENAME}-{version}-windows-x64-Setup.exe",
            [f"/DSourceDir={application}"],
        ),
        (
            "uninstaller.iss",
            output / f"{APP_BASENAME}-{version}-windows-x64-Uninstall.exe",
            [],
        ),
    )
    for script, expected, definitions in packages:
        source = PROJECT_ROOT / "packaging" / "windows" / script
        _run(common + definitions + [str(source)])
        if not expected.is_file():
            raise RuntimeError(f"Inno Setup did not create the expected {expected.name}")
    return packages[0][1], packages[1][1]


def _remove_windows_packages(output: Path, version: str) -> None:
    for suffix in ("Setup", "Uninstall"):
        path = output / f"{APP_BASENAME}-{version}-windows-x64-{suffix}.exe"
        if path.exists() or path.is_symlink():
            path.unlink()


def _find_iscc() -> Path:
    candidates = [os.environ.get("ISCC_PATH"), shutil.which("ISCC")]
    local_programs = os.environ.get("LOCALAPPDATA")
    if local_programs:
        candidates.extend(
            str(Path(local_programs) / "Programs" / name / "ISCC.exe")
            for name in ("Inno Setup 7", "Inno Setup 6")
        )
    for program_files in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if program_files:
            candidates.extend(
                str(Path(program_files) / name / "ISCC.exe")
                for name in ("Inno Setup 7", "Inno Setup 6")
            )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError(
        "Inno Setup compiler was not found; install JRSoftware.InnoSetup or use "
        "--portable-only to create the signed-ready ZIP without the Setup or "
        "standalone Uninstall executables"
    )


def _macos_dmg(
    application: Path, output: Path, version: str, target: ReleaseTarget
) -> Path:
    identity = os.environ.get("MACOS_SIGN_IDENTITY")
    profile = os.environ.get("MACOS_NOTARY_PROFILE")
    if profile and not identity:
        raise ValueError("MACOS_NOTARY_PROFILE requires MACOS_SIGN_IDENTITY")
    staging = output / "dmg-root"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    _run(["ditto", str(application), str(staging / application.name)])
    (staging / "Applications").symlink_to("/Applications")
    dmg = output / f"{APP_BASENAME}-{version}-{target.tag}.dmg"
    if dmg.exists() or dmg.is_symlink():
        dmg.unlink()
    _run(["hdiutil", "create", "-fs", "HFS+", "-format", "UDZO", "-volname",
          APP_DISPLAY_NAME, "-srcfolder", str(staging), str(dmg)])
    if identity:
        _run(["codesign", "--force", "--timestamp", "--sign", identity, str(dmg)])
    if profile:
        _run(["xcrun", "notarytool", "submit", str(dmg), "--keychain-profile", profile,
              "--wait"])
        _run(["xcrun", "stapler", "staple", str(dmg)])
    return dmg


def _linux_archive(
    application: Path, output: Path, version: str, target: ReleaseTarget
) -> Path:
    stem = f"{APP_BASENAME}-{version}-{target.tag}"
    staging = output / stem
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(application, staging / "app", symlinks=True)
    linux_assets = PROJECT_ROOT / "packaging" / "linux"
    for name in ("install.sh", "uninstall.sh", "fantasy-trade-evaluator.svg"):
        shutil.copy2(linux_assets / name, staging / name)
    (staging / "release-target.txt").write_text(
        "fantasy-trade-evaluator release target v1\n"
        f"system={target.system}\n"
        f"architecture={target.architecture}\n"
        f"version={version}\n",
        encoding="utf-8",
        newline="\n",
    )
    for name in ("install.sh", "uninstall.sh"):
        (staging / name).chmod(0o755)
    archive = output / f"{stem}.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as target_file:
        target_file.add(staging, arcname=stem, recursive=True, filter=_linux_tar_mode)
    return archive


def _linux_tar_mode(member: tarfile.TarInfo) -> tarfile.TarInfo:
    executable_names = ("/install.sh", "/uninstall.sh", f"/app/{APP_BASENAME}")
    if member.name.endswith(executable_names):
        member.mode = (member.mode & ~0o777) | 0o755
    return member


def _write_checksums(output: Path, artifacts: list[Path]) -> Path:
    path = output / "SHA256SUMS"
    lines = [f"{_file_digest(item)}  {item.name}\n" for item in artifacts]
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return path


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_output(value: Path) -> Path:
    if value.is_symlink():
        raise ValueError("release output cannot be a symbolic link")
    output = value.resolve()
    if output == PROJECT_ROOT or not output.is_relative_to(PROJECT_ROOT):
        raise ValueError("release output must be a dedicated directory inside the project")
    marker = output / _OUTPUT_MARKER
    if output.exists():
        if not output.is_dir():
            raise ValueError("release output must be a directory")
        entries = tuple(output.iterdir())
        if entries and not marker.exists():
            raise ValueError("nonempty release output is not owned by this builder")
        if marker.exists() and (
            marker.is_symlink()
            or not marker.is_file()
            or marker.read_text(encoding="utf-8") != _OUTPUT_MARKER_CONTENT
        ):
            raise ValueError("release output ownership marker is invalid")
    else:
        output.mkdir(parents=True)
    marker.write_text(_OUTPUT_MARKER_CONTENT, encoding="utf-8", newline="\n")
    return output


def _run(
    command: list[str], *, env: dict[str, str] | None = None, timeout: int = 1_200
) -> None:
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "release")
    parser.add_argument("--portable-only", action="store_true")
    parser.add_argument("--check-tag", metavar="TAG")
    args = parser.parse_args(argv)
    try:
        if args.check_tag is not None:
            validate_release_tag(args.check_tag)
            print(f"Release tag {args.check_tag} matches the project version.")
            return 0
        artifacts = build_release(args.output, portable_only=args.portable_only)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        return 2
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
