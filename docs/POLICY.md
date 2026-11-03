# Policy engine

Code: `src/aios/policy/`. Tests: `tests/unit/test_policy_files.py`, `test_policy_operations.py`.

## What it does

**File access (`PathGuard`)** — one guard per registered project root. Callers pass
project-relative paths; the guard returns a real absolute path or raises `PolicyViolation` with a
stable `code`.

Order of checks in `resolve()`:

1. Reject control characters and paths over 4096 characters (`invalid_path`).
2. Reject absolute paths: leading `/` or `\`, drive letters, UNC (`absolute_path`).
3. Reject `:` anywhere (also blocks NTFS alternate data streams) (`invalid_path`).
4. Reject any `..` component, even one that would stay inside the root (`path_escape`).
5. Per component: reject trailing dot/space, Windows reserved device names (`con`, `nul`,
   `com1`...), and likely 8.3 short-name aliases (`invalid_path`).
6. Deny-list on the path **as given**: denied directory names anywhere in the path (`.git`,
   `.ssh`, `.aws`, ...) and denied file-name patterns on the final component (`.env*`, `*.pem`,
   `id_rsa*`, ...), case-insensitive.
7. Resolve symlinks; the real path must be inside the real root (`path_escape`).
8. Run the deny-list again on the **resolved** location, so a link to `.env` or `.git/config`
   inside the root is still refused.

`check_readable_file()` adds: exists, is a regular file, extension or file name on the allow-list,
size at most `max_file_bytes` (default 1 MiB). `is_visible()` is the non-raising form for
filtering listings and search results. `looks_binary()` is a NUL-byte check on a leading sample.

**Operations (`OperationPolicy`)** — default deny. An operation must be registered with a
`PermissionLevel`, and that level must be enabled. Phase 1 enables only `READ_ONLY`, so
`PROPOSE`, `APPROVAL_REQUIRED` and `WRITE` operations are refused even if registered.

Error messages contain only the caller-supplied path, never the resolved absolute path or a
symlink target (tested).

## Limitations (not covered, or only partly covered)

- **Check-then-open race (TOCTOU):** the path is vetted, then opened later by the caller. A local
  actor who can swap a file for a symlink between the two steps is not stopped. Relevant only if
  something else can write to the project while the server runs.
- **Hard links** inside the root to files outside it are not detected.
- **The deny-list is a baseline.** Secrets in files with allowed names or extensions (a key pasted
  into `config.py`) are not detected: there is no content scanning or redaction yet.
- **Name patterns apply to the final path component only.** A directory named `.env` (a common
  virtualenv name) is therefore readable; only the directory names in `denied_dir_names` are
  blocked as directories.
- **Over-blocking by design:** any `:` in a path, any `..`, trailing dot/space, and names shaped
  like 8.3 aliases (for example `backup~1`) are refused on every OS. Unusual file types are
  refused until added to `allowed_extensions`.
- **8.3 and symlink behaviour on Windows** is covered by heuristics and CI on GitHub's Windows
  runners, not by testing on a real user machine. Symlink tests skip themselves if the OS
  refuses to create links.
- Local filesystem only. Network shares and case-sensitivity oddities are not specifically tested.

## Test evidence

- 117 tests pass locally on Linux (Python 3.13) at the time of writing; CI runs Linux and Windows.
- Adversarial cases: traversal, absolute and UNC paths, drive letters, ADS, control characters,
  reserved names, 8.3 aliases, symlinks out of the root and into denied locations, case variants.
- An invariant fuzz test tries all 2,379 combinations of up to three path tokens and asserts that a successful
  `resolve()` is inside the root and never reaches `.git` or a denied final name.
- Mutation check (manual, once): disabling the post-symlink deny check, the confinement check, or
  the `..` rejection each made the suite fail.

## Extending

Pass a `FileAccessPolicy` with extra `denied_name_patterns`, `denied_dir_names` or
`allowed_extensions` when constructing a `PathGuard`. Loading these from configuration is not
implemented yet.
