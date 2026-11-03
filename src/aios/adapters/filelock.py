"""Exclusive lock across threads and processes, with a bounded wait.

Used by the audit log and by memory writes. Threads of one process queue on an in-process lock
*per lock file*, then the winner takes an OS file lock by polling non-blocking attempts
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows). The Windows blocking mode (``LK_LOCK``)
gives up after about ten one-second retries with "Resource deadlock avoided"; polling with our own
timeout avoids that (see docs/AUDIT.md).

Do not nest acquisitions of the *same* lock file in one thread: the lock is not re-entrant.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from aios.errors import AuditError

_LOCK_POLL_S = 0.02
_guard = threading.Lock()
_process_locks: dict[str, threading.Lock] = {}


def _process_lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _guard:
        return _process_locks.setdefault(key, threading.Lock())


def _try_os_lock(fd: int) -> bool:
    """One non-blocking attempt at the exclusive OS lock; False if another process holds it."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_lock(path: Path, timeout_s: float, what: str = "lock") -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if missing).

    ``what`` names the lock in the timeout message. Raises ``AuditError`` on timeout.
    """
    deadline = time.monotonic() + timeout_s
    process_lock = _process_lock_for(path)
    if not process_lock.acquire(timeout=timeout_s):
        raise AuditError(f"timed out after {timeout_s:g}s waiting for the {what}")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while not _try_os_lock(fd):
                if time.monotonic() >= deadline:
                    raise AuditError(f"timed out after {timeout_s:g}s waiting for the {what}")
                time.sleep(_LOCK_POLL_S)
            try:
                yield
            finally:
                _os_unlock(fd)
        finally:
            os.close(fd)
    finally:
        process_lock.release()
