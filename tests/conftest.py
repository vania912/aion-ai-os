from __future__ import annotations

import subprocess
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
