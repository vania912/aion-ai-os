"""Hardened git runner for the few *writing* operations Phase 4 needs.

Unlike :class:`aios.adapters.git_cli.GitRunner` this one is not a pass-through: callers use typed
methods (``worktree_add``, ``worktree_remove``, ``branch_delete``, ``changes``...) and the argument
lists are built here from validated values. There is no way to hand it a free-form git command.

Why this is hardened (each measure has a hostile-repository test, see tests/unit/test_git_exec.py):

* A plain ``git worktree add`` runs the repository's own hooks and smudge/clean/process filters
  (observed 2026-11-04). Creating a worktree would execute project-controlled code. So hooks are
  pointed at an empty directory, and every ``filter.<name>.{smudge,clean,process,required}`` found
  in the repository's configuration (including files pulled in by ``include.path``) is overridden
  on the command line.
* System and user git configuration are not read (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL``
  pointing at an empty file), and every ``GIT_*`` variable from the parent is dropped.
* ``core.fsmonitor`` is off; diffs never use external diff programs or textconv; submodule
  recursion, auto-gc and network protocols are disabled; stdin is closed; calls have a timeout and
  an output cap.
* Only branches named ``aios/PLAN-nnnn`` are created or deleted, only worktrees that are registered
  with such a branch are removed, and the worktree path must be outside the repository.
* Taking a snapshot of a worktree's changes never touches the worktree's own index: it works on a
  temporary copy (``GIT_INDEX_FILE``).

What it does not do: push, fetch, commit, merge, check out in the main working tree, or run any
project code on purpose. Residual risk: git features not covered by the tests.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import weakref
from collections.abc import Iterator, Sequence
from pathlib import Path

from aios.adapters.git_cli import GitOutput, run_process
from aios.domain.gitwrite import Change, WorktreeChanges, WorktreeInfo
from aios.domain.plans import BRANCH_RE, SHA_RE
from aios.errors import GitError

_REV_BAD = re.compile(r"[\s:\x00-\x1f\x7f~^?*\[\\]|\.\.|@\{")
_FILTER_KEY = re.compile(
    r"^filter\.(.+)\.(smudge|clean|process|required)\Z", re.IGNORECASE | re.DOTALL
)
_STATUS = frozenset("AMDT")


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class GitWriteRunner:
    def __init__(
        self,
        root: Path,
        *,
        executable: str | None = None,
        timeout_s: float = 120.0,
        max_diff_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        exe = executable or shutil.which("git")
        if exe is None:
            raise GitError("git executable not found on PATH")
        self._exe = exe
        self._root = root.resolve()
        self._timeout_s = timeout_s
        self._max_diff_bytes = max_diff_bytes
        # a private scratch area: empty hooks directory, empty global config, temp indexes
        self._scratch = Path(tempfile.mkdtemp(prefix="aios-git-"))
        weakref.finalize(self, _cleanup, str(self._scratch))
        self._hooks = self._scratch / "hooks"
        self._hooks.mkdir()
        self._empty_config = self._scratch / "gitconfig"
        self._empty_config.write_bytes(b"")
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(self._empty_config),
                "XDG_CONFIG_HOME": str(self._scratch / "xdg"),
            }
        )
        self._env = env

    # -- the one place a git process is started -------------------------------------------

    def _static_config(self) -> list[str]:
        settings = {
            "core.hooksPath": self._hooks.as_posix(),
            "core.fsmonitor": "false",
            "core.quotepath": "off",
            "core.longpaths": "true",
            "color.ui": "never",
            "gc.auto": "0",
            "maintenance.auto": "false",
            "submodule.recurse": "false",
            "protocol.allow": "never",
            "credential.helper": "",
            "diff.renames": "false",
            "diff.noprefix": "false",
            "diff.mnemonicPrefix": "false",
            "advice.detachedHead": "false",
        }
        out: list[str] = []
        for key, value in settings.items():
            out += ["-c", f"{key}={value}"]
        return out

    def _filter_overrides(self, cwd: Path) -> list[str]:
        """Override every configured filter, wherever it is defined in the repository's config."""
        argv = [self._exe, "--no-pager", "-c", "core.fsmonitor=false", "config", "--null",
                "--get-regexp", r"^filter\."]  # fmt: skip
        out = run_process(
            argv, cwd=cwd, env=self._env, timeout_s=self._timeout_s, cap=1_000_000,
            ok_codes=(0, 1), label="config",
        )  # fmt: skip
        names: set[str] = set()
        for entry in out.stdout.split(b"\x00"):
            key = entry.split(b"\n", 1)[0].decode("utf-8", errors="replace")
            match = _FILTER_KEY.match(key)
            if match:
                names.add(match.group(1))
        overrides: list[str] = []
        for name in sorted(names):
            if "=" in name or "\n" in name or "\x00" in name:
                raise GitError(f"cannot neutralise the git filter named {name!r}")
            overrides += ["-c", f"filter.{name}.process="]
            overrides += ["-c", f"filter.{name}.smudge=cat", "-c", f"filter.{name}.clean=cat"]
            overrides += ["-c", f"filter.{name}.required=false"]
        return overrides

    def _git(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        index_file: Path | None = None,
        cap: int = 2_000_000,
        ok_codes: Sequence[int] = (0,),
    ) -> GitOutput:
        for arg in args:
            if "\x00" in arg:
                raise GitError("NUL byte in git argument")
        env = dict(self._env)
        env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
        if index_file is not None:
            env["GIT_INDEX_FILE"] = str(index_file)
        argv = [
            self._exe,
            "--no-pager",
            "--no-optional-locks",
            *self._static_config(),
            *self._filter_overrides(cwd),
            *args,
        ]
        return run_process(
            argv, cwd=cwd, env=env, timeout_s=self._timeout_s, cap=cap, ok_codes=ok_codes,
            label=args[0], redact={str(self._root): "<root>"},
        )  # fmt: skip

    # -- validation -----------------------------------------------------------------------

    @staticmethod
    def _check_branch(branch: str) -> None:
        if not BRANCH_RE.match(branch):
            raise GitError(f"branch name must look like aios/PLAN-0001, not {branch!r}")

    def _check_worktree_path(self, path: Path) -> Path:
        if not path.is_absolute():
            raise GitError("the worktree path must be absolute")
        if any(part in ("..", ".") for part in path.parts) or "\x00" in str(path):
            raise GitError("the worktree path must not contain '.' or '..'")
        resolved = path.resolve()
        if resolved == self._root or self._root in resolved.parents:
            raise GitError("the worktree must be outside the project directory")
        if resolved in self._root.parents:
            raise GitError("the worktree must not contain the project directory")
        return resolved

    # -- queries --------------------------------------------------------------------------

    def resolve_commit(self, rev: str) -> str:
        """The full object id of a commit, or ``GitError``. Refuses option-like and range syntax."""
        if not rev or rev.startswith("-") or _REV_BAD.search(rev):
            raise GitError(f"not an acceptable revision: {rev!r}")
        out = self._git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"],
            cwd=self._root,
            ok_codes=(0, 1),
        )
        sha = out.stdout.decode("ascii", errors="replace").strip()
        if out.returncode != 0 or not SHA_RE.match(sha):
            raise GitError(f"no such commit: {rev!r}")
        return sha

    def worktree_list(self) -> list[WorktreeInfo]:
        out = self._git(["worktree", "list", "--porcelain"], cwd=self._root).stdout
        text = out.decode("utf-8", errors="replace")
        found: list[WorktreeInfo] = []
        for block in text.split("\n\n"):
            path = head = branch = None
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = line[len("worktree ") :]
                elif line.startswith("HEAD "):
                    head = line[len("HEAD ") :]
                elif line.startswith("branch "):
                    branch = line[len("branch ") :].removeprefix("refs/heads/")
            if path:
                found.append(WorktreeInfo(path=path, head=head, branch=branch))
        return found

    def _registered(self, path: Path) -> WorktreeInfo:
        """The registered aios worktree at ``path``, or ``GitError``. Never the main worktree."""
        target = os.path.normcase(str(path.resolve()))
        listed = self.worktree_list()
        for index, info in enumerate(listed):
            if os.path.normcase(str(Path(info.path).resolve())) != target:
                continue
            if index == 0:
                raise GitError("that is the main working tree, not an aios worktree")
            if info.branch is None or not BRANCH_RE.match(info.branch):
                raise GitError("that worktree was not created by aios")
            return info
        raise GitError("no aios worktree is registered at that path")

    # -- operations -----------------------------------------------------------------------

    def worktree_add(self, path: Path, branch: str, base_sha: str) -> WorktreeInfo:
        """Create ``branch`` at ``base_sha`` in a new worktree at ``path`` (must not exist)."""
        self._check_branch(branch)
        if not SHA_RE.match(base_sha):
            raise GitError("the base must be a full commit id")
        target = self._check_worktree_path(path)
        if target.exists():
            raise GitError("the worktree path already exists")
        if not target.parent.is_dir():
            raise GitError("the parent directory of the worktree does not exist")
        self._git(
            ["worktree", "add", "--quiet", "-b", branch, "--", str(target), base_sha],
            cwd=self._root,
        )
        return self._registered(target)

    def worktree_remove(self, path: Path) -> None:
        """Remove an aios worktree (and its files). Refuses anything not registered by aios."""
        info = self._registered(path)
        self._git(["worktree", "remove", "--force", "--", info.path], cwd=self._root)

    def worktree_prune(self) -> None:
        """Forget worktrees whose directory is gone (git's own bookkeeping; deletes no files)."""
        self._git(["worktree", "prune"], cwd=self._root)

    def branch_delete(self, branch: str) -> None:
        self._check_branch(branch)
        self._git(["branch", "-D", "--", branch], cwd=self._root)

    def current_branch(self) -> str | None:
        """The checked-out branch of the project's main working tree (None if detached)."""
        out = self._git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=self._root, ok_codes=(0, 1))
        name = out.stdout.decode("utf-8", errors="replace").strip()
        return name if out.returncode == 0 and name else None

    def dirty_paths(self) -> list[str]:
        """Paths with uncommitted changes (or untracked) in the project's main working tree."""
        out = self._git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self._root,
            cap=self._max_diff_bytes,
        )
        entries = out.stdout.split(b"\x00")
        paths: list[str] = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            paths.append(_decode(entry[3:]))
            if entry[:1] in (b"R", b"C"):  # a rename lists the old path as the next field
                i += 1
        return sorted(paths)

    def branches(self) -> list[str]:
        """Local branches named like aios plan branches."""
        out = self._git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/aios/"], cwd=self._root
        )
        names = out.stdout.decode("utf-8", errors="replace").split("\n")
        return sorted(n.strip() for n in names if BRANCH_RE.match(n.strip()))

    def branch_exists(self, branch: str) -> bool:
        self._check_branch(branch)
        out = self._git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self._root,
            ok_codes=(0, 1),
        )
        return out.returncode == 0

    @contextlib.contextmanager
    def _intent_to_add_index(self, cwd: Path, base_sha: str) -> Iterator[Path]:
        """A temporary index built fresh from ``base_sha`` with every untracked, non-ignored file
        added intent-to-add. The worktree's own index is never read or written.

        Building it from the base commit (not copying the worktree's index) matters: a copy has a
        newer timestamp than its entries, so git would trust the recorded size and mtime of files
        edited right after checkout and miss the edit (seen on Windows, whose clock is coarse). An
        index made by ``read-tree`` has no stat data, so git compares every file's content.
        """
        fd, tmp_name = tempfile.mkstemp(prefix="index-", dir=self._scratch)
        os.close(fd)
        tmp_index = Path(tmp_name)
        tmp_index.unlink()  # git creates it
        try:
            self._git(["read-tree", base_sha], cwd=cwd, index_file=tmp_index)
            self._git(["add", "--intent-to-add", "--all", "--", "."], cwd=cwd, index_file=tmp_index)
            yield tmp_index
        finally:
            tmp_index.unlink(missing_ok=True)

    def worktree_head(self, worktree: Path) -> str:
        """The commit a registered aios worktree currently has checked out."""
        info = self._registered(worktree)
        out = self._git(["rev-parse", "--verify", "HEAD"], cwd=Path(info.path))
        sha = out.stdout.decode("ascii", errors="replace").strip()
        if not SHA_RE.match(sha):
            raise GitError("could not read the worktree's HEAD")
        return sha

    def diff_text(
        self, worktree: Path, base_sha: str, *, max_bytes: int = 1_000_000
    ) -> tuple[bytes, bool]:
        """The textual diff of everything changed since ``base_sha`` (capped), and whether it was
        cut. Binary files appear as git's 'Binary files differ' line."""
        if not SHA_RE.match(base_sha):
            raise GitError("the base must be a full commit id")
        cwd = Path(self._registered(worktree).path)
        common = ["--no-renames", "--no-ext-diff", "--no-textconv", "--no-color"]
        with self._intent_to_add_index(cwd, base_sha) as index:
            out = self._git(
                ["diff", "--src-prefix=a/", "--dst-prefix=b/", *common, base_sha],
                cwd=cwd, index_file=index, cap=max_bytes,
            )  # fmt: skip
        return out.stdout, out.truncated

    def changes(self, worktree: Path, base_sha: str) -> WorktreeChanges:
        """Everything that differs from ``base_sha`` in an aios worktree, plus a hash of the diff.

        Untracked files are included by adding them *intent-to-add* to a temporary copy of the
        index, so the worktree itself is left exactly as it was.
        """
        if not SHA_RE.match(base_sha):
            raise GitError("the base must be a full commit id")
        cwd = Path(self._registered(worktree).path)
        common = ["--no-renames", "--no-ext-diff", "--no-textconv", "--no-color"]
        cap = self._max_diff_bytes
        with self._intent_to_add_index(cwd, base_sha) as index:
            raw = self._git(
                ["diff", "--raw", "-z", "--full-index", *common, base_sha],
                cwd=cwd, index_file=index, cap=cap,
            )  # fmt: skip
            numstat = self._git(
                ["diff", "--numstat", "-z", *common, base_sha], cwd=cwd, index_file=index, cap=cap
            )
            patch = self._git(
                ["diff", "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/",
                 *common, base_sha],
                cwd=cwd, index_file=index, cap=cap,
            )  # fmt: skip
            ignored = self._git(
                ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                cwd=cwd, index_file=index, cap=cap,
            )  # fmt: skip
        if raw.truncated or numstat.truncated or patch.truncated or ignored.truncated:
            raise GitError(f"the change is larger than {self._max_diff_bytes} bytes")
        binary = _parse_numstat(numstat.stdout)
        changes = _parse_raw(raw.stdout, binary)
        listed_ignored = sorted(_decode(p) for p in ignored.stdout.split(b"\x00") if p)
        return WorktreeChanges(
            base=base_sha,
            changes=changes,
            ignored=listed_ignored,
            diff_sha256=hashlib.sha256(patch.stdout).hexdigest(),
            diff_bytes=len(patch.stdout),
        )


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _parse_numstat(data: bytes) -> set[str]:
    """Paths git treats as binary (numstat prints '-' for both counts). Renames are disabled."""
    binary: set[str] = set()
    for entry in data.split(b"\x00"):
        parts = entry.split(b"\t", 2)
        if len(parts) == 3 and parts[0] == b"-" and parts[1] == b"-":
            binary.add(_decode(parts[2]))
    return binary


def _parse_raw(data: bytes, binary: set[str]) -> list[Change]:
    fields = data.split(b"\x00")
    changes: list[Change] = []
    i = 0
    while i + 1 < len(fields):
        meta = fields[i].decode("ascii", errors="replace")
        if not meta.startswith(":"):
            i += 1
            continue
        parts = meta[1:].split()
        if len(parts) < 5 or parts[4][:1] not in _STATUS:
            raise GitError("unexpected output from git diff")
        path = _decode(fields[i + 1])
        change = Change(
            path=path,
            status=parts[4][:1],
            old_mode=parts[0],
            new_mode=parts[1],
            binary=path in binary,
        )
        changes.append(change)
        i += 2
    return sorted(changes, key=lambda c: c.path)
