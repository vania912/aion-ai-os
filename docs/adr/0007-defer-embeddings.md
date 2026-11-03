# ADR-0007: No embeddings or vector database until retrieval failures are measured

- **Status:** Proposed (pending Phase 0 approval)
- **Date:** 2026-11-03

## Decision
Retrieval is layered: metadata, keyword, git search. Semantic search is a Phase 5 option behind a Retriever interface.

## Reason
User prefers starting simple. Expected scale (hundreds to low thousands of records, an assumption) does not obviously need it.

## Alternatives considered
- Embeddings from day one.
- Local vector store.

## Consequences / trade-offs
Paraphrased questions may miss relevant records until tags and consistent 'why' fields compensate. Reconsider if measured misses justify it.
