# ADR-0004: Memory and registry live outside the public code repository

- **Status:** Accepted (architecture approved 2026-11-03)
- **Date:** 2026-11-03

## Decision
Code lives in the public aion-ai-os repo. The project registry and memory data live in a separate **local git repository with no remote** (chosen 2026-11-03), located via AIOS_DATA_DIR.

## Reason
aion-ai-os is public. Registry paths and decision logs about company projects must not be published.

## Alternatives considered
- Make aion-ai-os private and keep everything together.
- Local data only, unversioned.

## Consequences / trade-offs
Two locations to manage. With no remote there is no off-machine backup unless one is added later; loss of the machine means loss of memory history. Revisit if that risk matters.
