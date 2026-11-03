"""Review state (origin / reviewed) on the read side: index, search, snapshot, history, MCP."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aios.adapters.memory_index import SCHEMA_VERSION, MemoryIndex
from aios.domain.models import ProjectConfig
from aios.services.context import build_context, render_context
from aios.services.memory import MemoryService
from tests.mcp_helpers import World, call, make_world, run
from tests.memory_helpers import write_record


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    write_record(
        d, id="DEC-0001", title="Use a relational store", date="2026-09-01", why=["simple"]
    )
    write_record(
        d, id="DEC-0002", title="Use SQLite full text search", status="proposed", date="2026-10-01",
        origin="claude", reviewed=False, supersedes="DEC-0001", evidence=["commit abc1234"],
        why=["ranking"], tags=["search"],
    )  # fmt: skip
    write_record(d, id="TSK-0001", type="task", title="Measure misses", date="2026-10-02")
    write_record(
        d, id="TSK-0002", type="task", title="Investigate workspace mismatch", date="2026-10-03",
        origin="claude", reviewed=False,
    )  # fmt: skip
    write_record(
        d, id="Q-0001", type="question", title="Is the quota right?", date="2026-10-04",
        origin="claude", reviewed=False,
    )  # fmt: skip
    write_record(
        d, id="CON-0001", type="constraint", title="No external services", date="2026-09-02"
    )
    return d


@pytest.fixture
def mem(data: Path) -> MemoryService:
    return MemoryService(data)


# -- index ----------------------------------------------------------------------------------


def test_the_index_schema_is_version_2_and_old_indexes_rebuild_themselves(
    mem: MemoryService, data: Path
) -> None:
    assert SCHEMA_VERSION == 2
    mem.rebuild_index()
    index = data / ".index" / "aios.sqlite"
    with sqlite3.connect(index) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.close()
    assert MemoryIndex(index).status() is None  # an old index is not usable...
    hits = mem.search("quota").hits  # ...so the next query rebuilds it, transparently
    assert [h.id for h in hits] == ["Q-0001"] and (hits[0].origin, hits[0].reviewed) == (
        "claude",
        False,
    )


# -- search and list ------------------------------------------------------------------------


def test_hits_carry_origin_and_review_state(mem: MemoryService) -> None:
    by_id = {h.id: h for h in mem.list_records().records}
    assert (by_id["DEC-0001"].origin, by_id["DEC-0001"].reviewed) == ("human", True)
    assert (by_id["DEC-0002"].origin, by_id["DEC-0002"].reviewed) == ("claude", False)
    assert by_id["DEC-0002"].status == "proposed"


def test_reviewed_filter_in_list_and_search(mem: MemoryService) -> None:
    assert {h.id for h in mem.list_records(reviewed=False).records} == {
        "DEC-0002",
        "TSK-0002",
        "Q-0001",
    }
    assert {h.id for h in mem.list_records(reviewed=True).records} == {
        "DEC-0001",
        "TSK-0001",
        "CON-0001",
    }
    assert len(mem.list_records().records) == 6
    assert [h.id for h in mem.search("workspace", reviewed=False).hits] == ["TSK-0002"]
    assert mem.search("workspace", reviewed=True).hits == []
    assert [h.id for h in mem.search("DEC-0002", reviewed=False).hits] == ["DEC-0002"]  # id layer
    assert (
        mem.search("DEC-0002", reviewed=True).hits == []
        or mem.search("DEC-0002", reviewed=True).layer != "id"
    )


# -- snapshot -------------------------------------------------------------------------------


def test_unreviewed_records_never_appear_as_established_in_the_snapshot(
    mem: MemoryService, tmp_path: Path
) -> None:
    project = ProjectConfig(path=tmp_path, description="d", git=False)  # absolute on every OS
    ctx = build_context("pulse", project, git=None, memory=mem)
    assert [h.id for h in ctx.active_decisions] == ["DEC-0001"]
    assert ctx.proposed_decisions == []  # the only proposal is unreviewed
    assert [h.id for h in ctx.open_tasks] == ["TSK-0001"]
    assert ctx.open_questions == [] and [h.id for h in ctx.constraints] == ["CON-0001"]
    assert {h.id for h in ctx.awaiting_review} == {"DEC-0002", "TSK-0002", "Q-0001"}
    text = render_context(ctx)
    established, _, waiting = text.partition("## Awaiting review")
    for rid in ("DEC-0002", "TSK-0002", "Q-0001"):
        assert rid not in established and rid in waiting
    assert "UNVERIFIED" in text and "[proposed, unreviewed, claude]" in waiting
    assert "DEC-0001 [active]" in established


def test_a_human_proposal_is_listed_as_proposed_not_as_awaiting_review(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0001", status="proposed", title="Hand-written proposal")
    project = ProjectConfig(path=tmp_path, git=False)  # absolute on every OS
    ctx = build_context("pulse", project, git=None, memory=MemoryService(tmp_path))
    assert [h.id for h in ctx.proposed_decisions] == ["DEC-0001"] and ctx.awaiting_review == []


# -- decision history -------------------------------------------------------------------------


def test_a_proposed_successor_is_pending_not_current(mem: MemoryService) -> None:
    for start in ("DEC-0001", "DEC-0002"):
        chain = mem.decision_history(start)
        assert [e.id for e in chain.chain] == ["DEC-0001", "DEC-0002"]
        assert chain.current_id == "DEC-0001" and chain.pending_id == "DEC-0002"
        assert any("awaiting review" in n and "still DEC-0001" in n for n in chain.notes)
        assert [e.reviewed for e in chain.chain] == [True, False]


def test_an_accepted_successor_becomes_current(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0001", status="superseded", superseded_by="DEC-0002")
    write_record(tmp_path, id="DEC-0002", supersedes="DEC-0001", title="B")
    chain = MemoryService(tmp_path).decision_history("DEC-0001")
    assert chain.current_id == "DEC-0002" and chain.pending_id is None and chain.notes == []


def test_a_chain_with_nothing_in_force_falls_back_to_the_newest(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0001", status="superseded", superseded_by="DEC-0002")
    write_record(
        tmp_path, id="DEC-0002", supersedes="DEC-0001", status="proposed",
        origin="claude", reviewed=False,
    )  # fmt: skip
    chain = MemoryService(tmp_path).decision_history("DEC-0002")
    assert chain.current_id == "DEC-0002"


# -- MCP ------------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = make_world(tmp_path)
    write_record(
        w.data, id="DEC-0003", project="pulse", title="Route alerts via a queue",
        status="proposed", origin="claude", reviewed=False, supersedes="DEC-0002",
        evidence=["commit abc"], why=["scale"], date="2026-10-05",
    )  # fmt: skip
    return w


def test_mcp_search_shows_review_state_only_when_it_is_not_the_trusted_default(
    world: World,
) -> None:
    hits = json.loads(call(world, "search_memory", query="queue"))["hits"]
    assert (
        hits[0]["id"] == "DEC-0003"
        and hits[0]["reviewed"] is False
        and hits[0]["origin"] == "claude"
    )
    trusted = json.loads(call(world, "search_memory", query="separate MENA Slack"))["hits"][0]
    assert trusted["id"] == "DEC-0002" and "reviewed" not in trusted and "origin" not in trusted
    assert "path" not in hits[0] and "score" not in hits[0]


def test_mcp_reviewed_filter(world: World) -> None:
    only_unreviewed = json.loads(call(world, "list_memory", reviewed=False))["records"]
    assert [r["id"] for r in only_unreviewed] == ["DEC-0003"]
    assert all(r["reviewed"] is False for r in only_unreviewed)
    established = json.loads(call(world, "list_memory", reviewed=True))["records"]
    assert "DEC-0003" not in {r["id"] for r in established} and all(
        "reviewed" not in r for r in established
    )
    assert json.loads(call(world, "search_memory", query="queue", reviewed=True))["hits"] == []


def test_mcp_history_reports_the_pending_successor_and_the_decision_in_force(world: World) -> None:
    chain = json.loads(call(world, "get_decision_history", record_id="DEC-0002"))
    assert chain["current_id"] == "DEC-0002" and chain["pending_id"] == "DEC-0003"
    by_id = {e["id"]: e for e in chain["chain"]}
    assert by_id["DEC-0003"]["reviewed"] is False and by_id["DEC-0003"]["status"] == "proposed"
    assert "reviewed" not in by_id["DEC-0002"]
    assert any("awaiting review" in n for n in chain["notes"])


def test_mcp_snapshot_resources_and_record_view_mark_unreviewed_records(world: World) -> None:
    context = call(world, "get_project_context", project="pulse")
    assert (
        "## Awaiting review (UNVERIFIED" in context
        and "DEC-0003 [proposed, unreviewed, claude]" in context
    )
    assert context.index("DEC-0003") > context.index("## Awaiting review")
    decisions = "".join(
        c.content for c in run(world.server.read_resource("project://pulse/decisions"))
    )
    assert (
        "DEC-0003 [proposed, UNREVIEWED (claude)]" in decisions and "DEC-0002 [active]" in decisions
    )
    record = call(world, "get_memory_record", record_id="DEC-0003")
    assert (
        '"reviewed":false' in record.splitlines()[0]
        and '"origin":"claude"' in record.splitlines()[0]
    )


def test_mcp_instructions_and_descriptions_warn_about_unreviewed_records(world: World) -> None:
    assert "reviewed:false" in (world.server.instructions or "")
    descriptions = {t.name: t.description or "" for t in run(world.server.list_tools())}
    assert (
        "UNVERIFIED" in descriptions["search_memory"]
        and "unverified" in descriptions["list_memory"]
    )
    assert "pending_id" in descriptions["get_decision_history"]
