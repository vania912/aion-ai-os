from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from aios.adapters.code_search import PythonSearcher, RipgrepSearcher
from aios.domain.files import FilesConfig, Searcher
from aios.errors import InvalidRequestError, PolicyViolation, SearchError
from aios.policy import PathGuard
from aios.services.files import FileService

HAS_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")

TOKEN = "SECRET_TOKEN_XYZ"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        f"import os\n# find me: Workspace mismatch\nx = '{TOKEN}'\n"
    )
    (root / "src" / "pkg" / "util.py").write_text("def workspace():\n    return 1\n")
    (root / "src" / "ui.ts").write_text("export const workspace = 1;\n")
    (root / "README").write_text("hello workspace\n")
    # Things that must never surface, even though they contain the token:
    (root / ".env").write_text(f"KEY={TOKEN}\n")
    (root / "server.pem").write_text(f"{TOKEN}\n")
    (root / ".git" / "config").write_text(f"token={TOKEN}\n")
    (root / "node_modules" / "dep" / "index.js").write_text(f"{TOKEN}\n")
    (root / "blob.txt").write_bytes(b"\x00\x01" + TOKEN.encode())
    (root / "image.png").write_bytes(TOKEN.encode())
    (root / "huge.txt").write_text(TOKEN + "\n" + "x" * 2_000_000)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside"
    out.mkdir()
    (out / "leak.txt").write_text(f"{TOKEN}\n")
    return out


def _engines() -> list[Searcher]:
    engines: list[Searcher] = [PythonSearcher()]
    rg = RipgrepSearcher.find()
    if rg is not None:
        engines.append(rg)
    return engines


@pytest.fixture(params=_engines(), ids=lambda s: s.name)
def service(request: pytest.FixtureRequest, project: Path) -> FileService:
    return FileService(PathGuard(project), request.param)


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")


# -- list ---------------------------------------------------------------------------------


def test_list_root_hides_denied_ignored_and_disallowed(service: FileService) -> None:
    listing = service.list_files()
    names = [e.path for e in listing.entries]
    assert names == ["src", "README", "blob.txt", "huge.txt"][:1] + [n for n in names if n != "src"]
    for hidden in (".env", ".git", "server.pem", "node_modules", "image.png"):
        assert hidden not in names
    assert "src" in names and "README" in names
    assert not listing.truncated


def test_list_orders_dirs_first_then_files_case_insensitively(service: FileService) -> None:
    kinds = [e.kind for e in service.list_files().entries]
    assert kinds == sorted(kinds)  # "dir" < "file"


def test_list_depth_and_subpath(service: FileService) -> None:
    deep = {e.path for e in service.list_files(depth=3).entries}
    assert {"src", "src/app.py", "src/pkg", "src/pkg/util.py"} <= deep
    sub = service.list_files("src")
    assert sub.path == "src"
    assert {e.path for e in sub.entries} == {"src/app.py", "src/pkg", "src/ui.ts"}


def test_list_truncates(service: FileService) -> None:
    listing = service.list_files(depth=3, max_entries=2)
    assert len(listing.entries) == 2 and listing.truncated


@pytest.mark.parametrize("kwargs", [{"depth": 0}, {"depth": 99}, {"max_entries": 0}])
def test_list_validates_arguments(service: FileService, kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequestError):
        service.list_files(**kwargs)


@pytest.mark.parametrize("path", ["../", ".git", "src/app.py"])
def test_list_rejects_escape_denied_and_files(service: FileService, path: str) -> None:
    with pytest.raises(PolicyViolation):
        service.list_files(path)


def test_list_hides_symlink_to_outside(service: FileService, project: Path, outside: Path) -> None:
    _symlink(project / "link.txt", outside / "leak.txt")
    _symlink(project / "extdir", outside)
    names = {e.path for e in service.list_files().entries}
    assert "link.txt" not in names and "extdir" not in names


# -- read ---------------------------------------------------------------------------------


def test_read_whole_file_and_range(service: FileService) -> None:
    whole = service.read_file("src/app.py")
    assert whole.total_lines == 3 and whole.start_line == 1 and whole.end_line == 3
    assert not whole.truncated
    part = service.read_file("src/app.py", start_line=2, end_line=2)
    assert part.content == "# find me: Workspace mismatch"
    assert (part.start_line, part.end_line, part.total_lines) == (2, 2, 3)


