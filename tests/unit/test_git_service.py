from __future__ import annotations

import os
from pathlib import Path

import pytest

from aios.adapters.git_cli import GitRunner
from aios.errors import GitError, InvalidRequestError, PolicyViolation
from aios.policy import PathGuard
from aios.services.git import GitService
from tests.git_helpers import SECRET, History, commit, make_history, run_git

posix_only = pytest.mark.skipif(os.name != "posix", reason="uses POSIX shell scripts")


@pytest.fixture
def history(tmp_path: Path) -> History:
    return make_history(tmp_path)


@pytest.fixture
def git(history: History) -> GitService:
    return GitService(PathGuard(history.repo))


def _no_secret(*texts: str) -> None:
    for text in texts:
        assert "hunter2" not in text and "SECRET_VALUE" not in text and ".env" not in text


# -- recent changes -----------------------------------------------------------------------


def test_recent_changes_newest_first_with_filtered_files(git: GitService, history: History) -> None:
    result = git.get_recent_changes()
    assert [c.hash for c in result.commits] == [
        history.c5,
        history.c4,
        history.c3,
        history.c2,
        history.c1,
    ]
    by_hash = {c.hash: c for c in result.commits}
    assert [(f.path, f.status) for f in by_hash[history.c1].files] == [
        ("README.md", "A"),
        ("src/app.py", "A"),
    ]
    assert [(f.path, f.status) for f in by_hash[history.c2].files] == [("src/app.py", "M")]
    assert by_hash[history.c2].hidden_files == 1  # .env
    assert [(f.path, f.status) for f in by_hash[history.c3].files] == [("README.md", "M")]
    assert by_hash[history.c3].hidden_files == 2  # server.pem + logo.png (type not allowed)
    assert by_hash[history.c4].files == [] and by_hash[history.c4].hidden_files == 1
    assert by_hash[history.c5].files == [] and by_hash[history.c5].hidden_files == 1
    _no_secret(result.model_dump_json())


def test_recent_changes_metadata(git: GitService, history: History) -> None:
    first = git.get_recent_changes(limit=1).commits[0]
    assert first.subject == "only secret" and first.author == "Test Author"
    assert first.date.startswith("2026-01-04T10:00:00") and first.short_hash == history.c5[:10]


def test_recent_changes_limit_and_truncation(git: GitService) -> None:
    two = git.get_recent_changes(limit=2)
    assert len(two.commits) == 2 and two.truncated
    five = git.get_recent_changes(limit=5)
    assert len(five.commits) == 5 and not five.truncated


def test_recent_changes_since(git: GitService) -> None:
    assert len(git.get_recent_changes(since="2025-01-01").commits) == 4


def test_recent_changes_path_filter(git: GitService, history: History) -> None:
    subjects = [c.subject for c in git.get_recent_changes(path="src/app.py").commits]
    assert subjects == ["add env config", "initial: add app"]


@pytest.mark.parametrize(
    "path", [".env", ".git/config", "server.pem", "logo.png", "../x", "/etc/passwd", "a:b"]
)
def test_recent_changes_rejects_unreadable_paths(git: GitService, path: str) -> None:
    with pytest.raises(PolicyViolation):
        git.get_recent_changes(path=path)


@pytest.mark.parametrize("since", ["--output=x", "-1", "a;b", "$(id)", "x" * 100, ""])
def test_recent_changes_validates_since(git: GitService, since: str) -> None:
    with pytest.raises(InvalidRequestError):
        git.get_recent_changes(since=since)


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_limit_validation(git: GitService, limit: int) -> None:
    with pytest.raises(InvalidRequestError):
        git.get_recent_changes(limit=limit)


# -- single commit ------------------------------------------------------------------------


def test_get_commit_hides_protected_files_and_their_content(
    git: GitService, history: History
) -> None:
    detail = git.get_commit(history.c2)
    assert detail.subject == "add env config" and detail.hash == history.c2
    assert [f.path for f in detail.files] == ["src/app.py"] and detail.hidden_files == 1
    assert "app_marker v2" in detail.diff and "+++ b/src/app.py" in detail.diff
    _no_secret(detail.diff, detail.model_dump_json())


