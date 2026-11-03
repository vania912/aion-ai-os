"""A ready-made world for MCP tests: a git project with a hostile history, memory, a registry."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from aios.interfaces.mcp.context import ServerContext
from aios.interfaces.mcp.server import build_server
from tests.git_helpers import SECRET, History, commit, make_history, run_git
from tests.memory_helpers import write_record

T = TypeVar("T")
SECRET_STRINGS = ("hunter2", "SECRET_VALUE")


@dataclass
class World:
    root: Path
    data: Path
    hist: History
    server: Any
    ctx: ServerContext

    @property
    def repo(self) -> Path:
        return self.hist.repo


def run(coro: Awaitable[T]) -> T:
    async def wrapper() -> T:
        return await coro

    return asyncio.run(wrapper())


def text_of(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def call(world: World, name: str, **args: Any) -> str:
    return text_of(run(world.server.call_tool(name, args)))


def make_world(tmp_path: Path) -> World:
    hist = make_history(tmp_path)
    repo = hist.repo
    (repo / "src" / "util.py").write_text("def workspace():\n    return 1\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "add util", date="2026-02-01T10:00:00+00:00")
    commit(
        repo,
        "touch only logo",
        "2026-02-02T10:00:00+00:00",
        {"logo.png": b"\x89PNG\x00v2" + SECRET.encode()},
    )
    (repo / ".env").write_text(SECRET + "\n")  # untracked secret in the working tree

    data = tmp_path / "data"
    data.mkdir()
    (data / "registry.yaml").write_text(
        f"projects:\n  pulse:\n    path: '{repo.as_posix()}'\n    description: Demo project\n"
    )
    write_record(
        data, id="DEC-0001", project="pulse", title="Use a shared Slack workspace",
        status="superseded", superseded_by="DEC-0002", date="2026-09-01", why=["Simpler setup"],
    )  # fmt: skip
    write_record(
        data, id="DEC-0002", project="pulse", title="Separate MENA Slack from US Slack",
        supersedes="DEC-0001", date="2026-09-15", why=["Avoid mixing monitoring data"],
        reason_for_change="Monitoring data got mixed", made_by=["Raheel"], tags=["slack"],
        body="Use separate workspace handling per region.",
    )  # fmt: skip
    write_record(
        data,
        id="TSK-0001",
        type="task",
        project="pulse",
        title="Investigate workspace mismatch",
        date="2026-10-03",
    )
    write_record(
        data,
        id="Q-0001",
        type="question",
        project="pulse",
        title="Do we need per-region routing?",
        date="2026-10-04",
    )
    server, ctx = build_server(data)
    return World(tmp_path, data, hist, server, ctx)
