# Memory (read path)

Code: `src/aios/domain/memory.py`, `src/aios/adapters/memory_files.py`,
`src/aios/adapters/memory_index.py`, `src/aios/services/memory.py`.
Design background: ARCHITECTURE.md section D and ADR-0003.

**Scope today:** reading (Phase 1), human-authored writes from the CLI (Phase 2a), the **review
workflow** (Phase 2b) and, **only when the server is started with `--allow-propose`**, MCP tools
through which Claude proposes unreviewed records (Phase 2c). By default the MCP server is
read-only. `accepted_digest` is written on acceptance but not yet verified (2d). Decisions are in
[ADR-0009](adr/0009-phase2-write-model.md); the plan is in [PHASE2-PLAN.md](PHASE2-PLAN.md).

## Where records live

The data directory (`--data-dir` or `AIOS_DATA_DIR`, kept outside this public repository):

```
<data-dir>/
├── registry.yaml
├── projects/<project>/
│   ├── decisions/DEC-0001.md      assumptions/ASM-0001.md   constraints/CON-0001.md
│   ├── questions/Q-0001.md        discoveries/DSC-0001.md   tasks/TSK-0001.md   notes/NOTE-0001.md
└── .index/aios.sqlite             derived cache, ignored by its own .gitignore, safe to delete
```

Each record is Markdown with YAML front matter. Ids are unique across the whole store (not per
project), at least four digits, and the file name must be `<ID>.md`.

```markdown
---
id: DEC-0002
type: decision            # decision | assumption | constraint | question | discovery | task | note
project: pulse            # must match the directory name
title: Separate MENA Slack from US Slack
status: active
date: 2026-10-05          # ISO date
made_by: [Name]
why:
  - Different workspace requirements
alternatives_considered:
  - A single workspace handler with a region flag
supersedes: DEC-0001      # decisions only
superseded_by: null       # decisions only
reason_for_change: Why the previous approach was replaced
related_files: [src/app.py]     # project-relative; display only, never opened by the server
related_tasks: [TSK-0001]
related_decisions: []
assumptions: [ASM-0001]
constraints: [CON-0001]
tags: [slack]             # lowercase letters, digits, '_' or '-'
---
Free-text context, trade-offs, anything a future reader needs.
```

Statuses: decision `proposed|active|superseded|deprecated|rejected`; task
`open|in_progress|blocked|done|cancelled`; question `open|answered|dropped`; assumption
`unverified|validated|invalidated`; constraint `active|lifted`; discovery `active|obsolete`;
note `active|archived`. Decision-only fields on other types are rejected, as are unknown fields
(typos are errors). All problems in a file are reported together.

## Writing records (Phase 2a)

```
aios decision create <project> --title T --why W [--why W2] [--alternative A] [--made-by NAME]
                     [--supersedes DEC-0001 --reason-for-change R] [--tag t] [--body TEXT | --body-file F]
aios decision repair <id>          # finish a half-applied supersession (safe to repeat)
aios task add <project> --title T [--tag t] [--related-decision DEC-0002] [--body ...]
aios task status <id> <open|in_progress|blocked|done|cancelled>
aios memory add <project> --type assumption|constraint|question|discovery|note --title T [...]
```

Records created this way are `origin: human`, `reviewed: true`, dated today, with ids allocated
for you (never reused, including ids of rejected or unparseable files). Nothing is committed: the
data repository changes on disk only. Each write adds a `note` event to the audit log (actor
`cli`, id and type only; never titles or content); if the audit log cannot be written you get a
warning and the write still stands.

