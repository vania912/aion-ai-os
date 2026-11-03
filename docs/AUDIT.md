# Audit log

Code: `src/aios/domain/audit.py`, `src/aios/adapters/audit_store.py`, `src/aios/services/audit.py`.

**Status: built and tested, but not wired into anything yet.** `AuditLog.run()` is the wrapper the
MCP server will put around every tool call (next step). The `aios` CLI commands are human debugging
tools and are **not** audited. Until the MCP server exists the log is empty unless you write to it.

## What is recorded

Two events per call, so a crash mid-operation leaves evidence:

1. `request` (before anything runs): operation, project, permission level, verdict
   (`allow`/`deny`) and sanitised arguments.
2. `result` (after): outcome `ok` / `error` / `denied`, an error **code** (never the message),
   duration, and small counts supplied by the caller (for example `lines=120`).

`call_id` links the pair. A denied operation still produces both events and is never executed.
`note` events record free-standing actions.

```json
{"seq":3,"ts":"2026-11-03T15:04:05.123Z","event":"request","op":"read_project_file","call_id":"9f1c…",
 "actor":"mcp","project":"pulse","level":"read_only","verdict":"allow","outcome":null,"code":null,
 "args":{"path":"src/app.py"},"meta":{},"prev":"<hash of seq 2>","hash":"<sha-256 of this event>"}
```

**Never logged:** file contents, search hits, diffs, record bodies, exception messages. Arguments
are limited to 12 scalar values of at most 200 characters (longer text is truncated, lists and
objects become `<list of N>`), control characters are replaced, and known credential shapes are
redacted: private-key headers, JWTs, AWS access key ids, GitHub and Slack tokens, `Bearer ...`,
`password=` / `token=` / `api_key=` style assignments, and any 48+ character token-like string.
Redaction is best effort: a secret typed into a query without one of these shapes is logged
(truncated). Ordinary text such as "token refresh logic" and 40-character git hashes is left alone.

## Failure policy

- **Request cannot be written → the operation does not run** (fail closed). Trade-off: a full or
  unwritable disk stops tool use rather than letting actions go unrecorded.
- **Result cannot be written →** the operation has already happened, so its result is returned and
  an error is logged to stderr. This leaves a request without a result, which is itself visible.

## Tamper evidence (and its limits)

Events are append-only JSON lines in `<data-dir>/audit/YYYY-MM.jsonl` (UTC months). Each carries a
global sequence number, the previous event's hash and its own SHA-256 hash. `aios audit verify`
(also run by `aios doctor`) detects edited, deleted, reordered or duplicated events, a missing month
file, and unparseable lines.

It is **not tamper-proof**:
- Anyone who can write the files can rewrite the whole chain consistently.
- **Cutting off the end of the log is not detectable from the log alone** (a test documents this).
  Run `aios audit anchor` now and then and keep the output somewhere the log's writers cannot edit;
  a later anchor that is older than the log, or a log shorter than the anchor, shows truncation.
- Files are created with mode `0600` on POSIX. On Windows POSIX mode bits do not restrict access;
  use the ACLs of the data directory [not verified here].
- A crash can leave a torn last line. It is reported as `unparseable`; the next event starts on a
  fresh line and chains to the last valid event, so one crash does not poison the rest.

## Concurrency and durability

Appends from several threads and processes (the CLI and the MCP server) are serialised in two
steps: threads of one process queue on an in-process lock, then the writer takes an exclusive OS
file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) by **polling** non-blocking
attempts. If the lock cannot be taken within 30 seconds (configurable) the append fails with
"timed out waiting for the audit lock" and, because request events are written before anything
runs, the call is refused (fail closed). Every event is flushed with `fsync`; its cost per call was
not measured.

*History:* the first version used the blocking Windows lock mode. A Windows CI run failed under 20
simultaneous calls with "Resource deadlock avoided". My understanding, from that error and from
Microsoft's documentation as I remember it [not verified], is that this mode gives up after about
ten one-second retries, which many queued writers can exceed on a slow disk. The polling lock and
the in-process queue replace it; the Windows CI result for that fix is the confirmation.

## Retention

Monthly files are never rotated, compressed or deleted automatically, and there is no size cap.
The data directory is a git repository in your setup; decide whether `audit/` should be committed
or listed in its `.gitignore` (it is not ignored automatically).

## Commands

`aios audit list [--limit --op --project --outcome --call-id --since]` (newest first) ·
`aios audit show <call-id>` · `aios audit verify` (exit 1 on problems) · `aios audit anchor`.

## Test evidence

- 72 tests added (600 pass in total, 1 POSIX-skip, locally on Linux). They cover the chain across
  restarts and month rollover, a backwards clock, torn writes, edits / deletions / reordering /
  duplication / a deleted month, the documented tail-truncation limit, 8 threads and 3 separate
  processes appending concurrently, 20 writers behind a slow `fsync`, a lock held by another
  process (timeout, then recovery), oversized events, redaction patterns and false-positive
  guards, argument bounds, request/result pairing, denied operations never running, failure
  classification, fail-closed and result-failure behaviour, listing filters, and the CLI.
- 15 deliberate breakages each make the suite fail (verification checks, fail-closed, denied
  operations executing, redaction, file lock, torn-tail repair, month ordering, truncation,
  swallowed result failures, file mode, outcome classification, doctor integration, control
  characters). Several first attempts were invalid and were redone: one used a helper that was
  not imported, two used patterns that no longer matched the formatted code, and one mutation
  survived across runs because Python reused stale cached bytecode (same-length edit, same
  second); mutation runs now disable bytecode caching.
- The Windows file-lock path (`msvcrt`) and the 3-process test are exercised by the Windows CI jobs
  only; see the CI result for the commit that introduced this document.
