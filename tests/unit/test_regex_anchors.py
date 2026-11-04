"""Python's ``$`` also matches before a trailing newline, so ``re.match(r"^x$", "x\\n")`` succeeds.
Validators must end with ``\\Z`` instead (found while testing the git write runner)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aios.adapters.git_exec import BRANCH_RE, SHA_RE
from aios.domain.memory import ID_RE
from aios.domain.plans import PLAN_ID_RE

SRC = Path(__file__).resolve().parents[2] / "src"
COMPILE = re.compile(r"re\.compile\(\s*r?f?\"((?:[^\"\\]|\\.)*)\"")


def test_no_validator_regex_ends_with_a_bare_dollar() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        for pattern in COMPILE.findall(path.read_text(encoding="utf-8")):
            if pattern.endswith("$") and not pattern.endswith("\\$"):
                offenders.append(f"{path.name}: {pattern}")
    assert offenders == []


@pytest.mark.parametrize(
    ("pattern", "good"),
    [
        (BRANCH_RE, "aios/PLAN-0001"),
        (SHA_RE, "a" * 40),
        (ID_RE, "DEC-0001"),
        (PLAN_ID_RE, "PLAN-0001"),
    ],
)
def test_a_trailing_newline_is_not_accepted(pattern: re.Pattern[str], good: str) -> None:
    assert pattern.match(good)
    assert pattern.match(good + "\n") is None
