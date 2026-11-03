"""Spike: environment facts the aios design depends on. Stdlib only.

Run on the machine that will host the server (e.g. your Windows machine):
    python env_check.py
Paste the output back; it contains no project data.
"""
import platform
import shutil
import sqlite3
import subprocess
import sys


def main() -> None:
    print("os        :", platform.platform())
    print("python    :", sys.version.split()[0], "(need >= 3.11 for the mcp SDK)")
    print("sqlite    :", sqlite3.sqlite_version)
    try:
        c = sqlite3.connect(":memory:")
        c.execute("create virtual table t using fts5(a, b)")
        c.execute("insert into t values ('hello world','x')")
        ok = c.execute("select count(*) from t where t match 'hello'").fetchone()[0] == 1
        print("fts5      :", "OK" if ok else "FAILED")
    except sqlite3.Error as e:
        print("fts5      : NOT AVAILABLE ->", e)
    for tool in ("git", "rg", "uv"):
        path = shutil.which(tool)
        ver = ""
        if path:
            try:
                ver = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
            except Exception as e:  # noqa: BLE001
                ver = f"(version check failed: {e})"
        print(f"{tool:<10}:", ver if path else "not found on PATH")
    print("stdin tty :", sys.stdin.isatty())


if __name__ == "__main__":
    main()
