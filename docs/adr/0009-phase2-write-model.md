# ADR-0009: Phase 2 write model

- **Status:** Accepted for D1–D4 (user choices, 2026-11-03); D5–D7 **assumed** (defaults proposed in
  docs/PHASE2-PLAN.md, no objection raised). Phase 2 implementation has not started.
- **Date:** 2026-11-03

## Decisions

| # | Topic | Choice | Alternatives offered | Trade-off |
|---|---|---|---|---|
| D1 | How Claude's writes land | **In place**, flagged `reviewed: false`; Claude-created decisions get status `proposed`; excluded from "active" views until accepted | Separate inbox folder; no Claude writes in Phase 2 | Unreviewed content sits in the same store and index (labelled, excluded from active views); no second storage path |
| D2 | Who can accept | **Human CLI only** (`aios review accept`) plus a Claude Code deny rule on that command; no MCP accept tool | MCP accept tool with an `ask` rule; both | A deny rule matches by pattern and, per the Claude Code docs as read, does not catch the same program run by another path or inside `sh -c`; a determined model could still run the CLI. The unreviewed label and exclusion limit the damage. Revisit if the TTY spike (S9b) or testing shows a stronger layer is needed |
| D3 | Commits in the data repo | **Never auto-commit** | Auto-commit CLI actions only; auto-commit everything | History comes only from the user's own commits |
| D4 | Claude Code prompts for propose tools | **Silent** (`allow`); review later in the CLI | `ask` rule on every proposal | Relies on the unreviewed label and the per-project quota instead of an interrupting prompt |
| D5 (assumed) | Writing on by default | **Off** unless the MCP server is started with `--allow-propose` | On by default | One more flag in the registration command |
| D6 (assumed) | Editing existing record files | **Targeted single-line edits**, re-validated after each edit | Round-trip YAML library; re-serialise with PyYAML | Unusual front-matter shapes are refused with a message; comments in hand-written files are preserved |
| D7 (assumed) | Claude changing task status | **Allowed**, audited | Only via proposal | A wrong status from injected text is possible but reversible |

## Consequences
- A per-project quota of unreviewed records (default 20, my estimate) and provenance fields
  (`origin`, `evidence`) are part of the design.
- Phase 2 tools are `PROPOSE`-level and enabled only behind the flag (D5).

## Revisit triggers
- The TTY result (S9b), or any finding that the deny rule can be bypassed in practice.
- Review queue growing faster than it is reviewed.
