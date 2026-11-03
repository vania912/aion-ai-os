from __future__ import annotations

import pytest
from pydantic import ValidationError

from aios.domain.models import ProjectConfig, Registry
from aios.errors import UnknownProjectError


@pytest.mark.parametrize("path", ["D:/KC/Pulse", "D:\\KC\\Pulse", "/srv/pulse"])
def test_absolute_paths_accepted_on_any_os(path: str) -> None:
    assert ProjectConfig(path=path).git is True


@pytest.mark.parametrize("path", ["relative/dir", "./here", "pulse"])
def test_relative_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        ProjectConfig(path=path)


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({"path": "/x", "gti": True})


@pytest.mark.parametrize("bad_id", ["Pulse", "has space", "-lead", "", "a/b"])
def test_invalid_project_id_rejected(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        Registry.model_validate({"projects": {bad_id: {"path": "/x"}}})


def test_get_unknown_project_lists_registered() -> None:
    registry = Registry.model_validate({"projects": {"pulse": {"path": "/x"}}})
    with pytest.raises(UnknownProjectError, match="registered: pulse"):
        registry.get("nope")
