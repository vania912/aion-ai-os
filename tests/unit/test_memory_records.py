from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aios.adapters.memory_files import RecordParseError, parse_record
from aios.domain.memory import MemoryRecord

BASE = {
    "id": "DEC-0001",
    "type": "decision",
    "project": "pulse",
    "title": "Separate workspaces",
    "status": "active",
    "date": "2026-10-01",
}


def make(**overrides: object) -> MemoryRecord:
    return MemoryRecord.model_validate({**BASE, **overrides})


def test_valid_decision_with_all_fields() -> None:
    r = make(
        made_by=["A"],
        why=["x"],
        alternatives_considered=["y"],
        supersedes="DEC-0000" if False else None,
        tags=["slack", "ops_1"],
        related_files=["src/app.py"],
        related_tasks=["TSK-0001"],
        related_decisions=["DEC-0002"],
        assumptions=["ASM-0001"],
        constraints=["CON-0001"],
    )
    assert r.date.isoformat() == "2026-10-01" and r.superseded_by is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": "DEC-1"}, "must look like"),
        ({"id": "TSK-0001"}, "does not match type"),
        ({"id": "dec-0001"}, "must look like"),
        ({"status": "open"}, "not valid for decision"),
        ({"type": "task", "id": "TSK-0001", "status": "active"}, "not valid for task"),
        ({"date": "2026-10-XX"}, "valid date"),
        ({"title": ""}, "at least 1"),
        ({"title": "   "}, "blank"),
        ({"title": "x" * 201}, "at most 200"),
        ({"project": "Pulse"}, "pattern"),
        ({"tags": ["Has Space"]}, "lowercase"),
        ({"related_tasks": ["nope"]}, "not a record id"),
        ({"related_files": ["../secret"]}, "project-relative"),
        ({"related_files": ["/etc/passwd"]}, "project-relative"),
        ({"related_files": ["C:/x"]}, "project-relative"),
        ({"supersedes": "TSK-0001"}, "decision id"),
        ({"supersedes": "DEC-0001"}, "cannot supersede itself"),
        ({"superseded_by": "DEC-0001"}, "cannot supersede itself"),
        ({"unknown_field": 1}, "Extra inputs"),
    ],
)
def test_invalid_records_are_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make(**overrides)


@pytest.mark.parametrize(
    "field", ["why", "made_by", "alternatives_considered", "reason_for_change"]
)
def test_decision_only_fields_rejected_on_other_types(field: str) -> None:
    value: object = "x" if field == "reason_for_change" else ["x"]
    with pytest.raises(ValidationError, match="only valid on decisions"):
        make(type="task", id="TSK-0001", status="open", **{field: value})


def test_every_type_has_a_valid_default_status() -> None:
    for rtype, prefix, status in [
        ("assumption", "ASM", "unverified"),
        ("constraint", "CON", "active"),
        ("question", "Q", "open"),
        ("discovery", "DSC", "active"),
        ("task", "TSK", "in_progress"),
        ("note", "NOTE", "archived"),
    ]:
        assert make(type=rtype, id=f"{prefix}-0001", status=status).type == rtype


# -- parsing ------------------------------------------------------------------------------

GOOD = (
    "---\nid: DEC-0001\ntype: decision\nproject: pulse\ntitle: T\nstatus: active\n"
    "date: 2026-10-01\n---\nBody\n\nMore\n"
)


def test_parse_valid_file_keeps_body() -> None:
    record = parse_record(GOOD)
    assert record.title == "T" and record.body == "Body\n\nMore"


def test_parse_handles_bom_and_crlf() -> None:
    assert parse_record("\ufeff" + GOOD.replace("\n", "\r\n")).body == "Body\n\nMore"


def test_parse_accepts_closing_delimiter_at_eof() -> None:
    text = GOOD.split("Body")[0].rstrip("\n")
    assert parse_record(text).body == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("no front matter", "missing YAML front matter"),
        ("---\nid: DEC-0001\n", "missing YAML front matter"),
        ("---\n- a\n- b\n---\n", "must be a mapping"),
        ("---\n: : :\n  bad: [\n---\n", "invalid YAML"),
        ("---\n" + GOOD.split("---\n")[1] + "body: nope\n---\n", "reserved"),
        ("---\nid: DEC-0001\n---\n", "type"),
        ("---\n1: 2\n---\n", "text keys"),
    ],
)
def test_parse_errors_are_clear(text: str, message: str) -> None:
    with pytest.raises(RecordParseError, match=message):
        parse_record(text)


def test_parse_reports_every_validation_problem() -> None:
    bad = GOOD.replace("status: active", "status: bogus").replace("2026-10-01", "2026-10-XX")
    with pytest.raises(RecordParseError) as info:
        parse_record(bad)
    assert "status" in str(info.value) and "date" in str(info.value)


def test_yaml_is_parsed_safely(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    evil = GOOD.replace(
        "title: T", f"title: !!python/object/apply:os.mkdir ['{marker.as_posix()}']"
    )
    with pytest.raises(RecordParseError):
        parse_record(evil)
    assert not marker.exists(), "YAML constructors must not execute code"
