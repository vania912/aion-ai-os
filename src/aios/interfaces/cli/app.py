"""``aios`` command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aios import __version__
from aios.config import resolve_settings
from aios.domain.models import Registry, Severity
from aios.errors import AiosError
from aios.services.registry import check_registry, load_registry

app = typer.Typer(
    name="aios",
    help="Personal AI operating layer: inspect projects and memory without Claude.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Registered projects.", no_args_is_help=True)
app.add_typer(project_app, name="project")

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
