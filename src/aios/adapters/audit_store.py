"""Append-only, hash-chained JSONL audit store.

Layout: ``<audit dir>/YYYY-MM.jsonl`` (UTC month), one JSON event per line. Every event holds a
global sequence number, the hash of the previous event and its own hash, so edits, deletions and
reordering are detectable by :meth:`JsonlAuditStore.verify`.

Not tamper-proof: anyone who can write the files can recompute a consistent chain, and removing
the *end* of the log cannot be detected from the log alone (record :meth:`anchor` elsewhere).

Appends from several processes (CLI, MCP server) are serialised with an OS file lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aios.adapters.filelock import exclusive_lock
from aios.domain.audit import AuditEvent, VerifyProblem, VerifyResult
from aios.errors import AuditError

AUDIT_DIRNAME = "audit"
_MONTH_FILE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])\.jsonl$")
_TAIL_BYTES = 64 * 1024


def event_hash(data: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the event without its ``hash`` field."""
    body = {k: v for k, v in data.items() if k != "hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _exclusive_lock(path: Path, timeout_s: float) -> contextlib.AbstractContextManager[None]:
    """The audit lock (module-level so tests can hold it directly)."""
    return exclusive_lock(path, timeout_s, "audit lock")


def _parse(line: str) -> AuditEvent | None:
    try:
        return AuditEvent.model_validate_json(line)
    except ValidationError:
        return None


class JsonlAuditStore:
    def __init__(
        self, directory: Path, *, max_event_bytes: int = 4096, lock_timeout_s: float = 30.0
    ) -> None:
        self._dir = directory
        self._max_event_bytes = max_event_bytes
        self._lock_timeout_s = lock_timeout_s

    # -- write ---------------------------------------------------------------------------

    def append(self, fields: Mapping[str, Any]) -> AuditEvent:
        """Append one event. ``fields`` must not contain ``seq``, ``prev`` or ``hash``."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with _exclusive_lock(self._dir / ".lock", self._lock_timeout_s):
                files = self._files()
                seq, prev = self._tail_state(files)
                data = {**fields, "seq": seq + 1, "prev": prev}
                data["hash"] = event_hash(data)
                event = AuditEvent.model_validate(data)
                line = (event.model_dump_json() + "\n").encode("utf-8")
                if len(line) > self._max_event_bytes:
                    raise AuditError(f"audit event exceeds {self._max_event_bytes} bytes")
                self._write_line(self._target_file(event.ts, files), line)
                return event
        except OSError as exc:
            raise AuditError(f"cannot write audit log: {exc.strerror or exc}") from exc

    def _target_file(self, ts: str, files: list[Path]) -> Path:
        month = ts[:7]
        if files and files[-1].stem > month:  # clock moved backwards: keep the chain's file order
            month = files[-1].stem
        return self._dir / f"{month}.jsonl"

    @staticmethod
    def _write_line(path: Path, line: bytes) -> None:
        is_new = not path.exists()
        needs_newline = False
        if not is_new and path.stat().st_size > 0:
            with path.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                needs_newline = f.read(1) != b"\n"  # a torn write left no line ending
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (b"\n" if needs_newline else b"") + line)
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- read ----------------------------------------------------------------------------

    def _files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(
            (p for p in self._dir.iterdir() if _MONTH_FILE.match(p.name) and p.is_file()),
            key=lambda p: p.name,
        )

    def _tail_state(self, files: list[Path]) -> tuple[int, str]:
        """(last seq, last hash) of the newest parseable event, or (0, '') for an empty log."""
        for path in reversed(files):
            size = path.stat().st_size
            if size == 0:
                continue
            with path.open("rb") as f:
                f.seek(max(0, size - _TAIL_BYTES))
                chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.splitlines()
            if size > _TAIL_BYTES:
                lines = lines[1:]  # first line may be cut in half
            for line in reversed(lines):
                event = _parse(line)
                if event is not None:
                    return event.seq, event.hash
            if size <= _TAIL_BYTES:
                continue  # file holds only unparseable lines; look further back
            raise AuditError(f"cannot find the end of the audit chain in {path.name}")
        return 0, ""

    def iter_newest_first(self) -> Iterator[AuditEvent]:
        """Parseable events, newest first (unparseable lines are skipped; see verify)."""
        for path in reversed(self._files()):
            for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
                event = _parse(line)
                if event is not None:
                    yield event

    def anchor(self) -> tuple[int, str]:
        """(last seq, last hash): record it somewhere the log's writer cannot edit."""
        return self._tail_state(self._files())

    def verify(self, *, max_problems: int = 20) -> VerifyResult:
        problems: list[VerifyProblem] = []
        more = False
        count = 0
        last_seq, last_hash = 0, ""

        def problem(file: str, line: int, seq: int | None, code: str, message: str) -> None:
            nonlocal more
            if len(problems) < max_problems:
                problems.append(
                    VerifyProblem(file=file, line=line, seq=seq, code=code, message=message)
                )
            else:
                more = True

        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                event = _parse(line)
                if event is None:
                    problem(
                        path.name, number, None, "unparseable", "line is not a valid audit event"
                    )
                    continue
                count += 1
                if event.hash != event_hash(event.model_dump()):
                    problem(
                        path.name, number, event.seq, "bad_hash", "event does not match its hash"
                    )
                if event.prev != last_hash:
                    problem(
                        path.name,
                        number,
                        event.seq,
                        "broken_chain",
                        "prev does not match the previous event",
                    )
                if event.seq != last_seq + 1:
                    problem(
                        path.name, number, event.seq, "bad_sequence", f"expected seq {last_seq + 1}"
                    )
                last_seq, last_hash = event.seq, event.hash
        return VerifyResult(
            ok=not problems,
            events=count,
            last_seq=last_seq,
            last_hash=last_hash,
            problems=problems,
            more_problems=more,
        )
