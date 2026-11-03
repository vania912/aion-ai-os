"""Files service: list, read and search inside one registered project.

Every path goes through ``PathGuard``; this module adds argument validation, output caps and
deterministic ordering. It never builds filesystem paths from user input itself.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from aios.adapters.code_search import default_searcher
from aios.domain.files import (
    FileContent,
    FileEntry,
    FileListing,
    FilesConfig,
    Searcher,
    SearchRequest,
    SearchResult,
)
from aios.domain.models import Registry
from aios.errors import InvalidRequestError, PolicyViolation
from aios.policy.files import FileAccessPolicy, PathGuard, looks_binary

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_GLOB_CHARS = 200


class FileService:
    def __init__(
        self,
        guard: PathGuard,
        searcher: Searcher | None = None,
        config: FilesConfig | None = None,
    ) -> None:
        self._guard = guard
        self._searcher = searcher if searcher is not None else default_searcher()
        self._config = config if config is not None else FilesConfig()

    @property
    def engine(self) -> str:
        return self._searcher.name

    def list_files(self, path: str = "", depth: int = 1, max_entries: int = 200) -> FileListing:
        cfg = self._config
        if not 1 <= depth <= cfg.max_depth:
            raise InvalidRequestError(f"depth must be between 1 and {cfg.max_depth}")
        if not 1 <= max_entries <= cfg.max_list_entries:
            raise InvalidRequestError(f"max_entries must be between 1 and {cfg.max_list_entries}")
        directory = self._guard.resolve(path)
        if not directory.is_dir():
            raise PolicyViolation("not_a_directory", f"not a directory: {path!r}")

        entries: list[FileEntry] = []
        truncated = False

        def walk(current: Path, remaining: int) -> bool:
            """Append entries depth-first; return False once the cap is hit."""
            nonlocal truncated
            try:
                children = sorted(
                    os.scandir(current), key=lambda e: (not e.is_dir(), e.name.lower(), e.name)
                )
            except OSError:
                return True
            for child in children:
                rel = Path(child.path).relative_to(self._guard.root).as_posix()
                is_dir = child.is_dir()
                if is_dir and child.name.lower() in {n.lower() for n in cfg.ignored_dir_names}:
                    continue
                if not self._guard.is_visible(rel):
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    return False
                if is_dir:
                    entries.append(FileEntry(path=rel, kind="dir"))
                    if (
                        remaining > 1
                        and not child.is_symlink()
                        and not walk(Path(child.path), remaining - 1)
                    ):
                        return False
                else:
                    size = child.stat().st_size
                    entries.append(FileEntry(path=rel, kind="file", size=size))
            return True

        walk(directory, depth)
        shown = "" if directory == self._guard.root else self._guard.to_relative(directory)
        return FileListing(path=shown, entries=entries, truncated=truncated)

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> FileContent:
        if start_line < 1:
            raise InvalidRequestError("start_line must be >= 1")
        if end_line is not None and end_line < start_line:
            raise InvalidRequestError("end_line must be >= start_line")
        file = self._guard.check_readable_file(path)
        data = file.read_bytes()
        if looks_binary(data):
            raise PolicyViolation("binary_file", f"file looks binary: {path!r}")
        text = data.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        if total and start_line > total:
            raise InvalidRequestError(f"start_line {start_line} is beyond the end ({total} lines)")

        last = total if end_line is None else min(end_line, total)
        chosen: list[str] = []
        used = 0
        truncated = False
        for line in lines[start_line - 1 : last]:
            cost = len(line) + 1
            if used + cost > self._config.max_output_chars and chosen:
                truncated = True
                break
            chosen.append(line)
            used += cost
        if chosen and used > self._config.max_output_chars:  # a single oversized first line
            chosen[0] = chosen[0][: self._config.max_output_chars]
            truncated = True
        returned_end = start_line + len(chosen) - 1
        return FileContent(
            path=self._guard.to_relative(file),
            content="\n".join(chosen),
            start_line=start_line,
            end_line=returned_end,
            total_lines=total,
            truncated=truncated or returned_end < last,
            size_bytes=len(data),
        )

    def search(
        self,
        query: str,
        *,
        path: str = "",
        glob: str | None = None,
        regex: bool = False,
        ignore_case: bool = True,
        max_results: int = 50,
    ) -> SearchResult:
        cfg = self._config
        if not query or len(query) > cfg.max_query_chars or _CONTROL_CHARS.search(query):
            raise InvalidRequestError(
                f"query must be 1-{cfg.max_query_chars} characters without control characters"
            )
        if not 1 <= max_results <= cfg.max_search_results:
            raise InvalidRequestError(f"max_results must be between 1 and {cfg.max_search_results}")
        if glob is not None:
            _validate_glob(glob)
        directory = self._guard.resolve(path)  # confinement and deny checks for the start path
        shown = "" if directory == self._guard.root else self._guard.to_relative(directory)
        request = SearchRequest(
            query=query,
            path=shown,
            glob=glob,
            regex=regex,
            ignore_case=ignore_case,
            max_results=max_results,
        )
        return self._searcher.search(self._guard, request, cfg)


def _validate_glob(glob: str) -> None:
    if (
        not glob
        or len(glob) > _MAX_GLOB_CHARS
        or _CONTROL_CHARS.search(glob)
        or glob.startswith(("-", "!", "/", "\\"))
        or ".." in glob.replace("\\", "/").split("/")
        or ":" in glob
    ):
        raise InvalidRequestError(
            "glob must be a relative pattern (no '..', ':', leading '-', '!' or '/')"
        )


def build_file_service(
    registry: Registry,
    project_id: str,
    *,
    policy: FileAccessPolicy | None = None,
    searcher: Searcher | None = None,
    config: FilesConfig | None = None,
) -> FileService:
    """Create a FileService for a registered project (raises UnknownProjectError)."""
    project = registry.get(project_id)
    return FileService(PathGuard(project.path, policy), searcher, config)
