"""Hardened, read-only git runner.

Safety measures (each covered by a test; see docs/GIT.md):

* only an allowlist of subcommands runs, and dangerous options are refused before ``--``;
* the environment is rebuilt without any ``GIT_*`` variables from the parent;
* config that can execute programs or write is overridden on the command line
  (``core.fsmonitor``, signature verification, pager, colour);
* ``--no-optional-locks`` keeps ``git status`` from refreshing the index on disk;
* ``GIT_CEILING_DIRECTORIES`` stops repository discovery above the project root;
* stdin is closed, there is a timeout, and stdout is capped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aios.errors import GitError

ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset(
    {"log", "show", "diff", "diff-tree", "status", "rev-parse", "for-each-ref"}
)

# Refused anywhere before a "--" separator. Callers never need these.
_BLOCKED_OPTIONS: frozenset[str] = frozenset(
    {
        "-c", "-C", "--config", "--git-dir", "--work-tree", "--exec-path", "--namespace",
        "--no-index", "--ext-diff", "--textconv", "--paginate", "-p", "--open-files-in-pager",
        "--upload-pack", "--receive-pack", "--super-prefix", "--attr-source",
    }
)  # fmt: skip
_BLOCKED_PREFIXES: tuple[str, ...] = ("--output", "-O", "--git-dir", "--work-tree", "--exec-path")


@dataclass(frozen=True)
class GitOutput:
    stdout: bytes
    truncated: bool
    returncode: int


class GitRunner:
    def __init__(
        self,
        root: Path,
        *,
        executable: str | None = None,
        timeout_s: float = 30.0,
        max_output_bytes: int = 2_000_000,
    ) -> None:
        exe = executable or shutil.which("git")
        if exe is None:
            raise GitError("git executable not found on PATH")
        self._root = root
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._prefix = [
            exe,
            "--no-pager",
            "--no-optional-locks",
            "-c", "core.fsmonitor=false",
            "-c", "core.quotepath=off",
            "-c", "color.ui=never",
            "-c", "log.showSignature=false",
            "-c", "status.renames=false",
            "-c", "diff.renames=false",
            "-c", "i18n.logOutputEncoding=UTF-8",
        ]  # fmt: skip
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_CEILING_DIRECTORIES": str(root.parent),
            }
        )
        self._env = env

    @staticmethod
    def validate(args: Sequence[str]) -> None:
        """Raise ``GitError`` unless ``args`` is an allowlisted, option-safe invocation."""
        if not args or args[0] not in ALLOWED_SUBCOMMANDS:
            name = args[0] if args else ""
            raise GitError(f"git subcommand is not allowed: {name!r}")
        for arg in args[1:]:
            if arg == "--":
                break
            if "\x00" in arg:
                raise GitError("NUL byte in git argument")
            head = arg.split("=", 1)[0]
            if head in _BLOCKED_OPTIONS or arg.startswith(_BLOCKED_PREFIXES):
                raise GitError(f"git option is not allowed: {head!r}")

    def run(
        self,
        args: Sequence[str],
        *,
        ok_codes: Sequence[int] = (0,),
        max_bytes: int | None = None,
    ) -> GitOutput:
        self.validate(args)
        cap = max_bytes if max_bytes is not None else self._max_output_bytes
        timed_out = threading.Event()
        chunks: list[bytes] = []
        total = 0
        truncated = False
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(  # argument list, shell=False
                [*self._prefix, *args],
                cwd=self._root,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
            )

            def _kill() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(self._timeout_s, _kill)
            timer.start()
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    if total + len(chunk) > cap:
                        chunks.append(chunk[: cap - total])
                        truncated = True
                        proc.kill()
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            finally:
                timer.cancel()
                if proc.stdout is not None:
                    proc.stdout.close()
                proc.wait()
            err.seek(0)
            stderr_text = err.read().decode("utf-8", errors="replace")
        if timed_out.is_set():
            raise GitError(f"git timed out after {self._timeout_s:g}s")
        if not truncated and proc.returncode not in ok_codes:
            first = stderr_text.strip().splitlines()[:1]
            detail = first[0].replace(str(self._root), "<root>") if first else "unknown error"
            raise GitError(f"git {args[0]} failed: {detail}")
        return GitOutput(b"".join(chunks), truncated, proc.returncode)
