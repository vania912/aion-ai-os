"""``aios`` command-line interface."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import typer

from aios import __version__
from aios.config import resolve_settings
from aios.domain.git import ChangedFile
from aios.domain.models import Registry, Severity
from aios.errors import AiosError
from aios.services.files import FileService, build_file_service
from aios.services.git import GitService, build_git_service
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
    """Check the registry against the filesystem. Exit code 1 if any error is found."""
    registry = _load(data_dir)
    issues = check_registry(registry)
    for issue in issues:
        typer.echo(f"{issue.severity.value}\t{issue.project_id}\t{issue.code}\t{issue.message}")
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
