"""MCP resources (application-controlled, stable readable documents).

Five concrete resources per registered project, created at server start:

    project://<id>                 overview
    project://<id>/context         state snapshot (same as get_project_context)
    project://<id>/decisions       index of decisions, newest first
    project://<id>/tasks           open, in-progress and blocked tasks
    project://<id>/recent-changes  last 10 commits

Projects added to the registry later are available to the *tools* immediately, but their
resources appear after the server restarts (Claude Code starts it per session).
Everything goes through ``ServerContext.run`` (policy and audit).
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from aios.domain.memory import MemoryHit
from aios.interfaces.mcp.context import ServerContext
from aios.services.context import build_context, render_context

Builder = Callable[[ServerContext, str], str]


def _hits_markdown(title: str, hits: list[MemoryHit], truncated: bool) -> str:
    lines = [f"# {title}"]
    if not hits:
        lines.append("(none)")
    for h in hits:
        arrow = f" -> superseded by {h.superseded_by}" if h.superseded_by else ""
        flag = "" if h.reviewed else f", UNREVIEWED ({h.origin})"
        lines.append(f"- {h.id} [{h.status}{arrow}{flag}] {h.date} {h.title}")
    if truncated:
        lines.append("(more not shown; use list_memory or search_memory)")
    return "\n".join(lines) + "\n"


def _overview(ctx: ServerContext, project: str) -> str:
    config = ctx.registry().get(project)
    available = "yes" if config.path.is_dir() else "no (directory not found on this machine)"
    return (
        f"# Project {project}\n{config.description or '(no description)'}\n\n"
        f"- uses git: {'yes' if config.git else 'no'}\n- directory available: {available}\n\n"
        f"Resources: project://{project}/context, /decisions, /tasks, /recent-changes\n"
    )


def _context(ctx: ServerContext, project: str) -> str:
    config = ctx.registry().get(project)
    return render_context(
        build_context(
            project,
            config,
            git=(lambda: ctx.git(project)) if config.git else None,
            memory=ctx.memory,
        )
    )


def _decisions(ctx: ServerContext, project: str) -> str:
    result = ctx.memory.list_records(project=project, record_type="decision", limit=50)
    return _hits_markdown(f"Decisions: {project}", result.records, result.truncated)


def _tasks(ctx: ServerContext, project: str) -> str:
    found: list[MemoryHit] = []
    truncated = False
    for status in ("in_progress", "blocked", "open"):
        part = ctx.memory.list_records(project=project, record_type="task", status=status, limit=50)
        found += part.records
        truncated = truncated or part.truncated
    found.sort(key=lambda h: (h.date, h.id), reverse=True)
    return _hits_markdown(f"Open tasks: {project}", found[:50], truncated or len(found) > 50)


def _recent(ctx: ServerContext, project: str) -> str:
    result = ctx.git(project).get_recent_changes(limit=10)
    lines = [f"# Recent changes: {project}"]
    for c in result.commits:
        files = ", ".join(f"{f.status} {f.path}" for f in c.files[:8])
        hidden = f" (+{c.hidden_files} withheld by policy)" if c.hidden_files else ""
        lines.append(f"- {c.short_hash} {c.date[:10]} {c.author}: {c.subject} [{files}]{hidden}")
    if not result.commits:
        lines.append("(no commits)")
    return "\n".join(lines) + "\n"


RESOURCES: tuple[tuple[str, str, str, Builder], ...] = (
    ("", "overview", "Project overview", _overview),
    ("/context", "context", "Project state snapshot", _context),
    ("/decisions", "decisions", "Decision index", _decisions),
    ("/tasks", "tasks", "Open tasks", _tasks),
    ("/recent-changes", "recent-changes", "Recent git changes", _recent),
)


def register_resources(server: MCPServer, ctx: ServerContext) -> None:
    for project in sorted(ctx.registry().projects):
        for suffix, slug, title, builder in RESOURCES:
            uri = f"project://{project}{suffix}"

            def make(
                uri: str = uri, project: str = project, builder: Builder = builder
            ) -> Callable[[], str]:
                def read() -> str:
                    return ctx.run(
                        "read_resource",
                        lambda: builder(ctx, project),
                        project=project,
                        args={"uri": uri},
                        summarize=lambda text: {"chars": len(text)},
                        error=ResourceError,
                    )

                return read

            server.resource(
                uri,
                name=f"{project}-{slug}",
                title=f"{project}: {title}",
                description=f"{title} for project {project} (read-only).",
                mime_type="text/markdown",
            )(make())
