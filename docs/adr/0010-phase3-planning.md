# ADR-0010: Phase 3 planning model

- **Status:** Assumed (user asked to proceed without a separate decision round, 2026-11-03).
- **Date:** 2026-11-03

| # | Choice | Main alternative | Trade-off |
|---|---|---|---|
| P1 | Immutable plan content + append-only events; status derived | Status field edited in place | Approval invalid by construction after any edit; two files per plan |
| P2 | Approval by human CLI only | MCP `approve_plan` with an `ask` rule | Same bypass caveat as ADR-0009 D2 |
| P3 | Outcome and `verified` set only by the human; Claude files unverified reports | Claude sets status | More human effort; no status by assertion |
| P4 | Tier computed from files; can be raised, not lowered | Declared by Claude | Heuristic can under-classify |
| P5 | Revision = new plan with `revises` | Edit in place | Follows from P1 |
| P6 | Quota 10 awaiting-approval plans per project (estimate) | None | Bounds spam |
| P7 | Not in the SQLite index | Index plans | Unchanged schema |

Consequences: approved plans are records, not barriers, until Phase 4 (ADR-0006).
Revisit triggers: Phase 4 design; evidence that approval events are forged or bypassed; plan volume.
