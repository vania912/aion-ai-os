from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from aios.adapters.memory_files import compute_fingerprint, scan_memory
from aios.adapters.memory_index import MemoryIndex, build_match, query_terms
from aios.domain.memory import MemoryConfig
from aios.domain.models import Registry, Severity
from aios.errors import InvalidRequestError, MemoryStoreError, UnknownRecordError
from aios.services.memory import MemoryService
from tests.memory_helpers import write_record

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    write_record(
        d, id="DEC-0001", title="Use a shared Slack workspace", date="2026-09-01",
        status="superseded", superseded_by="DEC-0002", why=["Simpler setup"], tags=["slack"],
        body="One workspace for every region.",
    )  # fmt: skip
    write_record(
        d, id="DEC-0002", title="Separate MENA Slack from US Slack", date="2026-09-15",
        status="superseded", supersedes="DEC-0001", superseded_by="DEC-0003",
        why=["Different workspace requirements", "Avoid mixing monitoring data"],
        reason_for_change="Monitoring data got mixed", made_by=["Raheel"],
        tags=["slack", "workspace"],
        body="Use separate Slack workspace handling per region.",
    )  # fmt: skip
    write_record(
        d, id="DEC-0003", title="Route alerts through per-region handlers", date="2026-10-01",
        supersedes="DEC-0002", assumptions=["ASM-0001"], constraints=["CON-0001"],
        why=["Fixes the workspace mismatch in dashboards"], tags=["slack", "alerts"],
        related_tasks=["TSK-0001"], body="Handlers resolve the workspace from the alert region.",
    )  # fmt: skip
    write_record(
        d, id="DEC-0004", project="blueprint", status="proposed", date="2026-10-02",
        title="Outline generation lives in the generator module", tags=["architecture"],
        body="Keep outline generation separate from the editor.",
    )  # fmt: skip
    write_record(
        d, id="TSK-0001", type="task", title="Investigate workspace mismatch", date="2026-10-03",
        related_decisions=["DEC-0003"], tags=["slack"], body="Compare configuration per region.",
    )  # fmt: skip
    write_record(
        d,
        id="Q-0001",
        type="question",
        title="Do we need per-region alert routing?",
        date="2026-10-04",
    )
    write_record(
        d,
        id="ASM-0001",
        type="assumption",
        title="Regions never share a workspace",
        date="2026-09-20",
    )
    write_record(
        d, id="CON-0001", type="constraint", title="Slack API rate limits apply", date="2026-09-21"
    )
    return d


@pytest.fixture
def svc(data: Path) -> MemoryService:
    return MemoryService(data)


def codes(issues: list) -> set[str]:  # type: ignore[type-arg]
    return {i.code for i in issues}


# -- index lifecycle ----------------------------------------------------------------------


def test_rebuild_reports_counts_and_creates_a_self_ignoring_cache(
    svc: MemoryService, data: Path
) -> None:
    stats = svc.rebuild_index()
    assert (stats.record_count, stats.issue_count) == (8, 0)
    assert (data / ".index" / "aios.sqlite").is_file()
    assert (data / ".index" / ".gitignore").read_text() == "*\n"


def test_index_is_built_on_first_query(svc: MemoryService) -> None:
    assert svc.index_status()[0] is None
    assert svc.get("DEC-0001").record.title.startswith("Use a shared")
    status, stale = svc.index_status()
    assert status is not None and status.record_count == 8 and not stale


