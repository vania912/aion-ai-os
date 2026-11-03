# ADR-0005: Risk-tiered approvals (in-chat and out-of-band)

- **Status:** Accepted by user in brainstorm (architecture still pending approval)
- **Date:** 2026-11-03

## Decision
Reads are allowed by policy. Memory proposals and plans use Claude Code permission prompts. Medium-risk project changes use a hash-bound in-chat plan approval. High-risk operations (commit, push, delete, deploy, anything outside a registered root) require approval out-of-band in the user's terminal. Claude can raise but not lower a risk tier.

## Reason
User chose 'both, by risk level'. Balances friction against safety.

## Alternatives considered
- All in-chat.
- All out-of-band.

## Consequences / trade-offs
Out-of-band approval can be attempted by Claude through a shell tool. Requires layered mitigations (TTY check, typed confirmation, Claude Code deny rule or hook). Residual risk to be accepted explicitly.
