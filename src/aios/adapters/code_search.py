"""Code search backends: ripgrep (preferred) and a literal-only pure-Python fallback.

Both engines return only paths for which ``PathGuard.is_visible`` is true, so deny-listed,
non-text, oversized or out-of-root files never appear, whatever the engine finds.

Known differences (documented in docs/FILES.md): regex only with ripgrep; ripgrep honours
.gitignore, the fallback does not; glob semantics and path ordering can differ in edge cases.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from aios.domain.files import FilesConfig, Searcher, SearchMatch, SearchRequest, SearchResult
from aios.errors import SearchError
from aios.policy.files import looks_binary

if TYPE_CHECKING:
    from aios.policy.files import PathGuard


def _clip(text: str, limit: int) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= limit else text[:limit] + "…"


class RipgrepSearcher:
    name: Literal["ripgrep", "python"] = "ripgrep"

    def __init__(self, executable: str) -> None:
        self._exe = executable

    @classmethod
    def find(cls) -> RipgrepSearcher | None:
        exe = shutil.which("rg")
        return cls(exe) if exe else None

    def _command(self, guard: PathGuard, request: SearchRequest, config: FilesConfig) -> list[str]:
        cmd = [
            self._exe,
            "--json",
            "--no-config",  # ignore RIPGREP_CONFIG_PATH
            "--hidden",  # dotfiles are vetted by the guard, not skipped here
            "--no-follow",
            "--sort=path",  # single-threaded, deterministic order
            f"--max-filesize={guard.policy.max_file_bytes}",
            "--ignore-case" if request.ignore_case else "--case-sensitive",
        ]
        if not request.regex:
            cmd.append("--fixed-strings")
        names = sorted(config.ignored_dir_names | guard.policy.denied_dir_names)
        cmd += [f"--glob=!{name}/" for name in names]
        if request.glob:
            cmd.append(f"--glob={request.glob}")
        # Query and path can never be parsed as options: --regexp=VALUE and the '--' separator.
        cmd += [f"--regexp={request.query}", "--", request.path or "."]
        return cmd

    def search(self, guard: PathGuard, request: SearchRequest, config: FilesConfig) -> SearchResult:
        cmd = self._command(guard, request, config)
        matches: list[SearchMatch] = []
        visible: dict[str, bool] = {}
        truncated = False
        timed_out = threading.Event()
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(  # argument list, shell=False
                cmd, cwd=guard.root, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL
            )

            def _kill() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(config.search_timeout_s, _kill)
            timer.start()
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    match = _parse_match(raw, config.max_line_chars)
                    if match is None:
                        continue
                    if match.path not in visible:
                        visible[match.path] = guard.is_visible(match.path)
                    if not visible[match.path]:
                        continue
                    if len(matches) >= request.max_results:
                        truncated = True
                        proc.kill()
                        break
                    matches.append(match)
            finally:
                timer.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
                proc.wait()
            err.seek(0)
            stderr_text = err.read().decode("utf-8", errors="replace")
        if timed_out.is_set():
            raise SearchError(f"search timed out after {config.search_timeout_s:g}s")
        if not truncated and proc.returncode not in (0, 1) and not matches:
            first = stderr_text.strip().splitlines()[:1]
            detail = first[0].replace(str(guard.root), "<root>") if first else "unknown error"
            raise SearchError(f"ripgrep failed: {detail}")
        return SearchResult(matches=matches, truncated=truncated, engine="ripgrep")


def _parse_match(raw: bytes, line_limit: int) -> SearchMatch | None:
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if event.get("type") != "match":
        return None
    data = event["data"]
    path = data.get("path", {}).get("text")
    text = data.get("lines", {}).get("text")
    line = data.get("line_number")
    if path is None or text is None or line is None:  # non-UTF-8 path/content: skip
        return None
    rel = path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return SearchMatch(path=rel, line=line, text=_clip(text, line_limit))


class PythonSearcher:
    """Literal-only fallback. Regex is refused: Python's ``re`` cannot be interrupted mid-match,
    so a pathological pattern could hang the server."""

    name: Literal["ripgrep", "python"] = "python"

    def search(self, guard: PathGuard, request: SearchRequest, config: FilesConfig) -> SearchResult:
        if request.regex:
            raise SearchError("regex search requires ripgrep (not found); use a literal query")
        deadline = time.monotonic() + config.search_timeout_s
        needle = request.query.lower() if request.ignore_case else request.query
        matches: list[SearchMatch] = []
        glob_re = _compile_glob(request.glob) if request.glob else None
        start = guard.resolve(request.path)
        for rel in self._iter_files(guard, start, config, deadline):
            if glob_re is not None and not (
                glob_re.match(rel)
                or ("/" not in (request.glob or "") and glob_re.match(_name(rel)))
            ):
                continue
            if not guard.is_visible(rel):
                continue
            try:
                data = (guard.root / rel).read_bytes()
            except OSError:
                continue
            if looks_binary(data):
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                hay = line.lower() if request.ignore_case else line
                if needle in hay:
                    if len(matches) >= request.max_results:
                        return SearchResult(matches=matches, truncated=True, engine="python")
                    matches.append(
                        SearchMatch(path=rel, line=number, text=_clip(line, config.max_line_chars))
                    )
            if time.monotonic() > deadline:
                raise SearchError(f"search timed out after {config.search_timeout_s:g}s")
        return SearchResult(matches=matches, truncated=False, engine="python")

    def _iter_files(
        self, guard: PathGuard, start: Path, config: FilesConfig, deadline: float
    ) -> Iterator[str]:
        skip = {n.lower() for n in config.ignored_dir_names | guard.policy.denied_dir_names}
        if start.is_file():
            yield _rel(guard, start)
            return
        # Depth-first, entries in name order with files and directories interleaved (the same
        # order ripgrep's --sort=path produces). Iterative, so deep trees cannot recurse out.
        stack = [iter(self._sorted_entries(start))]
        while stack:
            if time.monotonic() > deadline:
                raise SearchError(f"search timed out after {config.search_timeout_s:g}s")
            entry = next(stack[-1], None)
            if entry is None:
                stack.pop()
            elif entry.is_dir(follow_symlinks=False):
                if entry.name.lower() not in skip:
                    stack.append(iter(self._sorted_entries(Path(entry.path))))
            elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                yield _rel(guard, Path(entry.path))

    @staticmethod
    def _sorted_entries(directory: Path) -> list[os.DirEntry[str]]:
        try:
            return sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return []


def _rel(guard: PathGuard, path: Path) -> str:
    """Project-relative POSIX path *without* resolving symlinks (they may point outside)."""
    return path.relative_to(guard.root).as_posix()


def _name(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _compile_glob(glob: str) -> re.Pattern[str]:
    """Translate a simple glob ('*', '**', '?') to an anchored regex over POSIX paths."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i : i + 2] == "**":
                out.append(".*")
                i += 2
                if glob[i : i + 1] == "/":
                    i += 1
                    out[-1] = "(?:.*/)?"
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def default_searcher() -> Searcher:
    """ripgrep if it is on PATH, otherwise the literal-only Python fallback."""
    return RipgrepSearcher.find() or PythonSearcher()
