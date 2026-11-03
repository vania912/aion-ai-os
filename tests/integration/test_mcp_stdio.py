"""End-to-end: a real `aios serve` subprocess spoken to over stdio, as Claude Code would."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tests.mcp_helpers import SECRET_STRINGS, World, make_world

SERVE = [sys.executable, "-m", "aios", "serve"]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def env_for(data: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIOS_")}
    env["AIOS_DATA_DIR"] = str(data)
    return env


def text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def is_error(result: Any) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def test_full_session_over_stdio(world: World) -> None:
    async def session() -> dict[str, Any]:
        params = StdioServerParameters(command=SERVE[0], args=SERVE[1:], env=env_for(world.data))
        out: dict[str, Any] = {}
        async with Client(params) as client:
            out["tools"] = sorted(t.name for t in (await client.list_tools()).tools)
            out["resources"] = sorted(str(r.uri) for r in (await client.list_resources()).resources)
            out["prompts"] = sorted(p.name for p in (await client.list_prompts()).prompts)
            out["projects"] = text(await client.call_tool("list_projects", {}))
            out["file"] = text(
                await client.call_tool(
                    "read_project_file", {"project": "pulse", "path": "src/util.py"}
                )
            )
            out["memory"] = text(
                await client.call_tool("search_memory", {"query": "why separate the MENA Slack"})
            )
            denied = await client.call_tool(
                "read_project_file", {"project": "pulse", "path": ".env"}
            )
            out["denied"] = (is_error(denied), text(denied))
            resource = await client.read_resource("project://pulse/context")
            out["context_resource"] = getattr(resource.contents[0], "text", "")
            prompt = await client.get_prompt("analyze_project", {"project": "pulse"})
            out["prompt"] = getattr(prompt.messages[0].content, "text", "")
        return out

    out = asyncio.run(session())
    assert len(out["tools"]) == 14 and len(out["resources"]) == 5
    assert out["prompts"] == ["analyze_project", "review_recent_changes"]
    assert json.loads(out["projects"])["projects"][0]["id"] == "pulse"
    assert "def workspace" in out["file"]
    assert json.loads(out["memory"])["hits"][0]["id"] == "DEC-0002"
    assert out["denied"][0] is True and "denied_name" in out["denied"][1]
    assert "# Project pulse" in out["context_resource"] and 'project "pulse"' in out["prompt"]
    for secret in SECRET_STRINGS:
        assert not any(secret in str(v) for v in out.values())

    # The server process has exited; its audit trail is on disk.
    log = "".join(p.read_text(encoding="utf-8") for p in (world.data / "audit").glob("*.jsonl"))
    events = [json.loads(line) for line in log.splitlines()]
    assert events[0]["op"] == "server_start" and events[0]["event"] == "note"
    assert {e["actor"] for e in events} == {"mcp"}
    ops = [e["op"] for e in events if e["event"] == "request"]
    for expected in (
        "list_projects",
        "read_project_file",
        "search_memory",
        "read_resource",
        "get_prompt",
    ):
        assert expected in ops
    assert any(e["outcome"] == "denied" and e["code"] == "denied_name" for e in events)
    from aios.services.audit import AuditLog

    assert AuditLog(world.data, actor="cli").verify().ok


def test_stdout_stays_protocol_only_and_shutdown_is_prompt(world: World) -> None:
    proc = subprocess.Popen(
        SERVE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env_for(world.data),
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}},
    }  # fmt: skip
    proc.stdin.write((json.dumps(init) + "\n").encode())
    proc.stdin.flush()
    first = json.loads(proc.stdout.readline())  # must be a clean JSON-RPC response
    assert first["id"] == 1 and first["result"]["serverInfo"]["name"] == "aios"
    assert "READ-ONLY" in first["result"]["instructions"]
    assert set(first["result"]["capabilities"]) >= {"tools", "resources", "prompts"}

    started = time.monotonic()
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr.read().decode()
    # Claude Code force-stops a stdio server that is still running ~300 ms after closing stdin
    # (documented behaviour); allow generous slack for slow CI but fail on anything slow-ish.
    assert elapsed < 3, f"shutdown took {elapsed:.2f}s"
    rest = proc.stdout.read().decode()
    assert all(json.loads(line) for line in rest.splitlines() if line.strip())  # still only JSON


def _start_failure(env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(SERVE, stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=60)


def test_startup_failures_use_stderr_and_leave_stdout_empty(tmp_path: Path) -> None:
    base = {k: v for k, v in os.environ.items() if not k.startswith("AIOS_")}

    no_dir = _start_failure(base)
    assert no_dir.returncode == 2 and no_dir.stdout == b"" and b"AIOS_DATA_DIR" in no_dir.stderr

    empty = tmp_path / "empty"
    empty.mkdir()
    no_registry = _start_failure({**base, "AIOS_DATA_DIR": str(empty)})
    assert no_registry.returncode == 1 and no_registry.stdout == b""
    assert b"registry" in no_registry.stderr.lower()


def test_unwritable_audit_log_stops_the_server_at_start(world: World) -> None:
    (world.data / "audit").write_text("this is a file, not a directory")
    failed = _start_failure(env_for(world.data))
    assert failed.returncode == 1 and failed.stdout == b"" and b"audit" in failed.stderr.lower()