def test_get_commit_touching_only_hidden_files(git: GitService, history: History) -> None:
    detail = git.get_commit(history.c5)
    assert detail.files == [] and detail.diff == "" and detail.hidden_files == 1


def test_get_commit_with_hidden_binary_and_pem(git: GitService, history: History) -> None:
    detail = git.get_commit(history.c3)
    assert [f.path for f in detail.files] == ["README.md"] and detail.hidden_files == 2
    assert "hello workspace" in detail.diff
    assert "BEGIN" not in detail.diff and "PNG" not in detail.diff


def test_get_commit_root_commit(git: GitService, history: History) -> None:
    detail = git.get_commit(history.c1)
    assert {f.path for f in detail.files} == {"README.md", "src/app.py"}
    assert "app_marker v1" in detail.diff


@pytest.mark.parametrize("ref_for", ["HEAD", "HEAD~1", "HEAD^", "main", "v1", "short"])
def test_get_commit_accepts_ordinary_refs(git: GitService, history: History, ref_for: str) -> None:
    ref = history.c3[:8] if ref_for == "short" else ref_for
    assert git.get_commit(ref).hash in {history.c5, history.c4, history.c3}


@pytest.mark.parametrize(
    "ref",
    ["--output=/tmp/x", "-x", "HEAD:.env", "main:src/app.py", "v1:server.pem", "a..b", "a...b", "",
     "has space", "a\nb", "x" * 300, "$(id)", "a;b", "a|b", "HEAD@{upstream}:x", "\\x"],
)  # fmt: skip
def test_get_commit_rejects_dangerous_refs(git: GitService, ref: str) -> None:
    # Must be refused by *validation*, before git is asked (not merely fail to resolve).
    with pytest.raises(InvalidRequestError, match="ref must be"):
        git.get_commit(ref)


def test_get_commit_unknown_revision(git: GitService) -> None:
    with pytest.raises(InvalidRequestError, match="unknown revision"):
        git.get_commit("no-such-branch")


def test_get_commit_truncates_big_diffs(history: History) -> None:
    from aios.domain.git import GitConfig

    big = commit(history.repo, "big", "2026-02-01T10:00:00+00:00", {"big.py": "x = 1\n" * 20_000})
    svc = GitService(PathGuard(history.repo), config=GitConfig(max_diff_chars=1000))
    detail = svc.get_commit(big)
    assert detail.diff_truncated and len(detail.diff) == 1000


def test_get_commit_limits_files_in_diff(history: History) -> None:
    from aios.domain.git import GitConfig

    many = commit(
        history.repo, "many", "2026-02-02T10:00:00+00:00", {f"f{i}.py": f"v{i}\n" for i in range(5)}
    )
    detail = GitService(PathGuard(history.repo), config=GitConfig(max_diff_files=2)).get_commit(
        many
    )
    assert len(detail.files) == 5 and detail.files_omitted == 3
    assert detail.diff.count("diff --git") == 2


# -- diff ---------------------------------------------------------------------------------


def test_diff_between_refs_filters_protected_files(git: GitService, history: History) -> None:
    result = git.get_diff(history.c1, history.c3)
    assert [f.path for f in result.files] == ["README.md", "src/app.py"]
    assert result.hidden_files == 3  # .env, server.pem, logo.png
    assert "hello workspace" in result.diff and "app_marker v2" in result.diff
    _no_secret(result.diff)
    assert "BEGIN" not in result.diff


def test_diff_to_head_with_only_hidden_changes(git: GitService, history: History) -> None:
    result = git.get_diff(history.c4)
    assert result.files == [] and result.diff == "" and result.hidden_files == 1
    assert result.head == history.c5


def test_diff_path_filter_and_validation(git: GitService, history: History) -> None:
    only = git.get_diff(history.c1, history.c3, path="src/app.py")
    assert [f.path for f in only.files] == ["src/app.py"]
    for bad in (".env", "../x", "logo.png", ".git/HEAD"):
        with pytest.raises(PolicyViolation):
            git.get_diff(history.c1, history.c3, path=bad)


def test_diff_rejects_bad_refs(git: GitService) -> None:
    for base, head in (("HEAD:.env", "HEAD"), ("HEAD", "--output=x"), ("a..b", "HEAD")):
        with pytest.raises(InvalidRequestError):
            git.get_diff(base, head)


