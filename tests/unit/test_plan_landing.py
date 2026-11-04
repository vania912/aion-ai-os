"""land-check and cleanup: landing is a person's git step; aios only checks and tidies up."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aios.domain.plans import StepReport
from aios.errors import InvalidRequestError
from aios.interfaces.cli.app import app
from tests.git_helpers import run_git
from tests.mcp_helpers import make_world, run
from tests.unit.test_plan_execution import Env
from tests.unit.test_plan_seal import seal, started


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def verified(env: Env, content: str = "def f():\n    return 2\n") -> tuple[str, Path]:
    """A plan whose worktree was edited, sealed, reported on and verified (but not committed)."""
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text(content)
    seal(env, pid)
    env.plans.record_report(
        pid,
        [StepReport(step=1, outcome="pass"), StepReport(step=2, outcome="pass")],
        actor="mcp",
        by="claude",
    )
    env.ex.verify(pid, by="Vania")
    return pid, wt


def commit_in_worktree(wt: Path) -> None:
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", "implement the plan")


def failed(check: Any) -> list[str]:
    return [i.text for i in check.items if not i.ok]


def test_a_plan_without_a_sealed_execution_cannot_be_checked(env: Env) -> None:
    pid = env.approved()
    with pytest.raises(InvalidRequestError, match="no sealed execution"):
        env.ex.land_check(pid)
    pid2, _wt = started(env, title="Other")
    with pytest.raises(InvalidRequestError, match="no sealed execution"):
        env.ex.land_check(pid2)


def test_uncommitted_work_is_not_ready_and_the_commit_commands_are_offered(env: Env) -> None:
    pid, _wt = verified(env)
    check = env.ex.land_check(pid)
    assert not check.ready
    assert len(failed(check)) == 1 and "commits identical to the sealed diff" in failed(check)[0]
    assert any(c.startswith("git -C") and " add -A" in c for c in check.commands)
    assert any("commit -m" in c for c in check.commands)
    assert any("not committed on the branch yet" in n for n in check.notes)


def test_committed_verified_work_is_ready_and_the_commands_are_printed_not_run(env: Env) -> None:
    pid, wt = verified(env)
    commit_in_worktree(wt)
    head_before = env.head()
    check = env.ex.land_check(pid)
    assert check.ready and failed(check) == []
    merge = [c for c in check.commands if "merge" in c]
    assert merge == [f'git -C "{env.repo}" merge --ff-only aios/PLAN-0001']
    assert check.commands[-1] == f"aios plan cleanup {pid}"
    assert env.head() == head_before  # aios merged nothing
    assert run_git(env.repo, "branch", "--show-current") == "main"


def test_an_unverified_plan_is_not_ready(env: Env) -> None:
    pid, wt = started(env)
    (wt / "src" / "util.py").write_text("x\n")
    seal(env, pid)
    commit_in_worktree(wt)
    check = env.ex.land_check(pid)
    assert not check.ready and any("a person verified it" in t for t in failed(check))


def test_a_partial_or_failed_seal_is_not_ready(env: Env) -> None:
    files = [
        {"path": "src/util.py", "action": "modify"},
        {"path": "src/new.py", "action": "create"},
    ]
    pid, wt = started(env, files=files)
    (wt / "src" / "util.py").write_text("half\n")
    seal(env, pid)
    commit_in_worktree(wt)
    assert any("sealed outcome is 'executed'" in t for t in failed(env.ex.land_check(pid)))


def test_changes_after_the_seal_make_it_not_ready_even_if_committed(env: Env) -> None:
    pid, wt = verified(env)
    (wt / "src" / "util.py").write_text("def f():\n    return 3  # sneaked in after verification\n")
    commit_in_worktree(wt)
    check = env.ex.land_check(pid)
    assert not check.ready
    texts = failed(check)
    assert any("exactly as it was sealed" in t for t in texts)
    assert any("commits identical" in t for t in texts)


def test_committing_something_different_from_the_seal_is_caught(env: Env) -> None:
    pid, wt = verified(env)
    commit_in_worktree(wt)
    (wt / "src" / "util.py").write_text("def f():\n    return 2\n")  # same as sealed: fine
    assert env.ex.land_check(pid).ready
    (wt / "extra.txt").write_text("x\n")
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", "an extra file nobody approved")
    check = env.ex.land_check(pid)
    assert not check.ready and any("commits identical" in t for t in failed(check))


def test_if_your_branch_moved_on_the_merge_is_not_a_fast_forward(env: Env) -> None:
    pid, wt = verified(env)
    commit_in_worktree(wt)
    (env.repo / "other.txt").write_text("unrelated\n")
    run_git(env.repo, "add", "-A")
    run_git(env.repo, "commit", "-q", "-m", "unrelated work on main")
    check = env.ex.land_check(pid)
    assert check.ready
    assert any("--no-ff" in c for c in check.commands) and not any(
        "--ff-only" in c for c in check.commands
    )
    assert any("moved on" in n for n in check.notes)


def test_a_changed_plan_is_not_ready(env: Env) -> None:
    pid, wt = verified(env)
    commit_in_worktree(wt)
    path = env.data / "plans" / f"{pid}.yaml"
    path.write_text(path.read_text().replace("add a cache", "add a cache and more"))
    with pytest.raises(InvalidRequestError):
        env.ex.land_check(pid)  # the events no longer apply: no sealed execution is left to check


def land(env: Env, wt: Path) -> None:
    commit_in_worktree(wt)
    run_git(env.repo, "merge", "--ff-only", "aios/PLAN-0001")


def test_cleanup_after_landing_removes_the_worktree_and_branch(env: Env) -> None:
    pid, wt = verified(env)
    land(env, wt)
    preview = env.ex.preview_cleanup(pid)
    assert preview.landed and preview.uncommitted == []
    env.ex.cleanup(pid)
    assert not wt.exists() and env.registered() == ["main"]
    assert run_git(env.repo, "branch", "--list", "aios/*") == ""
    assert env.plans.get(pid).status == "verified"  # the plan itself is untouched
    assert (env.repo / "src" / "util.py").read_text() == "def f():\n    return 2\n"  # work landed


def test_cleanup_refuses_before_landing(env: Env) -> None:
    pid, wt = verified(env)
    commit_in_worktree(wt)  # committed on the branch, but not merged
    with pytest.raises(InvalidRequestError, match="no commits of its own that your current commit"):
        env.ex.cleanup(pid)
    assert wt.is_dir() and env.registered().count("aios/PLAN-0001") == 1


def test_cleanup_never_destroys_uncommitted_work_even_when_the_branch_looks_merged(
    env: Env,
) -> None:
    """Regression: a branch still sitting on the base commit is trivially 'merged'; cleanup used to
    treat that as landed and would delete a worktree holding the only copy of the sealed work."""
    pid, wt = verified(env)
    with pytest.raises(InvalidRequestError, match="no commits of its own"):
        env.ex.cleanup(pid)
    assert (wt / "src" / "util.py").read_text() == "def f():\n    return 2\n"
    # and after a real landing, extra uncommitted work in the worktree still blocks cleanup
    land(env, wt)
    (wt / "late-notes.txt").write_text("not committed\n")
    with pytest.raises(InvalidRequestError, match=r"not committed.*would be lost"):
        env.ex.cleanup(pid)
    assert (wt / "late-notes.txt").exists()


def test_cleanup_is_only_for_verified_plans(env: Env) -> None:
    pid, wt = started(env)
    with pytest.raises(InvalidRequestError, match="cleanup is for a verified plan"):
        env.ex.preview_cleanup(pid)
    assert wt.is_dir()


def test_cleanup_when_the_directory_is_already_gone(env: Env) -> None:
    import shutil

    pid, wt = verified(env)
    land(env, wt)
    shutil.rmtree(wt)
    env.ex.cleanup(pid)
    assert env.registered() == ["main"] and run_git(env.repo, "branch", "--list", "aios/*") == ""


def test_doctor_notices_a_landed_plan_that_was_not_cleaned_up(env: Env) -> None:
    pid, wt = verified(env)
    assert env.ex.check() == []  # verified, not landed: nothing to say, and not an orphan either
    commit_in_worktree(wt)
    assert env.ex.check() == []
    run_git(env.repo, "merge", "--ff-only", "aios/PLAN-0001")
    assert {i.code for i in env.ex.check()} == {"plan_landed_not_cleaned"}
    env.ex.cleanup(pid)
    assert env.ex.check() == []


# -- CLI --------------------------------------------------------------------------------------

runner = CliRunner()


def cli_env(tmp_path: Path) -> tuple[Env, list[str]]:
    env = Env(tmp_path)
    (env.data / "registry.yaml").write_text(
        f"projects:\n  pulse:\n    path: '{env.repo.as_posix()}'\n    description: d\n",
        encoding="utf-8",
    )
    return env, ["--data-dir", str(env.data)]


def test_cli_land_check_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOS_WORKTREES_DIR", raising=False)
    env, base = cli_env(tmp_path)
    pid, wt = verified(env)
    not_ready = runner.invoke(app, ["plan", "land-check", pid, *base])
    assert (
        not_ready.exit_code == 1
        and "[FAIL]" in not_ready.output
        and "NOT READY" in not_ready.output
    )
    commit_in_worktree(wt)
    ready = runner.invoke(app, ["plan", "land-check", pid, *base])
    assert (
        ready.exit_code == 0
        and "READY" in ready.output
        and "merge --ff-only aios/PLAN-0001" in ready.output
    )
    assert "aios does not merge anything itself" in ready.output
    early = runner.invoke(app, ["plan", "cleanup", pid, "--yes", *base])
    assert early.exit_code == 2 and "no commits of its own" in early.output and wt.is_dir()
    run_git(env.repo, "merge", "--ff-only", "aios/PLAN-0001")
    declined = runner.invoke(app, ["plan", "cleanup", pid, *base], input="n\n")
    assert declined.exit_code == 1 and wt.is_dir()
    done = runner.invoke(app, ["plan", "cleanup", pid, "--yes", *base])
    assert done.exit_code == 0 and "cleaned up" in done.output and not wt.exists()
    log = "".join(p.read_text(encoding="utf-8") for p in (env.data / "audit").glob("*.jsonl"))
    assert "cleanup_plan" in log


def test_the_prompt_tells_claude_to_work_only_in_the_worktree_and_to_check_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "w").mkdir()
    world = make_world(tmp_path / "w", allow_propose=True)
    result = run(world.server.get_prompt("prepare_change_plan", {"project": "pulse"}))
    text = getattr(result.messages[0].content, "text", "")
    for needle in ("only inside that folder", "check_plan_scope", "never commit to the person's branch",
                   "aios plan land-check", "do not add files the plan does not list"):  # fmt: skip
        assert needle in text, needle
    assert "isolated worktree" in (world.server.instructions or "")
