"""The hand-maintained project state file (``projects/<project>/state.yaml``).

It holds what cannot be inferred from code or git: a short summary, the architecture in a few
sentences, and known constraints. A *person* writes it; nothing in aios writes it. It is shown in
the project snapshot labelled as unverified, and its text is data, never instructions.
"""

from __future__ import annotations

import datetime as dt
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # keeps \t \n \r
STATE_FILENAME = "state.yaml"
MAX_STATE_BYTES = 64 * 1024


class ProjectState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(default="", max_length=1500)
    architecture: str = Field(default="", max_length=3000)
    known_constraints: list[str] = Field(default_factory=list, max_length=20)
    #: When a person last confirmed this file is still true. Drives the stale report.
    updated: dt.date | None = None
    updated_by: str | None = Field(default=None, max_length=100)

    @field_validator("summary", "architecture", "updated_by")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        if value is not None and _BAD_CONTROL.search(value):
            raise ValueError("control characters are not allowed")
        return value.strip() if value is not None else None

    @field_validator("known_constraints")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        for item in value:
            if not 1 <= len(item.strip()) <= 300 or _BAD_CONTROL.search(item) or "\n" in item:
                raise ValueError("each constraint must be one line of 1-300 characters")
        return [item.strip() for item in value]
