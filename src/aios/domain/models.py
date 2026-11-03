"""Core domain models: the project registry and registry check results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from aios.errors import UnknownProjectError

#: Lowercase slug used in URIs (``project://<id>``) and file paths.
ProjectId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class ProjectConfig(BaseModel):
    """One registered project. Adding a project means adding an entry, not changing code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    description: str = ""
    git: bool = True

    @field_validator("path", mode="before")
    @classmethod
    def _require_absolute(cls, value: object) -> object:
        # Accept Windows (``D:/KC/Pulse``) and POSIX absolute paths on any OS, so a registry
        # written for one machine still parses (and can be reported on) elsewhere.
        text = str(value)
        if not (PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()):
            raise ValueError(f"project path must be absolute, got {text!r}")
        return value


class Registry(BaseModel):
    """All registered projects, keyed by project id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projects: dict[ProjectId, ProjectConfig] = Field(default_factory=dict)

    def get(self, project_id: str) -> ProjectConfig:
        try:
            return self.projects[project_id]
        except KeyError:
            known = ", ".join(sorted(self.projects)) or "none"
            raise UnknownProjectError(
                f"unknown project {project_id!r} (registered: {known})"
            ) from None


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """A problem found while checking the registry against the filesystem."""

    project_id: str
    severity: Severity
    code: str
    message: str
