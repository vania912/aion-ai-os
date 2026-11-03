# Memory (read path)

Code: `src/aios/domain/memory.py`, `src/aios/adapters/memory_files.py`,
`src/aios/adapters/memory_index.py`, `src/aios/services/memory.py`.
Design background: ARCHITECTURE.md section D and ADR-0003.

**Scope today: read only.** Humans create and edit records by hand (an example is in
`examples/memory/`). Claude-initiated writes, `aios decision create`, project-state snapshots and
the "immutable after active" rule for decisions belong to Phase 2 and are **not enforced yet**.

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

## Decision lifecycle

`DEC-0001 (superseded) <-- supersedes -- DEC-0002 (active)`. Nothing is deleted. The reader
tools follow **both** pointers, so a chain with a one-sided link still resolves, and report
inconsistencies as notes. `aios decision history DEC-0001` prints the chain oldest first, with the
reasons and the `reason_for_change` of each replacement, and the current decision.

`aios doctor` / `aios memory check` flag: `status_mismatch`, `superseded_without_successor`,
`superseded_by_missing`, `supersedes_missing`, `not_reciprocal` (warning),
`predecessor_not_superseded` (warning), `bad_supersession_target`, `multiple_successors`,
`supersession_cycle`, `dangling_reference` (warning), `unknown_project` (warning, needs the registry),
plus file problems: `invalid_record`, `location_mismatch`, `duplicate_id`, `file_too_large`,
`unexpected_file` (warning), `bad_project_dir`. One bad file is skipped and reported; it never stops
the index or the other records.

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
- **Read path only** (see above). The immutability rule for active decisions is not enforced.
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

- 489 tests pass locally (Linux, Python 3.13). They cover record validation, parsing, scanning,
  index lifecycle (corrupt, missing, old-schema, stale), layered search, filter validation,
  hostile queries, listing, fetching, decision history (chains, one-sided links, cycles, missing
  and multiple successors), every integrity check, and the CLI.
- Manual mutation checks: 17 deliberate breakages (including FTS operators from user text,
  skipped freshness checks, ignored project filters, symlink following, unsafe YAML, alias
  expansion, a leftover temp file after a failed rebuild) each make the suite fail. **Three
  initially survived** because my tests were too weak (symlink and unsafe-YAML tests could not
  tell the difference; the first alias-bomb check did not exist). They were rewritten.
