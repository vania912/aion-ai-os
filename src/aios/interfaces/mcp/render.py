"""Compact renderings of service results for a model: JSON without indentation for structured
data, plain text for content. Fewer tokens than pretty-printed JSON, and code stays readable."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from aios.domain.files import FileContent
from aios.domain.git import CommitDetail, DiffResult
from aios.domain.memory import MemoryRecordView


def compact(model: BaseModel, exclude: Any = None) -> str:
    return model.model_dump_json(exclude_none=True, exclude=exclude)


def _meta(model: BaseModel, exclude: set[str]) -> str:
    data: dict[str, Any] = model.model_dump(mode="json", exclude=exclude, exclude_none=True)
    keep_even_if_false = {"reviewed"}  # an unreviewed record must never look reviewed
    data = {
        k: v for k, v in data.items() if k in keep_even_if_false or v not in ([], {}, "", False, 0)
    }
    if data.get("origin") == "human":
        del data["origin"]
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def render_file(result: FileContent) -> str:
    flag = " [truncated]" if result.truncated else ""
    header = (
        f"[{result.path} lines {result.start_line}-{result.end_line} of {result.total_lines}{flag}]"
    )
    return f"{header}\n{result.content}"


def render_commit(detail: CommitDetail) -> str:
    parts = [_meta(detail, {"diff", "body"})]
    if detail.body:
        parts.append("--- message ---\n" + detail.body)
    if detail.diff:
        parts.append("--- diff ---\n" + detail.diff)
    return "\n".join(parts)


def render_diff(result: DiffResult) -> str:
    parts = [_meta(result, {"diff"})]
    if result.diff:
        parts.append("--- diff ---\n" + result.diff)
    return "\n".join(parts)


def render_record(view: MemoryRecordView) -> str:
    meta = _meta(view.record, {"body"})
    extra = f" path={view.path}{' [body truncated]' if view.body_truncated else ''}"
    parts = [meta + extra]
    if view.record.body:
        parts.append("--- body ---\n" + view.record.body)
    return "\n".join(parts)


def _dump(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def compact_hits(result: BaseModel, field: str) -> str:
    """Search/list results for a model: no path or score, and review state shown only when it
    is NOT the trusted default (an unreviewed record must always say so)."""
    data: dict[str, Any] = result.model_dump(mode="json", exclude_none=True)
    for hit in data[field]:
        hit.pop("path", None)
        hit.pop("score", None)
        if hit.get("origin") == "human":
            hit.pop("origin")
        if hit.get("reviewed") is True:
            hit.pop("reviewed")
    return _dump(data)


def compact_chain(result: BaseModel) -> str:
    """Decision history: ``reviewed`` appears only on entries that are not reviewed."""
    data: dict[str, Any] = result.model_dump(mode="json", exclude_none=True)
    for entry in data["chain"]:
        if entry.get("reviewed") is True:
            entry.pop("reviewed")
        for key in ("made_by", "why"):
            if entry.get(key) == []:
                entry.pop(key)
    return _dump(data)
