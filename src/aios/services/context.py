"""Build the project context snapshot from the git and memory services.

Each section fails independently: a project whose directory is missing still returns its
description and memory sections, with the git section listed under ``unavailable``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from aios.domain.context import GitSummary, ProjectContext
from aios.domain.memory import MemoryHit
from aios.domain.models import ProjectConfig
from aios.errors import AiosError
from aios.services.git import GitService
from aios.services.memory import MemoryService
from aios.services.plans import PlanService

_OPEN_TASK_STATUSES = ("in_progress", "blocked", "open")


def build_context(
    project_id: str,
    project: ProjectConfig,
    *,
    git: Callable[[], GitService] | None,
    memory: MemoryService,
    recent_commits: int = 5,
    per_section: int = 8,
    today: date | None = None,
    plans: PlanService | None = None,
) -> ProjectContext:
    unavailable: dict[str, str] = {}
    open_plans = []
    if plans is not None:
        try:
            open_plans = plans.list_plans(project=project_id, open_only=True, limit=per_section)
        except AiosError as exc:
            unavailable["plans"] = str(exc)
    state = None
    state_age: int | None = None
    try:
        state = memory.project_state(project_id)
        if state is not None and state.updated is not None:
            state_age = ((today or datetime.now(UTC).date()) - state.updated).days
    except AiosError as exc:
        unavailable["state"] = str(exc)
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
        name: str,
        record_type: str | None,
        statuses: tuple[str | None, ...],
        limit: int,
        *,
        reviewed: bool = True,
    ) -> list[MemoryHit]:
        """Records for one section. Established sections take reviewed records only; the
        "awaiting review" section takes the unreviewed ones."""
        try:
            found: list[MemoryHit] = []
            for status in statuses:
                found += memory.list_records(
                    project=project_id,
                    record_type=record_type,
                    status=status,
                    reviewed=reviewed,
                    limit=limit,
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
        awaiting_review=records("awaiting_review", None, (None,), per_section, reviewed=False),
        plans=open_plans,
        state=state,
        state_age_days=state_age,
        unavailable=unavailable,
    )


def _hit_line(h: MemoryHit) -> str:
    marks = [h.status] + ([] if h.reviewed else [f"unreviewed, {h.origin}"])
    return f"- {h.id} [{', '.join(marks)}] {h.date} {h.title}"


def _render_plans(ctx: ProjectContext) -> list[str]:
    lines = ["## Plans (open; an approval is a record, not enforced by aios)"]
    for p in ctx.plans:
        if p.status == "proposed":
            mark = f"proposed, NOT approved, tier {p.tier}, {p.origin}"
        elif p.status == "approved":
            mark = f"approved (recorded), tier {p.tier}, no outcome yet"
        else:
            mark = f"{p.status}, tier {p.tier}"
        stale = ", APPROVAL STALE" if p.approval_stale else ""
        lines.append(f"- {p.id} [{mark}{stale}] {p.created} {p.title}")
    lines.append("")
    return lines


def _quote(text: str) -> list[str]:
    return [f"> {line}" for line in text.splitlines()]


def _render_state(ctx: ProjectContext) -> list[str]:
    state = ctx.state
    assert state is not None
    if state.updated is None:
        when = "no update date"
    else:
        when = f"updated {state.updated.isoformat()}, {ctx.state_age_days} days ago"
    lines = [f"## Project state (hand-maintained file, {when}; aios does not verify it)"]
    # Every line is quoted so file text cannot start its own heading or list entry in the snapshot.
    if state.summary:
        lines += _quote(state.summary)
    if state.architecture:
        lines += _quote("Architecture: " + state.architecture)
    if state.known_constraints:
        lines.append("Known constraints:")
        lines += _quote("\n".join(f"- {c}" for c in state.known_constraints))
    lines.append("")
    return lines


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
    if ctx.state is not None:
        lines += _render_state(ctx)
    if ctx.plans:
        lines += _render_plans(ctx)
    for title, hits in (
        ("Active decisions", ctx.active_decisions),
        ("Proposed decisions", ctx.proposed_decisions),
        ("Open tasks", ctx.open_tasks),
        ("Open questions", ctx.open_questions),
        ("Active constraints", ctx.constraints),
        ("Awaiting review (UNVERIFIED, not established facts)", ctx.awaiting_review),
    ):
        if hits:
            lines.append(f"## {title}")
            lines += [_hit_line(h) for h in hits]
            lines.append("")
    if ctx.unavailable:
        lines.append("## Unavailable")
        lines += [f"- {name}: {reason}" for name, reason in sorted(ctx.unavailable.items())]
    return "\n".join(lines).rstrip() + "\n"
