"""Change plans: the content, its hash, events, and the state derived from them.

A plan's *content* never changes after it is written. Approval, rejection, outcome and
verification are append-only events, each naming the plan hash it applies to. An event whose hash
differs from the plan's current content hash does not apply, so editing a plan after approval
cancels the approval by construction. The status is always derived (:func:`replay`), never stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aios.domain.memory import ID_RE
from aios.domain.models import ProjectId

PLAN_ID_RE = re.compile(r"^PLAN-(\d{4,})\Z")
#: Branches aios creates for a plan, and full commit ids (40 hex, or 64 for sha256 repositories).
BRANCH_RE = re.compile(r"^aios/PLAN-\d{4,}\Z")
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?\Z")
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t \n \r

MAX_FILES = 50
MAX_LIST_ITEMS = 20
FileAction = Literal["create", "modify", "delete"]
Tier = Literal["R1", "R2", "R3"]
Origin = Literal["human", "claude"]
Status = Literal[
    "proposed",
    "approved",
    "started",
    "rejected",
    "executed",
    "partially_completed",
    "failed",
    "verified",
]
EventKind = Literal[
    "approved", "rejected", "started", "abandoned", "sealed", "outcome", "report", "verified"
]
Outcome = Literal["executed", "partially_completed", "failed"]
StepResult = Literal["pass", "fail", "not_run"]


def _text(value: str, field: str, *, single_line: bool = False) -> str:
    if _BAD_CONTROL.search(value):
        raise ValueError(f"{field}: control characters are not allowed")
    value = value.replace("\r\n", "\n").strip()
    if single_line and "\n" in value:
        raise ValueError(f"{field}: must be a single line")
    return value


class PlanFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    action: FileAction
    note: str = Field(default="", max_length=200)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        parts = value.replace("\\", "/").split("/")
        if (
            value.startswith(("/", "\\"))
            or ".." in parts
            or "" in parts
            or ":" in value
            or _BAD_CONTROL.search(value)
        ):
            raise ValueError(f"path {value!r} must be a clean project-relative path")
        return value

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        return _text(value, "note", single_line=True)


class PlanContent(BaseModel):
    """Everything an approver signs off on. This is what the hash covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: ProjectId
    title: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=2000)
    what_will_change: str = Field(min_length=1, max_length=3000)
    why: str = Field(min_length=1, max_length=2000)
    files: list[PlanFile] = Field(default_factory=list, max_length=MAX_FILES)
    risks: list[str] = Field(min_length=1, max_length=MAX_LIST_ITEMS)
    expected_result: str = Field(min_length=1, max_length=2000)
    verification: list[str] = Field(min_length=1, max_length=MAX_LIST_ITEMS)
    requested_tier: Tier | None = None
    related_decisions: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    related_tasks: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    evidence: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    revises: str | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = _text(value, "title", single_line=True)
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("request", "what_will_change", "why", "expected_result")
    @classmethod
    def _long(cls, value: str, info: Any) -> str:
        value = _text(value, str(info.field_name))
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("risks", "verification", "evidence")
    @classmethod
    def _lines(cls, value: list[str], info: Any) -> list[str]:
        out = []
        for item in value:
            item = _text(item, str(info.field_name), single_line=True)
            if not 1 <= len(item) <= 300:
                raise ValueError(f"{info.field_name}: each entry must be 1-300 characters")
            out.append(item)
        return out

    @field_validator("related_decisions", "related_tasks")
    @classmethod
    def _ids(cls, value: list[str], info: Any) -> list[str]:
        kind = "DEC" if info.field_name == "related_decisions" else "TSK"
        for item in value:
            match = ID_RE.match(item)
            if not match or match.group(1) != kind:
                raise ValueError(f"{info.field_name}: {item!r} is not a {kind} id")
        return value

    @field_validator("revises")
    @classmethod
    def _revises(cls, value: str | None) -> str | None:
        if value is not None and not PLAN_ID_RE.match(value):
            raise ValueError("revises must be a plan id such as PLAN-0001")
        return value

    @model_validator(mode="after")
    def _files_unique(self) -> PlanContent:
        paths = [f.path.replace("\\", "/").casefold() for f in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("files: a path is listed twice")
        return self


class Plan(PlanContent):
    id: str
    origin: Origin = "human"
    created: dt.date

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not PLAN_ID_RE.match(value):
            raise ValueError("id must look like PLAN-0001")
        return value


def plan_hash(content: PlanContent) -> str:
    """SHA-256 of the canonical content (id, origin and date are not part of it)."""
    data = content.model_dump(mode="json")
    canonical = json.dumps(
        {"v": 1, **data}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- events ---------------------------------------------------------------------------------


class PlanEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    ts: str
    event: EventKind
    actor: Literal["cli", "mcp"]
    by: str = Field(min_length=1, max_length=100)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data: dict[str, Any] = Field(default_factory=dict)


class StepReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step: int = Field(ge=1, le=MAX_LIST_ITEMS)
    outcome: StepResult
    evidence: str = Field(default="", max_length=500)

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, value: str) -> str:
        return _text(value, "evidence")


class ApprovalInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    by: str
    ts: str
    tier: str
    actor: str


class Finding(BaseModel):
    """One observation from comparing a worktree with its plan (see ``domain/seal.py``)."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    path: str | None = None
    blocking: bool = True


class SealInfo(BaseModel):
    """The latest seal: what the worktree looked like when someone recorded the outcome."""

    model_config = ConfigDict(frozen=True)

    ts: str
    by: str
    outcome: str
    diff_sha256: str
    diff_bytes: int
    head: str
    planned_total: int
    planned_done: int
    changed: int
    ignored: int
    findings: list[Finding]
    findings_truncated: bool


class ExecutionInfo(BaseModel):
    """Where an approved plan is being carried out (an isolated git worktree)."""

    model_config = ConfigDict(frozen=True)

    branch: str
    worktree: str
    base_commit: str
    base_branch: str | None
    by: str
    ts: str


class ReportInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: str
    actor: str
    by: str
    results: list[StepReport]
    all_pass: bool


class Replay(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Status
    approval: ApprovalInfo | None
    approval_stale: bool  # an approval exists for different content
    execution: ExecutionInfo | None
    seal: SealInfo | None
    reports: list[ReportInfo]
    rejection_reason: str | None
    note: str | None
    anomalies: list[str]


def _check_started(event: PlanEvent, plan_id: str | None) -> str | None:
    data = event.data
    branch, base, worktree = data.get("branch"), data.get("base_commit"), data.get("worktree")
    if not isinstance(branch, str) or not BRANCH_RE.match(branch):
        return "a start needs an aios/PLAN-nnnn branch"
    if plan_id is not None and branch != f"aios/{plan_id}":
        return f"the branch {branch} does not belong to {plan_id}"
    if not isinstance(base, str) or not SHA_RE.match(base):
        return "a start needs the full base commit id"
    if not isinstance(worktree, str) or not 1 <= len(worktree) <= 1000:
        return "a start needs the worktree path"
    if _BAD_CONTROL.search(worktree):
        return "the worktree path contains control characters"
    base_branch = data.get("base_branch")
    if base_branch is not None and (
        not isinstance(base_branch, str)
        or len(base_branch) > 200
        or _BAD_CONTROL.search(base_branch)
    ):
        return "bad base branch name"
    return None


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_sealed(event: PlanEvent) -> str | None:
    d = event.data
    if d.get("outcome") not in ("executed", "partially_completed", "failed"):
        return "a seal needs an outcome"
    digest = d.get("diff_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "a seal needs a valid diff_sha256"
    for key in ("head", "base_commit"):
        value = d.get(key)
        if not isinstance(value, str) or not SHA_RE.match(value):
            return f"a seal needs the full {key} commit id"
    for key in ("diff_bytes", "planned_total", "planned_done", "changed", "ignored"):
        if not _count(d.get(key)):
            return f"a seal needs a non-negative {key}"
    findings = d.get("findings")
    if not isinstance(findings, list) or len(findings) > 100:
        return "a seal needs at most 100 findings"
    for item in findings:
        try:
            Finding.model_validate(item)
        except ValueError:
            return "a seal has a malformed finding"
    return None


def check_event(
    status: Status,
    event: PlanEvent,
    steps: int,
    reports: list[ReportInfo],
    plan_id: str | None = None,
    executing: bool = False,
) -> str | None:
    """Why ``event`` is not allowed in ``status`` (None if it is). The one rule set for both
    writing an event and replaying the file."""
    kind = event.event
    if status in ("rejected", "verified", "failed"):
        return f"{kind} after the plan is {status}"
    if kind == "approved":
        return None if status == "proposed" else f"cannot approve a plan that is {status}"
    if kind == "started":
        if status != "approved":
            return f"only an approved plan can be started, not {status}"
        return _check_started(event, plan_id)
    if kind == "sealed":
        if status not in ("started", "partially_completed", "executed") or not executing:
            return f"only a plan being executed can be sealed, not {status}"
        return _check_sealed(event)
    if kind == "abandoned":
        if status not in ("started", "partially_completed", "executed"):
            return f"only an executing plan can be abandoned, not {status}"
        return None
    if kind == "rejected":
        if status in ("proposed", "approved"):
            return None
        if status == "started":
            return "abandon the worktree before rejecting the plan"
        return f"cannot reject a plan that is {status}"
    if kind == "outcome":
        value = event.data.get("outcome")
        if value not in ("executed", "partially_completed", "failed"):
            return "outcome must be executed, partially_completed or failed"
        if status == "started" or executing:
            return (
                "a plan executed in a worktree gets its outcome from the seal (or abandon it first)"
            )
        if status not in ("approved", "partially_completed"):
            return f"cannot record an outcome for a plan that is {status}"
        return None
    if kind == "report":
        if status == "proposed":
            return "a plan must be approved before a verification report can be filed"
        results = event.data.get("results")
        if not isinstance(results, list) or not results:
            return "a report needs per-step results"
        seen: set[int] = set()
        for item in results:
            try:
                step = StepReport.model_validate(item)
            except ValueError as exc:
                return f"bad report entry: {exc}"
            if step.step > steps:
                return f"step {step.step} does not exist (the plan has {steps})"
            if step.step in seen:
                return f"step {step.step} appears twice"
            seen.add(step.step)
        return None
    # verified
    if status not in ("executed", "partially_completed"):
        return f"only an executed or partially completed plan can be verified, not {status}"
    if not reports or not reports[-1].all_pass:
        return "verification needs a report in which every step passed"
    return None


def replay(content: PlanContent, events: list[PlanEvent], plan_id: str | None = None) -> Replay:
    """Derive the plan's state from its events and its current content hash."""
    current = plan_hash(content)
    if plan_id is None and isinstance(content, Plan):
        plan_id = content.id
    steps = len(content.verification)
    status: Status = "proposed"
    approval: ApprovalInfo | None = None
    execution: ExecutionInfo | None = None
    seal: SealInfo | None = None
    stale = False
    reports: list[ReportInfo] = []
    reason: str | None = None
    note: str | None = None
    anomalies: list[str] = []
    for event in events:
        if event.plan_hash != current and event.event != "rejected":
            if event.event == "approved":
                stale = True
            else:
                anomalies.append(f"event {event.seq} ({event.event}) is for different content")
            continue
        problem = check_event(status, event, steps, reports, plan_id, execution is not None)
        if problem:
            anomalies.append(f"event {event.seq}: {problem}")
            continue
        if event.event == "approved":
            status, stale = "approved", False
            approval = ApprovalInfo(
                by=event.by, ts=event.ts, tier=str(event.data.get("tier", "")), actor=event.actor
            )
        elif event.event == "started":
            status = "started"
            execution = ExecutionInfo(
                branch=event.data["branch"],
                worktree=event.data["worktree"],
                base_commit=event.data["base_commit"],
                base_branch=event.data.get("base_branch"),
                by=event.by,
                ts=event.ts,
            )
        elif event.event == "sealed":
            d = event.data
            status = d["outcome"]
            seal = SealInfo(
                ts=event.ts, by=event.by, outcome=d["outcome"], diff_sha256=d["diff_sha256"],
                diff_bytes=d["diff_bytes"], head=d["head"], planned_total=d["planned_total"],
                planned_done=d["planned_done"], changed=d["changed"], ignored=d["ignored"],
                findings=[Finding.model_validate(x) for x in d["findings"]],
                findings_truncated=bool(d.get("findings_truncated", False)),
            )  # fmt: skip
        elif event.event == "abandoned":
            status, execution, seal = "approved", None, None
        elif event.event == "rejected":
            status, reason = "rejected", str(event.data.get("reason", ""))
        elif event.event == "outcome":
            status = event.data["outcome"]
            note = str(event.data.get("note", "")) or note
        elif event.event == "report":
            results = [StepReport.model_validate(r) for r in event.data["results"]]
            covered = {r.step for r in results if r.outcome == "pass"}
            all_pass = covered == set(range(1, steps + 1))
            info = ReportInfo(
                ts=event.ts, actor=event.actor, by=event.by, results=results, all_pass=all_pass
            )
            reports.append(info)
        else:
            status = "verified"
            note = str(event.data.get("note", "")) or note
    return Replay(
        status=status,
        approval=approval,
        approval_stale=stale,
        execution=execution,
        seal=seal,
        reports=reports,
        rejection_reason=reason,
        note=note,
        anomalies=anomalies,
    )


class PlanView(BaseModel):
    """A plan with everything derived from it."""

    model_config = ConfigDict(frozen=True)

    plan: Plan
    plan_hash: str
    status: Status
    tier: Tier
    tier_reasons: list[str]
    approval: ApprovalInfo | None
    approval_stale: bool
    superseded_by: str | None
    execution: ExecutionInfo | None = None
    seal: SealInfo | None = None
    reports: list[ReportInfo]
    rejection_reason: str | None
    note: str | None
    anomalies: list[str]


class PlanSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    project: str
    title: str
    status: str
    tier: str
    origin: str
    created: str
    superseded_by: str | None = None
    approval_stale: bool = False
