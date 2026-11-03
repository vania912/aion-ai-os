# aion-ai-os

A personal AI operating layer for Claude Code: an MCP server that gives Claude
persistent, structured, searchable context about software projects (files, Git
history, decisions, tasks, memory) while keeping reading, planning, approval,
execution and verification separate.

**Status:** Phase 1 in progress. Architecture approved; project skeleton, registry loader and
policy engine, files service (list/read/search), a read-only git service, the memory read path (records, FTS index, decision history) and a CLI (`aios project`, `aios files`, `aios git`, `aios memory`, `aios decision`, `aios index`, `aios doctor`) exist. The MCP server is not built yet.

- Architecture proposal: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Decision records: [`docs/adr/`](docs/adr/)
- Phase 1 spike results: [`docs/spikes/PHASE1-SPIKES.md`](docs/spikes/PHASE1-SPIKES.md)
- Policy engine: [`docs/POLICY.md`](docs/POLICY.md)
- Files service: [`docs/FILES.md`](docs/FILES.md)
- Git service: [`docs/GIT.md`](docs/GIT.md)
- Memory (read path): [`docs/MEMORY.md`](docs/MEMORY.md)
- Development guide: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

This repository is public and contains code and documentation only. Project
registry and memory data are kept in a separate private location (see ADR-0004).
