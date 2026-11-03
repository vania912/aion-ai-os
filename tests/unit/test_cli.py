from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from aios import __version__
from aios.config import ENV_DATA_DIR
from aios.interfaces.cli.app import app

runner = CliRunner()


def _registry(data_dir: Path, text: str) -> None:
    (data_dir / "registry.yaml").write_text(text, encoding="utf-8")


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_no_data_dir_is_a_clear_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(ENV_DATA_DIR, raising=False)
    result = runner.invoke(app, ["project", "list"])
    assert result.exit_code == 2
    assert "AIOS_DATA_DIR" in result.output


def test_project_list(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> None:
    repo = make_git_repo("repo")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _registry(
        data_dir, f"projects:\n  pulse:\n    path: '{repo.as_posix()}'\n    description: demo\n"
    )
    result = runner.invoke(app, ["project", "list", "--data-dir", str(data_dir)])
    assert result.exit_code == 0
    assert result.output.startswith("pulse\t")
    assert "demo" in result.output


def test_doctor_exit_codes(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> None:
    repo = make_git_repo("repo")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    _registry(data_dir, f"projects:\n  pulse:\n    path: '{repo.as_posix()}'\n")
    ok = runner.invoke(app, ["doctor", "--data-dir", str(data_dir)])
    assert ok.exit_code == 0
    assert "ok: 1 project(s)" in ok.output

    _registry(data_dir, f"projects:\n  pulse:\n    path: '{(tmp_path / 'gone').as_posix()}'\n")
    bad = runner.invoke(app, ["doctor", "--data-dir", str(data_dir)])
    assert bad.exit_code == 1
    assert "path_missing" in bad.output
