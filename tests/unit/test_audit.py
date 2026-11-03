from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from aios.adapters.audit_store import JsonlAuditStore, event_hash
from aios.domain.audit import AuditConfig
from aios.errors import (
    AuditError,
    GitError,
    InvalidRequestError,
    PolicyViolation,
)
from aios.policy import OperationPolicy, PermissionLevel
from aios.services.audit import AuditLog, redact, sanitize_args


class Clock:
    def __init__(self, start: str = "2026-11-03T10:00:00+00:00") -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.now

    def set(self, iso: str) -> None:
        self.now = datetime.fromisoformat(iso)


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


@pytest.fixture
def store(audit_dir: Path) -> JsonlAuditStore:
    return JsonlAuditStore(audit_dir)


def draft(i: int = 0, ts: str = "2026-11-03T10:00:00.000Z", **extra: Any) -> dict[str, Any]:
    return {
        "ts": ts, "event": "note", "op": "probe", "call_id": f"{i:016x}", "actor": "test",
        "project": None, "level": None, "verdict": None, "outcome": None, "code": None,
        "args": {"i": i}, "meta": {}, **extra,
    }  # fmt: skip


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def rewrite(path: Path, new_lines: list[str]) -> None:
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# -- store: chain basics ------------------------------------------------------------------


def test_first_event_and_file_layout(store: JsonlAuditStore, audit_dir: Path) -> None:
    event = store.append(draft())
    assert (event.seq, event.prev) == (1, "")
    assert event.hash == event_hash(event.model_dump())
    assert [p.name for p in audit_dir.glob("*.jsonl")] == ["2026-11.jsonl"]
    if os.name == "posix":
        assert (audit_dir / "2026-11.jsonl").stat().st_mode & 0o777 == 0o600


def test_events_are_chained_and_survive_a_restart(audit_dir: Path) -> None:
    first = JsonlAuditStore(audit_dir)
    a, b = first.append(draft(1)), first.append(draft(2))
    assert (b.seq, b.prev) == (2, a.hash)
    c = JsonlAuditStore(audit_dir).append(draft(3))  # a new process / instance
    assert (c.seq, c.prev) == (3, b.hash)
    result = JsonlAuditStore(audit_dir).verify()
    assert result.ok and result.events == 3 and result.last_hash == c.hash


def test_month_rollover_keeps_one_continuous_chain(store: JsonlAuditStore, audit_dir: Path) -> None:
    a = store.append(draft(1, ts="2026-11-30T23:59:59.000Z"))
    b = store.append(draft(2, ts="2026-12-01T00:00:01.000Z"))
    assert sorted(p.name for p in audit_dir.glob("*.jsonl")) == ["2026-11.jsonl", "2026-12.jsonl"]
    assert (b.seq, b.prev) == (2, a.hash)
    assert store.verify().ok


def test_clock_moving_backwards_does_not_break_file_order(
    store: JsonlAuditStore, audit_dir: Path
) -> None:
    store.append(draft(1, ts="2026-12-01T00:00:01.000Z"))
    store.append(draft(2, ts="2026-11-15T00:00:01.000Z"))  # earlier month
    assert [p.name for p in audit_dir.glob("*.jsonl")] == ["2026-12.jsonl"]
    assert store.verify().ok


def test_empty_and_foreign_files(audit_dir: Path) -> None:
    store = JsonlAuditStore(audit_dir)
    assert store.verify().ok and store.anchor() == (0, "")
    audit_dir.mkdir()
    for name in ("notes.txt", "2026-13.jsonl", "2026-1.jsonl", ".lock"):
        (audit_dir / name).write_text("garbage\n")
    assert store.verify().ok and store.append(draft()).seq == 1


def test_oversized_events_are_refused(audit_dir: Path) -> None:
    store = JsonlAuditStore(audit_dir, max_event_bytes=300)
    with pytest.raises(AuditError, match="exceeds"):
        store.append(draft(args={"big": "x" * 1000}))
    assert store.verify().events == 0