def test_read_end_line_past_eof_is_clamped(service: FileService) -> None:
    assert service.read_file("src/app.py", 3, 99).end_line == 3


@pytest.mark.parametrize(
    "kwargs", [{"start_line": 0}, {"start_line": 3, "end_line": 2}, {"start_line": 50}]
)
def test_read_validates_ranges(service: FileService, kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequestError):
        service.read_file("src/app.py", **kwargs)


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (".env", "denied_name"),
        (".git/config", "denied_dir"),
        ("server.pem", "denied_name"),
        ("image.png", "extension_not_allowed"),
        ("huge.txt", "too_large"),
        ("blob.txt", "binary_file"),
        ("../etc/passwd", "path_escape"),
        ("missing.py", "not_found"),
        ("src", "not_a_file"),
    ],
)
def test_read_refusals(service: FileService, path: str, code: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        service.read_file(path)
    assert info.value.code == code


def test_read_handles_bom_and_crlf(service: FileService, project: Path) -> None:
    (project / "win.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    content = service.read_file("win.txt")
    assert content.content == "one\ntwo" and content.total_lines == 2


def test_read_truncates_by_chars(project: Path) -> None:
    svc = FileService(PathGuard(project), PythonSearcher(), FilesConfig(max_output_chars=30))
    (project / "long.txt").write_text("\n".join(f"line {i:03d}" for i in range(50)))
    result = svc.read_file("long.txt")
    assert result.truncated and result.end_line < 50 and len(result.content) <= 30
    assert result.total_lines == 50


def test_read_truncates_single_oversized_line(project: Path) -> None:
    svc = FileService(PathGuard(project), PythonSearcher(), FilesConfig(max_output_chars=10))
    (project / "one.txt").write_text("x" * 100)
    result = svc.read_file("one.txt")
    assert result.truncated and len(result.content) == 10


def test_read_empty_file(service: FileService, project: Path) -> None:
    (project / "empty.txt").write_text("")
    result = service.read_file("empty.txt")
    assert (result.content, result.total_lines, result.end_line) == ("", 0, 0)


# -- search: behaviour shared by both engines ---------------------------------------------


def test_search_finds_literal_case_insensitively_in_order(service: FileService) -> None:
    result = service.search("workspace")
    assert [(m.path, m.line) for m in result.matches] == [
        ("README", 1),
        ("src/app.py", 2),
        ("src/pkg/util.py", 1),
        ("src/ui.ts", 1),
    ]
    assert result.engine == service.engine and not result.truncated


def test_search_case_sensitive(service: FileService) -> None:
    result = service.search("Workspace", ignore_case=False)
    assert [m.path for m in result.matches] == ["src/app.py"]


def test_search_never_returns_protected_content(
    service: FileService, project: Path, outside: Path
) -> None:
    _symlink(project / "link.txt", outside / "leak.txt")
    _symlink(project / "extdir", outside)
    _symlink(project / "toenv.txt", project / ".env")
    result = service.search(TOKEN)
    assert [m.path for m in result.matches] == ["src/app.py"]


def test_search_glob_and_subpath(service: FileService) -> None:
    assert [m.path for m in service.search("workspace", glob="*.py").matches] == [
        "src/app.py",
        "src/pkg/util.py",
    ]
    assert [m.path for m in service.search("workspace", glob="src/*.ts").matches] == ["src/ui.ts"]
    assert [m.path for m in service.search("workspace", path="src/pkg").matches] == [
        "src/pkg/util.py"
    ]


def test_search_truncation_flag(service: FileService) -> None:
    result = service.search("workspace", max_results=2)
    assert len(result.matches) == 2 and result.truncated
    exact = service.search("workspace", max_results=4)
    assert len(exact.matches) == 4 and not exact.truncated


def test_search_no_match(service: FileService) -> None:
    result = service.search("definitely-not-present")
    assert result.matches == [] and not result.truncated


def test_search_query_starting_with_dash_is_literal(service: FileService, project: Path) -> None:
    (project / "flags.txt").write_text("use --files and -e foo\n")
    assert [m.path for m in service.search("--files").matches] == ["flags.txt"]
    assert [m.path for m in service.search("-e foo").matches] == ["flags.txt"]


@pytest.mark.parametrize("query", ["", "x" * 501, "a\x00b", "a\nb"])
def test_search_rejects_bad_queries(service: FileService, query: str) -> None:
    with pytest.raises(InvalidRequestError):
        service.search(query)


@pytest.mark.parametrize(
    "glob",
    ["", "-g", "--hidden", "!*.py", "/etc/*", "../*", "a/../b", "C:/x", "x" * 300, "a\nb"],
)
def test_search_rejects_bad_globs(service: FileService, glob: str) -> None:
    with pytest.raises(InvalidRequestError):
        service.search("workspace", glob=glob)


@pytest.mark.parametrize("path", ["../", ".git", ".env"])
def test_search_start_path_is_vetted(service: FileService, path: str) -> None:
    with pytest.raises(PolicyViolation):
        service.search("x", path=path)


def test_search_is_deterministic(service: FileService) -> None:
    first = service.search("workspace")
    assert all(service.search("workspace") == first for _ in range(3))


def test_search_clips_long_lines(project: Path) -> None:
    (project / "wide.txt").write_text("needle " + "y" * 1000 + "\n")
    for searcher in _engines():
        match = FileService(PathGuard(project), searcher).search("needle").matches[0]
        assert len(match.text) <= FilesConfig().max_line_chars + 1


# -- engine specifics ---------------------------------------------------------------------


def test_python_engine_refuses_regex(project: Path) -> None:
    svc = FileService(PathGuard(project), PythonSearcher())
    with pytest.raises(SearchError, match="requires ripgrep"):
        svc.search("work.*", regex=True)


@needs_rg
def test_ripgrep_regex(project: Path) -> None:
    svc = FileService(PathGuard(project), RipgrepSearcher.find())
    assert [m.path for m in svc.search(r"def \w+\(\)", regex=True).matches] == ["src/pkg/util.py"]


@needs_rg
def test_ripgrep_pathological_regex_is_bounded(project: Path) -> None:
    (project / "evil.txt").write_text("a" * 5000 + "!\n")
    svc = FileService(PathGuard(project), RipgrepSearcher.find())
    assert svc.search(r"(a+)+$", regex=True).matches == []  # Rust regex: linear time


@needs_rg
def test_ripgrep_ignores_user_config(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "rgrc"
    cfg.write_text("--glob=!*.ts\n")  # would hide src/ui.ts if the config were honoured
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(cfg))
    svc = FileService(PathGuard(project), RipgrepSearcher.find())
    assert "src/ui.ts" in [m.path for m in svc.search("workspace").matches]


posix_only = pytest.mark.skipif(os.name != "posix", reason="uses a POSIX shell script as a fake rg")


def _fake_rg(tmp_path: Path, body: str) -> RipgrepSearcher:
    script = tmp_path / "fake_rg"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return RipgrepSearcher(str(script))


@posix_only
def test_ripgrep_timeout_kills_the_process(project: Path, tmp_path: Path) -> None:
    svc = FileService(
        PathGuard(project), _fake_rg(tmp_path, "exec sleep 30"), FilesConfig(search_timeout_s=0.3)
    )
    start = time.monotonic()
    with pytest.raises(SearchError, match="timed out"):
        svc.search("workspace")
    assert time.monotonic() - start < 10


@posix_only
def test_ripgrep_failure_is_reported_without_leaking_the_root(
    project: Path, tmp_path: Path
) -> None:
    body = f"echo 'error: cannot read {project}/secret' >&2\nexit 2"
    svc = FileService(PathGuard(project), _fake_rg(tmp_path, body))
    with pytest.raises(SearchError) as info:
        svc.search("workspace")
    assert "ripgrep failed" in str(info.value)
    assert str(project) not in str(info.value) and "<root>" in str(info.value)


@posix_only
def test_ripgrep_no_match_exit_code_is_not_an_error(project: Path, tmp_path: Path) -> None:
    svc = FileService(PathGuard(project), _fake_rg(tmp_path, "exit 1"))
    assert svc.search("workspace").matches == []


def test_ripgrep_is_required_in_ci_when_requested() -> None:
    if os.environ.get("AIOS_REQUIRE_RG") == "1":
        assert HAS_RG, "AIOS_REQUIRE_RG=1 but ripgrep is not installed"


def test_python_engine_timeout(project: Path) -> None:
    svc = FileService(PathGuard(project), PythonSearcher(), FilesConfig(search_timeout_s=-1.0))
    with pytest.raises(SearchError, match="timed out"):
        svc.search("workspace")
