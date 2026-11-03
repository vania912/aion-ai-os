"""Memory write service: create records and make the few permitted edits.

One code path serves the human CLI and (later) the MCP propose tools, so the rules cannot drift:

* The caller supplies *content*. The server sets the id, the date, ``origin``, ``reviewed`` and the
  initial status; a caller cannot choose them.
* A record created for Claude is ``origin: claude``, ``reviewed: false``; a decision starts as
  ``proposed`` (never ``active``), and needs evidence. A per-project quota bounds unreviewed
  records. A proposed decision that supersedes another changes nothing until accepted.
* Reasoning is never edited after the fact: the only edits are status, ``superseded_by`` and the
  review fields (see ``memory_writer.EDITABLE_KEYS``).
* All writes happen under one lock (``<data>/.index/write.lock``) so ids stay unique.

This module never writes to a project repository, only to the memory area of the data directory.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aios.adapters.filelock import exclusive_lock
from aios.adapters.memory_files import INDEX_DIRNAME, scan_memory
from aios.adapters.memory_index import ensure_cache_dir
from aios.adapters.memory_writer import (
    allocate_id,
    create_record_file,
    edit_front_matter,
    record_path,
)
from aios.domain.memory import (
    VALID_STATUS,
    LoadedRecord,
    MemoryRecord,
    RecordType,
    decision_digest,
)
from aios.errors import InvalidRequestError, UnknownRecordError

Origin = Literal["human", "claude"]

#: Status a freshly created record starts in, by type.
INITIAL_STATUS: dict[str, str] = {
    "decision": "active",
    "assumption": "unverified",
    "constraint": "active",
    "question": "open",
    "discovery": "active",
    "task": "open",
    "note": "active",
}

#: Task status moves the tools and CLI allow.
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"in_progress", "blocked", "done", "cancelled"}),
    "in_progress": frozenset({"open", "blocked", "done", "cancelled"}),
    "blocked": frozenset({"open", "in_progress", "cancelled"}),
    "done": frozenset({"open"}),
    "cancelled": frozenset({"open"}),
}


class WriteConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    lock_timeout_s: float = 30.0
    #: Unreviewed records one project may hold before further Claude proposals are refused.
    max_unreviewed_per_project: int = 20
    #: Body size cap for Claude-origin records (hand-written records are not capped here).
    max_claude_body_chars: int = 8_000


class NewRecord(BaseModel):
    """Content supplied by a caller. Everything else is decided by the server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: RecordType
    project: str
    title: str
    body: str = ""
    made_by: list[str] = Field(default_factory=list)
    why: list[str] = Field(default_factory=list)
    alternatives_considered: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    reason_for_change: str | None = None
    related_files: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    related_decisions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class CreateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: MemoryRecord
    path: str  # relative to the data directory
    #: Steps that still need a human, or follow-up the caller should know about.
    notes: list[str] = Field(default_factory=list)


