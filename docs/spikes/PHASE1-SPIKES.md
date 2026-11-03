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
