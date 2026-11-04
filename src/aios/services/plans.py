"""Plans: create, list, show, approve, reject, record outcome and verification.

Rules that live here (and nowhere else):

* The caller supplies content only. Id, date, origin and status come from the server; a plan
  written for Claude (``origin: claude``) is always ``proposed``.
* Approval names the plan hash the approver was shown. If the plan changed since, it is refused.
* Every event passes :func:`aios.domain.plans.check_event` before it is appended, and the same
  function judges the file when it is replayed, so the two cannot drift.
* A plan awaiting approval can be revised only by a new plan (``revises``); the old one can then
  no longer be approved.

Nothing in this module touches a project repository.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from aios.adapters.filelock import exclusive_lock
from aios.adapters.memory_files import INDEX_DIRNAME, scan_memory
from aios.adapters.memory_index import ensure_cache_dir
from aios.adapters.plan_store import PlanStore
from aios.domain.models import Issue, Registry, Severity
from aios.domain.plans import (
    PLAN_ID_RE,
    Origin,
    Outcome,
    Plan,
    PlanContent,
    PlanEvent,
    PlanSummary,
    PlanView,
    Status,
    StepReport,
    check_event,
    plan_hash,
    replay,
)
from aios.errors import InvalidRequestError, MemoryStoreError, UnknownRecordError
from aios.policy.plans import classify

#: Plans in these states are finished; they are not "open" in listings and the snapshot.
CLOSED: frozenset[str] = frozenset({"rejected", "verified", "failed"})


class PlanConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    lock_timeout_s: float = 30.0
    #: Claude-written plans one project may have waiting for approval.
    max_awaiting_per_project: int = 10
    stale_proposal_days: int = 14
    stale_approved_days: int = 30


class PlanService:
    def __init__(
        self,
        data_dir: Path,
        *,
        registry: Registry | None = None,
        config: PlanConfig | None = None,
        today: date | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._store = PlanStore(data_dir)
        self._registry = registry
        self._cfg = config if config is not None else PlanConfig()
        self._today = today

    # -- reading -------------------------------------------------------------------------

    def _load_all(self) -> tuple[dict[str, tuple[Plan, list[PlanEvent], list[str]]], list[Issue]]:
        loaded: dict[str, tuple[Plan, list[PlanEvent], list[str]]] = {}
        issues: list[Issue] = []
        for plan_id in self._store.ids():
            try:
                plan = self._store.read(plan_id)
            except MemoryStoreError as exc:
                issues.append(Issue("-", Severity.ERROR, "invalid_plan", str(exc)))
                continue
            read = self._store.read_events(plan_id)
            loaded[plan_id] = (plan, read.events, read.problems)
        for orphan in self._store.orphan_event_files():
            msg = f"{orphan}.events.jsonl has no plan file"
            issues.append(Issue("-", Severity.ERROR, "orphan_plan_events", msg))
        return loaded, issues

    def _views(self) -> tuple[dict[str, PlanView], list[Issue]]:
        loaded, issues = self._load_all()
        revisions: dict[str, list[str]] = {}
        for plan_id, (plan, _events, _problems) in loaded.items():
            if plan.revises:
                revisions.setdefault(plan.revises, []).append(plan_id)
        views: dict[str, PlanView] = {}
        for plan_id, (plan, events, problems) in loaded.items():
            state = replay(plan, events)
            tier, reasons = classify(plan.files, plan.requested_tier)
            views[plan_id] = PlanView(
                plan=plan, plan_hash=plan_hash(plan), status=state.status, tier=tier,
                tier_reasons=reasons, approval=state.approval,
                approval_stale=state.approval_stale, superseded_by=None, execution=state.execution,
                seal=state.seal,
                reports=state.reports, rejection_reason=state.rejection_reason,
                note=state.note, anomalies=[*problems, *state.anomalies],
            )  # fmt: skip
        # a revision that was itself rejected does not supersede its original
        for original, successors in revisions.items():
            live = [s for s in successors if s in views and views[s].status != "rejected"]
            if original in views and live:
                views[original] = views[original].model_copy(update={"superseded_by": max(live)})
        return views, issues

    def get(self, plan_id: str) -> PlanView:
        plan_id = plan_id.strip().upper()
        if not PLAN_ID_RE.match(plan_id):
            raise InvalidRequestError(f"{plan_id!r} is not a plan id (expected PLAN-0001)")
        views, _ = self._views()
        if plan_id not in views:
            raise UnknownRecordError(f"no plan with id {plan_id}")
        return views[plan_id]

    def list_plans(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        open_only: bool = False,
        limit: int = 50,
    ) -> list[PlanSummary]:
        views, _ = self._views()
        out = []
        for view in sorted(views.values(), key=lambda v: v.plan.id, reverse=True):
            if project and view.plan.project != project:
                continue
            if status and view.status != status:
                continue
            if open_only and (view.status in CLOSED or view.superseded_by):
                continue
            out.append(summarize(view))
        return out[:limit]

    # -- creating ------------------------------------------------------------------------

    def create(self, content: PlanContent, *, origin: Origin = "human") -> PlanView:
        with self.lock():
            views, _ = self._views()
            self._validate_references(content, views)
            if origin == "claude":
                self._claude_rules(content, views)
            plan = Plan(
                **content.model_dump(),
                id=self._store.next_id(),
                origin=origin,
                created=self._today or datetime.now(UTC).date(),
            )
            self._store.create(plan)
        return self.get(plan.id)

    def _validate_references(self, content: PlanContent, views: dict[str, PlanView]) -> None:
        if self._registry is not None:
            self._registry.get(content.project)
        records = {x.record.id: x.record for x in scan_memory(self._data_dir).records}
        for field, kind in (("related_decisions", "decision"), ("related_tasks", "task")):
            for ref in getattr(content, field):
                target = records.get(ref)
                if target is None or target.type != kind:
                    raise InvalidRequestError(f"{field}: {ref} is not an existing {kind}")
                if target.project != content.project:
                    raise InvalidRequestError(f"{field}: {ref} belongs to another project")
        if content.revises:
            old = views.get(content.revises)
            if old is None:
                raise InvalidRequestError(f"revises: {content.revises} does not exist")
            if old.plan.project != content.project:
                raise InvalidRequestError("a plan can only revise a plan of the same project")
            if old.status == "verified":
                raise InvalidRequestError(f"{old.plan.id} is verified; write a new plan instead")
            if old.superseded_by:
                raise InvalidRequestError(
                    f"{old.plan.id} is already revised by {old.superseded_by}"
                )

    def _claude_rules(self, content: PlanContent, views: dict[str, PlanView]) -> None:
        wanted = " ".join(content.title.casefold().split())
        waiting = 0
        for view in views.values():
            p = view.plan
            if p.project != content.project or view.status != "proposed" or view.superseded_by:
                continue
            if p.origin == "claude":
                waiting += 1
            if " ".join(p.title.casefold().split()) == wanted:
                raise InvalidRequestError(
                    f"{p.id} already has this title and is awaiting approval; "
                    "revise it (revises) instead of proposing it twice"
                )
        if waiting >= self._cfg.max_awaiting_per_project:
            raise InvalidRequestError(
                f"project {content.project!r} already has {waiting} Claude plans awaiting "
                f"approval (limit {self._cfg.max_awaiting_per_project}); decide on them first "
                "with `aios plan list`"
            )

    # -- events (human actions, and Claude's reports) -------------------------------------

    def approve(self, plan_id: str, *, by: str, plan_hash_seen: str) -> PlanView:
        """Approve the plan whose content hash the approver was shown."""
        view = self.get(plan_id)
        if plan_hash_seen != view.plan_hash:
            raise InvalidRequestError(
                "the plan changed since it was shown to you; review it again before approving"
            )
        if view.superseded_by:
            raise InvalidRequestError(
                f"{view.plan.id} is replaced by {view.superseded_by}; review that plan instead"
            )
        self._append(view.plan.id, "approved", "cli", by, {"tier": view.tier})
        return self.get(view.plan.id)

    def reject(self, plan_id: str, *, by: str, reason: str) -> PlanView:
        reason = reason.strip()
        if not 1 <= len(reason) <= 500:
            raise InvalidRequestError("a rejection needs a reason of 1-500 characters")
        self._append(plan_id.strip().upper(), "rejected", "cli", by, {"reason": reason})
        return self.get(plan_id)

    def record_outcome(
        self, plan_id: str, *, by: str, outcome: Outcome, note: str = ""
    ) -> PlanView:
        self._append(
            plan_id.strip().upper(), "outcome", "cli", by, {"outcome": outcome, "note": note[:500]}
        )
        return self.get(plan_id)

    def record_report(
        self,
        plan_id: str,
        results: Sequence[StepReport],
        *,
        actor: str,
        by: str,
    ) -> PlanView:
        """File a verification report. It is a *claim* until a human verifies the plan."""
        if actor not in ("cli", "mcp"):
            raise InvalidRequestError("unknown actor")
        data = {"results": [r.model_dump(mode="json") for r in results]}
        self._append(plan_id.strip().upper(), "report", actor, by, data)
        return self.get(plan_id)

    def verify(self, plan_id: str, *, by: str, note: str = "") -> PlanView:
        self._append(plan_id.strip().upper(), "verified", "cli", by, {"note": note[:500]})
        return self.get(plan_id)

    def _append(
        self, plan_id: str, kind: str, actor: str, by: str, data: Mapping[str, object]
    ) -> None:
        with self.lock():
            self.append_locked(plan_id, kind, actor, by, data)

    def append_locked(
        self, plan_id: str, kind: str, actor: str, by: str, data: Mapping[str, object]
    ) -> None:
        """Validate and append one event. The caller holds :meth:`lock`."""
        by = by.strip()
        if not by or len(by) > 100:
            raise InvalidRequestError("who is acting must be 1-100 characters")
        views, _ = self._views()
        view = views.get(plan_id)
        if view is None:
            raise UnknownRecordError(f"no plan with id {plan_id}")
        if view.anomalies:
            raise InvalidRequestError(
                f"{plan_id}'s event file has problems ({view.anomalies[0]}); "
                "run `aios doctor` before changing it"
            )
        events = self._store.read_events(plan_id).events
        event = PlanEvent.model_validate(
            {
                "seq": len(events) + 1,
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "event": kind,
                "actor": actor,
                "by": by,
                "plan_hash": view.plan_hash,
                "data": data,
            }
        )
        problem = check_event(
            view.status,
            event,
            len(view.plan.verification),
            view.reports,
            view.plan.id,
            view.execution is not None,
        )
        if problem:
            raise InvalidRequestError(f"{plan_id}: {problem}")
        self._store.append_event(plan_id, event)

    def all_views(self) -> dict[str, PlanView]:
        """Every readable plan with its derived state."""
        return self._views()[0]

    # -- doctor --------------------------------------------------------------------------

    def check(self, *, today: date | None = None) -> list[Issue]:
        now = today or self._today or datetime.now(UTC).date()
        views, issues = self._views()
        for view in views.values():
            issues += self._check_one(view, views, now)
        return issues

    def _check_one(self, view: PlanView, views: dict[str, PlanView], now: date) -> list[Issue]:
        p = view.plan
        found: list[Issue] = []

        def add(severity: Severity, code: str, message: str) -> None:
            found.append(Issue(p.project, severity, code, f"{p.id}: {message}"))

        if self._registry is not None and p.project not in self._registry.projects:
            add(
                Severity.WARNING, "plan_unknown_project", f"project {p.project!r} is not registered"
            )
        for anomaly in view.anomalies:
            add(Severity.ERROR, "plan_events_inconsistent", anomaly)
        if view.approval_stale:
            add(
                Severity.WARNING,
                "plan_approval_stale",
                "it was approved but its content has changed since; it needs a new approval",
            )
        if p.revises and p.revises not in views:
            add(Severity.WARNING, "plan_revises_missing", f"revises {p.revises}, which is missing")
        waiting = (now - p.created).days
        open_proposal = view.status == "proposed" and not view.superseded_by
        if open_proposal and waiting > self._cfg.stale_proposal_days:
            add(Severity.WARNING, "stale_plan", f"awaiting approval for {waiting} days")
        if view.status == "approved" and view.approval is not None:
            approved_for = (now - date.fromisoformat(view.approval.ts[:10])).days
            if approved_for > self._cfg.stale_approved_days:
                add(
                    Severity.WARNING,
                    "plan_approved_not_done",
                    f"approved {approved_for} days ago with no outcome recorded",
                )
        return found

    # -- helpers -------------------------------------------------------------------------

    def lock(self) -> contextlib.AbstractContextManager[None]:
        cache = self._data_dir / INDEX_DIRNAME
        ensure_cache_dir(cache)
        return exclusive_lock(cache / "plans.lock", self._cfg.lock_timeout_s, "plan write lock")


def summarize(view: PlanView) -> PlanSummary:
    return PlanSummary(
        id=view.plan.id, project=view.plan.project, title=view.plan.title, status=view.status,
        tier=view.tier, origin=view.plan.origin, created=view.plan.created.isoformat(),
        superseded_by=view.superseded_by, approval_stale=view.approval_stale,
    )  # fmt: skip


__all__ = ["CLOSED", "PlanConfig", "PlanService", "Status", "summarize"]
