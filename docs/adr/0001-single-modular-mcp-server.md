# ADR-0001: One modular MCP server instead of a gateway plus multiple servers

- **Status:** Proposed (pending Phase 0 approval)
- **Date:** 2026-11-03

## Decision
Build one MCP server process. Project, git, memory, tasks, plans, audit and policy are internal packages behind interfaces. The CLI shares the same core library.

## Reason
Single user, single machine. Multiple servers add registrations, process management and cross-server coordination without a demonstrated benefit.

## Alternatives considered
- Gateway fronting separate Project/Git/Knowledge/Task/Decision servers.
- One server per domain, no gateway.

## Consequences / trade-offs
Gives up process-level isolation between modules. Mitigated by a policy engine in the core in front of every call. Modules can be split out later because the boundaries already exist.
