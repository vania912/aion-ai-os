"""Spike S9b: is stdin a *real console*, or just something that reports isatty()=True?

On Windows, sys.stdin.isatty() can be True for the NUL device (remembered, not verified), so this
script also asks the Win32 API. Stdlib only. Prints one line; contains no project data.

Run it in three places on the same machine and compare:
  1. your own PowerShell window
  2. Claude Code's PowerShell tool  (ask Claude: "run python spikes/tty_check.py")
  3. Claude Code's Bash tool         (ask Claude to run it with the Bash tool)
"""

from __future__ import annotations

import os
import sys


def windows_info() -> str:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = wintypes.HANDLE
    handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    ftype = k32.GetFileType(handle)  # 1=disk, 2=char (console OR NUL), 3=pipe
    mode = wintypes.DWORD()
    is_console = bool(k32.GetConsoleMode(handle, ctypes.byref(mode)))  # fails on NUL/pipes
    can_open_conin = False
    try:
        fd = os.open("CONIN$", os.O_RDWR)  # Windows console input device
        os.close(fd)
        can_open_conin = True
    except OSError:
        pass
    return (
        f"GetFileType={ftype} GetConsoleMode_ok={is_console} "
        f"can_open_CONIN$={can_open_conin} console_window={bool(k32.GetConsoleWindow())}"
    )


def posix_info() -> str:
    can_open_tty = False
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.close(fd)
        can_open_tty = True
    except OSError:
        pass
    return f"os.isatty(0)={os.isatty(0)} can_open_/dev/tty={can_open_tty}"


def main() -> None:
    info = windows_info() if sys.platform == "win32" else posix_info()
    sys.stdout.write(f"isatty={sys.stdin.isatty()} {info}\n")


if __name__ == "__main__":
    main()
