# aion-ai-os — Phase 0 Architecture

Status: **DRAFT for approval** · Date: 2026-11-03 · No implementation code exists yet.

This document covers sections A–J of the Phase 0 brief. Decisions that were
settled in the brainstorm are recorded as ADRs in [`docs/adr/`](adr/).

Legend: **[V]** verified against a source (listed in §K) · **[U]** unverified or
remembered, to be confirmed in a Phase 1 spike · **[A]** assumption.

---

## 0. Confirmed inputs (from the brainstorm)

| Topic | Decision |
|---|---|
| Client | Claude Code, local, MCP over **stdio** (ADR-0002) |
| Memory source of truth | **Markdown/YAML files**, plus a rebuildable **SQLite** index (ADR-0003) |
| Data location | Registry and memory live **outside this public repo** (ADR-0004) |
| Tasks | Own minimal task store inside the memory store (ADR-0003) |
| Users | Single user for now; keep `made_by` so a team can be added later |
| Approvals | **Both, by risk level**: in-chat for low/medium, out-of-band CLI for high (ADR-0005) |
| Who executes writes (Phase 4) | **Deferred** until Phases 1–3 are working (ADR-0006) |
| Deliverable | This document, committed to `main` for review |

---

## A. System architecture

### A.1 Challenge to the brief: is a gateway needed?

The brief sketches an MCP Gateway in front of Project / Git / Knowledge / Task /
Decision MCP servers. For one user on one machine, separate server processes add
process management, several `claude mcp add` registrations, and cross-server
coordination (for example, a plan that touches files, git and memory) without a
clear benefit.

**Recommendation:** one MCP server process, internally modular. The boundaries
you want (project, git, memory, tasks, decisions, audit, policy) are
**packages inside one codebase**, each behind an interface. The CLI uses the same
core library. If a module ever needs its own process (different trust level,
different host), it can be split out, because the boundary already exists.

| | Choice | Alternative | Trade-off |
|---|---|---|---|
| Server topology | One server, modular packages | Gateway + N servers | Less operational overhead vs. no process-level isolation between modules |

Process-level isolation is the real thing given up. The mitigation is that the
policy engine (§E) sits in the core, in front of every module, not inside
individual tools.

### A.2 Diagram

```
                     ┌──────────────────────────────┐
                     │         Claude Code          │
                     │  reasoning · planning        │
                     └───────────────┬──────────────┘
                                     │ MCP over stdio
                     ┌───────────────▼──────────────┐
                     │      aios MCP server         │   (interfaces/mcp)
                     │  tools · resources · prompts │
                     └───────────────┬──────────────┘
                                     │ calls
 ┌───────────────────────────────────▼──────────────────────────────────┐
 │                          aios core library                           │
 │                                                                      │
 │   Policy engine ──► every call is classified, checked, audited       │
 │                                                                      │
 │   registry   files   git   memory   tasks   search   context   plans │
 │                                                                      │
 └────────┬─────────────┬──────────────┬──────────────┬─────────────────┘
          │             │              │              │
   ┌──────▼─────┐ ┌─────▼──────┐ ┌─────▼──────┐ ┌─────▼──────┐
   │ Project    │ │ git CLI    │ │ Memory     │ │ Audit log  │
   │ repos      │ │ (read-only │ │ files      │ │ (append-   │
   │ (read-only │ │ allowlist) │ │ (.md/.yaml)│ │ only JSONL)│
   │ in Ph.1-3) │ │            │ │ + SQLite   │ │            │
   └────────────┘ └────────────┘ │ index      │ └────────────┘
                                 └────────────┘

   ┌──────────────┐        same core library, no MCP dependency
   │  aios CLI    │───────────────────────────────────────────────►
   └──────────────┘
   (debug without Claude · out-of-band approvals for high-risk ops)
```

### A.3 Layering rule

```
interfaces (mcp, cli)  →  services (use cases)  →  domain (models, ports)
                                   ↓
                           adapters (fs, git, sqlite)
```

- `domain` and `services` import **nothing** from the MCP SDK.
- MCP tool functions are thin: parse args, call a service, shape the result.
- This is what lets the CLI debug the system independently of Claude.

---

## B. Component breakdown

