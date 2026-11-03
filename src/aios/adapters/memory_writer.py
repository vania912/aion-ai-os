"""Write memory records to the data directory: serialise, create atomically, allocate ids, and
make *targeted* edits to existing files.

Callers hold the memory write lock (see ``services/memory_write.py``); nothing here locks.

Safety properties (each covered by a test):
* A record's path comes only from its validated project, type and id, never from free text, and is
  refused if ``projects/``, the project directory or the type directory is a symlink.
* Files are written to a temporary name and moved into place with ``os.replace`` (retried when
  Windows briefly locks the target), so a reader never sees a half-written record.
* Edits replace only whitelisted single-line scalar values; every other byte, including comments,
  quoting, line endings and a BOM, is preserved; the result is re-validated before it is written.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import yaml

from aios.adapters.memory_files import PROJECTS_DIRNAME, parse_record
from aios.domain.memory import TYPE_DIR, TYPE_PREFIX, MemoryRecord
from aios.errors import MemoryStoreError

#: Front-matter keys the tools and CLI may edit in an existing record.
EDITABLE_KEYS: frozenset[str] = frozenset(
    {"status", "superseded_by", "reviewed", "accepted_by", "accepted_digest", "rejection_reason"}
)
_FRONT = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")
_VALUE = (
    r"(?P<value>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\n]|'')*'|[^#\s][^#\n]*?|)"  # quoted, plain, or empty
)
_REPLACE_ATTEMPTS = 5
#: Windows opens files in text mode (LF -> CRLF on write) unless told otherwise; 0 elsewhere.
_O_BINARY = getattr(os, "O_BINARY", 0)


class UnsupportedEditError(MemoryStoreError):
    """The record file has a shape the targeted editor will not touch (nothing was written)."""


# -- serialisation ------------------------------------------------------------------------


def render_record(record: MemoryRecord) -> str:
    """Markdown with YAML front matter. Only fields that differ from their defaults are written."""
    front: dict[str, object] = {
        "id": record.id,
        "type": record.type,
        "project": record.project,
        "title": record.title,
        "status": record.status,
        "date": record.date,
    }
    if record.origin != "human":
        front["origin"] = record.origin
    if not record.reviewed:
        front["reviewed"] = False
    for name in (
        "made_by", "why", "alternatives_considered", "assumptions", "constraints",
        "supersedes", "superseded_by", "reason_for_change", "related_files", "related_tasks",
        "related_decisions", "tags", "evidence", "accepted_by", "accepted_digest",
        "rejection_reason",
    ):  # fmt: skip
        value = getattr(record, name)
        if value not in (None, [], ""):
            front[name] = value
    dumped = yaml.safe_dump(
        front, sort_keys=False, allow_unicode=True, default_flow_style=False, width=4096
    )
    body = record.body.replace("\r\n", "\n").strip("\n")
    return f"---\n{dumped}---\n" + (body + "\n" if body else "")


# -- paths and ids ------------------------------------------------------------------------


def record_path(data_dir: Path, record: MemoryRecord) -> Path:
    """Where a record lives. Built only from validated fields; symlinked directories refused."""
    root = data_dir / PROJECTS_DIRNAME
    path = root / record.project / TYPE_DIR[record.type] / f"{record.id}.md"
    for directory in (root, root / record.project, path.parent):
        if directory.is_symlink():
            raise MemoryStoreError(
                f"refusing to write through a symlinked directory: {directory.name}"
            )
    return path


def allocate_id(data_dir: Path, record_type: str) -> str:
    """Next free id for a type, above every id ever used in the store (including rejected ones and
    files that failed to parse), so an id is never reused."""
    prefix = TYPE_PREFIX[record_type]
    pattern = re.compile(rf"^{prefix}-(\d+)\.md$")
    highest = 0
    root = data_dir / PROJECTS_DIRNAME
    if root.is_dir():
        for _dirpath, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                match = pattern.match(name)
                if match:
                    highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1:04d}"


# -- atomic file replacement --------------------------------------------------------------


def replace_atomic(path: Path, data: bytes, *, must_not_exist: bool = False) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")  # not *.md: scans ignore it
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o666)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if must_not_exist and path.exists():
            raise MemoryStoreError(f"refusing to overwrite an existing record: {path.name}")
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: target briefly in use (antivirus, indexer, reader)
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise MemoryStoreError(f"cannot replace {path.name} (in use?)") from None
                time.sleep(0.1 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)
    if os.name == "posix":  # make the rename itself durable (best effort)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def create_record_file(data_dir: Path, record: MemoryRecord) -> Path:
    path = record_path(data_dir, record)
    replace_atomic(path, render_record(record).encode("utf-8"), must_not_exist=True)
    return path


# -- targeted edits -----------------------------------------------------------------------


def _format_value(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if _PLAIN_VALUE.match(text) and text.lower() not in (
        "true",
        "false",
        "null",
        "yes",
        "no",
        "on",
        "off",
    ):
        return text
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a valid YAML scalar


def edit_front_matter(path: Path, updates: Mapping[str, object]) -> MemoryRecord:
    """Set whitelisted single-line scalar keys in a record file, changing nothing else.

    Raises :class:`UnsupportedEditError` (and writes nothing) if the file has an unusual shape or
    the edited record would not validate.
    """
    bad = set(updates) - EDITABLE_KEYS
    if bad:
        raise UnsupportedEditError(f"keys cannot be edited: {', '.join(sorted(bad))}")
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UnsupportedEditError(f"{path.name} is not valid UTF-8") from None
    crlf = "\r\n" in text
    if crlf:
        if re.search(r"(?<!\r)\n", text):
            raise UnsupportedEditError(f"{path.name} mixes line endings")
        text = text.replace("\r\n", "\n")
    match = _FRONT.match(text)
    if match is None:
        raise UnsupportedEditError(f"{path.name} has no front matter")
    lines = match.group(1).split("\n")

    for key, value in updates.items():
        rendered = _format_value(value)
        indexes = [
            i for i, line in enumerate(lines) if re.match(rf"{re.escape(key)}:(?:[ \t]|$)", line)
        ]
        if len(indexes) > 1:
            raise UnsupportedEditError(f"{path.name} has the key {key!r} more than once")
        if not indexes:
            lines.append(f"{key}: {rendered}")
            continue
        i = indexes[0]
        parsed = re.match(
            rf"(?P<head>{re.escape(key)}:[ \t]*){_VALUE}(?P<comment>[ \t]+#.*)?$", lines[i]
        )
        continues = i + 1 < len(lines) and lines[i + 1][:1] in (" ", "\t")
        if parsed is None or not parsed.group("value") or continues:
            raise UnsupportedEditError(f"{path.name}: {key!r} is not a single-line scalar")
        lines[i] = f"{parsed.group('head')}{rendered}{parsed.group('comment') or ''}"

    try:
        before = parse_record(text)
    except ValueError as exc:
        raise UnsupportedEditError(f"{path.name} is not a valid record: {exc}") from None
    new_text = text[: match.start(1)] + "\n".join(lines) + text[match.end(1) :]
    try:
        after = parse_record(new_text)
    except ValueError as exc:  # RecordParseError subclasses ValueError
        raise UnsupportedEditError(f"edit would make {path.name} invalid: {exc}") from None
    changed = set(updates)
    unchanged_before = before.model_dump(exclude=changed)
    unchanged_after = after.model_dump(exclude=changed)
    if unchanged_before != unchanged_after:
        raise UnsupportedEditError(f"edit would change other fields of {path.name}")

    out = new_text.replace("\n", "\r\n") if crlf else new_text
    replace_atomic(path, (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8"))
    return after
