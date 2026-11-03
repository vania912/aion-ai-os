"""Plans: content hash, tiers, the state machine, hash-bound approval, tampering, the CLI."""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from aios.adapters.plan_store import PlanStore
from aios.domain.models import Registry
from aios.domain.plans import PlanContent, PlanFile, StepReport, plan_hash
from aios.errors import InvalidRequestError, MemoryStoreError, UnknownRecordError
from aios.interfaces.cli.app import app
from aios.policy.plans import classify
from aios.services.plans import PlanConfig, PlanService
from tests.memory_helpers import write_record

TODAY = date(2026, 11, 3)


def content(**over: Any) -> PlanContent:
    base: dict[str, Any] = {
        "project": "pulse",
        "title": "Cache workspace lookups",
        "request": "Make lookups faster",
        "what_will_change": "Add an in-memory cache in util.py",
        "why": "Lookups dominate request time",
        "files": [{"path": "src/util.py", "action": "modify", "note": "add cache"}],
        "risks": ["stale entries"],
        "expected_result": "p95 lookup under 5 ms",
        "verification": ["run the unit tests", "time 1000 lookups"],
    }
    return PlanContent.model_validate({**base, **over})


@pytest.fixture
def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def svc(data: Path) -> PlanService:
    registry = Registry.model_validate(
        {"projects": {"pulse": {"path": str(data)}, "blueprint": {"path": str(data)}}}
    )
    return PlanService(data, registry=registry, today=TODAY)


def approved(svc: PlanService, **over: Any) -> str:
    view = svc.create(content(**over))
    return svc.approve(view.plan.id, by="Vania", plan_hash_seen=view.plan_hash).plan.id


# -- hash ---------------------------------------------------------------------------------------


def test_the_hash_covers_every_content_field_and_nothing_else() -> None:
    base = plan_hash(content())
    assert plan_hash(content()) == base and len(base) == 64
    changes: dict[str, Any] = {
        "project": "blueprint",
        "title": "Other",
        "request": "other",
        "what_will_change": "other",
        "why": "other",
        "files": [],
        "risks": ["other"],
        "expected_result": "other",
        "verification": ["other"],
        "requested_tier": "R3",
        "related_decisions": ["DEC-0001"],
        "related_tasks": ["TSK-0001"],
        "evidence": ["commit abc"],
        "revises": "PLAN-0009",
    }
    for field, value in changes.items():
        assert plan_hash(content(**{field: value})) != base, field
    # a file note or action also counts
    assert plan_hash(content(files=[{"path": "src/util.py", "action": "create"}])) != base


def test_the_hash_ignores_whitespace_noise_the_model_normalises() -> None:
    assert plan_hash(content(title="  Cache workspace lookups \n")) == plan_hash(content())
    assert plan_hash(content(why="Lookups dominate request time\r\n")) == plan_hash(content())


# -- tiers --------------------------------------------------------------------------------------


def f(path: str, action: str = "modify") -> PlanFile:
    return PlanFile.model_validate({"path": path, "action": action})


def test_tiers() -> None:
    assert classify([])[0] == "R1"
    assert classify([f("src/app.py")])[0] == "R2"
    assert classify([f("src/app.py", "create")])[0] == "R2"
    assert classify([f("src/app.py", "delete")])[0] == "R3"
    assert classify([f(f"src/m{i}.py") for i in range(21)])[0] == "R3"
    assert classify([f(f"src/m{i}.py") for i in range(20)])[0] == "R2"


@pytest.mark.parametrize(
    "path",
    [
        ".env", ".env.production", "config/.ENV.local", "keys/server.pem", "id_rsa",
        ".github/workflows/ci.yml", ".GITHUB/x.yml", ".git/hooks/pre-commit", ".claude/settings.json",
        "Dockerfile", "docker-compose.yml", "infra/main.tf", "package.json", "web/package-lock.json",
        "pyproject.toml", "requirements-dev.txt", "uv.lock", "deploy/run.sh", "scripts/release.sh",
        "src\\.github\\x.yml", "credentials.json",
    ],
)  # fmt: skip
def test_risky_paths_are_r3(path: str) -> None:
    tier, reasons = classify([f(path)])
    assert tier == "R3" and any(path in r for r in reasons)


