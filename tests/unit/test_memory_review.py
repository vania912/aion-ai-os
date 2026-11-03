from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from aios.adapters import memory_writer
from aios.adapters.memory_files import parse_record, scan_memory
from aios.errors import InvalidRequestError, MemoryStoreError, UnknownRecordError
from aios.services import memory_review
from aios.services.memory import MemoryService
from aios.services.memory_review import MemoryReviewer
from aios.services.memory_write import MemoryWriter, NewRecord, decision_digest

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


@pytest.fixture
def reviewer(data: Path, writer: MemoryWriter) -> MemoryReviewer:
    return MemoryReviewer(data, writer=writer)


def stored(data: Path, rel: str) -> Any:
    return parse_record((data / rel).read_text(encoding="utf-8"))


def propose(writer: MemoryWriter, **kw: Any) -> str:
    kw.setdefault("evidence", ["commit abc1234"])
    return writer.create(nr(**kw), origin="claude").record.id


def files(data: Path) -> dict[str, bytes]:
    return {
        p.relative_to(data).as_posix(): p.read_bytes() for p in (data / "projects").rglob("*.md")
    }


# -- listing --------------------------------------------------------------------------------


def test_list_pending_shows_only_unreviewed_live_records_newest_first(
    writer: MemoryWriter, reviewer: MemoryReviewer
) -> None:
    writer.create(nr(title="Human decision"))  # reviewed
    first = propose(writer, title="First")
    second = propose(writer, type="task", why=[], title="Second", evidence=[])
    other = propose(writer, project="blueprint", title="Other project")
    ids = [r.id for r in reviewer.list_pending()]
    assert set(ids) == {first, second, other} and ids == sorted(ids, key=lambda i: i, reverse=True)
    assert [r.id for r in reviewer.list_pending(project="blueprint")] == [other]
    assert [r.id for r in reviewer.list_pending(record_type="task")] == [second]
    with pytest.raises(InvalidRequestError, match="type must be"):
        reviewer.list_pending(record_type="bogus")


# -- accepting ------------------------------------------------------------------------------


