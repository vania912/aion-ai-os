"""Build the project context snapshot from the git and memory services.

Each section fails independently: a project whose directory is missing still returns its
description and memory sections, with the git section listed under ``unavailable``.
"""

from __future__ import annotations

from collections.abc import Callable

from aios.domain.context import GitSummary, ProjectContext
from aios.domain.memory import MemoryHit
from aios.domain.models import ProjectConfig
from aios.errors import AiosError
from aios.services.git import GitService
from aios.services.memory import MemoryService

_OPEN_TASK_STATUSES = ("in_progress", "blocked", "open")


def build_context(
    project_id: str,
    project: ProjectConfig,
    *,
    git: Callable[[], GitService] | None,
    memory: MemoryService,
    recent_commits: int = 5,
    per_section: int = 8,
) -> ProjectContext:
    unavailable: dict[str, str] = {}
    git_summary = None
    recent = []

    if git is None:
        unavailable["git"] = "project is registered with git: false"
    else:
        try:
            service = git()
            s = service.get_branch_status()
            git_summary = GitSummary(
                branch=s.branch, detached=s.detached, head=s.head, upstream=s.upstream,
                ahead=s.ahead, behind=s.behind, clean=s.clean, staged=s.staged,
                unstaged=s.unstaged, untracked=s.untracked,
            )  # fmt: skip
            recent = service.get_recent_changes(limit=recent_commits).commits
        except AiosError as exc:
            unavailable["git"] = str(exc)

    def records(
        name: str, record_type: str, statuses: tuple[str, ...], limit: int
    ) -> list[MemoryHit]:
        try:
            found: list[MemoryHit] = []
            for status in statuses:
                found += memory.list_records(
                    project=project_id, record_type=record_type, status=status, limit=limit
                ).records
            found.sort(key=lambda h: (h.date, h.id), reverse=True)
            return found[:limit]
        except AiosError as exc:
            unavailable[name] = str(exc)
            return []

    return ProjectContext(
        project=project_id,
        description=project.description,
        git=git_summary,
        recent_changes=recent,
        active_decisions=records("decisions", "decision", ("active",), per_section),
        proposed_decisions=records("proposed_decisions", "decision", ("proposed",), per_section),
        open_tasks=records("tasks", "task", _OPEN_TASK_STATUSES, per_section),
        open_questions=records("questions", "question", ("open",), per_section),
        constraints=records("constraints", "constraint", ("active",), per_section),
        unavailable=unavailable,
    )


def render_context(ctx: ProjectContext) -> str:
    """Compact Markdown for a model or a person."""
    lines = [f"# Project {ctx.project}", ctx.description or "(no description)", ""]
    if ctx.git:
        g = ctx.git
        where = "detached HEAD" if g.detached else f"branch {g.branch}"
        lines.append(f"## Git\n{where}, head {(g.head or 'none')[:10]}")
        if g.upstream:
            lines.append(f"upstream {g.upstream}: ahead {g.ahead}, behind {g.behind}")
        lines.append(
            "working tree clean"
            if g.clean
            else f"working tree: {g.staged} staged, {g.unstaged} unstaged, {g.untracked} untracked"
        )
        lines.append("")
    if ctx.recent_changes:
        lines.append("## Recent changes")
        for c in ctx.recent_changes:
            hidden = f" (+{c.hidden_files} withheld)" if c.hidden_files else ""
            files = ", ".join(f"{f.status} {f.path}" for f in c.files[:6])
            more = f" +{len(c.files) - 6} more" if len(c.files) > 6 else ""
            lines.append(f"- {c.short_hash} {c.date[:10]} {c.subject} [{files}{more}]{hidden}")
        lines.append("")
    for title, hits in (
        ("Active decisions", ctx.active_decisions),
        ("Proposed decisions", ctx.proposed_decisions),
        ("Open tasks", ctx.open_tasks),
        ("Open questions", ctx.open_questions),
        ("Active constraints", ctx.constraints),
    ):
        if hits:
            lines.append(f"## {title}")
            lines += [f"- {h.id} [{h.status}] {h.date} {h.title}" for h in hits]
            lines.append("")
    if ctx.unavailable:
        lines.append("## Unavailable")
        lines += [f"- {name}: {reason}" for name, reason in sorted(ctx.unavailable.items())]
    return "\n".join(lines).rstrip() + "\n"
