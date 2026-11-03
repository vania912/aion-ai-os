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


def _project(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> Path:
    repo = make_git_repo("repo")
    (repo / "app.py").write_text("print('hi')\nneedle here\n")
    (repo / ".env").write_text("needle=1\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _registry(data_dir, f"projects:\n  pulse:\n    path: '{repo.as_posix()}'\n")
    return data_dir


def test_files_list_read_search(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> None:
    data_dir = _project(tmp_path, make_git_repo)
    base = ["--data-dir", str(data_dir)]

    listing = runner.invoke(app, ["files", "list", "pulse", *base])
    assert listing.exit_code == 0 and "f\tapp.py\t" in listing.output
    sub = runner.invoke(app, ["files", "list", "pulse", ".", *base])  # positional path
    assert sub.exit_code == 0 and "app.py" in sub.output
    assert ".env" not in listing.output and ".git" not in listing.output

    read = runner.invoke(app, ["files", "read", "pulse", "app.py", "--start", "2", *base])
    assert read.exit_code == 0 and "needle here" in read.output

    found = runner.invoke(app, ["files", "search", "pulse", "needle", *base])
    assert found.exit_code == 0
    assert "app.py:2:needle here" in found.output and ".env" not in found.output


def test_files_commands_report_policy_errors(
    tmp_path: Path, make_git_repo: Callable[[str], Path]
) -> None:
    data_dir = _project(tmp_path, make_git_repo)
    base = ["--data-dir", str(data_dir)]
    denied = runner.invoke(app, ["files", "read", "pulse", ".env", *base])
    assert denied.exit_code == 2 and "denied_name" in denied.output
    escape = runner.invoke(app, ["files", "list", "pulse", "../", *base])
    assert escape.exit_code == 2 and "path_escape" in escape.output
    unknown = runner.invoke(app, ["files", "list", "nope", *base])
    assert unknown.exit_code == 2 and "unknown project" in unknown.output


def test_git_commands(tmp_path: Path) -> None:
    from tests.git_helpers import make_history

    history = make_history(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _registry(data_dir, f"projects:\n  pulse:\n    path: '{history.repo.as_posix()}'\n")
    base = ["--data-dir", str(data_dir)]

    recent = runner.invoke(app, ["git", "recent", "pulse", "--limit", "5", *base])
    assert recent.exit_code == 0 and "only secret" in recent.output
    assert "withheld by policy" in recent.output and ".env" not in recent.output

    show = runner.invoke(app, ["git", "show", "pulse", history.c2, *base])
    assert show.exit_code == 0 and "app_marker v2" in show.output
    assert "hunter2" not in show.output and ".env" not in show.output

    diff = runner.invoke(app, ["git", "diff", "pulse", history.c1, history.c3, *base])
    assert diff.exit_code == 0 and "hello workspace" in diff.output

    found = runner.invoke(app, ["git", "search", "pulse", "hunter2", "--mode", "content", *base])
    assert found.exit_code == 0 and found.output.strip() == ""

    status = runner.invoke(app, ["git", "status", "pulse", *base])
    assert (
        status.exit_code == 0 and "branch: main" in status.output and "clean: True" in status.output
    )

    branches = runner.invoke(app, ["git", "branches", "pulse", *base])
    assert branches.exit_code == 0 and "* main" in branches.output

    bad = runner.invoke(app, ["git", "show", "pulse", "HEAD:.env", *base])
    assert bad.exit_code == 2 and "ref must be" in bad.output
    denied = runner.invoke(app, ["git", "recent", "pulse", "--path", ".env", *base])
    assert denied.exit_code == 2 and "denied_name" in denied.output
