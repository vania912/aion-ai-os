from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest

from aios.errors import ConfigError, PolicyViolation
from aios.policy import FileAccessPolicy, PathGuard, looks_binary


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[remote]\n url = https://token@example.com/x\n")
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README").write_text("readme\n")
    (root / "Makefile").write_text("all:\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / ".env.example").write_text("SECRET=\n")
    (root / "server.pem").write_text("-----BEGIN-----\n")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside"
    out.mkdir()
    (out / "secret.txt").write_text("outside\n")
    return out


@pytest.fixture
def guard(project: Path) -> PathGuard:
    return PathGuard(project)


def _code(guard: PathGuard, path: str) -> str:
    with pytest.raises(PolicyViolation) as info:
        guard.check_readable_file(path)
    return info.value.code


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")


# -- happy path ---------------------------------------------------------------------------


def test_reads_allowed_files(guard: PathGuard, project: Path) -> None:
    assert guard.check_readable_file("src/app.py") == (project / "src" / "app.py").resolve()
    assert guard.check_readable_file("README").name == "README"
    assert guard.check_readable_file("Makefile").name == "Makefile"
    assert guard.check_readable_file("src\\app.py").name == "app.py"  # backslash separators


@pytest.mark.parametrize("path", ["", ".", "./", "src/."])
def test_root_and_dot_resolve_to_directories(guard: PathGuard, path: str) -> None:
    assert guard.resolve(path).is_dir()


def test_extension_check_is_case_insensitive(guard: PathGuard, project: Path) -> None:
    (project / "MAIN.PY").write_text("x = 1\n")
    assert guard.check_readable_file("MAIN.PY").name == "MAIN.PY"


# -- confinement --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../x", "a/../../x", "..", "src/..", "src/../app.py", "..\\x", "src/..\\..\\x"],
)
def test_parent_traversal_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "path_escape"


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "\\\\server\\share\\x", "\\x", "C:/Windows/win.ini", "C:\\x", "c:x"],
)
def test_absolute_paths_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "absolute_path"


@pytest.mark.parametrize("path", ["file.txt:hidden", "a.py::$DATA", "src/a:b"])
def test_colon_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "invalid_path"


@pytest.mark.parametrize("path", ["a\x00b", "a\nb", "a\tb", "a\x7fb", "x" * 5000])
def test_control_chars_and_overlong_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "invalid_path"


def test_url_encoding_is_not_decoded(guard: PathGuard) -> None:
    # "%2e%2e" is an ordinary (nonexistent) name, not "..": nothing is decoded.
    assert _code(guard, "%2e%2e/app.py") == "not_found"


@pytest.mark.parametrize("path", ["secret.pem.", "x. ", "dir /a.py", "a.py "])
def test_trailing_dot_or_space_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "invalid_path"


@pytest.mark.parametrize("path", ["nul", "NUL.txt", "con", "aux.py", "com1", "LPT9.md", "src/prn"])
def test_reserved_device_names_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "invalid_path"


