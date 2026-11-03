from __future__ import annotations

import pytest

from aios.policy import OperationPolicy, PermissionLevel, Verdict


def test_unregistered_operation_is_denied() -> None:
    decision = OperationPolicy().authorize("read_project_file")
    assert decision.verdict is Verdict.DENY
    assert not decision.allowed
    assert decision.level is None


def test_read_only_allowed_by_default() -> None:
    policy = OperationPolicy()
    policy.register("read_project_file", PermissionLevel.READ_ONLY)
    assert policy.authorize("read_project_file").allowed


@pytest.mark.parametrize(
    "level", [PermissionLevel.PROPOSE, PermissionLevel.APPROVAL_REQUIRED, PermissionLevel.WRITE]
)
def test_non_read_levels_denied_until_enabled(level: PermissionLevel) -> None:
    policy = OperationPolicy()
    policy.register("op", level)
    decision = policy.authorize("op")
    assert not decision.allowed
    assert decision.level is level

    enabled = OperationPolicy(frozenset({PermissionLevel.READ_ONLY, level}))
    enabled.register("op", level)
    assert enabled.authorize("op").allowed


def test_duplicate_registration_rejected() -> None:
    policy = OperationPolicy()
    policy.register("op", PermissionLevel.READ_ONLY)
    with pytest.raises(ValueError, match="already registered"):
        policy.register("op", PermissionLevel.READ_ONLY)
