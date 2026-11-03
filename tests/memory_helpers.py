"""Helpers to write memory record files for tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aios.domain.memory import TYPE_DIR

DEFAULT_STATUS = {
    "decision": "active",
    "assumption": "unverified",
    "constraint": "active",
    "question": "open",
    "discovery": "active",
    "task": "open",
    "note": "active",
}


def write_record(
    data_dir: Path,
    *,
    id: str,
    type: str = "decision",
    project: str = "pulse",
    title: str = "A title",
    status: str | None = None,
    date: str = "2026-10-01",
    body: str = "Body text.",
    directory: str | None = None,
    filename: str | None = None,
    **extra: Any,
) -> Path:
    front: dict[str, Any] = {
        "id": id,
        "type": type,
        "project": project,
        "title": title,
        "status": status or DEFAULT_STATUS[type],
        "date": date,
        **extra,
    }
    path = (
        data_dir / "projects" / project / (directory or TYPE_DIR[type]) / (filename or f"{id}.md")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body + "\n", encoding="utf-8"
    )
    return path
