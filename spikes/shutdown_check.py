"""Spike: does a stdio MCP server emit clean JSON-RPC first, and how fast does it exit on stdin close?

Usage: python shutdown_check.py <server_script.py>
"""
import json
import subprocess
import sys
import time


def run(script: str) -> None:
    p = subprocess.Popen(
        [sys.executable, script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}}
    assert p.stdin and p.stdout
    p.stdin.write(json.dumps(init) + "\n")
    p.stdin.flush()
    first = p.stdout.readline()
    try:
        clean = json.loads(first).get("id") == 1
    except Exception:
        clean = False
    start = time.time()
    p.stdin.close()
    try:
        p.wait(timeout=5)
        ms: int | None = round((time.time() - start) * 1000)
    except subprocess.TimeoutExpired:
        p.kill()
        ms = None
    print(f"{script}: clean first line={clean}; exit after stdin close={ms} ms")


if __name__ == "__main__":
    run(sys.argv[1])
