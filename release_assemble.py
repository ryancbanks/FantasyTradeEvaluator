"""Verify per-platform build artifacts and assemble one publishable release set."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import re
import shutil
import sys


APP_BASENAME = "FantasyTradeEvaluator"
ARTIFACT_LAYOUT = {
    "windows-x64": (
        "{app}-{version}-windows-x64-Setup.exe",
        "{app}-{version}-windows-x64-Uninstall.exe",
        "{app}-{version}-windows-x64-portable.zip",
    ),
    "macos-x64": ("{app}-{version}-macos-x64.dmg",),
    "macos-arm64": ("{app}-{version}-macos-arm64.dmg",),
    "linux-x64": ("{app}-{version}-linux-x64.tar.gz",),
    "linux-arm64": ("{app}-{version}-linux-arm64.tar.gz",),
}
ARTIFACT_PREFIX = "fantasy-trade-evaluator-"
CHECKSUM_NAME = "SHA256SUMS"
_CHECKSUM_LINE = re.compile(
    r"(?P<digest>[0-9a-f]{64})  (?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
)


def expected_artifacts(version: str) -> dict[str, tuple[str, ...]]:
    if not version or any(character not in "0123456789." for character in version):
        raise ValueError("release version must contain only digits and periods")
    return {
        ARTIFACT_PREFIX + platform_name: tuple(
            template.format(app=APP_BASENAME, version=version)
            for template in templates
        )
        for platform_name, templates in ARTIFACT_LAYOUT.items()
    }


def _digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _read_manifest(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing regular checksum manifest: {path}")
    text = path.read_text(encoding="ascii")
    if not text or not text.endswith("\n"):
        raise ValueError(f"checksum manifest must be nonempty and newline-terminated: {path}")
    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid checksum line in {path.name}: {line!r}")
        name = match.group("name")
        if name in entries:
            raise ValueError(f"duplicate checksum entry: {name}")
        entries[name] = match.group("digest")
    return entries


def _verify_directory(directory: Path, expected: tuple[str, ...]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"missing regular artifact directory: {directory.name}")
    children = tuple(directory.iterdir())
    if any(child.is_symlink() or not child.is_file() for child in children):
        raise ValueError(f"artifact directory contains a non-regular file: {directory.name}")
    expected_entries = set(expected) | {CHECKSUM_NAME}
    actual_entries = {child.name for child in children}
    if actual_entries != expected_entries:
        raise ValueError(
            f"artifact directory {directory.name} has the wrong payload set: "
            f"expected {sorted(expected_entries)}, found {sorted(actual_entries)}"
        )
    if any(not (directory / name).stat().st_size for name in expected):
        raise ValueError(f"artifact directory contains an empty payload: {directory.name}")
    checksums = _read_manifest(directory / CHECKSUM_NAME)
    if set(checksums) != set(expected):
        raise ValueError(f"checksum manifest does not match {directory.name}'s payloads")
    for name in expected:
        if _digest(directory / name) != checksums[name]:
            raise ValueError(f"checksum mismatch: {directory.name}/{name}")


def assemble_release(source: Path, destination: Path, version: str) -> tuple[Path, ...]:
    layout = expected_artifacts(version)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("artifact download root must be a regular directory")
    source_entries = tuple(source.iterdir())
    if {entry.name for entry in source_entries} != set(layout):
        raise ValueError("artifact download root does not contain the exact platform set")
    for directory_name, filenames in layout.items():
        _verify_directory(source / directory_name, filenames)

    if destination.is_symlink():
        raise ValueError("release destination cannot be a symbolic link")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("release destination must be a new or empty directory")
    else:
        destination.mkdir(parents=True)

    published: list[Path] = []
    for directory_name, filenames in layout.items():
        directory = source / directory_name
        for filename in filenames:
            target = destination / filename
            if target.exists():
                raise ValueError(f"duplicate release filename: {filename}")
            shutil.copy2(directory / filename, target)
            published.append(target)

    checksum = destination / CHECKSUM_NAME
    checksum.write_text(
        "".join(
            f"{_digest(path)}  {path.name}\n"
            for path in sorted(published, key=lambda item: item.name)
        ),
        encoding="ascii",
        newline="\n",
    )
    combined = _read_manifest(checksum)
    if set(combined) != {path.name for path in published}:
        raise RuntimeError("combined checksum manifest is incomplete")
    if any(_digest(path) != combined[path.name] for path in published):
        raise RuntimeError("combined checksum verification failed")
    return tuple(sorted(published, key=lambda item: item.name)) + (checksum,)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        files = assemble_release(args.source, args.destination, args.version)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Release assembly failed: {error}", file=sys.stderr)
        return 2
    for path in files:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
