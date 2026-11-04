"""Starting and abandoning the execution of an approved plan (Phase 4b).

Execution happens in an isolated git worktree on its own branch (``aios/PLAN-nnnn``), created from
a recorded base commit. This module never edits project files: it asks the hardened git write
runner to create or remove a worktree and records the fact as a ``started`` / ``abandoned`` event.

Crash handling: the worktree is created first and the event second, so a ``started`` event always
means a worktree existed when it was written. If the process dies in between, the next ``start``
*adopts* the leftover worktree when it is registered with the expected branch and still sits on the
current commit; anything else is reported and left alone (aios never deletes a directory git does
not know as an aios worktree).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from aios.adapters.git_exec import GitWriteRunner
from aios.domain.gitwrite import WorktreeChanges
from aios.domain.models import Issue, Registry, Severity
from aios.domain.plans import PlanFile, PlanView
from aios.domain.seal import ScopeResult, evaluate_scope
from aios.errors import GitError, InvalidRequestError
from aios.policy.plans import classify
from aios.services.plans import PlanService

RunnerFactory = Callable[[Path], GitWriteRunner]
MAX_LISTED = 10


@dataclass(frozen=True)
class StartPreview:
    view: PlanView
    project_path: Path
    worktree: Path
    branch: str
    base_commit: str
    base_branch: str | None
    warnings: list[str] = field(default_factory=list)
    already_started: bool = False
    adopt: bool = False  # an interrupted start left a worktree that will be adopted


@dataclass(frozen=True)
class AbandonPreview:
    view: PlanView
    worktree: Path
    branch: str
    worktree_registered: bool
    worktree_dir_exists: bool
    branch_exists: bool
    #: Files that differ from the base commit (None when the base is unknown).
    changed: list[str] | None
    diff_bytes: int


@dataclass(frozen=True)
class ScopePreview:
    view: PlanView
    scope: ScopeResult
    snapshot: WorktreeChanges
    head: str
    #: Whether the worktree still matches the latest seal (None if there is no seal).
    matches_seal: bool | None

    @property
    def nothing_changed(self) -> bool:
        return not self.snapshot.changes and not self.snapshot.ignored


@dataclass(frozen=True)
class CheckItem:
    ok: bool
    text: str


@dataclass(frozen=True)
class LandCheck:
    view: PlanView
    items: list[CheckItem]
    notes: list[str]
    commands: list[str]

    @property
    def ready(self) -> bool:
        return all(i.ok for i in self.items)


@dataclass(frozen=True)
class CleanupPreview:
    view: PlanView
    worktree: Path
    branch: str
    worktree_registered: bool
    branch_exists: bool
    landed: bool
    #: Files in the worktree that are not committed on the branch (cleanup would destroy them).
    uncommitted: list[str]


@dataclass(frozen=True)
class AbandonResult:
    worktree_removed: bool
    branch_deleted: bool
    event_recorded: bool


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def neutralise_terminal(text: str) -> str:
    """Replace control characters (terminal escapes, bidirectional overrides) with visible
    ``<U+xxxx>`` markers. Newlines and tabs stay."""
    return _CONTROL.sub(lambda m: f"<U+{ord(m.group()):04X}>", text)


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


class PlanExecutor:
    def __init__(
        self,
        plans: PlanService,
        registry: Registry,
        *,
        worktrees_dir: Path | None = None,
        runner_factory: RunnerFactory = GitWriteRunner,
        stale_started_days: int = 14,
        today: date | None = None,
    ) -> None:
        self._plans = plans
        self.plans = plans
        self._registry = registry
        self._worktrees_dir = worktrees_dir
        self._factory = runner_factory
        self._stale_started_days = stale_started_days
        self._today = today

    # -- where things go --------------------------------------------------------------------

    def worktree_path(self, project: str, plan_id: str) -> Path:
        """``<AIOS_WORKTREES_DIR>/<project>/<plan>`` or, by default, a sibling of the project:
        ``<project dir>.aios-worktrees/<plan>``. Always outside the project tree."""
        config = self._registry.get(project)
        if self._worktrees_dir is not None:
            return self._worktrees_dir / project / plan_id
        root = config.path
        return root.parent / f"{root.name}.aios-worktrees" / plan_id

    def _runner(self, project: str) -> tuple[GitWriteRunner, Path]:
        config = self._registry.get(project)
        if not config.git:
            raise InvalidRequestError(
                f"project {project!r} is registered with git: false; executing a plan needs git"
            )
        if not config.path.is_dir():
            raise InvalidRequestError(f"the directory of project {project!r} does not exist")
        return self._factory(config.path), config.path

    # -- start ------------------------------------------------------------------------------

    def preview_start(self, plan_id: str) -> StartPreview:
        """What ``start`` would do, with every refusal that can be known in advance."""
        view = self._plans.get(plan_id)
        plan = view.plan
        branch = f"aios/{plan.id}"
        worktree = self.worktree_path(plan.project, plan.id)
        if view.status == "started" and view.execution is not None:
            ex = view.execution
            return StartPreview(
                view, self._registry.get(plan.project).path, Path(ex.worktree), ex.branch,
                ex.base_commit, ex.base_branch, already_started=True,
            )  # fmt: skip
        if view.status != "approved":
            raise InvalidRequestError(
                f"{plan.id} is {view.status}; only an approved plan can be started"
            )
        if view.superseded_by:
            raise InvalidRequestError(
                f"{plan.id} is replaced by {view.superseded_by}; start that plan instead"
            )
        if not plan.files:
            raise InvalidRequestError(
                f"{plan.id} lists no files, so there is nothing to execute in a worktree; "
                "record its outcome with `aios plan outcome`"
            )
        runner, project_path = self._runner(plan.project)
        try:
            base = runner.resolve_commit("HEAD")
        except GitError:
            raise InvalidRequestError(
                f"project {plan.project!r} has no commits yet or is not a git repository"
            ) from None
        base_branch = runner.current_branch()
        adopt = self._leftover_check(runner, worktree, branch, base)
        return StartPreview(
            view, project_path, worktree, branch, base, base_branch,
            warnings=self._warnings(runner, view), adopt=adopt,
        )  # fmt: skip

    @staticmethod
    def _leftover_check(runner: GitWriteRunner, worktree: Path, branch: str, base: str) -> bool:
        """True if an interrupted start left a worktree we can adopt; raises if something else is
        in the way."""
        registered = [w for w in runner.worktree_list() if _same(w.path, worktree)]
        if registered:
            info = registered[0]
            if info.branch != branch:
                raise InvalidRequestError(
                    "a different worktree is registered at the planned path; "
                    f"remove it yourself ({worktree})"
                )
            if info.head != base:
                raise InvalidRequestError(
                    f"a worktree from an earlier start exists at {worktree} on another commit; "
                    "run `aios plan abandon` first"
                )
            return True
        if worktree.exists():
            raise InvalidRequestError(
                f"{worktree} exists but git does not know it as a worktree; aios will not touch "
                "it. Move or delete it yourself"
            )
        if runner.branch_exists(branch):
            raise InvalidRequestError(
                f"branch {branch} exists without a worktree; `aios plan abandon` removes it"
            )
        return False

    @staticmethod
    def _warnings(runner: GitWriteRunner, view: PlanView) -> list[str]:
        try:
            dirty = runner.dirty_paths()
        except GitError:
            return ["could not check the main working tree for uncommitted changes"]
        if not dirty:
            return []
        planned = {f.path.replace("\\", "/").casefold() for f in view.plan.files}
        overlap = sorted(p for p in dirty if p.replace("\\", "/").casefold() in planned)
        notes = [
            f"the main working tree has {len(dirty)} uncommitted change(s); the worktree starts "
            "from the last commit and will not contain them"
        ]
        if overlap:
            shown = ", ".join(overlap[:MAX_LISTED]) + (" ..." if len(overlap) > MAX_LISTED else "")
            notes.append(f"{len(overlap)} planned file(s) have uncommitted changes: {shown}")
        return notes

    def start(self, plan_id: str, *, by: str, expected_base: str) -> StartPreview:
        """Create the worktree and record ``started``. ``expected_base`` is the commit the person
        was shown; if the project moved since, nothing happens."""
        with self._plans.lock():
            preview = self.preview_start(plan_id)
            if preview.already_started:
                return preview
            if expected_base != preview.base_commit:
                raise InvalidRequestError(
                    "the project's HEAD changed since it was shown to you; run start again"
                )
            runner, _ = self._runner(preview.view.plan.project)
            if not preview.adopt:
                preview.worktree.parent.mkdir(parents=True, exist_ok=True)
                runner.worktree_add(preview.worktree, preview.branch, preview.base_commit)
            data: dict[str, object] = {
                "branch": preview.branch,
                "worktree": str(preview.worktree),
                "base_commit": preview.base_commit,
                "base_branch": preview.base_branch,
            }
            try:
                self._plans.append_locked(preview.view.plan.id, "started", "cli", by, data)
            except Exception as exc:
                raise InvalidRequestError(
                    f"the worktree exists at {preview.worktree} but starting could not be "
                    f"recorded ({exc}); run `aios plan start` again to adopt it"
                ) from exc
        return preview

    # -- scope check and seal ----------------------------------------------------------------

    def _evaluate(self, view: PlanView) -> tuple[ScopeResult, WorktreeChanges, str]:
        ex = view.execution
        if ex is None:
            raise InvalidRequestError(f"{view.plan.id} is not being executed in a worktree")
        runner, _ = self._runner(view.plan.project)
        worktree = Path(ex.worktree)
        if not worktree.is_dir():
            raise InvalidRequestError(f"the worktree {worktree} is missing; see `aios doctor`")
        snapshot = runner.changes(worktree, ex.base_commit)
        head = runner.worktree_head(worktree)

        def risky(path: str) -> bool:
            tier, _ = classify([PlanFile(path=path, action="modify")])
            return tier == "R3"

        scope = evaluate_scope(view.plan.files, snapshot.changes, snapshot.ignored, is_risky=risky)
        return scope, snapshot, head

    def preview_scope(self, plan_id: str) -> ScopePreview:
        """Compare the worktree with the plan *without recording anything*."""
        view = self._plans.get(plan_id)
        if view.status not in ("started", "partially_completed", "executed", "failed"):
            raise InvalidRequestError(
                f"{view.plan.id} is {view.status}; there is no execution to check"
            )
        scope, snapshot, head = self._evaluate(view)
        matches = None if view.seal is None else view.seal.diff_sha256 == snapshot.diff_sha256
        return ScopePreview(view, scope, snapshot, head, matches)

    def seal(self, plan_id: str, *, by: str, expected_diff_sha256: str) -> ScopePreview:
        """Record the outcome of an execution from the worktree's actual changes.

        ``expected_diff_sha256`` is the diff the person was shown; if the worktree changed since,
        nothing is recorded. The outcome is computed here, never typed by anyone.
        """
        with self._plans.lock():
            view = self._plans.get(plan_id)
            if view.status not in ("started", "partially_completed", "executed"):
                raise InvalidRequestError(
                    f"{view.plan.id} is {view.status}; only a plan being executed can be sealed"
                )
            scope, snapshot, head = self._evaluate(view)
            if snapshot.diff_sha256 != expected_diff_sha256:
                raise InvalidRequestError(
                    "the worktree changed since it was shown to you; check it again before sealing"
                )
            if not snapshot.changes and not snapshot.ignored:
                raise InvalidRequestError("nothing has changed in the worktree yet")
            assert view.execution is not None
            data: dict[str, object] = {
                "outcome": scope.outcome,
                "diff_sha256": snapshot.diff_sha256,
                "diff_bytes": snapshot.diff_bytes,
                "head": head,
                "base_commit": view.execution.base_commit,
                "planned_total": scope.planned_total,
                "planned_done": scope.planned_done,
                "changed": len(snapshot.changes),
                "ignored": len(snapshot.ignored),
                "findings": [f.model_dump(mode="json", exclude_none=True) for f in scope.findings],
                "findings_truncated": scope.findings_truncated,
            }
            self._plans.append_locked(view.plan.id, "sealed", "cli", by, data)
        return ScopePreview(self._plans.get(plan_id), scope, snapshot, head, True)

    def verify(self, plan_id: str, *, by: str, note: str = "") -> PlanView:
        """Mark a plan verified. For a plan executed in a worktree, the worktree must still be
        exactly what was sealed."""
        with self._plans.lock():
            view = self._plans.get(plan_id)
            if view.execution is not None and view.seal is not None:
                _, snapshot, _ = self._evaluate(view)
                if snapshot.diff_sha256 != view.seal.diff_sha256:
                    raise InvalidRequestError(
                        "the worktree changed after it was sealed; seal it again before verifying"
                    )
            self._plans.append_locked(view.plan.id, "verified", "cli", by, {"note": note[:500]})
        return self._plans.get(plan_id)

    def diff_for_review(self, plan_id: str, *, max_bytes: int = 1_000_000) -> tuple[str, bool]:
        """The worktree's diff as text for a person to read, with terminal control characters made
        visible (a diff is untrusted: an escape sequence could hide lines from the reader)."""
        view = self._plans.get(plan_id)
        if view.execution is None:
            raise InvalidRequestError(f"{view.plan.id} is not being executed in a worktree")
        runner, _ = self._runner(view.plan.project)
        raw, truncated = runner.diff_text(
            Path(view.execution.worktree), view.execution.base_commit, max_bytes=max_bytes
        )
        return neutralise_terminal(raw.decode("utf-8", errors="replace")), truncated

    # -- landing ----------------------------------------------------------------------------

    @staticmethod
    def _quote(path: str | Path) -> str:
        return '"' + str(path).replace('"', '\\"') + '"'

    def land_check(self, plan_id: str) -> LandCheck:
        """Check that what is on the plan's branch is exactly what was approved, sealed and
        verified, and print the git commands to land it. Runs nothing that changes the project."""
        view = self._plans.get(plan_id)
        plan, ex, seal = view.plan, view.execution, view.seal
        if ex is None or seal is None:
            raise InvalidRequestError(f"{plan.id} has no sealed execution to land")
        runner, project_path = self._runner(plan.project)
        worktree = Path(ex.worktree)
        items: list[CheckItem] = []
        notes: list[str] = []
        valid = view.approval is not None and not view.approval_stale
        who = f" by {view.approval.by}" if view.approval else ""
        items.append(
            CheckItem(valid, f"the plan is approved{who} and unchanged since (tier {view.tier})")
        )
        items.append(
            CheckItem(
                view.status in ("executed", "verified") and seal.outcome == "executed",
                f"the sealed outcome is 'executed' (now: {view.status}, seal: {seal.outcome})",
            )
        )
        items.append(
            CheckItem(view.status == "verified", "a person verified it (`aios plan verify`)")
        )
        unchanged = False
        if worktree.is_dir():
            unchanged = runner.changes(worktree, ex.base_commit).diff_sha256 == seal.diff_sha256
        items.append(CheckItem(unchanged, "the worktree is exactly as it was sealed"))
        head = runner.branch_head(ex.branch)
        committed = False
        if head is not None:
            committed = runner.commit_diff_sha256(ex.base_commit, head) == seal.diff_sha256
        items.append(
            CheckItem(committed, f"branch {ex.branch} holds commits identical to the sealed diff")
        )
        commands: list[str] = []
        if unchanged and not committed:
            commands += [
                f"git -C {self._quote(worktree)} add -A",
                f'git -C {self._quote(worktree)} commit -m "{plan.title} ({plan.id})"',
            ]
            notes.append(
                "the work is not committed on the branch yet; commit it in the worktree, then run land-check again"
            )
        current = runner.resolve_commit("HEAD")
        target = runner.current_branch() or "a detached HEAD"
        if head is not None and runner.is_ancestor(current, head):
            merge = f"git -C {self._quote(project_path)} merge --ff-only {ex.branch}"
            notes.append(
                f"your branch {target} has not moved since the plan started or is an ancestor: a fast-forward merge works"
            )
        else:
            merge = f"git -C {self._quote(project_path)} merge --no-ff {ex.branch}"
            notes.append(
                f"your branch {target} has moved on since the plan started: this is not a fast-forward, so the merge result is not exactly the sealed diff; review it"
            )
        dirty = set(runner.dirty_paths())
        touched = {c for c in (f.path for f in plan.files)}
        if dirty & touched:
            notes.append(
                "uncommitted changes in your working tree touch planned files: "
                + ", ".join(sorted(dirty & touched)[:MAX_LISTED])
            )
        commands += [merge, f"aios plan cleanup {plan.id}"]
        return LandCheck(view, items, notes, commands)

    def preview_cleanup(self, plan_id: str) -> CleanupPreview:
        view = self._plans.get(plan_id)
        if view.status != "verified" or view.execution is None:
            raise InvalidRequestError(
                f"{view.plan.id} is {view.status}; cleanup is for a verified plan after it was landed"
            )
        runner, _ = self._runner(view.plan.project)
        ex = view.execution
        worktree = Path(ex.worktree)
        registered = any(_same(w.path, worktree) for w in runner.worktree_list())
        head = runner.branch_head(ex.branch)
        if head is None and not registered:
            raise InvalidRequestError(f"{view.plan.id} has no worktree or branch left to clean up")
        # "landed" needs commits of the plan's own on the branch, and the project must contain them
        landed = (
            head is not None
            and head != ex.base_commit
            and runner.is_ancestor(head, runner.resolve_commit("HEAD"))
        )
        uncommitted: list[str] = []
        if registered and worktree.is_dir() and head is not None:
            snapshot = runner.changes(worktree, head)
            uncommitted = [c.path for c in snapshot.changes] + snapshot.ignored
        return CleanupPreview(
            view, worktree, ex.branch, registered, head is not None, landed, uncommitted
        )

    def cleanup(self, plan_id: str) -> CleanupPreview:
        """Remove the worktree and branch of a verified plan whose branch has been merged into the
        project's current commit. Refuses otherwise (use ``abandon`` to discard unlanded work)."""
        with self._plans.lock():
            preview = self.preview_cleanup(plan_id)
            if not preview.landed:
                raise InvalidRequestError(
                    f"branch {preview.branch} has no commits of its own that your current commit "
                    "contains; land it first (see `aios plan land-check`)"
                )
            if preview.uncommitted:
                shown = ", ".join(preview.uncommitted[:MAX_LISTED])
                raise InvalidRequestError(
                    f"the worktree has changes that are not committed on {preview.branch} and "
                    f"would be lost: {shown}"
                )
            runner, _ = self._runner(preview.view.plan.project)
            if preview.worktree_registered:
                if preview.worktree.is_dir():
                    runner.worktree_remove(preview.worktree)
                else:
                    runner.worktree_prune()
            elif preview.worktree.exists():
                raise InvalidRequestError(
                    f"{preview.worktree} exists but git does not know it as a worktree; aios will not delete it"
                )
            if preview.branch_exists:
                runner.branch_delete(preview.branch)
        return preview

    # -- abandon ----------------------------------------------------------------------------

    def preview_abandon(self, plan_id: str) -> AbandonPreview:
        view = self._plans.get(plan_id)
        plan = view.plan
        if view.status not in ("started", "approved", "partially_completed", "executed", "failed"):
            raise InvalidRequestError(f"{plan.id} is {view.status}; there is nothing to abandon")
        branch = f"aios/{plan.id}"
        worktree = (
            Path(view.execution.worktree)
            if view.execution
            else self.worktree_path(plan.project, plan.id)
        )
        runner, _ = self._runner(plan.project)
        registered = any(_same(w.path, worktree) for w in runner.worktree_list())
        exists = worktree.exists()
        has_branch = runner.branch_exists(branch)
        if view.status == "approved" and not (registered or has_branch or exists):
            raise InvalidRequestError(f"{plan.id} has no worktree or branch to abandon")
        changed: list[str] | None = None
        size = 0
        if view.execution and registered and exists:  # what would be lost
            snapshot = runner.changes(worktree, view.execution.base_commit)
            changed = [c.path for c in snapshot.changes] + snapshot.ignored
            size = snapshot.diff_bytes
        return AbandonPreview(view, worktree, branch, registered, exists, has_branch, changed, size)

    def abandon(self, plan_id: str, *, by: str, keep_branch: bool = False) -> AbandonResult:
        """Remove the plan's worktree (and branch unless ``keep_branch``) and record it."""
        with self._plans.lock():
            preview = self.preview_abandon(plan_id)
            runner, _ = self._runner(preview.view.plan.project)
            removed = False
            if preview.worktree_registered:
                if preview.worktree_dir_exists:
                    runner.worktree_remove(preview.worktree)
                else:
                    runner.worktree_prune()  # the directory is already gone; clear git's record
                removed = True
            elif preview.worktree_dir_exists:
                raise InvalidRequestError(
                    f"{preview.worktree} exists but git does not know it as a worktree; aios "
                    "will not delete it. Remove it yourself"
                )
            deleted = False
            if preview.branch_exists and not keep_branch:
                runner.branch_delete(preview.branch)
                deleted = True
            recorded = False
            if preview.view.status in ("started", "partially_completed", "executed"):
                data = {"worktree_removed": removed, "branch_deleted": deleted}
                self._plans.append_locked(preview.view.plan.id, "abandoned", "cli", by, data)
                recorded = True
        return AbandonResult(removed, deleted, recorded)

    # -- doctor -----------------------------------------------------------------------------

    def check(self) -> list[Issue]:
        """Compare plans that claim a worktree with what git actually has."""
        now = self._today or datetime.now(UTC).date()
        views = self._plans.all_views()
        issues: list[Issue] = []
        for project in sorted({v.plan.project for v in views.values()}):
            config = self._registry.projects.get(project)
            if config is None or not config.git or not config.path.is_dir():
                continue
            try:
                issues += self._check_project(project, views, now)
            except GitError as exc:
                issues.append(
                    Issue(
                        project,
                        Severity.WARNING,
                        "plan_git_unavailable",
                        f"cannot inspect git: {exc}",
                    )
                )
        return issues

    def _check_project(self, project: str, views: dict[str, PlanView], now: date) -> list[Issue]:
        runner, _ = self._runner(project)
        registered = runner.worktree_list()
        branches = set(runner.branches())
        mine = {v.plan.id: v for v in views.values() if v.plan.project == project}
        issues: list[Issue] = []

        def add(severity: Severity, code: str, message: str) -> None:
            issues.append(Issue(project, severity, code, message))

        for view in mine.values():
            ex = view.execution
            if view.status != "started" or ex is None:
                continue
            if not any(_same(w.path, ex.worktree) for w in registered):
                add(Severity.ERROR, "plan_worktree_missing",
                    f"{view.plan.id} is started but git has no worktree at {ex.worktree}; "
                    "`aios plan abandon` clears it")  # fmt: skip
            elif not Path(ex.worktree).is_dir():
                add(Severity.ERROR, "plan_worktree_missing",
                    f"{view.plan.id}: the worktree directory {ex.worktree} is gone")  # fmt: skip
            age = (now - date.fromisoformat(ex.ts[:10])).days
            if age > self._stale_started_days:
                add(Severity.WARNING, "plan_started_stale",
                    f"{view.plan.id} was started {age} days ago and has no outcome")  # fmt: skip
        for view in mine.values():
            ex = view.execution
            if (
                view.seal is None
                or ex is None
                or view.status not in ("executed", "partially_completed")
            ):
                continue
            if not Path(ex.worktree).is_dir():
                add(Severity.WARNING, "plan_worktree_missing",
                    f"{view.plan.id} is {view.status} but its worktree {ex.worktree} is gone")  # fmt: skip
                continue
            try:
                now_sha = runner.changes(Path(ex.worktree), ex.base_commit).diff_sha256
            except GitError:
                continue
            if now_sha != view.seal.diff_sha256:
                add(Severity.WARNING, "plan_seal_changed",
                    f"{view.plan.id}: the worktree changed after it was sealed; seal it again")  # fmt: skip
        for view in mine.values():
            ex = view.execution
            if view.status != "verified" or ex is None:
                continue
            head = runner.branch_head(ex.branch)
            still_there = head is not None or any(w.branch == ex.branch for w in registered)
            if (
                head is not None
                and still_there
                and head != ex.base_commit
                and runner.is_ancestor(head, runner.resolve_commit("HEAD"))
            ):
                add(Severity.WARNING, "plan_landed_not_cleaned",
                    f"{view.plan.id} is verified and its branch is merged; "
                    f"`aios plan cleanup {view.plan.id}` removes the worktree and branch")  # fmt: skip
        in_use = {
            v.plan.id
            for v in mine.values()
            if v.status in ("started", "partially_completed", "executed", "verified")
        }
        for info in registered:
            name = info.branch or ""
            plan_id = name.removeprefix("aios/")
            if name.startswith("aios/") and plan_id not in in_use:
                add(Severity.WARNING, "orphan_worktree",
                    f"worktree {info.path} (branch {name}) belongs to no started plan; "
                    "`aios plan abandon` or `git worktree remove` clears it")  # fmt: skip
        with_worktree = {w.branch for w in registered}
        for name in sorted(branches - with_worktree):
            if name.removeprefix("aios/") not in in_use:
                add(Severity.WARNING, "orphan_branch",
                    f"branch {name} has no worktree and no started plan "
                    "(kept on purpose? otherwise `git branch -D` it)")  # fmt: skip
        return issues