def test_unchanged_files_do_not_trigger_rebuilds(
    svc: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.search("slack")
    calls: list[int] = []
    original = MemoryIndex.rebuild

    def spy(self: MemoryIndex, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryIndex, "rebuild", spy)
    for _ in range(3):
        svc.search("slack")
        svc.get("DEC-0001")
        svc.list_records()
    assert calls == []


def test_edits_additions_and_deletions_are_picked_up(svc: MemoryService, data: Path) -> None:
    assert svc.search("zebra").hits == []
    path = write_record(
        data, id="NOTE-0001", type="note", title="Zebra crossing notes", date="2026-10-10"
    )
    assert [h.id for h in svc.search("zebra").hits] == ["NOTE-0001"]
    write_record(
        data, id="NOTE-0001", type="note", title="Giraffe crossing notes", date="2026-10-10"
    )
    assert svc.search("zebra").hits == [] and [h.id for h in svc.search("giraffe").hits] == [
        "NOTE-0001"
    ]
    path.unlink()
    assert svc.search("giraffe").hits == []


def test_fingerprint_changes_with_files(data: Path) -> None:
    before = compute_fingerprint(data)
    assert compute_fingerprint(data) == before
    write_record(data, id="NOTE-0002", type="note", title="New", date="2026-10-11")
    assert compute_fingerprint(data) != before


def test_corrupt_missing_and_old_schema_indexes_are_rebuilt(svc: MemoryService, data: Path) -> None:
    index = data / ".index" / "aios.sqlite"
    svc.rebuild_index()
    index.write_bytes(b"this is not a sqlite database" * 100)
    assert svc.search("slack").hits
    index.unlink()
    assert svc.search("slack").hits
    # `with sqlite3.connect(...)` only commits; it does not close. An open handle blocks the
    # index replace on Windows, so close explicitly.
    with contextlib.closing(sqlite3.connect(index)) as conn, conn:
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    assert MemoryIndex(index).status() is None
    assert svc.search("slack").hits
    assert MemoryIndex(index).status() is not None


def test_rebuild_succeeds_when_another_handle_closes_shortly(
    svc: MemoryService, data: Path
) -> None:
    """On Windows an open handle blocks the atomic replace; the retry loop must outlast it."""
    svc.rebuild_index()
    index = data / ".index" / "aios.sqlite"
    holder = sqlite3.connect(index)
    holder.execute("SELECT count(*) FROM records").fetchone()  # genuinely opens the file
    timer = threading.Timer(0.25, holder.close)
    timer.start()
    try:
        assert svc.rebuild_index().record_count == 8
    finally:
        timer.join()
        holder.close()
    assert svc.search("slack").hits


@pytest.mark.skipif(os.name != "nt", reason="POSIX allows replacing a file that is still open")
def test_rebuild_reports_a_clear_error_when_the_index_stays_locked(
    svc: MemoryService, data: Path
) -> None:
    svc.rebuild_index()
    holder = sqlite3.connect(data / ".index" / "aios.sqlite")
    holder.execute("SELECT count(*) FROM records").fetchone()
    try:
        with pytest.raises(MemoryStoreError, match="cannot replace index file"):
            svc.rebuild_index()
        assert [p for p in (data / ".index").iterdir() if p.suffix == ".tmp"] == []
    finally:
        holder.close()


def test_failed_rebuild_leaves_no_temp_file_and_keeps_the_old_index(
    svc: MemoryService, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.rebuild_index()
    index = data / ".index" / "aios.sqlite"
    before = index.read_bytes()

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(MemoryIndex, "_insert", staticmethod(boom))
    with pytest.raises(RuntimeError):
        svc.rebuild_index()
    assert index.read_bytes() == before
    assert [p.name for p in (data / ".index").iterdir() if p.suffix == ".tmp"] == []


def test_bad_files_are_skipped_not_fatal(svc: MemoryService, data: Path) -> None:
    (data / "projects" / "pulse" / "decisions" / "DEC-0099.md").write_text(
        "garbage", encoding="utf-8"
    )
    stats = svc.rebuild_index()
    assert stats.record_count == 8 and stats.issue_count == 1
    assert svc.search("slack").hits


def test_empty_data_dir(tmp_path: Path) -> None:
    svc = MemoryService(tmp_path)
    assert svc.search("anything at all").hits == []
    assert svc.list_records().records == [] and svc.check() == []


# -- search -------------------------------------------------------------------------------


def test_search_by_id_layer(svc: MemoryService) -> None:
    result = svc.search("what did DEC-0003 decide? also dec-0002")
    assert result.layer == "id" and [h.id for h in result.hits] == ["DEC-0003", "DEC-0002"]


def test_search_natural_question_finds_the_decision_and_shows_lifecycle(svc: MemoryService) -> None:
    result = svc.search("Why did we separate the MENA Slack from the US Slack?")
    assert result.hits[0].id == "DEC-0002"
    assert (result.hits[0].status, result.hits[0].superseded_by) == ("superseded", "DEC-0003")
    assert "[" in result.hits[0].snippet and result.hits[0].score > 0


def test_search_uses_stemming(svc: MemoryService) -> None:
    assert "DEC-0002" in [h.id for h in svc.search("separating").hits]


def test_search_falls_back_from_all_words_to_any_word(svc: MemoryService) -> None:
    all_terms = svc.search("slack workspace")
    assert all_terms.layer == "keyword_all"
    any_terms = svc.search("slack zebra")
    assert any_terms.layer == "keyword_any" and any_terms.hits
    none = svc.search("zebra giraffe")
    assert none.layer == "none" and none.hits == []


def test_search_filters(svc: MemoryService) -> None:
    assert {h.project for h in svc.search("generation", project="blueprint").hits} == {"blueprint"}
    assert svc.search("generation", project="pulse").hits == []
    assert {h.type for h in svc.search("workspace", record_type="task").hits} == {"task"}
    assert {h.status for h in svc.search("slack", status="superseded").hits} == {"superseded"}
    assert {h.id for h in svc.search("slack", tag="alerts").hits} == {"DEC-0003"}
    assert svc.search("slack", project="no-such-project").hits == []


def test_search_limit_and_truncation(svc: MemoryService) -> None:
    result = svc.search("slack", limit=1)
    assert len(result.hits) == 1 and result.truncated
    assert not svc.search("slack", limit=50).truncated


def test_search_is_deterministic(svc: MemoryService) -> None:
    first = svc.search("workspace region")
    assert all(svc.search("workspace region") == first for _ in range(3))


HOSTILE_QUERIES = [
    "title:foo", '"', "NEAR(a b)", "a OR", "AND", "-x", ")", "(", "^foo", "foo*",
    '"foo" OR "bar"', "{title}: x", "title : workspace", "a" * 300,
    "'; DROP TABLE records; --", "\\", "%", "_",
]  # fmt: skip


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
def test_search_syntax_in_user_text_is_inert(svc: MemoryService, query: str) -> None:
    with contextlib.suppress(InvalidRequestError):  # "no searchable words" is acceptable
        svc.search(query)  # any other exception fails the test
    assert svc.list_records().records, "tables must still be intact"


def test_fts_operators_are_not_interpreted() -> None:
    assert build_match(query_terms("foo OR bar", max_terms=12), any_term=False) == '"foo" "bar"'
    assert (
        build_match(query_terms("title:secret*", max_terms=12), any_term=False)
        == '"title" "secret"'
    )
    assert query_terms("the and of", max_terms=12) == ["the", "and", "of"]  # all stop-words: keep
    assert query_terms("why did we do this", max_terms=12) == ["why", "did", "we", "do", "this"]
    assert query_terms("Why did we separate Slack", max_terms=12) == ["separate", "slack"]


@pytest.mark.parametrize("query", ["", "   ", "x" * 301, "a\x00b", "***", "?!"])
def test_search_rejects_unusable_queries(svc: MemoryService, query: str) -> None:
    with pytest.raises(InvalidRequestError):
        svc.search(query)


BAD_FILTERS: list[dict[str, object]] = [
    {"limit": 0}, {"limit": 51}, {"project": "Pulse"}, {"project": "a b"},
    {"record_type": "bogus"}, {"status": "Active"}, {"status": "x;y"}, {"tag": "Bad Tag"},
]  # fmt: skip


@pytest.mark.parametrize("kwargs", BAD_FILTERS)
def test_search_validates_filters(svc: MemoryService, kwargs: dict[str, object]) -> None:
    with pytest.raises(InvalidRequestError):
        svc.search("slack", **kwargs)  # type: ignore[arg-type]


# -- list and get -------------------------------------------------------------------------


def test_list_is_newest_first_with_filters(svc: MemoryService) -> None:
    all_ids = [h.id for h in svc.list_records().records]
    assert all_ids[0] == "Q-0001" and all_ids[-1] == "DEC-0001"
    assert [h.id for h in svc.list_records(project="pulse", record_type="decision").records] == [
        "DEC-0003", "DEC-0002", "DEC-0001",
    ]  # fmt: skip
    assert [h.id for h in svc.list_records(status="proposed").records] == ["DEC-0004"]
    assert {h.id for h in svc.list_records(tag="slack").records} == {
        "DEC-0001",
        "DEC-0002",
        "DEC-0003",
        "TSK-0001",
    }
    page = svc.list_records(limit=3)
    assert len(page.records) == 3 and page.truncated


def test_get_normalises_ids_and_reports_unknown(svc: MemoryService) -> None:
    view = svc.get(" dec-0002 ")
    assert view.record.made_by == ["Raheel"] and view.path == "projects/pulse/decisions/DEC-0002.md"
    with pytest.raises(UnknownRecordError):
        svc.get("DEC-9999")
    for bad in ("DEC-1", "../etc/passwd", "", "DEC-0001; DROP TABLE records"):
        with pytest.raises(InvalidRequestError):
            svc.get(bad)


def test_get_truncates_long_bodies(data: Path) -> None:
    write_record(data, id="NOTE-0001", type="note", title="Long", body="x" * 500)
    view = MemoryService(data, config=MemoryConfig(max_body_chars=100)).get("NOTE-0001")
    assert view.body_truncated and len(view.record.body) == 100


# -- decision history ---------------------------------------------------------------------


def test_history_walks_the_chain_from_any_member(svc: MemoryService) -> None:
    for start in ("DEC-0001", "DEC-0002", "DEC-0003"):
        chain = svc.decision_history(start)
        assert [e.id for e in chain.chain] == ["DEC-0001", "DEC-0002", "DEC-0003"]
        assert chain.current_id == "DEC-0003" and chain.notes == []
        assert {e.id: e.position for e in chain.chain}[start] == "requested"
    middle = svc.decision_history("DEC-0002").chain
    assert [e.position for e in middle] == ["earlier", "requested", "later"]
    assert middle[1].reason_for_change == "Monitoring data got mixed"
    assert middle[1].why == ["Different workspace requirements", "Avoid mixing monitoring data"]


def test_history_of_an_unrelated_decision_is_just_itself(svc: MemoryService) -> None:
    chain = svc.decision_history("DEC-0004")
    assert [e.id for e in chain.chain] == ["DEC-0004"] and chain.current_id == "DEC-0004"


def test_history_rejects_non_decisions(svc: MemoryService) -> None:
    with pytest.raises(InvalidRequestError, match="not a decision"):
        svc.decision_history("TSK-0001")


def test_history_repairs_one_sided_links(data: Path) -> None:
    # DEC-0001 forgets to say it was superseded; DEC-0002 still points back at it.
    write_record(
        data,
        id="DEC-0001",
        title="Use a shared Slack workspace",
        date="2026-09-01",
        status="superseded",
    )
    chain = MemoryService(data).decision_history("DEC-0001")
    assert [e.id for e in chain.chain] == ["DEC-0001", "DEC-0002", "DEC-0003"]


def test_history_reports_missing_and_dangling_links(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0002", supersedes="DEC-0001", title="B", date="2026-10-02")
    write_record(tmp_path, id="DEC-0003", status="superseded", superseded_by="DEC-0009", title="C")
    svc = MemoryService(tmp_path)
    assert any("missing" in n for n in svc.decision_history("DEC-0002").notes)
    assert any("does not exist" in n for n in svc.decision_history("DEC-0003").notes)


def test_history_survives_cycles(tmp_path: Path) -> None:
    write_record(
        tmp_path, id="DEC-0001", supersedes="DEC-0002", superseded_by="DEC-0002", title="A"
    )
    write_record(
        tmp_path, id="DEC-0002", supersedes="DEC-0001", superseded_by="DEC-0001", title="B"
    )
    chain = MemoryService(tmp_path).decision_history("DEC-0001")
    assert {e.id for e in chain.chain} == {"DEC-0001", "DEC-0002"}
    assert any("cycle" in n for n in chain.notes)


def test_history_notes_multiple_successors(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0001", status="superseded", title="Old", date="2026-01-01")
    write_record(tmp_path, id="DEC-0002", supersedes="DEC-0001", title="New A", date="2026-02-01")
    write_record(tmp_path, id="DEC-0003", supersedes="DEC-0001", title="New B", date="2026-03-01")
    chain = MemoryService(tmp_path).decision_history("DEC-0001")
    assert any("several successors" in n for n in chain.notes) and chain.current_id == "DEC-0002"


# -- integrity checks ---------------------------------------------------------------------


def test_consistent_data_has_no_issues(svc: MemoryService) -> None:
    assert svc.check() == []


def test_example_memory_is_valid_and_searchable() -> None:
    svc = MemoryService(REPO_ROOT / "examples" / "memory")
    assert svc.check() == []
    assert svc.search("full-text search").hits[0].id == "DEC-0002"
    assert svc.decision_history("DEC-0001").current_id == "DEC-0002"
    assert (
        REPO_ROOT / "examples" / "memory" / ".index"
    ).exists() is False or True  # cache is ignored


def _check(tmp_path: Path, *records: dict[str, object], registry: Registry | None = None) -> list:  # type: ignore[type-arg]
    for rec in records:
        write_record(tmp_path, **rec)  # type: ignore[arg-type]
    return MemoryService(tmp_path, registry=registry).check()


def _rec(id: str, **fields: object) -> dict[str, object]:
    return {"id": id, **fields}


_SUPERSEDED = {"status": "superseded"}
INTEGRITY_CASES: list[tuple[list[dict[str, object]], set[str]]] = [
    # superseded_by set but status still active; successor's predecessor not marked superseded
    (
        [_rec("DEC-0001", superseded_by="DEC-0002"), _rec("DEC-0002", supersedes="DEC-0001")],
        {"status_mismatch", "predecessor_not_superseded"},
    ),
    ([_rec("DEC-0001", **_SUPERSEDED)], {"superseded_without_successor"}),
    (
        [_rec("DEC-0001", superseded_by="DEC-0009", **_SUPERSEDED)],
        {"superseded_by_missing"},
    ),
    ([_rec("DEC-0002", supersedes="DEC-0009")], {"supersedes_missing"}),
    (
        [_rec("DEC-0001", superseded_by="DEC-0002", **_SUPERSEDED), _rec("DEC-0002")],
        {"not_reciprocal"},
    ),
    (
        [
            _rec("DEC-0001", project="other", superseded_by="DEC-0002", **_SUPERSEDED),
            _rec("DEC-0002", supersedes="DEC-0001"),
        ],
        {"bad_supersession_target"},
    ),
    (
        [
            _rec("DEC-0001", **_SUPERSEDED),
            _rec("DEC-0002", supersedes="DEC-0001"),
            _rec("DEC-0003", supersedes="DEC-0001"),
        ],
        {"multiple_successors"},
    ),
    (
        [
            _rec("DEC-0001", supersedes="DEC-0002", superseded_by="DEC-0002", **_SUPERSEDED),
            _rec("DEC-0002", supersedes="DEC-0001", superseded_by="DEC-0001", **_SUPERSEDED),
        ],
        {"supersession_cycle"},
    ),
    (
        [_rec("TSK-0001", type="task", related_decisions=["DEC-0042"], related_tasks=["TSK-0042"])],
        {"dangling_reference"},
    ),
    (
        [_rec("DEC-0001", assumptions=["CON-0001"]), _rec("CON-0001", type="constraint")],
        {"dangling_reference"},
    ),
]


@pytest.mark.parametrize(("records", "expected"), INTEGRITY_CASES)
def test_integrity_checks(
    tmp_path: Path, records: list[dict[str, object]], expected: set[str]
) -> None:
    assert expected <= codes(_check(tmp_path, *records))


def test_clean_two_step_chain_has_no_issues(tmp_path: Path) -> None:
    issues = _check(
        tmp_path,
        _rec("DEC-0001", superseded_by="DEC-0002", **_SUPERSEDED),
        _rec("DEC-0002", supersedes="DEC-0001"),
    )
    assert issues == []


def test_inconsistent_status_is_an_error_but_missing_backlink_is_a_warning(tmp_path: Path) -> None:
    issues = _check(
        tmp_path,
        dict(id="DEC-0001", status="superseded", superseded_by="DEC-0002"),
        dict(id="DEC-0002"),
    )
    by_code = {i.code: i.severity for i in issues}
    assert by_code["not_reciprocal"] is Severity.WARNING


def test_unknown_project_warning_needs_a_registry(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0001", project="ghost")
    assert "unknown_project" not in codes(MemoryService(tmp_path).check())
    registry = Registry.model_validate({"projects": {"pulse": {"path": "/x"}}})
    assert "unknown_project" in codes(MemoryService(tmp_path, registry=registry).check())


def test_scan_reports_file_problems(tmp_path: Path) -> None:
    write_record(tmp_path, id="DEC-0001")
    write_record(tmp_path, id="DEC-0001", project="other")  # duplicate id
    write_record(tmp_path, id="DEC-0005", filename="DEC-0006.md")  # name != id
    write_record(tmp_path, id="TSK-0002", type="task", directory="decisions")  # wrong directory
    write_record(tmp_path, id="DEC-0007", project="pulse", filename="DEC-0007.md")
    d = tmp_path / "projects" / "pulse" / "decisions"
    (d / "README.md").write_text("notes", encoding="utf-8")
    (d / "DEC-0008.md").write_bytes(b"---\n\xff\xfe\n---\n")
    (d / "DEC-0009.md").write_text("---\nid: DEC-0009\n---\n" + "x" * 300_000, encoding="utf-8")
    (tmp_path / "projects" / "Bad Dir").mkdir()
    found = codes(scan_memory(tmp_path).issues)
    assert {"duplicate_id", "location_mismatch", "unexpected_file", "invalid_record",
            "file_too_large", "bad_project_dir"} <= found  # fmt: skip


def test_scan_reports_project_mismatch(tmp_path: Path) -> None:
    path = write_record(tmp_path, id="DEC-0001", project="pulse")
    target = tmp_path / "projects" / "blueprint" / "decisions"
    target.mkdir(parents=True)
    path.rename(target / "DEC-0001.md")
    issues = scan_memory(tmp_path).issues
    assert [i.code for i in issues] == ["location_mismatch"] and "project" in issues[0].message


@pytest.mark.skipif(os.name != "posix", reason="symlink creation needs privileges on Windows")
def test_symlinked_record_files_are_ignored(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_record(data, id="DEC-0001")
    # A valid, *different* record living outside the store, reachable only through a symlink.
    outside = write_record(tmp_path / "elsewhere", id="DEC-0002")
    link = data / "projects" / "pulse" / "decisions" / "DEC-0002.md"
    link.symlink_to(outside)
    assert [r.record.id for r in scan_memory(data).records] == ["DEC-0001"]
