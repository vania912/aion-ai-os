from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aios.errors import PolicyViolation
from aios.interfaces.cli.app import app
from aios.policy import OperationPolicy, PermissionLevel
from aios.services.audit import AuditLog

runner = CliRunner()


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    policy = OperationPolicy()
    policy.register("read_project_file", PermissionLevel.READ_ONLY)
    audit = AuditLog(d, actor="mcp")
    audit.run(
        "read_project_file", lambda: "x", policy=policy, project="pulse",
        args={"path": "src/app.py"}, summarize=lambda r: {"chars": len(r)},
    )  # fmt: skip
    with pytest.raises(PolicyViolation):
        audit.run(
            "delete_file", lambda: None, policy=policy, project="pulse", args={"path": "a.py"}
        )
    return d


def run(data: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--data-dir", str(data)])
    return result.exit_code, result.output


def test_list_shows_newest_first_with_state_and_args(data: Path) -> None:
    code, out = run(data, "audit", "list")
    rows = [line.split("\t") for line in out.strip().splitlines()]
    assert code == 0 and [r[0] for r in rows] == ["4", "3", "2", "1"]
    assert (
        rows[0][3:7] == ["result", "delete_file", "pulse", "denied"]
        and rows[0][7] == "operation_denied"
    )
    assert rows[1][3:7] == ["request", "delete_file", "pulse", "deny"]
    assert rows[2][6] == "ok" and rows[3][8] == "path='src/app.py'"


def test_list_filters(data: Path) -> None:
    assert run(data, "audit", "list", "--outcome", "denied")[1].count("\n") == 1
    assert run(data, "audit", "list", "--op", "read_project_file")[1].count("\n") == 2
    code, out = run(data, "audit", "list", "--project", "nobody")
    assert code == 0 and "no matching events" in out


def test_show_prints_the_whole_call(data: Path) -> None:
    call_id = run(data, "audit", "list", "--op", "read_project_file")[1].split("\t")[2]
    full = AuditLog(data, actor="cli").list_events(op="read_project_file")[0].call_id
    assert full.startswith(call_id)
    code, out = run(data, "audit", "show", full)
    assert code == 0 and out.index("--- request") < out.index("--- result")
    assert "outcome: ok" in out and "verdict: allow" in out and "'chars': 1" in out


@pytest.mark.parametrize(
    "args",
    [("audit", "show", "0" * 16), ("audit", "show", "xyz"), ("audit", "list", "--limit", "0")],
)
def test_errors_exit_with_2(data: Path, args: tuple[str, ...]) -> None:
    code, out = run(data, *args)
    assert code == 2 and "error:" in out


def test_verify_and_anchor(data: Path) -> None:
    code, out = run(data, "audit", "verify")
    assert code == 0 and "ok: 4 event(s), last seq 4" in out
    code, out = run(data, "audit", "anchor")
    assert code == 0 and "seq: 4" in out and "hash: " in out and "(empty log)" not in out


def test_verify_and_doctor_detect_tampering(
    tmp_path: Path, make_git_repo: Callable[[str], Path], data: Path
) -> None:
    repo = make_git_repo("repo")
    (data / "registry.yaml").write_text(f"projects:\n  pulse:\n    path: '{repo.as_posix()}'\n")
    assert run(data, "doctor")[0] == 0

    log = next((data / "audit").glob("*.jsonl"))
    rows = log.read_text(encoding="utf-8").splitlines()
    forged = rows[2].replace('"verdict":"deny"', '"verdict":"allow"')  # rewrite history
    assert forged != rows[2], "the edit must actually change the line"
    rows[2] = forged
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    code, out = run(data, "audit", "verify")
    assert code == 1 and "bad_hash" in out
    code, out = run(data, "doctor")
    assert code == 1 and "audit_chain_broken" in out


def test_empty_audit_log(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(empty, "audit", "verify")[1].strip() == "ok: 0 event(s), last seq 0"
    assert "(empty log)" in run(empty, "audit", "anchor")[1]
    assert run(empty, "audit", "list")[0] == 0
