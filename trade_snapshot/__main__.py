import argparse
import os
from pathlib import Path
import sys

from .model import SnapshotRequest
from .snapshot import SnapshotInputError, create_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create one versioned weekly NFL data snapshot.")
    parser.add_argument("--season", required=True, type=int)
    parser.add_argument("--week", required=True, type=int)
    parser.add_argument("--scoring", required=True, type=str.upper, choices=("STD", "HALF", "PPR"))
    parser.add_argument("--output", type=Path, default=Path("snapshots"))
    parser.add_argument("--fantasypros-file", type=Path)
    parser.add_argument("--espn-file", type=Path)
    parser.add_argument("--yahoo-file", type=Path)
    parser.add_argument("--league-state-file", type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    imported_files = {
        provider: path
        for provider, path in {
            "fantasypros": args.fantasypros_file,
            "espn": args.espn_file,
            "yahoo": args.yahoo_file,
            "league_state": args.league_state_file,
        }.items()
        if path is not None
    }
    try:
        request = SnapshotRequest(args.season, args.week, args.scoring)
        result = create_snapshot(
            request,
            args.output,
            imported_files=imported_files,
            fantasypros_api_key=os.environ.get("FANTASYPROS_API_KEY") or None,
            timeout=args.timeout,
        )
    except (SnapshotInputError, ValueError) as error:
        print(f"Snapshot not created: {error}", file=sys.stderr)
        return 2

    print(f"Snapshot written: {result.path}")
    if result.failed_sources:
        print(
            f"Snapshot incomplete; unavailable or failed sources: {', '.join(result.failed_sources)}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
