from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aios.adapters import memory_writer
from aios.adapters.filelock import exclusive_lock
from aios.adapters.memory_files import parse_record, scan_memory
from aios.errors import AuditError, InvalidRequestError, UnknownRecordError
from aios.services.memory import MemoryService
from aios.services.memory_write import (
    TASK_TRANSITIONS,
    MemoryWriter,
    NewRecord,
    WriteConfig,
    decision_digest,
)

TODAY = date(2026, 11, 3)


def nr(**kw: Any) -> NewRecord:
    base: dict[str, Any] = {
        "type": "decision",
        "project": "pulse",
        "title": "Use SQLite",
        "why": ["simple"],
    }
    return NewRecord(**{**base, **kw})


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def writer(data: Path) -> MemoryWriter:
    return MemoryWriter(data, today=TODAY)


def issues(data: Path) -> list[Any]:
    return MemoryService(data).check()


def read(data: Path, rel: str) -> str:
    return (data / rel).read_text(encoding="utf-8")


# -- creating: humans -----------------------------------------------------------------------


def test_human_decision_is_active_reviewed_and_searchable(writer: MemoryWriter, data: Path) -> None:
    result = writer.create(nr(made_by=["Vania"], body="Body text.", tags=["storage"]))
    r = result.record
    assert (r.id, r.status, r.origin, r.reviewed, r.date) == (
        "DEC-0001",
        "active",
        "human",
        True,
        TODAY,
    )
    assert result.path == "projects/pulse/decisions/DEC-0001.md" and result.notes == []
    assert parse_record(read(data, result.path)).title == "Use SQLite"
    assert [h.id for h in MemoryService(data).search("sqlite").hits] == ["DEC-0001"]
    assert issues(data) == []


@pytest.mark.parametrize(
    ("rtype", "prefix", "status"),
    [
        ("assumption", "ASM", "unverified"), ("constraint", "CON", "active"),
        ("question", "Q", "open"), ("discovery", "DSC", "active"),
        ("task", "TSK", "open"), ("note", "NOTE", "active"),
    ],
)  # fmt: skip
def test_every_type_gets_its_initial_status_and_prefix(
    writer: MemoryWriter, rtype: str, prefix: str, status: str
) -> None:
    r = writer.create(nr(type=rtype, why=[])).record
    assert r.id == f"{prefix}-0001" and r.status == status and r.reviewed


