"""``aios plan ...``: create, review and approve plans (a human's commands).

Approval, rejection, outcome and verification are only available here, never as MCP tools.
"""

from __future__ import annotations

import contextlib
import getpass
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from aios.adapters.memory_files import _NoAliasLoader
from aios.config import Settings
from aios.domain.models import Registry
from aios.domain.plans import Outcome, PlanContent, StepReport
from aios.errors import InvalidRequestError
from aios.services.plan_execution import PlanExecutor
from aios.services.plan_render import render_plan
from aios.services.plans import PlanService

DataDirOption = Annotated[
    Path | None, typer.Option("--data-dir", help="Data directory (default: $AIOS_DATA_DIR).")
]
YesOpt = Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")]
AsOpt = Annotated[str | None, typer.Option("--as", help="Name recorded for this action.")]
MAX_PLAN_FILE_BYTES = 128 * 1024


@dataclass(frozen=True)
class Helpers:
    """The few functions of the main CLI module that plan commands share."""

    settings: Callable[[Path | None], Settings]
    registry: Callable[[Path | None], Registry]
    errors: Callable[[], contextlib.AbstractContextManager[None]]
    audit: Callable[[Settings, str, str | None, dict[str, object]], None]


def _me(name: str | None) -> str:
    return name or os.environ.get("AIOS_USER", "").strip() or getpass.getuser()


def _read_plan_file(path: Path, project: str) -> PlanContent:
    try:
        if path.stat().st_size > MAX_PLAN_FILE_BYTES:
            raise InvalidRequestError(f"{path.name} is larger than {MAX_PLAN_FILE_BYTES} bytes")
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_NoAliasLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InvalidRequestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidRequestError(f"{path.name} must be a YAML mapping")
    data.setdefault("project", project)
    if data["project"] != project:
        raise InvalidRequestError(f"the file says project {data['project']!r}, not {project!r}")
    try:
        return PlanContent.model_validate(data)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise InvalidRequestError(f"invalid plan: {detail}") from exc