def test_a_requested_tier_can_raise_but_never_lower() -> None:
    assert classify([f("src/app.py")], "R3")[0] == "R3"
    assert classify([f("src/app.py")], "R1")[0] == "R2"
    assert classify([f(".env")], "R1")[0] == "R3"
    assert classify([], "R2")[0] == "R2"


# -- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../x", "a/../../b", "C:/x", "a//b", "\\abs", "x\x00y", "", "a/./..", "d:x"],
)
def test_unsafe_file_paths_are_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        content(files=[{"path": path, "action": "modify"}])


@pytest.mark.parametrize(
    "over",
    [
        {"title": ""},
        {"title": "a\nb"},
        {"title": "x" * 201},
        {"risks": []},
        {"verification": []},
        {"risks": ["a\nb"]},
        {"verification": ["x" * 301]},
        {"what_will_change": "bad \x00 text"},
        {"files": [{"path": f"f{i}", "action": "modify"} for i in range(51)]},
        {"files": [{"path": "a", "action": "modify"}, {"path": "A", "action": "delete"}]},
        {"files": [{"path": "a", "action": "rewrite"}]},
        {"related_decisions": ["TSK-0001"]},
        {"related_tasks": ["DEC-0001"]},
        {"revises": "plan-1"},
        {"requested_tier": "R9"},
        {"unknown_field": "x"},
        {"project": "Bad Project"},
    ],
)
def test_invalid_content_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        content(**over)


# -- creating -----------------------------------------------------------------------------------


def test_create_assigns_server_side_fields_and_starts_proposed(
    svc: PlanService, data: Path
) -> None:
    view = svc.create(content())
    assert view.plan.id == "PLAN-0001" and view.status == "proposed" and view.tier == "R2"
    assert view.plan.origin == "human" and view.plan.created == TODAY
    assert (data / "plans" / "PLAN-0001.yaml").is_file()
    assert svc.create(content(title="Second")).plan.id == "PLAN-0002"
    assert [p.id for p in svc.list_plans()] == ["PLAN-0002", "PLAN-0001"]
    assert [p.id for p in svc.list_plans(status="proposed", limit=1)] == ["PLAN-0002"]


def test_ids_are_unique_under_concurrency_and_never_reused(svc: PlanService, data: Path) -> None:
    ids: list[str] = []
    errors: list[BaseException] = []

    def make(n: int) -> None:
        try:
            ids.append(svc.create(content(title=f"Plan {n}")).plan.id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=make, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(ids)) == 8
    (data / "plans" / "PLAN-0008.yaml").unlink()  # even a deleted plan's number is not reused
    # its events file never existed, so the highest remaining id is below it; reuse is only
    # prevented while any file for the id exists
    (data / "plans" / "PLAN-0009.events.jsonl").write_text("")
    assert svc.create(content(title="After")).plan.id == "PLAN-0010"


def test_references_are_checked(svc: PlanService, data: Path) -> None:
    write_record(data, id="DEC-0001", project="pulse", why=["x"])
    write_record(data, id="TSK-0001", type="task", project="pulse")
    write_record(data, id="DEC-0002", project="blueprint", why=["x"])
    ok = svc.create(content(related_decisions=["DEC-0001"], related_tasks=["TSK-0001"]))
    assert ok.plan.related_decisions == ["DEC-0001"]
    for over, text in (
        ({"related_decisions": ["DEC-0099"]}, "not an existing decision"),
        ({"related_decisions": ["DEC-0002"]}, "another project"),
        ({"related_tasks": ["TSK-0099"]}, "not an existing task"),
        ({"project": "nope"}, "unknown project"),
        ({"revises": "PLAN-0099"}, "does not exist"),
    ):
        with pytest.raises(Exception, match=text):
            svc.create(content(**over))


