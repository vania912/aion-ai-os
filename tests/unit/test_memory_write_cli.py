from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aios.adapters.memory_files import parse_record
from aios.interfaces.cli.app import app
from aios.services.audit import AuditLog
from aios.services.memory import MemoryService
from aios.services.memory_write import MemoryWriter, NewRecord

runner = CliRunner()


@pytest.fixture(autouse=True)
def user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOS_USER", "Tester")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def run(data: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--data-dir", str(data)])
    return result.exit_code, result.output


def record(data: Path, rid: str, rtype: str = "decisions") -> object:
    return parse_record(
        (data / "projects" / "pulse" / rtype / f"{rid}.md").read_text(encoding="utf-8")
    )


def test_decision_create_writes_an_active_reviewed_record(data: Path) -> None:
    code, out = run(
        data, "decision", "create", "pulse", "--title", "Use SQLite", "--why", "no server",
        "--why", "few records", "--alternative", "a vector db", "--tag", "storage",
        "--body", "More detail.",
    )  # fmt: skip
    assert code == 0 and "created DEC-0001 [active] projects/pulse/decisions/DEC-0001.md" in out
    assert "written to disk only" in out
    r = MemoryService(data).get("DEC-0001").record
    assert (r.status, r.origin, r.reviewed, r.made_by, r.date) == (
        "active",
        "human",
        True,
        ["Tester"],
        date.today(),
    )
    assert r.why == ["no server", "few records"] and r.alternatives_considered == ["a vector db"]
    assert r.body == "More detail." and r.tags == ["storage"]
    assert run(data, "memory", "check")[0] == 0


def test_made_by_can_be_overridden(data: Path) -> None:
    run(
        data,
        "decision",
        "create",
        "pulse",
        "--title",
        "T",
        "--why",
        "w",
        "--made-by",
        "Raheel",
        "--made-by",
        "Vania",
    )
    assert MemoryService(data).get("DEC-0001").record.made_by == ["Raheel", "Vania"]


def test_supersession_from_the_cli_updates_both_and_keeps_history(data: Path) -> None:
    run(data, "decision", "create", "pulse", "--title", "Plain tables", "--why", "simple")
    code, out = run(
        data, "decision", "create", "pulse", "--title", "FTS5", "--why", "ranking",
        "--supersedes", "DEC-0001", "--reason-for-change", "LIKE cannot rank",
    )  # fmt: skip
    assert code == 0 and "stays active" not in out
    assert (
        "updated DEC-0002: proposed -> active" in out
        and "updated DEC-0001: active -> superseded (by DEC-0002)" in out
    )
    new = MemoryService(data).get("DEC-0002").record
    assert (new.status, new.reviewed, new.accepted_by) == (
        "active",
        True,
        "Tester",
    ) and new.accepted_digest
    code, out = run(data, "decision", "history", "DEC-0001")
    assert (
        "current: DEC-0002" in out
        and "replaced the previous decision because: LIKE cannot rank" in out
    )
    assert run(data, "memory", "check")[1].strip() == "ok: 0 issue(s)"


def test_repair_finishes_a_half_applied_supersession_and_is_idempotent(data: Path) -> None:
    writer = MemoryWriter(data)
    old = writer.create(NewRecord(type="decision", project="pulse", title="A", why=["w"])).record.id
    new = writer.create(
        NewRecord(type="decision", project="pulse", title="B", why=["w"], supersedes=old)
    ).record.id  # created but never completed
    code, out = run(data, "decision", "repair", new)
    assert code == 0 and f"updated {old}: active -> superseded (by {new})" in out
    code, out = run(data, "decision", "repair", new)
    assert code == 0 and "nothing to do" in out
    assert run(data, "decision", "repair", old)[0] == 2


@pytest.mark.parametrize(
    "args",
    [
        ("decision", "create", "pulse", "--title", "T"),  # --why is required
        ("decision", "create", "pulse", "--title", "T", "--why", "w", "--supersedes", "DEC-0099"),
        ("decision", "create", "Bad Project", "--title", "T", "--why", "w"),
        ("decision", "create", "pulse", "--title", "  ", "--why", "w"),
        ("decision", "create", "pulse", "--title", "T", "--why", "w", "--tag", "Bad Tag"),
        ("decision", "create", "pulse", "--title", "T", "--why", "w", "--related-task", "TSK-0099"),
        (
            "decision",
            "create",
            "pulse",
            "--title",
            "T",
            "--why",
            "w",
            "--body",
            "x",
            "--body-file",
            "f",
        ),
        ("task", "add", "pulse", "--title", "T", "--body-file", "/definitely/not/here.txt"),
        ("task", "status", "TSK-0099", "done"),
        ("memory", "add", "pulse", "--type", "decision", "--title", "T"),
    ],
)
def test_bad_input_is_refused_and_creates_nothing(data: Path, args: tuple[str, ...]) -> None:
    code, _ = run(data, *args)
    assert code == 2
    assert not (data / "projects").exists() or [p for p in (data / "projects").rglob("*.md")] == []


