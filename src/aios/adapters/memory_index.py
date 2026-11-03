"""SQLite + FTS5 index over memory records. A derived cache: always rebuildable from the files.

* Rebuilt into a temporary file and moved into place atomically, so readers never see a
  half-built index.
* Queries use a read-only connection and bound parameters only. User text never becomes SQL or
  FTS syntax: :func:`build_match` reduces it to quoted tokens.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from aios.domain.memory import (
    IndexStatus,
    LoadedRecord,
    MemoryHit,
    MemoryRecord,
)
from aios.errors import MemoryStoreError

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE records(
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    date TEXT NOT NULL,
    supersedes TEXT,
    superseded_by TEXT,
    path TEXT NOT NULL,
    json TEXT NOT NULL
);
CREATE INDEX records_project_type_status ON records(project, type, status);
CREATE INDEX records_supersedes ON records(supersedes);
CREATE TABLE tags(record_id TEXT NOT NULL, tag TEXT NOT NULL);
CREATE INDEX tags_tag ON tags(tag, record_id);
CREATE VIRTUAL TABLE records_fts USING fts5(
    title, body, extra, tags, tokenize = 'porter unicode61'
);
"""

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = frozenset(
    [
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do", "does", "for", "from",
        "had", "has", "have", "how", "i", "if", "in", "is", "it", "its", "of", "on", "or", "our",
        "so", "that", "the", "their", "then", "there", "these", "this", "to", "was", "we", "were",
        "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your",
    ]
)  # fmt: skip
_SNIPPET_COLUMN = -1  # FTS5 picks the best-matching column


def query_terms(text: str, *, max_terms: int) -> list[str]:
    """Lower-cased word tokens; stop-words dropped unless that would leave nothing."""
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    kept = [t for t in tokens if t not in _STOPWORDS]
    chosen = kept or tokens
    return list(dict.fromkeys(chosen))[:max_terms]


def build_match(terms: Sequence[str], *, any_term: bool) -> str:
    """FTS5 MATCH expression made only of double-quoted tokens (no operators from user text)."""
    quoted = [f'"{t}"' for t in terms]  # tokens are \w+, so they cannot contain a quote
    return (" OR " if any_term else " ").join(quoted)


def ensure_cache_dir(directory: Path) -> None:
    """Create the derived-data directory and keep it out of a git data repository."""
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_bytes(b"*\n")  # bytes: no newline translation on Windows


class MemoryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path

    # -- build ---------------------------------------------------------------------------

    def rebuild(self, records: Sequence[LoadedRecord], fingerprint: str) -> IndexStatus:
        ensure_cache_dir(self.path.parent)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.unlink(missing_ok=True)
        built_at = datetime.now(UTC).isoformat(timespec="seconds")
        conn = sqlite3.connect(tmp)
        try:
            try:
                conn.executescript(_SCHEMA)
            except sqlite3.OperationalError as exc:
                raise MemoryStoreError(f"SQLite FTS5 is not available: {exc}") from exc
            for loaded in records:
                self._insert(conn, loaded)
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES(?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("fingerprint", fingerprint),
                    ("built_at", built_at),
                    ("record_count", str(len(records))),
                ],
            )
            conn.commit()
        except BaseException:
            conn.close()
            tmp.unlink(missing_ok=True)
            raise
        conn.close()
        self._replace(tmp)
        return IndexStatus(
            schema_version=SCHEMA_VERSION,
            fingerprint=fingerprint,
            built_at=built_at,
            record_count=len(records),
        )

    @staticmethod
    def _insert(conn: sqlite3.Connection, loaded: LoadedRecord) -> None:
        r = loaded.record
        cursor = conn.execute(
            "INSERT INTO records(id, project, type, title, status, date, supersedes, "
            "superseded_by, path, json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                r.id, r.project, r.type, r.title, r.status, r.date.isoformat(),
                r.supersedes, r.superseded_by, loaded.path, r.model_dump_json(),
            ),
        )  # fmt: skip
        extra = " ".join(
            [*r.why, *r.alternatives_considered, *r.made_by, r.reason_for_change or ""]
        )
        conn.execute(
            "INSERT INTO records_fts(rowid, title, body, extra, tags) VALUES(?,?,?,?,?)",
            (cursor.lastrowid, r.title, r.body, extra, " ".join(r.tags)),
        )
        conn.executemany(
            "INSERT INTO tags(record_id, tag) VALUES(?,?)", [(r.id, t) for t in r.tags]
        )

    def _replace(self, tmp: Path) -> None:
        for attempt in range(5):  # Windows refuses to replace a file another process has open
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 4:
                    tmp.unlink(missing_ok=True)
                    raise MemoryStoreError(
                        f"cannot replace index file (in use?): {self.path}"
                    ) from None
                time.sleep(0.1 * (attempt + 1))

    # -- read ----------------------------------------------------------------------------

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise MemoryStoreError(f"cannot open memory index: {exc}") from exc
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        except sqlite3.Error as exc:
            raise MemoryStoreError(f"memory index query failed: {exc}") from exc
        finally:
            conn.close()

    def status(self) -> IndexStatus | None:
        """Current index metadata, or ``None`` if missing, unreadable or from another schema."""
        if not self.path.is_file():
            return None
        try:
            with self._read() as conn:
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            if int(meta["schema_version"]) != SCHEMA_VERSION:
                return None
            return IndexStatus(
                schema_version=SCHEMA_VERSION,
                fingerprint=meta["fingerprint"],
                built_at=meta["built_at"],
                record_count=int(meta["record_count"]),
            )
        except (MemoryStoreError, KeyError, ValueError):
            return None

    def get(self, record_id: str) -> LoadedRecord | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT json, path FROM records WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        return LoadedRecord(record=MemoryRecord.model_validate_json(row["json"]), path=row["path"])

    def successors(self, record_id: str) -> list[str]:
        """Ids of decisions whose ``supersedes`` points at ``record_id`` (oldest first)."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT id FROM records WHERE supersedes = ? ORDER BY date, id", (record_id,)
            ).fetchall()
        return [r["id"] for r in rows]

    def list_records(
        self,
        *,
        project: str | None,
        record_type: str | None,
        status: str | None,
        tag: str | None,
        limit: int,
    ) -> list[MemoryHit]:
        where, params = self._filters(project, record_type, status, tag)
        sql = (
            "SELECT r.id, r.project, r.type, r.title, r.status, r.date, r.superseded_by, r.path "
            f"FROM records r {where} ORDER BY r.date DESC, r.id DESC LIMIT ?"
        )
        with self._read() as conn:
            rows = conn.execute(sql, [*params, limit]).fetchall()
        return [_hit(row, snippet="", score=0.0) for row in rows]

    def search(
        self,
        match: str,
        *,
        project: str | None,
        record_type: str | None,
        status: str | None,
        tag: str | None,
        limit: int,
    ) -> list[MemoryHit]:
        where, params = self._filters(project, record_type, status, tag)
        where = (where + " AND " if where else "WHERE ") + "records_fts MATCH ?"
        sql = (
            "SELECT r.id, r.project, r.type, r.title, r.status, r.date, r.superseded_by, r.path, "
            f"snippet(records_fts, {_SNIPPET_COLUMN}, '[', ']', '…', 16) AS snippet, "
            "bm25(records_fts, 6.0, 1.0, 2.0, 3.0) AS rank "
            "FROM records_fts JOIN records r ON r.rowid = records_fts.rowid "
            f"{where} ORDER BY rank, r.id LIMIT ?"
        )
        with self._read() as conn:
            rows = conn.execute(sql, [*params, match, limit]).fetchall()
        return [
            _hit(row, snippet=row["snippet"] or row["title"], score=round(-row["rank"], 4))
            for row in rows
        ]

    @staticmethod
    def _filters(
        project: str | None, record_type: str | None, status: str | None, tag: str | None
    ) -> tuple[str, list[str]]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (("project", project), ("type", record_type), ("status", status)):
            if value is not None:
                clauses.append(f"r.{column} = ?")
                params.append(value)
        if tag is not None:
            clauses.append("EXISTS (SELECT 1 FROM tags t WHERE t.record_id = r.id AND t.tag = ?)")
            params.append(tag)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def _hit(row: sqlite3.Row, *, snippet: str, score: float) -> MemoryHit:
    return MemoryHit(
        id=row["id"],
        project=row["project"],
        type=row["type"],
        title=row["title"],
        status=row["status"],
        date=row["date"],
        superseded_by=row["superseded_by"],
        snippet=snippet,
        score=score,
        path=row["path"],
    )
