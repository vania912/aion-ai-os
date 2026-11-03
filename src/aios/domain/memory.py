"""Memory records (decisions, tasks, ...) and the models returned by memory queries.

Records are Markdown files with YAML front matter; these models validate the front matter.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from aios.domain.models import ProjectId

RecordType = Literal[
    "decision", "assumption", "constraint", "question", "discovery", "task", "note"
]

TYPE_PREFIX: dict[str, str] = {
    "decision": "DEC",
    "assumption": "ASM",
    "constraint": "CON",
    "question": "Q",
    "discovery": "DSC",
    "task": "TSK",
    "note": "NOTE",
}
TYPE_DIR: dict[str, str] = {
    "decision": "decisions",
    "assumption": "assumptions",
    "constraint": "constraints",
    "question": "questions",
    "discovery": "discoveries",
    "task": "tasks",
    "note": "notes",
}
VALID_STATUS: dict[str, frozenset[str]] = {
    "decision": frozenset({"proposed", "active", "superseded", "deprecated", "rejected"}),
    "assumption": frozenset({"unverified", "validated", "invalidated"}),
    "constraint": frozenset({"active", "lifted"}),
    "question": frozenset({"open", "answered", "dropped"}),
    "discovery": frozenset({"active", "obsolete"}),
    "task": frozenset({"open", "in_progress", "blocked", "done", "cancelled"}),
    "note": frozenset({"active", "archived"}),
}
#: Statuses meaning "no longer the current position" for decisions.
INACTIVE_DECISION_STATUSES = frozenset({"superseded", "deprecated", "rejected"})

ID_RE = re.compile(r"^(DEC|ASM|CON|Q|DSC|TSK|NOTE)-(\d{4,})$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_DECISION_ONLY = (
    "made_by",
    "why",
    "alternatives_considered",
    "assumptions",
    "constraints",
    "supersedes",
    "superseded_by",
    "reason_for_change",
)


def _check_ids(values: list[str], field: str) -> list[str]:
    for value in values:
        if not ID_RE.match(value):
            raise ValueError(f"{field}: {value!r} is not a record id like DEC-0001")
    return values


class MemoryRecord(BaseModel):
    """One memory record. Decision-only fields must be empty on other record types."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: RecordType
    id: str
    project: ProjectId
    title: str = Field(min_length=1, max_length=200)
    status: str
    date: dt.date
    tags: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)
    related_tasks: list[str] = Field(default_factory=list)
    related_decisions: list[str] = Field(default_factory=list)
    # decision-only
    made_by: list[str] = Field(default_factory=list)
    why: list[str] = Field(default_factory=list)
    alternatives_considered: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    reason_for_change: str | None = None
    body: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, value: str, info: ValidationInfo) -> str:
        match = ID_RE.match(value)
        if not match:
            raise ValueError(f"id {value!r} must look like DEC-0001")
        rtype = info.data.get("type")
        if rtype is not None and match.group(1) != TYPE_PREFIX[rtype]:
            raise ValueError(f"id prefix {match.group(1)!r} does not match type {rtype!r}")
        return value

    @field_validator("status")
    @classmethod
    def _status(cls, value: str, info: ValidationInfo) -> str:
        rtype = info.data.get("type")
        if rtype is not None and value not in VALID_STATUS[rtype]:
            allowed = ", ".join(sorted(VALID_STATUS[rtype]))
            raise ValueError(f"status {value!r} is not valid for {rtype} (use: {allowed})")
        return value

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not _TAG_RE.match(tag):
                raise ValueError(f"tag {tag!r} must be lowercase letters, digits, '_' or '-'")
        return value

    @field_validator("related_files")
    @classmethod
    def _files(cls, value: list[str]) -> list[str]:
        for path in value:
            parts = path.replace("\\", "/").split("/")
            if path.startswith(("/", "\\")) or ".." in parts or ":" in path or "\x00" in path:
                raise ValueError(f"related_files: {path!r} must be a project-relative path")
        return value

    @field_validator("related_tasks", "related_decisions", "assumptions", "constraints")
    @classmethod
    def _id_lists(cls, value: list[str], info: object) -> list[str]:
        return _check_ids(value, getattr(info, "field_name", "ids"))

    @model_validator(mode="after")
    def _consistency(self) -> MemoryRecord:
        if self.type != "decision":
            used = [f for f in _DECISION_ONLY if getattr(self, f)]
            if used:
                raise ValueError(f"fields only valid on decisions: {', '.join(used)}")
        else:
            for field in ("supersedes", "superseded_by"):
                value = getattr(self, field)
                if value is not None and not (ID_RE.match(value) and value.startswith("DEC-")):
                    raise ValueError(f"{field} must be a decision id like DEC-0001")
            if self.supersedes == self.id or self.superseded_by == self.id:
                raise ValueError("a decision cannot supersede itself")
        return self


class LoadedRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: MemoryRecord
    path: str  # relative to the data directory, POSIX style


class MemoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_search_results: int = 50
    max_list_results: int = 200
    max_body_chars: int = 20_000
    max_query_terms: int = 12
    max_query_chars: int = 300
    max_file_bytes: int = 256 * 1024


class MemoryHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    project: str
    type: str
    title: str
    status: str
    date: str
    superseded_by: str | None
    snippet: str
    score: float
    path: str


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    hits: list[MemoryHit]
    truncated: bool
    layer: Literal["id", "keyword_all", "keyword_any", "none"]


class ListResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    records: list[MemoryHit]
    truncated: bool


class MemoryRecordView(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: MemoryRecord
    path: str
    body_truncated: bool


class ChainEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    status: str
    date: str
    made_by: list[str]
    why: list[str]
    reason_for_change: str | None
    supersedes: str | None
    superseded_by: str | None
    position: Literal["earlier", "requested", "later"]


class DecisionChain(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain: list[ChainEntry]  # oldest first
    current_id: str  # newest decision in the chain
    notes: list[str]  # inconsistencies found while walking (broken links, cycles, ...)


class IndexStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int
    fingerprint: str
    built_at: str
    record_count: int


class IndexStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_count: int
    issue_count: int
    built_at: str
