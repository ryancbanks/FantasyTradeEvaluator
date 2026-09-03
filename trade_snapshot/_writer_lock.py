"""Crash-safe, cross-process ownership for one persisted search writer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class WriterOwnershipError(RuntimeError):
    """Raised when another process already owns a search result writer."""


class ExclusiveWriterLock:
    """Hold a non-blocking OS lock until the owner closes or exits.

    The small sidecar file is only a rendezvous point.  Ownership lives in the
    operating-system lock, so a crash cannot leave a stale logical lock behind.
    """

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        resolved = Path(database_path).resolve()
        self.path = resolved.with_name(f"{resolved.name}.writer.lock")
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        try:
            lock_file = self.path.open("a+b")
        except OSError as error:
            raise WriterOwnershipError(
                f"could not establish search writer ownership: {error}"
            ) from error
        try:
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            _lock_file(lock_file)
        except (OSError, ValueError) as error:
            lock_file.close()
            raise WriterOwnershipError(
                "search result store already has an active writer"
            ) from error
        self._file = lock_file

    def close(self) -> None:
        lock_file, self._file = self._file, None
        if lock_file is None:
            return
        try:
            lock_file.seek(0)
            _unlock_file(lock_file)
        finally:
            lock_file.close()


if os.name == "nt":
    import msvcrt

    def _lock_file(lock_file: BinaryIO) -> None:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(lock_file: BinaryIO) -> None:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


__all__ = ("ExclusiveWriterLock", "WriterOwnershipError")
