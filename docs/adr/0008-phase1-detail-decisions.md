# ADR-0008: Phase 1 detail decisions

- **Status:** Accepted (user choices, 2026-11-03)
- **Date:** 2026-11-03

## Decisions

| Topic | Choice | Alternatives offered | Trade-off |
|---|---|---|---|
| Approval hardening (R3) | **Layered**: CLI refuses without an interactive TTY; typed confirmation shown only in the terminal; Claude Code deny rule/hook on the approve command | No high-risk tools at all; separate OS account/machine; Claude Code prompts only | Residual bypass risk remains. The TTY assumption was observed only in a cloud harness, not on the user's local Windows Claude Code |
| Memory seeding | **Start empty, capture going forward** | Manual seed; Claude drafts from Notion/Slack; bulk import | Early retrieval has little to find; older reasoning is added on demand |
| Data repo | **Local git only, no remote** (see ADR-0004) | Private GitHub repo; company-org repo; plain folder | No off-machine backup |
| File access rules | **Deny-list + text-extension allow-list + size cap** | Deny-list only; plus output redaction | Unusual file types are blocked until added to the allow-list |
| Code search | **ripgrep if found, pure-Python fallback**; no persistent code index | Python only; persistent FTS code index | Fallback is slower on large repos; Windows ripgrep availability unconfirmed |
| Decision template | **title + free-text body** (front matter holds why[], alternatives[], status, links) | + `what` line; ADR-style required sections | Less enforced structure; relies on consistent `why` fields for retrieval |
| CI | **Linux and Windows runners** from the start | Linux only | Slower CI; catches path/shutdown/encoding problems early |
| Project size | **Unknown**; Phase 1 adds a size report to `aios project inspect` and decides from data | — | Search limits may need tuning after first real use |

## Revisit triggers
- Approval hardening: if Claude Code on the user's machine is found to run shell commands with a TTY (spike S9 on Windows).
- Data repo: if loss of the local machine would be unacceptable, add a private remote.
- Code search: if `aios project inspect` shows large repos or the fallback is too slow.