def test_revising_a_plan_supersedes_it(svc: PlanService) -> None:
    first = svc.create(content())
    second = svc.create(content(title="Better", revises=first.plan.id))
    assert svc.get("PLAN-0001").superseded_by == second.plan.id
    with pytest.raises(InvalidRequestError, match="replaced by PLAN-0002"):
        svc.approve("PLAN-0001", by="V", plan_hash_seen=first.plan_hash)
    with pytest.raises(InvalidRequestError, match="already revised"):
        svc.create(content(title="Third", revises="PLAN-0001"))
    with pytest.raises(InvalidRequestError, match=r"another project|same project"):
        svc.create(content(project="blueprint", title="x", revises="PLAN-0002"))
    assert [p.id for p in svc.list_plans(open_only=True)] == ["PLAN-0002"]
    # rejecting the revision frees the original again
    svc.reject("PLAN-0002", by="V", reason="no")
    assert svc.get("PLAN-0001").superseded_by is None


def test_claude_plans_are_proposed_limited_and_not_duplicated(data: Path) -> None:
    svc = PlanService(data, config=PlanConfig(max_awaiting_per_project=2), today=TODAY)
    a = svc.create(content(title="One"), origin="claude")
    assert a.plan.origin == "claude" and a.status == "proposed"
    with pytest.raises(InvalidRequestError, match="already has this title"):
        svc.create(content(title="  one "), origin="claude")
    svc.create(content(title="Two"), origin="claude")
    with pytest.raises(InvalidRequestError, match="awaiting approval"):
        svc.create(content(title="Three"), origin="claude")
    svc.create(content(title="Human one is not limited"), origin="human")
    svc.reject("PLAN-0001", by="V", reason="not now")
    svc.create(content(title="Three"), origin="claude")  # room again


# -- approval is bound to the content hash ------------------------------------------------------


def test_approval_records_who_tier_and_hash(svc: PlanService, data: Path) -> None:
    view = svc.create(content())
    done = svc.approve(view.plan.id, by="Vania", plan_hash_seen=view.plan_hash)
    assert done.status == "approved" and done.approval is not None
    assert (done.approval.by, done.approval.tier, done.approval.actor) == ("Vania", "R2", "cli")
    line = json.loads((data / "plans" / "PLAN-0001.events.jsonl").read_text().splitlines()[0])
    assert line["plan_hash"] == view.plan_hash and line["event"] == "approved" and line["seq"] == 1


def test_approving_content_that_changed_since_it_was_shown_is_refused(svc: PlanService) -> None:
    view = svc.create(content())
    with pytest.raises(InvalidRequestError, match="changed since"):
        svc.approve(view.plan.id, by="V", plan_hash_seen="0" * 64)
    assert svc.get(view.plan.id).status == "proposed"


def test_editing_an_approved_plan_cancels_the_approval(svc: PlanService, data: Path) -> None:
    pid = approved(svc)
    path = data / "plans" / f"{pid}.yaml"
    original = path.read_text(encoding="utf-8")
    for old, new in (
        ("Add an in-memory cache", "Add an in-memory cache and delete the logs"),
        ("stale entries", "nothing at all"),
        ("modify", "delete"),
        ("run the unit tests", "skip the tests"),
    ):
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        view = svc.get(pid)
        assert view.status == "proposed" and view.approval is None and view.approval_stale, old
    path.write_text(original, encoding="utf-8")  # restoring the content restores the approval
    assert svc.get(pid).status == "approved"