def register_plan_commands(app: typer.Typer, h: Helpers) -> None:
    plan_app = typer.Typer(
        help="Change plans: create, approve, record outcome.", no_args_is_help=True
    )
    app.add_typer(plan_app, name="plan")

    def service(data_dir: Path | None) -> tuple[Settings, PlanService]:
        settings = h.settings(data_dir)
        return settings, PlanService(settings.data_dir, registry=h.registry(data_dir))

    @contextlib.contextmanager
    def done(
        settings: Settings, op: str, plan_id: str, project: str, extra: dict[str, object]
    ) -> Iterator[None]:
        yield
        h.audit(settings, op, project, {"id": plan_id, **extra})
        typer.echo("(written to disk only; commit your data repo when you are ready)", err=True)

    @plan_app.command("list")
    def plan_list(
        project: str | None = None,
        status: str | None = None,
        open_only: Annotated[bool, typer.Option("--open", help="Hide finished plans.")] = False,
        data_dir: DataDirOption = None,
    ) -> None:
        """List plans, newest first."""
        _, svc = service(data_dir)
        with h.errors():
            rows = svc.list_plans(project=project, status=status, open_only=open_only)
        for r in rows:
            flags = (f" superseded-by {r.superseded_by}" if r.superseded_by else "") + (
                " APPROVAL-STALE" if r.approval_stale else ""
            )
            typer.echo(f"{r.id}\t{r.status}\t{r.tier}\t{r.project}\t{r.origin}\t{r.title}{flags}")
        if not rows:
            typer.echo("no plans")

    @plan_app.command("show")
    def plan_show(plan_id: str, data_dir: DataDirOption = None) -> None:
        """Show a plan in full, with its hash, tier, approval and verification reports."""
        _, svc = service(data_dir)
        with h.errors():
            typer.echo(render_plan(svc.get(plan_id)))

    @plan_app.command("create")
    def plan_create(
        project: str,
        file: Annotated[Path, typer.Option("--file", help="YAML file with the plan content.")],
        data_dir: DataDirOption = None,
    ) -> None:
        """Create a plan from a YAML file (it starts as 'proposed'; approving is a separate step)."""
        settings, svc = service(data_dir)
        with h.errors():
            view = svc.create(_read_plan_file(file, project), origin="human")
        h.audit(settings, "create_plan", project, {"id": view.plan.id, "tier": view.tier})
        typer.echo(f"created {view.plan.id} [{view.status}] tier {view.tier}")
        typer.echo(
            f"review it with `aios plan show {view.plan.id}`; approve with `aios plan approve`"
        )

    @plan_app.command("approve")
    def plan_approve(
        plan_id: str,
        yes: YesOpt = False,
        confirm: Annotated[
            str | None,
            typer.Option(help="R3 plans: the first 8 characters of the plan hash (for scripts)."),
        ] = None,
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """Approve the plan you are shown. The approval is bound to its content hash."""
        settings, svc = service(data_dir)
        me = _me(as_name)
        with h.errors():
            view = svc.get(plan_id)
            typer.echo(render_plan(view, untrusted_note=True))
            if view.status != "proposed":
                raise InvalidRequestError(
                    f"{view.plan.id} is {view.status}; only a proposed plan can be approved"
                )
            if view.tier == "R3":
                typed = (
                    confirm
                    if (yes and confirm)
                    else typer.prompt(
                        f"R3 plan. Type the first 8 characters of the hash ({view.plan_hash[:8]}) to approve"
                    )
                )
                if typed.strip().lower() != view.plan_hash[:8]:
                    typer.echo("hash did not match; nothing changed")
                    raise typer.Exit(code=1)
            elif not yes and not typer.confirm("Approve this plan?", default=False):
                typer.echo("cancelled; nothing changed")
                raise typer.Exit(code=1)
            with done(
                settings,
                "approve_plan",
                view.plan.id,
                view.plan.project,
                {"tier": view.tier, "hash": view.plan_hash[:12]},
            ):
                approved = svc.approve(view.plan.id, by=me, plan_hash_seen=view.plan_hash)
        typer.echo(
            f"approved {approved.plan.id} (tier {approved.tier}, hash {approved.plan_hash[:12]})"
        )
        typer.echo("note: this is a record. aios does not yet stop a change that was not approved.")

    @plan_app.command("reject")
    def plan_reject(
        plan_id: str,
        reason: Annotated[str, typer.Option(help="Why (stored with the plan).")],
        yes: YesOpt = False,
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """Reject a plan (or withdraw its approval before any outcome). Final."""
        settings, svc = service(data_dir)
        with h.errors():
            view = svc.get(plan_id)
            typer.echo(f"{view.plan.id} [{view.status}] {view.plan.title}")
            if not yes and not typer.confirm("Reject it?", default=False):
                typer.echo("cancelled; nothing changed")
                raise typer.Exit(code=1)
            with done(settings, "reject_plan", view.plan.id, view.plan.project, {}):
                svc.reject(view.plan.id, by=_me(as_name), reason=reason)
        typer.echo(f"rejected {view.plan.id}")

    def executor(data_dir: Path | None) -> tuple[Settings, PlanExecutor]:
        settings = h.settings(data_dir)
        registry = h.registry(data_dir)
        plans = PlanService(settings.data_dir, registry=registry)
        return settings, PlanExecutor(plans, registry, worktrees_dir=settings.worktrees_dir)

    @plan_app.command("start")
    def plan_start(
        plan_id: str,
        yes: YesOpt = False,
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """Create an isolated git worktree and branch for an approved plan, from the project's current commit."""
        settings, ex = executor(data_dir)
        with h.errors():
            preview = ex.preview_start(plan_id)
            p = preview.view.plan
            if preview.already_started:
                typer.echo(f"{p.id} is already started.")
                typer.echo(f"  worktree: {preview.worktree}\n  branch:   {preview.branch}")
                return
            typer.echo(
                f"{p.id}  {p.title}  (tier {preview.view.tier}, approved by {preview.view.approval.by if preview.view.approval else '?'})"
            )
            typer.echo(f"  project:   {p.project} at {preview.project_path}")
            typer.echo(
                f"  base:      {preview.base_commit[:12]} on {preview.base_branch or 'a detached HEAD'}"
            )
            typer.echo(f"  branch:    {preview.branch}")
            typer.echo(
                f"  worktree:  {preview.worktree}"
                + ("  (left by an interrupted start: will be adopted)" if preview.adopt else "")
            )
            for warning in preview.warnings:
                typer.echo(f"  warning: {warning}")
            typer.echo(
                "This writes branch and worktree data into the project's .git. Your working tree is not touched."
            )
            if not yes and not typer.confirm("Start?", default=False):
                typer.echo("cancelled; nothing changed")
                raise typer.Exit(code=1)
            done_preview = ex.start(
                preview.view.plan.id, by=_me(as_name), expected_base=preview.base_commit
            )
        h.audit(
            settings,
            "start_plan",
            p.project,
            {"id": p.id, "branch": preview.branch, "base": preview.base_commit[:12]},
        )
        typer.echo(f"started {p.id}. Work only inside:\n  {done_preview.worktree}")
        typer.echo(
            "Start Claude Code with that folder as its working directory. aios cannot stop edits made elsewhere."
        )
        typer.echo(
            "(written to the project's .git only; commit nothing to your own branch yet)", err=True
        )

    @plan_app.command("abandon")
    def plan_abandon(
        plan_id: str,
        keep_branch: Annotated[
            bool,
            typer.Option(
                "--keep-branch",
                help="Remove the worktree but keep the branch (and any commits on it).",
            ),
        ] = False,
        yes: YesOpt = False,
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """Discard an execution: remove the plan's worktree and branch. Uncommitted work in the worktree is lost."""
        settings, ex = executor(data_dir)
        with h.errors():
            preview = ex.preview_abandon(plan_id)
            p = preview.view.plan
            typer.echo(f"{p.id}  {p.title}  [{preview.view.status}]")
            typer.echo(
                f"  worktree: {preview.worktree}  ({'registered' if preview.worktree_registered else 'not registered'}, {'exists' if preview.worktree_dir_exists else 'missing'})"
            )
            typer.echo(
                f"  branch:   {preview.branch}  ({'exists' if preview.branch_exists else 'absent'})"
                + ("  -> kept" if keep_branch and preview.branch_exists else "")
            )
            if preview.changed:
                typer.echo(f"  {len(preview.changed)} changed or ignored file(s) will be lost:")
                for path in preview.changed[:20]:
                    typer.echo(f"    {path}")
                if len(preview.changed) > 20:
                    typer.echo(f"    ... and {len(preview.changed) - 20} more")
            if not yes and not typer.confirm("Discard it?", default=False):
                typer.echo("cancelled; nothing changed")
                raise typer.Exit(code=1)
            result = ex.abandon(p.id, by=_me(as_name), keep_branch=keep_branch)
        h.audit(
            settings,
            "abandon_plan",
            p.project,
            {
                "id": p.id,
                "worktree_removed": result.worktree_removed,
                "branch_deleted": result.branch_deleted,
            },
        )
        typer.echo(
            f"abandoned {p.id}: worktree {'removed' if result.worktree_removed else 'not present'}, branch {'deleted' if result.branch_deleted else 'kept or absent'}."
        )

    @plan_app.command("outcome")
    def plan_outcome(
        plan_id: str,
        outcome: Annotated[str, typer.Argument(help="executed, partially_completed or failed.")],
        note: str = "",
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """Record what happened to an approved plan. aios does not execute plans; you say so."""
        settings, svc = service(data_dir)
        with h.errors():
            if outcome not in ("executed", "partially_completed", "failed"):
                raise InvalidRequestError("outcome must be executed, partially_completed or failed")
            kind: Outcome = outcome  # type: ignore[assignment]
            view = svc.get(plan_id)
            with done(
                settings, "plan_outcome", view.plan.id, view.plan.project, {"outcome": outcome}
            ):
                after = svc.record_outcome(view.plan.id, by=_me(as_name), outcome=kind, note=note)
        typer.echo(f"{after.plan.id} is now {after.status}")

    @plan_app.command("report")
    def plan_report(
        plan_id: str,
        passed: Annotated[
            list[int] | None, typer.Option("--pass", help="Step that passed.")
        ] = None,
        failed: Annotated[
            list[int] | None, typer.Option("--fail", help="Step that failed.")
        ] = None,
        not_run: Annotated[list[int] | None, typer.Option("--skip", help="Step not run.")] = None,
        evidence: Annotated[str, typer.Option(help="Evidence, applied to every listed step.")] = "",
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """File a verification report (per-step results)."""
        settings, svc = service(data_dir)
        results = (
            [StepReport(step=n, outcome="pass", evidence=evidence) for n in passed or []]
            + [StepReport(step=n, outcome="fail", evidence=evidence) for n in failed or []]
            + [StepReport(step=n, outcome="not_run", evidence=evidence) for n in not_run or []]
        )
        with h.errors():
            view = svc.get(plan_id)
            with done(
                settings, "plan_report", view.plan.id, view.plan.project, {"steps": len(results)}
            ):
                after = svc.record_report(view.plan.id, results, actor="cli", by=_me(as_name))
        typer.echo(
            f"reported on {after.plan.id}: "
            + ("all steps pass" if after.reports[-1].all_pass else "not all steps pass")
        )

    @plan_app.command("verify")
    def plan_verify(
        plan_id: str,
        note: str = "",
        yes: YesOpt = False,
        as_name: AsOpt = None,
        data_dir: DataDirOption = None,
    ) -> None:
        """Mark a plan verified. Needs an executed/partial plan and a report in which every step passed."""
        settings, svc = service(data_dir)
        with h.errors():
            view = svc.get(plan_id)
            typer.echo(render_plan(view, untrusted_note=True))
            if not yes and not typer.confirm(
                "You have checked the evidence. Mark verified?", default=False
            ):
                typer.echo("cancelled; nothing changed")
                raise typer.Exit(code=1)
            with done(settings, "verify_plan", view.plan.id, view.plan.project, {}):
                after = svc.verify(view.plan.id, by=_me(as_name), note=note)
        typer.echo(f"{after.plan.id} is now {after.status}")

    @plan_app.command("check")
    def plan_check(plan_id: str, data_dir: DataDirOption = None) -> None:
        """Exit 0 only if the plan has a valid approval for its current content (for scripts and hooks)."""
        _, svc = service(data_dir)
        with h.errors():
            view = svc.get(plan_id)
        if view.approval is not None and view.status in (
            "approved",
            "executed",
            "partially_completed",
            "verified",
        ):
            typer.echo(
                f"{view.plan.id}: approved by {view.approval.by}, tier {view.tier}, hash {view.plan_hash[:12]}"
            )
            return
        typer.echo(
            f"{view.plan.id}: NOT approved ({view.status}{', approval stale' if view.approval_stale else ''})"
        )
        raise typer.Exit(code=1)
