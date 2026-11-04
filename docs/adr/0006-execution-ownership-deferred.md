# ADR-0006: Who executes approved writes is deferred

- **Status:** Superseded by [ADR-0011](0011-phase4-execution-model.md) (decided 2026-11-04)
- **Date:** 2026-11-03

## Decision
Do not decide yet whether Claude Code or the MCP server performs approved file edits and git operations. No write tools exist in Phases 1-3.

## Reason
User chose to decide after Phases 1-3. Planning, approval and audit records are needed under either option.

## Alternatives considered
- Claude Code executes; MCP records plan, approval, verification, audit.
- MCP executes writes itself.

## Consequences / trade-offs
The Phase 4 design remains open. Phases 1-3 must not assume either.