def test_unwritable_directory_raises_audit_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(AuditError, match="cannot write"):
        JsonlAuditStore(blocker / "audit").append(draft())


# -- store: torn writes -------------------------------------------------------------------


def test_torn_tail_is_flagged_and_does_not_corrupt_the_next_event(
    store: JsonlAuditStore, audit_dir: Path
) -> None:
    a = store.append(draft(1))
    with (audit_dir / "2026-11.jsonl").open("ab") as f:
        f.write(b'{"seq": 2, "ts": "2026-11-03T10:0')  # crash mid-write, no newline
    b = store.append(draft(3))
    assert (b.seq, b.prev) == (2, a.hash)  # chains to the last *valid* event
    result = store.verify()
    assert not result.ok and [p.code for p in result.problems] == ["unparseable"]
    assert result.events == 2 and result.last_seq == 2


# -- store: tamper detection --------------------------------------------------------------


def _three(store: JsonlAuditStore, audit_dir: Path) -> tuple[Path, list[str]]:
    for i in (1, 2, 3):
        store.append(draft(i))
    path = audit_dir / "2026-11.jsonl"
    return path, lines(path)


def test_edited_event_is_detected(store: JsonlAuditStore, audit_dir: Path) -> None:
    path, rows = _three(store, audit_dir)
    rows[1] = rows[1].replace('"i":2', '"i":99')
    rewrite(path, rows)
    problems = store.verify().problems
    assert [(p.code, p.line) for p in problems] == [("bad_hash", 2)]


def test_deleted_event_is_detected(store: JsonlAuditStore, audit_dir: Path) -> None:
    path, rows = _three(store, audit_dir)
    rewrite(path, [rows[0], rows[2]])
    codes = {p.code for p in store.verify().problems}
    assert codes == {"broken_chain", "bad_sequence"}


def test_reordered_events_are_detected(store: JsonlAuditStore, audit_dir: Path) -> None:
    path, rows = _three(store, audit_dir)
    rewrite(path, [rows[0], rows[2], rows[1]])
    assert not store.verify().ok


def test_duplicated_event_is_detected(store: JsonlAuditStore, audit_dir: Path) -> None:
    path, rows = _three(store, audit_dir)
    rewrite(path, [rows[0], rows[1], rows[1], rows[2]])
    assert not store.verify().ok


def test_deleting_a_whole_month_file_is_detected(store: JsonlAuditStore, audit_dir: Path) -> None:
    store.append(draft(1, ts="2026-10-31T23:00:00.000Z"))
    store.append(draft(2, ts="2026-11-01T00:00:01.000Z"))
    store.append(draft(3, ts="2026-12-01T00:00:01.000Z"))
    (audit_dir / "2026-11.jsonl").unlink()
    assert {p.code for p in store.verify().problems} >= {"broken_chain"}


def test_known_limit_truncating_the_end_is_not_detected_but_the_anchor_changes(
    store: JsonlAuditStore, audit_dir: Path
) -> None:
    path, rows = _three(store, audit_dir)
    anchor = store.anchor()
    rewrite(path, rows[:2])
    assert store.verify().ok  # documented limitation: the log alone cannot show a cut tail
    assert store.anchor() != anchor  # ...but a previously recorded anchor does


def test_problem_list_is_capped(store: JsonlAuditStore, audit_dir: Path) -> None:
    for i in range(10):
        store.append(draft(i))
    path = audit_dir / "2026-11.jsonl"
    rewrite(path, ["garbage"] * 30)
    result = store.verify(max_problems=5)
    assert len(result.problems) == 5 and result.more_problems


# -- store: concurrency -------------------------------------------------------------------