| Component | Responsibility | Phase |
|---|---|---|
| **registry** | Loads `registry.yaml` (projects, paths, options). Validates paths. The only place that knows where projects live. No project-specific logic anywhere else. | 1 |
| **policy** | Classifies every operation (risk tier + permission level), enforces path confinement and deny-lists, decides allow / needs-approval / deny. | 1 |
| **files** | List, read and keyword-search files inside a registered project root. Size limits, binary detection, deny-list (`.env`, keys, etc.). | 1 |
| **git** | Read-only git: log, search history, show commit, diff, status, branch. Runs `git` with an allowlisted argument list, never a shell string. | 1 |
| **memory** | Decisions, assumptions, constraints, open questions, discoveries, project state. File-backed, indexed in SQLite. | 1 (read) / 2 (write) |
| **tasks** | Minimal task records linked to decisions and files. | 2 |
| **search** | Layered retrieval router (§D.5). | 1 → 2 |
| **context** | Builds the project snapshot and the "high-signal bundle" for a question. | 2 |
| **plans** | Plan, approval, execution and verification records. State machine in §E.4. | 3 |
| **audit** | Append-only JSONL log of every call and state change. | 1 |
| **interfaces/mcp** | Registers tools, resources and prompts. | 1 |
| **interfaces/cli** | `aios ...` commands. Same services. | 1 |

---

## C. MCP design

### C.1 Rule of thumb used

MCP separates three things by **who controls them**:

- **Tools** — model-controlled: Claude decides when to call them; they perform
  a computation or an action, typically with parameters.
- **Resources** — application-controlled: readable context identified by URI;
  the client or user chooses to attach them (Claude Code: `@server:uri`) [V-CC2].
- **Prompts** — user-controlled: reusable workflows the user triggers (Claude
  Code lists them as `/mcp__server__prompt`) [V-CC1].

(This framing is the protocol's design intent as I understand it [U]; the
Claude Code behaviours above are verified.)

Test applied to each capability: *does it take parameters and run a query or
action?* → tool. *Is it a named, stable, readable document?* → resource.
*Is it a reusable instruction template the user invokes?* → prompt.

### C.2 Tools

All names are prefixed `mcp__aios__` in Claude Code [V-CC4]. Level = permission
level from §E.

| Tool | Level | Why a tool |
|---|---|---|
| `list_projects` | READ_ONLY | Parameterless query, but cheap and needed for discovery |
| `search_project(project, query, glob?, max_results?)` | READ_ONLY | Parameterised search |
| `list_project_files(project, path?, depth?)` | READ_ONLY | Parameterised listing |
| `read_project_file(project, path, start?, end?)` | READ_ONLY | Parameterised read with range |
| `search_git_history(project, query, mode, since?)` | READ_ONLY | Parameterised (message / pickaxe / path) |
| `get_recent_changes(project, since?, limit?)` | READ_ONLY | Parameterised |
| `get_commit(project, ref)` · `get_diff(project, base, head, path?)` · `get_branch_status(project)` | READ_ONLY | Parameterised |
| `search_memory(query, project?, kinds?, status?)` | READ_ONLY | Layered search (§D.5) |
| `get_decision(id)` · `get_decision_history(id)` | READ_ONLY | Walk supersession chain |
| `build_context(project, question)` | READ_ONLY | Runs retrieval across layers, returns a bounded bundle |
| `propose_decision(...)` · `propose_task(...)` · `propose_memory_item(...)` | PROPOSE | Writes a *proposed* record to the memory store only; never touches project repos |
| `update_task_status(id, status)` | PROPOSE | Additive state change in memory store |
| `create_plan(project, request, ...)` | PROPOSE | Writes a plan record (status `draft`) |
| `approve_plan(id, plan_hash)` | APPROVAL_REQUIRED | Records approval bound to the plan's content hash (§E.4) |
| `record_verification(plan_id, ...)` | PROPOSE | Phase 3–4 |
| `execute_plan(id)` | WRITE | **Phase 4 only; shape depends on ADR-0006** |

Deliberately **not** tools in Phases 1–3: `modify_file`, `delete_file`,
`git_commit`, `git_push`, `run_deployment`. They do not exist in the server
until Phase 4, so they cannot be called, misconfigured, or prompt-injected into
running.