@pytest.mark.parametrize("path", ["PROGRA~1", "CREDEN~1.JSO", "src/SECRET~2.PEM"])
def test_short_name_aliases_rejected(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "invalid_path"


def test_names_that_merely_contain_reserved_words_are_fine(guard: PathGuard, project: Path) -> None:
    (project / "console.py").write_text("x = 1\n")
    (project / "communication.md").write_text("x\n")
    assert guard.check_readable_file("console.py")
    assert guard.check_readable_file("communication.md")


# -- deny-list ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".ENV",
        ".env.local",
        "config/.env",
        "prod.env",
        "server.pem",
        "KEY.PEM",
        "id_rsa",
        "id_ed25519.pub",
        "credentials.json",
        ".netrc",
        "terraform.tfstate",
        "prod.tfvars",
        "service-account-prod.json",
    ],
)
def test_secret_names_denied(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "denied_name"


@pytest.mark.parametrize(
    "path", [".git/config", "sub/.git/HEAD", ".GIT/config", ".ssh/id_rsa", ".aws/credentials"]
)
def test_secret_directories_denied(guard: PathGuard, path: str) -> None:
    with pytest.raises(PolicyViolation) as info:
        guard.resolve(path)
    assert info.value.code == "denied_dir"


def test_env_template_exception(guard: PathGuard) -> None:
    assert guard.check_readable_file(".env.example").name == ".env.example"


def test_source_files_named_like_secrets_are_not_blocked(guard: PathGuard, project: Path) -> None:
    (project / "credentials.py").write_text("def get(): ...\n")
    (project / "secrets.py").write_text("x = 1\n")
    assert guard.check_readable_file("credentials.py")
    assert guard.check_readable_file("secrets.py")


# -- symlinks -----------------------------------------------------------------------------


def test_symlink_to_outside_file_rejected(guard: PathGuard, project: Path, outside: Path) -> None:
    _symlink(project / "link.txt", outside / "secret.txt")
    assert _code(guard, "link.txt") == "path_escape"


def test_symlinked_directory_to_outside_rejected(
    guard: PathGuard, project: Path, outside: Path
) -> None:
    _symlink(project / "ext", outside)
    assert _code(guard, "ext/secret.txt") == "path_escape"


def test_symlink_to_denied_name_inside_root_rejected(guard: PathGuard, project: Path) -> None:
    _symlink(project / "innocent.txt", project / ".env")
    assert _code(guard, "innocent.txt") == "denied_name"


def test_symlink_into_denied_directory_rejected(guard: PathGuard, project: Path) -> None:
    _symlink(project / "gitcfg.txt", project / ".git" / "config")
    assert _code(guard, "gitcfg.txt") == "denied_dir"


def test_symlink_to_allowed_file_inside_root_ok(guard: PathGuard, project: Path) -> None:
    _symlink(project / "alias.py", project / "src" / "app.py")
    assert guard.check_readable_file("alias.py") == (project / "src" / "app.py").resolve()


def test_error_messages_do_not_leak_absolute_paths(
    guard: PathGuard, project: Path, outside: Path
) -> None:
    _symlink(project / "link.txt", outside / "secret.txt")
    with pytest.raises(PolicyViolation) as info:
        guard.resolve("link.txt")
    assert str(outside) not in str(info.value)
    assert str(project) not in str(info.value)


# -- file checks --------------------------------------------------------------------------


def test_extension_allow_list(guard: PathGuard, project: Path) -> None:
    (project / "data.csv").write_text("a,b\n")
    (project / "archive.tar.gz").write_bytes(b"x")
    for name in ("logo.png", "data.csv", "archive.tar.gz"):
        assert _code(guard, name) == "extension_not_allowed"


def test_not_found_and_not_a_file(guard: PathGuard) -> None:
    assert _code(guard, "nope.py") == "not_found"
    assert _code(guard, "src") == "not_a_file"


def test_size_cap_boundary(project: Path) -> None:
    guard = PathGuard(project, FileAccessPolicy(max_file_bytes=10))
    (project / "exact.txt").write_bytes(b"x" * 10)
    (project / "over.txt").write_bytes(b"x" * 11)
    assert guard.check_readable_file("exact.txt")
    assert _code(guard, "over.txt") == "too_large"


def test_policy_is_extensible(project: Path) -> None:
    policy = FileAccessPolicy(
        denied_name_patterns=(*FileAccessPolicy().denied_name_patterns, "internal-*.md")
    )
    (project / "internal-notes.md").write_text("x\n")
    with pytest.raises(PolicyViolation) as info:
        PathGuard(project, policy).resolve("internal-notes.md")
    assert info.value.code == "denied_name"


def test_is_visible_filters_listings(guard: PathGuard) -> None:
    assert guard.is_visible("src")
    assert guard.is_visible("src/app.py")
    for hidden in (".env", ".git", ".git/config", "server.pem", "logo.png", "../x", "nope.py"):
        assert not guard.is_visible(hidden), hidden


def test_to_relative_is_posix_style(guard: PathGuard) -> None:
    assert guard.to_relative(guard.resolve("src/app.py")) == "src/app.py"


def test_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        PathGuard(tmp_path / "missing")
    file = tmp_path / "f.txt"
    file.write_text("x")
    with pytest.raises(ConfigError):
        PathGuard(file)


def test_looks_binary() -> None:
    assert looks_binary(b"abc\x00def")
    assert not looks_binary("héllo".encode())
    assert not looks_binary(b"a" * 9000 + b"\x00")  # only the leading sample is inspected


# -- invariant fuzz -----------------------------------------------------------------------

_TOKENS = ["a", "src", "..", ".", "", ".env", ".git", "ext", "B.PEM", "x:y", "nul", "app.py", "e~1"]


def test_resolve_never_yields_a_path_outside_root_or_a_denied_target(
    guard: PathGuard, project: Path, outside: Path
) -> None:
    _symlink(project / "ext", outside)
    root = guard.root
    checked = 0
    for length in (1, 2, 3):
        for combo in itertools.product(_TOKENS, repeat=length):
            candidate = "/".join(combo)
            try:
                resolved = guard.resolve(candidate)
            except PolicyViolation:
                continue
            checked += 1
            assert resolved == root or root in resolved.parents, candidate
            parts = [p.lower() for p in resolved.relative_to(root).parts]
            assert ".git" not in parts, candidate
            # Name patterns apply to the final component only (a directory called ".env" is a
            # common virtualenv name); a denied *file* can never be reached as a parent.
            assert parts[-1:] not in ([".env"], ["b.pem"]), candidate
    assert checked > 0