class MemoryWriter:
    def __init__(
        self,
        data_dir: Path,
        *,
        config: WriteConfig | None = None,
        today: date | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._cfg = config if config is not None else WriteConfig()
        self._today = today

    # -- creating ------------------------------------------------------------------------

    def create(self, new: NewRecord, *, origin: Origin = "human") -> CreateResult:
        """Create a record. Raises ``InvalidRequestError`` with a reason if it is refused."""
        with self.lock():
            existing = self.load_all()
            record = self._build(new, origin, existing)
            path = create_record_file(self._data_dir, record)
        notes = self._notes_for(record)
        return CreateResult(
            record=record, path=path.relative_to(self._data_dir).as_posix(), notes=notes
        )

    def _build(
        self, new: NewRecord, origin: Origin, existing: dict[str, LoadedRecord]
    ) -> MemoryRecord:
        claude = origin == "claude"
        initial = INITIAL_STATUS[new.type]
        if new.type == "decision" and (claude or new.supersedes):
            # A human decision that supersedes another starts proposed too; complete_supersession
            # (run right after, for human creates) activates it together with the predecessor.
            initial = "proposed"
        if claude:
            self._claude_rules(new, existing)
        self._check_references(new, existing)
        if new.supersedes:
            self._check_supersedes(new, existing)
        if new.type == "decision" and not new.why:
            raise InvalidRequestError("a decision needs at least one reason ('why')")
        try:
            return MemoryRecord(
                id=allocate_id(self._data_dir, new.type),
                type=new.type,
                project=new.project,
                title=new.title,
                status=initial,
                date=self._today or datetime.now(UTC).date(),
                origin=origin,
                reviewed=not claude,
                evidence=new.evidence,
                body=new.body,
                made_by=new.made_by,
                why=new.why,
                alternatives_considered=new.alternatives_considered,
                assumptions=new.assumptions,
                constraints=new.constraints,
                supersedes=new.supersedes,
                reason_for_change=new.reason_for_change,
                related_files=new.related_files,
                related_tasks=new.related_tasks,
                related_decisions=new.related_decisions,
                tags=new.tags,
            )
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise InvalidRequestError(f"invalid record: {detail}") from None

    def _claude_rules(self, new: NewRecord, existing: dict[str, LoadedRecord]) -> None:
        if new.type == "decision" and not new.evidence:
            raise InvalidRequestError(
                "a proposed decision needs evidence (commit hashes, file:line, record ids)"
            )
        if len(new.body) > self._cfg.max_claude_body_chars:
            raise InvalidRequestError(
                "body is too long for a proposal "
                f"({self._cfg.max_claude_body_chars} characters max)"
            )
        wanted = " ".join(new.title.casefold().split())
        for loaded in existing.values():
            r = loaded.record
            if (
                not r.reviewed
                and r.project == new.project
                and r.type == new.type
                and " ".join(r.title.casefold().split()) == wanted
            ):
                raise InvalidRequestError(
                    f"{r.id} already proposes this {r.type} with the same title and is "
                    "awaiting review; do not propose it twice"
                )
        pending = sum(
            1
            for loaded in existing.values()
            if loaded.record.project == new.project and not loaded.record.reviewed
        )
        if pending >= self._cfg.max_unreviewed_per_project:
            raise InvalidRequestError(
                f"project {new.project!r} already has {pending} records awaiting review "
                f"(limit {self._cfg.max_unreviewed_per_project}); review them first with "
                "`aios review list`"
            )

    @staticmethod
    def _check_references(new: NewRecord, existing: dict[str, LoadedRecord]) -> None:
        expected = (
            ("related_tasks", "task", new.related_tasks),
            ("related_decisions", "decision", new.related_decisions),
            ("assumptions", "assumption", new.assumptions),
            ("constraints", "constraint", new.constraints),
        )
        for field, kind, ids in expected:
            for ref in ids:
                target = existing.get(ref)
                if target is None or target.record.type != kind:
                    raise InvalidRequestError(f"{field}: {ref} is not an existing {kind}")

    @staticmethod
    def _check_supersedes(new: NewRecord, existing: dict[str, LoadedRecord]) -> None:
        if new.type != "decision":
            raise InvalidRequestError("only a decision can supersede another")
        target = existing.get(new.supersedes or "")
        if target is None or target.record.type != "decision":
            raise InvalidRequestError(f"supersedes: {new.supersedes} is not an existing decision")
        if target.record.project != new.project:
            raise InvalidRequestError("a decision can only supersede one in the same project")
        if target.record.status != "active":
            raise InvalidRequestError(
                f"{target.record.id} is {target.record.status}; "
                "only an active decision can be superseded"
            )
        for other in existing.values():
            if other.record.supersedes == target.record.id and other.record.status in (
                "proposed",
                "active",
            ):
                raise InvalidRequestError(
                    f"{target.record.id} is already being replaced by {other.record.id}"
                )

    @staticmethod
    def _notes_for(record: MemoryRecord) -> list[str]:
        notes = []
        if not record.reviewed:
            notes.append(f"awaiting human review: aios review show {record.id}")
        if record.supersedes and record.status == "proposed":
            notes.append(f"{record.supersedes} stays active until {record.id} is accepted")
        return notes

    # -- task status ---------------------------------------------------------------------

    def set_task_status(self, task_id: str, status: str) -> MemoryRecord:
        """Move a task along its allowed transitions. Returns the updated record."""
        if status not in VALID_STATUS["task"]:
            raise InvalidRequestError(
                f"status must be one of: {', '.join(sorted(VALID_STATUS['task']))}"
            )
        with self.lock():
            loaded = self.load_all().get(task_id.strip().upper())
            if loaded is None:
                raise UnknownRecordError(f"no record with id {task_id.strip().upper()}")
            record = loaded.record
            if record.type != "task":
                raise InvalidRequestError(f"{record.id} is a {record.type}, not a task")
            if status == record.status:
                return record
            if status not in TASK_TRANSITIONS[record.status]:
                allowed = ", ".join(sorted(TASK_TRANSITIONS[record.status])) or "none"
                raise InvalidRequestError(
                    f"{record.id}: cannot move from {record.status} to {status} "
                    f"(allowed: {allowed})"
                )
            return edit_front_matter(self._data_dir / loaded.path, {"status": status})

    # -- supersession --------------------------------------------------------------------

    def complete_supersession(
        self, successor_id: str, *, accepted_by: str | None = None
    ) -> list[str]:
        """Finish (or re-run) replacing a decision. Idempotent. Returns what was changed.

        Steps, in this order: (1) activate the successor, (2) mark the predecessor superseded.
        A crash between the steps leaves a one-sided link that the reader already tolerates and
        ``aios doctor`` reports; running this again completes the second step.
        """
        with self.lock():
            return self.complete_supersession_locked(
                successor_id, self.load_all(), accepted_by=accepted_by
            )

    def complete_supersession_locked(
        self,
        successor_id: str,
        existing: dict[str, LoadedRecord],
        *,
        accepted_by: str | None = None,
    ) -> list[str]:
        """The body of :meth:`complete_supersession`; the caller already holds :meth:`lock`."""
        changed: list[str] = []
        loaded = existing.get(successor_id.strip().upper())
        if loaded is None or loaded.record.type != "decision" or not loaded.record.supersedes:
            raise InvalidRequestError(f"{successor_id} is not a decision that supersedes another")
        succ = loaded.record
        pred_loaded = existing.get(succ.supersedes or "")
        if pred_loaded is None:
            raise InvalidRequestError(f"{succ.supersedes} does not exist")
        pred = pred_loaded.record
        if pred.type != "decision" or pred.project != succ.project:
            raise InvalidRequestError(f"{pred.id} is not a decision of project {succ.project}")
        if pred.status not in ("active", "superseded") or (
            pred.status == "superseded" and pred.superseded_by != succ.id
        ):
            raise InvalidRequestError(
                f"{pred.id} is {pred.status} (superseded_by {pred.superseded_by}); "
                f"it cannot be replaced by {succ.id}"
            )
        if succ.origin == "claude" and not accepted_by:
            raise InvalidRequestError(
                f"{succ.id} was proposed by Claude; accepting it must record who accepted it"
            )
        if succ.status not in ("proposed", "active"):
            raise InvalidRequestError(
                f"{succ.id} is {succ.status}; only a proposed or active decision "
                f"can replace {succ.supersedes}"
            )
        if succ.status != "active":
            updates: dict[str, object] = {"status": "active", "reviewed": True}
            if accepted_by:
                updates["accepted_by"] = accepted_by
            updates["accepted_digest"] = decision_digest(succ)
            edit_front_matter(self._data_dir / loaded.path, updates)
            changed.append(f"{succ.id}: proposed -> active")
        if pred.status != "superseded" or pred.superseded_by != succ.id:
            edit_front_matter(
                self._data_dir / pred_loaded.path,
                {"status": "superseded", "superseded_by": succ.id},
            )
            changed.append(f"{pred.id}: {pred.status} -> superseded (by {succ.id})")
        return changed

    # -- helpers -------------------------------------------------------------------------

    def lock(self) -> contextlib.AbstractContextManager[None]:
        cache = self._data_dir / INDEX_DIRNAME
        ensure_cache_dir(cache)
        return exclusive_lock(cache / "write.lock", self._cfg.lock_timeout_s, "memory write lock")

    def load_all(self) -> dict[str, LoadedRecord]:
        return {x.record.id: x for x in scan_memory(self._data_dir).records}

    def path_of(self, record: MemoryRecord) -> Path:
        return record_path(self._data_dir, record)
