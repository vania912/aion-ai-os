"""Review workflow: list, describe, accept and reject proposed memory records.

Run by a human through the CLI (ADR-0009, D2). Accepting makes a record count as reviewed; for a
decision it also puts the decision in force (and retires the one it replaces). Rejecting moves
the record to ``projects/<project>/_rejected/<type-dir>/`` where it is no longer indexed; ids of
rejected records are never reused.

Both actions are idempotent and run under the memory write lock. A crash between a rejection's
edit and its move is repaired by running the rejection again.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from aios.adapters.memory_files import PROJECTS_DIRNAME
from aios.adapters.memory_writer import edit_front_matter
from aios.domain.memory import TYPE_DIR, LoadedRecord, MemoryHit, MemoryRecord
from aios.errors import InvalidRequestError, MemoryStoreError, UnknownRecordError
from aios.services.memory import MemoryService
from aios.services.memory_write import MemoryWriter, decision_digest

REJECTED_DIRNAME = "_rejected"
_MOVE_ATTEMPTS = 5


class ReviewPlan(BaseModel):
    """What a reviewer should see before accepting."""

    model_config = ConfigDict(frozen=True)

    record: MemoryRecord
    path: str
    #: Exactly what ``accept`` will change.
    actions: list[str]
    #: Things the reviewer should know (stale predecessor, missing evidence, ...).
    warnings: list[str] = Field(default_factory=list)
    #: The decision this one would replace, if any.
    predecessor: MemoryRecord | None = None
    #: Existing records with similar text (possible duplicates).
    similar: list[MemoryHit] = Field(default_factory=list)
    #: ``accept`` would be refused for this reason.
    blocked: str | None = None


class MemoryReviewer:
    def __init__(self, data_dir: Path, *, writer: MemoryWriter | None = None) -> None:
        self._data_dir = data_dir
        self._writer = writer if writer is not None else MemoryWriter(data_dir)

    # -- listing and describing ----------------------------------------------------------

    def list_pending(
        self, *, project: str | None = None, record_type: str | None = None
    ) -> list[MemoryRecord]:
        """Unreviewed records, newest first."""
        if record_type is not None and record_type not in TYPE_DIR:
            raise InvalidRequestError(f"type must be one of: {', '.join(sorted(TYPE_DIR))}")
        found = [
            x.record
            for x in self._writer.load_all().values()
            if not x.record.reviewed
            and (project is None or x.record.project == project)
            and (record_type is None or x.record.type == record_type)
        ]
        return sorted(found, key=lambda r: (r.date, r.id), reverse=True)

    def plan_accept(self, record_id: str, *, accepted_by: str) -> ReviewPlan:
        existing = self._writer.load_all()
        loaded = self._find(existing, record_id)
        r = loaded.record
        actions: list[str] = []
        warnings: list[str] = []
        blocked: str | None = None
        predecessor: MemoryRecord | None = None

        if r.reviewed and not (r.type == "decision" and r.supersedes):
            actions.append("nothing to do: already reviewed")
        elif r.type == "decision":
            if r.status not in ("proposed", "active"):
                blocked = (
                    f"{r.id} is {r.status}; only a proposed or active decision can be accepted"
                )
            if r.supersedes:
                pred_loaded = existing.get(r.supersedes)
                predecessor = pred_loaded.record if pred_loaded else None
                if predecessor is None:
                    blocked = f"{r.supersedes} does not exist"
                elif predecessor.status == "active":
                    actions.append(f"{r.supersedes}: active -> superseded (by {r.id})")
                elif not (predecessor.status == "superseded" and predecessor.superseded_by == r.id):
                    blocked = (
                        f"{r.supersedes} is {predecessor.status}, so it can no longer be replaced "
                        f"by {r.id}; reject this proposal instead"
                    )
            if r.status == "proposed":
                actions.insert(0, f"{r.id}: proposed -> active")
        if not r.reviewed:
            actions.append(f"mark {r.id} reviewed, accepted by {accepted_by}")
        elif r.type == "decision" and r.supersedes and not actions:
            actions.append("nothing to do: already reviewed and in force")

        if r.origin == "claude" and r.type == "decision" and not r.evidence:
            warnings.append("this decision has no evidence")
        for field in ("related_tasks", "related_decisions", "assumptions", "constraints"):
            for ref in getattr(r, field):
                if ref not in existing:
                    warnings.append(f"{field}: {ref} does not exist")
        return ReviewPlan(
            record=r,
            path=loaded.path,
            actions=actions,
            warnings=warnings,
            predecessor=predecessor,
            similar=self._similar(r),
            blocked=blocked,
        )

    def _similar(self, record: MemoryRecord) -> list[MemoryHit]:
        return MemoryService(self._data_dir).find_similar(
            record.title, project=record.project, exclude_id=record.id, limit=5
        )

    # -- accept --------------------------------------------------------------------------

    def accept(self, record_id: str, *, accepted_by: str) -> list[str]:
        """Accept a proposal. Returns what changed (empty if it was already accepted)."""
        name = accepted_by.strip()
        if not name:
            raise InvalidRequestError("accepted_by must not be empty")
        with self._writer.lock():
            existing = self._writer.load_all()
            loaded = self._find(existing, record_id)
            r = loaded.record
            if r.type == "decision" and r.supersedes:
                # Also completes a half-applied supersession of an already reviewed successor.
                return self._writer.complete_supersession_locked(r.id, existing, accepted_by=name)
            if r.reviewed:
                return []
            if r.type == "decision":
                if r.status not in ("proposed", "active"):
                    raise InvalidRequestError(
                        f"{r.id} is {r.status}; only a proposed or active decision can be accepted"
                    )
                updates: dict[str, object] = {
                    "reviewed": True,
                    "accepted_by": name,
                    "accepted_digest": decision_digest(r),
                }
                changes = [f"{r.id}: reviewed"]
                if r.status == "proposed":
                    updates["status"] = "active"
                    changes = [f"{r.id}: proposed -> active", f"{r.id}: reviewed"]
                edit_front_matter(self._data_dir / loaded.path, updates)
                return changes
            edit_front_matter(self._data_dir / loaded.path, {"reviewed": True, "accepted_by": name})
            return [f"{r.id}: reviewed (status stays {r.status})"]

    # -- reject --------------------------------------------------------------------------

    def reject(self, record_id: str, reason: str) -> str:
        """Reject a proposal: record the reason and move it out of the index. Returns its new
        location (relative to the data directory). Safe to run again after a crash."""
        text = reason.strip()
        if not 1 <= len(text) <= 300 or any(ord(c) < 32 for c in text):
            raise InvalidRequestError("a rejection needs a single-line reason of 1-300 characters")
        rid = record_id.strip().upper()
        with self._writer.lock():
            existing = self._writer.load_all()
            archived = self._find_archived(rid)
            if rid not in existing and archived is not None:
                return archived.relative_to(self._data_dir).as_posix()  # already rejected
            loaded = self._find(existing, rid)
            r = loaded.record
            if r.reviewed:
                raise InvalidRequestError(
                    f"{r.id} has been reviewed; only unreviewed proposals can be rejected"
                )
            source = self._data_dir / loaded.path
            if r.rejection_reason is None:  # not yet recorded (a re-run after a crash skips this)
                updates: dict[str, object] = {"rejection_reason": text}
                if r.type == "decision":
                    updates["status"] = "rejected"
                edit_front_matter(source, updates)
            target = self._archive_path(r)
            if target.exists():
                raise MemoryStoreError(f"{target.name} already exists in the rejected archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            _move(source, target)
            return target.relative_to(self._data_dir).as_posix()

    # -- helpers -------------------------------------------------------------------------

    @staticmethod
    def _find(existing: dict[str, LoadedRecord], record_id: str) -> LoadedRecord:
        rid = record_id.strip().upper()
        loaded = existing.get(rid)
        if loaded is None:
            raise UnknownRecordError(f"no record with id {rid}")
        return loaded

    def _archive_path(self, record: MemoryRecord) -> Path:
        root = self._data_dir / PROJECTS_DIRNAME / record.project
        for directory in (self._data_dir / PROJECTS_DIRNAME, root, root / REJECTED_DIRNAME):
            if directory.is_symlink():
                raise MemoryStoreError(
                    f"refusing to write through a symlinked directory: {directory.name}"
                )
        return root / REJECTED_DIRNAME / TYPE_DIR[record.type] / f"{record.id}.md"

    def find_archived(self, record_id: str) -> str | None:
        """Where a rejected record was archived (relative to the data directory), if it was."""
        found = self._find_archived(record_id.strip().upper())
        return found.relative_to(self._data_dir).as_posix() if found else None

    def _find_archived(self, rid: str) -> Path | None:
        root = self._data_dir / PROJECTS_DIRNAME
        if not root.is_dir():
            return None
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            if REJECTED_DIRNAME in Path(dirpath).parts and f"{rid}.md" in files:
                return Path(dirpath) / f"{rid}.md"
        return None


def _move(source: Path, target: Path) -> None:
    for attempt in range(_MOVE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:  # Windows: the file is briefly in use
            if attempt == _MOVE_ATTEMPTS - 1:
                raise MemoryStoreError(f"cannot move {source.name} (in use?)") from None
            time.sleep(0.1 * (attempt + 1))