def test_body_file_is_read_and_size_limited(data: Path, tmp_path: Path) -> None:
    good = tmp_path / "body.md"
    good.write_text("From a file.\n\nSecond paragraph.", encoding="utf-8")
    assert run(data, "task", "add", "pulse", "--title", "T", "--body-file", str(good))[0] == 0
    assert MemoryService(data).get("TSK-0001").record.body == "From a file.\n\nSecond paragraph."
    big = tmp_path / "big.md"
    big.write_text("x" * 300_000, encoding="utf-8")
    code, out = run(data, "task", "add", "pulse", "--title", "T2", "--body-file", str(big))
    assert code == 2 and "larger than" in out


def test_task_commands(data: Path) -> None:
    assert run(data, "task", "add", "pulse", "--title", "Measure", "--tag", "search")[0] == 0
    assert (
        "TSK-0001: open -> in_progress" in run(data, "task", "status", "TSK-0001", "in_progress")[1]
    )
    assert "TSK-0001: in_progress -> done" in run(data, "task", "status", "tsk-0001", "done")[1]
    code, out = run(data, "task", "status", "TSK-0001", "in_progress")
    assert code == 2 and "cannot move from done to in_progress" in out
    assert run(data, "task", "status", "TSK-0001", "bogus")[0] == 2
    assert [h.id for h in MemoryService(data).list_records(status="done").records] == ["TSK-0001"]


def test_memory_add_creates_each_kind(data: Path) -> None:
    kinds = [
        ("assumption", "ASM", "unverified"),
        ("constraint", "CON", "active"),
        ("question", "Q", "open"),
        ("discovery", "DSC", "active"),
        ("note", "NOTE", "active"),
    ]
    for rtype, prefix, status in kinds:
        code, out = run(data, "memory", "add", "pulse", "--type", rtype, "--title", f"A {rtype}")
        assert code == 0 and f"created {prefix}-0001 [{status}]" in out


def test_writes_are_audited_with_the_cli_actor(data: Path) -> None:
    run(data, "decision", "create", "pulse", "--title", "A", "--why", "w")
    run(data, "task", "add", "pulse", "--title", "T")
    run(data, "task", "status", "TSK-0001", "done")
    run(data, "task", "status", "TSK-0001", "done")  # no change: no event
    events = AuditLog(data, actor="cli").list_events(limit=50)
    assert [(e.op, e.actor) for e in reversed(events)] == [
        ("create_record", "cli"), ("create_record", "cli"), ("set_task_status", "cli"),
    ]  # fmt: skip
    assert events[0].args == {"id": "TSK-0001", "from": "open", "to": "done"}
    assert "Use" not in str([e.args for e in events])  # no titles or content in the log
    assert AuditLog(data, actor="cli").verify().ok


def test_an_unwritable_audit_log_warns_but_does_not_block_the_human(data: Path) -> None:
    (data / "audit").write_text("a file where the directory should be")
    code, out = run(data, "task", "add", "pulse", "--title", "T")
    assert code == 0 and "created TSK-0001" in out and "audit log could not be written" in out
    assert MemoryService(data).get("TSK-0001").record.title == "T"


def test_nothing_is_ever_committed_to_a_git_data_repo(data: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(data)], check=True)
    run(data, "decision", "create", "pulse", "--title", "A", "--why", "w")
    run(data, "task", "add", "pulse", "--title", "T")
    git = ["git", "-C", str(data)]
    head = subprocess.run([*git, "rev-parse", "--verify", "-q", "HEAD"], capture_output=True)
    assert head.returncode != 0  # still no commit
    porcelain = subprocess.run(
        [*git, "status", "--porcelain", "-uall"], capture_output=True, text=True
    ).stdout
    assert "projects/pulse/decisions/DEC-0001.md" in porcelain and ".index" not in porcelain