### C.3 Resources

Principle: a small number of **stable, bounded, human-readable** documents.
Anything that depends on a search query is a tool, not a resource.

| URI | Content | Why a resource |
|---|---|---|
| `project://{name}` | Metadata: description, path alias, branch, last activity | Stable, small |
| `project://{name}/context` | The generated snapshot (§D.6) | Stable document Claude can be given up front |
| `project://{name}/decisions` | Index of active decisions (id, title, status, date) | Browsable index |
| `project://{name}/tasks` | Open tasks | Browsable index |
| `project://{name}/recent-changes` | Last N commits, summarised | Bounded |

Granularity: **5 resources per registered project**, not one per file or per
decision. Individual decisions are read with `get_decision` (tool). Claude Code
`@` autocomplete lists resources across servers [V-CC2], so a long list would
degrade it.

Known uncertainty: Claude Code's documentation, as read, does not mention
**resource templates** [U-CC]. Plan: register **concrete resources** per project
at startup (generated from the registry) and treat templates as optional. If
the registry changes, the server must either restart or emit `list_changed`;
interactive Claude Code refreshes on that notification [V-CC2].

### C.4 Prompts

| Prompt | Why a prompt |
|---|---|
| `analyze_project(project)` | User-initiated workflow: build context → analyse → report uncertainty |
| `summarize_project_state(project)` | Same |
| `review_recent_changes(project, since)` | Same |
| `investigate_bug(project, symptom)` | Workflow that enforces the read-only investigation order |
| `prepare_change_plan(project, request)` | Workflow that ends in `create_plan`, never in an edit |
| `prepare_handoff(project)` | Summarises state, open questions, active plans |

Prompts carry the **agent workflow** (request → context → memory → changes →
analyse → plan → approval check). They are text templates plus argument
slots, so they cost nothing at runtime and are versioned with the code.
Claude Code splits prompt arguments on whitespace [V-CC1], so arguments
should be simple tokens; long free text goes to the conversation, not the
argument.

### C.5 Why not "everything as a tool"

Exposing resources as tools would force Claude to spend a call (and context)
to learn what a project is, and would hide the stable documents from the user's
`@` picker. Exposing workflows as tools would let the model decide to run them
instead of the user. The split keeps user-controlled, application-controlled and
model-controlled surfaces distinct.

### C.6 Tool annotations are hints, not security

The SDK supports `read_only_hint`, `destructive_hint`, `idempotent_hint`,
`open_world_hint` and its docs call them hints, not security [V-SDK4]. Claude
Code uses `readOnlyHint` to parallelise read-only calls [V-CC5]; I found **no
documentation that it uses `destructiveHint` for permissions** [U-CC]. So
safety must come from the policy engine and Claude Code permission rules, not
from annotations. Annotations are still set accurately.

---

## D. Memory design

### D.1 Options compared

| | A. Files (MD/YAML) | B. SQLite only | C. SQLite + embeddings | D. Vector DB |
|---|---|---|---|---|
| Complexity | Low | Low–medium | Medium–high | High (new service) |
| Retrieval quality | Good for exact/structured; weak for paraphrase unless indexed | Good with FTS5 + metadata | Best for paraphrase/"why are we doing this?" | Same as C at larger scale |
| Cost | None | None | Embedding calls or local model; re-embed on change | Infra + embedding |
| Maintenance | Edit by hand, diff in git | Schema migrations; opaque binary | Model/version drift, re-indexing | Operate a service |
| Local privacy | Full | Full | Full only with a local model; otherwise text leaves the machine | Depends |
| Scalability | Thousands of records fine [A] | Large | Large | Very large |
| Human review / history | **Excellent** (git diff, PR review) | Poor | Poor | Poor |

**Recommendation: A as source of truth + B as a derived index** (ADR-0003).
Files give reviewable history, which matters for decisions and the audit story.
SQLite (FTS5) gives fast keyword and metadata queries. The index is
**disposable**: `aios index rebuild` regenerates it from files, so there is no
migration burden and no second source of truth.

Embeddings (C) are deferred to Phase 5 and only if measured retrieval failures
justify them (ADR-0007). A vector DB (D) is not justified at the expected
scale (hundreds to low thousands of records) [A].