def test_ids_increase_per_prefix(writer: MemoryWriter) -> None:
    ids = [writer.create(nr(type=t, why=[])).record.id for t in ("task", "task", "note", "task")]
    assert ids == ["TSK-0001", "TSK-0002", "NOTE-0001", "TSK-0003"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"why": []}, "needs at least one reason"),
        ({"title": "  "}, "title"),
        ({"project": "Bad Project"}, "project"),
        ({"project": "../x"}, "project"),
        ({"tags": ["Not A Slug"]}, "tags"),
        ({"related_files": ["../secret"]}, "related_files"),
        ({"body": "nul\x00"}, "control characters"),
        ({"why": ["bell\x07"]}, "control characters"),
        ({"title": "two\nlines"}, "single line"),
    ],
)
def test_invalid_content_is_refused_and_writes_nothing(
    writer: MemoryWriter, data: Path, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        writer.create(nr(**kwargs))
    assert not (data / "projects").exists() or list((data / "projects").rglob("*")) == []
    assert writer.create(nr()).record.id == "DEC-0001"  # the id was not consumed


def test_references_must_exist_and_have_the_right_type(writer: MemoryWriter) -> None:
    task = writer.create(nr(type="task", why=[])).record.id
    dec = writer.create(nr()).record.id
    assert writer.create(nr(related_tasks=[task], related_decisions=[dec])).record.id == "DEC-0002"
    bad_refs = [
        ("related_tasks", dec),
        ("related_decisions", task),
        ("related_tasks", "TSK-0099"),
        ("assumptions", task),
        ("constraints", dec),
    ]
    for field, ref in bad_refs:
        with pytest.raises(InvalidRequestError, match=f"{field}: {ref}"):
            writer.create(nr(**{field: [ref]}))


def test_new_record_cannot_carry_server_controlled_fields() -> None:
    forbidden = {
        "status": "active", "origin": "human", "reviewed": True, "id": "DEC-0001",
        "date": "2026-01-01", "accepted_by": "me", "accepted_digest": "a" * 64,
        "superseded_by": "DEC-0002",
    }  # fmt: skip
    for field, value in forbidden.items():
        with pytest.raises(ValidationError):
            nr(**{field: value})


# -- creating: Claude ---------------------------------------------------------------------


def test_a_claude_decision_is_proposed_unreviewed_and_needs_evidence(
    writer: MemoryWriter, data: Path
) -> None:
    with pytest.raises(InvalidRequestError, match="needs evidence"):
        writer.create(nr(), origin="claude")
    result = writer.create(nr(evidence=["commit 1e9d7eb7a7"]), origin="claude")
    r = result.record
    assert (r.status, r.origin, r.reviewed, r.accepted_by) == ("proposed", "claude", False, None)
    assert result.notes == ["awaiting human review: aios review show DEC-0001"]
    assert "reviewed: false" in read(data, result.path) and "origin: claude" in read(
        data, result.path
    )
    assert issues(data) == []


def test_other_claude_records_are_unreviewed_with_their_normal_status(writer: MemoryWriter) -> None:
    task = writer.create(nr(type="task", why=[]), origin="claude").record
    note = writer.create(nr(type="note", why=[]), origin="claude").record
    assert (task.status, task.reviewed, task.origin) == ("open", False, "claude")
    assert (note.status, note.reviewed) == ("active", False)


def test_a_claude_record_can_never_be_active_or_reviewed(writer: MemoryWriter) -> None:
    for rtype, why, supersedes in itertools.product(
        ("decision", "task", "note"), ([], ["r"]), (None,)
    ):
        try:
            r = writer.create(
                nr(type=rtype, why=why, evidence=["e"], supersedes=supersedes), origin="claude"
            ).record
        except InvalidRequestError:
            continue
        assert r.reviewed is False and r.origin == "claude"
        assert not (r.type == "decision" and r.status == "active")


def test_claude_text_cannot_smuggle_in_fields(writer: MemoryWriter, data: Path) -> None:
    sneaky = "x\n---\nstatus: active\nreviewed: true\norigin: human\n---\ny"
    r = writer.create(
        nr(title="Legit title", why=["status: active", "reviewed: true", "# comment"], body=sneaky,
           evidence=["status: active"]),
        origin="claude",
    ).record  # fmt: skip
    stored = parse_record(read(data, f"projects/pulse/decisions/{r.id}.md"))
    assert (stored.status, stored.reviewed, stored.origin) == ("proposed", False, "claude")
    assert stored.body == sneaky and stored.why[0] == "status: active"


def test_the_body_cap_applies_to_claude_only(data: Path) -> None:
    writer = MemoryWriter(data, config=WriteConfig(max_claude_body_chars=50), today=TODAY)
    with pytest.raises(InvalidRequestError, match="too long"):
        writer.create(nr(type="note", why=[], body="x" * 51), origin="claude")
    assert (
        writer.create(nr(type="note", why=[], body="x" * 50), origin="claude").record.reviewed
        is False
    )
    assert writer.create(nr(type="note", why=[], body="x" * 5000)).record.reviewed is True


def test_the_unreviewed_quota_is_per_project_and_ignores_humans(data: Path) -> None:
    writer = MemoryWriter(data, config=WriteConfig(max_unreviewed_per_project=3), today=TODAY)
    for n in range(3):
        writer.create(nr(type="note", why=[], title=f"Note {n}"), origin="claude")
    with pytest.raises(InvalidRequestError, match="awaiting review"):
        writer.create(nr(type="task", why=[], title="Task"), origin="claude")
    other = nr(type="note", why=[], project="blueprint", title="Note 0")
    assert writer.create(other, origin="claude").record.project == "blueprint"
    assert writer.create(nr(type="note", why=[], title="Human")).record.reviewed is True
    memory_writer.edit_front_matter(data / "projects/pulse/notes/NOTE-0001.md", {"reviewed": True})
    assert (
        writer.create(nr(type="task", why=[], title="Task"), origin="claude").record.id
        == "TSK-0001"
    )


def test_claude_cannot_propose_the_same_thing_twice_while_it_awaits_review(data: Path) -> None:
    writer = MemoryWriter(data, today=TODAY)
    first = writer.create(nr(type="note", why=[], title="Cache  Keys"), origin="claude").record
    with pytest.raises(InvalidRequestError, match=f"{first.id} already proposes"):
        writer.create(nr(type="note", why=[], title="cache keys"), origin="claude")
    # a different type, a different project, or a human record with that title is not a repeat
    writer.create(nr(type="task", why=[], title="cache keys"), origin="claude")
    writer.create(nr(type="note", why=[], title="cache keys", project="blueprint"), origin="claude")
    writer.create(nr(type="note", why=[], title="cache keys"), origin="human")
    # once reviewed, it no longer blocks
    memory_writer.edit_front_matter(
        data / f"projects/pulse/notes/{first.id}.md", {"reviewed": True}
    )
    writer.create(nr(type="note", why=[], title="cache keys"), origin="claude")


# -- superseding --------------------------------------------------------------------------


def seeded(writer: MemoryWriter) -> str:
    return writer.create(nr(title="Use plain tables")).record.id


def test_a_proposed_superseding_decision_changes_nothing_until_completed(
    writer: MemoryWriter, data: Path
) -> None:
    old = seeded(writer)
    before = read(data, f"projects/pulse/decisions/{old}.md")
    proposal = nr(
        title="Use FTS5",
        supersedes=old,
        reason_for_change="LIKE cannot rank",
        evidence=["commit abc"],
    )
    result = writer.create(proposal, origin="claude")
    assert result.record.status == "proposed" and result.record.supersedes == old
    assert f"{old} stays active until {result.record.id} is accepted" in result.notes
    assert read(data, f"projects/pulse/decisions/{old}.md") == before  # untouched


def test_completing_a_supersession_updates_both_records(writer: MemoryWriter, data: Path) -> None:
    old = seeded(writer)
    proposal = nr(
        title="Use FTS5", supersedes=old, reason_for_change="rank", evidence=["commit abc"]
    )
    new = writer.create(proposal, origin="claude").record.id
    changes = writer.complete_supersession(new, accepted_by="Vania")
    assert changes == [f"{new}: proposed -> active", f"{old}: active -> superseded (by {new})"]
    succ = parse_record(read(data, f"projects/pulse/decisions/{new}.md"))
    pred = parse_record(read(data, f"projects/pulse/decisions/{old}.md"))
    assert (succ.status, succ.reviewed, succ.accepted_by) == ("active", True, "Vania")
    assert succ.accepted_digest == decision_digest(succ) and succ.origin == "claude"
    assert (pred.status, pred.superseded_by) == ("superseded", new)
    chain = MemoryService(data).decision_history(old)
    assert (
        [e.id for e in chain.chain] == [old, new] and chain.current_id == new and chain.notes == []
    )
    assert issues(data) == []


def test_completing_twice_changes_nothing_the_second_time(writer: MemoryWriter, data: Path) -> None:
    old = seeded(writer)
    new = writer.create(nr(title="Use FTS5", supersedes=old)).record.id
    writer.complete_supersession(new)
    files = {p: p.read_bytes() for p in (data / "projects").rglob("*.md")}
    assert writer.complete_supersession(new) == []
    assert {p: p.read_bytes() for p in (data / "projects").rglob("*.md")} == files


def test_a_crash_between_the_two_steps_is_visible_and_recoverable(
    writer: MemoryWriter, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = seeded(writer)
    proposal = nr(title="Use FTS5", supersedes=old, evidence=["commit abc"])
    new = writer.create(proposal, origin="claude").record.id
    real, calls = memory_writer.edit_front_matter, {"n": 0}

    def crash_on_second(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("power cut")
        return real(*args, **kwargs)

    monkeypatch.setattr("aios.services.memory_write.edit_front_matter", crash_on_second)
    with pytest.raises(OSError, match="power cut"):
        writer.complete_supersession(new, accepted_by="Vania")
    monkeypatch.undo()

    pred = parse_record(read(data, f"projects/pulse/decisions/{old}.md"))
    assert (
        pred.status == "active"
    )  # half-applied: successor is active, predecessor is not yet superseded
    found = {i.code for i in issues(data)}
    assert {"predecessor_not_superseded", "not_reciprocal"} <= found
    assert (
        MemoryService(data).decision_history(old).current_id == new
    )  # the reader still resolves it
    assert writer.complete_supersession(new, accepted_by="Vania") == [
        f"{old}: active -> superseded (by {new})"
    ]
    assert issues(data) == []


@pytest.mark.parametrize(
    "setup",
    [
        "missing",
        "not_decision",
        "other_project",
        "not_active",
        "already_replaced",
        "non_decision_type",
    ],
)
def test_invalid_supersession_requests_are_refused(writer: MemoryWriter, setup: str) -> None:
    old = seeded(writer)
    task = writer.create(nr(type="task", why=[])).record.id
    other = writer.create(nr(project="blueprint")).record.id
    if setup == "missing":
        kwargs: dict[str, Any] = {"supersedes": "DEC-0099"}
    elif setup == "not_decision":
        kwargs = {"supersedes": task}
    elif setup == "other_project":
        kwargs = {"supersedes": other}
    elif setup == "not_active":
        replaced = writer.create(nr(title="B", supersedes=old)).record.id
        writer.complete_supersession(replaced)
        kwargs = {"supersedes": old}
    elif setup == "already_replaced":
        writer.create(nr(title="B", supersedes=old))
        kwargs = {"supersedes": old}
    else:
        kwargs = {"type": "task", "why": [], "supersedes": old}
    with pytest.raises(InvalidRequestError):
        writer.create(nr(title="C", **kwargs))


def test_complete_supersession_guards(writer: MemoryWriter, data: Path) -> None:
    old = seeded(writer)
    new = writer.create(nr(title="B", supersedes=old)).record.id
    with pytest.raises(InvalidRequestError, match="not a decision that supersedes"):
        writer.complete_supersession(old)
    with pytest.raises(InvalidRequestError):
        writer.complete_supersession("DEC-0099")
    memory_writer.edit_front_matter(
        data / f"projects/pulse/decisions/{new}.md", {"status": "rejected"}
    )
    with pytest.raises(InvalidRequestError, match="only a proposed or active"):
        writer.complete_supersession(new)  # a rejected decision must not be revived
    assert parse_record(read(data, f"projects/pulse/decisions/{new}.md")).status == "rejected"


def test_hand_written_comments_survive_supersession(writer: MemoryWriter, data: Path) -> None:
    path = data / "projects/pulse/decisions/DEC-0001.md"
    path.parent.mkdir(parents=True)
    hand = "\n".join(
        [
            "---", "# my note", "id: DEC-0001", "type: decision", "project: pulse", "title: Old",
            "status: active  # still true", "date: 2026-01-01", "why:", "  - because # reason",
            "---", "Old body", "",
        ]
    )  # fmt: skip
    path.write_text(hand, encoding="utf-8")
    new = writer.create(nr(title="New", supersedes="DEC-0001")).record.id
    writer.complete_supersession(new)
    now = path.read_text(encoding="utf-8")
    assert "# my note" in now and "# reason" in now and "status: superseded  # still true" in now
    assert now.endswith("---\nOld body\n") and "superseded_by: DEC-0002" in now


# -- task status --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "target"), [(s, t) for s, ts in TASK_TRANSITIONS.items() for t in ts]
)
def test_allowed_task_transitions(
    writer: MemoryWriter, data: Path, start: str, target: str
) -> None:
    tid = writer.create(nr(type="task", why=[])).record.id
    path = data / f"projects/pulse/tasks/{tid}.md"
    if start != "open":
        memory_writer.edit_front_matter(path, {"status": start})
    assert writer.set_task_status(tid.lower(), target).status == target
    assert parse_record(path.read_text(encoding="utf-8")).status == target


@pytest.mark.parametrize(
    ("start", "target"),
    [("done", "in_progress"), ("done", "blocked"), ("cancelled", "done"), ("blocked", "done")],
)
def test_forbidden_task_transitions(
    writer: MemoryWriter, data: Path, start: str, target: str
) -> None:
    tid = writer.create(nr(type="task", why=[])).record.id
    memory_writer.edit_front_matter(data / f"projects/pulse/tasks/{tid}.md", {"status": start})
    with pytest.raises(InvalidRequestError, match="cannot move"):
        writer.set_task_status(tid, target)


def test_task_status_edge_cases(writer: MemoryWriter, data: Path) -> None:
    tid = writer.create(nr(type="task", why=[], body="Do it.")).record.id
    path = data / f"projects/pulse/tasks/{tid}.md"
    before = path.read_bytes()
    assert (
        writer.set_task_status(tid, "open").status == "open" and path.read_bytes() == before
    )  # no-op
    with pytest.raises(InvalidRequestError, match="status must be one of"):
        writer.set_task_status(tid, "finished")
    with pytest.raises(UnknownRecordError):
        writer.set_task_status("TSK-0099", "done")
    dec = writer.create(nr()).record.id
    with pytest.raises(InvalidRequestError, match="not a task"):
        writer.set_task_status(dec, "done")
    writer.set_task_status(tid, "done")
    assert "Do it." in path.read_text(encoding="utf-8")


# -- concurrency, locks and failures ----------------------------------------------------------


def test_threads_creating_concurrently_get_unique_ids(data: Path) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        w = MemoryWriter(data, today=TODAY)
        try:
            for i in range(5):
                w.create(nr(type="note", why=[], title=f"note {n}-{i}"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    scan = scan_memory(data)
    ids = [r.record.id for r in scan.records]
    assert len(ids) == 40 == len(set(ids)) and scan.issues == []


def test_processes_creating_concurrently_get_unique_ids(data: Path) -> None:
    code = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "from aios.services.memory_write import MemoryWriter, NewRecord",
            "w = MemoryWriter(Path(sys.argv[1]))",
            "for i in range(5):",
            "    w.create(NewRecord(type='note', project='pulse', title=f'n {sys.argv[2]}-{i}'))",
        ]
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(data), str(n)], stderr=subprocess.PIPE)
        for n in range(3)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    scan = scan_memory(data)
    ids = [r.record.id for r in scan.records]
    assert len(ids) == 15 == len(set(ids)) and scan.issues == []


def test_a_held_write_lock_times_out_clearly_and_recovers(data: Path) -> None:
    writer = MemoryWriter(data, config=WriteConfig(lock_timeout_s=0.3), today=TODAY)
    lock = data / ".index" / "write.lock"
    holder_code = "\n".join(
        [
            "import sys, time",
            "from pathlib import Path",
            "from aios.adapters.filelock import exclusive_lock",
            "with exclusive_lock(Path(sys.argv[1]), 10):",
            "    print('locked', flush=True)",
            "    time.sleep(2.0)",
        ]
    )
    (data / ".index").mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(lock)], stdout=subprocess.PIPE
    )
    assert holder.stdout is not None
    try:
        assert holder.stdout.readline().strip() == b"locked"
        started = time.monotonic()
        with pytest.raises(AuditError, match="memory write lock"):
            writer.create(nr(type="note", why=[]))
        assert 0.25 <= time.monotonic() - started < 1.9
    finally:
        holder.wait(timeout=30)
    assert writer.create(nr(type="note", why=[])).record.id == "NOTE-0001"