def test_a_stale_approval_can_be_replaced_by_a_new_one(svc: PlanService, data: Path) -> None:
    pid = approved(svc)
    path = data / "plans" / f"{pid}.yaml"
    path.write_text(path.read_text().replace("stale entries", "memory growth"), encoding="utf-8")
    view = svc.get(pid)
    assert view.approval_stale
    again = svc.approve(pid, by="Vania", plan_hash_seen=view.plan_hash)
    assert again.status == "approved" and not again.approval_stale


def test_double_approval_and_approval_after_rejection_are_refused(svc: PlanService) -> None:
    pid = approved(svc)
    with pytest.raises(InvalidRequestError, match="cannot approve"):
        svc.approve(pid, by="V", plan_hash_seen=svc.get(pid).plan_hash)
    other = svc.create(content(title="Other"))
    svc.reject(other.plan.id, by="V", reason="no")
    with pytest.raises(InvalidRequestError, match="rejected"):
        svc.approve(other.plan.id, by="V", plan_hash_seen=other.plan_hash)
    with pytest.raises(InvalidRequestError, match="reason"):
        svc.reject(pid, by="V", reason="  ")


def test_unknown_and_malformed_ids(svc: PlanService) -> None:
    with pytest.raises(UnknownRecordError):
        svc.get("PLAN-0042")
    for bad in ("../x", "PLAN-1", "plan", "DEC-0001", ""):
        with pytest.raises(InvalidRequestError):
            svc.get(bad)


# -- outcome and verification ---------------------------------------------------------------------


def passing(*steps: int) -> list[StepReport]:
    return [StepReport(step=n, outcome="pass", evidence="ok") for n in steps]


def test_the_full_lifecycle(svc: PlanService) -> None:
    pid = approved(svc)
    with pytest.raises(InvalidRequestError, match="can be verified"):
        svc.verify(pid, by="V")
    assert svc.record_outcome(pid, by="V", outcome="executed", note="done").status == "executed"
    with pytest.raises(InvalidRequestError, match="every step passed"):
        svc.verify(pid, by="V")
    partial = svc.record_report(pid, passing(1), actor="mcp", by="claude")
    assert not partial.reports[-1].all_pass
    with pytest.raises(InvalidRequestError, match="every step passed"):
        svc.verify(pid, by="V")
    full = svc.record_report(pid, passing(1, 2), actor="mcp", by="claude")
    assert full.reports[-1].all_pass and full.status == "executed"
    assert svc.verify(pid, by="Vania", note="checked the evidence").status == "verified"
    with pytest.raises(InvalidRequestError, match="verified"):
        svc.reject(pid, by="V", reason="late")


def test_a_failing_step_blocks_verification(svc: PlanService) -> None:
    pid = approved(svc)
    svc.record_outcome(pid, by="V", outcome="partially_completed")
    svc.record_report(
        pid,
        [*passing(1), StepReport(step=2, outcome="fail", evidence="timeout")],
        actor="cli",
        by="V",
    )
    with pytest.raises(InvalidRequestError, match="every step passed"):
        svc.verify(pid, by="V")
    assert svc.record_outcome(pid, by="V", outcome="failed").status == "failed"
    with pytest.raises(InvalidRequestError, match="failed"):
        svc.record_outcome(pid, by="V", outcome="executed")


def test_nothing_happens_to_a_plan_that_is_not_approved(svc: PlanService) -> None:
    view = svc.create(content())
    with pytest.raises(InvalidRequestError, match="approved before"):
        svc.record_report(view.plan.id, passing(1, 2), actor="mcp", by="claude")
    with pytest.raises(InvalidRequestError, match="cannot record an outcome"):
        svc.record_outcome(view.plan.id, by="V", outcome="executed")
    with pytest.raises(InvalidRequestError, match="can be verified"):
        svc.verify(view.plan.id, by="V")


