"""Audit events and verification results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Scalar = str | int | float | bool | None

_FROZEN = ConfigDict(frozen=True)


class AuditConfig(BaseModel):
    model_config = _FROZEN

    max_event_bytes: int = 4096
    max_arg_chars: int = 200
    max_args: int = 12
    max_list: int = 500
    max_problems: int = 20


class AuditEvent(BaseModel):
    """One line of the audit log. ``hash`` covers every other field, including ``prev``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    ts: str  # UTC, ISO 8601 with milliseconds and 'Z'
    event: Literal["request", "result", "note"]
    op: str
    call_id: str
    actor: str
    project: str | None = None
    level: str | None = None
    verdict: Literal["allow", "deny"] | None = None  # on "request" events
    outcome: Literal["ok", "error", "denied"] | None = None  # on "result" events
    code: str | None = None
    args: dict[str, Scalar] = Field(default_factory=dict)
    meta: dict[str, Scalar] = Field(default_factory=dict)
    prev: str
    hash: str


class VerifyProblem(BaseModel):
    model_config = _FROZEN

    file: str
    line: int
    seq: int | None
    code: str  # unparseable | bad_hash | broken_chain | bad_sequence
    message: str


class VerifyResult(BaseModel):
    model_config = _FROZEN

    ok: bool
    events: int
    last_seq: int
    last_hash: str
    problems: list[VerifyProblem]
    more_problems: bool