### D.2 Record types

All records are Markdown with YAML front matter. Body is free text (reasoning,
context). Front matter is validated by Pydantic models.

| Type | ID | Purpose |
|---|---|---|
| decision | `DEC-0001` | A choice, its reasons, alternatives, status |
| assumption | `ASM-0001` | Something believed true but unverified; has `validated_by` |
| constraint | `CON-0001` | A limit we must respect |
| question | `Q-0001` | Unresolved question; closed with a link to a decision |
| discovery | `DSC-0001` | Important finding (bug cause, behaviour) |
| task | `TSK-0001` | Work item |
| note | `NOTE-0001` | Free-form note |
| project-state | (one per project) | Hand/auto-maintained current state |
| plan | `PLAN-0001` | Change plan with approval/verification state (Phase 3) |

IDs are per-store sequential with zero-padding. Your example used `DEC-001`;
I use four digits to avoid a format change later. Allocation scans the
directory under a file lock. In a single-user setup this is safe. Two machines
writing concurrently could collide (see Risks).

### D.3 Decision record

```yaml
---
id: DEC-0001
type: decision
project: pulse
title: Separate MENA Slack from US Slack
status: active              # proposed | active | superseded | deprecated | rejected
date: 2026-10-XX
made_by: [Raheel]
supersedes: null            # id of the decision this replaces
superseded_by: null         # filled in when replaced
why:
  - Different workspace requirements
  - Avoid mixing monitoring data
alternatives_considered:
  - Single workspace handler with a region flag
assumptions: []             # ASM ids
constraints: []             # CON ids
related_files: []           # project-relative paths
related_tasks: []
related_decisions: []
tags: [slack, workspace]
---
Context, trade-offs, and anything else a future reader needs.
```

Your template had `decision:` twice (a title block and a what block); I
merged them to `title` plus body. Tell me if you want `what` kept separate.

### D.4 Decision lifecycle

```
DEC-0001 (active)
    │  new information
    ▼
create DEC-0007 with supersedes: DEC-0001
    │
    ├── DEC-0001: status → superseded, superseded_by → DEC-0007
    └── DEC-0007: status → active
```

Rules:
1. Decisions are **never deleted**. Superseded records stay searchable.
2. After a decision is `active`, its `why`, `alternatives` and body are
   **immutable**; only `status`, `superseded_by` and links change. Corrections
   are a new decision that supersedes it.
3. The server maintains both directions (`supersedes` / `superseded_by`) in one
   atomic operation, and `aios doctor` detects broken chains.
4. `get_decision_history(id)` returns the full chain with each record's reasons
   and date, which answers "What was the previous approach?" and "Why was it
   replaced?" (the replacement's `why` plus an optional `reason_for_change`
   field).

### D.5 Retrieval: layered, cheapest first

```
question
   │
   ▼
1. Metadata filter     project, type, status, tags, date, ids   (SQLite)
   │ enough?  yes → return
   ▼
2. Keyword search      FTS5 over memory; ripgrep/fallback over code
   │ enough?  yes → return
   ▼
3. Git search          log --grep, pickaxe (-S/-G), path history
   │ enough?  yes → return
   ▼
4. Semantic search     ── NOT BUILT; Phase 5, only if layers 1–3 measurably fail
```

The router is deterministic and logs which layers ran, so retrieval failures
are debuggable. Layer 4 is reachable only through an interface (`Retriever`
protocol) so adding it later does not change callers.

Spikes needed before relying on this [U]: SQLite FTS5 availability in the
Python build you will use on Windows; ripgrep availability on your machine
(otherwise a pure-Python fallback is needed, which is slower on large repos).

### D.6 Project snapshot

`project://{name}/context` is generated, not hand-written, from:

- registry metadata and current branch (git)
- last N commits (git)
- active decisions (last M, plus any `tag: constraint`)
- open tasks, open questions, known issues (memory)
- hand-maintained fields in `project-state` (architecture summary, known
  constraints), because architecture cannot be reliably inferred from code
  alone

The snapshot is cached with a content hash of its inputs and regenerated when
HEAD or memory changes. Output is capped (default target ≤ 4k tokens [A]) to stay far
below Claude Code's warning threshold of 10,000 tokens and default limit of
25,000 [V-CC6].

