"""Plan files in the data directory: ``plans/PLAN-0001.yaml`` (content, never rewritten) and
``plans/PLAN-0001.events.jsonl`` (append-only events).

Callers hold the plan write lock (``services/plans.py``); nothing here locks. Symlinked plan
directories and files are refused, files are size-capped, YAML aliases are rejected, and a plan
file is created atomically and never overwritten.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from aios.adapters.memory_files import _NoAliasLoader
from aios.adapters.memory_writer import replace_atomic
from aios.domain.plans import PLAN_ID_RE, Plan, PlanEvent
from aios.errors import MemoryStoreError

PLANS_DIRNAME = "plans"
MAX_PLAN_BYTES = 128 * 1024
MAX_EVENTS_BYTES = 512 * 1024
_O_BINARY = getattr(os, "O_BINARY", 0)
_NAME = re.compile(r"^(PLAN-\d{4,})\.(yaml|events\.jsonl)$")


@dataclass(frozen=True)
class EventsRead:
    events: list[PlanEvent]
    problems: list[str]


class PlanStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir / PLANS_DIRNAME

    # -- paths ---------------------------------------------------------------------------

    def _checked_dir(self, *, create: bool = False) -> Path:
        if self._dir.is_symlink():
            raise MemoryStoreError("refusing to use a symlinked plans directory")
        if create:
            self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def plan_path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.match(plan_id):
            raise MemoryStoreError(f"not a plan id: {plan_id!r}")
        return self._dir / f"{plan_id}.yaml"

    def events_path(self, plan_id: str) -> Path:
        if not PLAN_ID_RE.match(plan_id):
            raise MemoryStoreError(f"not a plan id: {plan_id!r}")
        return self._dir / f"{plan_id}.events.jsonl"

    # -- listing and ids -----------------------------------------------------------------

    def ids(self) -> list[str]:
        """Plan ids that have a content file, oldest first."""
        if not self._dir.is_dir() or self._dir.is_symlink():
            return []
        found = {
            m.group(1)
            for n in os.listdir(self._dir)
            if (m := _NAME.match(n)) and m.group(2) == "yaml"
        }
        return sorted(found)

    def orphan_event_files(self) -> list[str]:
        if not self._dir.is_dir():
            return []
        have = set(self.ids())
        return sorted(
            m.group(1) for n in os.listdir(self._dir)
            if (m := _NAME.match(n)) and m.group(2) == "events.jsonl" and m.group(1) not in have
        )  # fmt: skip

    def next_id(self) -> str:
        """Above every plan id ever used (content or events file), so ids are never reused."""
        highest = 0
        if self._dir.is_dir():
            for name in os.listdir(self._dir):
                match = _NAME.match(name)
                if match:
                    highest = max(highest, int(match.group(1).split("-")[1]))
        return f"PLAN-{highest + 1:04d}"

    # -- plans ---------------------------------------------------------------------------

    def create(self, plan: Plan) -> Path:
        directory = self._checked_dir(create=True)
        path = directory / f"{plan.id}.yaml"
        front = plan.model_dump(mode="json", exclude_defaults=False)
        front["files"] = [dict(f) for f in front["files"]]
        text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=4096)
        replace_atomic(path, text.encode("utf-8"), must_not_exist=True)
        return path

    def read(self, plan_id: str) -> Plan:
        self._checked_dir()
        path = self.plan_path(plan_id)
        if path.is_symlink() or not path.is_file():
            raise MemoryStoreError(f"no plan file for {plan_id}")
        if path.stat().st_size > MAX_PLAN_BYTES:
            raise MemoryStoreError(f"{path.name} is larger than {MAX_PLAN_BYTES} bytes")
        try:
            data = yaml.load(path.read_bytes().decode("utf-8"), Loader=_NoAliasLoader)
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise MemoryStoreError(f"{path.name}: unreadable ({exc})") from None
        if not isinstance(data, dict):
            raise MemoryStoreError(f"{path.name}: must be a mapping")
        try:
            plan = Plan.model_validate(data)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise MemoryStoreError(f"{path.name}: {detail}") from None
        if plan.id != plan_id:
            raise MemoryStoreError(f"{path.name}: contains id {plan.id}")
        return plan

    # -- events --------------------------------------------------------------------------

    def read_events(self, plan_id: str) -> EventsRead:
        path = self.events_path(plan_id)
        if path.is_symlink():
            return EventsRead([], [f"{path.name} is a symbolic link"])
        if not path.is_file():
            return EventsRead([], [])
        if path.stat().st_size > MAX_EVENTS_BYTES:
            return EventsRead([], [f"{path.name} is larger than {MAX_EVENTS_BYTES} bytes"])
        events: list[PlanEvent] = []
        problems: list[str] = []
        raw = path.read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = PlanEvent.model_validate(json.loads(line))
            except (ValueError, ValidationError) as exc:
                problems.append(f"{path.name} line {number}: invalid event ({str(exc)[:80]})")
                continue
            if event.seq != len(events) + 1:
                problems.append(f"{path.name} line {number}: sequence {event.seq} is out of order")
                continue
            events.append(event)
        return EventsRead(events, problems)

    def append_event(self, plan_id: str, event: PlanEvent) -> None:
        directory = self._checked_dir(create=True)
        path = directory / f"{plan_id}.events.jsonl"
        if path.is_symlink():
            raise MemoryStoreError(f"refusing to append through a symlink: {path.name}")
        line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o666)
        try:
            os.write(fd, line.encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