Rules the writer enforces (the same code will serve Claude's proposals):

- **Server-controlled fields.** A caller supplies content only; `id`, `date`, `origin`, `reviewed`,
  initial status and the acceptance fields are set by the server and rejected if sent.
- **Claude-created records** (not reachable yet) are `origin: claude`, `reviewed: false`; a decision
  starts `proposed` (never `active`) and needs at least one `evidence` entry; bodies are capped at
  8,000 characters; a project may hold at most 20 unreviewed records (my estimate for the default).
- **References must exist** with the right type (`related_tasks`, `related_decisions`,
  `assumptions`, `constraints`); a decision needs at least one reason.
- **Supersession** needs an *active* decision of the same project that is not already being
  replaced. A decision that supersedes another starts `proposed`; completing it (the CLI does so
  immediately) activates it and marks the predecessor `superseded` in this order: successor first,
  then predecessor. A crash in between leaves a one-sided link that the reader tolerates and
  `aios doctor` reports; `aios decision repair` (or running the step again) finishes it. A
  successor that is `rejected` or `superseded` cannot be revived.
- **Tasks** move only along allowed transitions (for example `done` can only be reopened).
- **Existing files** are edited only for `status`, `superseded_by`, `reviewed`, `accepted_by` and
  `accepted_digest`, by replacing that one value: comments, quoting, trailing comments, CRLF line
  endings and a BOM are preserved, and the result is re-validated before it is written. Files with
  an unusual shape (duplicate keys, multi-line values, mixed line endings, invalid UTF-8) are refused
  with a message and left untouched.
- **Files are written atomically** (temporary file, then replace; retried when Windows briefly locks
  the target), under a lock file in the git-ignored `.index/` directory, so ids stay unique across
  threads and processes.
- **Never edited:** a record's title, reasons, alternatives and body after creation (not exposed).

New optional fields (all backward compatible; old files stay valid): `origin`, `reviewed`,
`evidence` (what a claim rests on), `accepted_by`, `accepted_digest`, `rejection_reason`. Text fields reject NUL and
other control characters, and bodies are stored with LF endings and no outer blank lines.

## Reviewing proposals (Phase 2b)

Records created for Claude are `origin: claude`, `reviewed: false` (a decision is also `proposed`).
Until a human accepts them they are **unverified**: they are returned by search and `get_memory_record`
with the state shown, but they never count as established.

```
aios review list [--project P] [--type T]     # the queue, newest first
aios review show <id> [--as NAME]             # full content, evidence, similar records, what accepting does
aios review accept <id> [--yes] [--as NAME]   # asks first; --yes skips the question
aios review reject <id> --reason "..." [--yes]
aios memory list|search --unreviewed | --reviewed
```

- **Accept.** A decision becomes `active`, `reviewed`, with `accepted_by` and `accepted_digest`;
  if it supersedes another decision, that one becomes `superseded` (successor first, then
  predecessor, as in 2a). Other record types only become `reviewed` (status unchanged). Accepting
  again is a no-op, and accepting a reviewed successor whose predecessor was never updated finishes
  that step. **Blocked** (with the reason shown): a decision that is not `proposed` or `active`, or
  one whose predecessor is no longer active ("reject this proposal instead").
- **Reject.** Only unreviewed records can be rejected. The reason is recorded in the file
  (`rejection_reason`; a decision also gets status `rejected`) and the file moves to
  `projects/<project>/_rejected/<type-dir>/`, where it is **not indexed or searchable**. Its id is
  never reused. A superseding proposal that is rejected leaves its predecessor untouched. Rejecting
  again is a no-op. If a crash happens after the reason is recorded but before the move, `aios
  doctor` reports `rejection_pending_move` and rerunning the reject finishes it.
- **The confirmation prompt is a convenience, not a security control.** It makes you read what
  accepting changes. Claude could answer it from a shell command, so the real controls are the
  Claude Code deny rule on these commands (ADR-0009, D2: pattern-based, with documented limits), the
  unreviewed labelling below, and the audit log (`accept_record` / `reject_record`, actor `cli`, id
  and type only, never content or the reason).
- **What "unverified" means in every view:**
  - the project snapshot (`get_project_context`) and the `project://<id>/context` resource list
    unreviewed records only under "Awaiting review (UNVERIFIED, not established facts)", never under
    active decisions, open tasks, questions or constraints;
  - `search_memory` / `list_memory` hits carry `"reviewed":false,"origin":"claude"` (omitted for
    trusted records to save tokens) and take a `reviewed` filter; `aios memory search|list` print
    `UNREVIEWED(claude)`;
  - `get_decision_history` reports a proposed successor as `pending_id` and keeps `current_id` on
    the decision actually in force, with a note;
  - the server instructions and tool descriptions tell the model to treat `reviewed:false` as an
    unverified proposal.
- **Similar records.** `review show` lists existing records that share words with the proposal's
  title (any shared word, best match first, never the record itself), as a duplicate check. It is a
  keyword overlap, so it is noisy for very short or generic titles.
- **`aios doctor`** no longer reports "not reciprocal / predecessor not superseded" for a *pending*
  proposal (that state is expected); it still does once a successor is in force.
- The search index schema is now version 2 (`origin`, `reviewed` columns). An index from before
  rebuilds itself on the next query.

## Decision lifecycle

`DEC-0001 (superseded) <-- supersedes -- DEC-0002 (active)`. Nothing is deleted. The reader
tools follow **both** pointers, so a chain with a one-sided link still resolves, and report
inconsistencies as notes. `aios decision history DEC-0001` prints the chain oldest first, with the
reasons and the `reason_for_change` of each replacement, and the current decision.

`aios doctor` / `aios memory check` flag: `status_mismatch`, `superseded_without_successor`,
`superseded_by_missing`, `supersedes_missing`, `not_reciprocal` (warning),
`predecessor_not_superseded` (warning), `bad_supersession_target`, `multiple_successors`,
`supersession_cycle`, `dangling_reference` (warning), `unknown_project` (warning, needs the registry),
`unreviewed_active` (an unreviewed decision marked active), `reviewed_without_acceptance` (warning),
`review_backlog` (warning at 20 unreviewed), `rejection_pending_move` (a rejection was recorded but the
file was not archived yet), plus file problems: `invalid_record`,
`location_mismatch`, `duplicate_id`, `file_too_large`, `unexpected_file` (warning),
`bad_project_dir`. One bad file is skipped and reported; it never stops the index or the other
records.

## Search (layered, cheapest first)

1. **Exact id** anywhere in the query (`what did dec-0002 decide?`).
2. **All words** (FTS5, Porter stemming, `title` weighted above `body`, `why`/`alternatives`/people
   included, tags included).
3. **Any word**, if no record contains all of them.

Questions are reduced to words; common question words ("why", "did", "we", ...) are dropped unless
nothing else is left. Filters: project, type, status, tag. Superseded and deprecated decisions are
returned with their `status` and `superseded_by` shown, because history is the point of memory.

There is **no semantic search**: a question that shares no stems with the record will not find it
(ADR-0007). Tags and consistent `why` wording are the mitigation until misses are measured.

## The index

A cache, rebuilt from the files automatically when they change (path, size and modification time
are compared), when it is missing or corrupt, or when its schema version differs.
`aios index rebuild` forces it; `aios index status` shows whether it is stale. It is built in a
temporary file and moved into place atomically, so a reader never sees a half-built index; queries
use a read-only connection and bound parameters, and user text is reduced to quoted word tokens
before it reaches FTS5.

## Commands

`aios index rebuild|status` · `aios memory search|list|show|check` ·
`aios decision list|show|history` · `aios doctor` (registry and memory).

## Limitations

- **Change detection is by path, size and mtime.** An edit that keeps both size and mtime
  identical is not noticed until `aios index rebuild`.
- **Hand edits are not blocked.** The writer never edits reasoning, but nothing stops you editing a
  file by hand; detecting that (via `accepted_digest`) is planned for 2d.
- **Keyword search only**; no synonyms, no embeddings.
- **YAML aliases are rejected** (a tiny file of nested aliases can expand enormously when walked);
  a 256 KiB file-size cap applies to every record.
- **Record text is untrusted data when shown to a model.** Records are written by people, but may
  quote external text; the MCP layer should label them as data, not instructions.
- Symlinked record files and directories are ignored, not followed.
- The index replace step retries when Windows refuses to replace an open file; that retry path
  is not tested.
- SQLite FTS5 availability: confirmed on the Linux test environment and on the user's Windows
  Python (SQLite 3.50.4, spike result). If FTS5 is missing, rebuilding raises a clear error.

## Test evidence

Phase 2b adds about 75 tests (828 pass in total locally on Linux, 1 POSIX-skip): read-side review
state (index migration, filters, snapshot sections, resource markers, "pending is not current" in
history, MCP output), the review service (every accept path, every blocking rule, reject and its
crash recovery, concurrent accepts and rejects, Windows-style move retries, symlinked archive
refused, unrelated files byte-identical) and the review CLI (prompts answered y, n and not at all,
`--yes`, audit events without content, nothing committed to a git data repo). 22 deliberate
breakages of this code each make the suite fail (three first needed their patterns corrected
because the code had been reformatted). Findings fixed along the way: the similarity lookup could
never find a near-duplicate, because the proposal always matched its own title in the all-words
layer; pending proposals produced misleading doctor warnings; rejecting an already archived record
failed from the CLI instead of being a no-op.

Phase 1 (read path): 489 tests and 17 deliberate breakages, all caught (details in the Phase 1 commit
history). Phase 2a adds about 265 tests (754 pass in total locally on Linux, 1 POSIX-skip):

- writer: a hostile-string round-trip fuzz over every text field (YAML syntax, `---`, aliases,
  tags, unicode, 400-character lines), path safety including symlinked directories, atomic-write
  failure modes (failed replace, transient and permanent Windows-style locks, no temp litter),
  id allocation cases isolated one rule at a time, and byte-level checks that edits change only the
  intended lines (comments, quoting, CRLF, BOM preserved; unusual shapes refused);
- service: creation rules for humans and for Claude, server-controlled fields, quota, references,
  all supersession guards, idempotent completion, a simulated crash between the two steps, task
  transitions, 8 threads and 3 processes creating at once, a held lock (timeout, then recovery),
  failed writes consuming no id, and injection-style content that tries to smuggle in fields;
- CLI: every command, bad input creating nothing, audit events with the `cli` actor and no content,
  a warning (not a block) when the audit log is unwritable, and proof that nothing is committed to
  a git data repository.

19 deliberate breakages of the write path each make the suite fail. Two first survived because
other checks masked them (the supersession status guard; id allocation over a `_rejected` folder);
the tests were split so each rule is decided on its own, and the breakages are now caught. The first
Windows CI run then found a fourth, which Linux cannot show: files opened with `os.open` are in
text mode on Windows unless `O_BINARY` is passed, so every `\n` the writer wrote became `\r\n` (and
`\r\n` in a CRLF file became `\r\r\n`), after which the next edit could no longer parse the file
(14 failures). The writer and the audit log now open data files in binary mode, and a test
fixture simulates Windows text-mode descriptors so the regression is caught on Linux too
(removing the flag fails four tests). Writing
the suite also exposed three real defects, since fixed: a rejected decision could have been revived
by completing its supersession, the editor reported a block-style value with a generic parse error,
and accepting a Claude proposal did not require recording who accepted it.
