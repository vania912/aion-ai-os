"""The MCP propose tools (PROPOSE level). Registered only when the server starts with
``--allow-propose``; without the flag they do not exist for the client, and the policy denies them
anyway.

What a tool can do is deliberately narrow:

* create an *unreviewed* record (``origin: claude``, ``reviewed: false``; a decision starts
  ``proposed``) inside the memory area of the data directory, never in a project;
* move a task's status along the allowed transitions.

It cannot accept or reject anything (that is a human CLI action), choose an id, a date, the
origin or the review state, edit reasoning after the fact, or touch a project repository.
Whatever the caller sends is data: it is validated by the same writer the CLI uses and stored
as text. The audit log records ids, types and sizes, never the content.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from aios.domain.memory import MemoryHit
from aios.interfaces.mcp.context import ServerContext
from aios.interfaces.mcp.render import compact
from aios.services.memory_write import CreateResult, NewRecord

F = TypeVar("F", bound=Callable[..., Any])

CREATES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
UPDATES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

Project = Annotated[str, Field(description="Project id from list_projects, e.g. 'pulse'.")]
Title = Annotated[str, Field(description="Short title, one line (max 200 characters).")]
Body = Annotated[str, Field(description="Details, plain text (max 8000 characters).")]
Evidence = Annotated[
    list[str],
    Field(
        description=(
            "What this is based on: commit hashes, file:line, record ids, one short line each "
            "(max 20)."
        )
    ),
]
Tags = Annotated[list[str], Field(description="Optional tags.")]
Files = Annotated[list[str], Field(description="Project-relative file paths this concerns.")]
Superseded = Annotated[
    str | None,
    Field(
        description=(
            "Id of the ACTIVE decision this one would replace. Nothing changes until a human "
            "accepts this proposal; until then the old decision stays in force."
        )
    ),
]

MemoryKind = Literal["assumption", "constraint", "question", "discovery", "note"]
TaskStatus = Literal["open", "in_progress", "blocked", "done", "cancelled"]


class _Duplicate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    status: str
    reviewed: bool


class _Proposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    project: str
    status: str
    reviewed: bool
    note: str
    next_steps: list[str]
    similar_existing: list[_Duplicate]


class _TaskUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    status: str


def _duplicates(ctx: ServerContext, result: CreateResult) -> list[_Duplicate]:
    hits: list[MemoryHit] = ctx.memory.find_similar(
        result.record.title, project=result.record.project, exclude_id=result.record.id, limit=3
    )
    return [_Duplicate(id=h.id, title=h.title, status=h.status, reviewed=h.reviewed) for h in hits]


def register_write_tools(server: MCPServer, ctx: ServerContext) -> None:
    def tool(name: str, title: str, annotations: ToolAnnotations = CREATES) -> Callable[[F], F]:
        return server.tool(name=name, title=title, annotations=annotations, structured_output=False)

    def propose(operation: str, new: NewRecord) -> str:
        def go() -> _Proposal:
            ctx.registry().get(new.project)  # unknown project -> clear error, nothing written
            result = ctx.writer.create(new, origin="claude")
            r = result.record
            return _Proposal(
                id=r.id,
                type=r.type,
                project=r.project,
                status=r.status,
                reviewed=r.reviewed,
                note=(
                    "UNREVIEWED proposal. It is not an established fact or a decision in force, "
                    "and will not appear as one until a human accepts it. Do not present it as "
                    "settled."
                ),
                next_steps=[*result.notes, f"human: aios review accept {r.id}  (or reject)"],
                similar_existing=_duplicates(ctx, result),
            )

        args = {
            "project": new.project,
            "type": new.type,
            "title_chars": len(new.title),
            "body_chars": len(new.body),
            "evidence": len(new.evidence),
            "supersedes": new.supersedes,
        }
        return compact(
            ctx.run(
                operation,
                go,
                project=new.project,
                args=args,
                summarize=lambda p: {
                    "id": p.id,
                    "type": p.type,
                    "status": p.status,
                    "similar": len(p.similar_existing),
                },
            )
        )

    @tool("propose_decision", "Propose a decision")
    def propose_decision(
        project: Project,
        title: Title,
        why: Annotated[list[str], Field(description="Reasons, at least one.")],
        evidence: Evidence,
        alternatives_considered: Annotated[list[str], Field(description="Options rejected.")] = [],  # noqa: B006
        body: Body = "",
        supersedes: Superseded = None,
        reason_for_change: Annotated[
            str | None, Field(description="Why the old decision is being replaced.")
        ] = None,
        related_files: Files = [],  # noqa: B006
        tags: Tags = [],  # noqa: B006
    ) -> str:
        """Record a decision you believe was made, as an UNREVIEWED proposal with evidence. It is
        stored as 'proposed' and stays out of active decisions until a human accepts it with
        `aios review accept`. Check search_memory first; do not propose what already exists. The
        result lists similar existing records. Only propose what the evidence supports."""
        return propose(
            "propose_decision",
            NewRecord(
                type="decision",
                project=project,
                title=title,
                why=why,
                evidence=evidence,
                alternatives_considered=alternatives_considered,
                body=body,
                supersedes=supersedes,
                reason_for_change=reason_for_change,
                related_files=related_files,
                tags=tags,
            ),
        )

    @tool("propose_task", "Propose a task")
    def propose_task(
        project: Project,
        title: Title,
        body: Body = "",
        related_decisions: Annotated[
            list[str], Field(description="Ids of existing decisions this task follows from.")
        ] = [],  # noqa: B006
        related_files: Files = [],  # noqa: B006
        evidence: Evidence = [],  # noqa: B006
        tags: Tags = [],  # noqa: B006
    ) -> str:
        """Suggest a task as an UNREVIEWED proposal. It is not on the human's open-task list
        until reviewed. Check list_memory/search_memory first for an existing task."""
        return propose(
            "propose_task",
            NewRecord(
                type="task",
                project=project,
                title=title,
                body=body,
                related_decisions=related_decisions,
                related_files=related_files,
                evidence=evidence,
                tags=tags,
            ),
        )

    @tool("propose_memory_item", "Propose a memory item")
    def propose_memory_item(
        project: Project,
        record_type: Annotated[
            MemoryKind,
            Field(description="assumption, constraint, question, discovery or note."),
        ],
        title: Title,
        body: Body = "",
        related_files: Files = [],  # noqa: B006
        evidence: Evidence = [],  # noqa: B006
        tags: Tags = [],  # noqa: B006
    ) -> str:
        """Save an assumption, constraint, open question, discovery or note as an UNREVIEWED
        proposal. State only what you observed, and cite it in `evidence`. Use propose_decision
        for decisions and propose_task for tasks."""
        return propose(
            "propose_memory_item",
            NewRecord(
                type=record_type,
                project=project,
                title=title,
                body=body,
                related_files=related_files,
                evidence=evidence,
                tags=tags,
            ),
        )

    @tool("update_task_status", "Update a task's status", UPDATES)
    def update_task_status(
        task_id: Annotated[str, Field(description="Task id such as TSK-0001.")],
        status: Annotated[TaskStatus, Field(description="The new status.")],
    ) -> str:
        """Move a task to a new status (allowed moves only, for example open to in_progress, or
        in_progress to done). Do not mark a task done unless you verified it. Recorded in the
        audit log."""

        def go() -> _TaskUpdate:
            record = ctx.writer.set_task_status(task_id, status)
            return _TaskUpdate(id=record.id, title=record.title, status=record.status)

        return compact(
            ctx.run(
                "update_task_status",
                go,
                args={"task_id": task_id, "status": status},
                summarize=lambda u: {"id": u.id, "status": u.status},
            )
        )