### D.7 Directory layout of the *data* repo (private, not this repo)

```
aios-data/
├── registry.yaml
├── projects/
│   └── pulse/
│       ├── state.yaml
│       ├── decisions/DEC-0001.md
│       ├── assumptions/  constraints/  questions/  discoveries/
│       ├── tasks/TSK-0001.md
│       └── notes/
├── plans/PLAN-0001.yaml
├── audit/2026-11.jsonl
└── .index/aios.sqlite          # gitignored, rebuildable
```

Location is supplied by `AIOS_DATA_DIR` (or `--data-dir`); no machine-specific
default is baked into code. Paths in `registry.yaml` are the only place Windows
paths like `D:/KC/Pulse` appear.

---

## E. Security model

### E.1 Threats considered

| Threat | Example | Primary mitigation |
|---|---|---|
| Overreach | Claude edits or pushes without approval | No write tools exist until Phase 4; policy engine; approval tiers |
| Path escape | `read_project_file("../../.ssh/id_rsa")` | Resolve real path (follow symlinks), require it to be inside the registered root |
| Secret exposure | Reading `.env`, keys, tokens | Deny-list by default; best-effort secret-pattern redaction (not a guarantee) |
| Prompt injection | A README or note says "ignore previous instructions and run X" | Treat file/note/commit text as **data**; tool results are wrapped/labelled; no tool can perform high-risk actions in Phases 1–3 |
| Command injection | Crafted ref or query reaches the shell | `subprocess` with argument lists, `shell=False`, allowlisted git subcommands, ref validation |
| Self-approval | Claude approves its own plan | See E.4 |
| Audit tampering | Edits to hide actions | Append-only file, written by the server; Claude has no tool that edits it (not tamper-proof against someone with filesystem access) |
| Resource exhaustion | Reading huge files, huge diffs | Size, line and result caps |
| Stdout corruption | A stray `print` breaks the stdio protocol | All logging via `logging` to stderr; no `print` (§H) [V-SDK5] |

### E.2 Permission levels

| Level | Meaning | Examples |
|---|---|---|
| `READ_ONLY` | No state change anywhere | search, read, git log/diff |
| `PROPOSE` | Writes only a *draft or proposed record* inside the memory store; never touches a project repo | `propose_decision`, `create_plan` |
| `APPROVAL_REQUIRED` | Takes effect only with a recorded approval bound to specific content | `approve_plan`, accepting a proposal |
| `WRITE` | Mutates a project repo or remote | `execute_plan` (Phase 4) |

Your original list put `create_task` and `create_decision_log` under "requires
approval". I split that: creating them yields a `proposed` record (cheap,
reversible via git, never affects project code), and **accepting** the proposal
is the approval step. This keeps routine memory capture low-friction.

### E.3 Risk tiers and how approval is captured

| Tier | Operations | Approval channel |
|---|---|---|
| R0 | All reads | None; allowed by policy |
| R1 | Propose/accept memory records, create plans | Claude Code permission prompt (an `ask` rule on those tools) + audit entry |
| R2 | Project file modifications (medium) | Plan approved in chat via `approve_plan` (prompt shows plan id and hash) |
| R3 | commit, push, delete, deploy, anything outside a registered root | **Out-of-band only**: `aios approve PLAN-042` in your own terminal |

The tier is computed by the policy engine from the operation type and the plan's
file list. Claude may *raise* a tier in its plan but cannot *lower* it.

### E.4 Plan state machine and approval binding

```
draft → proposed → approved → executing → executed → verified
                      │            │          │
                      ▼            ▼          ▼
                   rejected      failed   partially_completed
```

- `proposed` plans include: WHAT WILL CHANGE · WHY · FILES AFFECTED · RISKS ·
  EXPECTED RESULT · VERIFICATION PLAN.
- Approval is bound to a **hash of the plan content**. Any edit after approval
  invalidates it.
- Statuses are the ones you asked for: `planned`, `executed`, `verified`,
  `failed`, `partially completed`. Only the server sets `executed` or
  `verified`, and only `verified` after a recorded verification result, so a
  plan cannot be reported complete by assertion.

