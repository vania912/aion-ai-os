"""Load the project registry and check it against the filesystem."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from aios.domain.models import Issue, Registry, Severity
from aios.errors import RegistryError


def load_registry(file: Path) -> Registry:
    """Parse and validate ``registry.yaml``. Does not touch the registered project paths.

    Loading stays pure so a registry for another machine (for example, Windows paths checked
    from Linux) still parses; use :func:`check_registry` to verify paths exist.
    """
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read registry file {file}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {file}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RegistryError(f"{file}: top level must be a mapping with a 'projects' key")
    try:
        return Registry.model_validate(raw)
    except ValidationError as exc:
        raise RegistryError(f"{file}: invalid registry:\n{_format_errors(exc)}") from exc


def check_registry(registry: Registry) -> list[Issue]:
    """Compare the registry with the filesystem. Returns issues; never raises for bad paths."""
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    for project_id, project in sorted(registry.projects.items()):
        path = project.path
        if not path.is_dir():
            issues.append(
                Issue(project_id, Severity.ERROR, "path_missing", f"not a directory: {path}")
            )
            continue
        if project.git and not (path / ".git").exists():
            issues.append(
                Issue(
                    project_id,
                    Severity.ERROR,
                    "not_a_git_repo",
                    f"git: true but no .git found in {path}",
                )
            )
        key = os.path.normcase(str(path.resolve()))
        if key in seen:
            issues.append(
                Issue(
                    project_id,
                    Severity.WARNING,
                    "duplicate_path",
                    f"same directory as project {seen[key]!r}",
                )
            )
        else:
            seen[key] = project_id
    return issues


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {location}: {err['msg']}")
    return "\n".join(lines)
