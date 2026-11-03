# Files service

Code: `src/aios/services/files.py`, `src/aios/adapters/code_search.py`, models in
`src/aios/domain/files.py`. Built on the [policy engine](POLICY.md): every path goes through
`PathGuard`.

## Operations

| Operation | Behaviour |
|---|---|
| `list_files(path, depth, max_entries)` | Visible files/dirs, directories first then files, case-insensitive name order. Depth 1-5, up to 1000 entries (`truncated` flag). Skips noise directories (`node_modules`, `.venv`, `dist`, ...) |
| `read_file(path, start_line, end_line)` | UTF-8 text (BOM and CRLF handled), 1-based inclusive line ranges, output capped at 40,000 characters (`truncated` flag, `total_lines`). Binary files (NUL in the first 8 KiB) are refused |
| `search(query, path, glob, regex, ignore_case, max_results)` | Literal search by default, up to 200 results, lines clipped to 300 characters, deterministic order |

CLI: `aios files list|read|search <project> ...` (add `--data-dir` or set `AIOS_DATA_DIR`).

## Search engines

| | ripgrep | Python fallback |
|---|---|---|
| Chosen when | `rg` is on PATH | otherwise |
| Literal search | yes | yes |
| Regex | yes (Rust regex engine) | **refused** |
| `.gitignore` | honoured | **not** honoured (only the noise-directory list) |
| Glob | ripgrep globs | simple `*`, `**`, `?` translation; edge cases may differ |
| Path order | ripgrep `--sort=path` | name order, depth-first (same in common cases) |

Why regex is ripgrep-only: Python's `re` cannot be interrupted mid-match, so a pathological
pattern could hang the server. The Rust engine is documented as linear-time [remembered, not
verified]; one test checks that `(a+)+$` on 5,000 `a`s returns promptly.

## Safety properties (and how they are enforced)

- **Nothing protected appears in results.** Both engines' matches are filtered through
  `PathGuard.is_visible`, so deny-listed, non-text, oversized, out-of-root and symlinked-away
  files are dropped even if the engine finds them.
- **No option injection into ripgrep.** The command is an argument list (`shell=False`); the query
  is passed as `--regexp=VALUE`, the glob as `--glob=VALUE`, and the path after `--`. Globs
  starting with `-`, `!` or `/`, or containing `..` or `:`, are rejected.
- **User config ignored:** `--no-config` stops `RIPGREP_CONFIG_PATH` from changing behaviour.
- **Bounded:** query length, result count, line length, output size and a 20 s search timeout
  (the process is killed on timeout).
- **Errors do not leak the project's absolute path** (replaced with `<root>`).

## Limitations

- **ripgrep availability:** CI installs ripgrep on Linux only (and fails the build if it is
  missing there). The Windows CI jobs exercise the Python fallback; whether the Windows runner
  images happen to include `rg` was not checked. On the user's Windows machine ripgrep was not
  installed when last checked, so the fallback would be used there.
- **Check-then-open race** (see POLICY.md) applies to reads as well.
- **Fallback speed** on large repositories is unmeasured. `aios project inspect` (not built yet)
  is meant to supply size data.
- The timeout tests that fake `rg` with a shell script run on POSIX only.
- Noise-directory skipping is convenience, not security; the deny-list is the security control.

## Test evidence

- 235 tests pass locally (Linux, Python 3.13, ripgrep 14.1.0 installed); the shared tests run
  against both engines.
- Manual mutation checks, each of which makes the suite fail: removing the visibility filter in
  either engine, passing the query positionally to ripgrep, and dropping `--no-config`.
  The last one initially **survived**: my first version of its test could not detect the change
  (a user-supplied glob overrode the fake config). It was rewritten and the mutation is now caught.