def test_the_cache_directory_is_git_ignored(writer: MemoryWriter, data: Path) -> None:
    writer.create(nr(type="note", why=[]))
    assert (data / ".index" / ".gitignore").read_text() == "*\n"
    assert (data / ".index" / "write.lock").exists()


def test_a_failed_write_leaves_no_trace_and_does_not_consume_the_id(
    writer: MemoryWriter, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        writer.create(nr(type="note", why=[]))
    monkeypatch.undo()
    leftovers = [p for p in (data / "projects").rglob("*") if p.is_file()]
    assert leftovers == []
    assert writer.create(nr(type="note", why=[])).record.id == "NOTE-0001"


def test_exclusive_lock_is_importable_for_other_writers() -> None:
    assert callable(exclusive_lock)


def test_accepting_a_claude_proposal_requires_a_name(writer: MemoryWriter, data: Path) -> None:
    old = seeded(writer)
    proposal = nr(title="B", supersedes=old, evidence=["commit abc"])
    new = writer.create(proposal, origin="claude").record.id
    with pytest.raises(InvalidRequestError, match="who accepted"):
        writer.complete_supersession(new)
    assert parse_record(read(data, f"projects/pulse/decisions/{new}.md")).status == "proposed"
    assert writer.complete_supersession(new, accepted_by="Vania")


@pytest.mark.parametrize("status", ["deprecated", "rejected", "proposed", "superseded"])
def test_only_an_active_decision_can_be_superseded(
    writer: MemoryWriter, data: Path, status: str
) -> None:
    """Isolates the status guard: nothing else is wrong with the request."""
    old = seeded(writer)
    memory_writer.edit_front_matter(data / f"projects/pulse/decisions/{old}.md", {"status": status})
    with pytest.raises(InvalidRequestError, match="only an active decision"):
        writer.create(nr(title="Replacement", supersedes=old))