# -- history search -----------------------------------------------------------------------


def test_search_by_message(git: GitService, history: History) -> None:
    hits = git.search_history("DOCS", "message")
    assert [c.hash for c in hits.commits] == [history.c3] and not hits.truncated


def test_search_by_content_finds_visible_changes(git: GitService, history: History) -> None:
    hits = git.search_history("app_marker v2", "content")
    assert [c.hash for c in hits.commits] == [history.c2]


def test_content_search_is_not_an_oracle_for_protected_files(
    git: GitService, history: History
) -> None:
    # Premise: unrestricted pickaxe DOES find the secret in history (.env, .pem, .png, .env.local).
    raw = run_git(history.repo, "log", "-S", "hunter2", "--format=%H").split()
    assert set(raw) == {history.c2, history.c3, history.c4, history.c5}
    assert git.search_history("hunter2", "content").commits == []
    assert git.search_history(SECRET, "content").commits == []


def test_search_by_path(git: GitService, history: History) -> None:
    hits = git.search_history("README.md", "path")
    assert [c.hash for c in hits.commits] == [history.c3, history.c1]
    for bad in (".env", "../x", "logo.png"):
        with pytest.raises(PolicyViolation):
            git.search_history(bad, "path")


def test_search_validation_and_option_injection(git: GitService, history: History) -> None:
    for query in ("", "x" * 300, "a\x00b", "a\nb"):
        with pytest.raises(InvalidRequestError):
            git.search_history(query, "message")
    with pytest.raises(InvalidRequestError):
        git.search_history("x", "bogus")  # type: ignore[arg-type]
    # A query that looks like an option is just text.
    assert git.search_history("--output=/tmp/should-not-exist", "message").commits == []
    assert not Path("/tmp/should-not-exist").exists()
    assert git.search_history("-p", "content").commits == []


def test_search_truncation(git: GitService) -> None:
    hits = git.search_history("e", "message", limit=1)  # matches many subjects
    assert len(hits.commits) == 1 and hits.truncated


# -- status and branches ------------------------------------------------------------------


def test_status_clean(git: GitService, history: History) -> None:
    status = git.get_branch_status()
    assert (status.branch, status.detached, status.head) == ("main", False, history.c5)
    assert status.clean and status.upstream is None and status.ahead is None
    assert (status.staged, status.unstaged, status.untracked) == (0, 0, 0)


