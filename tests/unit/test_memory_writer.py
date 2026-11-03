from __future__ import annotations

import difflib
import itertools
import os
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aios.adapters.memory_files import parse_record, scan_memory
from aios.adapters.memory_writer import (
    UnsupportedEditError,
    allocate_id,
    create_record_file,
    edit_front_matter,
    record_path,
    render_record,
    replace_atomic,
)
from aios.domain.memory import MemoryRecord
from aios.errors import MemoryStoreError


def rec(**overrides: Any) -> MemoryRecord:
    base: dict[str, Any] = {
        "id": "DEC-0001", "type": "decision", "project": "pulse", "title": "Use SQLite",
        "status": "active", "date": "2026-10-01",
    }  # fmt: skip
    return MemoryRecord.model_validate({**base, **overrides})


def line_diff(old: str, new: str) -> tuple[list[str], list[str]]:
    """(removed lines, added lines) between two texts."""
    removed, added = [], []
    for line in difflib.ndiff(old.split("\n"), new.split("\n")):
        if line.startswith("- "):
            removed.append(line[2:])
        elif line.startswith("+ "):
            added.append(line[2:])
    return removed, added


def same(a: MemoryRecord, b: MemoryRecord) -> bool:
    return a.model_dump() == b.model_dump()


# -- rendering ----------------------------------------------------------------------------


def test_minimal_record_renders_exactly_and_omits_defaults() -> None:
    assert render_record(rec(body="Hello.")) == (
        "---\nid: DEC-0001\ntype: decision\nproject: pulse\ntitle: Use SQLite\nstatus: active\n"
        "date: 2026-10-01\n---\nHello.\n"
    )


def test_claude_records_carry_provenance() -> None:
    text = render_record(
        rec(origin="claude", reviewed=False, status="proposed", evidence=["commit abc"])
    )
    assert "origin: claude\nreviewed: false\n" in text and "evidence:\n- commit abc\n" in text


def test_full_record_round_trips() -> None:
    full = rec(
        made_by=["Raheel"], why=["a", "b"], alternatives_considered=["x"], assumptions=["ASM-0001"],
        constraints=["CON-0001"], supersedes="DEC-0000", reason_for_change="because",
        related_files=["src/a.py"], related_tasks=["TSK-0001"], related_decisions=["DEC-0002"],
        tags=["slack", "ops_1"], evidence=["commit 1e9d7eb7a7", "src/a.py:3"], origin="claude",
        reviewed=False, accepted_by="Vania", accepted_digest="f" * 64,
        body="Line one.\n\nLine two.",
    )  # fmt: skip
    assert same(parse_record(render_record(full)), full)


HOSTILE = [
    "plain", "---", "--- ", "...", "key: value", "# not a comment", "- item", "'single'",
    '"double"', "{braces}", "[brackets]", "&anchor", "*alias", "!tag", "%directive", "@at",
    "`tick`", "|", ">", "null", "true", "false", "yes", "no", "~", "1e3", "0x1F", "2026-10-01",
    "héllo 世界 🚀", "  leading", "trailing  ", "tab\there", "colon: in: middle", "x" * 400,
    "a\\b\\n", "it's", 'say "hi"', "? maybe", ": colon first", "=", "<<", chr(0x2028),
    chr(0xA0) + "nbsp",
]  # fmt: skip


def test_hostile_strings_survive_a_round_trip_in_every_text_field() -> None:
    for text in HOSTILE:
        try:
            r = rec(
                title=text.strip() or "t", why=[text], alternatives_considered=[text],
                made_by=[text], reason_for_change=text, evidence=[text.strip() or "e"],
                body=f"{text}\n---\n{text}\n\n...\n",
            )  # fmt: skip
        except ValidationError:
            continue  # the model itself refuses it (for example an empty evidence entry)
        rendered = render_record(r)
        front = rendered.split("\n---\n", 1)[0]
        assert "\n---" not in front.removeprefix("---"), text  # no stray front-matter delimiter
        back = parse_record(rendered)
        assert same(back, r), text


