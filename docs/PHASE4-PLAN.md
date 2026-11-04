# Phase 4 plan — controlled execution (decision needed: ADR-0006)

Status: **D1-D5 decided by the user (2026-11-04); D6-D8 assumed (no objection raised). Implementation
not started; waiting for the user's go for step 4a.** Decisions are recorded in
[ADR-0011](adr/0011-phase4-execution-model.md). Date: 2026-11-04.
Legend: **[V]** verified by an experiment in this session · **[J]** my judgement · **[U]** unverified.

## 1. The problem Phase 4 solves

After Phase 3, a plan can be approved, but approval is only a record: aios cannot modify a project, so
nothing makes the change that actually happens match the plan that was approved. Phase 4 is about
closing that gap: **execute -> verify -> audit -> memory update**, with the spec's rule that only the
server sets `executed` / `verified`, and `verified` only after a recorded verification.

The open question since the start (ADR-0006): **who performs the edit, Claude Code or aios?**

## 2. Options for D1 (the central choice)

| | A. Claude Code edits, aios guards the client | B. aios applies the approved change | C. Claude Code edits in an isolated git branch, aios checks scope and a person lands it |
|---|---|---|---|
| How | Claude Code hooks (PreToolUse on Edit/Write/Bash) ask `aios` whether the file is in an approved, hash-valid plan | The plan carries the exact change (a diff). `execute_plan` applies it after checking approval, hash, base commit and path rules | `aios plan start` makes a branch + separate worktree from a recorded base commit; Claude Code works there; `aios plan seal` diffs the result against the plan's file list and records the diff hash; a person merges |
| What is guaranteed | Only what the client config enforces. Hooks match tool calls, not what a program does; `sh -c` and other paths can bypass (see MCP.md deny-rule findings) | The applied change is byte-identical to the approved diff, for whatever aios applies | Work cannot land on your branch without you; the *sealed* diff is checked against the plan scope; out-of-scope or unplanned files are caught |
| Weak point | Enforcement lives in settings that live next to the model; bypassable | Plans must contain the full diff before approval, so exploratory work does not fit; biggest new write surface (Windows line endings, atomicity, rollback, symlinks) | Prevention is not enforced inside the worktree (Claude Code can still write anywhere its own permissions allow); violations are *detected*, and contained to a throwaway branch |
| Size [J] | small-medium | large | medium |
| Needs git | no | no (but base-commit check is only meaningful with git) | **yes** |

**Recommendation [J]: C**, optionally with A added later as a prevention layer. Reasons: it adds no
general-purpose "write file" tool to aios; the property that matters most (nothing lands on your
branch unreviewed, and what landed matches the approved scope) is checkable; it works for exploratory
work, which B does not; and it is the smallest design that can honestly set `executed` from evidence
(a diff hash) instead of a claim. Be clear about what C does *not* give: it does not stop a determined
or injected Claude Code session from editing files outside the worktree if its own permissions allow
that. Combine it with Claude Code permission rules and start sessions inside the worktree; I have not
tested how Claude Code treats edits outside its start directory [U].

## 3. What I verified that shapes the design

Experiment (git 2.43.0, Linux, a scratch repo with a repo-local `post-checkout` hook and a repo-local
smudge filter configured):

- **[V] A plain `git worktree add` ran both the project's hook and its smudge filter.** Creating a
  worktree in a project therefore *executes project-controlled code*. With
  `-c core.hooksPath=/dev/null` and the filter overridden to `cat`, nothing ran. The Phase 1 git runner
  already scrubs read-only calls; Phase 4 needs a second, small write runner with the same hardening
  applied to every call (hooks off, filters neutralised, no system/global config, no fsmonitor).
  Windows equivalents (`NUL`, path forms) are **[U]**.
- **[V]** A fresh worktree contains no git-ignored files, so any ignored file present at seal time is
  new. `git status --ignored` shows it (`!!`). That closes the case where an agent writes a file the
  diff would not show (for example a git-ignored `.env`).
- **[V]** After `git add -N -A`, `git diff --name-status --no-renames HEAD` lists modified, created and
  deleted files, and symlinks show mode `120000`, so symlinks, mode changes and deletes can be
  classified from the diff.
- **[U]** Binary files, submodules, CRLF/autocrlf noise, long Windows paths and `git worktree` on
  Windows are untested; they are listed as test cases for 4a-4c.

## 4. Decisions needed

