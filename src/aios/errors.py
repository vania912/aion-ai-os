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
