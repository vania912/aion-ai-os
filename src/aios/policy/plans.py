"""Risk tier of a plan, computed from its files. A requested tier can raise it, never lower it.

R1  no file changes (analysis, memory-only)
R2  creates or modifies ordinary files
R3  deletes, touches CI/deploy/dependency/secret-like paths, or changes many files

The path rules are a heuristic: they flag the common risky places, they do not prove a change is
safe. The approver still reads the whole plan.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence

from aios.domain.plans import PlanFile, Tier

_ORDER: dict[str, int] = {"R1": 1, "R2": 2, "R3": 3}
MANY_FILES = 20

_RISKY_DIRS = (
    ".git", ".github", ".gitlab", ".circleci", ".claude", ".ssh", ".aws", ".husky",
    "deploy", "infra", "terraform", "k8s", "helm", "secrets",
)  # fmt: skip
_RISKY_NAMES = (
    ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "*credentials*",
    "dockerfile*", "docker-compose*", "*.tf", "jenkinsfile", "package.json",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml", "requirements*.txt",
    "poetry.lock", "uv.lock", "pipfile*", "go.mod", "go.sum", "cargo.toml", "cargo.lock",
    "gemfile*", "pom.xml", "build.gradle*", "*.sh", "makefile",
)  # fmt: skip
_SPLIT = re.compile(r"[\\/]+")


def _risky(path: str) -> str | None:
    parts = [p.casefold() for p in _SPLIT.split(path) if p]
    if any(p in _RISKY_DIRS for p in parts[:-1]):
        return "CI/deploy/secret-like directory"
    name = parts[-1] if parts else ""
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _RISKY_NAMES):
        return "dependency, build, deploy or secret-like file"
    return None


def classify(files: Sequence[PlanFile], requested: Tier | None = None) -> tuple[Tier, list[str]]:
    """The effective tier and the reasons for it."""
    reasons: list[str] = []
    tier: Tier = "R1"
    if files:
        tier = "R2"
        reasons.append("changes project files")
    for f in files:
        if f.action == "delete":
            tier = "R3"
            reasons.append(f"deletes {f.path}")
        why = _risky(f.path)
        if why:
            tier = "R3"
            reasons.append(f"{f.path}: {why}")
    if len(files) > MANY_FILES:
        tier = "R3"
        reasons.append(f"{len(files)} files (more than {MANY_FILES})")
    if requested is not None and _ORDER[requested] > _ORDER[tier]:
        tier = requested
        reasons.append(f"raised to {requested} by the plan author")
    return tier, reasons
