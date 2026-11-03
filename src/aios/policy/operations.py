"""Operation-level authorization: default deny, only enabled permission levels may run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermissionLevel(StrEnum):
    """Permission levels from the architecture (docs/ARCHITECTURE.md, section E.2)."""

    READ_ONLY = "read_only"  # no state change anywhere
    PROPOSE = "propose"  # writes only a draft/proposed record in the memory store
    APPROVAL_REQUIRED = "approval_required"  # needs a recorded approval
    WRITE = "write"  # mutates a project repo or remote


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    operation: str
    level: PermissionLevel | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


class OperationPolicy:
    """Maps operation names to permission levels and decides whether they may run.

    Default deny: an unregistered operation, or one whose level is not enabled, is refused.
    Phase 1 enables only ``READ_ONLY``; enabling further levels is an explicit configuration
    change made in the phase that introduces them.
    """

    def __init__(self, enabled: frozenset[PermissionLevel] | None = None) -> None:
        self._enabled = enabled if enabled is not None else frozenset({PermissionLevel.READ_ONLY})
        self._levels: dict[str, PermissionLevel] = {}

    def register(self, operation: str, level: PermissionLevel) -> None:
        if operation in self._levels:
            raise ValueError(f"operation already registered: {operation}")
        self._levels[operation] = level

    def authorize(self, operation: str) -> Decision:
        level = self._levels.get(operation)
        if level is None:
            return Decision(Verdict.DENY, operation, None, "operation is not registered")
        if level not in self._enabled:
            return Decision(
                Verdict.DENY, operation, level, f"permission level {level.value!r} is not enabled"
            )
        return Decision(Verdict.ALLOW, operation, level, "allowed")