def test_status_reports_changes_and_hides_protected_untracked(
    git: GitService, history: History
) -> None:
    repo = history.repo
    (repo / "src" / "app.py").write_text("changed\n")
    (repo / "README.md").write_text("staged\n")
    run_git(repo, "add", "README.md")
    (repo / "new.txt").write_text("n\n")
    (repo / ".env").write_text(SECRET + "\n")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "a.py").write_text("a\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "server.pem").write_text("k\n")

    status = git.get_branch_status()
    assert not status.clean
    assert (status.staged, status.unstaged) == (1, 1)
    assert {(e.path, e.index, e.worktree) for e in status.entries} == {
        ("README.md", "M", "."),
        ("src/app.py", ".", "M"),
    }
    assert set(status.untracked_paths) == {"new.txt", "newdir/", "secrets/"}
    assert status.hidden_files >= 1  # .env (the secrets/ directory itself is listed as a dir)
    _no_secret(status.model_dump_json())


def test_status_detached_and_upstream(git: GitService, history: History) -> None:
    repo = history.repo
    run_git(repo, "checkout", "-q", "-b", "feature")
    run_git(repo, "branch", "--set-upstream-to=main")
    commit(repo, "feature work", "2026-03-01T10:00:00+00:00", {"feat.py": "f\n"})
    status = git.get_branch_status()
    assert (status.branch, status.upstream, status.ahead, status.behind) == (
        "feature",
        "main",
        1,
        0,
    )

    run_git(repo, "checkout", "-q", "main")
    commit(repo, "main moves", "2026-03-02T10:00:00+00:00", {"m.py": "m\n"})
    run_git(repo, "checkout", "-q", "feature")
    status = git.get_branch_status()
    assert (status.ahead, status.behind) == (1, 1)

    run_git(repo, "checkout", "-q", "--detach", history.c3)
    status = git.get_branch_status()
    assert status.detached and status.branch is None and status.head == history.c3


def test_list_branches(git: GitService, history: History) -> None:
    run_git(history.repo, "branch", "other")
    branches = git.list_branches()
    assert [(b.name, b.current) for b in branches.branches] == [("main", True), ("other", False)]
    assert git.list_branches(limit=1).truncated
    assert git.list_branches(remotes=True).branches == []


# -- repository edge cases ----------------------------------------------------------------


def test_empty_repository(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    svc = GitService(PathGuard(repo))
    assert svc.get_recent_changes().commits == []
    assert svc.search_history("x", "message").commits == []
    status = svc.get_branch_status()
    assert (status.branch, status.head, status.clean) == ("main", None, True)
    with pytest.raises(InvalidRequestError, match="unknown revision"):
        svc.get_commit("HEAD")


def test_directory_without_dot_git_is_refused_even_inside_a_repo(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    (parent / "sub").mkdir(parents=True)
    run_git(parent, "init", "-q", "-b", "main")
    with pytest.raises(GitError, match="not a git repository"):
        GitService(PathGuard(parent / "sub"))


# -- hardening against hostile repository config ------------------------------------------


def _marker_script(tmp_path: Path, name: str, extra: str = "") -> tuple[Path, Path]:
    marker = tmp_path / f"{name}.marker"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n{extra}\n")
    script.chmod(0o755)
    return script, marker


@posix_only
def test_status_does_not_run_fsmonitor_programs(
    git: GitService, history: History, tmp_path: Path
) -> None:
    script, marker = _marker_script(tmp_path, "fsmon")
    run_git(history.repo, "config", "core.fsmonitor", str(script))
    git.get_branch_status()
    assert not marker.exists()


@posix_only
def test_diff_does_not_run_external_diff_programs(
    git: GitService, history: History, tmp_path: Path
) -> None:
    script, marker = _marker_script(tmp_path, "extdiff")
    run_git(history.repo, "config", "diff.external", str(script))
    run_git(history.repo, "diff", history.c1, history.c3)  # sanity: unhardened git runs it
    assert marker.exists(), "test premise failed"
    marker.unlink()
    result = git.get_diff(history.c1, history.c3)
    assert not marker.exists() and "hello workspace" in result.diff


@posix_only
def test_diff_does_not_run_textconv_programs(
    git: GitService, history: History, tmp_path: Path
) -> None:
    script, marker = _marker_script(tmp_path, "textconv", extra='cat "$1"')
    (history.repo / ".gitattributes").write_text("*.py diff=evil\n")
    run_git(history.repo, "config", "diff.evil.textconv", str(script))
    run_git(history.repo, "show", history.c2, "--patch")  # sanity: unhardened git runs it
    assert marker.exists(), "test premise failed"
    marker.unlink()
    detail = git.get_commit(history.c2)
    assert not marker.exists() and "app_marker v2" in detail.diff


def test_service_for_registry_rejects_non_git_projects(tmp_path: Path) -> None:
    from aios.domain.models import Registry
    from aios.services.git import build_git_service

    registry = Registry.model_validate(
        {"projects": {"plain": {"path": str(tmp_path), "git": False}}}
    )
    with pytest.raises(GitError, match="git: false"):
        build_git_service(registry, "plain")


def test_runner_can_be_injected(history: History) -> None:
    runner = GitRunner(history.repo)
    svc = GitService(PathGuard(history.repo), runner)
    assert svc.get_recent_changes(limit=1).commits[0].hash == history.c5


def test_status_does_not_write_to_the_repository(git: GitService, history: History) -> None:
    index = history.repo / ".git" / "index"
    target = history.repo / "README.md"

    def make_stat_dirty() -> None:
        # Same content, new mtime: plain `git status` normally refreshes (rewrites) the index.
        stat = target.stat()
        os.utime(target, (stat.st_atime + 5, stat.st_mtime + 5))

    make_stat_dirty()
    before = index.read_bytes()
    git.get_branch_status()
    assert index.read_bytes() == before

    run_git(history.repo, "status")  # premise: unhardened git DOES rewrite the index here
    assert index.read_bytes() != before
