"""Results of the hardened git write runner (worktrees and change snapshots)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WorktreeInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    head: str | None
    branch: str | None  # short name, e.g. "aios/PLAN-0001"; None when detached


class Change(BaseModel):
    """One path that differs between the base commit and the worktree."""

    model_config = ConfigDict(frozen=True)

    path: str
    status: str  # A added, M modified, D deleted, T type changed
    old_mode: str
    new_mode: str
    binary: bool

    @property
    def is_symlink(self) -> bool:
        return "120000" in (self.old_mode, self.new_mode)

    @property
    def is_gitlink(self) -> bool:
        return "160000" in (self.old_mode, self.new_mode)

    @property
    def mode_changed(self) -> bool:
        both = self.status == "M" and self.old_mode != self.new_mode
        return both or self.status == "T"


class WorktreeChanges(BaseModel):
    model_config = ConfigDict(frozen=True)

    base: str
    changes: list[Change]
    #: Git-ignored files present in the worktree. A fresh worktree has none, so all are new.
    ignored: list[str]
    diff_sha256: str
    diff_bytes: int