def test_threads_appending_concurrently_keep_the_chain_intact(audit_dir: Path) -> None:
    store = JsonlAuditStore(audit_dir)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(10):
                store.append(draft(n * 100 + i))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    result = store.verify()
    assert result.ok and result.events == 80 and result.last_seq == 80


def test_separate_processes_appending_concurrently_keep_the_chain_intact(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    code = (
        "import sys\nfrom pathlib import Path\nfrom aios.services.audit import AuditLog\n"
        "a = AuditLog(Path(sys.argv[1]), actor='proc')\n"
        "for i in range(10):\n    a.note('probe', args={'i': i})\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(data)], stderr=subprocess.PIPE)
        for _ in range(3)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    result = AuditLog(data, actor="cli").verify()
    assert result.ok and result.events == 30 and result.last_seq == 30


# -- sanitising and redaction -------------------------------------------------------------

CFG = AuditConfig()


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "must_contain"),
    [
        ("password=hunter2", "hunter2", "password=[redacted]"),
        ("PASSWORD: 'my secret value'", "my secret value", "[redacted]"),
        ("token = abc123def", "abc123def", "token = [redacted]"),
        ("api_key=sk-live-123456", "sk-live-123456", "[redacted]"),
        ("Authorization: Bearer abcdefgh12345678", "abcdefgh12345678", "Bearer [redacted]"),
        ("key AKIAABCDEFGHIJKLMNOP here", "AKIAABCDEFGHIJKLMNOP", "[redacted-aws-key]"),
        ("ghp_abcdefghijklmnopqrstuvwxyz123456", "ghp_abcdefghijkl", "[redacted-github-token]"),
        ("xoxb-1234567890-abcdefghij", "xoxb-1234567890", "[redacted-slack-token]"),
        ("-----BEGIN RSA PRIVATE KEY-----", "RSA PRIVATE KEY", "[redacted-private-key]"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk",
            "eyJhbGci",
            "[redacted-jwt]",
        ),
        ("x" * 60, "x" * 60, "[redacted-long-token]"),
    ],
)
def test_credentials_are_redacted(raw: str, must_not_contain: str, must_contain: str) -> None:
    out = redact(raw)
    assert must_not_contain not in out and must_contain in out


@pytest.mark.parametrize(
    "benign",
    ["token refresh logic", "why did we separate the workspace", "src/app.py",
     "9f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d", "DEC-0042", "password reset flow"],
)  # fmt: skip
def test_ordinary_text_is_left_alone(benign: str) -> None:
    assert redact(benign) == benign  # includes a 40-char git SHA and prose containing 'token'


def test_sanitize_args_bounds_and_types() -> None:
    out = sanitize_args(
        {
            "path": "src/app.py", "n": 3, "flag": True, "none": None, "ratio": 0.5,
            "long": "y " * 500, "multi": "a\nb\tc\x00d", "items": [1, 2, 3], "mapping": {"a": 1},
            "obj": object(), "bad key!": "dropped", "": "dropped",
        },
        CFG,
    )  # fmt: skip
    assert out["path"] == "src/app.py" and out["n"] == 3 and out["flag"] is True
    assert out["none"] is None and out["ratio"] == 0.5
    assert isinstance(out["long"], str) and len(out["long"]) == CFG.max_arg_chars + 1
    assert out["multi"] == "a b c d"
    assert (
        out["items"] == "<list of 3>"
        and out["mapping"] == "<dict of 1>"
        and out["obj"] == "<object>"
    )
    assert "bad key!" not in out and "" not in out


def test_sanitize_args_caps_the_number_of_keys() -> None:
    out = sanitize_args({f"k{i}": i for i in range(30)}, CFG)
    assert len(out) == CFG.max_args + 1 and out["_more_args"] is True
    assert sanitize_args(None, CFG) == {}


# -- AuditLog.run -------------------------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def audit(tmp_path: Path, clock: Clock) -> AuditLog:
    return AuditLog(tmp_path / "data", actor="mcp", clock=clock)


