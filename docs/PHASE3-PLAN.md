# Phase 3 plan — planning (plans, hash-bound approval, verification records)

Status: **assumed defaults, implementation in progress.** You asked me to lay out the plan and keep
executing; the decisions below are my recommendations, taken as defaults, not choices you made.
Each can be changed; the cost of changing is stated. Date: 2026-11-03.
Legend: **[A]** assumption · **[J]** my judgement · **[V]** verified in this repo's earlier work.

## 1. What Phase 3 delivers (and does not)

Delivers: a **plan** record Claude (or you) can write before any change is made, a state machine,
approval **bound to the plan's content hash**, and records of the outcome and of verification.

Does **not** deliver: any execution. aios still cannot modify a project repository (Phase 4,
ADR-0006). After you approve a plan, Claude Code carries it out with its own tools under its own
permission rules; aios records and checks, it does not enforce yet. Be clear about what that means:
**in Phase 3 an approved plan is a record and a checkable fact, not a technical barrier.**

## 2. Plan content (what a plan must say)

Matches the original spec: WHAT WILL CHANGE · WHY · FILES AFFECTED · RISKS · EXPECTED RESULT ·
VERIFICATION PLAN, plus the original request. Fields: `title`, `request`, `what_will_change`, `why`,
`files[] {path, action: create|modify|delete, note}`, `risks[]` (at least one), `expected_result`,
`verification[]` (at least one step), optional `requested_tier`, `related_decisions`,
`related_tasks`, `evidence`, `revises`. Paths are project-relative and validated like memory
`related_files`. Limits: 50 files, bounded text lengths.

The **plan hash** is SHA-256 over a canonical encoding of the content fields (project, title,
request, text fields, files, lists, requested tier, relations). It excludes id, origin and date.

## 3. Decisions

| # | Decision | Options | Recommendation [J] and trade-off |
|---|---|---|---|
| P1 | How a plan is stored | **A.** Immutable content file (`plans/PLAN-0001.yaml`) + append-only events file (`PLAN-0001.events.jsonl`); status is *derived*. **B.** One file with a status field edited in place. **C.** SQLite table. | **A.** Approval is an event naming the hash, so editing the content makes the approval invalid by construction, with no in-place edit machinery. Cost: two files per plan, status needs a small derivation step. B reuses the Phase 2 editor but a content edit and a status edit can then hide each other. C hides the data from git review. |
| P2 | Who can approve | **A.** Human CLI only (`aios plan approve`), no MCP approve tool. **B.** MCP `approve_plan` guarded by a Claude Code `ask` rule. | **A**, same reasoning as ADR-0009 D2: the approver should not be a tool Claude can call. Cost: you switch to a terminal. The same bypass caveat applies (Claude Code can run shell commands; see MCP.md, "Keeping the review step human-only"). |
| P3 | Who sets outcome and `verified` | **A.** Only you, by CLI; Claude may *file a verification report* (per step pass/fail/not_run + evidence) that is shown as an unverified claim. **B.** Claude sets them. | **A.** aios does not execute in Phase 3, so it cannot know the outcome; a status set by Claude would be assertion. `verified` additionally requires a report with every step `pass`. Cost: more of your time per plan. |
| P4 | Risk tier | **A.** Computed from the file list by fixed rules; Claude may raise but not lower. **B.** Claude declares it. | **A** (spec E.3). Rules are a heuristic [A]: delete, CI/deploy/dependency/secret-like paths, or more than 20 files -> R3; any create/modify -> R2; no files -> R1. R3 approval requires typing the first 8 characters of the hash. |
| P5 | Editing a plan | **A.** New plan with `revises: PLAN-n`; the old one shows as superseded and can no longer be approved. **B.** Edit in place. | **A**, which follows from P1. |
| P6 | Claude's plan quota | 10 plans per project awaiting approval; an exact repeat of a waiting title is refused. | Same idea as the Phase 2 quota. The number is my estimate [A]. |
| P7 | Searchable? | Plans are **not** in the SQLite index; `list_plans` scans the (few) files. | Keeps the index schema unchanged. Revisit if you keep hundreds of plans. |

## 4. State machine

```
proposed --approve--> approved --outcome--> executed ----verify----> verified
   |                     |  \--> partially_completed --verify--> verified
   |                     |  \--> failed
   +--reject--> rejected <+ (also from approved, before any outcome)
```

- `approved` is only true while the latest approval event's hash equals the current content hash;
  otherwise the plan reads as `proposed` with `approval_stale` (a hand edit after approval).
- A plan that another plan `revises` is shown `superseded_by` and cannot be approved.
- Claude-created plans are always `proposed`. Outcome and `verified` need an *approved* plan.
- Status words match the spec: planned (= proposed), executed, verified, failed, partially completed.

## 5. Sub-steps (each ends with tests, mutation checks, CI)

| Step | Content | Exit criteria |
|---|---|---|
| 3a | Domain model + hash, tier rules, store (immutable file + events), `PlanService` (create, get, list, approve, reject, check), CLI `aios plan create/list/show/approve/reject/check` | approval invalidated by any content edit; tier cannot be lowered; hostile text round-trips; concurrent creates give unique ids |
| 3b | Outcome and verification: `aios plan outcome`, `aios plan report` (human), `aios plan verify`; `aios doctor` plan checks | `verified` impossible without approval + passing report; stale approval and orphan events reported |
| 3c | MCP: `list_plans`, `get_plan` (read-only); `create_plan`, `record_verification` (behind `--allow-propose`); prompt `prepare_change_plan`; "Plans" section in the snapshot; docs; a real headless Claude Code run | propose -> human approve (hash-bound) -> report -> verify, all audited; no tool can approve |

## 6. Risks specific to this phase

- **False sense of safety.** Approval is recorded, not enforced, until Phase 4. The docs and the
  snapshot say "approved (recorded)"; nothing claims enforcement.
- **Hash-bound but not signed.** Anyone who can write the data directory can add an approval event.
  The audit log (written by the CLI) is the second record; `aios doctor` does not yet cross-check
  the two [A: acceptable for Phase 3; a cross-check is a candidate for Phase 4].
- **Heuristic tiers** can under-classify (a risky change in an ordinary-looking file). The tier is
  advice for the approver, who reads the whole plan.
- **Plan text is untrusted data** when Claude wrote it (injection): it is quoted/labelled when shown
  and never executed.