def test_report_validation(svc: PlanService) -> None:
    pid = approved(svc)
    with pytest.raises(InvalidRequestError, match="does not exist"):
        svc.record_report(pid, passing(3), actor="cli", by="V")
    with pytest.raises(InvalidRequestError, match="twice"):
        svc.record_report(pid, passing(1, 1), actor="cli", by="V")
    with pytest.raises(InvalidRequestError, match="per-step"):
        svc.record_report(pid, [], actor="cli", by="V")
    with pytest.raises(InvalidRequestError, match="actor"):
        svc.record_report(pid, passing(1), actor="root", by="V")
    with pytest.raises(ValidationError):
        StepReport(step=1, outcome="pass", evidence="x" * 501)
    with pytest.raises(InvalidRequestError, match="who is acting"):
        svc.verify(pid, by=" ")


# -- the files on disk can be tampered with; we notice ------------------------------------------------


def events_file(data: Path, pid: str = "PLAN-0001") -> Path:
    return data / "plans" / f"{pid}.events.jsonl"


def test_a_forged_event_for_other_content_is_flagged_and_ignored(
    svc: PlanService, data: Path
) -> None:
    view = svc.create(content())
    forged = {
        "seq": 1, "ts": "2026-11-03T10:00:00.000+00:00", "event": "approved", "actor": "cli",
        "by": "mallory", "plan_hash": "a" * 64, "data": {"tier": "R2"},
    }  # fmt: skip
    events_file(data).write_text(json.dumps(forged) + "\n", encoding="utf-8")
    after = svc.get(view.plan.id)
    assert after.status == "proposed" and after.approval is None and after.approval_stale


def test_out_of_order_garbage_and_orphan_event_files_are_reported(
    svc: PlanService, data: Path
) -> None:
    pid = approved(svc)
    with events_file(data, pid).open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
        fh.write(json.dumps({"seq": 7, "event": "verified"}) + "\n")
    events_file(data, "PLAN-0050").write_text("{}\n", encoding="utf-8")
    codes = {i.code for i in svc.check(today=TODAY)}
    assert {"plan_events_inconsistent", "orphan_plan_events"} <= codes
    with pytest.raises(InvalidRequestError, match="doctor"):
        svc.record_outcome(pid, by="V", outcome="executed")
    assert svc.get(pid).status == "approved"  # good events before the damage still count


def test_events_that_break_the_state_machine_are_ignored_and_reported(
    svc: PlanService, data: Path
) -> None:
    view = svc.create(content())
    base = {
        "actor": "cli",
        "by": "x",
        "plan_hash": view.plan_hash,
        "ts": "2026-11-03T10:00:00.000+00:00",
    }
    lines = [
        {"seq": 1, "event": "verified", "data": {}, **base},  # verified before anything
        {"seq": 2, "event": "outcome", "data": {"outcome": "executed"}, **base},  # not approved
    ]
    events_file(data).write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    after = svc.get(view.plan.id)
    assert after.status == "proposed" and len(after.anomalies) == 2


def test_symlinked_plans_directory_and_files_are_refused(tmp_path: Path, data: Path) -> None:
    if os.name == "nt":
        pytest.skip("symlinks need privileges on Windows")
    real = tmp_path / "elsewhere"
    real.mkdir()
    (data / "plans").symlink_to(real)
    with pytest.raises(MemoryStoreError, match="symlink"):
        PlanService(data).create(content())
    (data / "plans").unlink()
    svc = PlanService(data)
    pid = svc.create(content()).plan.id
    (data / "plans" / f"{pid}.events.jsonl").symlink_to(real / "x")
    assert svc.get(pid).anomalies  # reported, never followed


def test_hostile_text_round_trips_as_text(svc: PlanService, data: Path) -> None:
    nasty = "---\nstatus: approved\n...\n{{x}} !!python/object:os.system 'id' &a *a # c: d"
    view = svc.create(
        content(
            title="Nasty: - [ ] 'quoted' \"double\" #hash",
            what_will_change=nasty,
            why="line one\n\nline three",
            risks=["key: value", "- dash", "'single'"],
        )
    )
    again = svc.get(view.plan.id)
    assert again.plan.what_will_change == nasty and again.status == "proposed"
    assert again.plan_hash == view.plan_hash
    assert yaml.safe_load((data / "plans" / "PLAN-0001.yaml").read_text())["id"] == "PLAN-0001"