@pytest.fixture
def policy() -> OperationPolicy:
    p = OperationPolicy()
    p.register("read_project_file", PermissionLevel.READ_ONLY)
    p.register("propose_decision", PermissionLevel.PROPOSE)
    return p


def test_successful_call_records_request_then_result(
    audit: AuditLog, policy: OperationPolicy
) -> None:
    result = audit.run(
        "read_project_file", lambda: {"lines": 3}, policy=policy, project="pulse",
        args={"path": "src/app.py"}, summarize=lambda r: {"lines": r["lines"]},
    )  # fmt: skip
    assert result == {"lines": 3}
    request, outcome = audit.get_call(audit.list_events(limit=1)[0].call_id)
    assert (request.event, outcome.event) == ("request", "result")
    assert request.call_id == outcome.call_id and outcome.seq == request.seq + 1
    assert (request.verdict, request.level, request.project, request.actor) == (
        "allow",
        "read_only",
        "pulse",
        "mcp",
    )
    assert request.args == {"path": "src/app.py"}
    assert outcome.outcome == "ok" and outcome.code is None
    assert outcome.meta["lines"] == 3 and "duration_ms" in outcome.meta
    assert audit.verify().ok


@pytest.mark.parametrize("operation", ["unknown_operation", "propose_decision"])
def test_denied_operations_are_logged_and_never_executed(
    audit: AuditLog, policy: OperationPolicy, operation: str
) -> None:
    calls: list[int] = []
    with pytest.raises(PolicyViolation) as info:
        audit.run(operation, lambda: calls.append(1), policy=policy, args={"x": 1})
    assert info.value.code == "operation_denied" and calls == []
    request, outcome = audit.get_call(audit.list_events(limit=1)[0].call_id)
    assert request.verdict == "deny" and (outcome.outcome, outcome.code) == (
        "denied",
        "operation_denied",
    )


def test_enabled_level_is_allowed(tmp_path: Path, clock: Clock) -> None:
    policy = OperationPolicy(frozenset({PermissionLevel.READ_ONLY, PermissionLevel.PROPOSE}))
    policy.register("propose_decision", PermissionLevel.PROPOSE)
    audit = AuditLog(tmp_path / "d", actor="mcp", clock=clock)
    assert audit.run("propose_decision", lambda: "ok", policy=policy) == "ok"


@pytest.mark.parametrize(
    ("exc", "outcome", "code"),
    [
        (PolicyViolation("denied_name", "file is not accessible"), "denied", "denied_name"),
        (PolicyViolation("path_escape", "x"), "denied", "path_escape"),
        (InvalidRequestError("bad limit"), "error", "invalid_request"),
        (GitError("git failed"), "error", "GitError"),
        (RuntimeError("boom /etc/shadow"), "error", "internal_error"),
    ],
)
def test_failures_are_classified_and_reraised(
    audit: AuditLog,
    policy: OperationPolicy,
    exc: Exception,
    outcome: str,
    code: str,
    tmp_path: Path,
) -> None:
    def fail() -> None:
        raise exc

    with pytest.raises(type(exc)):
        audit.run("read_project_file", fail, policy=policy, args={"path": "x"})
    last = audit.list_events(limit=1)[0]
    assert (last.event, last.outcome, last.code) == ("result", outcome, code)
    raw = "".join(p.read_text() for p in (tmp_path / "data" / "audit").glob("*.jsonl"))
    assert "shadow" not in raw and "git failed" not in raw  # exception messages are never logged


def test_request_failure_fails_closed(
    tmp_path: Path, clock: Clock, policy: OperationPolicy
) -> None:
    class Broken(JsonlAuditStore):
        def append(self, fields: Any) -> Any:
            raise AuditError("disk full")

    audit = AuditLog(tmp_path, actor="mcp", clock=clock, store=Broken(tmp_path / "audit"))
    calls: list[int] = []
    with pytest.raises(AuditError, match="disk full"):
        audit.run("read_project_file", lambda: calls.append(1), policy=policy)
    assert calls == []


