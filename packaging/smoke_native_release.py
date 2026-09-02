"""Install or mount this host's finished artifact and run its frozen self-check."""

from pathlib import Path
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile


APP = "FantasyTradeEvaluator"
WINDOWS_UNINSTALL_KEY = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
    "{FD659318-22E8-45E3-A51B-1BF298CBFC90}_is1"
)


def _run(command: list[str], *, env=None, timeout: int = 180) -> None:
    subprocess.run(command, check=True, env=env, timeout=timeout)


def _one(root: Path, pattern: str) -> Path:
    matches = tuple(root.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"expected one {pattern} artifact, found {len(matches)}")
    return matches[0]


def _wait_until_removed(path: Path, *, timeout: float = 60.0) -> None:
    """Wait for self-deleting native uninstallers to finish their cleanup."""

    deadline = time.monotonic() + timeout
    while _path_lexists(path) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _path_lexists(path):
        raise RuntimeError(f"native smoke uninstaller left this path behind: {path}")


def _path_lexists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _windows_uninstall_registration() -> tuple[bool, Path | None]:
    if os.name != "nt":
        return False, None
    import winreg

    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                WINDOWS_UNINSTALL_KEY,
                0,
                winreg.KEY_READ | view,
            ) as key:
                try:
                    value, value_type = winreg.QueryValueEx(key, "InstallLocation")
                except OSError:
                    return True, None
                if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    return True, None
                location = os.path.expandvars(value).strip()
                return True, Path(location) if location else None
        except FileNotFoundError:
            pass
        except OSError:
            return True, None
    return False, None


def _windows_install_registered() -> bool:
    return _windows_uninstall_registration()[0]


def _registered_windows_install_matches(expected: Path) -> bool:
    registered, location = _windows_uninstall_registration()
    if not registered or location is None:
        return False
    try:
        return location.samefile(expected)
    except OSError:
        return False


def _cleanup_windows_smoke_install(installed: Path) -> None:
    if not _registered_windows_install_matches(installed):
        return
    try:
        uninstaller = _one(installed, "unins*.exe")
        subprocess.run(
            [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            check=False,
            timeout=300,
        )
        _wait_until_removed(installed)
    except Exception as error:
        print(f"WARNING: could not clean up the temporary Windows install: {error}",
              file=sys.stderr)


def _windows(root: Path, temporary: Path) -> None:
    if _windows_install_registered():
        raise RuntimeError(
            "native release smoke testing requires a clean Windows user; "
            "refusing to replace an existing uninstall registration"
        )
    installed = temporary / "installed"
    try:
        portable = _one(root, f"{APP}-*-windows-x64-portable.zip")
        extracted = temporary / "portable"
        with zipfile.ZipFile(portable) as archive:
            archive.extractall(extracted)
        _run([str(_one(extracted, f"{APP}/{APP}.exe")), "--self-check"])

        installer = _one(root, f"{APP}-*-windows-x64-Setup.exe")
        _run([
            str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CURRENTUSER", f"/DIR={installed}",
        ], timeout=600)
        _run([str(installed / f"{APP}.exe"), "--self-check"])
        _one(installed, "unins*.exe")
        uninstaller = _one(root, f"{APP}-*-windows-x64-Uninstall.exe")
        _run([
            str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        ], timeout=300)
        _wait_until_removed(installed)
        if _windows_install_registered():
            raise RuntimeError("standalone uninstaller left its Windows registration behind")
    finally:
        _cleanup_windows_smoke_install(installed)


def _macos(root: Path, temporary: Path) -> None:
    dmg = _one(root, f"{APP}-*-macos-*.dmg")
    mount = temporary / "mount"
    mount.mkdir()
    _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(dmg)])
    try:
        application = mount / "Fantasy Trade Evaluator.app"
        executable = application / "Contents" / "MacOS" / APP
        _run([str(executable), "--self-check"])
        _run(["codesign", "--verify", "--deep", "--strict", str(application)])
        if os.environ.get("MACOS_SIGN_IDENTITY"):
            _run(["spctl", "--assess", "--type", "execute", "--verbose=2", str(application)])
            _run(["xcrun", "stapler", "validate", str(dmg)])
    finally:
        _run(["hdiutil", "detach", str(mount)])


def _linux(root: Path, temporary: Path) -> None:
    archive_path = _one(root, f"{APP}-*-linux-*.tar.gz")
    extracted = temporary / "archive"
    extracted.mkdir()
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    package = next(path for path in extracted.iterdir() if path.is_dir())
    home = temporary / "home"
    home.mkdir()
    environment = os.environ | {
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "Data Files"),
        "XDG_BIN_HOME": str(home / "Local Bin"),
    }
    _run(["sh", str(package / "install.sh")], env=environment)
    launcher = Path(environment["XDG_BIN_HOME"]) / "fantasy-trade-evaluator"
    _run([str(launcher), "--self-check"], env=environment)
    _run(["sh", str(package / "uninstall.sh")], env=environment)
    _wait_until_removed(launcher)


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    root = Path(arguments[0] if arguments else "release").resolve()
    if not root.is_dir():
        raise ValueError(f"release directory does not exist: {root}")
    with tempfile.TemporaryDirectory(prefix="fte-native-smoke-") as temporary:
        work = Path(temporary)
        if sys.platform == "win32":
            _windows(root, work)
        elif sys.platform == "darwin":
            _macos(root, work)
        elif sys.platform.startswith("linux"):
            _linux(root, work)
        else:
            raise RuntimeError(f"unsupported smoke-test host: {sys.platform}")
    print("Installed native artifact passed its self-check and uninstall smoke test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
