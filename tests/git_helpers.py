"""Helpers to build real git repositories for tests (no network, deterministic identity)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SECRET = "SECRET_VALUE=hunter2"


def run_git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="author@example.com",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="author@example.com",
        GIT_CONFIG_NOSYSTEM="1",
    )
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def commit(repo: Path, message: str, date: str, files: dict[str, str | bytes | None]) -> str:
    """Write (or delete, for ``None``) files, commit everything, return the new hash."""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message, date=date)
    return run_git(repo, "rev-parse", "HEAD")


@dataclass(frozen=True)
class History:
    repo: Path
    c1: str  # initial: README.md, src/app.py
    c2: str  # adds .env (secret), modifies src/app.py
    c3: str  # modifies README.md, adds server.pem and logo.png
    c4: str  # deletes .env
    c5: str  # adds ONLY .env.local (touches nothing visible)


def make_history(root: Path) -> History:
    repo = root / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    c1 = commit(
        repo,
        "initial: add app",
        "2020-01-01T10:00:00+00:00",
        {"README.md": "hello\n", "src/app.py": "app_marker v1\n"},
    )
    c2 = commit(
        repo,
        "add env config",
        "2026-01-01T10:00:00+00:00",
        {".env": SECRET + "\n", "src/app.py": "app_marker v2\n"},
    )
    c3 = commit(
        repo,
        "update docs workspace mismatch",
        "2026-01-02T10:00:00+00:00",
        {
            "README.md": "hello workspace\n",
            "server.pem": f"-----BEGIN {SECRET}-----\n",
            "logo.png": b"\x89PNG\x00" + SECRET.encode(),
        },
    )
    c4 = commit(repo, "remove env", "2026-01-03T10:00:00+00:00", {".env": None})
    c5 = commit(repo, "only secret", "2026-01-04T10:00:00+00:00", {".env.local": SECRET + "\n"})
    run_git(repo, "tag", "v1", c3)
    return History(repo, c1, c2, c3, c4, c5)
