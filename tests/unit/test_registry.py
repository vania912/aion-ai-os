from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aios.domain.models import Severity
from aios.errors import RegistryError
from aios.services.registry import check_registry, load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, text: str) -> Path:
    file = tmp_path / "registry.yaml"
    file.write_text(text, encoding="utf-8")
    return file


def test_example_registry_parses() -> None:
    registry = load_registry(REPO_ROOT / "examples" / "registry.yaml")
    assert sorted(registry.projects) == ["blueprint", "pulse"]


def test_empty_file_is_an_empty_registry(tmp_path: Path) -> None:
    assert load_registry(_write(tmp_path, "")).projects == {}


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="cannot read"):
        load_registry(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="invalid YAML"):
        load_registry(_write(tmp_path, "projects: [unclosed"))


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="mapping"):
        load_registry(_write(tmp_path, "- a\n- b\n"))


def test_validation_error_names_the_field(tmp_path: Path) -> None:
    text = "projects:\n  pulse:\n    path: relative/dir\n"
    with pytest.raises(RegistryError, match=r"projects\.pulse\.path"):
        load_registry(_write(tmp_path, text))


def test_check_reports_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    registry = load_registry(
        _write(tmp_path, f"projects:\n  pulse:\n    path: '{missing.as_posix()}'\n")
    )
    [issue] = check_registry(registry)
    assert (issue.project_id, issue.severity, issue.code) == (
        "pulse",
        Severity.ERROR,
        "path_missing",
    )


def test_check_requires_git_when_declared(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    registry = load_registry(
        _write(tmp_path, f"projects:\n  pulse:\n    path: '{plain.as_posix()}'\n")
    )
    [issue] = check_registry(registry)
    assert issue.code == "not_a_git_repo"


def test_check_passes_for_real_repo_and_git_false(
    tmp_path: Path, make_git_repo: Callable[[str], Path]
) -> None:
    repo = make_git_repo("repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    text = (
        f"projects:\n  a:\n    path: '{repo.as_posix()}'\n"
        f"  b:\n    path: '{plain.as_posix()}'\n    git: false\n"
    )
    assert check_registry(load_registry(_write(tmp_path, text))) == []


def test_check_warns_on_duplicate_path(
    tmp_path: Path, make_git_repo: Callable[[str], Path]
) -> None:
    repo = make_git_repo("repo")
    text = f"projects:\n  a:\n    path: '{repo.as_posix()}'\n  b:\n    path: '{repo.as_posix()}'\n"
    [issue] = check_registry(load_registry(_write(tmp_path, text)))
    assert (issue.project_id, issue.severity, issue.code) == (
        "b",
        Severity.WARNING,
        "duplicate_path",
    )
