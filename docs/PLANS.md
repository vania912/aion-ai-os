# Plans (Phase 3)

A plan is a written, reviewable proposal for a change, approved by a person **before** the change is
made. Decisions and assumptions are in [PHASE3-PLAN.md](PHASE3-PLAN.md) and
[ADR-0010](adr/0010-phase3-planning.md).

**Read this first.** In Phase 3 an approval is a *record*, not a barrier. aios cannot modify a project
(Phase 4 is not built), so nothing stops Claude Code, or you, from changing files without an approved
plan. What aios gives you: a fixed, hashed description of what was agreed; a record of who approved it
and when; and a check (`aios plan check`) that tells you whether an approval is still valid.

## What a plan contains

Request · what will change · why · files affected (create/modify/delete) · risks (at least one) ·
expected result · verification steps (at least one, numbered). Optional: requested tier, related
decisions/tasks, evidence, `revises`. Limits: 50 files, 20 risks/steps, bounded text.

Example file for `aios plan create <project> --file plan.yaml`:

```yaml
title: Cache workspace lookups
request: Make lookups faster
what_will_change: Add a small in-memory cache in src/util.py
why: Lookups dominate request time
files:
  - {path: src/util.py, action: modify, note: add cache}
risks: [stale entries]
expected_result: p95 lookup under 5 ms
verification: [run the unit tests, time 1000 lookups]
```

## Storage and the hash

`<data>/plans/PLAN-0001.yaml` holds the content and is never rewritten. `PLAN-0001.events.jsonl` is
append-only: approval, rejection, outcome, verification reports, verified. Every event names the plan
**hash** (SHA-256 of the canonical content; id, origin and date excluded). An event for different
content does not apply. So editing a plan after approval cancels the approval (it reads `proposed` with
"APPROVAL STALE"); restoring the exact content restores it. Status is always derived, never stored.

## States

```
proposed --approve--> approved --outcome--> executed ----verify----> verified
   |                     |  \--> partially_completed --verify--> verified
   |                     |  \--> failed
   |                     +--start--> started --abandon--> approved   (4b)
   +--reject--> rejected <+ (also from approved, before any outcome)
```
`started` leaves only by `abandon` until the seal exists (step 4c).

- Claude-written plans (`origin: claude`) always start `proposed`.
- `verified` needs: an `executed` or `partially_completed` plan **and** a report in which every
  verification step passed **and** a person running `aios plan verify`.
- A plan revised by a newer plan (`revises`) can no longer be approved; if the revision is rejected,
  the original is usable again.

## Executing a plan in an isolated worktree (Phase 4b)

`aios plan start PLAN-0001` (you, in a terminal) creates a git **branch `aios/PLAN-0001` and a
separate worktree** from the project's current commit, and records a `started` event with the base
commit. Claude Code is then started with the worktree folder as its working directory. Your own
branch and working tree are not touched; the project's `.git` gets the branch and worktree data.

- **Where:** `AIOS_WORKTREES_DIR/<project>/PLAN-nnnn` if you set that variable (absolute path),
  otherwise a sibling of the project: `<project folder>.aios-worktrees/PLAN-nnnn`. Never inside the
  project and never inside your data repo.
- **Before it runs** you see the plan, the base commit and branch, the folder, and warnings, for
  example uncommitted changes in your main working tree (the worktree starts from the last commit and
  will not contain them; planned files with such changes are listed).
- **Idempotent and crash-safe.** The worktree is created first and the event second, so `started`
  always means a worktree existed. If a crash happens in between, running `start` again adopts the
  leftover worktree if it has the expected branch and is still on the current commit. Anything else
  in the way (a folder git does not know, a branch without a worktree, a different worktree at the
  path) is reported and left untouched: aios never deletes a directory that is not a registered aios
  worktree.
- **`aios plan abandon PLAN-0001`** removes the worktree and deletes the branch (`--keep-branch` keeps
  it, with any commits on it) after listing the changed and git-ignored files that will be lost; the
  plan goes back to `approved`. It also cleans the leftovers of an interrupted start. A kept branch
  blocks a new start until it is deleted.
- While a plan is `started` it cannot be rejected or given a manual outcome (abandon first); the
  outcome will come from the seal (step 4c, not built yet). Editing a plan's content during execution
  cancels its approval, as before.
- `aios doctor` adds `plan_worktree_missing` (error), `plan_started_stale` (14 days),
  `orphan_worktree`, `orphan_branch` and `plan_git_unavailable`.
- No MCP tool can start or abandon a plan.

## Who can do what

| Action | Person (CLI) | Claude (MCP) |
|---|---|---|
| read plans (`list_plans`, `get_plan`) | yes | yes |
| create a plan | `aios plan create` | `create_plan`, only with `--allow-propose`; always unapproved |
| approve / reject | `aios plan approve/reject` | **no tool exists** |
| record outcome (executed/partial/failed) | `aios plan outcome` | **no** |
| file a verification report | `aios plan report` | `record_verification` (a claim) |
| mark verified | `aios plan verify` | **no** |

Approving shows the whole plan. R1/R2 ask for confirmation. **R3** requires typing the first 8
characters of the hash (`--yes` alone is not enough; scripts add `--confirm <8 chars>`).

## Risk tier

Computed from the files; a requested tier can raise it, never lower it. R1: no file changes. R2:
creates or modifies files. R3: deletes a file, touches CI/deploy/dependency/secret-like paths
(`.github/`, `.env*`, `*.pem`, `Dockerfile`, `package.json`, `pyproject.toml`, `*.sh`, ...), or lists
more than 20 files. This is a heuristic: it flags common risky places, it does not prove safety.

## Showing plans safely

Everything a plan's author wrote is shown with a `| ` prefix. Lines without it come from aios. Plan
text claiming "approved by the CTO" therefore cannot pass for the real approval line. Claude-written
plans carry a note that they are proposals, not instructions. The audit log records ids, tiers and
counts, never plan text.

## Commands

`aios plan list [--project P] [--status S] [--open]` · `show` · `create <project> --file F` ·
`approve [--yes] [--confirm HASH8] [--as NAME]` · `reject --reason R` · `outcome <executed|
partially_completed|failed>` · `report --pass N --fail N --skip N [--evidence T]` · `verify` ·
`check` (exit 0 only if approved and valid). `aios doctor` adds: `invalid_plan`, `orphan_plan_events`,
`plan_events_inconsistent`, `plan_approval_stale`, `stale_plan` (waiting over 14 days),
`plan_approved_not_done` (over 30 days), `plan_revises_missing`, `plan_unknown_project`.

MCP: `list_plans`, `get_plan`, `create_plan`, `record_verification`, prompt `prepare_change_plan
<project>`, and a "Plans" section in the project snapshot.

## Limits you should know

- **Not enforced** (see above).
- **Not signed.** Anyone who can write the data directory can add an event. The audit log (written by
  the CLI) is a second record; doctor does not cross-check the two yet.
- **Tampering is detected only partly.** A content edit cancels approval, and malformed or
  out-of-order events are reported, but deleting the last event line is not detectable.
- The CLI-only approval has the same bypass caveat as `aios review accept` (a Claude Code session can
  run shell commands); see MCP.md, "Keeping the review step human-only".
- Plans are not in the search index; `list_plans` scans the plan files.
