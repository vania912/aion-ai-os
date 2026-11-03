"""Memory service (read path): search, list, fetch, decision history and integrity checks.

Files are the source of truth. The SQLite index is rebuilt automatically when the files change
(or the index is missing, corrupt or from another schema version), so callers never manage it.
Nothing here writes to project repositories; the only thing written is the derived index.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

from aios.adapters.memory_files import (
    INDEX_DIRNAME,
    PROJECTS_DIRNAME,
    compute_fingerprint,
    scan_memory,
)
from aios.adapters.memory_index import MemoryIndex, build_match, query_terms
from aios.domain.memory import (
    ID_RE,
    TYPE_DIR,
    ChainEntry,
    DecisionChain,
    IndexStats,
    IndexStatus,
    ListResult,
    LoadedRecord,
    MemoryConfig,
    MemoryHit,
    MemoryRecord,
    MemoryRecordView,
    SearchResult,
)
from aios.domain.models import Issue, Registry, Severity
from aios.errors import InvalidRequestError, UnknownRecordError

_ID_IN_TEXT = re.compile(r"\b(?:DEC|ASM|CON|Q|DSC|TSK|NOTE)-\d{4,}\b", re.IGNORECASE)
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_STATUS_RE = re.compile(r"^[a-z_]{1,20}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class MemoryService:
    def __init__(
        self,
        data_dir: Path,
        *,
        registry: Registry | None = None,
        config: MemoryConfig | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._registry = registry
        self._cfg = config if config is not None else MemoryConfig()
        self._index = MemoryIndex(data_dir / INDEX_DIRNAME / "aios.sqlite")
        self._rebuild_lock = threading.Lock()  # tool calls may run on several threads

    # -- index management ----------------------------------------------------------------

    def rebuild_index(self) -> IndexStats:
        """Force a rebuild from the files. Records with problems are skipped and counted."""
        scan = scan_memory(self._data_dir, max_file_bytes=self._cfg.max_file_bytes)
        status = self._index.rebuild(scan.records, scan.fingerprint)
        return IndexStats(
            record_count=status.record_count,
            issue_count=len(scan.issues),
            built_at=status.built_at,
        )

    def index_status(self) -> tuple[IndexStatus | None, bool]:
        """(status, is_stale). ``status`` is ``None`` if there is no usable index."""
        status = self._index.status()
        stale = status is None or status.fingerprint != compute_fingerprint(self._data_dir)
        return status, stale

    def _ensure_fresh(self) -> None:
        if not self._is_stale():
            return
        with self._rebuild_lock:
            if self._is_stale():  # another thread may have rebuilt while we waited
                self.rebuild_index()

    def _is_stale(self) -> bool:
        status = self._index.status()
        return status is None or status.fingerprint != compute_fingerprint(self._data_dir)

    # -- queries -------------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        record_type: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> SearchResult:
        """Layered keyword search: exact id, then all terms, then any term.

        Superseded/deprecated decisions are returned too (their ``status`` and
        ``superseded_by`` say so): history is part of the point of memory.
        """
        cfg = self._cfg
        if not query.strip() or len(query) > cfg.max_query_chars or "\x00" in query:
            raise InvalidRequestError(f"query must be 1-{cfg.max_query_chars} characters")
        self._check_filters(project, record_type, status, tag)
        if not 1 <= limit <= cfg.max_search_results:
            raise InvalidRequestError(f"limit must be between 1 and {cfg.max_search_results}")
        self._ensure_fresh()

        ids = list(dict.fromkeys(m.group(0).upper() for m in _ID_IN_TEXT.finditer(query)))
        id_hits = [
            _hit_from(loaded)
            for loaded in (self._index.get(i) for i in ids)
            if loaded is not None and _matches(loaded, project, record_type, status, tag)
        ]
        if id_hits:
            return SearchResult(hits=id_hits[:limit], truncated=len(id_hits) > limit, layer="id")

        terms = query_terms(query, max_terms=cfg.max_query_terms)
        if not terms:
            raise InvalidRequestError("query has no searchable words")
        filters = {"project": project, "record_type": record_type, "status": status, "tag": tag}
        rows = self._index.search(build_match(terms, any_term=False), limit=limit + 1, **filters)
        layer = "keyword_all"
        if not rows and len(terms) > 1:
            rows = self._index.search(build_match(terms, any_term=True), limit=limit + 1, **filters)
            layer = "keyword_any"
        if not rows:
            return SearchResult(hits=[], truncated=False, layer="none")
        return SearchResult(hits=rows[:limit], truncated=len(rows) > limit, layer=layer)

    def list_records(
        self,
        *,
        project: str | None = None,
        record_type: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
    ) -> ListResult:
        """Metadata-only listing (the cheapest retrieval layer), newest first."""
        self._check_filters(project, record_type, status, tag)
        if not 1 <= limit <= self._cfg.max_list_results:
            raise InvalidRequestError(f"limit must be between 1 and {self._cfg.max_list_results}")
        self._ensure_fresh()
        rows = self._index.list_records(
            project=project, record_type=record_type, status=status, tag=tag, limit=limit + 1
        )
        return ListResult(records=rows[:limit], truncated=len(rows) > limit)

    def get(self, record_id: str) -> MemoryRecordView:
        loaded = self._load(record_id)
        body = loaded.record.body
        limit = self._cfg.max_body_chars
        record = (
            loaded.record
            if len(body) <= limit
            else loaded.record.model_copy(update={"body": body[:limit]})
        )
        return MemoryRecordView(record=record, path=loaded.path, body_truncated=len(body) > limit)

    def decision_history(self, record_id: str) -> DecisionChain:
        """The supersession chain around a decision, oldest first.

        Follows both pointers, so a chain with a one-sided link (A.superseded_by missing but
        B.supersedes = A) is still found. Problems met on the way are reported in ``notes``.
        """
        start = self._load(record_id)
        if start.record.type != "decision":
            raise InvalidRequestError(f"{start.record.id} is a {start.record.type}, not a decision")
        notes: list[str] = []
        earlier: list[LoadedRecord] = []
        later: list[LoadedRecord] = []
        seen = {start.record.id}

        cursor = start
        while cursor.record.supersedes:
            prev = self._index.get(cursor.record.supersedes)
            if prev is None:
                notes.append(
                    f"{cursor.record.id} supersedes {cursor.record.supersedes}, which is missing"
                )
                break
            if prev.record.id in seen:
                notes.append(f"supersession cycle at {prev.record.id}")
                break
            seen.add(prev.record.id)
            earlier.insert(0, prev)
            cursor = prev

        cursor = start
        while True:
            successors = self._index.successors(cursor.record.id)
            declared = cursor.record.superseded_by
            if declared and declared not in successors:
                nxt_id = declared
                if self._index.get(declared) is None:
                    notes.append(
                        f"{cursor.record.id} is superseded by {declared}, which does not exist"
                    )
                    break
                notes.append(
                    f"{declared} does not point back to {cursor.record.id} via 'supersedes'"
                )
            elif successors:
                nxt_id = declared or successors[0]
                if len(successors) > 1:
                    notes.append(
                        f"{cursor.record.id} has several successors: {', '.join(successors)}"
                    )
            else:
                break
            nxt = self._index.get(nxt_id)
            if nxt is None or nxt.record.id in seen:
                notes.append(f"supersession cycle at {nxt_id}")
                break
            seen.add(nxt.record.id)
            later.append(nxt)
            cursor = nxt

        ordered = [*earlier, start, *later]
        chain = [
            ChainEntry(
                id=r.record.id,
                title=r.record.title,
                status=r.record.status,
                date=r.record.date.isoformat(),
                made_by=r.record.made_by,
                why=r.record.why,
                reason_for_change=r.record.reason_for_change,
                supersedes=r.record.supersedes,
                superseded_by=r.record.superseded_by,
                position="earlier" if r in earlier else ("requested" if r is start else "later"),
            )
            for r in ordered
        ]
        return DecisionChain(chain=chain, current_id=ordered[-1].record.id, notes=notes)

    # -- integrity -----------------------------------------------------------------------

    def check(self) -> list[Issue]:
        """Read the files directly (not the index) and report problems."""
        scan = scan_memory(self._data_dir, max_file_bytes=self._cfg.max_file_bytes)
        issues = list(scan.issues)
        by_id = {x.record.id: x.record for x in scan.records}

        def err(r: MemoryRecord, code: str, msg: str) -> None:
            issues.append(Issue(r.project, Severity.ERROR, code, f"{r.id}: {msg}"))

        def warn(r: MemoryRecord, code: str, msg: str) -> None:
            issues.append(Issue(r.project, Severity.WARNING, code, f"{r.id}: {msg}"))

        if self._registry is not None:
            for project in sorted({r.project for r in by_id.values()}):
                if project not in self._registry.projects:
                    msg = f"memory for {project!r}, which is not in the registry"
                    issues.append(Issue(project, Severity.WARNING, "unknown_project", msg))

        successors_of: dict[str, list[str]] = {}
        for r in by_id.values():
            if r.type == "decision" and r.supersedes:
                successors_of.setdefault(r.supersedes, []).append(r.id)

        for r in by_id.values():
            for field, expected in _REFERENCE_FIELDS:
                for ref in getattr(r, field):
                    target = by_id.get(ref)
                    if target is None or target.type != expected:
                        warn(
                            r,
                            "dangling_reference",
                            f"{field} entry {ref} is not an existing {expected}",
                        )
            if r.type == "decision":
                _check_decision(r, by_id, successors_of, err, warn)

        for target_id, succ in sorted(successors_of.items()):
            if len(succ) > 1:
                first = by_id[succ[0]]
                err(
                    first,
                    "multiple_successors",
                    f"{target_id} is replaced by several decisions: {', '.join(sorted(succ))}",
                )

        reported: set[str] = set()
        for r in by_id.values():
            if r.type == "decision" and r.id not in reported:
                cycle = _find_cycle(r, by_id)
                if cycle:
                    reported.update(cycle)
                    err(r, "supersession_cycle", "cycle: " + " -> ".join([*cycle, cycle[0]]))
        return issues

    # -- helpers -------------------------------------------------------------------------

    def _load(self, record_id: str) -> LoadedRecord:
        normalised = record_id.strip().upper()
        if not ID_RE.match(normalised):
            raise InvalidRequestError("id must look like DEC-0001, TSK-0007, Q-0003, ...")
        self._ensure_fresh()
        loaded = self._index.get(normalised)
        if loaded is None:
            raise UnknownRecordError(f"no record with id {normalised}")
        return loaded

    @staticmethod
    def _check_filters(
        project: str | None, record_type: str | None, status: str | None, tag: str | None
    ) -> None:
        if project is not None and not _PROJECT_RE.match(project):
            raise InvalidRequestError("project must be a lowercase project id")
        if record_type is not None and record_type not in TYPE_DIR:
            raise InvalidRequestError(f"type must be one of: {', '.join(sorted(TYPE_DIR))}")
        if status is not None and not _STATUS_RE.match(status):
            raise InvalidRequestError("status must be lowercase letters and underscores")
        if tag is not None and not _TAG_RE.match(tag):
            raise InvalidRequestError("tag must be lowercase letters, digits, '_' or '-'")


_REFERENCE_FIELDS = (
    ("related_tasks", "task"),
    ("related_decisions", "decision"),
    ("assumptions", "assumption"),
    ("constraints", "constraint"),
)


def _check_decision(
    r: MemoryRecord,
    by_id: dict[str, MemoryRecord],
    successors_of: dict[str, list[str]],
    err: Callable[[MemoryRecord, str, str], None],
    warn: Callable[[MemoryRecord, str, str], None],
) -> None:
    if r.superseded_by:
        target = by_id.get(r.superseded_by)
        if target is None:
            err(r, "superseded_by_missing", f"superseded_by {r.superseded_by} does not exist")
        elif target.type != "decision" or target.project != r.project:
            err(r, "bad_supersession_target", f"{r.superseded_by} is not a decision of {r.project}")
        elif target.supersedes != r.id:
            warn(
                r,
                "not_reciprocal",
                f"superseded_by {target.id}, whose 'supersedes' is {target.supersedes}",
            )
        if r.status != "superseded":
            err(r, "status_mismatch", f"has superseded_by but status is {r.status!r}")
    elif r.status == "superseded" and r.id not in successors_of:
        err(r, "superseded_without_successor", "status is superseded but no decision replaces it")
    if r.supersedes:
        target = by_id.get(r.supersedes)
        if target is None:
            err(r, "supersedes_missing", f"supersedes {r.supersedes}, which does not exist")
        elif target.type != "decision" or target.project != r.project:
            err(r, "bad_supersession_target", f"{r.supersedes} is not a decision of {r.project}")
        else:
            if target.superseded_by != r.id:
                warn(
                    r,
                    "not_reciprocal",
                    f"supersedes {target.id}, whose 'superseded_by' is {target.superseded_by}",
                )
            if target.status != "superseded":
                warn(
                    r,
                    "predecessor_not_superseded",
                    f"supersedes {target.id}, whose status is {target.status!r}",
                )


def _find_cycle(start: MemoryRecord, by_id: dict[str, MemoryRecord]) -> list[str]:
    path = [start.id]
    cur = start
    while cur.supersedes and cur.supersedes in by_id:
        nxt = by_id[cur.supersedes]
        if nxt.id in path:
            return path[path.index(nxt.id) :]
        path.append(nxt.id)
        cur = nxt
    return []


def _matches(
    loaded: LoadedRecord,
    project: str | None,
    record_type: str | None,
    status: str | None,
    tag: str | None,
) -> bool:
    r = loaded.record
    return (
        (project is None or r.project == project)
        and (record_type is None or r.type == record_type)
        and (status is None or r.status == status)
        and (tag is None or tag in r.tags)
    )


def _hit_from(loaded: LoadedRecord) -> MemoryHit:
    r = loaded.record
    return MemoryHit(
        id=r.id,
        project=r.project,
        type=r.type,
        title=r.title,
        status=r.status,
        date=r.date.isoformat(),
        superseded_by=r.superseded_by,
        snippet=r.title,
        score=1.0,
        path=loaded.path,
    )


def memory_dir_exists(data_dir: Path) -> bool:
    return (data_dir / PROJECTS_DIRNAME).is_dir()