def test_fuzzed_combinations_round_trip() -> None:
    count = 0
    for a, b in itertools.product(HOSTILE[::3], repeat=2):
        try:
            r = rec(title=(a + b).strip() or "t", why=[a, b], body=a + "\n" + b)
        except ValidationError:
            continue
        assert same(parse_record(render_record(r)), r), (a, b)
        count += 1
    assert count > 100


def test_body_line_endings_are_normalised_for_a_stable_round_trip() -> None:
    r = rec(body="one\r\ntwo\r\n\r\nthree")
    assert parse_record(render_record(r)).body == "one\ntwo\n\nthree"


# -- paths and creation -------------------------------------------------------------------


def test_record_path_comes_only_from_validated_fields(tmp_path: Path) -> None:
    assert (
        record_path(tmp_path, rec())
        == tmp_path / "projects" / "pulse" / "decisions" / "DEC-0001.md"
    )
    for field, bad in (
        ("project", "../x"),
        ("project", "a/b"),
        ("id", "DEC-0001/../../x"),
        ("id", "../DEC-0001"),
    ):
        with pytest.raises(ValidationError):
            rec(**{field: bad})


@pytest.mark.skipif(os.name != "posix", reason="symlink creation needs privileges on Windows")
def test_symlinked_directories_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "data" / "projects").mkdir(parents=True)
    (tmp_path / "data" / "projects" / "pulse").symlink_to(outside)
    with pytest.raises(MemoryStoreError, match="symlinked"):
        create_record_file(tmp_path / "data", rec())
    assert list(outside.iterdir()) == []


def test_create_writes_a_parseable_file_and_refuses_to_overwrite(tmp_path: Path) -> None:
    path = create_record_file(tmp_path, rec(body="Body."))
    assert same(parse_record(path.read_text(encoding="utf-8")), rec(body="Body."))
    with pytest.raises(MemoryStoreError, match="refusing to overwrite"):
        create_record_file(tmp_path, rec(title="Other"))
    assert parse_record(path.read_text(encoding="utf-8")).title == "Use SQLite"
    assert [p.name for p in path.parent.iterdir()] == ["DEC-0001.md"]  # no temp left behind


def test_temp_files_are_ignored_by_the_scanner(tmp_path: Path) -> None:
    path = create_record_file(tmp_path, rec())
    (path.parent / ".DEC-0009.md.abcd1234.tmp").write_text("half written")
    scan = scan_memory(tmp_path)
    assert [r.record.id for r in scan.records] == ["DEC-0001"] and scan.issues == []


# -- id allocation ------------------------------------------------------------------------


def touch(data: Path, rel: str) -> None:
    path = data / "projects" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")


def test_allocate_id_on_an_empty_store(tmp_path: Path) -> None:
    assert allocate_id(tmp_path, "decision") == "DEC-0001"


@pytest.mark.parametrize(
    ("rel", "rtype", "expected"),
    [
        ("pulse/decisions/DEC-0003.md", "decision", "DEC-0004"),  # an existing record
        ("other/decisions/DEC-0007.md", "decision", "DEC-0008"),  # ids are global across projects
        ("pulse/_rejected/decisions/DEC-0009.md", "decision", "DEC-0010"),  # rejected ids stay used
        ("pulse/_rejected/tasks/TSK-0012.md", "task", "TSK-0013"),
        ("pulse/decisions/DEC-0011.md", "decision", "DEC-0012"),  # a file that would not parse
        ("pulse/decisions/DEC-10000.md", "decision", "DEC-10001"),  # more than four digits
        ("pulse/tasks/TSK-0020.md", "task", "TSK-0021"),
    ],
)
def test_allocate_id_goes_above_every_id_ever_used(
    tmp_path: Path, rel: str, rtype: str, expected: str
) -> None:
    touch(tmp_path, rel)
    assert allocate_id(tmp_path, rtype) == expected