def test_accepting_a_plain_decision_puts_it_in_force(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    rid = propose(writer, title="Use SQLite")
    before = stored(data, f"projects/pulse/decisions/{rid}.md")
    assert (before.status, before.reviewed, before.accepted_by) == ("proposed", False, None)
    changes = reviewer.accept(rid, accepted_by="Vania")
    assert changes == [f"{rid}: proposed -> active", f"{rid}: reviewed"]
    after = stored(data, f"projects/pulse/decisions/{rid}.md")
    assert (after.status, after.reviewed, after.accepted_by, after.origin) == (
        "active",
        True,
        "Vania",
        "claude",
    )
    assert after.accepted_digest == decision_digest(after)
    assert MemoryService(data).check() == []
    assert [h.id for h in MemoryService(data).list_records(reviewed=False).records] == []


def test_accepting_a_superseding_decision_updates_both_records(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    old = writer.create(nr(title="Plain tables")).record.id
    new = propose(writer, title="FTS5", supersedes=old, reason_for_change="rank")
    assert (
        stored(data, f"projects/pulse/decisions/{old}.md").status == "active"
    )  # untouched until now
    changes = reviewer.accept(new, accepted_by="Vania")
    assert changes == [f"{new}: proposed -> active", f"{old}: active -> superseded (by {new})"]
    chain = MemoryService(data).decision_history(old)
    assert chain.current_id == new and chain.pending_id is None and chain.notes == []
    assert MemoryService(data).check() == []


def test_accepting_a_task_or_note_only_marks_it_reviewed(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    task = propose(writer, type="task", why=[], evidence=[])
    changes = reviewer.accept(task, accepted_by="Vania")
    assert changes == [f"{task}: reviewed (status stays open)"]
    r = stored(data, f"projects/pulse/tasks/{task}.md")
    assert (r.status, r.reviewed, r.accepted_by, r.accepted_digest) == ("open", True, "Vania", None)


def test_accept_is_idempotent_and_touches_nothing_else(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    keep = writer.create(nr(type="note", why=[], title="Unrelated")).record.id
    other = propose(writer, type="note", why=[], evidence=[], title="Other proposal")
    target = propose(writer, title="Target")
    snapshot = files(data)
    reviewer.accept(target, accepted_by="Vania")
    after = files(data)
    changed = {k for k in after if after[k] != snapshot.get(k)}
    assert changed == {f"projects/pulse/decisions/{target}.md"}
    assert reviewer.accept(target, accepted_by="Vania") == []
    assert files(data) == after
    assert keep in " ".join(after) and other in " ".join(after)


def test_accept_completes_a_half_applied_supersession(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = writer.create(nr(title="Plain")).record.id
    new = propose(writer, title="FTS5", supersedes=old)
    real, calls = memory_writer.edit_front_matter, {"n": 0}

    def crash_on_second(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("power cut")
        return real(*a, **k)

    monkeypatch.setattr("aios.services.memory_write.edit_front_matter", crash_on_second)
    with pytest.raises(OSError, match="power cut"):
        reviewer.accept(new, accepted_by="Vania")
    monkeypatch.undo()
    assert stored(data, f"projects/pulse/decisions/{new}.md").reviewed is True  # half applied
    plan = reviewer.plan_accept(new, accepted_by="Vania")
    assert plan.actions == [f"{old}: active -> superseded (by {new})"] and plan.blocked is None
    assert reviewer.accept(new, accepted_by="Vania") == [f"{old}: active -> superseded (by {new})"]
    assert MemoryService(data).check() == []


@pytest.mark.parametrize("name", ["", "   "])
def test_an_accepter_name_is_required(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, name: str
) -> None:
    rid = propose(writer)
    with pytest.raises(InvalidRequestError, match="accepted_by"):
        reviewer.accept(rid, accepted_by=name)
    assert stored(data, f"projects/pulse/decisions/{rid}.md").reviewed is False


def test_unknown_ids_are_refused(reviewer: MemoryReviewer) -> None:
    for call in (
        lambda: reviewer.accept("DEC-0099", accepted_by="V"),
        lambda: reviewer.plan_accept("DEC-0099", accepted_by="V"),
        lambda: reviewer.reject("DEC-0099", "no"),
    ):
        with pytest.raises(UnknownRecordError):
            call()


@pytest.mark.parametrize("status", ["rejected", "superseded", "deprecated"])
def test_a_decision_that_is_not_proposed_or_active_cannot_be_accepted(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, status: str
) -> None:
    rid = propose(writer)
    memory_writer.edit_front_matter(data / f"projects/pulse/decisions/{rid}.md", {"status": status})
    assert reviewer.plan_accept(rid, accepted_by="V").blocked is not None
    with pytest.raises(InvalidRequestError, match="only a proposed or active decision"):
        reviewer.accept(rid, accepted_by="V")
    assert stored(data, f"projects/pulse/decisions/{rid}.md").reviewed is False


@pytest.mark.parametrize("pred_status", ["deprecated", "rejected", "proposed"])
def test_a_stale_predecessor_blocks_acceptance(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, pred_status: str
) -> None:
    old = writer.create(nr(title="Plain")).record.id
    new = propose(writer, title="FTS5", supersedes=old)
    memory_writer.edit_front_matter(
        data / f"projects/pulse/decisions/{old}.md", {"status": pred_status}
    )
    plan = reviewer.plan_accept(new, accepted_by="V")
    assert plan.blocked and "reject this proposal instead" in plan.blocked
    before = files(data)
    with pytest.raises(InvalidRequestError):
        reviewer.accept(new, accepted_by="V")
    assert files(data) == before


# -- the plan shown to the reviewer -----------------------------------------------------------


def test_the_plan_says_exactly_what_accepting_will_do(
    writer: MemoryWriter, reviewer: MemoryReviewer
) -> None:
    old = writer.create(nr(title="Plain tables", why=["no server"])).record.id
    new = propose(writer, title="Plain tables with FTS5", supersedes=old)
    plan = reviewer.plan_accept(new, accepted_by="Vania")
    assert plan.actions == [
        f"{new}: proposed -> active",
        f"{old}: active -> superseded (by {new})",
        f"mark {new} reviewed, accepted by Vania",
    ]
    assert plan.predecessor is not None and plan.predecessor.why == ["no server"]
    assert plan.blocked is None and plan.warnings == []
    assert old in [h.id for h in plan.similar] and new not in [h.id for h in plan.similar]


def test_the_plan_warns_about_missing_evidence_and_dangling_references(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    rid = propose(writer)
    path = data / f"projects/pulse/decisions/{rid}.md"
    text = path.read_text(encoding="utf-8").replace(
        "evidence:\n- commit abc1234\n", "related_tasks:\n- TSK-0099\n"
    )
    path.write_text(text, encoding="utf-8")
    plan = reviewer.plan_accept(rid, accepted_by="V")
    assert "this decision has no evidence" in plan.warnings
    assert "related_tasks: TSK-0099 does not exist" in plan.warnings


def test_the_plan_for_an_accepted_record_is_a_no_op(
    writer: MemoryWriter, reviewer: MemoryReviewer
) -> None:
    rid = propose(writer)
    reviewer.accept(rid, accepted_by="V")
    assert reviewer.plan_accept(rid, accepted_by="V").actions == ["nothing to do: already reviewed"]


# -- rejecting ------------------------------------------------------------------------------


def test_rejecting_archives_the_record_out_of_the_index(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    rid = propose(writer, title="Dubious claim")
    where = reviewer.reject(rid, "no evidence on inspection")
    assert where == f"projects/pulse/_rejected/decisions/{rid}.md"
    assert not (data / f"projects/pulse/decisions/{rid}.md").exists()
    r = stored(data, where)
    assert (r.status, r.reviewed, r.rejection_reason) == (
        "rejected",
        False,
        "no evidence on inspection",
    )
    svc = MemoryService(data)
    assert svc.search("dubious").hits == [] and svc.list_records().records == []
    scan = scan_memory(data)
    assert scan.records == [] and scan.issues == [] and svc.check() == []


def test_a_rejected_id_is_never_reused(writer: MemoryWriter, reviewer: MemoryReviewer) -> None:
    rid = propose(writer)
    reviewer.reject(rid, "no")
    assert writer.create(nr(title="Next")).record.id == "DEC-0002"


def test_rejecting_a_superseding_proposal_leaves_the_predecessor_alone(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    old = writer.create(nr(title="Plain")).record.id
    before = (data / f"projects/pulse/decisions/{old}.md").read_bytes()
    new = propose(writer, title="FTS5", supersedes=old)
    reviewer.reject(new, "not now")
    assert (data / f"projects/pulse/decisions/{old}.md").read_bytes() == before
    assert MemoryService(data).decision_history(old).current_id == old


def test_non_decisions_keep_their_status_when_rejected(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    rid = propose(writer, type="task", why=[], evidence=[])
    where = reviewer.reject(rid, "duplicate of another task")
    r = stored(data, where)
    assert (r.status, r.rejection_reason) == ("open", "duplicate of another task")


def test_reviewed_records_cannot_be_rejected(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    human = writer.create(nr(title="Mine")).record.id
    accepted = propose(writer, title="Accepted")
    reviewer.accept(accepted, accepted_by="V")
    for rid in (human, accepted):
        with pytest.raises(InvalidRequestError, match="only unreviewed proposals"):
            reviewer.reject(rid, "no")
        assert (data / f"projects/pulse/decisions/{rid}.md").exists()


@pytest.mark.parametrize("reason", ["", "   ", "two\nlines", "x" * 301, "bell\x07"])
def test_a_rejection_needs_a_single_line_reason(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, reason: str
) -> None:
    rid = propose(writer)
    before = files(data)
    with pytest.raises(InvalidRequestError, match="single-line reason"):
        reviewer.reject(rid, reason)
    assert files(data) == before


def test_rejecting_twice_is_a_no_op(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    rid = propose(writer)
    first = reviewer.reject(rid, "no")
    snapshot = files(data)
    assert reviewer.reject(rid, "a different reason") == first
    assert files(data) == snapshot and stored(data, first).rejection_reason == "no"


def test_a_crash_between_recording_and_moving_is_visible_and_recoverable(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = propose(writer, title="Half rejected")

    def fail_move(source: Path, target: Path) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(memory_review, "_move", fail_move)
    with pytest.raises(OSError, match="power cut"):
        reviewer.reject(rid, "no good")
    monkeypatch.undo()
    live = data / f"projects/pulse/decisions/{rid}.md"
    assert (
        live.exists()
        and stored(data, f"projects/pulse/decisions/{rid}.md").rejection_reason == "no good"
    )
    assert "rejection_pending_move" in {i.code for i in MemoryService(data).check()}
    where = reviewer.reject(rid, "ignored on re-run")  # finishes the move; keeps the first reason
    assert not live.exists() and stored(data, where).rejection_reason == "no good"
    assert MemoryService(data).check() == []


def test_an_existing_archive_file_is_never_overwritten(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    rid = propose(writer)
    target = data / "projects/pulse/_rejected/decisions" / f"{rid}.md"
    target.parent.mkdir(parents=True)
    target.write_text("precious")
    live = data / f"projects/pulse/decisions/{rid}.md"
    with pytest.raises(MemoryStoreError, match="already exists"):
        reviewer.reject(rid, "no")
    assert target.read_text() == "precious" and live.exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink creation needs privileges on Windows")
def test_a_symlinked_archive_directory_is_refused(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, tmp_path: Path
) -> None:
    rid = propose(writer)
    outside = tmp_path / "outside"
    outside.mkdir()
    (data / "projects/pulse/_rejected").symlink_to(outside)
    with pytest.raises(MemoryStoreError, match="symlinked"):
        reviewer.reject(rid, "no")
    assert list(outside.iterdir()) == []


def test_transient_windows_locks_are_retried_when_moving(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    rid = propose(writer)
    real, calls = os.replace, {"n": 0}

    def flaky(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert reviewer.reject(rid, "no").endswith(f"{rid}.md")


# -- concurrency ----------------------------------------------------------------------------


def test_concurrent_accepts_and_rejects_leave_every_record_consistent(
    writer: MemoryWriter, reviewer: MemoryReviewer, data: Path
) -> None:
    ids = [propose(writer, type="note", why=[], evidence=[], title=f"n{i}") for i in range(8)]
    errors: list[BaseException] = []

    def work(rid: str, accept: bool) -> None:
        try:
            r = MemoryReviewer(data, writer=MemoryWriter(data, today=TODAY))
            r.accept(rid, accepted_by="V") if accept else r.reject(rid, "no")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(rid, i % 2 == 0)) for i, rid in enumerate(ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    svc = MemoryService(data)
    assert (
        len(svc.list_records(reviewed=True).records) == 4
        and svc.list_records(reviewed=False).records == []
    )
    assert len(list((data / "projects/pulse/_rejected/notes").glob("*.md"))) == 4
    assert svc.check() == []
