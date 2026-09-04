"""Small, shell-free helpers for revealing local artifacts to the user."""

from pathlib import Path
import subprocess
import sys


def reveal_file(path: str | Path) -> bool:
    """Open the platform file manager at ``path``; return false when unavailable."""

    target = Path(path).resolve(strict=True)
    if not target.is_file():
        raise FileNotFoundError(target)
    if sys.platform == "win32":
        command = ["explorer.exe", f"/select,{target}"]
    elif sys.platform == "darwin":
        command = ["open", "-R", str(target)]
    else:
        command = ["xdg-open", str(target.parent)]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        return False
    return True
