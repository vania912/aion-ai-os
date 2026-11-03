# ADR-0002: Local stdio transport targeting Claude Code

- **Status:** Accepted by user in brainstorm (architecture still pending approval)
- **Date:** 2026-11-03

## Decision
The server runs locally and is started by Claude Code over stdio. No HTTP layer or FastAPI in the MVP.

## Reason
User chose Claude Code, local. stdio has no network surface and needs no authentication layer.

## Alternatives considered
- Streamable HTTP (needed for remote or claude.ai use; adds auth and hosting).
- Claude Desktop only.

## Consequences / trade-offs
Not reachable from claude.ai cloud sessions. Server must log to stderr only and exit promptly when stdin closes.