def test_result_failure_returns_the_result_and_logs_a_warning(
    tmp_path: Path, clock: Clock, policy: OperationPolicy, caplog: pytest.LogCaptureFixture
) -> None:
    class FailsSecondWrite(JsonlAuditStore):
        writes = 0

        def append(self, fields: Any) -> Any:
            FailsSecondWrite.writes += 1
            if FailsSecondWrite.writes == 2:
                raise AuditError("disk full")
            return super().append(fields)

    audit = AuditLog(tmp_path, actor="mcp", clock=clock, store=FailsSecondWrite(tmp_path / "audit"))
    with caplog.at_level(logging.ERROR, logger="aios.audit"):
        assert audit.run("read_project_file", lambda: 42, policy=policy) == 42
    assert "could not record the result" in caplog.text


def test_broken_summariser_does_not_break_the_operation(
    audit: AuditLog, policy: OperationPolicy
) -> None:
    def bad(_: object) -> dict[str, int]:
        raise KeyError("nope")

    assert audit.run("read_project_file", lambda: 1, policy=policy, summarize=bad) == 1
    assert audit.list_events(limit=1)[0].meta["summary_error"] is True


def test_secrets_and_content_do_not_reach_the_log(
    audit: AuditLog, policy: OperationPolicy, tmp_path: Path
) -> None:
    secret_file_body = "TOP-SECRET-FILE-CONTENT " * 200
    audit.run(
        "read_project_file", lambda: secret_file_body, policy=policy,
        args={"path": "a.py", "query": "password=hunter2", "content": secret_file_body},
        summarize=lambda r: {"chars": len(r)},
    )  # fmt: skip
    raw = "".join(p.read_text() for p in (tmp_path / "data" / "audit").glob("*.jsonl"))
    assert "hunter2" not in raw
    assert secret_file_body not in raw and raw.count("TOP-SECRET") <= 10  # only a truncated prefix
    assert '"chars":4800' in raw.replace(" ", "")


def test_unicode_and_newlines_stay_on_one_line(
    audit: AuditLog, policy: OperationPolicy, tmp_path: Path
) -> None:
    audit.run("read_project_file", lambda: 1, policy=policy, args={"q": 'héllo\n世界\r\n{"a": 1}'})
    path = next((tmp_path / "data" / "audit").glob("*.jsonl"))
    rows = lines(path)
    assert len(rows) == 2 and all(json.loads(r) for r in rows)
    assert "héllo" in rows[0] and "世界" in rows[0]


def test_invalid_names_are_rejected(
    audit: AuditLog, policy: OperationPolicy, tmp_path: Path
) -> None:
    for bad in ("", "Bad Name", "a" * 100, "../x", "UPPER"):
        with pytest.raises(InvalidRequestError):
            audit.run(bad, lambda: 1, policy=policy)
    with pytest.raises(ValueError):
        AuditLog(tmp_path, actor="Bad Actor")
    assert audit.verify().events == 0


def test_note_events(audit: AuditLog) -> None:
    event = audit.note("manual_action", project="pulse", args={"why": "test"})
    assert (event.event, event.op, event.project) == ("note", "manual_action", "pulse")


# -- reading ------------------------------------------------------------------------------