@pytest.mark.parametrize(
    "rel",
    [
        "pulse/decisions/.DEC-0099.md.ab12.tmp",  # a leftover temp file
        "pulse/decisions/dec-0050.md",  # wrong case
        "pulse/decisions/TSK-0030.md",  # another prefix
        "pulse/decisions/DEC-0040.txt",  # another extension
        "pulse/decisions/XDEC-0060.md",  # prefix only as a suffix
    ],
)
def test_allocate_id_ignores_files_that_are_not_ids_of_that_type(tmp_path: Path, rel: str) -> None:
    touch(tmp_path, rel)
    assert allocate_id(tmp_path, "decision") == "DEC-0001"


def test_allocate_id_is_independent_per_prefix(tmp_path: Path) -> None:
    touch(tmp_path, "pulse/tasks/TSK-0020.md")
    assert allocate_id(tmp_path, "question") == "Q-0001"
    assert allocate_id(tmp_path, "note") == "NOTE-0001"


# -- atomic replacement -------------------------------------------------------------------


def test_a_failed_replace_leaves_no_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        replace_atomic(tmp_path / "x.md", b"data")
    assert list(tmp_path.iterdir()) == []


def test_a_failed_replace_keeps_the_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "x.md"
    target.write_bytes(b"old")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        replace_atomic(target, b"new")
    assert target.read_bytes() == b"old" and [p.name for p in tmp_path.iterdir()] == ["x.md"]


def test_windows_style_transient_locks_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real, calls = os.replace, {"n": 0}

    def flaky(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    replace_atomic(tmp_path / "x.md", b"ok")
    assert (tmp_path / "x.md").read_bytes() == b"ok" and calls["n"] == 3


def test_a_permanent_lock_gives_a_clear_error_and_no_litter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("in use")))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(MemoryStoreError, match="in use"):
        replace_atomic(tmp_path / "x.md", b"data")
    assert list(tmp_path.iterdir()) == []


# -- targeted edits -----------------------------------------------------------------------

SAMPLE = (
    "---\n# a comment at the top\nid: DEC-0001\ntype: decision\nproject: pulse\n"
    'title: "Use SQLite"   # keep this comment\nstatus: active  # current\ndate: 2026-10-01\n'
    "why:\n  - simple   # because\ntags: [storage]\n---\n\nBody here.\n\nMore body.\n"
)


def sample_file(tmp_path: Path, text: str = SAMPLE, **kw: Any) -> Path:
    path = tmp_path / "DEC-0001.md"
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def test_edit_changes_only_the_edited_lines_and_keeps_comments(tmp_path: Path) -> None:
    path = sample_file(tmp_path)
    after = edit_front_matter(path, {"status": "superseded", "superseded_by": "DEC-0002"})
    assert (after.status, after.superseded_by) == ("superseded", "DEC-0002")
    new = path.read_text(encoding="utf-8")
    removed, added = line_diff(SAMPLE, new)
    assert removed == ["status: active  # current"]
    assert added == ["status: superseded  # current", "superseded_by: DEC-0002"]
    for kept in (
        "# a comment at the top",
        'title: "Use SQLite"   # keep this comment',
        "  - simple   # because",
    ):
        assert kept in new.split("\n")
    assert new.endswith("---\n\nBody here.\n\nMore body.\n")  # body bytes intact


def test_quoted_existing_values_are_replaced(tmp_path: Path) -> None:
    for quote in ('"active"', "'active'"):
        path = sample_file(
            tmp_path, SAMPLE.replace("status: active  # current", f"status: {quote}")
        )
        edit_front_matter(path, {"status": "deprecated"})
        assert "status: deprecated" in path.read_text(encoding="utf-8")


def test_a_missing_key_is_inserted_at_the_end_of_the_front_matter(tmp_path: Path) -> None:
    path = sample_file(tmp_path)
    edit_front_matter(path, {"accepted_by": "Vania"})
    assert "tags: [storage]\naccepted_by: Vania\n---\n" in path.read_text(encoding="utf-8")


def test_bools_and_null_are_written_as_yaml(tmp_path: Path) -> None:
    text = SAMPLE.replace(
        "status: active", "status: active\nreviewed: false\nsuperseded_by: DEC-0009"
    )
    path = sample_file(tmp_path, text)
    edit_front_matter(path, {"reviewed": True, "superseded_by": None})
    content = path.read_text(encoding="utf-8")
    assert "reviewed: true" in content and "superseded_by: null" in content
    assert parse_record(content).superseded_by is None and parse_record(content).reviewed is True


