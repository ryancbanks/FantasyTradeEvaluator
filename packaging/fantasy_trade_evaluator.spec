# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import sys
import tomllib

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPEC).resolve().parent.parent
sys.path.insert(0, str(project_root))
from release_build import validated_notice_source

signing_identity = os.environ.get("MACOS_SIGN_IDENTITY") if sys.platform == "darwin" else None
with (project_root / "pyproject.toml").open("rb") as source:
    application_version = tomllib.load(source)["project"]["version"]
try:
    notices_root = validated_notice_source(os.environ.get("FTE_NOTICES_DIR"))
except (OSError, RuntimeError, ValueError) as error:
    raise SystemExit(str(error)) from None
web_datas = collect_data_files("trade_snapshot.web_assets")
extension_datas = collect_data_files("trade_snapshot.browser_extension")
datas = web_datas + extension_datas + [(str(notices_root), "THIRD_PARTY_NOTICES")]

analysis = Analysis(
    [str(project_root / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "greenlet",
        "playwright",
        "pyee",
        "trade_snapshot._playwright_backend",
        "trade_snapshot._playwright_capture",
        "trade_snapshot._playwright_worker",
        "typing_extensions",
        "tkinter",
    ],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="FantasyTradeEvaluator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=signing_identity,
    entitlements_file=None,
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FantasyTradeEvaluator",
)
if sys.platform == "darwin":
    application = BUNDLE(
        bundle,
        name="Fantasy Trade Evaluator.app",
        icon=None,
        bundle_identifier="com.fantasytradeevaluator.desktop",
        codesign_identity=signing_identity,
        info_plist={
            "CFBundleDisplayName": "Fantasy Trade Evaluator",
            "CFBundleShortVersionString": application_version,
            "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True,
        },
    )
