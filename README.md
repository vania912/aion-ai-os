# aion-ai-os

A personal AI operating layer for Claude Code: an MCP server that gives Claude
persistent, structured, searchable context about software projects (files, Git
history, decisions, tasks, memory) while keeping reading, planning, approval,
execution and verification separate.

**Status:** Phase 1 in progress. Architecture approved; project skeleton, registry loader and
minimal CLI (`aios project list`, `aios doctor`) exist. The MCP server is not built yet.

- Architecture proposal: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Decision records: [`docs/adr/`](docs/adr/)
- Phase 1 spike results: [`docs/spikes/PHASE1-SPIKES.md`](docs/spikes/PHASE1-SPIKES.md)
- Policy engine: [`docs/POLICY.md`](docs/POLICY.md)
- Development guide: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

This repository is public and contains code and documentation only. Project
registry and memory data are kept in a separate private location (see ADR-0004).
