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
    assert len(out["tools"]) == 16 and len(out["resources"]) == 5
    assert out["prompts"] == ["analyze_project", "prepare_change_plan", "review_recent_changes"]
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


def test_the_allow_propose_flag_is_accepted_and_recorded(world: World) -> None:
    async def count_tools() -> int:
        params = StdioServerParameters(
            command=SERVE[0], args=[*SERVE[1:], "--allow-propose"], env=env_for(world.data)
        )
        async with Client(params) as client:
            return len((await client.list_tools()).tools)

    assert asyncio.run(count_tools()) == 22  # 16 read-only + 6 write tools
    log = "".join(p.read_text(encoding="utf-8") for p in (world.data / "audit").glob("*.jsonl"))
    start = json.loads(log.splitlines()[0])
    assert start["op"] == "server_start" and start["args"]["allow_propose"] is True


def test_propose_review_accept_history_over_stdio(world: World) -> None:
    """propose (Claude, over stdio, with the flag) -> unreviewed -> human accepts -> history."""

    async def propose() -> tuple[list[str], dict[str, Any], str]:
        params = StdioServerParameters(
            command=SERVE[0], args=[*SERVE[1:], "--allow-propose"], env=env_for(world.data)
        )
        async with Client(params) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            result = await client.call_tool(
                "propose_decision",
                {
                    "project": "pulse",
                    "title": "Route monitoring per region",
                    "why": ["Regions have separate alert owners"],
                    "evidence": ["DEC-0002"],
                    "supersedes": "DEC-0002",
                    "reason_for_change": "Alert ownership is regional",
                },
            )
            before = await client.call_tool("get_decision_history", {"record_id": "DEC-0002"})
            return names, json.loads(text(result)), text(before)

    names, proposal, before = asyncio.run(propose())
    assert len(names) == 22 and "propose_decision" in names
    assert proposal["reviewed"] is False and proposal["status"] == "proposed"
    assert json.loads(before)["current_id"] == "DEC-0002"
    new_id = proposal["id"]

    accept = subprocess.run(  # the human step, a separate process, as on the command line
        [*SERVE[:3], "review", "accept", new_id, "--as", "Vania", "--yes"],
        env=env_for(world.data), capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert accept.returncode == 0, accept.stderr

    async def after() -> str:
        params = StdioServerParameters(command=SERVE[0], args=SERVE[1:], env=env_for(world.data))
        async with Client(params) as client:  # read-only server this time
            return text(await client.call_tool("get_decision_history", {"record_id": "DEC-0001"}))

    chain = json.loads(asyncio.run(after()))
    assert chain["current_id"] == new_id and [e["id"] for e in chain["chain"]][-1] == new_id
    audit = "".join(p.read_text("utf-8") for p in (world.data / "audit").glob("*.jsonl"))
    assert "propose_decision" in audit and "accept_record" in audit


def test_plan_propose_approve_report_verify_over_stdio(world: World) -> None:
    """Claude proposes and reports over stdio; only a person approves or verifies."""
    plan_args = {
        "project": "pulse", "title": "Cache lookups", "request": "faster", "why": "slow",
        "what_will_change": "add a cache", "risks": ["stale"], "expected_result": "faster",
        "files": [{"path": "src/util.py", "action": "modify"}],
        "verification": ["run tests"],
    }  # fmt: skip

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*SERVE[:3], *args], env=env_for(world.data), capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip

    async def session(steps: str) -> dict[str, Any]:
        params = StdioServerParameters(
            command=SERVE[0], args=[*SERVE[1:], "--allow-propose"], env=env_for(world.data)
        )
        out: dict[str, Any] = {}
        async with Client(params) as client:
            if steps == "propose":
                out["created"] = json.loads(text(await client.call_tool("create_plan", plan_args)))
                out["shown"] = text(await client.call_tool("get_plan", {"plan_id": "PLAN-0001"}))
            else:
                reported = await client.call_tool(
                    "record_verification",
                    {
                        "plan_id": "PLAN-0001",
                        "results": [{"step": 1, "outcome": "pass", "evidence": "12 passed"}],
                    },
                )
                out["reported"] = json.loads(text(reported))
                out["shown"] = text(await client.call_tool("get_plan", {"plan_id": "PLAN-0001"}))
        return out

    first = asyncio.run(session("propose"))
    assert first["created"]["status"] == "proposed" and "NOT APPROVED" in first["shown"]
    assert cli("plan", "check", "PLAN-0001").returncode == 1

    approve = cli("plan", "approve", "PLAN-0001", "--yes", "--as", "Vania")
    assert approve.returncode == 0, approve.stdout + approve.stderr
    assert cli("plan", "check", "PLAN-0001").returncode == 0
    assert cli("plan", "outcome", "PLAN-0001", "executed").returncode == 0

    second = asyncio.run(session("report"))
    assert second["reported"]["report_all_steps_pass"] is True
    assert second["reported"]["status"] == "executed" and "approved by Vania" in second["shown"]
    assert cli("plan", "verify", "PLAN-0001", "--yes", "--as", "Vania").returncode == 0
    assert "verified" in cli("plan", "list").stdout
    log = "".join(p.read_text("utf-8") for p in (world.data / "audit").glob("*.jsonl"))
    for op in ("create_plan", "approve_plan", "plan_outcome", "record_verification", "verify_plan"):
        assert op in log, op
