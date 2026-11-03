"""``aios`` command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aios import __version__
from aios.config import resolve_settings
from aios.domain.models import Registry, Severity
from aios.errors import AiosError
from aios.services.files import FileService, build_file_service
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
