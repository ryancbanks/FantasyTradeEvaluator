"""Fail closed unless a draft release has exactly the locally verified assets."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any


def _digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _local_assets(directory: Path) -> dict[str, tuple[int, str]]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("local release directory must be a regular directory")
    children = tuple(directory.iterdir())
    if not children:
        raise ValueError("local release directory cannot be empty")
    if any(child.is_symlink() or not child.is_file() for child in children):
        raise ValueError("local release directory contains a non-regular file")
    return {
        child.name: (child.stat().st_size, f"sha256:{_digest(child)}")
        for child in children
    }


def verify_remote_inventory(directory: Path, response: Any) -> None:
    expected = _local_assets(directory)
    if not isinstance(response, dict) or not isinstance(response.get("assets"), list):
        raise ValueError("remote release response does not contain an asset list")

    actual: dict[str, tuple[int, str]] = {}
    for asset in response["assets"]:
        if not isinstance(asset, dict):
            raise ValueError("remote release contains invalid asset metadata")
        name = asset.get("name")
        size = asset.get("size")
        digest = asset.get("digest")
        state = asset.get("state")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(size, int)
            or not isinstance(digest, str)
            or state != "uploaded"
        ):
            raise ValueError("remote release contains invalid asset metadata")
        if name in actual:
            raise ValueError(f"remote release contains a duplicate asset: {name}")
        actual[name] = (size, digest)

    if actual.keys() != expected.keys():
        raise ValueError(
            "remote release has the wrong asset set: "
            f"expected {sorted(expected)}, found {sorted(actual)}"
        )
    mismatches = [name for name, metadata in expected.items() if actual[name] != metadata]
    if mismatches:
        raise ValueError(
            f"remote release asset size or digest mismatch: {sorted(mismatches)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_directory", type=Path)
    parser.add_argument("remote_json", type=Path)
    args = parser.parse_args(argv)
    try:
        response = json.loads(args.remote_json.read_text(encoding="utf-8"))
        verify_remote_inventory(args.local_directory, response)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"Release inventory verification failed: {error}", file=sys.stderr)
        return 2
    print("Remote release asset names, sizes, states, and digests match the local set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
