# ADR-0003: Markdown/YAML files as source of truth with a rebuildable SQLite FTS index

- **Status:** Accepted by user in brainstorm (architecture still pending approval)
- **Date:** 2026-11-03

## Decision
Decisions, assumptions, constraints, questions, discoveries, tasks, notes and project state are Markdown files with YAML front matter. A SQLite database (FTS5 plus metadata) is a derived index and can be rebuilt from the files. Tasks live in the same store.

## Reason
Reviewable, diffable history is central to the decision lifecycle and audit story. SQLite adds fast keyword and metadata queries without becoming a second source of truth.

## Alternatives considered
- SQLite only.
- SQLite plus embeddings.
- Dedicated vector database.

## Consequences / trade-offs
Two representations to keep in sync (mitigated: index is disposable, `aios index rebuild`). Paraphrase-heavy queries may miss; measure before adding embeddings (ADR-0007). FTS5 availability on the target Python build is unverified.