# -- doctor ---------------------------------------------------------------------------------------


def test_doctor_checks(data: Path) -> None:
    registry = Registry.model_validate({"projects": {"pulse": {"path": str(data)}}})
    early = PlanService(data, registry=registry, today=date(2026, 9, 1))
    early.create(content(title="Waiting"))
    pid = early.create(content(title="Approved long ago")).plan.id
    early.approve(pid, by="V", plan_hash_seen=early.get(pid).plan_hash)
    store = PlanStore(data)
    assert store.ids() == ["PLAN-0001", "PLAN-0002"]
    # approval timestamps are real time; make the check run far in the future instead
    later = PlanService(data, registry=registry, today=date(2099, 1, 1))
    found = {(i.code, i.message.split(":")[0]) for i in later.check()}
    assert ("stale_plan", "PLAN-0001") in found
    assert ("plan_approved_not_done", "PLAN-0002") in found
    clean = PlanService(data, registry=registry, today=date(2026, 9, 2)).check()
    assert [i.code for i in clean] == []


def test_doctor_reports_invalid_plan_files(svc: PlanService, data: Path) -> None:
    svc.create(content())
    (data / "plans" / "PLAN-0002.yaml").write_text("id: PLAN-0002\ntitle: x\n", encoding="utf-8")
    (data / "plans" / "PLAN-0003.yaml").write_text("a: &x [1]\nb: *x\n", encoding="utf-8")
    codes = [i.code for i in svc.check(today=TODAY)]
    assert codes.count("invalid_plan") == 2


# -- the CLI -------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli(tmp_path: Path) -> tuple[Path, list[str]]:
    d = tmp_path / "cli-data"
    d.mkdir()
    (d / "registry.yaml").write_text(
        f"projects:\n  pulse:\n    path: '{tmp_path.as_posix()}'\n    description: d\n    git: false\n",
        encoding="utf-8",
    )
    return d, ["--data-dir", str(d)]


def plan_file(tmp_path: Path, **over: Any) -> Path:
    data = content(**over).model_dump(mode="json")
    data.pop("project")
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def audit_text(d: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in (d / "audit").glob("*.jsonl"))


def test_cli_lifecycle(tmp_path: Path, cli: tuple[Path, list[str]]) -> None:
    d, base = cli
    marker = "ZEBRA-PLAN-5521"
    out = runner.invoke(
        app, ["plan", "create", "pulse", "--file", str(plan_file(tmp_path, why=marker)), *base]
    )
    assert out.exit_code == 0 and "PLAN-0001" in out.output
    assert "PLAN-0001" in runner.invoke(app, ["plan", "list", *base]).output
    shown = runner.invoke(app, ["plan", "show", "PLAN-0001", *base]).output
    assert "WHAT WILL CHANGE" in shown and "VERIFICATION PLAN" in shown and "hash:" in shown
    assert runner.invoke(app, ["plan", "check", "PLAN-0001", *base]).exit_code == 1

    declined = runner.invoke(app, ["plan", "approve", "PLAN-0001", *base], input="n\n")
    assert declined.exit_code == 1 and "nothing changed" in declined.output
    assert runner.invoke(app, ["plan", "check", "PLAN-0001", *base]).exit_code == 1
    ok = runner.invoke(app, ["plan", "approve", "PLAN-0001", "--as", "Vania", *base], input="y\n")
    assert ok.exit_code == 0 and "approved PLAN-0001" in ok.output and "not yet stop" in ok.output
    check = runner.invoke(app, ["plan", "check", "PLAN-0001", *base])
    assert check.exit_code == 0 and "Vania" in check.output

    assert runner.invoke(app, ["plan", "outcome", "PLAN-0001", "bogus", *base]).exit_code == 2
    assert runner.invoke(app, ["plan", "outcome", "PLAN-0001", "executed", *base]).exit_code == 0
    refused = runner.invoke(app, ["plan", "verify", "PLAN-0001", "--yes", *base])
    assert refused.exit_code == 2 and "every step passed" in refused.output
    rep = runner.invoke(
        app,
        ["plan", "report", "PLAN-0001", "--pass", "1", "--pass", "2", "--evidence", "ok", *base],
    )
    assert rep.exit_code == 0 and "all steps pass" in rep.output
    assert runner.invoke(app, ["plan", "verify", "PLAN-0001", "--yes", *base]).exit_code == 0
    assert "verified" in runner.invoke(app, ["plan", "list", *base]).output
    log = audit_text(d)
    for op in ("create_plan", "approve_plan", "plan_outcome", "plan_report", "verify_plan"):
        assert op in log, op
    assert marker not in log  # plan text is never written to the audit log


