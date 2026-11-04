"""Read ``projects/<project>/state.yaml`` safely (bounded, no aliases, no symlinks)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from aios.adapters.memory_files import PROJECTS_DIRNAME, _NoAliasLoader
from aios.domain.state import MAX_STATE_BYTES, STATE_FILENAME, ProjectState
from aios.errors import MemoryStoreError

_PROJECT_DIR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")


def state_path(data_dir: Path, project: str) -> Path | None:
    """Where the state file would be, or None if the project id is not a plain directory name."""
    if not _PROJECT_DIR_RE.match(project):
        return None
    return data_dir / PROJECTS_DIRNAME / project / STATE_FILENAME


def load_state(data_dir: Path, project: str) -> ProjectState | None:
    """The parsed state, or None if there is no state file.

    Raises :class:`MemoryStoreError` with a user-facing reason if the file exists but cannot be
    used (a symlink, too large, not a mapping, invalid YAML, unknown or invalid fields).
    """
    path = state_path(data_dir, project)
    if path is None:
        return None
    try:
        if path.is_symlink():
            raise MemoryStoreError(f"{STATE_FILENAME} is a symbolic link; symlinks are not read")
        if not path.exists():
            return None
        if not path.is_file():
            raise MemoryStoreError(f"{STATE_FILENAME} is not a regular file")
        if path.stat().st_size > MAX_STATE_BYTES:
            raise MemoryStoreError(f"{STATE_FILENAME} is larger than {MAX_STATE_BYTES} bytes")
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise MemoryStoreError(f"{STATE_FILENAME} is not valid UTF-8") from None
    except OSError as exc:
        raise MemoryStoreError(f"cannot read {STATE_FILENAME}: {exc.strerror or exc}") from None
    try:
        data = yaml.load(raw.removeprefix("\ufeff"), Loader=_NoAliasLoader)
    except yaml.YAMLError as exc:
        raise MemoryStoreError(f"{STATE_FILENAME}: invalid YAML: {exc}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise MemoryStoreError(f"{STATE_FILENAME} must be a mapping with text keys")
    try:
        return ProjectState.model_validate(data)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<file>'}: {e['msg']}" for e in exc.errors()
        )
        raise MemoryStoreError(f"{STATE_FILENAME}: {detail}") from None
