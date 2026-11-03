# MCP server

Code: `src/aios/interfaces/mcp/` (`server.py`, `tools.py`, `resources.py`, `prompts.py`,
`context.py`, `render.py`), plus `src/aios/services/context.py` for the project snapshot.
Transport: **stdio**, started by Claude Code. **Read-only**: no tool can change a project, its git
state or the memory store. Every call goes through the operation policy and the audit log.

## Set up (Claude Code, Windows example)

1. Install: `uv venv` and `uv pip install -e ".[dev]"` in the `aion-ai-os` checkout.
2. Create a data directory **outside this repo** (ADR-0004) with a `registry.yaml`
   (see `examples/registry.yaml`) and, optionally, memory records (see `examples/memory/`).
   Check it with `aios doctor --data-dir <dir>`.
3. Register the server (note the option order from the Claude Code docs: `--env` before
   `--transport`, or the name is read as `KEY=value`):

```bash
claude mcp add --env AIOS_DATA_DIR=C:\path\to\aios-data --transport stdio aios -- C:\path\to\aion-ai-os\.venv\Scripts\python.exe -m aios serve
```

4. In Claude Code, `/mcp` should show `aios` connected. Tools appear as `mcp__aios__<tool>`,
   prompts as `/mcp__aios__analyze_project <project>`, resources under `@` as
   `aios:project://<id>/...`.
5. Permissions: all 14 tools are read-only. Claude Code permission rules name MCP tools
   `mcp__<server>__<tool>`, and a rule `mcp__aios` covers every tool of the server (per the Claude
   Code permission docs). The exact settings-file layout should be taken from the current docs.
6. Troubleshooting: the server logs JSON to stderr only (stdout is the protocol). If it does not
   start, run the same command in a terminal: configuration errors print on stderr, with exit code
   2 (no data directory) or 1 (registry or audit problem).

## What it exposes, and why each kind

MCP separates what the model decides to call (**tools**), what the application or user attaches
(**resources**) and what the user triggers (**prompts**).

**Tools** (parameterised queries; the model chooses when to call them):

| Tool | Purpose | Notable limits |
|---|---|---|
| `list_projects` | ids, descriptions, whether the directory exists | no absolute paths |
| `get_project_context` | state snapshot: git, recent commits, decisions, tasks, questions, constraints | sections that fail are listed |
| `list_project_files` / `read_project_file` / `search_project` | files | policy deny-list, text types only, 1 MiB per file, ~40k characters per read, regex needs ripgrep |
| `get_recent_changes` / `get_commit` / `get_diff` / `search_git_history` / `get_branch_status` | git | visible files only; hidden ones counted, never named |
| `search_memory` / `list_memory` / `get_memory_record` / `get_decision_history` | decisions, tasks, questions | superseded decisions are returned with the id that replaced them |

**Resources** (stable documents; five per project, created when the server starts):
`project://<id>`, `/context`, `/decisions`, `/tasks`, `/recent-changes`. Anything that depends on a
query is a tool, not a resource. Projects added to `registry.yaml` later work in the tools
immediately (the registry is re-read when the file changes) but their resources appear after a
restart.

**Prompts** (user-triggered workflows; text only): `analyze_project <project>` and
`review_recent_changes <project>`. They take one argument because Claude Code splits prompt
arguments on whitespace. An unknown project returns an explanatory message listing the valid ids
(the SDK hides exception text for prompts).

## Write access (not yet available)

`aios serve --allow-propose` exists but currently changes nothing a client can see: it enables the
PROPOSE permission level in the policy (default is read-only), records `allow_propose` in the
`server_start` audit event, and leaves the tool list at 14. The propose tools themselves arrive in
Phase 2c ([PHASE2-PLAN.md](PHASE2-PLAN.md)). Registering the server without the flag keeps it
read-only.

## Safety design

- **No write path.** There is no tool for editing files, committing, pushing, deleting or
  running anything. The policy engine enables only `READ_ONLY` operations, and every operation the
  server exposes is registered as such (tested).
- **Policy and audit on every call** (tools, resource reads, prompts): `request` event first, then
  `result`. If the request cannot be written the call is refused (fail closed); the server also
  refuses to start if the audit log is unwritable.
- **Errors:** expected problems (denied path, unknown project, bad argument) reach the model as
  clean messages. An unexpected exception reaches it only as "Error executing tool ..."; details go
  to stderr.
- **Untrusted text.** File contents, commit messages and notes are written by people and may contain
  instructions. The server instructions and tool descriptions tell the model to treat them as data.
  That is guidance the model may or may not follow; the hard barrier is that no tool can act.
- **No leakage.** A battery of about 45 calls (including hostile ones) is checked so that no planted
  secret, and no local absolute path, appears in any output, resource, prompt, error message or
  audit entry. The audit log does record call *arguments*, so a probe such as a search for a guessed
  secret is visible there (tested), but secret content is not.
- **Token cost.** Structured results are compact JSON (no indentation) and content is plain text;
  search and list hits omit `path` and `score`.

## Verified in a real client (single run)

With the Claude Code CLI (headless, `claude -p`) against a sample project, I asked "what changed
recently?" and "why did we separate MENA Slack from US Slack, and was there a previous approach?".
The model called `get_recent_changes`, `search_memory` and `get_decision_history`, and its answer
cited the right commit hashes and decision ids (checked against the fixture). All seven audit events
were present and the chain verified. This is **one run** of one scenario: it shows the path works,
not how reliably a model chooses tools.

## Not verified / limitations

- Interactive Claude Code on Windows with real projects; behaviour across models and sessions; how
  well the tool descriptions steer tool choice (unmeasured).
- Token sizes of outputs were not measured systematically (caps are in characters).
- Regex search needs ripgrep; it was absent on the Windows machine when last checked, so only
  literal search would work there.
- Under heavy parallel use only an in-process 20-call test exists.
- Resource templates are not used (concrete resources only).
- Approvals, plans and any write operation do not exist yet (Phases 3-4); the approval design
  is still open (see docs/spikes/PHASE1-SPIKES.md, S9).

## Test evidence

- 35 MCP tests: 31 in-process (inventory, content of every tool, error behaviour, the leak battery,
  audit pairing, fail-closed, registry reload, degraded projects, resources, prompts, concurrency)
  and 4 over real stdio (a full session through the SDK client; first stdout line is clean JSON-RPC;
  shutdown on stdin close in under 3 s; start-up failures use stderr and leave stdout empty).
  597 tests pass in total locally on Linux.
- 16 deliberate breakages each make the suite fail: audit bypass, a missing policy entry, errors
  not converted, exception text leaking, registry never reloaded, fail-open auditing, pretty-printed
  JSON, noisy hits, unvalidated prompt arguments, unaudited resources, missing instruction
  warnings, tools not marked read-only, a local path in a resource, a stray `print` before serving,
  no start-up audit check, a prompt naming a non-existent tool. Two first results were invalid and
  redone (one referenced an unimported name; one result line was hidden by log output).
- Fixes found along the way: memory index rebuilds could collide between threads (now serialised,
  with a test); a prompt error message was swallowed by the SDK (now explained in the prompt).