def test_crlf_files_keep_their_line_endings(tmp_path: Path) -> None:
    path = sample_file(tmp_path, SAMPLE.replace("\n", "\r\n"))
    edit_front_matter(path, {"status": "deprecated"})
    data = path.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") and b"status: deprecated  # current\r\n" in data


def test_a_bom_is_preserved(tmp_path: Path) -> None:
    path = sample_file(tmp_path, b"\xef\xbb\xbf" + SAMPLE.encode())  # type: ignore[arg-type]
    edit_front_matter(path, {"status": "deprecated"})
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf---\n") and b"status: deprecated" in data
    assert data.count(b"\xef\xbb\xbf") == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda t: t.replace(
                "status: active  # current\n", "status: active\nstatus: proposed\n"
            ),
            "more than once",
        ),
        (
            lambda t: t.replace("status: active  # current", "status:\n  active"),
            "single-line scalar",
        ),
        (
            lambda t: t.replace("status: active  # current", "status: >\n  active"),
            "single-line scalar",
        ),
        (lambda t: t.replace("\n", "\r\n", 3), "mixes line endings"),
        (lambda t: t.replace("---\n", "", 1), "no front matter"),
    ],
)
def test_unusual_shapes_are_refused_and_nothing_is_written(
    tmp_path: Path, mutate: Any, message: str
) -> None:
    path = sample_file(tmp_path, mutate(SAMPLE))
    before = path.read_bytes()
    with pytest.raises(UnsupportedEditError, match=message):
        edit_front_matter(path, {"status": "deprecated"})
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == [path.name]


def test_invalid_utf8_and_non_whitelisted_keys_are_refused(tmp_path: Path) -> None:
    path = sample_file(tmp_path)
    for updates in ({"title": "New"}, {"why": "x"}, {"id": "DEC-0002"}, {"body": "x"}):
        with pytest.raises(UnsupportedEditError, match="cannot be edited"):
            edit_front_matter(path, updates)
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"---\nid: \xff\xfe\n---\n")
    with pytest.raises(UnsupportedEditError, match="UTF-8"):
        edit_front_matter(bad, {"status": "active"})
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_an_edit_that_would_make_the_record_invalid_writes_nothing(tmp_path: Path) -> None:
    path = sample_file(tmp_path)
    for updates in (
        {"status": "bogus"},
        {"superseded_by": "TSK-0001"},
        {"accepted_digest": "nope"},
    ):
        with pytest.raises(UnsupportedEditError, match="invalid"):
            edit_front_matter(path, updates)
    assert path.read_text(encoding="utf-8") == SAMPLE


def test_hostile_values_are_written_safely_and_round_trip(tmp_path: Path) -> None:
    names = [
        'He said "hi" \\ ok # not a comment', "colon: inside", "'quoted'", "héllo 世界", "- dash",
        "null", "true", "a # b", "{x}", "*alias", "&anchor", "!tag", "x" * 80,
    ]  # fmt: skip
    for name in names:
        path = sample_file(tmp_path)
        after = edit_front_matter(path, {"accepted_by": name})
        assert after.accepted_by == name, name
        content = path.read_text(encoding="utf-8")
        assert parse_record(content).accepted_by == name
        removed, added = line_diff(SAMPLE, content)
        assert removed == [] and len(added) == 1 and added[0].startswith("accepted_by: "), name


def test_repeated_edits_never_disturb_the_rest_of_the_file(tmp_path: Path) -> None:
    path = sample_file(tmp_path)
    combos = itertools.product(
        ("active", "deprecated", "superseded"), (None, "DEC-0002", "DEC-0010")
    )
    for status, by in combos:
        edit_front_matter(path, {"status": status, "superseded_by": by})
        current = path.read_text(encoding="utf-8")
        removed, added = line_diff(SAMPLE, current)
        assert all(line.startswith(("status:", "superseded_by:")) for line in removed + added)
        assert parse_record(current).status == status and parse_record(current).superseded_by == by
