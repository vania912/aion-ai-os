from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aios.interfaces.cli.app import app
from tests.memory_helpers import write_record

runner = CliRunner()


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    write_record(
        d, id="DEC-0001", title="Use a shared Slack workspace", date="2026-09-01",
        status="superseded", superseded_by="DEC-0002", why=["Simpler setup"],
    )  # fmt: skip
    write_record(
        d, id="DEC-0002", title="Separate MENA Slack from US Slack", date="2026-09-15",
        supersedes="DEC-0001", why=["Avoid mixing monitoring data"],
        reason_for_change="Monitoring data got mixed", made_by=["Raheel"], tags=["slack"],
        body="Use separate Slack workspace handling.",
    )  # fmt: skip
    write_record(
        d, id="TSK-0001", type="task", title="Investigate workspace mismatch", date="2026-10-03"
    )
    return d


def run(data: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--data-dir", str(data)])
    return result.exit_code, result.output


def test_index_commands(data: Path) -> None:
    code, out = run(data, "index", "status")
    assert code == 0 and "no usable index" in out
    code, out = run(data, "index", "rebuild")
    assert code == 0 and "indexed 3 record(s); 0 issue(s) skipped" in out
    code, out = run(data, "index", "status")
    assert code == 0 and "records: 3" in out and "stale: False" in out
    write_record(data, id="NOTE-0001", type="note", title="Another note")
    assert "stale: True" in run(data, "index", "status")[1]


def test_memory_search_shows_lifecycle_and_layer(data: Path) -> None:
    code, out = run(data, "memory", "search", "separate slack")
    assert code == 0 and "DEC-0002\tactive\tpulse\tSeparate MENA Slack from US Slack" in out
    assert "[layer=keyword_all]" in out
    code, out = run(data, "memory", "search", "slack", "--status", "superseded")
    result_ids = [line.split("\t")[0] for line in out.splitlines() if line.startswith("DEC-")]
    assert "DEC-0001\tsuperseded -> DEC-0002" in out and result_ids == ["DEC-0001"]
    code, out = run(data, "memory", "search", "what is DEC-0001")
    assert "[layer=id]" in out and out.startswith("DEC-0001")


def test_memory_list_and_filters(data: Path) -> None:
    code, out = run(data, "memory", "list", "--type", "decision", "--project", "pulse")
    ids = [line.split("\t")[0] for line in out.splitlines() if line.startswith("DEC-")]
    assert code == 0 and ids == ["DEC-0002", "DEC-0001"]
    assert run(data, "memory", "list", "--type", "task")[1].startswith("TSK-0001")


def test_show_prints_reasons_and_lifecycle(data: Path) -> None:
    code, out = run(data, "memory", "show", "dec-0002")
    assert code == 0
    for expected in ("DEC-0002  [active]", "made by: Raheel", "why: Avoid mixing monitoring data",
                     "supersedes: DEC-0001", "reason for change: Monitoring data got mixed",
                     "Use separate Slack workspace handling."):  # fmt: skip
        assert expected in out
    assert run(data, "decision", "show", "DEC-0002")[1] == out


def test_decision_list_and_history(data: Path) -> None:
    code, out = run(data, "decision", "list", "pulse")
    listed = [line.split("\t")[0] for line in out.splitlines()]
    assert code == 0 and listed == ["DEC-0002", "DEC-0001"]
    assert (
        "DEC-0001\tsuperseded -> DEC-0002"
        in run(data, "decision", "list", "pulse", "--status", "superseded")[1]
    )
    code, out = run(data, "decision", "history", "DEC-0001")
    assert code == 0
    assert "> DEC-0001 [superseded]" in out and "  DEC-0002 [active]" in out
    assert "replaced the previous decision because: Monitoring data got mixed" in out
    assert "current: DEC-0002" in out


@pytest.mark.parametrize(
    "args",
    [("memory", "show", "DEC-9999"), ("memory", "show", "nope"), ("memory", "search", "  "),
     ("memory", "search", "x", "--type", "bogus"), ("decision", "history", "TSK-0001"),
     ("decision", "list", "Bad Project")],
)  # fmt: skip
def test_errors_are_reported_with_exit_code_2(data: Path, args: tuple[str, ...]) -> None:
    code, out = run(data, *args)
    assert code == 2 and "error:" in out


def test_memory_commands_do_not_need_a_registry(data: Path) -> None:
    assert not (data / "registry.yaml").exists()
    assert run(data, "memory", "list")[0] == 0


def test_doctor_checks_memory_too(tmp_path: Path, make_git_repo: Callable[[str], Path]) -> None:
    data = tmp_path / "data"
    data.mkdir()
    repo = make_git_repo("repo")
    (data / "registry.yaml").write_text(f"projects:\n  pulse:\n    path: '{repo.as_posix()}'\n")
    write_record(data, id="DEC-0001")
    write_record(data, id="DEC-0002", project="ghost")

    code, out = run(data, "doctor")
    assert code == 0 and "unknown_project" in out and "ghost" in out  # a warning only

    write_record(data, id="DEC-0003", superseded_by="DEC-0001")  # active but has superseded_by
    code, out = run(data, "doctor")
    assert code == 1 and "status_mismatch" in out
    code, out = run(data, "memory", "check")
    assert code == 1 and "status_mismatch" in out