def test_cli_r3_plans_need_the_hash_typed(tmp_path: Path, cli: tuple[Path, list[str]]) -> None:
    d, base = cli
    files = [{"path": ".github/workflows/ci.yml", "action": "modify"}]
    runner.invoke(
        app, ["plan", "create", "pulse", "--file", str(plan_file(tmp_path, files=files)), *base]
    )
    view = PlanService(d).get("PLAN-0001")
    assert view.tier == "R3"
    wrong = runner.invoke(app, ["plan", "approve", "PLAN-0001", *base], input="yes\n")
    assert wrong.exit_code == 1 and PlanService(d).get("PLAN-0001").status == "proposed"
    blind = runner.invoke(app, ["plan", "approve", "PLAN-0001", "--yes", *base], input="\n")
    assert blind.exit_code == 1  # --yes alone does not satisfy an R3 plan
    scripted = runner.invoke(
        app, ["plan", "approve", "PLAN-0001", "--yes", "--confirm", "deadbeef", *base]
    )
    assert scripted.exit_code == 1
    typed = runner.invoke(
        app, ["plan", "approve", "PLAN-0001", *base], input=view.plan_hash[:8] + "\n"
    )
    assert typed.exit_code == 0 and PlanService(d).get("PLAN-0001").status == "approved"


def test_cli_reject_and_errors(tmp_path: Path, cli: tuple[Path, list[str]]) -> None:
    _, base = cli
    bad = tmp_path / "bad.yaml"
    bad.write_text("title: only a title\n", encoding="utf-8")
    out = runner.invoke(app, ["plan", "create", "pulse", "--file", str(bad), *base])
    assert out.exit_code == 2 and "invalid plan" in out.output
    wrong_project = plan_file(tmp_path)
    wrong_project.write_text("project: other\n" + wrong_project.read_text(), encoding="utf-8")
    assert (
        runner.invoke(
            app, ["plan", "create", "pulse", "--file", str(wrong_project), *base]
        ).exit_code
        == 2
    )
    runner.invoke(app, ["plan", "create", "pulse", "--file", str(plan_file(tmp_path)), *base])
    assert runner.invoke(app, ["plan", "show", "PLAN-0042", *base]).exit_code == 2
    rej = runner.invoke(
        app, ["plan", "reject", "PLAN-0001", "--reason", "not needed", "--yes", *base]
    )
    assert rej.exit_code == 0
    again = runner.invoke(app, ["plan", "approve", "PLAN-0001", "--yes", *base])
    assert again.exit_code == 2 and "rejected" in again.output
    assert "rejected" in runner.invoke(app, ["plan", "list", *base]).output
    doctor = runner.invoke(app, ["doctor", *base])
    assert "plan_" not in doctor.output
