# Development

Requires Python >= 3.11.

## Setup

```bash
uv venv
uv pip install -e ".[dev]"
```

(`python -m venv .venv` and `pip install -e ".[dev]"` also work.) Activate with
`.venv\Scripts\activate` on Windows or `source .venv/bin/activate` elsewhere.

## Checks (the same ones CI runs)

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Rules that matter

- **No `print()` in `src/`.** stdout is the MCP stdio protocol channel; a stray print corrupts
  it (spike S4). Ruff rule `T20` enforces this. Use `logging` (stderr) or, in the CLI only,
  `typer.echo`.
- `domain/` and `services/` must not import the MCP SDK; keep MCP code in `interfaces/mcp/`.
- No machine-specific paths in code. The data directory comes from `--data-dir` or `AIOS_DATA_DIR`.

## Try the CLI

```bash
# data dir holds registry.yaml (see examples/registry.yaml); keep it outside this repo
aios project list --data-dir <dir>
aios doctor --data-dir <dir>
```
