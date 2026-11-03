# ADR-0004: Memory and registry live outside the public code repository

- **Status:** Accepted by user in brainstorm (architecture still pending approval)
- **Date:** 2026-11-03

## Decision
Code lives in the public aion-ai-os repo. The project registry and memory data live in a separate private repo or local folder, located via AIOS_DATA_DIR.

## Reason
aion-ai-os is public. Registry paths and decision logs about company projects must not be published.

## Alternatives considered
- Make aion-ai-os private and keep everything together.
- Local data only, unversioned.

## Consequences / trade-offs
Two locations to manage. Private data repo needs its own backup and access control.