| # | Decision | Options | Recommendation [J] and trade-off |
|---|---|---|---|
| D1 | Who executes | A hooks · B aios applies diff · C worktree + seal | **C** (section 2). Revisit B only if you need a byte-exact guarantee for small patches. |
| D2 | Who starts execution | **a.** human CLI only (`aios plan start`). **b.** also an MCP `start_plan`, behind a new `--allow-execute` flag | **a** first. Starting writes to the project's `.git` (branch + worktree metadata). b is convenient but is the first MCP tool that touches a project repo; add it only after C has run in practice. |
| D3 | Landing the change on your branch | **a.** aios only checks (`aios plan land-check`) and prints the git commands; you run them. **b.** `aios plan land` does a fast-forward merge itself | **a.** A merge is the R3 step in the spec; the fewer write paths in aios the better. Cost: you type two git commands. |
| D4 | Running verification | **a.** none: Claude reports, you verify (as in Phase 3). **b.** plans may list commands aios runs in the worktree (allowlist, timeout) | **a.** b means aios executes project-controlled code (tests, build scripts), which needs real sandboxing I cannot promise. |
| D5 | Projects without git | **a.** unsupported in Phase 4. **b.** snapshot-copy fallback | **a.** The scope check relies on git. |
| D6 | Where worktrees live | **a.** `AIOS_WORKTREES_DIR`, default a sibling `<project>.aios-worktrees/` **b.** inside the data dir | **a.** Must be outside the project tree (so it is not part of the repo) and outside your private data repo. |
| D7 | Recording the base commit | **a.** in the `started` event (plan format unchanged). **b.** in the plan content (changes the hash format; needs migration) | **a.** Cost: the plan itself does not say which commit it was written against; the start step records and shows it. |
| D8 | Scope violation at seal | **a.** the plan becomes `failed` with the reason; fix by a revised plan or a new run. **b.** a separate `blocked` state | **a.** No new state; the audit trail shows why. |

## 5. Design under the recommended answers (C, 2a, 3a, 4a, 5a, 6a, 7a, 8a)

New events (same append-only file, same `check_event` rules): `started` (branch, worktree path, base
commit), `sealed` (changed files, diff hash, scope result, ignored-file findings), `abandoned`.
Status additions: `started` between `approved` and the outcome. `executed` / `partially_completed` /
`failed` are then **set from the seal**, not typed by a person (the manual `aios plan outcome` stays
for Phase 3-style plans and is labelled self-reported).

- `aios plan start PLAN-n` (human): needs an approved, hash-valid plan; a git project; a base commit;
  creates branch `aios/PLAN-n` and a worktree via the hardened write runner; records `started`.
  Idempotent; refuses if the branch or directory already exists.
- Claude Code is started **inside the worktree**. aios stays read-only for project files.
- `aios plan seal PLAN-n` (human or a read-only check callable by Claude): `git add -N`, then compare
  the changed set with the plan's `files`: every change must be listed with the same action; extra
  paths -> out of scope; planned but unchanged -> not done. Also flags: symlinks, mode changes, binary
  files, submodule changes, any git-ignored file, any path R3 would flag. Records the diff hash.
  Outcome: all in scope and all done -> `executed`; some done -> `partially_completed`; anything out
  of scope -> `failed` (D8).
- `aios plan land-check PLAN-n`: confirms the approval is still valid, the sealed diff hash still
  matches the branch, and the target branch can fast-forward; prints the commands to run. `verified`
  still needs a passing report and `aios plan verify`, as in Phase 3.
- `aios doctor`: worktree without a plan, branch without a worktree, sealed diff changed afterwards,
  started but not sealed for N days.
- Memory update: after `verified`, the model may propose a note/decision with the existing tools; the
  plan id goes in `evidence`. No new code.

## 6. Sub-steps (each ends with tests, mutation checks and CI)

| Step | Content | Exit criteria |
|---|---|---|
| 4a | Hardened git **write** runner (worktree add/remove, branch, add -N, diff/status for seal); hostile-repo tests (hook, filters, config includes, `core.fsmonitor`, hooksPath, templates) | none of the hostile repo's code runs; Windows CI green |
| 4b | `started` / `abandoned` events, `aios plan start/abandon`, crash recovery, doctor checks | start twice is safe; crash between branch and event is recovered |
| 4c | `seal`: scope comparison and findings, diff hash, status from seal | each finding class has a test; an unplanned file or ignored file is caught; binary/symlink/mode/delete handled |
| 4d | `land-check`, snapshot/`get_plan` show execution state, prompt update, docs, a real headless Claude Code run inside a worktree | run shows: approve -> start -> Claude edits in worktree -> seal -> land-check -> verify; one injected out-of-scope edit is caught |

## 7. Risks

- **Running project-controlled code.** Worktree creation executes hooks/filters unless neutralised
  [V]. Mitigation: hardened runner, tested against a hostile repo. Residual: other git features I did
  not test [U].
- **Containment is not prevention.** C detects and isolates; it does not stop edits outside the
  worktree. Say so everywhere; do not call it "enforced".
- **Heuristics.** Scope is by *path*, not by what changed inside a file: a planned file can still
  contain an unplanned change. The approver reads the diff; aios does not judge it.
- **Windows.** Worktrees, long paths, CRLF and `NUL` behave differently [U]; CI on Windows is the
  check, and I will not claim support before it passes.
- **Disk and clutter.** Each plan leaves a worktree until `abandon` or a landing; doctor reports old
  ones.
- **Your working tree.** The worktree starts from a commit, not from your uncommitted edits; a plan
  written against uncommitted code can be stale. `start` warns if the files in the plan are dirty in
  the main working tree.

## 8. What I would not do in Phase 4

No push, no commit to your branch, no deploy, no network access, no running of project code by aios,
no delete of your files outside the plan's worktree. Those stay manual (R3) and are outside this phase.
