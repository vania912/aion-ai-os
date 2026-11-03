"""Models for the Git service. All values are read-only snapshots."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True)


class GitConfig(BaseModel):
    model_config = _FROZEN

    max_commits: int = 100
    max_diff_files: int = 50
    max_diff_chars: int = 40_000
    max_body_chars: int = 4_000
    max_status_entries: int = 200
    max_since_chars: int = 64
    max_query_chars: int = 200
    timeout_s: float = 30.0


class ChangedFile(BaseModel):
    model_config = _FROZEN

    path: str
    status: str  # A added, M modified, D deleted, T type change (renames are reported as D + A)


class CommitSummary(BaseModel):
    model_config = _FROZEN

    hash: str
    short_hash: str
    author: str
    date: str  # strict ISO 8601
    subject: str


class CommitChanges(CommitSummary):
    files: list[ChangedFile]
    hidden_files: int  # changed files withheld by the access policy (names are never revealed)


class RecentChanges(BaseModel):
    model_config = _FROZEN

    commits: list[CommitChanges]
    truncated: bool


class CommitDetail(CommitChanges):
    body: str
    body_truncated: bool
    files_omitted: int  # visible files not included in the diff because of max_diff_files
    diff: str
    diff_truncated: bool


class DiffResult(BaseModel):
    model_config = _FROZEN

    base: str
    head: str
    files: list[ChangedFile]
    hidden_files: int
    files_omitted: int
    diff: str
    diff_truncated: bool


class HistoryHits(BaseModel):
    model_config = _FROZEN

    mode: Literal["message", "content", "path"]
    commits: list[CommitSummary]
    truncated: bool


class StatusEntry(BaseModel):
    model_config = _FROZEN

    path: str
    index: str  # X column of porcelain status ('.' = unchanged)
    worktree: str  # Y column


class BranchStatus(BaseModel):
    model_config = _FROZEN

    branch: str | None
    detached: bool
    head: str | None  # None in a repository without commits
    upstream: str | None
    ahead: int | None
    behind: int | None
    staged: int
    unstaged: int
    untracked: int
    conflicted: int
    entries: list[StatusEntry]
    untracked_paths: list[str]
    hidden_files: int
    clean: bool
    truncated: bool


class BranchInfo(BaseModel):
    model_config = _FROZEN

    name: str
    hash: str
    current: bool


class BranchList(BaseModel):
    model_config = _FROZEN

    branches: list[BranchInfo]
    truncated: bool
