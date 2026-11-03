"""Project context snapshot: a small, high-signal summary of one project's current state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aios.domain.git import CommitChanges
from aios.domain.memory import MemoryHit

_FROZEN = ConfigDict(frozen=True)


class GitSummary(BaseModel):
    model_config = _FROZEN

    branch: str | None
    detached: bool
    head: str | None
    upstream: str | None
    ahead: int | None
    behind: int | None
    clean: bool
    staged: int
    unstaged: int
    untracked: int


class ProjectContext(BaseModel):
    model_config = _FROZEN

    project: str
    description: str
    git: GitSummary | None
    recent_changes: list[CommitChanges] = Field(default_factory=list)
    active_decisions: list[MemoryHit] = Field(default_factory=list)
    proposed_decisions: list[MemoryHit] = Field(default_factory=list)
    open_tasks: list[MemoryHit] = Field(default_factory=list)
    open_questions: list[MemoryHit] = Field(default_factory=list)
    constraints: list[MemoryHit] = Field(default_factory=list)
    #: section name -> why it could not be built (the rest of the snapshot is still returned)
    unavailable: dict[str, str] = Field(default_factory=dict)
