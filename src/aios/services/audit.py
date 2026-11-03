"""Audit service: record every operation (request, then result) and read the log back.

What is logged: operation, project, permission level, verdict, outcome, error code, sanitised
arguments, and small result counts. What is **never** logged: file contents, search hits, diffs,
record bodies, exception messages. Arguments are truncated and redacted on a best-effort basis
(patterns for common credentials); redaction is not a guarantee.

Failure policy: if the ``request`` event cannot be written the operation is **not executed**
(fail closed). If the ``result`` event cannot be written the operation has already happened, so
the result is returned and a warning is logged to stderr.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

from aios.adapters.audit_store import AUDIT_DIRNAME, JsonlAuditStore
from aios.domain.audit import AuditConfig, AuditEvent, Scalar, VerifyResult
from aios.errors import AiosError, AuditError, InvalidRequestError, PolicyViolation
from aios.policy.operations import OperationPolicy

T = TypeVar("T")
log = logging.getLogger("aios.audit")

_OP_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CALL_RE = re.compile(r"^[0-9a-f]{16}$")
_ACTOR_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,39}$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,39}$")
_OUTCOMES = ("ok", "error", "denied")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Best-effort credential patterns. Order matters: specific before generic.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "[redacted-private-key]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
        "[redacted-jwt]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted-aws-key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[redacted-github-token]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[redacted-slack-token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth)\b"
            r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"\b[A-Za-z0-9+/_-]{48,}={0,2}"), "[redacted-long-token]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_args(args: Mapping[str, object] | None, config: AuditConfig) -> dict[str, Scalar]:
    """Bounded, printable, redacted copy of call arguments (scalars only)."""
    out: dict[str, Scalar] = {}
    for key, value in (args or {}).items():
        if len(out) >= config.max_args:
            out["_more_args"] = True
            break
        if not _KEY_RE.match(key):
            continue
        if value is None or isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str):
            text = _CONTROL.sub(" ", redact(value))
            out[key] = (
                text if len(text) <= config.max_arg_chars else text[: config.max_arg_chars] + "…"
            )
        elif isinstance(value, list | tuple | set | frozenset | dict):
            out[key] = f"<{type(value).__name__} of {len(value)}>"
        else:
            out[key] = f"<{type(value).__name__}>"
    return out


class AuditLog:
    def __init__(
        self,
        data_dir: Path,
        *,
        actor: str,
        config: AuditConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        store: JsonlAuditStore | None = None,
    ) -> None:
        if not _ACTOR_RE.match(actor):
            raise ValueError("actor must be a short lowercase name such as 'cli' or 'mcp'")
        self._actor = actor
        self._cfg = config if config is not None else AuditConfig()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = store or JsonlAuditStore(
            data_dir / AUDIT_DIRNAME, max_event_bytes=self._cfg.max_event_bytes
        )

    # -- writing -------------------------------------------------------------------------

    def run(
        self,
        operation: str,
        func: Callable[[], T],
        *,
        policy: OperationPolicy,
        project: str | None = None,
        args: Mapping[str, object] | None = None,
        summarize: Callable[[T], Mapping[str, Scalar]] | None = None,
    ) -> T:
        """Authorise, log the request, run ``func``, log the result.

        ``summarize`` turns a result into small counts for the log (never content).
        """
        if not _OP_RE.match(operation):
            raise InvalidRequestError("operation name is not valid")
        call_id = uuid.uuid4().hex[:16]
        decision = policy.authorize(operation)
        level = decision.level.value if decision.level else None
        safe_args = sanitize_args(args, self._cfg)
        self._emit(
            "request",
            operation,
            call_id,
            project=project,
            level=level,
            verdict="allow" if decision.allowed else "deny",
            args=safe_args,
        )  # a failure here propagates: the operation does not run
        if not decision.allowed:
            self._emit_result(operation, call_id, project, level, "denied", "operation_denied", {})
            raise PolicyViolation("operation_denied", f"{operation}: {decision.reason}")

        started = time.monotonic()
        try:
            result = func()
        except PolicyViolation as exc:
            self._emit_result(
                operation, call_id, project, level, "denied", exc.code, _elapsed(started)
            )
            raise
        except InvalidRequestError:
            self._emit_result(
                operation, call_id, project, level, "error", "invalid_request", _elapsed(started)
            )
            raise
        except AiosError as exc:
            self._emit_result(
                operation, call_id, project, level, "error", type(exc).__name__, _elapsed(started)
            )
            raise
        except Exception:
            self._emit_result(
                operation, call_id, project, level, "error", "internal_error", _elapsed(started)
            )
            raise
        meta = _elapsed(started)
        if summarize is not None:
            try:
                meta.update(summarize(result))
            except Exception:  # a broken summariser must not break the operation
                meta["summary_error"] = True
        self._emit_result(operation, call_id, project, level, "ok", None, meta)
        return result

    def note(
        self,
        operation: str,
        *,
        project: str | None = None,
        args: Mapping[str, object] | None = None,
    ) -> AuditEvent:
        """Record a free-standing event (for example a manual action)."""
        if not _OP_RE.match(operation):
            raise InvalidRequestError("operation name is not valid")
        return self._emit(
            "note",
            operation,
            uuid.uuid4().hex[:16],
            project=project,
            args=sanitize_args(args, self._cfg),
        )

    def _emit_result(
        self,
        op: str,
        call_id: str,
        project: str | None,
        level: str | None,
        outcome: Literal["ok", "error", "denied"],
        code: str | None,
        meta: Mapping[str, Scalar],
    ) -> None:
        try:
            self._emit(
                "result",
                op,
                call_id,
                project=project,
                level=level,
                outcome=outcome,
                code=code,
                meta=sanitize_args(meta, self._cfg),
            )
        except AuditError as exc:
            # The operation already ran; do not hide its result, but make the gap loud.
            log.error("audit: could not record the result of %s (%s): %s", op, call_id, exc)

    def _emit(
        self,
        event: str,
        op: str,
        call_id: str,
        *,
        project: str | None = None,
        level: str | None = None,
        verdict: str | None = None,
        outcome: str | None = None,
        code: str | None = None,
        args: Mapping[str, Scalar] | None = None,
        meta: Mapping[str, Scalar] | None = None,
    ) -> AuditEvent:
        fields: dict[str, object] = {
            "ts": self._clock()
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "event": event,
            "op": op,
            "call_id": call_id,
            "actor": self._actor,
            "project": project,
            "level": level,
            "verdict": verdict,
            "outcome": outcome,
            "code": code,
            "args": dict(args or {}),
            "meta": dict(meta or {}),
        }
        return self._store.append(fields)

    # -- reading -------------------------------------------------------------------------

    def list_events(
        self,
        *,
        limit: int = 50,
        op: str | None = None,
        project: str | None = None,
        outcome: str | None = None,
        call_id: str | None = None,
        since: str | None = None,
    ) -> list[AuditEvent]:
        """Newest first. ``since`` is an ISO 8601 date or datetime (UTC if no zone is given)."""
        if not 1 <= limit <= self._cfg.max_list:
            raise InvalidRequestError(f"limit must be between 1 and {self._cfg.max_list}")
        if op is not None and not _OP_RE.match(op):
            raise InvalidRequestError("op is not a valid operation name")
        if project is not None and not _PROJECT_RE.match(project):
            raise InvalidRequestError("project must be a lowercase project id")
        if outcome is not None and outcome not in _OUTCOMES:
            raise InvalidRequestError(f"outcome must be one of: {', '.join(_OUTCOMES)}")
        if call_id is not None and not _CALL_RE.match(call_id):
            raise InvalidRequestError("call id must be 16 hex characters")
        since_ts = _parse_since(since)
        out: list[AuditEvent] = []
        for event in self._store.iter_newest_first():
            if since_ts is not None and event.ts < since_ts:
                break  # events are in time order, so everything older is older still
            if (
                (op is None or event.op == op)
                and (project is None or event.project == project)
                and (outcome is None or event.outcome == outcome)
                and (call_id is None or event.call_id == call_id)
            ):
                out.append(event)
                if len(out) >= limit:
                    break
        return out

    def get_call(self, call_id: str) -> list[AuditEvent]:
        """All events of one call, oldest first."""
        return list(reversed(self.list_events(limit=self._cfg.max_list, call_id=call_id)))

    def verify(self) -> VerifyResult:
        return self._store.verify(max_problems=self._cfg.max_problems)

    def anchor(self) -> tuple[int, str]:
        return self._store.anchor()


def audit_dir_exists(data_dir: Path) -> bool:
    return (data_dir / AUDIT_DIRNAME).is_dir()


def _elapsed(started: float) -> dict[str, Scalar]:
    return {"duration_ms": round((time.monotonic() - started) * 1000)}


def _parse_since(since: str | None) -> str | None:
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidRequestError("since must be an ISO 8601 date or datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
