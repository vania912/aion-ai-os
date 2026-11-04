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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from aios.adapters.git_exec import GitWriteRunner
from aios.domain.models import Issue, Registry, Severity
from aios.domain.plans import PlanView
from aios.errors import GitError, InvalidRequestError
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
class AbandonResult:
    worktree_removed: bool
    branch_deleted: bool
    event_recorded: bool


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

    # -- abandon ----------------------------------------------------------------------------

    def preview_abandon(self, plan_id: str) -> AbandonPreview:
        view = self._plans.get(plan_id)
        plan = view.plan
        if view.status not in ("started", "approved"):
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
        if view.execution and registered and exists:
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
            if preview.view.status == "started":
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
        in_use = {v.plan.id for v in mine.values() if v.status == "started"}
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
