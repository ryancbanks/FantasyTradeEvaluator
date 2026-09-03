"""Build and exercise the installed wheel outside the source checkout."""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import venv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path, timeout: int = 300) -> None:
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fte-wheel-smoke-") as temporary:
        root = Path(temporary)
        wheelhouse = root / "wheelhouse"
        wheelhouse.mkdir()
        _run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), "."],
            cwd=PROJECT_ROOT,
        )
        wheels = tuple(wheelhouse.glob("fantasy_trade_evaluator-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("wheel build did not produce exactly one application wheel")
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        _run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheels[0])],
            cwd=root,
        )
        suffix = ".exe" if os.name == "nt" else ""
        for launcher in ("fantasy-trade-evaluator", "fantasy-trade-snapshot"):
            _run([str(scripts / f"{launcher}{suffix}"), "--help"], cwd=root, timeout=60)
        _run(
            [str(scripts / f"fantasy-trade-evaluator{suffix}"), "--self-check"],
            cwd=root,
            timeout=60,
        )
        asset_check = (
            "import importlib.util,json; from importlib.resources import files; "
            "from importlib.metadata import distribution; "
            "from pathlib import PurePosixPath; "
            "from trade_snapshot.app_launcher import "
            "_required_extension_assets,_required_web_assets; "
            "web=files('trade_snapshot.web_assets'); "
            "assert all(web.joinpath(*PurePosixPath(name).parts).read_bytes() "
            "for name in _required_web_assets()); "
            "ext=files('trade_snapshot.browser_extension'); "
            "manifest=json.loads(ext.joinpath('manifest.json').read_text()); "
            "assert manifest['manifest_version']==3; "
            "assert all(ext.joinpath(*PurePosixPath(name).parts).read_bytes() "
            "for name in _required_extension_assets(manifest)); "
            "assert any(PurePosixPath(str(path)).name=='THIRD_PARTY_NOTICES.md' "
            "for path in distribution('fantasy-trade-evaluator').files); "
            "assert all(importlib.util.find_spec(name) is None for name in "
            "('playwright','greenlet','pyee','typing_extensions'))"
        )
        _run([str(python), "-I", "-c", asset_check], cwd=root, timeout=60)
    print("Installed wheel launchers, extension assets, and dependency boundary passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
