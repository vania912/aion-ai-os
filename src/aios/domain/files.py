"""Models and ports for the Files service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from aios.policy.files import PathGuard

_FROZEN = ConfigDict(frozen=True)


class FilesConfig(BaseModel):
    """Limits for listing, reading and searching. Defaults keep outputs small and bounded."""

    model_config = _FROZEN

    #: Noise directories skipped in listings and searches. Not a security control: the
    #: deny-list in the policy engine is. Override per deployment.
    ignored_dir_names: frozenset[str] = frozenset(
        {
            "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache",
            ".pytest_cache", ".tox", "dist", "build", ".next",
        }
    )  # fmt: skip
    max_depth: int = 5
    max_list_entries: int = 1000
    #: Cap on characters returned by read_file (Claude Code warns above ~10k tokens).
    max_output_chars: int = 40_000
    max_search_results: int = 200
    max_query_chars: int = 500
    max_line_chars: int = 300
    search_timeout_s: float = 20.0


class FileEntry(BaseModel):
    model_config = _FROZEN

    path: str
    kind: Literal["file", "dir"]
    size: int | None = None


class FileListing(BaseModel):
    model_config = _FROZEN

    path: str
    entries: list[FileEntry]
    truncated: bool


class FileContent(BaseModel):
    model_config = _FROZEN

    path: str
    content: str
    start_line: int
    end_line: int  # last line actually returned (start_line - 1 if nothing returned)
    total_lines: int
    truncated: bool
    size_bytes: int


class SearchRequest(BaseModel):
    model_config = _FROZEN

    query: str
    path: str = ""
    glob: str | None = None
    regex: bool = False
    ignore_case: bool = True
    max_results: int = 50


class SearchMatch(BaseModel):
    model_config = _FROZEN

    path: str
    line: int
    text: str


class SearchResult(BaseModel):
    model_config = _FROZEN

    matches: list[SearchMatch]
    truncated: bool
    engine: Literal["ripgrep", "python"]


class Searcher(Protocol):
    """Port for code search. Implementations must only return paths the guard shows."""

    name: Literal["ripgrep", "python"]

    def search(
        self, guard: PathGuard, request: SearchRequest, config: FilesConfig
    ) -> SearchResult: ...
