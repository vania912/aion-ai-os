"""Runtime settings. No machine-specific defaults: the data directory must be supplied."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from aios.errors import ConfigError

ENV_DATA_DIR = "AIOS_DATA_DIR"
ENV_WORKTREES_DIR = "AIOS_WORKTREES_DIR"
REGISTRY_FILENAME = "registry.yaml"


@dataclass(frozen=True)
class Settings:
    """Resolved settings. ``data_dir`` holds the registry and memory (kept outside this repo)."""

    data_dir: Path
    #: Where plan worktrees are created (default: a sibling of each project, see PHASE4-PLAN D6).
    worktrees_dir: Path | None = None

    @property
    def registry_file(self) -> Path:
        return self.data_dir / REGISTRY_FILENAME


def resolve_settings(
    data_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Resolve settings from an explicit argument, then ``AIOS_DATA_DIR``.

    ``env`` is injectable so behaviour is deterministic in tests.
    """
    environ = os.environ if env is None else env
    chosen = data_dir if data_dir is not None else _from_env(environ)
    if chosen is None:
        raise ConfigError(f"no data directory configured: pass --data-dir or set {ENV_DATA_DIR}")
    resolved = chosen.expanduser()
    if not resolved.is_dir():
        raise ConfigError(f"data directory does not exist or is not a directory: {resolved}")
    worktrees = environ.get(ENV_WORKTREES_DIR, "").strip()
    if worktrees:
        wt_path = Path(worktrees).expanduser()
        if not wt_path.is_absolute():
            raise ConfigError(f"{ENV_WORKTREES_DIR} must be an absolute path")
        return Settings(data_dir=resolved, worktrees_dir=wt_path)
    return Settings(data_dir=resolved)


def _from_env(environ: Mapping[str, str]) -> Path | None:
    value = environ.get(ENV_DATA_DIR, "").strip()
    return Path(value) if value else None