**Known weakness of out-of-band approval:** Claude Code can run shell commands,
so it could try to run `aios approve` itself. Mitigations (layered, none alone
sufficient): (1) the CLI refuses to approve when stdin is not an interactive
terminal [U: confirm Claude Code's Bash tool is non-TTY]; (2) the CLI requires
typing a confirmation value shown only in the terminal; (3) a deny rule in
Claude Code settings for that command; (4) a `PreToolUse` hook, which Claude
Code supports for MCP tools and others [V-CC4]. Residual risk should be
accepted explicitly or reduced by running approvals from a separate machine
account. I'd like your view on this.

### E.5 Claude Code configuration (defence in depth)

Server-side policy is authoritative. Claude Code settings add a second layer:
`allow` for the R0 tools, `ask` for R1/R2 tools, and deny/hook rules for R3.
Rule names use `mcp__aios__<tool>`; allow-rule globs must follow a literal
`mcp__aios__` prefix [V-CC4]. The exact settings keys will be taken from the
current docs when we write the sample config in Phase 1.

---

## F. Repository structure

Two repositories:

**`aion-ai-os` (public; code only)**

```
aion-ai-os/
├── README.md
├── pyproject.toml
├── docs/
│   ├── ARCHITECTURE.md
│   └── adr/0001-….md …
├── src/aios/
│   ├── domain/            # Pydantic models, enums, ports (Protocols)
│   ├── services/          # registry, files, git, memory, tasks, search,
│   │                      #   context, plans, audit use cases
│   ├── policy/            # risk tiers, path confinement, deny-lists
│   ├── adapters/          # fs, git_cli, sqlite_index, jsonl_audit
│   ├── interfaces/
│   │   ├── mcp/           # server.py, tools.py, resources.py, prompts.py
│   │   └── cli/           # typer app
│   └── config.py          # settings (env + files), no secrets in code
├── tests/
│   ├── unit/
│   ├── integration/       # real git repos built in tmp dirs
│   └── fixtures/
├── examples/              # sample registry.yaml, sample .mcp.json (no real paths)
└── .github/workflows/     # lint, type-check, test (Phase 1)
```

**`aios-data` (private; your memory and registry)** — layout in §D.7.

---

## G. MVP scope

The smallest thing that shows `Claude → MCP → Project → Git/Files/Memory`:

**Phase 1 (read-only):**
- Registry loader + validation (+ example config)
- Policy engine: path confinement, deny-list, caps
- Files: list, read, keyword search
- Git: recent changes, log search, commit, diff, status
- Memory: **read** and search of hand-written decision/note files; SQLite FTS index with `aios index rebuild`
- Audit log (JSONL) for every call
- MCP: ~10 read tools, 3–4 resources per project, 2 prompts (`analyze_project`, `review_recent_changes`)
- CLI: `project list|inspect`, `search`, `decision list|show`, `memory search`, `context build`, `audit list`, `index rebuild`, `doctor`
- Tests: unit tests, plus integration tests against temporary git repos

**Explicitly not in Phase 1:** any write by Claude, tasks, plans, embeddings,
HTTP transport, FastAPI.

Decisions are created in Phase 1 by **you** via CLI (`aios decision create`) or
by hand-editing files. This keeps Phase 1 read-only and gives us real data to
test retrieval against.

**Phase 1 exit criteria:** in Claude Code you can ask "What changed in <project>
recently?" and "Why did we decide X?" and get answers grounded in tool results,
and every call appears in `aios audit list`.

---

## H. Technology decisions

| Area | Choice | Why | Alternative / trade-off |
|---|---|---|---|
| Language | Python ≥ 3.11 | MCP SDK requires 3.11+ [V-SDK5]; your preference | — |
| MCP SDK | `mcp` v2.x, `from mcp.server import MCPServer` | PyPI latest is 2.3.0 (checked); docs call v2 the current stable line [V-SDK1, V-SDK6] | v1 `FastMCP` path is older; I'd pin `mcp>=2,<3` (my recommendation; the docs only give a `<2` bound for libraries) |
| Transport | stdio | Local, no network surface, no auth to build [V-SDK3] | HTTP only if remote use is needed |
| HTTP/FastAPI | **Not used** | No HTTP layer is needed for local stdio | Add only if a remote client appears |
| Models | Pydantic v2 | Validation of front matter, config, tool I/O [U on exact version features] | dataclasses (less validation) |
| Memory files | Markdown + YAML front matter | Reviewable history | See §D.1 |
| Index | SQLite + FTS5 (stdlib `sqlite3`) | Zero infra, rebuildable | Whoosh/Tantivy (extra dep, no need yet). FTS5 availability must be confirmed [U] |
| Git | `git` CLI via `subprocess`, argument lists | Exact git behaviour, allowlist-friendly, no extra dependency | GitPython (extra dep; ergonomics) |
| Code search | `ripgrep` if present, else Python fallback | Fast on large repos | Fallback only is simpler but slower [U] |
| CLI | Typer | Less boilerplate for many subcommands | `argparse` (no dependency, more code) |
| Logging | stdlib `logging`, JSON formatter, **stderr only** | stdout is the protocol channel; SDK docs recommend `logging` [V-SDK5] | structlog (extra dep) |
| Config | YAML + env vars (`AIOS_DATA_DIR`) | Add a project by editing config | — |
| Quality | pytest, ruff, mypy (strict), CI on GitHub Actions | Your engineering standards | — |
| Env | `uv` recommended, `pip` works | SDK docs use `uv` [V-SDK5] | — |

Windows notes: Claude Code on Windows closes a stdio server's stdin and ends
the process tree if it is still running 300 ms later [V-CC3], so the server must
exit promptly when stdin closes and must not depend on slow shutdown work.

---

## I. Risks

| # | Risk | Likelihood / impact [A] | Mitigation |
|---|---|---|---|
| 1 | **Self-approval** via shell (E.4) | Medium / High | Layered mitigations; accept or reduce residual risk explicitly |
| 2 | **Prompt injection** from repo, notes or commit text | Medium / High | Data-labelling, no high-risk tools in Phases 1–3, R3 out-of-band |
| 3 | **Stale memory** (decisions no longer true) | High / Medium | Status lifecycle, `last_reviewed`, `aios doctor` stale report, supersession |
| 4 | **Memory poisoning / noise** (low-quality auto-saved records) | Medium / Medium | Claude only *proposes*; you accept; no automatic detection until Phase 5 |
| 5 | **Retrieval misses** ("why are we doing this?" phrased differently from the record) | Medium / Medium | Tags, consistent `why` fields, layered search; measure before adding embeddings |
| 6 | **Context bloat** | Medium / Medium | Hard caps, snapshot size target, Claude Code limits [V-CC6] |
| 7 | **SDK churn**: v2 is recent and its migration guide was only partly read | Medium / Medium | Pin range, thin MCP layer, keep core independent of SDK |
| 8 | **Windows specifics**: paths, shutdown, ripgrep, FTS5 | Medium / Medium | Phase 1 spike on your machine; CI includes Windows [A] |
| 9 | **Verification theatre**: tests pass but don't test the change | Medium / High | Verification plan is part of the plan; diff review is mandatory; `verified` only set with a result record |
| 10 | **Scope creep** toward the Phase 5 list | High / Medium | Phase gates; this document is the contract |
| 11 | **Confidential data in memory repo** (company code context) | — / High | Private repo; secrets deny-list; no secrets in records; review company data-handling policy [A: not checked] |
| 12 | **ID collisions** if memory is later edited from two machines | Low now / Medium later | Single-user now; revisit IDs (ULID/prefix per author) before adding users |

---

## J. Implementation roadmap

**Phase 0 — Architecture** (this document). Exit: you approve or amend it.

**Phase 1 — Read-only MCP MVP**
1. Project skeleton: `pyproject.toml`, ruff, mypy, pytest, CI.
2. Spikes (time-boxed): FTS5 on target Python/Windows; ripgrep presence; a
   hello-world `MCPServer` registered in Claude Code via stdio; confirm
   resources/prompts appear in Claude Code as documented.
3. Domain models + registry loader + tests.
4. Policy engine + path confinement + deny-list + tests (adversarial paths).
5. Files and Git services + integration tests on temp repos.
6. Memory read path + index + `aios index rebuild`.
7. Audit log.
8. MCP interface: tools → resources → prompts.
9. CLI parity.
10. Sample Claude Code config and a walkthrough doc.
Exit: criteria in §G.

**Phase 2 — Persistent memory**: write path (`propose_*`, accept), decisions
lifecycle with supersession and history, tasks, assumptions, known issues,
project state, snapshot generation, `doctor`.

**Phase 3 — Planning**: `create_plan`, plan format, hash-bound approval,
`approve_plan`, CLI approval, plan state machine. No writes to project repos.

**Phase 4 — Controlled execution**: decide ADR-0006, then implement
execute → verify → audit → memory update with the status vocabulary.

**Phase 5 — Evaluate** (only with evidence): semantic search, summarisation,
health reports, decision detection, subagents, caching.

Each phase ships something usable and is gated by your approval.

---

## K. Open questions

1. **Approval self-bypass (E.4):** which mitigations do you want, and is any residual risk acceptable?
2. **Seeding memory:** do you want to backfill existing decisions (Slack, Notion, docs) in Phase 2, and from where?
3. **Project sizes and languages:** needed to size the file-search approach (ripgrep vs fallback).
4. **Deny-list contents:** default is `.env*`, `*.pem`, `*.key`, `id_*`, credential files; add company-specific patterns?
5. **Data repo:** will it be a private GitHub repo or a local folder only? (Affects backup.)
6. **Decision template:** keep `title` plus body, or restore separate `what`?
7. **Windows CI:** do you want a Windows runner in CI from the start?
8. **Execution ownership (ADR-0006):** revisit after Phase 3.

---

## L. Sources and verification status

Verified in this session (fetched or checked):

| Tag | Claim | Source |
|---|---|---|
| V-SDK1 | `from mcp.server import MCPServer`; v2 renamed from `FastMCP` | MCP Python SDK README and migration notes (py.sdk.modelcontextprotocol.io/whats-new, /migration) — migration page only partly read |
| V-SDK2 | `@mcp.tool()`, `@mcp.resource("users://{user_id}/profile")`, `@mcp.prompt()` decorators; template placeholder names must match function parameters | py.sdk.modelcontextprotocol.io/servers/resources/, /servers/prompts/ |
| V-SDK3 | `mcp.run()` defaults to stdio | py.sdk.modelcontextprotocol.io/run/ |
| V-SDK4 | `ToolAnnotations(read_only_hint=…)`; annotations are hints | py.sdk.modelcontextprotocol.io/servers/tools/ |
| V-SDK5 | stdout is the wire; use `logging`; Python 3.11+; `uv add "mcp[cli]"` | py.sdk.modelcontextprotocol.io/run/, /get-started/installation/ |
| V-SDK6 | v2 described as the current stable line; PyPI latest `mcp` 2.3.0 | SDK installation docs; `pip index versions mcp` run in this environment |
| V-CC1 | Prompts appear as `/servername:promptname (MCP)` and run as `/mcp__servername__promptname`; args split on whitespace | code.claude.com/docs/en/mcp.md |
| V-CC2 | Resources via `@server:protocol://path`; interactive `list_changed` refresh | same |
| V-CC3 | `claude mcp add … -- cmd args`; scopes local/project/user; `.mcp.json`; Windows stdin-close shutdown with 300 ms | same, plus permissions page |
| V-CC4 | Tool naming `mcp__<server>__<tool>`; hooks can match MCP tools with regex like `mcp__x__.*`; allow-globs need literal prefix | permissions.md, hooks.md |
| V-CC5 | `readOnlyHint` used for parallel read-only calls | mcp.md |
| V-CC6 | Warning above 10,000 tokens; default cap 25,000 (`MAX_MCP_OUTPUT_TOKENS`) | mcp.md |

Not found in the documentation sections read (treated as unverified): resource
templates in Claude Code [U-CC]; `destructiveHint` used for permissions [U-CC];
Windows `cmd /c` guidance; full semantics of hook `ask`/`defer` decisions;
whether Claude Code's Bash tool runs without a TTY; FTS5/ripgrep availability on
your machine; SQLite, Pydantic, and GitPython specifics stated from memory.

Fetches by sub-agents were partial for some pages (noted above), so "not found"
means "not seen", not "absent".
