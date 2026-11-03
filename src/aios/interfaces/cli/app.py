"""``aios`` command-line interface."""

from __future__ import annotations

import contextlib
import getpass
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import typer

from aios import __version__
from aios.config import Settings, resolve_settings
from aios.domain.audit import AuditEvent
from aios.domain.git import ChangedFile
from aios.domain.memory import MemoryHit
from aios.domain.models import Issue, Registry, Severity
from aios.errors import AiosError, InvalidRequestError
from aios.services.audit import AuditLog, audit_dir_exists
from aios.services.files import FileService, build_file_service
from aios.services.git import GitService, build_git_service
from aios.services.memory import MemoryService, memory_dir_exists
from aios.services.memory_review import MemoryReviewer
from aios.services.memory_write import CreateResult, MemoryWriter, NewRecord
from aios.services.registry import check_registry, load_registry

app = typer.Typer(
    name="aios",
    help="Personal AI operating layer: inspect projects and memory without Claude.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Registered projects.", no_args_is_help=True)
app.add_typer(project_app, name="project")
files_app = typer.Typer(help="List, read and search project files.", no_args_is_help=True)
app.add_typer(files_app, name="files")
git_app = typer.Typer(help="Read-only git history, diffs and status.", no_args_is_help=True)
app.add_typer(git_app, name="git")
index_app = typer.Typer(help="The derived memory search index.", no_args_is_help=True)
app.add_typer(index_app, name="index")
memory_app = typer.Typer(help="Search and read memory records.", no_args_is_help=True)
app.add_typer(memory_app, name="memory")
decision_app = typer.Typer(help="Decision records and their history.", no_args_is_help=True)
app.add_typer(decision_app, name="decision")
audit_app = typer.Typer(help="The audit log of operations.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
task_app = typer.Typer(help="Tasks: add and change status.", no_args_is_help=True)
app.add_typer(task_app, name="task")
review_app = typer.Typer(help="Review records that Claude proposed.", no_args_is_help=True)
app.add_typer(review_app, name="review")

DataDirOption = Annotated[
    Path | None,
    typer.Option("--data-dir", help="Data directory (default: $AIOS_DATA_DIR)."),
]


def _load(data_dir: Path | None) -> Registry:
    """Resolve settings and load the registry, converting expected errors to exit code 2."""
    try:
        return load_registry(resolve_settings(data_dir).registry_file)
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _settings(data_dir: Path | None) -> Settings:
    with _cli_errors():
        return resolve_settings(data_dir)


def _memory(data_dir: Path | None) -> MemoryService:
    settings = _settings(data_dir)
    registry = None
    if settings.registry_file.is_file():
        with contextlib.suppress(AiosError):  # memory commands do not require a valid registry
            registry = load_registry(settings.registry_file)
    return MemoryService(settings.data_dir, registry=registry)


def _files(data_dir: Path | None, project: str) -> FileService:
    try:
        return build_file_service(_load(data_dir), project)
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the aios version."""
    typer.echo(__version__)


@project_app.command("list")
def project_list(data_dir: DataDirOption = None) -> None:
    """List registered projects."""
    registry = _load(data_dir)
    if not registry.projects:
        typer.echo("no projects registered")
        return
    for project_id, project in sorted(registry.projects.items()):
        typer.echo(f"{project_id}\t{project.path}\t{project.description}")


@project_app.command("state")
def project_state(
    project: Annotated[str, typer.Argument(help="Project id.")],
    data_dir: DataDirOption = None,
) -> None:
    """Show a project's hand-maintained state file (projects/<project>/state.yaml)."""
    registry = _load(data_dir)
    try:
        registry.get(project)  # unknown project -> clear error
        state = _memory(data_dir).project_state(project)
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if state is None:
        typer.echo(f"no state file: create projects/{project}/state.yaml (see docs/MEMORY.md)")
        return
    typer.echo(state.model_dump_json(indent=2, exclude_none=True))


@app.command()
def doctor(data_dir: DataDirOption = None) -> None:
    """Check the registry and the memory store. Exit code 1 if any error is found."""
    settings = _settings(data_dir)
    registry = _load(data_dir)
    issues = check_registry(registry)
    if memory_dir_exists(settings.data_dir):
        issues += MemoryService(settings.data_dir, registry=registry).check()
    if audit_dir_exists(settings.data_dir):
        audit = AuditLog(settings.data_dir, actor="cli").verify()
        for p in audit.problems:
            msg = f"{p.file}:{p.line} {p.code}: {p.message}"
            issues.append(Issue("-", Severity.ERROR, "audit_chain_broken", msg))
    for issue in issues:
        typer.echo(
            f"{issue.severity.value}	{issue.project_id}	{issue.code}	{issue.message}"
        )
    if any(issue.severity is Severity.ERROR for issue in issues):
        raise typer.Exit(code=1)
    typer.echo(f"ok: {len(registry.projects)} project(s) checked, {len(issues)} issue(s)")


@files_app.command("list")
def files_list(
    project: str,
    path: Annotated[str, typer.Argument(help="Directory, relative to the project root.")] = "",
    depth: int = 1,
    max_entries: int = 200,
    data_dir: DataDirOption = None,
) -> None:
    """List visible files and directories (tab-separated: kind, path, size)."""
    service = _files(data_dir, project)
    try:
        listing = service.list_files(path, depth, max_entries)
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for entry in listing.entries:
        size = "" if entry.size is None else str(entry.size)
        typer.echo(f"{'d' if entry.kind == 'dir' else 'f'}\t{entry.path}\t{size}")
    if listing.truncated:
        typer.echo("[truncated]", err=True)


@files_app.command("read")
def files_read(
    project: str,
    path: str,
    start: int = 1,
    end: int | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """Print a file (or a line range) from a project."""
    service = _files(data_dir, project)
    try:
        result = service.read_file(path, start, end)
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(result.content)
    note = f"[lines {result.start_line}-{result.end_line} of {result.total_lines}]"
    typer.echo(note + (" [truncated]" if result.truncated else ""), err=True)


@files_app.command("search")
def files_search(
    project: str,
    query: str,
    path: str = "",
    glob: str | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
    data_dir: DataDirOption = None,
) -> None:
    """Search project files (path:line:text). Regex requires ripgrep."""
    service = _files(data_dir, project)
    try:
        result = service.search(
            query,
            path=path,
            glob=glob,
            regex=regex,
            ignore_case=not case_sensitive,
            max_results=max_results,
        )
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for match in result.matches:
        typer.echo(f"{match.path}:{match.line}:{match.text}")
    typer.echo(
        f"[engine={result.engine} matches={len(result.matches)}"
        f"{' truncated' if result.truncated else ''}]",
        err=True,
    )


@contextmanager
def _cli_errors() -> Iterator[None]:
    """Turn expected errors into a message on stderr and exit code 2."""
    try:
        yield
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _git(data_dir: Path | None, project: str) -> GitService:
    with _cli_errors():
        return build_git_service(_load(data_dir), project)


def _echo_files(files: list[ChangedFile], hidden: int) -> None:
    for file in files:
        typer.echo(f"    {file.status} {file.path}")
    if hidden:
        typer.echo(f"    (+{hidden} file(s) withheld by policy)")


@git_app.command("recent")
def git_recent(
    project: str,
    since: str | None = None,
    limit: int = 20,
    path: str | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """Recent commits with the (policy-visible) files each changed."""
    service = _git(data_dir, project)
    with _cli_errors():
        result = service.get_recent_changes(since, limit, path)
    for c in result.commits:
        typer.echo(f"{c.short_hash} {c.date[:10]} {c.author}: {c.subject}")
        _echo_files(c.files, c.hidden_files)
    if result.truncated:
        typer.echo("[truncated]", err=True)


@git_app.command("show")
def git_show(project: str, ref: str, data_dir: DataDirOption = None) -> None:
    """Show one commit: message, visible files and patch."""
    service = _git(data_dir, project)
    with _cli_errors():
        d = service.get_commit(ref)
    typer.echo(f"{d.hash}\n{d.author}  {d.date}\n\n    {d.subject}")
    if d.body:
        typer.echo("\n" + d.body)
    typer.echo("")
    _echo_files(d.files, d.hidden_files)
    if d.diff:
        typer.echo("\n" + d.diff)
    notes = [
        n
        for n, on in (("diff truncated", d.diff_truncated), ("body truncated", d.body_truncated))
        if on
    ]
    if d.files_omitted:
        notes.append(f"{d.files_omitted} file(s) omitted from the diff")
    if notes:
        typer.echo("[" + "; ".join(notes) + "]", err=True)


@git_app.command("diff")
def git_diff(
    project: str,
    base: str,
    head: Annotated[str, typer.Argument(help="Defaults to HEAD.")] = "HEAD",
    path: str | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """Diff between two commits (policy-visible files only)."""
    service = _git(data_dir, project)
    with _cli_errors():
        r = service.get_diff(base, head, path)
    _echo_files(r.files, r.hidden_files)
    if r.diff:
        typer.echo("\n" + r.diff)
    if r.diff_truncated or r.files_omitted:
        typer.echo(f"[truncated; {r.files_omitted} file(s) omitted]", err=True)


@git_app.command("search")
def git_search(
    project: str,
    query: str,
    mode: Literal["message", "content", "path"] = "message",
    since: str | None = None,
    limit: int = 20,
    data_dir: DataDirOption = None,
) -> None:
    """Find commits by message, changed content (pickaxe) or path."""
    service = _git(data_dir, project)
    with _cli_errors():
        hits = service.search_history(query, mode, since=since, limit=limit)
    for c in hits.commits:
        typer.echo(f"{c.short_hash} {c.date[:10]} {c.author}: {c.subject}")
    if hits.truncated:
        typer.echo("[truncated]", err=True)


@git_app.command("status")
def git_status(project: str, data_dir: DataDirOption = None) -> None:
    """Branch, upstream and working-tree summary."""
    service = _git(data_dir, project)
    with _cli_errors():
        s = service.get_branch_status()
    where = "(detached)" if s.detached else (s.branch or "(none)")
    typer.echo(f"branch: {where}  head: {(s.head or '(no commits)')[:10]}")
    if s.upstream:
        typer.echo(f"upstream: {s.upstream}  ahead {s.ahead}  behind {s.behind}")
    typer.echo(
        f"staged {s.staged}  unstaged {s.unstaged}  untracked {s.untracked}  "
        f"conflicted {s.conflicted}  clean: {s.clean}"
    )
    for e in s.entries:
        typer.echo(f"  {e.index}{e.worktree} {e.path}")
    for u in s.untracked_paths:
        typer.echo(f"  ?? {u}")
    if s.hidden_files:
        typer.echo(f"  (+{s.hidden_files} path(s) withheld by policy)")


@git_app.command("branches")
def git_branches(
    project: str, remotes: bool = False, limit: int = 50, data_dir: DataDirOption = None
) -> None:
    """List branches (current one marked with *)."""
    service = _git(data_dir, project)
    with _cli_errors():
        result = service.list_branches(remotes=remotes, limit=limit)
    for b in result.branches:
        typer.echo(f"{'*' if b.current else ' '} {b.name}  {b.hash[:10]}")
    if result.truncated:
        typer.echo("[truncated]", err=True)


def _echo_hits(hits: list[MemoryHit], *, snippets: bool) -> None:
    for h in hits:
        note = f" -> {h.superseded_by}" if h.superseded_by else ""
        flag = "" if h.reviewed else f" UNREVIEWED({h.origin})"
        typer.echo(f"{h.id}\t{h.status}{note}{flag}\t{h.project}\t{h.title}")
        if snippets and h.snippet:
            typer.echo(f"    {h.snippet}")


@index_app.command("rebuild")
def index_rebuild(data_dir: DataDirOption = None) -> None:
    """Rebuild the search index from the record files."""
    service = _memory(data_dir)
    with _cli_errors():
        stats = service.rebuild_index()
    typer.echo(f"indexed {stats.record_count} record(s); {stats.issue_count} issue(s) skipped")
    if stats.issue_count:
        typer.echo("run `aios doctor` for details", err=True)


@index_app.command("status")
def index_status(data_dir: DataDirOption = None) -> None:
    """Show index metadata and whether it is stale."""
    service = _memory(data_dir)
    with _cli_errors():
        status, stale = service.index_status()
    if status is None:
        typer.echo("no usable index (it is created automatically on first query)")
        return
    typer.echo(f"records: {status.record_count}  built: {status.built_at}  stale: {stale}")


@memory_app.command("search")
def memory_search(
    query: str,
    project: str | None = None,
    type: Annotated[str | None, typer.Option("--type")] = None,
    status: str | None = None,
    tag: str | None = None,
    reviewed: Annotated[bool | None, typer.Option("--reviewed/--unreviewed")] = None,
    limit: int = 20,
    data_dir: DataDirOption = None,
) -> None:
    """Search memory: exact id, then all words, then any word."""
    service = _memory(data_dir)
    with _cli_errors():
        result = service.search(
            query,
            project=project,
            record_type=type,
            status=status,
            tag=tag,
            reviewed=reviewed,
            limit=limit,
        )
    _echo_hits(result.hits, snippets=True)
    typer.echo(f"[layer={result.layer}{' truncated' if result.truncated else ''}]", err=True)


@memory_app.command("list")
def memory_list(
    project: str | None = None,
    type: Annotated[str | None, typer.Option("--type")] = None,
    status: str | None = None,
    tag: str | None = None,
    reviewed: Annotated[bool | None, typer.Option("--reviewed/--unreviewed")] = None,
    limit: int = 50,
    data_dir: DataDirOption = None,
) -> None:
    """List records, newest first."""
    service = _memory(data_dir)
    with _cli_errors():
        result = service.list_records(
            project=project,
            record_type=type,
            status=status,
            tag=tag,
            reviewed=reviewed,
            limit=limit,
        )
    _echo_hits(result.records, snippets=False)
    if result.truncated:
        typer.echo("[truncated]", err=True)


def _show(record_id: str, data_dir: Path | None) -> None:
    service = _memory(data_dir)
    with _cli_errors():
        view = service.get(record_id)
    r = view.record
    typer.echo(f"{r.id}  [{r.status}]  {r.title}")
    typer.echo(f"project: {r.project}  type: {r.type}  date: {r.date}  file: {view.path}")
    lists = (
        ("made by", r.made_by),
        ("why", r.why),
        ("alternatives", r.alternatives_considered),
        ("tags", r.tags),
        ("files", r.related_files),
    )
    for label, values in lists:
        if values:
            typer.echo(f"{label}: " + "; ".join(values))
    singles = (
        ("supersedes", r.supersedes),
        ("superseded by", r.superseded_by),
        ("reason for change", r.reason_for_change),
    )
    for label, value in singles:
        if value:
            typer.echo(f"{label}: {value}")
    if r.body:
        typer.echo("\n" + r.body)
    if view.body_truncated:
        typer.echo("[body truncated]", err=True)


@memory_app.command("show")
def memory_show(record_id: str, data_dir: DataDirOption = None) -> None:
    """Print one record."""
    _show(record_id, data_dir)


@memory_app.command("check")
def memory_check(data_dir: DataDirOption = None) -> None:
    """Check records for problems (also part of `aios doctor`)."""
    service = _memory(data_dir)
    with _cli_errors():
        issues = service.check()
    for issue in issues:
        typer.echo(f"{issue.severity.value}\t{issue.project_id}\t{issue.code}\t{issue.message}")
    if any(i.severity is Severity.ERROR for i in issues):
        raise typer.Exit(code=1)
    typer.echo(f"ok: {len(issues)} issue(s)")


@decision_app.command("list")
def decision_list(
    project: str,
    status: str | None = None,
    limit: int = 50,
    data_dir: DataDirOption = None,
) -> None:
    """List a project's decisions, newest first."""
    service = _memory(data_dir)
    with _cli_errors():
        result = service.list_records(
            project=project, record_type="decision", status=status, limit=limit
        )
    _echo_hits(result.records, snippets=False)
    if result.truncated:
        typer.echo("[truncated]", err=True)


@decision_app.command("show")
def decision_show(record_id: str, data_dir: DataDirOption = None) -> None:
    """Print one decision."""
    _show(record_id, data_dir)


@decision_app.command("history")
def decision_history(record_id: str, data_dir: DataDirOption = None) -> None:
    """Show what a decision replaced and what replaced it (oldest first)."""
    service = _memory(data_dir)
    with _cli_errors():
        result = service.decision_history(record_id)
    for e in result.chain:
        marker = ">" if e.position == "requested" else " "
        typer.echo(f"{marker} {e.id} [{e.status}] {e.date}  {e.title}")
        if e.why:
            typer.echo("    why: " + "; ".join(e.why))
        if e.reason_for_change:
            typer.echo(f"    replaced the previous decision because: {e.reason_for_change}")
    typer.echo(f"current: {result.current_id}")
    for note in result.notes:
        typer.echo(f"warning: {note}", err=True)


def _audit(data_dir: Path | None) -> AuditLog:
    return AuditLog(_settings(data_dir).data_dir, actor="cli")


def _fmt_args(values: Mapping[str, object]) -> str:
    parts = []
    for key, value in values.items():
        shown = repr(value[:60]) if isinstance(value, str) else repr(value)  # None stays None
        parts.append(f"{key}={shown}")
    return " ".join(parts)


def _echo_event(e: AuditEvent) -> None:
    state = e.verdict if e.event == "request" else (e.outcome or "")
    typer.echo(
        f"{e.seq}\t{e.ts}\t{e.call_id[:8]}\t{e.event}\t{e.op}\t{e.project or '-'}"
        f"\t{state}\t{e.code or ''}\t{_fmt_args(e.args)}"
    )


@audit_app.command("list")
def audit_list(
    limit: int = 50,
    op: str | None = None,
    project: str | None = None,
    outcome: str | None = None,
    call_id: str | None = None,
    since: str | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """List audit events, newest first."""
    audit = _audit(data_dir)
    with _cli_errors():
        events = audit.list_events(
            limit=limit, op=op, project=project, outcome=outcome, call_id=call_id, since=since
        )
    for e in events:
        _echo_event(e)
    if not events:
        typer.echo("no matching events", err=True)


@audit_app.command("show")
def audit_show(call_id: str, data_dir: DataDirOption = None) -> None:
    """Show every event of one call (request and result), oldest first."""
    audit = _audit(data_dir)
    with _cli_errors():
        events = audit.get_call(call_id)
    if not events:
        typer.echo(f"error: no events for call {call_id}", err=True)
        raise typer.Exit(code=2)
    for e in events:
        typer.echo(f"--- {e.event} #{e.seq} {e.ts}")
        for key, value in e.model_dump(exclude={"event", "seq", "ts"}).items():
            if value not in (None, {}, ""):
                typer.echo(f"{key}: {value}")


@audit_app.command("verify")
def audit_verify(data_dir: DataDirOption = None) -> None:
    """Check the hash chain. Exit code 1 if anything is wrong."""
    audit = _audit(data_dir)
    with _cli_errors():
        result = audit.verify()
    for p in result.problems:
        typer.echo(f"{p.file}:{p.line}\t{p.code}\t{p.message}")
    if result.more_problems:
        typer.echo("(more problems not shown)")
    if not result.ok:
        raise typer.Exit(code=1)
    typer.echo(f"ok: {result.events} event(s), last seq {result.last_seq}")


@audit_app.command("anchor")
def audit_anchor(data_dir: DataDirOption = None) -> None:
    """Print the newest sequence number and hash (record it where writers cannot edit)."""
    audit = _audit(data_dir)
    with _cli_errors():
        seq, digest = audit.anchor()
    typer.echo(f"seq: {seq}\nhash: {digest or '(empty log)'}")


@app.command()
def serve(
    data_dir: DataDirOption = None,
    allow_propose: Annotated[
        bool,
        typer.Option(
            "--allow-propose",
            help="Enable PROPOSE-level operations (Claude may create unreviewed records).",
        ),
    ] = False,
) -> None:
    """Run the MCP server over stdio (Claude Code starts this; see docs/MCP.md)."""
    settings = _settings(data_dir)
    try:
        from aios.interfaces.mcp.server import serve as run_server

        run_server(settings.data_dir, allow_propose=allow_propose)
    except AiosError as exc:
        typer.echo(f"error: {exc}", err=True)  # stderr: stdout belongs to the protocol
        raise typer.Exit(code=1) from exc


# -- writing memory (human CLI; reviewed records, never committed) --------------------------

ListOpt = Annotated[list[str] | None, typer.Option()]
MAX_BODY_FILE_BYTES = 256 * 1024


def _whoami() -> str:
    return os.environ.get("AIOS_USER", "").strip() or getpass.getuser()


def _body(body: str | None, body_file: Path | None) -> str:
    if body is not None and body_file is not None:
        raise typer.BadParameter("use either --body or --body-file, not both")
    if body_file is None:
        return body or ""
    with _cli_errors():
        try:
            if body_file.stat().st_size > MAX_BODY_FILE_BYTES:
                raise InvalidRequestError(
                    f"{body_file.name} is larger than {MAX_BODY_FILE_BYTES} bytes"
                )
            return body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InvalidRequestError(f"cannot read {body_file}: {exc}") from exc
    return ""  # unreachable: _cli_errors exits


def _audit_write(settings: Settings, op: str, project: str | None, args: dict[str, object]) -> None:
    """Record a human CLI write. The write already happened, so a failure only warns."""
    try:
        AuditLog(settings.data_dir, actor="cli").note(op, project=project, args=args)
    except AiosError as exc:
        typer.echo(f"warning: the audit log could not be written: {exc}", err=True)


def _created(settings: Settings, result: CreateResult, *, show_notes: bool = True) -> None:
    r = result.record
    _audit_write(settings, "create_record", r.project, {"id": r.id, "type": r.type})
    typer.echo(f"created {r.id} [{r.status}] {result.path}")
    for note in result.notes if show_notes else []:
        typer.echo(f"note: {note}")
    typer.echo("(written to disk only; commit your data repo when you are ready)", err=True)


@decision_app.command("create")
def decision_create(
    project: str,
    title: Annotated[str, typer.Option(help="One-line title.")],
    why: Annotated[list[str], typer.Option(help="A reason. Repeat for several.")],
    alternative: ListOpt = None,
    made_by: ListOpt = None,
    supersedes: Annotated[str | None, typer.Option(help="Decision id this replaces.")] = None,
    reason_for_change: str | None = None,
    tag: ListOpt = None,
    related_file: ListOpt = None,
    related_task: ListOpt = None,
    related_decision: ListOpt = None,
    assumption: ListOpt = None,
    constraint: ListOpt = None,
    evidence: ListOpt = None,
    body: str | None = None,
    body_file: Path | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """Record a decision (active and reviewed). With --supersedes, the old one is superseded."""
    settings = _settings(data_dir)
    text = _body(body, body_file)
    me = _whoami()
    with _cli_errors():
        writer = MemoryWriter(settings.data_dir)
        result = writer.create(
            NewRecord(
                type="decision", project=project, title=title, why=why, body=text,
                alternatives_considered=alternative or [], made_by=made_by or [me],
                supersedes=supersedes, reason_for_change=reason_for_change, tags=tag or [],
                related_files=related_file or [], related_tasks=related_task or [],
                related_decisions=related_decision or [], assumptions=assumption or [],
                constraints=constraint or [], evidence=evidence or [],
            )
        )  # fmt: skip
        _created(settings, result, show_notes=not supersedes)
        if supersedes:
            changes = writer.complete_supersession(result.record.id, accepted_by=me)
            _audit_write(
                settings, "complete_supersession", project,
                {"id": result.record.id, "replaces": supersedes},
            )  # fmt: skip
            for change in changes:
                typer.echo(f"updated {change}")


@decision_app.command("repair")
def decision_repair(record_id: str, data_dir: DataDirOption = None) -> None:
    """Finish a half-applied supersession (safe to run again)."""
    settings = _settings(data_dir)
    with _cli_errors():
        changes = MemoryWriter(settings.data_dir).complete_supersession(
            record_id, accepted_by=_whoami()
        )
    if not changes:
        typer.echo("nothing to do: the supersession is already complete")
        return
    _audit_write(settings, "complete_supersession", None, {"id": record_id.upper()})
    for change in changes:
        typer.echo(f"updated {change}")


@task_app.command("add")
def task_add(
    project: str,
    title: Annotated[str, typer.Option(help="One-line title.")],
    tag: ListOpt = None,
    related_decision: ListOpt = None,
    related_file: ListOpt = None,
    body: str | None = None,
    body_file: Path | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """Add a task (open, reviewed)."""
    settings = _settings(data_dir)
    text = _body(body, body_file)
    with _cli_errors():
        result = MemoryWriter(settings.data_dir).create(
            NewRecord(
                type="task", project=project, title=title, body=text, tags=tag or [],
                related_decisions=related_decision or [], related_files=related_file or [],
            )
        )  # fmt: skip
        _created(settings, result)


@task_app.command("status")
def task_status(task_id: str, status: str, data_dir: DataDirOption = None) -> None:
    """Move a task to a new status (open, in_progress, blocked, done, cancelled)."""
    settings = _settings(data_dir)
    with _cli_errors():
        before = MemoryService(settings.data_dir).get(task_id).record
        after = MemoryWriter(settings.data_dir).set_task_status(task_id, status)
    if before.status != after.status:
        _audit_write(
            settings, "set_task_status", after.project,
            {"id": after.id, "from": before.status, "to": after.status},
        )  # fmt: skip
    typer.echo(f"{after.id}: {before.status} -> {after.status}")


@memory_app.command("add")
def memory_add(
    project: str,
    type: Annotated[
        Literal["assumption", "constraint", "question", "discovery", "note"],
        typer.Option("--type", help="Kind of record."),
    ],
    title: Annotated[str, typer.Option(help="One-line title.")],
    tag: ListOpt = None,
    related_decision: ListOpt = None,
    related_file: ListOpt = None,
    evidence: ListOpt = None,
    body: str | None = None,
    body_file: Path | None = None,
    data_dir: DataDirOption = None,
) -> None:
    """Add an assumption, constraint, question, discovery or note (reviewed)."""
    settings = _settings(data_dir)
    text = _body(body, body_file)
    with _cli_errors():
        result = MemoryWriter(settings.data_dir).create(
            NewRecord(
                type=type, project=project, title=title, body=text, tags=tag or [],
                related_decisions=related_decision or [], related_files=related_file or [],
                evidence=evidence or [],
            )
        )  # fmt: skip
        _created(settings, result)


# -- review workflow (human CLI) --------------------------------------------------------------

YesOpt = Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")]


@review_app.command("list")
def review_list(
    project: str | None = None,
    type: Annotated[str | None, typer.Option("--type")] = None,
    data_dir: DataDirOption = None,
) -> None:
    """List records awaiting review, newest first."""
    settings = _settings(data_dir)
    with _cli_errors():
        pending = MemoryReviewer(settings.data_dir).list_pending(project=project, record_type=type)
    for r in pending:
        typer.echo(f"{r.id}\t{r.type}\t{r.status}\t{r.project}\t{r.origin}\t{r.date}\t{r.title}")
    typer.echo(f"[{len(pending)} awaiting review]", err=True)


@review_app.command("show")
def review_show(
    record_id: str,
    as_name: Annotated[str | None, typer.Option("--as")] = None,
    data_dir: DataDirOption = None,
) -> None:
    """Show a proposal in full and exactly what accepting it would change."""
    settings = _settings(data_dir)
    me = as_name or _whoami()
    with _cli_errors():
        plan = MemoryReviewer(settings.data_dir).plan_accept(record_id, accepted_by=me)
    r = plan.record
    state = "reviewed" if r.reviewed else "NOT REVIEWED"
    typer.echo(f"{r.id}  [{r.status}]  {r.title}")
    typer.echo(f"project: {r.project}  type: {r.type}  origin: {r.origin}  {state}  date: {r.date}")
    typer.echo(f"file: {plan.path}")
    for label, values in (
        ("why", r.why),
        ("alternatives", r.alternatives_considered),
        ("evidence", r.evidence),
        ("tags", r.tags),
        ("files", r.related_files),
    ):
        for value in values:
            typer.echo(f"{label}: {value}")
    if r.reason_for_change:
        typer.echo(f"reason for change: {r.reason_for_change}")
    if r.body:
        typer.echo("\n" + r.body)
    if plan.predecessor is not None:
        p = plan.predecessor
        typer.echo(f"\nWould replace {p.id} [{p.status}] {p.title}")
        for reason in p.why:
            typer.echo(f"  its reason: {reason}")
    typer.echo("\nIf you accept:")
    for action in plan.actions:
        typer.echo(f"  - {action}")
    if plan.blocked:
        typer.echo(f"BLOCKED: {plan.blocked}")
    for warning in plan.warnings:
        typer.echo(f"warning: {warning}")
    if plan.similar:
        typer.echo("\nSimilar records:")
        for h in plan.similar:
            typer.echo(f"  {h.id} [{h.status}] {h.title}")


@review_app.command("accept")
def review_accept(
    record_id: str,
    yes: YesOpt = False,
    as_name: Annotated[
        str | None, typer.Option("--as", help="Name recorded as the accepter.")
    ] = None,
    data_dir: DataDirOption = None,
) -> None:
    """Accept a proposal: it counts as reviewed (decisions also come into force)."""
    settings = _settings(data_dir)
    me = as_name or _whoami()
    reviewer = MemoryReviewer(settings.data_dir)
    with _cli_errors():
        plan = reviewer.plan_accept(record_id, accepted_by=me)
        if plan.blocked:
            raise InvalidRequestError(plan.blocked)
        typer.echo(f"{plan.record.id} [{plan.record.status}] {plan.record.title}")
        for action in plan.actions:
            typer.echo(f"  - {action}")
        if not yes and not typer.confirm("Accept?", default=False):
            typer.echo("cancelled; nothing changed")
            raise typer.Exit(code=1)
        changes = reviewer.accept(record_id, accepted_by=me)
    if not changes:
        typer.echo("nothing to do: already accepted")
        return
    _audit_write(
        settings, "accept_record", plan.record.project,
        {"id": plan.record.id, "type": plan.record.type},
    )  # fmt: skip
    for change in changes:
        typer.echo(f"updated {change}")
    typer.echo("(written to disk only; commit your data repo when you are ready)", err=True)


@review_app.command("reject")
def review_reject(
    record_id: str,
    reason: Annotated[str, typer.Option(help="Why (one line). Stored with the archived record.")],
    yes: YesOpt = False,
    data_dir: DataDirOption = None,
) -> None:
    """Reject a proposal: it is archived under _rejected/ and no longer searchable."""
    settings = _settings(data_dir)
    reviewer = MemoryReviewer(settings.data_dir)
    with _cli_errors():
        archived = reviewer.find_archived(record_id)
        if archived is not None and not any(
            r.id == record_id.strip().upper() for r in reviewer.list_pending()
        ):
            typer.echo(f"already archived -> {archived}")
            return
        plan = reviewer.plan_accept(record_id, accepted_by=_whoami())
        r = plan.record
        typer.echo(f"{r.id} [{r.status}] {r.title}")
        if not yes and not typer.confirm("Reject and archive it?", default=False):
            typer.echo("cancelled; nothing changed")
            raise typer.Exit(code=1)
        location = reviewer.reject(record_id, reason)
    _audit_write(settings, "reject_record", r.project, {"id": r.id, "type": r.type})
    typer.echo(f"archived {r.id} -> {location}")
    typer.echo("(written to disk only; commit your data repo when you are ready)", err=True)