def test_list_filters_order_and_limits(
    audit: AuditLog, policy: OperationPolicy, clock: Clock
) -> None:
    for i, project in enumerate(["pulse", "blueprint", "pulse"]):
        clock.set(f"2026-11-0{i + 1}T10:00:00+00:00")
        audit.run("read_project_file", lambda: 1, policy=policy, project=project)
    with pytest.raises(PolicyViolation):
        audit.run("nope", lambda: 1, policy=policy, project="pulse")

    everything = audit.list_events(limit=100)
    assert [e.seq for e in everything] == sorted((e.seq for e in everything), reverse=True)
    assert len(everything) == 8
    assert len(audit.list_events(limit=3)) == 3
    assert {e.project for e in audit.list_events(project="blueprint")} == {"blueprint"}
    assert {e.outcome for e in audit.list_events(outcome="denied")} == {"denied"}
    assert {e.op for e in audit.list_events(op="nope")} == {"nope"}
    assert len(audit.list_events(since="2026-11-02", limit=100)) == 6  # events on the 2nd and 3rd
    assert audit.list_events(since="2027-01-01") == []
    assert audit.get_call("0" * 16) == []


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 501}, {"op": "Bad Op"}, {"project": "Bad Project"},
     {"outcome": "weird"}, {"call_id": "xyz"}, {"since": "yesterday-ish"}],
)  # fmt: skip
def test_list_validates_filters(audit: AuditLog, kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequestError):
        audit.list_events(**kwargs)


def test_service_verify_and_anchor(audit: AuditLog, policy: OperationPolicy) -> None:
    assert audit.anchor() == (0, "")
    audit.run("read_project_file", lambda: 1, policy=policy)
    seq, digest = audit.anchor()
    assert seq == 2 and len(digest) == 64
    assert audit.verify().last_hash == digest


# -- lock behaviour -----------------------------------------------------------------------


def test_a_held_lock_makes_appends_time_out_with_a_clear_error(audit_dir: Path) -> None:
    from aios.adapters.audit_store import _exclusive_lock

    store = JsonlAuditStore(audit_dir, lock_timeout_s=0.3)
    store.append(draft(1))
    failures: list[BaseException] = []

    def contender() -> None:
        try:
            store.append(draft(2))
        except BaseException as exc:
            failures.append(exc)

    with _exclusive_lock(audit_dir / ".lock", 5.0):  # another writer holds the lock
        thread = threading.Thread(target=contender)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=10)
        waited = time.monotonic() - started
    assert len(failures) == 1 and isinstance(failures[0], AuditError)
    assert "timed out" in str(failures[0]) and "audit lock" in str(failures[0])
    assert 0.25 <= waited < 5
    assert store.append(draft(3)).seq == 2  # the lock was released cleanly; nothing was lost
    assert store.verify().ok


def test_many_writers_survive_a_slow_disk(audit_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writers queue behind a slow fsync for longer than a single wait; none may fail."""
    real_fsync = os.fsync

    def slow_fsync(fd: int) -> None:
        time.sleep(0.03)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    store = JsonlAuditStore(audit_dir, lock_timeout_s=60)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(4):
                store.append(draft(n * 10 + i))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    result = store.verify()
    assert result.ok and result.events == 80


def test_a_lock_held_by_another_process_times_out_then_recovers(audit_dir: Path) -> None:
    """The OS-level lock path: a different process holds the audit lock."""
    store = JsonlAuditStore(audit_dir, lock_timeout_s=0.4)
    store.append(draft(1))
    holder_code = (
        "import sys, time\nfrom pathlib import Path\n"
        "from aios.adapters.audit_store import _exclusive_lock\n"
        "with _exclusive_lock(Path(sys.argv[1]), 10):\n"
        "    print('locked', flush=True)\n    time.sleep(float(sys.argv[2]))\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(audit_dir / ".lock"), "2.0"],
        stdout=subprocess.PIPE,
    )
    assert holder.stdout is not None
    try:
        assert holder.stdout.readline().strip() == b"locked"
        started = time.monotonic()
        with pytest.raises(AuditError, match="timed out"):
            store.append(draft(2))
        assert 0.3 <= time.monotonic() - started < 1.9  # gave up on our own timeout, not theirs
    finally:
        holder.wait(timeout=30)
    assert store.append(draft(3)).seq == 2  # after the holder exits, writing works again
    assert store.verify().ok
