"""Absolute-import shim because PyInstaller executes its input as a script."""

from trade_snapshot.app_launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
