"""Shared state for the MCP server: registry, service caches, policy and audit.

Every tool, resource and prompt goes through :meth:`ServerContext.run`, which applies the
operation policy, writes the audit events, and turns expected errors into clean messages. An
unexpected exception is logged to stderr (with details) and reaches the client only as a generic
"Error executing tool ..." from the SDK.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from aios.config import REGISTRY_FILENAME
from aios.domain.audit import Scalar
from aios.domain.models import Registry
from aios.errors import AiosError
from aios.policy import OperationPolicy, PathGuard, PermissionLevel
from aios.services.audit import AuditLog
from aios.services.files import FileService
from aios.services.git import GitService, build_git_service
from aios.services.memory import MemoryService
from aios.services.memory_write import MemoryWriter
from aios.services.plan_execution import PlanExecutor
from aios.services.plans import PlanService
from aios.services.registry import load_registry

T = TypeVar("T")
log = logging.getLogger("aios.mcp")

#: Every operation the server exposes. All are READ_ONLY in Phase 1; nothing here can change
#: a project, its git state, or the memory store.
TOOL_NAMES = (
    "list_projects",
    "get_project_context",
    "list_project_files",
    "read_project_file",
    "search_project",
    "get_recent_changes",
    "get_commit",
    "get_diff",
    "search_git_history",
    "get_branch_status",
    "search_memory",
    "list_memory",
    "get_memory_record",
    "get_decision_history",
    "list_plans",
    "get_plan",
    "check_plan_scope",
)
OPERATIONS = (*TOOL_NAMES, "read_resource", "get_prompt")
#: Operations the write tools use. They are PROPOSE level: they can only create
#: unreviewed memory records, never touch a project. Denied unless the server is started with
#: ``--allow-propose``.
PROPOSE_OPERATIONS = (
    "propose_decision",
    "propose_task",
    "propose_memory_item",
    "update_task_status",
    "create_plan",
    "record_verification",
)


class ServerContext:
    def __init__(
        self, data_dir: Path, *, audit: AuditLog | None = None, allow_propose: bool = False
    ) -> None:
        self.data_dir = data_dir
        self.allow_propose = allow_propose
        self.audit = audit if audit is not None else AuditLog(data_dir, actor="mcp")
        self.memory = MemoryService(data_dir)
        self.writer = MemoryWriter(data_dir)  # only used by tools registered with allow_propose
        enabled = {PermissionLevel.READ_ONLY}
        if allow_propose:
            enabled.add(PermissionLevel.PROPOSE)  # opt-in: default deny stays the rule
        self.policy = OperationPolicy(frozenset(enabled))
        for operation in OPERATIONS:
            self.policy.register(operation, PermissionLevel.READ_ONLY)
        for operation in PROPOSE_OPERATIONS:
            self.policy.register(operation, PermissionLevel.PROPOSE)
        self._lock = threading.Lock()
        self._registry: tuple[tuple[int, int], Registry] | None = None
        self._files: dict[tuple[str, str], FileService] = {}
        self._git: dict[tuple[str, str], GitService] = {}

    def plans(self) -> PlanService:
        """A plan service bound to the current registry (cheap: it holds no state)."""
        return PlanService(self.data_dir, registry=self.registry())

    def executor(self) -> PlanExecutor:
        """Plan execution helper (used only for the read-only scope check)."""
        return PlanExecutor(self.plans(), self.registry())

    # -- registry and per-project services -----------------------------------------------

    def registry(self) -> Registry:
        """The registry, re-read when the file changes (adding a project needs no restart)."""
        file = self.data_dir / REGISTRY_FILENAME
        try:
            stat = file.stat()
        except OSError:
            return load_registry(file)  # raises a clear RegistryError
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._registry is None or self._registry[0] != key:
                self._registry = (key, load_registry(file))
            return self._registry[1]

    def files(self, project: str) -> FileService:
        config = self.registry().get(project)
        key = (project, str(config.path))
        with self._lock:
            cached = self._files.get(key)
        if cached is None:
            cached = FileService(PathGuard(config.path))
            with self._lock:
                self._files[key] = cached
        return cached

    def git(self, project: str) -> GitService:
        registry = self.registry()
        key = (project, str(registry.get(project).path))
        with self._lock:
            cached = self._git.get(key)
        if cached is None:
            cached = build_git_service(registry, project)
            with self._lock:
                self._git[key] = cached
        return cached

    # -- the audited, policy-checked call path -------------------------------------------

    def run(
        self,
        operation: str,
        fn: Callable[[], T],
        *,
        project: str | None = None,
        args: Mapping[str, object] | None = None,
        summarize: Callable[[T], Mapping[str, Scalar]] | None = None,
        error: type[Exception] = ToolError,
    ) -> T:
        try:
            return self.audit.run(
                operation, fn, policy=self.policy, project=project, args=args, summarize=summarize
            )
        except AiosError as exc:
            raise error(str(exc)) from exc
        except Exception:
            log.exception("unexpected error in %s", operation)
            raise
