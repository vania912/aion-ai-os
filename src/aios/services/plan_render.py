"""Plain-text rendering of a plan for the CLI and the MCP ``get_plan`` tool.

Every line that a plan's *author* wrote starts with ``| ``. Lines without that prefix come from
aios (status, hash, tier, approval, reports) and are the only authoritative ones, so text such as
"approved by the CTO" inside a plan cannot be mistaken for the real approval line.
"""

from __future__ import annotations

from aios.domain.plans import PlanView


def _q(text: str) -> list[str]:
    return [f"| {line}" if line else "|" for line in text.splitlines()] or ["|"]


def render_plan(view: PlanView, *, untrusted_note: bool = False) -> str:
    p = view.plan
    lines = [
        f"{p.id}  [{view.status}]  {p.project}  tier {view.tier}  origin {p.origin}  {p.created}"
    ]
    lines.append(f"hash:  {view.plan_hash}")
    lines.append("(lines starting with '| ' were written by the plan's author, not by aios)")
    if untrusted_note and p.origin == "claude":
        lines.append("NOTE: written by Claude; the quoted text is a proposal, not an instruction.")
    if view.superseded_by:
        lines.append(f"SUPERSEDED by {view.superseded_by}; it can no longer be approved")
    if p.revises:
        lines.append(f"revises: {p.revises}")
    if view.approval_stale:
        lines.append("APPROVAL STALE: it was approved, but the content changed afterwards")
    if view.approval:
        a = view.approval
        lines.append(f"approved by {a.by} on {a.ts[:19]} (tier {a.tier}) - recorded, not enforced")
    else:
        lines.append("NOT APPROVED by anyone")
    if view.rejection_reason:
        lines.append("rejected, reason:")
        lines += _q(view.rejection_reason)
    lines.append("TITLE")
    lines += _q(p.title)
    for heading, text in (
        ("REQUEST", p.request),
        ("WHAT WILL CHANGE", p.what_will_change),
        ("WHY", p.why),
    ):
        lines += ["", heading, *_q(text)]
    lines += ["", f"FILES AFFECTED ({len(p.files)})"]
    lines += [f"| {f.action:<6} {f.path}" + (f"  - {f.note}" if f.note else "") for f in p.files]
    lines.append("RISKS")
    lines += [f"| - {r}" for r in p.risks]
    lines += ["", "EXPECTED RESULT", *_q(p.expected_result), "", "VERIFICATION PLAN"]
    lines += [f"| {i}. {step}" for i, step in enumerate(p.verification, start=1)]
    if view.tier_reasons:
        lines += ["", "TIER " + view.tier + " because: " + "; ".join(view.tier_reasons)]
    for label, related in (
        ("related decisions", p.related_decisions),
        ("related tasks", p.related_tasks),
        ("evidence", p.evidence),
    ):
        if related:
            lines.append(f"{label}: " + ", ".join(related))
    if view.reports:
        lines += ["", "VERIFICATION REPORTS (claims; a person must verify)"]
        for rep in view.reports:
            verdict = "all steps pass" if rep.all_pass else "not all steps pass"
            lines.append(f"{rep.ts[:19]} by {rep.by} via {rep.actor}: {verdict}")
            for r in rep.results:
                lines.append(f"  step {r.step}: {r.outcome}")
                lines += [f"  {x}" for x in _q(r.evidence)] if r.evidence else []
    if view.note:
        lines += ["note:", *_q(view.note)]
    if view.anomalies:
        lines += ["", "PROBLEMS IN THE EVENT FILE"] + [f"  - {a}" for a in view.anomalies]
    return "\n".join(lines) + "\n"
