"""Memory records (decisions, tasks, ...) and the models returned by memory queries.

Records are Markdown files with YAML front matter; these models validate the front matter.
"""

from __future__ import annotations

import datetime as dt
import hashlib
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

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t \n \r
MAX_EVIDENCE = 20
MAX_EVIDENCE_CHARS = 300
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
    # provenance and review (Phase 2): defaults describe a hand-written, trusted record
    origin: Literal["human", "claude"] = "human"
    reviewed: bool = True
    evidence: list[str] = Field(default_factory=list)
    accepted_by: str | None = None
    accepted_digest: str | None = None
    rejection_reason: str | None = None
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

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_EVIDENCE:
            raise ValueError(f"at most {MAX_EVIDENCE} evidence entries")
        for item in value:
            if (
                not 1 <= len(item.strip()) <= MAX_EVIDENCE_CHARS
                or _BAD_CONTROL.search(item)
                or "\n" in item
            ):
                raise ValueError(
                    f"evidence entries must be single-line text, 1-{MAX_EVIDENCE_CHARS} chars"
                )
        return value

    @field_validator("accepted_by")
    @classmethod
    def _accepted_by(cls, value: str | None) -> str | None:
        if value is not None and (
            not 1 <= len(value.strip()) <= 80 or _BAD_CONTROL.search(value) or "\n" in value
        ):
            raise ValueError("accepted_by must be a single-line name of 1-80 characters")
        return value

    @field_validator("rejection_reason")
    @classmethod
    def _rejection_reason(cls, value: str | None) -> str | None:
        if value is not None and (
            not 1 <= len(value.strip()) <= 300 or _BAD_CONTROL.search(value) or "\n" in value
        ):
            raise ValueError("rejection_reason must be single-line text of 1-300 characters")
        return value

    @field_validator("accepted_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.match(value):
            raise ValueError("accepted_digest must be a 64-character lowercase hex SHA-256")
        return value

    @field_validator("made_by", "why", "alternatives_considered")
    @classmethod
    def _text_lists(cls, value: list[str]) -> list[str]:
        for item in value:
            if _BAD_CONTROL.search(item):
                raise ValueError("control characters are not allowed in text fields")
        return value

    @field_validator("reason_for_change", "body")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        if value is not None and _BAD_CONTROL.search(value):
            raise ValueError("control characters are not allowed in text fields")
        return value

    @field_validator("body")
    @classmethod
    def _normalise_body(cls, value: str) -> str:
        """LF line endings and no outer blank lines, as the file format stores them."""
        return value.replace("\r\n", "\n").strip("\n")

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        if _BAD_CONTROL.search(value) or "\n" in value or "\r" in value:
            raise ValueError("title must be a single line without control characters")
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
    #: `aios doctor` warns about unreviewed records older than this many days.
    stale_proposal_days: int = 14
    #: ... and about a project state file whose `updated` date is older than this.
    stale_state_days: int = 90


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
    origin: str = "human"
    reviewed: bool = True


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
    reviewed: bool = True


class DecisionChain(BaseModel):
    model_config = ConfigDict(frozen=True)

    chain: list[ChainEntry]  # oldest first
    current_id: str  # newest *active, reviewed* decision in the chain (see pending_id)
    notes: list[str]  # inconsistencies found while walking (broken links, cycles, ...)
    #: A proposed successor that is not in force yet (awaiting review), if any.
    pending_id: str | None = None


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


def decision_digest(record: MemoryRecord) -> str:
    """SHA-256 of the reasoning fields of a decision (used to detect later hand edits)."""
    parts = [record.title, *record.why, "|", *record.alternatives_considered, "|", record.body,
             "|", record.supersedes or ""]  # fmt: skip
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
