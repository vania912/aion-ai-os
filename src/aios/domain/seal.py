"""Compare what a worktree actually changed with what an approved plan said it would change.

Pure logic, no git: input is the plan's file list and the snapshot of changes; output is a verdict
with findings. The comparison is by *path and action*. It does not look inside files, so a planned
file can still contain an unplanned edit: a person reads the diff.

Verdict rules (decision D8):

* any *blocking* finding -> ``failed`` (the work went outside what was approved);
* otherwise every planned file done -> ``executed``;
* otherwise some done -> ``partially_completed``; none done -> ``failed``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from aios.domain.gitwrite import Change
from aios.domain.plans import Finding, Outcome, PlanFile

_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_ACTION_OF_STATUS = {"A": "create", "M": "modify", "D": "delete"}
MAX_FINDINGS = 100


class ScopeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    planned_total: int
    planned_done: int
    findings: list[Finding]
    findings_truncated: bool = False

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]


def _finding(code: str, message: str, path: str | None = None, *, blocking: bool = True) -> Finding:
    return Finding(code=code, message=message, path=path, blocking=blocking)


def _kind_findings(change: Change) -> list[Finding]:
    """What kind of thing changed (applies to planned and unplanned paths alike)."""
    path = change.path
    found: list[Finding] = []
    if change.is_symlink:
        found.append(_finding("symlink", "a symbolic link was added or changed", path))
    if change.is_gitlink:
        found.append(
            _finding("gitlink", "a nested repository or submodule was added or changed", path)
        )
    if change.mode_changed and not change.is_symlink and change.status != "T":
        said = (
            f"file mode changed ({change.old_mode} -> {change.new_mode}); the plan does not say so"
        )
        found.append(_finding("mode_change", said, path))
    if change.binary:
        found.append(
            _finding("binary", "binary file: its diff cannot be read as text", path, blocking=False)
        )
    return found


def _change_findings(
    change: Change,
    spec: PlanFile | None,
    folded: dict[str, str],
    is_risky: Callable[[str], bool],
) -> tuple[list[Finding], bool]:
    """Findings for one changed path, and whether it counts as a planned file done."""
    path = change.path
    if _BAD_PATH_CHARS.search(path):
        shown = path.encode("utf-8", errors="backslashreplace").decode("ascii", "replace")
        return [_finding("unusual_path", f"unusual file name {shown!r}")], False
    found: list[Finding] = []
    done = False
    if spec is None:
        hint = ""
        if path.casefold() in folded:
            hint = f" (differs only in letter case from planned {folded[path.casefold()]})"
        risk = " (a CI, deploy, dependency or secret-like path)" if is_risky(path) else ""
        found.append(_finding("out_of_scope", f"changed but not in the plan{hint}{risk}", path))
    else:
        action = _ACTION_OF_STATUS.get(change.status)
        if change.status == "T":
            found.append(
                _finding("type_change", "the file's type changed (for example to a symlink)", path)
            )
        elif action != spec.action:
            said = f"the plan says {spec.action}, the worktree shows {action or change.status}"
            found.append(_finding("wrong_action", said, path))
        else:
            done = True
    return found + _kind_findings(change), done


def evaluate_scope(
    files: Sequence[PlanFile],
    changes: Sequence[Change],
    ignored: Sequence[str],
    *,
    is_risky: Callable[[str], bool] = lambda _path: False,
) -> ScopeResult:
    planned = {f.path: f for f in files}
    folded = {f.path.casefold(): f.path for f in files}
    findings: list[Finding] = []
    done: set[str] = set()
    for change in changes:
        found, counted = _change_findings(change, planned.get(change.path), folded, is_risky)
        findings += found
        if counted:
            done.add(change.path)
    for path in ignored:
        findings.append(
            _finding(
                "ignored_file",
                "a git-ignored file exists in the worktree (the diff cannot show it)",
                path,
            )
        )
    contradicted = {f.path for f in findings if f.code in ("wrong_action", "type_change")}
    for spec in files:
        if spec.path not in done and spec.path not in contradicted:
            message = f"planned ({spec.action}) but not changed"
            findings.append(_finding("not_done", message, spec.path, blocking=False))
    outcome: Outcome
    if any(f.blocking for f in findings):
        outcome = "failed"
    elif len(done) == len(files):
        outcome = "executed"
    else:
        outcome = "partially_completed" if done else "failed"
    return ScopeResult(
        outcome=outcome,
        planned_total=len(files),
        planned_done=len(done),
        findings=findings[:MAX_FINDINGS],
        findings_truncated=len(findings) > MAX_FINDINGS,
    )
