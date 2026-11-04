# ADR-0011: Phase 4 execution model

- **Status:** D1-D5 decided by the user (2026-11-04); D6-D8 assumed. Supersedes ADR-0006.
- **Date:** 2026-11-04
- **Details and evidence:** [PHASE4-PLAN.md](../PHASE4-PLAN.md)

| # | Topic | Choice | Alternatives offered | Trade-off |
|---|---|---|---|---|
| D1 | Who executes | **C: Claude Code edits in an isolated git worktree; aios records the base commit and checks the sealed diff against the approved file list** | A: client hooks; B: aios applies an approved diff | Violations are detected and contained, not prevented inside the worktree; needs git |
| D2 | Who starts execution | **Human CLI only** (`aios plan start`) | Also an MCP `start_plan` behind `--allow-execute` | No MCP tool touches a project repository in Phase 4 |
| D3 | Landing the change | **aios checks (`land-check`), the user merges with git** | `aios plan land` fast-forwards | One more manual step; one fewer write path |
| D4 | Verification commands | **aios runs none**; Claude reports, the user verifies | Allowlisted commands run in the worktree | aios never executes project-controlled code |
| D5 | Non-git projects | **Unsupported in Phase 4** | Snapshot-copy fallback | Scope check relies on git |
| D6 (assumed) | Worktree location | `AIOS_WORKTREES_DIR`, default a sibling `<project>.aios-worktrees/` | Inside the data dir | Outside both the project tree and the private data repo |
| D7 (assumed) | Base commit | Recorded in the `started` event | In the plan content (hash migration) | Plan text does not name its base commit |
| D8 (assumed) | Scope violation | Plan becomes `failed` with the reason | Separate `blocked` state | No new state |

## Consequences
- Plan approval becomes checkable against what actually changed (path scope), but remains
  non-preventive for edits made outside the worktree. Documentation must not call it "enforced".
- A hardened git write runner is required: a plain `git worktree add` ran a repo-local hook and
  smudge filter in the 2026-11-04 experiment.

## Revisit triggers
- Evidence that Claude Code edits outside the worktree in practice (add option A as a prevention layer).
- A need for byte-exact application of small patches (option B).
- Windows CI failures in worktree handling.
