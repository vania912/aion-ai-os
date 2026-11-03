from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def make_git_repo(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Factory creating a real (empty) git repository under tmp_path."""

    def _make(name: str) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        return repo

    return _make


FAKE_O_BINARY = 0x40000000  # a flag bit that no OS uses, standing in for Windows' O_BINARY


@pytest.fixture
def windows_text_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Simulate Windows' CRT: a file descriptor opened *without* O_BINARY translates LF to CRLF
    on write. Code that writes data through ``os.open`` must pass the flag or it fails under this
    fixture, as it did on the Windows CI runners."""
    import os

    from aios.adapters import audit_store, memory_writer

    if hasattr(os, "O_BINARY"):
        # On real Windows there is nothing to simulate: the tests using this fixture then check
        # that the *real* O_BINARY flag gives byte-exact files. (Faking it there would strip the
        # real flag and make the CRT translate on top of the simulation.)
        yield
        return

    real_open, real_write, real_close = os.open, os.write, os.close
    text_fds: set[int] = set()

    def fake_open(path, flags, mode=0o777, **kwargs):  # type: ignore[no-untyped-def]
        fd = real_open(path, flags & ~FAKE_O_BINARY, mode, **kwargs)
        if flags & FAKE_O_BINARY:
            text_fds.discard(fd)
        else:
            text_fds.add(fd)
        return fd

    def fake_write(fd, data):  # type: ignore[no-untyped-def]
        return real_write(fd, data.replace(b"\n", b"\r\n") if fd in text_fds else data)

    def fake_close(fd):  # type: ignore[no-untyped-def]
        text_fds.discard(fd)
        real_close(fd)

    monkeypatch.setattr(memory_writer, "_O_BINARY", FAKE_O_BINARY)
    monkeypatch.setattr(audit_store, "_O_BINARY", FAKE_O_BINARY)
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "write", fake_write)
    monkeypatch.setattr(os, "close", fake_close)
    yield
