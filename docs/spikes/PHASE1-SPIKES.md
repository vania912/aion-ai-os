# Phase 1 spikes — results

Date: 2026-11-03 · Environment: **Linux cloud container** (not the target Windows machine).
Code: [`spikes/`](../../spikes/). These are throwaway checks, not production code.

| # | Question | Result | Evidence / caveat |
|---|---|---|---|
| S1 | Is SQLite FTS5 usable from Python? | **Yes** here (Python 3.13.18, SQLite 3.53.1, `ENABLE_FTS5`, porter tokenizer, bm25 ranking worked) | Windows build **not tested** → run `spikes/env_check.py` there |
| S2 | Is ripgrep available? | **Yes** here (14.1.0) | Not tested on Windows |
| S3 | Does `mcp` 2.3.0 `MCPServer` serve tool + concrete resource + templated resource + prompt + `read_only_hint` over stdio? | **Yes**, all listed and callable via the SDK client | `spikes/hello_server.py`, `client_check.py`. Template `project://{name}/decisions` listed as a template and read correctly |
| S4 | Does a stray `print` break stdio? | **Yes**: a `print` at import made the first stdout line invalid JSON-RPC | `spikes/shutdown_check.py` against a modified copy. Confirms the stderr-only logging rule |
| S5 | Shutdown on stdin close | Exit in ~165 ms on Linux (<300 ms Claude Code limit) | **Windows not tested**; the 300 ms limit is from the Claude Code docs |
| S6 | Does real Claude Code (2.1.317) talk to the server? | **Yes**, headless (`claude -p --mcp-config`): tool call and concrete resource list/read worked | The model's reply showed the tool result as `{"result":"spike-ok"}` (structured wrapper; raw payload not inspected). Project-scoped `.mcp.json` servers showed "Pending approval" in `claude mcp list` |
| S7 | Do prompts show as `/mcp__server__prompt` and do resources appear in `@` autocomplete? | **Not verified** | Needs the interactive UI; headless mode cannot show it. Docs say yes (see ARCHITECTURE §L) |
| S8 | Are resource *templates* visible to Claude Code? | **Not verified** | The headless resource listing returned only the concrete resource; templates may be listed separately or not at all. Design already registers concrete resources per project |
| S9 | Does the shell tool Claude Code uses have a TTY on stdin? | **No** in this harness (`[ -t 0 ]` false) | Supports the TTY-check mitigation (ARCHITECTURE §E.4), but this is a cloud harness; the user's local Claude Code on Windows is **not verified** |

## Windows results (user's machine, `env_check.py`, 2026-11-03)

| Item | Result |
|---|---|
| OS | Windows 11 (10.0.26300) |
| Python | 3.13.14 (meets the >= 3.11 requirement) |
| SQLite / FTS5 | 3.50.4 / **OK** → S1 confirmed on Windows |
| git | 2.54.0 |
| ripgrep | **Not found on PATH** → S2: on this machine the pure-Python fallback would be used unless `rg` is installed |
| uv | 0.11.30 |
| stdin TTY | True, **in the user's own PowerShell**. This is the expected case for typed approvals. It does **not** answer S9, which is whether Claude Code's shell tool on Windows has a TTY (still untested) |

| `mcp==2.3.0` install (uv venv, CPython 3.12.13 chosen by uv) | Installed cleanly on Windows (29 packages incl. pywin32) |
| `claude mcp add` of the spike server (local scope) | Succeeded |
| S5 shutdown check, run directly from PowerShell | clean first line=True; exit after stdin close=**178 ms** (<300 ms). Not launched by Claude Code itself, so Claude Code's own handling is still untested |

Still open: S7/S8 (interactive prompt and resource behaviour), S9 (Claude Code shell tool TTY on Windows).

## To do on your machine (≈5 minutes)
1. `python spikes/env_check.py` and paste the output (no project data in it).
2. Register the spike server and check the interactive UI:
   `claude mcp add --transport stdio aios-spike -- <python> <path>/spikes/hello_server.py`, then in Claude Code:
   - type `/mcp__aios-spike__analyze_project` (S7)
   - type `@` and look for `project://demo` and whether the templated URI appears (S7/S8)
3. Optionally `python spikes/shutdown_check.py spikes/hello_server.py` on Windows (S5).

## Consequences for the design
- No change to the architecture so far. FTS5 and ripgrep remain assumptions for Windows until step 1 is done.
- The stdout rule is now backed by a measurement: Phase 1 must lint for `print(` and route logging to stderr.

## Interactive Claude Code results (user's Windows machine, 2026-11-03)

| # | Result |
|---|---|
| S7 prompts | `/aios-spike:analyze_project (MCP)` appeared and ran. It failed only because the required `project` argument was not supplied; Claude Code reported the usage as `/mcp__aios-spike__analyze_project project`. **Design consequence:** prompt arguments are positional and required ones must be typed, so keep prompts to one simple argument and consider making it optional |
| S8 resources | `@` listed `aios-spike:project://demo` and a second `aios-spike` entry whose description reads "Templated decisions…" (name truncated on screen). **Inferred**, not confirmed, to be the `project://{name}/decisions` template |
| S9 TTY | Per the user's Claude Code session, both its PowerShell and Bash tools printed **`True`** for `sys.stdin.isatty()`. That session noted that on Windows `isatty()` may be `True` for the NUL device (stated from model memory, **unverified**). `spikes/tty_check.py` was written to tell a real console from NUL; **its output on the user's machine is still needed** |
| Permission mode | The user's tool calls were labelled "Allowed by auto mode" |

**Consequence for ADR-0008:** the revisit trigger "Claude Code's shell has a TTY" has fired, so the
TTY-check layer of the approval design cannot be relied on as written. No approval code exists yet.

## Claude Code permission behaviour (documentation check, 2026-11-03)

Source: a documentation read by a sub-agent of code.claude.com pages (permissions, permission
modes, hooks, settings, tools reference). Some sections were truncated in its reads; "not found"
means "not seen". Quotes are as reported by that agent.

- Explicit **`ask` rules still force a prompt in auto mode**, and `bypassPermissions` lists
  ask-rule matches among actions no mode auto-approves. In `dontAsk` mode ask-rule matches are
  *denied* rather than prompted. `acceptEdits`: not stated.
- **`deny` rules block in every mode**, including `bypassPermissions`, and beat allow/ask. Matching
  is pattern-based; a Bash deny rule does not match the same program by another path or inside
  `sh -c`.
- **PreToolUse hooks** can deny a call (`permissionDecision: deny` or exit code 2). The docs state
  hook decisions don't bypass permission rules. Not found: an explicit statement that hooks run in
  every mode, or that a hook deny outranks an auto-mode classifier approval.
- **Managed settings** are the highest-precedence level and cannot be overridden by lower levels;
  `.claude` is a protected directory (writes prompt in default/acceptEdits mode, go to the
  classifier in auto mode, are denied in dontAsk, **allowed in bypassPermissions**).
- **Shell stdin/TTY:** the docs read do not say whether the shell tools have a TTY, a null stdin, or
  can open `/dev/tty` / `CONIN$`.

Implication: relying on `ask` rules for R1/R2 prompts is supported by the docs even in auto mode,
but the TTY property is undocumented and was observed as `True` here. The approval mechanism for
high-risk operations needs a decision before Phase 3 (options in the project discussion).
