"""Starting and abandoning plan execution (isolated worktrees): state machine, crash recovery,
leftovers, doctor, CLI."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aios.adapters.git_exec import GitWriteRunner
from aios.config import ENV_WORKTREES_DIR, resolve_settings
from aios.domain.models import Registry
from aios.domain.plans import PlanContent, PlanEvent, check_event, replay
from aios.errors import ConfigError, InvalidRequestError
from aios.interfaces.cli.app import app
from aios.services.plan_execution import PlanExecutor
from aios.services.plans import PlanService
from tests.git_helpers import run_git

BASE = "a" * 40


def content(**over: Any) -> PlanContent:
    base: dict[str, Any] = {
        "project": "pulse",
        "title": "Cache lookups",
        "request": "faster",
        "what_will_change": "add a cache",
        "why": "slow",
        "files": [{"path": "src/util.py", "action": "modify"}],
        "risks": ["stale"],
        "expected_result": "faster",
        "verification": ["run tests", "time it"],
    }
    return PlanContent.model_validate({**base, **over})


# -- the state machine (no git involved) ----------------------------------------------------


def ev(seq: int, kind: str, plan_hash: str, **data: Any) -> PlanEvent:
    return PlanEvent.model_validate(
        {
            "seq": seq, "ts": "2026-11-04T10:00:00.000+00:00", "event": kind, "actor": "cli",
            "by": "V", "plan_hash": plan_hash, "data": data,
        }
    )  # fmt: skip


START = {"branch": "aios/PLAN-0001", "worktree": "/w/PLAN-0001", "base_commit": BASE}


def plan_obj() -> Any:
    from aios.domain.plans import Plan

    return Plan(
        **content().model_dump(), id="PLAN-0001", origin="claude", created=date(2026, 11, 4)
    )


def test_started_and_abandoned_events() -> None:
    from aios.domain.plans import plan_hash

    plan = plan_obj()
    h = plan_hash(plan)
    approved = ev(1, "approved", h, tier="R2")
    state = replay(plan, [approved, ev(2, "started", h, **START)])
    assert state.status == "started" and state.execution is not None
    assert state.execution.branch == "aios/PLAN-0001" and state.execution.base_commit == BASE
    assert state.approval is not None  # still approved while started
    back = replay(plan, [approved, ev(2, "started", h, **START), ev(3, "abandoned", h)])
    assert back.status == "approved" and back.execution is None and back.anomalies == []
    again = replay(
        plan,
        [
            approved,
            ev(2, "started", h, **START),
            ev(3, "abandoned", h),
            ev(4, "started", h, **START),
        ],
    )
    assert again.status == "started"


@pytest.mark.parametrize(
    ("status", "kind", "message"),
    [
        ("proposed", "started", "only an approved plan"),
        ("started", "started", "only an approved plan"),
        ("executed", "started", "only an approved plan"),
        ("approved", "abandoned", "only a started plan"),
        ("proposed", "abandoned", "only a started plan"),
        ("started", "rejected", "abandon the worktree"),
        ("started", "outcome", "from the seal"),
        ("started", "verified", "can be verified"),
    ],
)
def test_transitions_that_are_refused(status: Any, kind: str, message: str) -> None:
    data: dict[str, Any] = {"outcome": "executed"} if kind == "outcome" else {}
    if kind == "started":
        data = START
    problem = check_event(status, ev(5, kind, "b" * 64, **data), 2, [], "PLAN-0001")
    assert problem is not None and message in problem


@pytest.mark.parametrize(
    "data",
    [
        {**START, "branch": "main"},
        {**START, "branch": "aios/PLAN-0002"},  # another plan's branch
        {**START, "branch": "aios/PLAN-0001\n"},
        {**START, "base_commit": "abc123"},
        {**START, "base_commit": "A" * 40},
        {**START, "worktree": ""},
        {**START, "worktree": "bad\x00path"},
        {**START, "worktree": "x" * 1001},
        {**START, "base_branch": "x" * 201},
        {"branch": START["branch"], "worktree": START["worktree"]},
    ],
)
def test_a_start_event_must_be_well_formed(data: dict[str, Any]) -> None:
    assert check_event("approved", ev(2, "started", "b" * 64, **data), 2, [], "PLAN-0001")


def test_reports_are_allowed_while_started_and_edits_during_execution_are_flagged() -> None:
    from aios.domain.plans import plan_hash

    plan = plan_obj()
    h = plan_hash(plan)
    report = ev(3, "report", h, results=[{"step": 1, "outcome": "pass"}])
    state = replay(plan, [ev(1, "approved", h, tier="R2"), ev(2, "started", h, **START), report])
    assert state.status == "started" and len(state.reports) == 1
    edited = plan.model_copy(update={"why": "something else"})
    flagged = replay(edited, [ev(1, "approved", h, tier="R2"), ev(2, "started", h, **START)])
    assert flagged.status == "proposed" and flagged.approval_stale
    assert any("different content" in a for a in flagged.anomalies)


# -- real git ---------------------------------------------------------------------------------


TODAY = date(2026, 11, 4)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "proj"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "util.py").write_text("def f():\n    return 1\n")
        (self.repo / ".gitignore").write_text("*.secret\n")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", "base")
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.registry = Registry.model_validate({"projects": {"pulse": {"path": str(self.repo)}}})
        self.plans = PlanService(self.data, registry=self.registry, today=TODAY)
        self.ex = PlanExecutor(self.plans, self.registry, today=TODAY)

    def head(self) -> str:
        return run_git(self.repo, "rev-parse", "HEAD")

    def approved(self, **over: Any) -> str:
        view = self.plans.create(content(**over))
        self.plans.approve(view.plan.id, by="Vania", plan_hash_seen=view.plan_hash)
        return view.plan.id

    def worktree(self, plan_id: str = "PLAN-0001") -> Path:
        return self.ex.worktree_path("pulse", plan_id)

    def start(self, plan_id: str = "PLAN-0001") -> Any:
        return self.ex.start(plan_id, by="Vania", expected_base=self.head())

    def make_leftover(self, plan_id: str = "PLAN-0001") -> None:
        """What an interrupted start leaves behind: a worktree and branch, but no event."""
        path = self.worktree(plan_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        GitWriteRunner(self.repo).worktree_add(path, f"aios/{plan_id}", self.head())

    def registered(self) -> list[str]:
        return [w.branch or "" for w in GitWriteRunner(self.repo).worktree_list()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_start_creates_an_isolated_worktree_and_leaves_the_project_alone(env: Env) -> None:
    pid = env.approved()
    base = env.head()
    status_before = run_git(env.repo, "status", "--porcelain")
    preview = env.start(pid)
    wt = env.worktree(pid)
    assert preview.branch == "aios/PLAN-0001" and preview.base_commit == base
    assert wt == env.repo.parent / "proj.aios-worktrees" / "PLAN-0001" and wt.is_dir()
    assert (wt / "src" / "util.py").read_text().startswith("def f()")
    view = env.plans.get(pid)
    assert view.status == "started" and view.execution is not None
    assert (view.execution.base_commit, view.execution.base_branch) == (base, "main")
    assert view.execution.worktree == str(wt) and view.approval is not None
    # the project's own branch, commit and working tree are exactly as before
    assert env.head() == base and run_git(env.repo, "branch", "--show-current") == "main"
    assert run_git(env.repo, "status", "--porcelain") == status_before
    assert env.registered() == ["main", "aios/PLAN-0001"]


def test_start_is_idempotent(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    again = env.start(pid)
    assert again.already_started and env.registered().count("aios/PLAN-0001") == 1
    assert env.plans.get(pid).execution is not None
    events = (env.data / "plans" / f"{pid}.events.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in events] == ["approved", "started"]


def test_a_configured_worktrees_dir_is_used(tmp_path: Path) -> None:
    env = Env(tmp_path)
    custom = tmp_path / "elsewhere"
    ex = PlanExecutor(env.plans, env.registry, worktrees_dir=custom)
    pid = env.approved()
    ex.start(pid, by="V", expected_base=env.head())
    assert (custom / "pulse" / pid / "src" / "util.py").is_file()


def test_only_an_approved_plan_can_be_started(env: Env) -> None:
    proposed = env.plans.create(content(title="Proposed only"))
    with pytest.raises(InvalidRequestError, match="only an approved plan"):
        env.ex.preview_start(proposed.plan.id)
    rejected = env.approved(title="Rejected")
    env.plans.reject(rejected, by="V", reason="no")
    with pytest.raises(InvalidRequestError, match="rejected"):
        env.ex.preview_start(rejected)
    first = env.approved(title="Original")
    env.plans.create(content(title="Revision", revises=first))
    with pytest.raises(InvalidRequestError, match="replaced by"):
        env.ex.preview_start(first)
    assert not env.worktree(first).exists()


def test_projects_that_cannot_be_executed_are_refused(tmp_path: Path) -> None:
    env = Env(tmp_path)
    pid = env.approved()
    no_git = Registry.model_validate({"projects": {"pulse": {"path": str(env.repo), "git": False}}})
    with pytest.raises(InvalidRequestError, match="git: false"):
        PlanExecutor(env.plans, no_git).preview_start(pid)
    gone = Registry.model_validate({"projects": {"pulse": {"path": str(tmp_path / "missing")}}})
    with pytest.raises(InvalidRequestError, match="does not exist"):
        PlanExecutor(env.plans, gone).preview_start(pid)
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    run_git(empty, "init", "-q")
    fresh = Registry.model_validate({"projects": {"pulse": {"path": str(empty)}}})
    with pytest.raises(InvalidRequestError, match="no commits"):
        PlanExecutor(env.plans, fresh).preview_start(pid)


def test_start_refuses_if_the_project_moved_since_it_was_shown(env: Env) -> None:
    pid = env.approved()
    shown = env.ex.preview_start(pid).base_commit
    (env.repo / "src" / "util.py").write_text("changed\n")
    run_git(env.repo, "commit", "-aq", "-m", "moved on")
    with pytest.raises(InvalidRequestError, match="HEAD changed"):
        env.ex.start(pid, by="V", expected_base=shown)
    assert not env.worktree(pid).exists() and env.plans.get(pid).status == "approved"


def test_uncommitted_changes_in_planned_files_are_warned_about(env: Env) -> None:
    pid = env.approved()
    (env.repo / "src" / "util.py").write_text("edited but not committed\n")
    (env.repo / "notes.txt").write_text("untracked\n")
    warnings = env.ex.preview_start(pid).warnings
    assert any("2 uncommitted" in w for w in warnings)
    assert any("src/util.py" in w and "planned file" in w for w in warnings)


def test_a_crash_between_the_worktree_and_the_event_is_recovered_by_starting_again(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = env.approved()
    real = env.plans.append_locked

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(env.plans, "append_locked", boom)
    with pytest.raises(InvalidRequestError, match="run `aios plan start` again"):
        env.start(pid)
    assert env.worktree(pid).is_dir() and env.plans.get(pid).status == "approved"
    monkeypatch.setattr(env.plans, "append_locked", real)
    preview = env.ex.preview_start(pid)
    assert preview.adopt
    env.start(pid)
    assert env.plans.get(pid).status == "started" and env.registered().count("aios/PLAN-0001") == 1


def test_a_leftover_worktree_on_another_commit_is_not_adopted(env: Env) -> None:
    pid = env.approved()
    env.make_leftover(pid)
    (env.repo / "src" / "util.py").write_text("new\n")
    run_git(env.repo, "commit", "-aq", "-m", "moved on")
    with pytest.raises(InvalidRequestError, match="another commit"):
        env.ex.preview_start(pid)


def test_things_in_the_way_are_reported_and_never_touched(env: Env) -> None:
    pid = env.approved()
    wt = env.worktree(pid)
    wt.mkdir(parents=True)
    (wt / "precious.txt").write_text("mine")
    with pytest.raises(InvalidRequestError, match="git does not know it"):
        env.ex.preview_start(pid)
    with pytest.raises(InvalidRequestError, match="git does not know it"):
        env.ex.abandon(pid, by="V")
    assert (wt / "precious.txt").read_text() == "mine"
    shutil.rmtree(wt)
    run_git(env.repo, "branch", "aios/PLAN-0001")
    with pytest.raises(InvalidRequestError, match="without a worktree"):
        env.ex.preview_start(pid)
    other = env.tmp / "users-own"
    run_git(env.repo, "branch", "-D", "aios/PLAN-0001")
    run_git(env.repo, "worktree", "add", "-q", "-b", "feature", str(other))
    shutil.move(str(other), str(wt))  # not a git operation: git still thinks it is at 'other'
    run_git(env.repo, "worktree", "repair", str(wt))
    with pytest.raises(InvalidRequestError, match="different worktree"):
        env.ex.preview_start(pid)
    assert wt.is_dir()


def test_abandon_removes_the_worktree_and_branch_and_the_plan_can_start_again(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    wt = env.worktree(pid)
    (wt / "src" / "util.py").write_text("work in progress\n")
    (wt / "new.py").write_text("x\n")
    (wt / "token.secret").write_text("s\n")
    preview = env.ex.preview_abandon(pid)
    assert preview.worktree_registered and preview.branch_exists
    assert sorted(preview.changed or []) == ["new.py", "src/util.py", "token.secret"]
    result = env.ex.abandon(pid, by="Vania")
    assert (result.worktree_removed, result.branch_deleted, result.event_recorded) == (
        True,
        True,
        True,
    )
    assert not wt.exists() and env.registered() == ["main"]
    assert env.plans.get(pid).status == "approved" and env.plans.get(pid).execution is None
    assert run_git(env.repo, "branch", "--list", "aios/*") == ""
    env.start(pid)  # same plan, fresh worktree
    assert env.plans.get(pid).status == "started"


def test_abandon_can_keep_the_branch(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    wt = env.worktree(pid)
    (wt / "a.py").write_text("x\n")
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", "work")
    result = env.ex.abandon(pid, by="V", keep_branch=True)
    assert result.worktree_removed and not result.branch_deleted
    assert "aios/PLAN-0001" in run_git(env.repo, "branch", "--list", "aios/*")
    with pytest.raises(InvalidRequestError, match="without a worktree"):
        env.ex.preview_start(pid)  # the kept branch blocks a restart until it is cleaned up
    assert env.ex.abandon(pid, by="V").branch_deleted
    env.start(pid)


def test_abandon_cleans_the_leftovers_of_an_interrupted_start_without_an_event(env: Env) -> None:
    pid = env.approved()
    env.make_leftover(pid)
    result = env.ex.abandon(pid, by="V")
    assert (result.worktree_removed, result.branch_deleted, result.event_recorded) == (
        True,
        True,
        False,
    )
    assert env.registered() == ["main"] and env.plans.get(pid).status == "approved"


def test_abandon_when_the_directory_was_deleted_by_hand(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    shutil.rmtree(env.worktree(pid))
    result = env.ex.abandon(pid, by="V")
    assert result.worktree_removed and result.branch_deleted
    assert env.registered() == ["main"] and env.plans.get(pid).status == "approved"


def test_abandon_has_nothing_to_do_in_other_states(env: Env) -> None:
    pid = env.approved()
    with pytest.raises(InvalidRequestError, match=r"nothing to abandon|no worktree or branch"):
        env.ex.abandon(pid, by="V")
    proposed = env.plans.create(content(title="P"))
    with pytest.raises(InvalidRequestError, match="nothing to abandon"):
        env.ex.abandon(proposed.plan.id, by="V")


def test_a_started_plan_cannot_be_rejected_or_given_a_manual_outcome(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    with pytest.raises(InvalidRequestError, match="abandon the worktree"):
        env.plans.reject(pid, by="V", reason="no")
    with pytest.raises(InvalidRequestError, match="from the seal"):
        env.plans.record_outcome(pid, by="V", outcome="executed")
    env.ex.abandon(pid, by="V")
    assert env.plans.reject(pid, by="V", reason="changed my mind").status == "rejected"


def test_editing_a_plan_that_is_being_executed_is_noticed(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    path = env.data / "plans" / f"{pid}.yaml"
    path.write_text(path.read_text().replace("add a cache", "add a cache and delete the logs"))
    view = env.plans.get(pid)
    assert view.status == "proposed" and view.approval_stale
    codes = {i.code for i in env.plans.check(today=TODAY)}
    assert {"plan_events_inconsistent", "plan_approval_stale"} <= codes


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX hook scripts")
def test_starting_never_runs_the_projects_hooks(env: Env) -> None:
    proof = env.tmp / "proof.txt"
    hook = env.repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran >> {proof}\n", newline="\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    pid = env.approved()
    env.start(pid)
    assert not proof.exists()
    subprocess.run(["git", "worktree", "add", "-q", "-b", "ctl", str(env.tmp / "ctl")],
                   cwd=env.repo, check=True, capture_output=True)  # fmt: skip
    assert proof.exists()  # control: plain git does run it


def test_two_simultaneous_starts_make_one_worktree(env: Env) -> None:
    pid = env.approved()
    errors: list[BaseException] = []

    def go() -> None:
        try:
            env.start(pid)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and env.registered().count("aios/PLAN-0001") == 1
    assert env.plans.get(pid).status == "started"


# -- doctor ---------------------------------------------------------------------------------


def test_doctor_is_quiet_when_everything_matches(env: Env) -> None:
    pid = env.approved()
    assert env.ex.check() == []
    env.start(pid)
    assert env.ex.check() == []


def test_doctor_reports_a_started_plan_whose_worktree_is_gone(env: Env) -> None:
    pid = env.approved()
    env.start(pid)
    shutil.rmtree(env.worktree(pid))
    found = {(i.code, i.severity.value) for i in env.ex.check()}
    assert ("plan_worktree_missing", "error") in found
    run_git(env.repo, "worktree", "prune")
    assert ("plan_worktree_missing", "error") in {
        (i.code, i.severity.value) for i in env.ex.check()
    }


def test_doctor_reports_orphans_and_stale_executions(env: Env) -> None:
    pid = env.approved()
    env.make_leftover(pid)
    assert {i.code for i in env.ex.check()} == {"orphan_worktree"}
    env.ex.abandon(pid, by="V")
    run_git(env.repo, "branch", "aios/PLAN-0007")
    assert {i.code for i in env.ex.check()} == {"orphan_branch"}
    run_git(env.repo, "branch", "-D", "aios/PLAN-0007")
    env.start(pid)
    late = PlanExecutor(env.plans, env.registry, today=date(2099, 1, 1), stale_started_days=14)
    assert {i.code for i in late.check()} == {"plan_started_stale"}


def test_doctor_skips_projects_it_cannot_inspect(env: Env) -> None:
    env.approved()
    gone = Registry.model_validate({"projects": {"pulse": {"path": str(env.tmp / "missing")}}})
    assert PlanExecutor(env.plans, gone).check() == []


# -- settings and CLI -------------------------------------------------------------------------


def test_worktrees_dir_setting(tmp_path: Path) -> None:
    data = tmp_path / "d"
    data.mkdir()
    absolute = tmp_path / "wts"
    assert resolve_settings(data, {ENV_WORKTREES_DIR: str(absolute)}).worktrees_dir == absolute
    assert resolve_settings(data, {}).worktrees_dir is None
    with pytest.raises(ConfigError, match="absolute"):
        resolve_settings(data, {ENV_WORKTREES_DIR: "relative/wts"})


runner = CliRunner()


def cli_env(tmp_path: Path) -> tuple[Env, list[str]]:
    env = Env(tmp_path)
    (env.data / "registry.yaml").write_text(
        f"projects:\n  pulse:\n    path: '{env.repo.as_posix()}'\n    description: d\n",
        encoding="utf-8",
    )
    return env, ["--data-dir", str(env.data)]


def test_cli_start_and_abandon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_WORKTREES_DIR, raising=False)
    env, base = cli_env(tmp_path)
    marker = "ZEBRA-EXEC-9917"
    pid = env.approved(what_will_change=marker)
    declined = runner.invoke(app, ["plan", "start", pid, *base], input="n\n")
    assert declined.exit_code == 1 and not env.worktree(pid).exists()
    assert "branch:    aios/PLAN-0001" in declined.output and "base:" in declined.output
    ok = runner.invoke(app, ["plan", "start", pid, "--yes", "--as", "Vania", *base])
    assert ok.exit_code == 0 and "Work only inside" in ok.output and env.worktree(pid).is_dir()
    assert "already started" in runner.invoke(app, ["plan", "start", pid, "--yes", *base]).output
    shown = runner.invoke(app, ["plan", "show", pid, *base]).output
    assert "EXECUTION started by Vania" in shown and "[started]" in shown
    (env.worktree(pid) / "scratch.py").write_text("x\n")
    keep = runner.invoke(app, ["plan", "abandon", pid, *base], input="n\n")
    assert keep.exit_code == 1 and "scratch.py" in keep.output and env.worktree(pid).is_dir()
    gone = runner.invoke(app, ["plan", "abandon", pid, "--yes", *base])
    assert (
        gone.exit_code == 0 and "worktree removed" in gone.output and not env.worktree(pid).exists()
    )
    log = "".join(p.read_text(encoding="utf-8") for p in (env.data / "audit").glob("*.jsonl"))
    assert "start_plan" in log and "abandon_plan" in log and marker not in log
    again = runner.invoke(app, ["plan", "abandon", pid, "--yes", *base])
    assert again.exit_code == 2 and "no worktree or branch" in again.output


def test_cli_doctor_reports_execution_problems(tmp_path: Path) -> None:
    env, base = cli_env(tmp_path)
    pid = env.approved()
    env.start(pid)
    shutil.rmtree(env.worktree(pid))
    out = runner.invoke(app, ["doctor", *base])
    assert out.exit_code == 1 and "plan_worktree_missing" in out.output


def test_the_mcp_server_cannot_start_or_abandon(tmp_path: Path) -> None:
    from tests.mcp_helpers import make_world, run

    world = make_world(tmp_path, allow_propose=True)
    names = {t.name for t in run(world.server.list_tools())}
    assert not [n for n in names if any(w in n for w in ("start", "abandon", "worktree", "seal"))]
    for op in ("start_plan", "abandon_plan", "execute_plan"):
        assert not world.ctx.policy.authorize(op).allowed
