"""Exception hierarchy. Interfaces (CLI, MCP) translate these into user-facing messages."""

from __future__ import annotations


class AiosError(Exception):
    """Base class for all expected, user-reportable errors."""


class ConfigError(AiosError):
    """Invalid or missing configuration (for example, no data directory)."""


class RegistryError(AiosError):
    """The project registry file cannot be read or is invalid."""


class UnknownProjectError(AiosError):
    """A project id is not present in the registry."""


class PolicyViolation(AiosError):
    """An operation or path was refused by the policy engine.

    ``code`` is a stable, machine-readable reason (for example ``path_escape``). The message
    contains only the caller-supplied path, never resolved absolute paths or link targets.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
