from __future__ import annotations

import os
from pathlib import Path

import pytest

from aios.adapters.git_cli import ALLOWED_SUBCOMMANDS, GitRunner
from aios.errors import GitError
from tests.git_helpers import run_git

posix_only = pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell script as fake git")


def _fake_git(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_git"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "r"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    return path


@pytest.mark.parametrize(
    "sub",
    ["push", "commit", "config", "checkout", "fetch", "clean", "reset", "branch", "tag", "remote",
     "gc", "apply", "am", "rebase", "merge", "pull", "add", "rm", "mv", "stash", "init", "clone",
     "submodule", "worktree", "update-ref", "hash-object", "cat-file", ""],
)  # fmt: skip
def test_only_allowlisted_subcommands(sub: str) -> None:
    assert sub not in ALLOWED_SUBCOMMANDS
    with pytest.raises(GitError, match="not allowed"):
        GitRunner.validate([sub, "x"] if sub else [])


_DANGEROUS = [
    "--output=/tmp/x", "--output", "-O/tmp/orderfile", "--no-index", "--git-dir=/x",
    "--work-tree=/x", "-c", "-C", "--ext-diff", "--textconv", "--exec-path=/x", "--paginate",
    "--upload-pack=x", "--config",
]  # fmt: skip


@pytest.mark.parametrize("arg", _DANGEROUS)
def test_dangerous_options_refused(arg: str) -> None:
    with pytest.raises(GitError, match="not allowed"):
        GitRunner.validate(["diff", arg])


def test_options_after_double_dash_are_pathspecs_not_options() -> None:
    GitRunner.validate(["log", "--", "--output=x", "-O"])  # must not raise


def test_nul_byte_refused() -> None:
    with pytest.raises(GitError, match="NUL"):
        GitRunner.validate(["log", "a\x00b"])


def test_allowlisted_commands_run(repo: Path) -> None:
    out = GitRunner(repo).run(["rev-parse", "--is-inside-work-tree"])
    assert out.returncode == 0 and out.stdout.strip() == b"true"


def test_nonzero_exit_raises_unless_expected(repo: Path) -> None:
    runner = GitRunner(repo)
    with pytest.raises(GitError, match="failed"):
        runner.run(["rev-parse", "--verify", "HEAD"])
    assert runner.run(["rev-parse", "--verify", "--quiet", "HEAD"], ok_codes=(0, 1)).returncode == 1


def test_parent_environment_git_variables_are_ignored(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    run_git(decoy, "init", "-q", "-b", "main")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    top = GitRunner(repo).run(["rev-parse", "--show-toplevel"]).stdout.decode().strip()
    assert os.path.samefile(top, repo), f"git was redirected to {top}"


def test_repository_discovery_stops_at_the_project_root(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    (parent / "sub").mkdir(parents=True)
    run_git(parent, "init", "-q", "-b", "main")
    out = GitRunner(parent / "sub").run(["rev-parse", "--is-inside-work-tree"], ok_codes=(0, 128))
    assert out.returncode == 128  # the enclosing repository must not be picked up


@posix_only
def test_timeout_kills_git(repo: Path, tmp_path: Path) -> None:
    runner = GitRunner(repo, executable=_fake_git(tmp_path, "exec sleep 30"), timeout_s=0.3)
    with pytest.raises(GitError, match="timed out"):
        runner.run(["log"])


@posix_only
def test_output_cap_truncates_and_stops(repo: Path, tmp_path: Path) -> None:
    runner = GitRunner(repo, executable=_fake_git(tmp_path, "exec head -c 5000000 /dev/zero"))
    out = runner.run(["log"], max_bytes=1000)
    assert out.truncated and len(out.stdout) == 1000


@posix_only
def test_error_messages_do_not_leak_the_root(repo: Path, tmp_path: Path) -> None:
    body = f"echo 'fatal: bad object in {repo}/.git' >&2\nexit 128"
    with pytest.raises(GitError) as info:
        GitRunner(repo, executable=_fake_git(tmp_path, body)).run(["log"])
    assert str(repo) not in str(info.value) and "<root>" in str(info.value)


@posix_only
def test_config_that_executes_programs_is_neutralised(repo: Path, tmp_path: Path) -> None:
    """A repo-level config can make plain `git status` run a program. The runner must not."""
    marker = tmp_path / "pwned"
    script = tmp_path / "hook.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    (repo / "a.txt").write_text("x\n")
    run_git(repo, "config", "core.fsmonitor", str(script))

    run_git(repo, "status")  # sanity: unhardened git DOES execute it
    assert marker.exists(), "test premise failed: git did not run the configured program"
    marker.unlink()

    GitRunner(repo).run(["status", "--porcelain=v2"])
    assert not marker.exists()
