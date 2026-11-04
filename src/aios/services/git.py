"""Git service: read-only history, diffs and status for one registered project.

Everything that comes out of git is filtered through the policy engine: a changed file the
policy would not let you read never appears in a file list, a patch, or a search hit, and its
name is not revealed (only a ``hidden_files`` count). See docs/GIT.md.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from aios.adapters.git_cli import GitRunner
from aios.domain.git import (
    BranchInfo,
    BranchList,
    BranchStatus,
    ChangedFile,
    CommitChanges,
    CommitDetail,
    CommitSummary,
    DiffResult,
    GitConfig,
    HistoryHits,
    RecentChanges,
    StatusEntry,
)
from aios.domain.models import Registry
from aios.errors import GitError, InvalidRequestError
from aios.policy.files import FileAccessPolicy, PathGuard

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Refs: no leading '-', no ':' (would allow 'rev:path' blob reads that bypass the file policy),
# no whitespace/control characters, no range operators.
_REF = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9._/@^~{}+-]{0,199}\Z")
_SINCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .:+,/-]*\Z")
_FS, _RS = "\x1f", "\x1e"
_LOG_FORMAT = f"%H{_FS}%an{_FS}%aI{_FS}%s{_RS}"


class GitService:
    def __init__(
        self,
        guard: PathGuard,
        runner: GitRunner | None = None,
        config: GitConfig | None = None,
    ) -> None:
        self._guard = guard
        self._cfg = config if config is not None else GitConfig()
        self._git = runner or GitRunner(guard.root, timeout_s=self._cfg.timeout_s)
        # Fail early (and clearly) if the project root is not itself a repository. Discovery
        # is capped at the root's parent, so an enclosing repository is never picked up.
        if self._git.run(["rev-parse", "--is-inside-work-tree"], ok_codes=(0, 128)).returncode != 0:
            raise GitError("project root is not a git repository")

    # -- public operations ---------------------------------------------------------------

    def get_recent_changes(
        self, since: str | None = None, limit: int = 20, path: str | None = None
    ) -> RecentChanges:
        self._check_limit(limit)
        if not self._has_commits():
            return RecentChanges(commits=[], truncated=False)
        args = ["log", f"--max-count={limit + 1}", f"--format={_LOG_FORMAT}"]
        args += self._since_args(since)
        args += self._path_tail(path)
        summaries = self._parse_log(self._text(args))
        truncated = len(summaries) > limit
        commits = []
        for summary in summaries[:limit]:
            files, hidden = self._commit_files(summary.hash)
            commits.append(CommitChanges(**summary.model_dump(), files=files, hidden_files=hidden))
        return RecentChanges(commits=commits, truncated=truncated)

    def get_commit(self, ref: str) -> CommitDetail:
        commit = self._resolve(ref)
        fmt = _FS.join(["%H", "%an", "%aI", "%s", "%b"])
        meta = self._text(["show", "-s", f"--format={fmt}", commit]).rstrip("\n")
        head_hash, author, date, subject, body = (meta.split(_FS, 4) + [""] * 5)[:5]
        files, hidden = self._commit_files(commit)
        diff, diff_truncated, omitted = self._patch(
            ["show", "--format=", "--patch", *self._patch_flags(), commit], files
        )
        body = body.strip()
        body_truncated = len(body) > self._cfg.max_body_chars
        return CommitDetail(
            hash=head_hash,
            short_hash=head_hash[:10],
            author=author,
            date=date,
            subject=subject,
            files=files,
            hidden_files=hidden,
            body=body[: self._cfg.max_body_chars],
            body_truncated=body_truncated,
            files_omitted=omitted,
            diff=diff,
            diff_truncated=diff_truncated,
        )

    def get_diff(self, base: str, head: str = "HEAD", path: str | None = None) -> DiffResult:
        base_hash, head_hash = self._resolve(base), self._resolve(head)
        tail = self._path_tail(path)
        names = self._text(
            ["diff", "--name-status", "-z", "--no-renames", base_hash, head_hash, *tail]
        )
        files, hidden = self._filter_files(_parse_name_status(names))
        diff, diff_truncated, omitted = self._patch(
            ["diff", "--patch", *self._patch_flags(), base_hash, head_hash], files
        )
        return DiffResult(
            base=base_hash,
            head=head_hash,
            files=files,
            hidden_files=hidden,
            files_omitted=omitted,
            diff=diff,
            diff_truncated=diff_truncated,
        )

    def search_history(
        self,
        query: str,
        mode: Literal["message", "content", "path"] = "message",
        *,
        since: str | None = None,
        limit: int = 20,
    ) -> HistoryHits:
        """Find commits by message, by changed content (pickaxe), or by path.

        ``content`` searches only files the policy allows: a pickaxe over denied files would
        let a caller confirm guessed secrets by watching which commits come back.
        """
        self._check_limit(limit)
        if mode not in ("message", "content", "path"):
            raise InvalidRequestError("mode must be 'message', 'content' or 'path'")
        if not self._has_commits():
            return HistoryHits(mode=mode, commits=[], truncated=False)
        args = ["log", f"--max-count={limit + 1}", f"--format={_LOG_FORMAT}"]
        args += self._since_args(since)
        if mode == "path":
            args += self._path_tail(query)
        else:
            self._check_query(query)
            if mode == "message":
                args += ["--fixed-strings", "--regexp-ignore-case", f"--grep={query}"]
            else:
                args += [f"-S{query}", "--", *self._content_pathspecs(self._guard.policy)]
        summaries = self._parse_log(self._text(args))
        return HistoryHits(mode=mode, commits=summaries[:limit], truncated=len(summaries) > limit)

    def get_branch_status(self) -> BranchStatus:
        out = self._text(["status", "--porcelain=v2", "--branch", "-z"])
        branch: str | None = None
        detached = False
        head: str | None = None
        upstream: str | None = None
        ahead = behind = None
        staged = unstaged = conflicted = hidden = 0
        entries: list[StatusEntry] = []
        untracked_paths: list[str] = []
        untracked_total = 0
        truncated = False
        cap = self._cfg.max_status_entries

        tokens = out.split("\x00")
        i = 0
        while i < len(tokens):
            line = tokens[i]
            i += 1
            if not line:
                continue
            if line.startswith("# branch.oid "):
                oid = line.split(" ", 2)[2]
                head = None if oid == "(initial)" else oid
            elif line.startswith("# branch.head "):
                name = line.split(" ", 2)[2]
                detached = name == "(detached)"
                branch = None if detached else name
            elif line.startswith("# branch.upstream "):
                upstream = line.split(" ", 2)[2]
            elif line.startswith("# branch.ab "):
                _, _, plus, minus = line.split(" ")
                ahead, behind = int(plus[1:]), int(minus[1:])
            elif line.startswith("? "):
                untracked_total += 1
                raw_path = line[2:]
                is_dir = raw_path.endswith("/")
                if not self._guard.is_repo_path_visible(raw_path, is_dir=is_dir):
                    hidden += 1
                elif len(untracked_paths) < cap:
                    untracked_paths.append(raw_path)
                else:
                    truncated = True
            elif line[:2] in ("1 ", "2 ", "u "):
                kind = line[0]
                fields = line.split(" ", {"1": 8, "2": 9, "u": 10}[kind])
                xy, path = fields[1], fields[-1]
                if kind == "2":
                    i += 1  # renames are disabled, but consume the extra "original path" token
                if kind == "u":
                    conflicted += 1
                else:
                    staged += xy[0] != "."
                    unstaged += xy[1] != "."
                if not self._guard.is_repo_path_visible(path):
                    hidden += 1
                elif len(entries) < cap:
                    entries.append(StatusEntry(path=path, index=xy[0], worktree=xy[1]))
                else:
                    truncated = True
        return BranchStatus(
            branch=branch,
            detached=detached,
            head=head,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            staged=staged,
            unstaged=unstaged,
            untracked=untracked_total,
            conflicted=conflicted,
            entries=entries,
            untracked_paths=untracked_paths,
            hidden_files=hidden,
            clean=not (entries or untracked_paths or hidden or conflicted),
            truncated=truncated,
        )

    def list_branches(self, *, remotes: bool = False, limit: int = 50) -> BranchList:
        self._check_limit(limit)
        fmt = f"%(refname:short){_FS}%(objectname){_FS}%(HEAD){_RS}"
        ref = "refs/remotes" if remotes else "refs/heads"
        out = self._text(
            ["for-each-ref", "--sort=refname", f"--count={limit + 1}", f"--format={fmt}", ref]
        )
        branches = []
        for record in out.split(_RS):
            record = record.strip("\n")
            if not record:
                continue
            name, obj, marker = record.split(_FS)
            branches.append(BranchInfo(name=name, hash=obj, current=marker == "*"))
        return BranchList(branches=branches[:limit], truncated=len(branches) > limit)

    # -- internals -----------------------------------------------------------------------

    def _text(self, args: Sequence[str], *, max_bytes: int | None = None) -> str:
        out = self._git.run(args, max_bytes=max_bytes)
        return out.stdout.decode("utf-8", errors="replace")

    def _has_commits(self) -> bool:
        return (
            self._git.run(
                ["rev-parse", "--verify", "--quiet", "HEAD"], ok_codes=(0, 1, 128)
            ).returncode
            == 0
        )

    def _resolve(self, ref: str) -> str:
        if not _REF.match(ref) or ".." in ref:
            raise InvalidRequestError(
                "ref must be a branch, tag, hash or HEAD-relative name "
                "(no ':', '..' or leading '-')"
            )
        out = self._git.run(
            ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], ok_codes=(0, 1, 128)
        )
        if out.returncode != 0:
            raise InvalidRequestError(f"unknown revision: {ref!r}")
        return out.stdout.decode().strip()

    def _check_limit(self, limit: int) -> None:
        if not 1 <= limit <= self._cfg.max_commits:
            raise InvalidRequestError(f"limit must be between 1 and {self._cfg.max_commits}")

    def _check_query(self, query: str) -> None:
        if not query or len(query) > self._cfg.max_query_chars or _CONTROL_CHARS.search(query):
            raise InvalidRequestError(
                f"query must be 1-{self._cfg.max_query_chars} characters without control characters"
            )

    def _since_args(self, since: str | None) -> list[str]:
        if since is None:
            return []
        if len(since) > self._cfg.max_since_chars or not _SINCE.match(since):
            raise InvalidRequestError("since must be a simple date expression, e.g. '2 weeks ago'")
        return [f"--since={since}"]

    def _path_tail(self, path: str | None) -> list[str]:
        """Pathspec tail for one vetted repo path ('--' separator and literal magic)."""
        if path is None:
            return []
        return ["--", f":(literal){self._guard.vet_repo_path(path)}"]

    @staticmethod
    def _patch_flags() -> list[str]:
        return ["--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]

    @staticmethod
    def _parse_log(text: str) -> list[CommitSummary]:
        out = []
        for record in text.split(_RS):
            record = record.strip("\n")
            if not record:
                continue
            h, author, date, subject = record.split(_FS)
            out.append(
                CommitSummary(hash=h, short_hash=h[:10], author=author, date=date, subject=subject)
            )
        return out

    def _commit_files(self, commit: str) -> tuple[list[ChangedFile], int]:
        names = self._text(
            [
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-z",
                "--no-renames",
                "--root",
                commit,
            ],
        )
        return self._filter_files(_parse_name_status(names))

    def _filter_files(self, changed: list[ChangedFile]) -> tuple[list[ChangedFile], int]:
        visible = [f for f in changed if self._guard.is_repo_path_visible(f.path)]
        return visible, len(changed) - len(visible)

    def _patch(self, base_args: list[str], files: list[ChangedFile]) -> tuple[str, bool, int]:
        """Patch restricted to the allowed files (at most ``max_diff_files`` of them)."""
        if not files:
            return "", False, 0
        shown = files[: self._cfg.max_diff_files]
        omitted = len(files) - len(shown)
        cap = self._cfg.max_diff_chars
        text = self._text(
            [*base_args, "--", *(f":(literal){f.path}" for f in shown)],
            max_bytes=cap * 4 + 4096,
        )
        if len(text) > cap:
            return text[:cap], True, omitted
        return text, False, omitted

    @staticmethod
    def _content_pathspecs(policy: FileAccessPolicy) -> list[str]:
        """Include only allowed file types; exclude denied names and directories."""
        includes = [f":(glob,icase)**/*{ext}" for ext in sorted(policy.allowed_extensions)]
        includes += [f":(glob,icase)**/{name}" for name in sorted(policy.allowed_filenames)]
        excludes = [f":(exclude,glob,icase)**/{p}" for p in policy.denied_name_patterns]
        excludes += [f":(exclude,glob,icase)**/{d}/**" for d in sorted(policy.denied_dir_names)]
        return [*includes, *excludes]


def _parse_name_status(text: str) -> list[ChangedFile]:
    """Parse ``--name-status -z`` output: STATUS NUL PATH NUL ... (renames are disabled)."""
    tokens = text.split("\x00")
    out = []
    for status, path in zip(tokens[0::2], tokens[1::2], strict=False):
        if status and path:
            out.append(ChangedFile(path=path, status=status[0]))
    return out


def build_git_service(
    registry: Registry,
    project_id: str,
    *,
    policy: FileAccessPolicy | None = None,
    config: GitConfig | None = None,
    runner: GitRunner | None = None,
) -> GitService:
    """Create a GitService for a registered project (raises UnknownProjectError)."""
    project = registry.get(project_id)
    if not project.git:
        raise GitError(f"project {project_id!r} is registered with git: false")
    guard = PathGuard(project.path, policy)
    return GitService(guard, runner, config)
