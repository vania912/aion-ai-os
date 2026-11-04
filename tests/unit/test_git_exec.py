"""The hardened git write runner, tested against a repository that tries to run its own code."""

from __future__ import annotations

import contextlib
import gc
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from aios.adapters.git_exec import GitWriteRunner
from aios.errors import GitError
from tests.git_helpers import run_git

POSIX = os.name == "posix"
#: git options that make the test's own plain git calls ignore the repository's booby traps
SAFE = (
    "-c",
    "filter.evil.process=",
    "-c",
    "filter.evil.required=false",
    "-c",
    "core.fsmonitor=false",
)


@dataclass
class Hostile:
    repo: Path
    proof: Path
    script: Path
    base: str

    def ran(self) -> set[str]:
        if not self.proof.exists():
            return set()
        return {line.strip() for line in self.proof.read_text().splitlines() if line.strip()}


@pytest.fixture
def hostile(tmp_path: Path) -> Hostile:
    """A repository whose own configuration, attributes and hooks all try to execute a script."""
    repo = tmp_path / "repo"
    repo.mkdir()
    proof = tmp_path / "proof.txt"
    script = tmp_path / "evil.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{proof.as_posix()}"\n'
        'case "$1" in smudge|clean) cat;; textconv) cat "$2";; esac\n'
        "exit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    run_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("alpha\n")
    (repo / "b.dat").write_text("data\n")
    (repo / "c.bin").write_bytes(b"\x00\x01\x02")
    (repo / ".gitattributes").write_text("*.dat filter=evil\n*.txt diff=evil\n*.bin -text\n")
    (repo / ".gitignore").write_text("*.secret\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "base")
    base = run_git(repo, "rev-parse", "HEAD")

    cmd = script.as_posix()
    hooks = tmp_path / "hostile-hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-commit", "pre-commit", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\n"{cmd}" hook-{name}\n', encoding="utf-8", newline="\n")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    included = tmp_path / "included.cfg"
    included.write_text(
        f'[filter "inc.x"]\n\tsmudge = "{cmd}" inc-smudge\n\tclean = "{cmd}" inc-clean\n'
    )
    cfg = repo / ".git" / "config"
    cfg.write_text(
        cfg.read_text()
        + f'[filter "evil"]\n\tsmudge = "{cmd}" smudge\n\tclean = "{cmd}" clean\n'
        + f'\tprocess = "{cmd}" process\n\trequired = true\n'
        + f'[diff "evil"]\n\ttextconv = "{cmd}" textconv\n'
        + f'[diff]\n\texternal = "{cmd}" external\n'
        + f'[core]\n\tfsmonitor = "{cmd}" fsmonitor\n\thooksPath = {hooks.as_posix()}\n'
        + f"[include]\n\tpath = {included.as_posix()}\n"
    )
    proof.unlink(missing_ok=True)
    return Hostile(repo, proof, script, base)


@pytest.fixture
def runner(hostile: Hostile) -> GitWriteRunner:
    return GitWriteRunner(hostile.repo, timeout_s=60)


def plain_git(h: Hostile, *args: str, cwd: Path | None = None) -> None:
    """Run git the unprotected way, to show what the hostile repository would do."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    with contextlib.suppress(subprocess.TimeoutExpired):  # fake long-running filter or monitor
        subprocess.run(
            ["git", *args], cwd=cwd or h.repo, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=4, check=False,
        )  # fmt: skip


def test_control_plain_git_does_run_the_hostile_code(hostile: Hostile, tmp_path: Path) -> None:
    """Without this the other tests could pass because the trap does not work on this machine."""
    plain_git(hostile, "worktree", "add", "-q", "-b", "c1", str(tmp_path / "ctl1"), "HEAD")
    plain_git(hostile, "diff", "HEAD")
    first = hostile.ran()
    # the long-running 'process' filter aborts the first checkout; remove it to show that the
    # smudge filter and the hooks fire as well
    run_git(hostile.repo, "config", "--unset", "filter.evil.process")
    plain_git(hostile, "worktree", "add", "-q", "-b", "c2", str(tmp_path / "ctl2"), "HEAD")
    ran = hostile.ran()
    if not ran and not POSIX:
        pytest.skip("the booby trap does not fire on this platform; the tests below prove nothing")
    assert {"process", "fsmonitor"} <= first, first
    assert {"smudge", "hook-post-checkout"} <= ran, ran


def wt(tmp_path: Path, n: int = 1) -> Path:
    base = tmp_path / "wts"
    base.mkdir(exist_ok=True)
    return base / f"PLAN-{n:04d}"


def test_worktree_add_runs_none_of_the_repositorys_code(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    info = runner.worktree_add(wt(tmp_path), "aios/PLAN-0001", hostile.base)
    assert info.branch == "aios/PLAN-0001" and info.head == hostile.base
    assert hostile.ran() == set()
    # the checkout itself still worked, with the filters replaced by a pass-through
    assert (wt(tmp_path) / "b.dat").read_text() == "data\n"
    assert (wt(tmp_path) / "a.txt").read_text() == "alpha\n"


def test_changes_runs_none_of_the_repositorys_code(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "a.txt").write_text("alpha changed\n")  # has a textconv driver
    (path / "b.dat").write_text("data changed\n")  # has clean/smudge/process filters
    (path / "new.dat").write_text("brand new\n")
    result = runner.changes(path, hostile.base)
    assert {c.path for c in result.changes} == {"a.txt", "b.dat", "new.dat"}
    assert hostile.ran() == set()


def test_system_and_user_git_config_are_not_read(
    hostile: Hostile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = hostile.script.as_posix()
    user = tmp_path / "usercfg"
    user.write_text(
        f'[filter "evil2"]\n\tsmudge = "{cmd}" user-smudge\n\tclean = "{cmd}" user-clean\n'
        f"[core]\n\thooksPath = {(tmp_path / 'hostile-hooks').as_posix()}\n"
    )
    (hostile.repo / ".gitattributes").write_text("*.dat filter=evil2\n")
    run_git(hostile.repo, "add", "-A")
    run_git(hostile.repo, "commit", "-q", "-m", "attrs")
    base = run_git(hostile.repo, "rev-parse", "HEAD")
    for key in ("GIT_CONFIG_GLOBAL", "HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(user) if key == "GIT_CONFIG_GLOBAL" else str(tmp_path))
    (tmp_path / ".gitconfig").write_text(user.read_text())
    hostile.proof.unlink(missing_ok=True)
    fresh = GitWriteRunner(hostile.repo)
    path = wt(tmp_path)
    fresh.worktree_add(path, "aios/PLAN-0001", base)
    (path / "b.dat").write_text("changed\n")
    fresh.changes(path, base)
    assert not {r for r in hostile.ran() if r.startswith(("user-", "hook-"))}


def test_only_the_repositorys_own_config_is_consulted_for_filters(
    hostile: Hostile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / ".gitconfig"
    user.write_text('[filter "from-user-config"]\n\tsmudge = true\n')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    overrides = " ".join(GitWriteRunner(hostile.repo)._filter_overrides(hostile.repo))
    assert "filter.evil." in overrides and "filter.inc.x." in overrides  # repo + included file
    assert "from-user-config" not in overrides


def test_git_environment_variables_from_the_parent_are_dropped(
    hostile: Hostile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", hostile.script.as_posix())
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_SSH_COMMAND", hostile.script.as_posix())
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath={tmp_path / 'hostile-hooks'}'")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "bogus-index"))
    runner = GitWriteRunner(hostile.repo)
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "a.txt").write_text("changed\n")
    assert [c.path for c in runner.changes(path, hostile.base).changes] == ["a.txt"]
    assert not any(r.startswith("hook-") or r == "external" for r in hostile.ran())


def test_a_filter_name_that_cannot_be_overridden_is_refused(
    hostile: Hostile, tmp_path: Path
) -> None:
    cfg = hostile.repo / ".git" / "config"
    cfg.write_text(cfg.read_text() + '[filter "a=b"]\n\tsmudge = true\n')
    with pytest.raises(GitError, match="cannot neutralise"):
        GitWriteRunner(hostile.repo).worktree_add(wt(tmp_path), "aios/PLAN-0001", hostile.base)
    assert not wt(tmp_path).exists()


# -- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch",
    ["main", "aios/PLAN-1", "aios/plan-0001", "aios/PLAN-0001/x", "aios/PLAN-0001 ", "-D",
     "aios/../x", "aios/PLAN-0001\n", "refs/heads/aios/PLAN-0001", "", "aios/PLAN-0001;rm"],
)  # fmt: skip
def test_only_aios_plan_branches_are_accepted(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path, branch: str
) -> None:
    with pytest.raises(GitError, match="branch name"):
        runner.worktree_add(wt(tmp_path), branch, hostile.base)
    with pytest.raises(GitError, match="branch name"):
        runner.branch_delete(branch)
    assert not wt(tmp_path).exists()


def test_worktree_path_rules(hostile: Hostile, runner: GitWriteRunner, tmp_path: Path) -> None:
    base = hostile.base
    with pytest.raises(GitError, match="absolute"):
        runner.worktree_add(Path("relative/dir"), "aios/PLAN-0001", base)
    with pytest.raises(GitError, match="outside the project"):
        runner.worktree_add(hostile.repo / "inside", "aios/PLAN-0001", base)
    with pytest.raises(GitError, match="outside the project"):
        runner.worktree_add(hostile.repo / ".git" / "x", "aios/PLAN-0001", base)
    with pytest.raises(GitError, match=r"'\.' or '\.\.'"):
        runner.worktree_add(tmp_path / "wts" / ".." / "esc", "aios/PLAN-0001", base)
    with pytest.raises(GitError, match="must not contain the project"):
        runner.worktree_add(tmp_path, "aios/PLAN-0001", base)
    with pytest.raises(GitError, match="parent directory"):
        runner.worktree_add(tmp_path / "missing" / "wt", "aios/PLAN-0001", base)
    (tmp_path / "exists").mkdir()
    with pytest.raises(GitError, match="already exists"):
        runner.worktree_add(tmp_path / "exists", "aios/PLAN-0001", base)
    if POSIX:
        link = tmp_path / "link"
        link.symlink_to(hostile.repo)
        with pytest.raises(GitError, match="outside the project"):
            runner.worktree_add(link / "sub", "aios/PLAN-0001", base)


def test_bases_and_revisions_are_validated(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    for bad in ("HEAD", "main", hostile.base[:12], hostile.base.upper(), "", "-1"):
        with pytest.raises(GitError, match="full commit id"):
            runner.worktree_add(wt(tmp_path), "aios/PLAN-0001", bad)
    assert runner.resolve_commit("HEAD") == hostile.base
    assert runner.resolve_commit("main") == hostile.base
    for bad in ("--output=x", "-n1", "HEAD:a.txt", "HEAD~1", "HEAD^", "a..b", "x y", "@{u}", "", "a\x00b",
                "HEAD\n", "*", "[x]"):  # fmt: skip
        with pytest.raises(GitError, match=r"acceptable revision|no such commit"):
            runner.resolve_commit(bad)
    with pytest.raises(GitError, match="no such commit"):
        runner.resolve_commit("does-not-exist")
    tree = run_git(hostile.repo, "rev-parse", "HEAD^{tree}")
    with pytest.raises(GitError, match="no such commit"):
        runner.resolve_commit(tree)  # a tree is not a commit


def test_errors_do_not_leak_the_project_path(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    runner.worktree_add(wt(tmp_path, 1), "aios/PLAN-0001", hostile.base)
    with pytest.raises(GitError) as err:
        runner.worktree_add(wt(tmp_path, 2), "aios/PLAN-0001", hostile.base)  # branch exists
    assert str(hostile.repo) not in str(err.value)


# -- remove / delete are limited to what aios created ------------------------------------------


def test_remove_only_touches_registered_aios_worktrees(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "scratch.txt").write_text("x")
    runner.worktree_remove(path)
    assert not path.exists() and [w.branch for w in runner.worktree_list()] == ["main"]
    assert runner.branch_exists("aios/PLAN-0001")  # the branch survives until deleted on purpose
    runner.branch_delete("aios/PLAN-0001")
    assert not runner.branch_exists("aios/PLAN-0001")


def test_remove_refuses_everything_else(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep me")
    with pytest.raises(GitError, match="no aios worktree"):
        runner.worktree_remove(precious)
    with pytest.raises(GitError, match="main working tree"):
        runner.worktree_remove(hostile.repo)
    other = tmp_path / "users-own"
    run_git(hostile.repo, *SAFE, "worktree", "add", "-q", "-b", "feature", str(other))
    with pytest.raises(GitError, match="not created by aios"):
        runner.worktree_remove(other)
    with pytest.raises(GitError, match="not created by aios"):
        runner.changes(other, hostile.base)
    assert (precious / "keep.txt").read_text() == "keep me" and other.is_dir()
    assert hostile.repo.is_dir()


# -- the change snapshot -----------------------------------------------------------------------


def test_changes_classifies_every_kind_of_change(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "a.txt").write_text("alpha changed\n")  # modified
    (path / "b.dat").unlink()  # deleted
    (path / "src").mkdir()
    (path / "src" / "new file.py").write_text("print(1)\n")  # added, with a space in the name
    (path / "c.bin").write_bytes(b"\x00\x01\x02\x03")  # modified binary
    (path / "blob.bin").write_bytes(b"\x00\xff\x00")  # added binary
    (path / "token.secret").write_text("s3cret")  # ignored
    (path / "dir").mkdir()
    (path / "dir" / "inner.secret").write_text("x")  # ignored, nested
    result = runner.changes(path, hostile.base)
    by_path = {c.path: c for c in result.changes}
    assert {p: c.status for p, c in by_path.items()} == {
        "a.txt": "M", "b.dat": "D", "src/new file.py": "A", "c.bin": "M", "blob.bin": "A",
    }  # fmt: skip
    assert by_path["c.bin"].binary and by_path["blob.bin"].binary and not by_path["a.txt"].binary
    assert result.ignored == ["dir/inner.secret", "token.secret"]
    assert result.base == hostile.base and len(result.diff_sha256) == 64 and result.diff_bytes > 0


@pytest.mark.skipif(not POSIX, reason="symlinks and file modes need POSIX")
def test_symlinks_mode_changes_and_nested_repositories(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "link").symlink_to("/etc/passwd")
    (path / "a.txt").chmod(0o755)
    nested = path / "vendor" / "lib"
    nested.mkdir(parents=True)
    run_git(nested, "init", "-q")
    (nested / "f").write_text("x")
    run_git(nested, "add", "-A")
    run_git(nested, "commit", "-q", "-m", "n")
    by_path = {c.path: c for c in runner.changes(path, hostile.base).changes}
    assert by_path["link"].is_symlink and by_path["link"].status == "A"
    assert by_path["a.txt"].mode_changed and by_path["a.txt"].status == "M"
    assert by_path["vendor/lib"].is_gitlink
    assert hostile.ran() == set()


@pytest.mark.skipif(not POSIX, reason="newline and non-UTF-8 file names need POSIX")
def test_awkward_file_names(hostile: Hostile, runner: GitWriteRunner, tmp_path: Path) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    names = ["line\nbreak.txt", "tab\there.txt", "ünïcode-名前.txt", "quote\"s'.txt", "-dash.txt"]
    for name in names:
        (path / name).write_text("x")
    (path / os.fsdecode(b"bad-\xff-utf8.txt")).write_text("x")
    found = {c.path for c in runner.changes(path, hostile.base).changes}
    assert set(names) <= found and len(found) == len(names) + 1


def test_the_snapshot_is_stable_and_sensitive(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    empty = runner.changes(path, hostile.base)
    assert empty.changes == [] and empty.ignored == [] and empty.diff_bytes == 0
    (path / "a.txt").write_text("one\n")
    first = runner.changes(path, hostile.base)
    assert runner.changes(path, hostile.base).diff_sha256 == first.diff_sha256
    (path / "a.txt").write_text("two\n")
    second = runner.changes(path, hostile.base)
    assert second.diff_sha256 != first.diff_sha256
    (path / "a.txt").write_text("one\n")
    assert runner.changes(path, hostile.base).diff_sha256 == first.diff_sha256


def test_the_snapshot_does_not_touch_the_worktrees_own_state(
    hostile: Hostile, runner: GitWriteRunner, tmp_path: Path
) -> None:
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "untracked.txt").write_text("u")
    (path / "a.txt").write_text("m\n")
    index_before = run_git(path, "rev-parse", "--git-path", "index")
    before = run_git(path, *SAFE, "status", "--porcelain")
    runner.changes(path, hostile.base)
    assert (
        run_git(path, *SAFE, "status", "--porcelain") == before
    )  # still '?? untracked.txt', not 'A'
    assert "?? untracked.txt" in before
    assert run_git(path, *SAFE, "diff", "--cached", "--name-only") == ""
    assert run_git(path, "rev-parse", "--git-path", "index") == index_before


def test_line_ending_noise_is_not_a_change(
    runner: GitWriteRunner, hostile: Hostile, tmp_path: Path
) -> None:
    (hostile.repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    run_git(hostile.repo, *SAFE, "add", "-A")
    run_git(hostile.repo, *SAFE, "commit", "-q", "-m", "crlf")
    base = run_git(hostile.repo, "rev-parse", "HEAD")
    path = wt(tmp_path)
    runner.worktree_add(path, "aios/PLAN-0001", base)
    assert runner.changes(path, base).changes == []
    (path / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert [c.path for c in runner.changes(path, base).changes] == ["crlf.txt"]


def test_oversized_changes_are_refused(hostile: Hostile, tmp_path: Path) -> None:
    small = GitWriteRunner(hostile.repo, max_diff_bytes=2000)
    path = wt(tmp_path)
    small.worktree_add(path, "aios/PLAN-0001", hostile.base)
    (path / "big.txt").write_text("x" * 50_000)
    with pytest.raises(GitError, match="larger than"):
        small.changes(path, hostile.base)


@pytest.mark.skipif(not POSIX, reason="uses a shell script as a fake git")
def test_a_slow_git_is_stopped(hostile: Hostile, tmp_path: Path) -> None:
    slow = tmp_path / "slow-git"
    slow.write_text("#!/bin/sh\nsleep 30\n", newline="\n")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    impatient = GitWriteRunner(hostile.repo, executable=str(slow), timeout_s=0.3)
    started = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        impatient.worktree_list()
    assert time.monotonic() - started < 10  # not held up by the 30 s sleep git "started"


def test_the_scratch_directory_is_removed_with_the_runner(hostile: Hostile) -> None:
    r = GitWriteRunner(hostile.repo)
    scratch = r._scratch
    assert scratch.is_dir()
    del r
    gc.collect()
    assert not scratch.exists()


def test_git_must_exist(hostile: Hostile, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(GitError, match="cannot run git"):
        GitWriteRunner(hostile.repo, executable=str(hostile.repo / "no-git")).worktree_list()
    monkeypatch.setenv("PATH", str(hostile.repo / "empty"))
    with pytest.raises(GitError, match="not found"):
        GitWriteRunner(hostile.repo)
