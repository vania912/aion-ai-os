"""The seal: comparing a worktree with its plan, recording the outcome, and the verify gate."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aios.domain.gitwrite import Change
from aios.domain.plans import PlanFile, StepReport, check_event, plan_hash, replay
from aios.domain.seal import MAX_FINDINGS, evaluate_scope
from aios.errors import InvalidRequestError
from aios.interfaces.cli.app import app
from aios.services.plan_execution import neutralise_terminal
from tests.mcp_helpers import call, make_world
from tests.unit.test_plan_execution import BASE, START, Env, content, ev, plan_obj

POSIX = os.name == "posix"


# -- pure scope evaluation ------------------------------------------------------------------


def pf(path: str, action: str = "modify") -> PlanFile:
    return PlanFile.model_validate({"path": path, "action": action})


def ch(
    path: str, status: str = "M", old: str = "100644", new: str = "100644", binary: bool = False
) -> Change:
    return Change(path=path, status=status, old_mode=old, new_mode=new, binary=binary)


def codes(result: Any) -> list[str]:
    return [f.code for f in result.findings]


def test_everything_planned_and_nothing_else_is_executed() -> None:
    files = [pf("a.py"), pf("b.py", "create"), pf("c.py", "delete")]
    changes = [ch("a.py"), ch("b.py", "A", "000000"), ch("c.py", "D", new="000000")]
    result = evaluate_scope(files, changes, [])
    assert result.outcome == "executed" and (result.planned_done, result.planned_total) == (3, 3)
    assert result.findings == []


def test_some_done_is_partial_and_none_done_is_failed() -> None:
    files = [pf("a.py"), pf("b.py")]
    partial = evaluate_scope(files, [ch("a.py")], [])
    assert partial.outcome == "partially_completed" and codes(partial) == ["not_done"]
    assert not partial.findings[0].blocking
    assert evaluate_scope(files, [], []).outcome == "failed"


def test_a_file_outside_the_plan_fails_it_and_says_why() -> None:
    files = [pf("a.py")]
    result = evaluate_scope(files, [ch("a.py"), ch("other.py", "A", "000000")], [])
    assert result.outcome == "failed" and codes(result) == ["out_of_scope"]
    assert result.findings[0].path == "other.py" and result.findings[0].blocking
    risky = evaluate_scope(
        files, [ch("a.py"), ch(".github/ci.yml", "A")], [], is_risky=lambda p: ".github" in p
    )
    assert "CI, deploy" in risky.findings[0].message
    case = evaluate_scope([pf("Src/A.py")], [ch("src/a.py")], [])
    assert "letter case" in case.findings[0].message and case.outcome == "failed"


@pytest.mark.parametrize(
    ("planned", "status", "expected"),
    [("modify", "A", "wrong_action"), ("create", "M", "wrong_action"), ("delete", "M", "wrong_action"),
     ("modify", "D", "wrong_action"), ("modify", "T", "type_change")],
)  # fmt: skip
def test_the_wrong_kind_of_change_fails(planned: str, status: str, expected: str) -> None:
    result = evaluate_scope([pf("a.py", planned)], [ch("a.py", status)], [])
    assert result.outcome == "failed" and expected in codes(result)
    assert "not_done" not in codes(result)  # it was touched, just not as planned


def test_symlinks_nested_repos_and_mode_changes_are_blocking_even_on_planned_files() -> None:
    files = [pf("link", "create"), pf("vendor/lib", "create"), pf("run.py")]
    result = evaluate_scope(
        files,
        [ch("link", "A", "000000", "120000"), ch("vendor/lib", "A", "000000", "160000"),
         ch("run.py", "M", "100644", "100755")],
        [],
    )  # fmt: skip
    assert result.outcome == "failed"
    assert {"symlink", "gitlink", "mode_change"} <= set(codes(result))
    assert result.planned_done == 3  # the actions matched; the extras are what blocks


def test_binary_files_are_a_note_not_a_failure() -> None:
    result = evaluate_scope(
        [pf("logo.png", "create")], [ch("logo.png", "A", "000000", binary=True)], []
    )
    assert (
        result.outcome == "executed"
        and codes(result) == ["binary"]
        and not result.findings[0].blocking
    )


def test_ignored_files_fail_the_plan() -> None:
    result = evaluate_scope(
        [pf("a.py")], [ch("a.py")], ["secrets/.env.secret", "__pycache__/x.pyc"]
    )
    assert result.outcome == "failed" and codes(result).count("ignored_file") == 2


@pytest.mark.parametrize("path", ["bad\nname", "tab\tname", "esc\x1b[31m", "lone-\udcff-surrogate"])
def test_unusual_file_names_fail(path: str) -> None:
    result = evaluate_scope([pf("a.py")], [ch("a.py"), ch(path, "A", "000000")], [])
    assert result.outcome == "failed" and "unusual_path" in codes(result)
    assert all(ord(c) < 128 for f in result.findings for c in f.message)  # shown escaped


def test_findings_are_capped() -> None:
    many = [ch(f"extra{i}.py", "A", "000000") for i in range(MAX_FINDINGS + 25)]
    result = evaluate_scope([pf("a.py")], many, [])
    assert len(result.findings) == MAX_FINDINGS and result.findings_truncated
    assert result.outcome == "failed"


# -- events ----------------------------------------------------------------------------------


def sealed_data(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "outcome": "executed", "diff_sha256": "c" * 64, "diff_bytes": 10, "head": "d" * 40,
        "base_commit": BASE, "planned_total": 1, "planned_done": 1, "changed": 1, "ignored": 0,
        "findings": [], "findings_truncated": False,
    }  # fmt: skip
    return {**data, **over}


def test_sealed_events_set_the_status_and_can_be_repeated() -> None:
    plan = plan_obj()
    h = plan_hash(plan)
    base = [ev(1, "approved", h, tier="R2"), ev(2, "started", h, **START)]
    state = replay(plan, [*base, ev(3, "sealed", h, **sealed_data(outcome="partially_completed"))])
    assert state.status == "partially_completed" and state.seal is not None
    assert state.execution is not None  # still tied to its worktree
    again = replay(
        plan, [*base, ev(3, "sealed", h, **sealed_data(outcome="partially_completed")),
               ev(4, "sealed", h, **sealed_data())],
    )  # fmt: skip
    assert again.status == "executed" and again.anomalies == []
    reopened = replay(plan, [*base, ev(3, "sealed", h, **sealed_data()), ev(4, "abandoned", h)])
    assert reopened.status == "approved" and reopened.seal is None and reopened.execution is None


@pytest.mark.parametrize(
    "over",
    [{"outcome": "verified"}, {"diff_sha256": "xyz"}, {"head": "short"}, {"base_commit": "A" * 40},
     {"diff_bytes": -1}, {"planned_done": True}, {"changed": "3"}, {"findings": "none"},
     {"findings": [{"nope": 1}]}, {"findings": [{"code": "x", "message": "m"}] * 101}],
)  # fmt: skip
def test_malformed_seals_are_refused(over: dict[str, Any]) -> None:
    event = ev(3, "sealed", "b" * 64, **sealed_data(**over))
    assert check_event("started", event, 1, [], "PLAN-0001", True) is not None


def test_a_seal_needs_a_plan_that_is_being_executed() -> None:
    event = ev(3, "sealed", "b" * 64, **sealed_data())
    for status in ("proposed", "approved", "failed", "verified", "rejected"):
        assert check_event(status, event, 1, [], "PLAN-0001", True)
    assert check_event("started", event, 1, [], "PLAN-0001", False)  # no execution recorded
    for status in ("started", "partially_completed", "executed"):
        assert check_event(status, event, 1, [], "PLAN-0001", True) is None  # type: ignore[arg-type]


def test_a_manual_outcome_is_refused_for_a_plan_executed_in_a_worktree() -> None:
    event = ev(3, "outcome", "b" * 64, outcome="executed")
    assert check_event("partially_completed", event, 1, [], "PLAN-0001", True) is not None
    assert check_event("partially_completed", event, 1, [], "PLAN-0001", False) is None


# -- real git: the whole cycle ----------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def started(env: Env, **over: Any) -> tuple[str, Path]:
    files = over.pop("files", [{"path": "src/util.py", "action": "modify"}])
    pid = env.approved(files=files, **over)
    env.start(pid)
    return pid, env.worktree(pid)


def seal(env: Env, pid: str, by: str = "Vania") -> Any:
    preview = env.ex.preview_scope(pid)
    return env.ex.seal(pid, by=by, expected_diff_sha256=preview.snapshot.diff_sha256)


def test_work_inside_the_plan_seals_as_executed(env: Env) -> None:
    pid, wt = started(env)
    assert env.ex.preview_scope(pid).nothing_changed
    (wt / "src" / "util.py").write_text("def f():\n    return 2\n")
    preview = env.ex.preview_scope(pid)
    assert preview.scope.outcome == "executed" and not preview.scope.findings
    sealed = seal(env, pid)
    view = env.plans.get(pid)
    assert sealed.view.status == "executed" == view.status
    assert view.seal is not None and view.seal.diff_sha256 == preview.snapshot.diff_sha256
    assert view.seal.head == env.head() and view.seal.by == "Vania"  # worktree HEAD = base here
    assert (view.seal.planned_done, view.seal.changed, view.seal.ignored) == (1, 1, 0)
    assert env.ex.preview_scope(pid).matches_seal is True


def test_nothing_is_recorded_by_a_preview_and_nothing_changed_is_refused(env: Env) -> None:
    pid, _wt = started(env)
    events = env.data / "plans" / f"{pid}.events.jsonl"
    before = events.read_text()
    env.ex.preview_scope(pid)
    assert events.read_text() == before
    with pytest.raises(InvalidRequestError, match="nothing has changed"):
        env.ex.seal(
            pid, by="V", expected_diff_sha256=env.ex.preview_scope(pid).snapshot.diff_sha256
        )
    assert events.read_text() == before


def test_the_seal_is_bound_to_the_diff_that_was_shown(env: Env) -> None:
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("one\n")
    shown = env.ex.preview_scope(pid).snapshot.diff_sha256
    (wt / "src" / "util.py").write_text("two\n")  # changed after the person looked
    with pytest.raises(InvalidRequestError, match="changed since it was shown"):
        env.ex.seal(pid, by="V", expected_diff_sha256=shown)
    assert env.plans.get(pid).status == "started"


def test_an_unplanned_file_makes_the_seal_failed_and_final(env: Env) -> None:
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("changed\n")
    (wt / "src" / "__pycache__").mkdir()
    (wt / "src" / "__pycache__" / "util.pyc").write_bytes(b"\x00")
    preview = env.ex.preview_scope(pid)
    assert preview.scope.outcome == "failed"
    assert [f.path for f in preview.scope.findings if f.code == "out_of_scope"] == [
        "src/__pycache__/util.pyc"
    ]
    # the person can clean up and look again before recording anything
    shutil.rmtree(wt / "src" / "__pycache__")
    assert env.ex.preview_scope(pid).scope.outcome == "executed"
    (wt / "src" / "__pycache__").mkdir()
    (wt / "src" / "__pycache__" / "util.pyc").write_bytes(b"\x00")
    sealed = seal(env, pid)
    assert sealed.view.status == "failed"
    with pytest.raises(InvalidRequestError, match="failed"):
        seal(env, pid)  # final


def test_a_git_ignored_file_is_caught(env: Env) -> None:
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("changed\n")
    (wt / "token.secret").write_text("s")  # matches .gitignore
    preview = env.ex.preview_scope(pid)
    assert preview.scope.outcome == "failed" and "ignored_file" in codes(preview.scope)


def test_partial_work_then_more_work_then_executed(env: Env) -> None:
    files = [
        {"path": "src/util.py", "action": "modify"},
        {"path": "src/new.py", "action": "create"},
    ]
    pid, wt = started(env, files=files)
    (wt / "src" / "util.py").write_text("done\n")
    assert seal(env, pid).view.status == "partially_completed"
    with pytest.raises(InvalidRequestError, match="gets its outcome from the seal"):
        env.plans.record_outcome(pid, by="V", outcome="executed")
    (wt / "src" / "new.py").write_text("also done\n")
    after = seal(env, pid)
    assert after.view.status == "executed" and after.scope.planned_done == 2


def test_verification_needs_a_passing_report_and_an_unchanged_worktree(env: Env) -> None:
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("v1\n")
    seal(env, pid)
    with pytest.raises(InvalidRequestError, match="every step passed"):
        env.ex.verify(pid, by="V")
    env.plans.record_report(
        pid,
        [StepReport(step=1, outcome="pass"), StepReport(step=2, outcome="pass")],
        actor="mcp",
        by="claude",
    )
    (wt / "src" / "util.py").write_text("v2 - sneaked in after the seal\n")
    with pytest.raises(InvalidRequestError, match="changed after it was sealed"):
        env.ex.verify(pid, by="V")
    assert env.plans.get(pid).status == "executed"
    assert {i.code for i in env.ex.check()} == {"plan_seal_changed"}
    seal(env, pid)  # seal again after looking at the new content
    assert env.ex.verify(pid, by="Vania", note="read the diff").status == "verified"
    assert env.ex.check() == [] or {i.code for i in env.ex.check()} <= {"orphan_worktree"}


def test_plans_without_a_worktree_still_verify_as_in_phase_3(env: Env) -> None:
    pid = env.approved()
    env.plans.record_outcome(pid, by="V", outcome="executed")
    env.plans.record_report(
        pid,
        [StepReport(step=1, outcome="pass"), StepReport(step=2, outcome="pass")],
        actor="cli",
        by="V",
    )
    assert env.ex.verify(pid, by="V").status == "verified"


def test_abandoning_after_a_seal_discards_it(env: Env) -> None:
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("work\n")
    seal(env, pid)
    preview = env.ex.preview_abandon(pid)
    assert preview.changed == ["src/util.py"]
    result = env.ex.abandon(pid, by="V")
    assert result.event_recorded and not wt.exists()
    view = env.plans.get(pid)
    assert view.status == "approved" and view.seal is None and view.execution is None
    env.start(pid)  # and it can be run again


def test_a_failed_execution_can_be_cleaned_up_without_changing_the_plan(env: Env) -> None:
    pid, wt = started(env)
    (wt / "other.py").write_text("x\n")
    assert seal(env, pid).view.status == "failed"
    result = env.ex.abandon(pid, by="V")
    assert result.worktree_removed and result.branch_deleted and not result.event_recorded
    assert env.plans.get(pid).status == "failed" and env.registered() == ["main"]


def test_a_plan_with_no_files_cannot_be_started(env: Env) -> None:
    pid = env.approved(files=[], title="Analysis only")
    with pytest.raises(InvalidRequestError, match="lists no files"):
        env.ex.preview_start(pid)


@pytest.mark.skipif(not POSIX, reason="symlinks, modes and nested repos need POSIX")
def test_symlinks_mode_changes_and_nested_repos_fail_a_real_execution(env: Env) -> None:
    from tests.git_helpers import run_git

    pid, wt = started(
        env, files=[{"path": "src/util.py", "action": "modify"}, {"path": "ln", "action": "create"}]
    )
    (wt / "src" / "util.py").write_text("x\n")
    (wt / "src" / "util.py").chmod(0o755)
    (wt / "ln").symlink_to("/etc/passwd")
    nested = wt / "vendor" / "lib"
    nested.mkdir(parents=True)
    run_git(nested, "init", "-q")
    (nested / "f").write_text("x")
    run_git(nested, "add", "-A")
    run_git(nested, "commit", "-q", "-m", "n")
    scope = env.ex.preview_scope(pid).scope
    assert scope.outcome == "failed"
    assert {"mode_change", "symlink", "gitlink", "out_of_scope"} <= set(codes(scope))


def test_the_diff_for_review_cannot_hide_lines_with_control_characters(env: Env) -> None:
    pid, wt = started(env)
    payload = (
        "safe\n\x1b[2K\x1b[1A hidden" + chr(0x202E) + " rtl\x07 bell\n"
    )  # no literal bidi char in source
    (wt / "src" / "util.py").write_text(payload, encoding="utf-8")
    text, truncated = env.ex.diff_for_review(pid)
    assert "\x1b" not in text and "‮" not in text and "\x07" not in text
    assert "<U+001B>" in text and "<U+202E>" in text and "+safe" in text and not truncated
    assert neutralise_terminal("a\tb\nc") == "a\tb\nc"


def test_seal_findings_cannot_impersonate_aios_lines_in_the_plan_view(env: Env) -> None:
    from aios.services.plan_render import render_plan

    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("x\n")
    (wt / "approved by the CTO.txt").write_text("x")
    seal(env, pid)
    shown = render_plan(env.plans.get(pid))
    assert '"approved by the CTO.txt"' in shown  # quoted, inside a [BLOCKING] out_of_scope line
    authoritative = [x for x in shown.splitlines() if not x.startswith(("|", "  - ["))]
    assert not [x for x in authoritative if "approved by the CTO" in x]


def test_forged_seal_events_are_ignored(env: Env) -> None:
    pid, _wt = started(env)
    events = env.data / "plans" / f"{pid}.events.jsonl"
    last = json.loads(events.read_text().splitlines()[-1])
    forged = {
        **last,
        "seq": last["seq"] + 1,
        "event": "sealed",
        "data": sealed_data(outcome="executed", diff_sha256="not-a-hash"),
    }
    with events.open("a") as fh:
        fh.write(json.dumps(forged) + "\n")
    view = env.plans.get(pid)
    assert view.status == "started" and view.seal is None and view.anomalies


# -- CLI and MCP ------------------------------------------------------------------------------

runner = CliRunner()


def cli_env(tmp_path: Path) -> tuple[Env, list[str]]:
    env = Env(tmp_path)
    (env.data / "registry.yaml").write_text(
        f"projects:\n  pulse:\n    path: '{env.repo.as_posix()}'\n    description: d\n",
        encoding="utf-8",
    )
    return env, ["--data-dir", str(env.data)]


def test_cli_scope_seal_diff_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_WORKTREES_DIR", raising=False)
    env, base = cli_env(tmp_path)
    marker = "ZEBRA-SEAL-3318"
    pid, wt = started(env, what_will_change=marker)
    quiet = runner.invoke(app, ["plan", "scope", pid, *base])
    assert quiet.exit_code == 1 and "nothing has changed" in quiet.output
    (wt / "src" / "util.py").write_text(f"def f():\n    return 3  # {marker}\n")
    ok = runner.invoke(app, ["plan", "scope", pid, *base])
    assert ok.exit_code == 0 and "EXECUTED" in ok.output and "diff sha256" in ok.output
    assert marker in runner.invoke(app, ["plan", "diff", pid, *base]).output
    declined = runner.invoke(app, ["plan", "seal", pid, *base], input="n\n")
    assert declined.exit_code == 1 and env.plans.get(pid).status == "started"
    done = runner.invoke(app, ["plan", "seal", pid, "--yes", "--as", "Vania", *base])
    assert done.exit_code == 0 and "is now executed" in done.output and "plan verify" in done.output
    assert "SEAL (computed by aios" in runner.invoke(app, ["plan", "show", pid, *base]).output
    refused = runner.invoke(app, ["plan", "verify", pid, "--yes", *base])
    assert refused.exit_code == 2 and "every step passed" in refused.output
    env.plans.record_report(
        pid,
        [StepReport(step=1, outcome="pass"), StepReport(step=2, outcome="pass")],
        actor="mcp",
        by="claude",
    )
    (wt / "src" / "util.py").write_text("tampered\n")
    stale = runner.invoke(app, ["plan", "verify", pid, "--yes", *base])
    assert stale.exit_code == 2 and "changed after it was sealed" in stale.output
    log = "".join(p.read_text(encoding="utf-8") for p in (env.data / "audit").glob("*.jsonl"))
    assert "seal_plan" in log and marker not in log


def test_cli_failed_seal_warns_and_defaults_to_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AIOS_WORKTREES_DIR", raising=False)
    env, base = cli_env(tmp_path)
    pid, wt = started(env)
    (wt / "stray.txt").write_text("x\n")
    out = runner.invoke(app, ["plan", "seal", pid, *base], input="\n")
    assert (
        out.exit_code == 1
        and "FAILED, which is final" in out.output
        and "[BLOCKING] out_of_scope" in out.output
    )
    assert env.plans.get(pid).status == "started"
    assert runner.invoke(app, ["plan", "scope", pid, *base]).exit_code == 1


def test_mcp_scope_check_is_read_only_and_matches_the_cli(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    world = make_world(tmp_path / "w", allow_propose=True)
    # build a started plan in the world's data dir, against the world's own git repo
    from aios.domain.models import Registry
    from aios.services.plan_execution import PlanExecutor
    from aios.services.plans import PlanService

    plans = PlanService(world.data, registry=world.ctx.registry(), today=date(2026, 11, 4))
    view = plans.create(content(files=[{"path": "src/util.py", "action": "modify"}]))
    plans.approve(view.plan.id, by="V", plan_hash_seen=view.plan_hash)
    ex = PlanExecutor(
        plans, Registry.model_validate({"projects": {"pulse": {"path": str(world.repo)}}})
    )
    pid = view.plan.id
    from tests.git_helpers import run_git

    ex.start(pid, by="V", expected_base=run_git(world.repo, "rev-parse", "HEAD"))
    wt = Path(plans.get(pid).execution.worktree)  # type: ignore[union-attr]
    (wt / "src" / "util.py").write_text("def workspace():\n    return 99\n")
    (wt / "stray.txt").write_text("x")
    events = world.data / "plans" / f"{pid}.events.jsonl"
    before = events.read_text()
    out = json.loads(call(world, "check_plan_scope", plan_id=pid))
    assert out["outcome_if_sealed"] == "failed" and out["status"] == "started"
    assert [f["code"] for f in out["findings"]] == ["out_of_scope"] and out["findings"][0][
        "path"
    ] == "stray.txt"
    assert events.read_text() == before and "Nothing was recorded" in out["note"]
    names = {
        t.name
        for t in __import__("tests.mcp_helpers", fromlist=["run"]).run(world.server.list_tools())
    }
    assert "check_plan_scope" in names and not [n for n in names if "seal" in n]


def test_an_edit_made_right_after_checkout_is_not_missed_by_git_stat_caching(env: Env) -> None:
    """Regression: copying the worktree's index made git trust size+mtime and miss a same-size
    edit made within the clock's resolution of the checkout (seen on Windows). Reproduced on any
    platform by comparing only size and mtime (core.checkStat=minimal) and restoring the mtime."""
    from tests.git_helpers import run_git

    run_git(env.repo, "config", "core.checkStat", "minimal")
    run_git(env.repo, "config", "core.trustctime", "false")
    pid, wt = started(env)
    time.sleep(1.5)  # long enough that git no longer treats the recorded entries as "racy"
    target = wt / "src" / "util.py"
    before = target.stat()
    original = target.read_bytes()
    edited = original.replace(b"return 1", b"return 2")
    assert len(edited) == len(original)
    target.write_bytes(edited)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))  # same size, same mtime
    preview = env.ex.preview_scope(pid)
    assert [c.path for c in preview.snapshot.changes] == ["src/util.py"]
    assert preview.scope.outcome == "executed"
