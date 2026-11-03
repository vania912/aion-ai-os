# aion-ai-os

A personal AI operating layer for Claude Code: an MCP server that gives Claude
persistent, structured, searchable context about software projects (files, Git
history, decisions, tasks, memory) while keeping reading, planning, approval,
execution and verification separate.

**Status:** Phase 1 is implemented: a read-only MCP server (`aios serve`) over projects, git
history and memory, backed by a policy engine and a hash-chained audit log, plus a debugging CLI.
Phase 2a adds human-authored memory writes from the CLI (decisions with supersession, tasks,
notes); Phase 2b adds the review workflow (`aios review ...`); Phase 2c adds four MCP tools that let
Claude *propose* memory records and change task status, only when the server is started with
`--allow-propose` (off by default). Proposals are unreviewed until you accept them. Phase 2d adds a hand-maintained project state file shown in the snapshot, and `aios doctor` checks for hand-edited accepted decisions and stale proposals. Phase 3 adds change plans with approval bound to a content hash (`aios plan ...`, [`docs/PLANS.md`](docs/PLANS.md)); approvals are recorded, not enforced, because aios still cannot modify a project. It awaits your
review and a check on your Windows machine.
Nothing can modify a project yet (Phases 3-4 are not started).

- Architecture proposal: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Decision records: [`docs/adr/`](docs/adr/)
- Phase 1 spike results: [`docs/spikes/PHASE1-SPIKES.md`](docs/spikes/PHASE1-SPIKES.md)
- Policy engine: [`docs/POLICY.md`](docs/POLICY.md)
- Files service: [`docs/FILES.md`](docs/FILES.md)
- Git service: [`docs/GIT.md`](docs/GIT.md)
- Memory (read path): [`docs/MEMORY.md`](docs/MEMORY.md)
- Audit log: [`docs/AUDIT.md`](docs/AUDIT.md)
- MCP server and setup: [`docs/MCP.md`](docs/MCP.md)
- Development guide: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

This repository is public and contains code and documentation only. Project
registry and memory data are kept in a separate private location (see ADR-0004).
