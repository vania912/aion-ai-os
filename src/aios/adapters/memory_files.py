"""Read memory records from the data directory (Markdown files with YAML front matter).

Layout: ``<data_dir>/projects/<project>/<type-dir>/<ID>.md``. One bad file never stops the scan:
it is reported as an :class:`Issue` and skipped. Symlinks are ignored.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from aios.domain.memory import TYPE_DIR, LoadedRecord, MemoryRecord
from aios.domain.models import Issue, Severity

PROJECTS_DIRNAME = "projects"
INDEX_DIRNAME = ".index"
DEFAULT_MAX_FILE_BYTES = 256 * 1024
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.DOTALL)
_FILENAME_RE = re.compile(r"^([A-Z]+-\d+)\.md$")
_PROJECT_DIR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DIR_TO_TYPE = {directory: rtype for rtype, directory in TYPE_DIR.items()}


class RecordParseError(ValueError):
    """The file is not a valid memory record (message is user-facing)."""


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that rejects YAML anchors/aliases.

    Records have no use for them, and a tiny file of nested aliases can expand to gigabytes
    when something later walks the structure (the "billion laughs" pattern).
    """

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.YAMLError(f"YAML aliases are not allowed (line {event.start_mark.line + 1})")
        return super().compose_node(parent, index)


@dataclass(frozen=True)
class ScanResult:
    records: list[LoadedRecord]
    issues: list[Issue]
    fingerprint: str


def parse_record(text: str) -> MemoryRecord:
    """Parse one record file's text. Raises :class:`RecordParseError` with a clear reason."""
    text = text.removeprefix("﻿").replace("\r\n", "\n")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise RecordParseError("missing YAML front matter (file must start with '---')")
    try:
        front = yaml.load(match.group(1), Loader=_NoAliasLoader)
    except yaml.YAMLError as exc:
        raise RecordParseError(f"invalid YAML front matter: {exc}") from exc
    if not isinstance(front, dict) or not all(isinstance(k, str) for k in front):
        raise RecordParseError("front matter must be a mapping with text keys")
    if "body" in front:
        raise RecordParseError("'body' is reserved; write the body after the closing '---'")
    try:
        return MemoryRecord.model_validate({**front, "body": match.group(2).strip("\n")})
    except ValidationError as exc:
        lines = [
            f"{'.'.join(str(p) for p in e['loc']) or '<record>'}: {e['msg']}" for e in exc.errors()
        ]
        raise RecordParseError("; ".join(lines)) from exc


def _candidates(data_dir: Path) -> list[tuple[str, str, Path]]:
    """(project_dir_name, type_dir_name, file) for every .md file in a known type directory."""
    root = data_dir / PROJECTS_DIRNAME
    out: list[tuple[str, str, Path]] = []
    if not root.is_dir():
        return out
    for project_entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not project_entry.is_dir(follow_symlinks=False):
            continue
        for type_dir in sorted(TYPE_DIR.values()):
            directory = Path(project_entry.path) / type_dir
            if not directory.is_dir() or directory.is_symlink():
                continue
            for entry in sorted(os.scandir(directory), key=lambda e: e.name):
                if entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                    out.append((project_entry.name, type_dir, Path(entry.path)))
    return out


def compute_fingerprint(data_dir: Path) -> str:
    """Cheap change detector over (relative path, size, mtime) of all candidate files.

    Edits that keep both size and mtime unchanged are not detected; ``aios index rebuild``
    always forces a rebuild.
    """
    digest = hashlib.sha256()
    for _, _, file in _candidates(data_dir):
        stat = file.stat()
        rel = file.relative_to(data_dir).as_posix()
        digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def scan_memory(data_dir: Path, *, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> ScanResult:
    records: list[LoadedRecord] = []
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    root = data_dir / PROJECTS_DIRNAME

    def issue(project: str, severity: Severity, code: str, message: str) -> None:
        issues.append(Issue(project, severity, code, message))

    if root.is_dir():
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False) and not _PROJECT_DIR_RE.match(entry.name):
                issue(
                    "-",
                    Severity.ERROR,
                    "bad_project_dir",
                    f"projects/{entry.name}: not a valid project id",
                )

    for project_dir, type_dir, file in _candidates(data_dir):
        rel = file.relative_to(data_dir).as_posix()
        if not _PROJECT_DIR_RE.match(project_dir):
            continue  # already reported
        name_match = _FILENAME_RE.match(file.name)
        if not name_match:
            issue(
                project_dir,
                Severity.WARNING,
                "unexpected_file",
                f"{rel}: file name is not <ID>.md; ignored",
            )
            continue
        size = file.stat().st_size
        if size > max_file_bytes:
            issue(
                project_dir,
                Severity.ERROR,
                "file_too_large",
                f"{rel}: {size} bytes exceeds {max_file_bytes}",
            )
            continue
        try:
            record = parse_record(file.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            issue(project_dir, Severity.ERROR, "invalid_record", f"{rel}: not valid UTF-8")
            continue
        except OSError as exc:
            issue(project_dir, Severity.ERROR, "unreadable", f"{rel}: {exc.strerror or exc}")
            continue
        except RecordParseError as exc:
            issue(project_dir, Severity.ERROR, "invalid_record", f"{rel}: {exc}")
            continue
        problems = []
        if name_match.group(1) != record.id:
            problems.append(f"file name says {name_match.group(1)} but id is {record.id}")
        if _DIR_TO_TYPE[type_dir] != record.type:
            problems.append(f"stored under {type_dir}/ but type is {record.type}")
        if record.project != project_dir:
            problems.append(f"stored under projects/{project_dir}/ but project is {record.project}")
        if problems:
            issue(project_dir, Severity.ERROR, "location_mismatch", f"{rel}: {'; '.join(problems)}")
            continue
        if record.id in seen:
            issue(
                project_dir,
                Severity.ERROR,
                "duplicate_id",
                f"{rel}: id {record.id} already used by {seen[record.id]}",
            )
            continue
        seen[record.id] = rel
        records.append(LoadedRecord(record=record, path=rel))
    return ScanResult(records=records, issues=issues, fingerprint=compute_fingerprint(data_dir))
