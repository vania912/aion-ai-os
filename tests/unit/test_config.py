from __future__ import annotations

from pathlib import Path

import pytest

from aios.config import ENV_DATA_DIR, resolve_settings
from aios.errors import ConfigError


def test_explicit_argument_wins_over_env(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    settings = resolve_settings(tmp_path, env={ENV_DATA_DIR: str(other)})
    assert settings.data_dir == tmp_path
    assert settings.registry_file == tmp_path / "registry.yaml"


def test_env_used_when_no_argument(tmp_path: Path) -> None:
    assert resolve_settings(env={ENV_DATA_DIR: str(tmp_path)}).data_dir == tmp_path


def test_missing_configuration_is_an_error() -> None:
    with pytest.raises(ConfigError, match="AIOS_DATA_DIR"):
        resolve_settings(env={})


def test_blank_env_is_treated_as_unset() -> None:
    with pytest.raises(ConfigError):
        resolve_settings(env={ENV_DATA_DIR: "   "})


def test_nonexistent_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_settings(tmp_path / "missing", env={})
