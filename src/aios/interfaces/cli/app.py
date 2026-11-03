"""``aios`` command-line interface."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import typer

from aios import __version__
from aios.config import Settings, resolve_settings
from aios.domain.git import ChangedFile
from aios.domain.memory import MemoryHit
from aios.domain.models import Registry, Severity
from aios.errors import AiosError
from aios.services.files import FileService, build_file_service
from aios.services.git import GitService, build_git_service
from aios.services.memory import MemoryService, memory_dir_exists
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


@app.command()
def doctor(data_dir: DataDirOption = None) -> None:
    """Check the registry and the memory store. Exit code 1 if any error is found."""
    settings = _settings(data_dir)
    registry = _load(data_dir)
    issues = check_registry(registry)
    if memory_dir_exists(settings.data_dir):
        issues += MemoryService(settings.data_dir, registry=registry).check()
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
        typer.echo(f"{h.id}\t{h.status}{note}\t{h.project}\t{h.title}")
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
    limit: int = 20,
    data_dir: DataDirOption = None,
) -> None:
    """Search memory: exact id, then all words, then any word."""
    service = _memory(data_dir)
    with _cli_errors():
        result = service.search(
            query, project=project, record_type=type, status=status, tag=tag, limit=limit
        )
    _echo_hits(result.hits, snippets=True)
    typer.echo(f"[layer={result.layer}{' truncated' if result.truncated else ''}]", err=True)


@memory_app.command("list")
def memory_list(
    project: str | None = None,
    type: Annotated[str | None, typer.Option("--type")] = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    data_dir: DataDirOption = None,
) -> None:
    """List records, newest first."""
    service = _memory(data_dir)
    with _cli_errors():
        result = service.list_records(
            project=project, record_type=type, status=status, tag=tag, limit=limit
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
