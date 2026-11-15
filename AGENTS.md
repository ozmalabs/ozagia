# Working on ozagia

Instructions for coding agents (and people) changing this repository.

## Layout

- `src/ozagia/config.py`: target root, state dir (`.ozagia/`), project config (`.ozagia.toml`).
- `src/ozagia/adapters.py`: per-project extensions (indexers, edit engines, MCP tools, graph queries).
- `src/ozagia/core/`: the graph (`memory.py`), repo map, code search, symbol graph, architecture summary, sessions.
- `src/ozagia/index/`: tree-sitter tag languages, SCIP import.
- `src/ozagia/edit/`: edit transaction (`txn.py`), lossless text, Python/CST/JSON/Markdown edit adapters, dispatch.
- `src/ozagia/lsp.py`: language-server bridge.
- `src/ozagia/integrations/mcp/`: the MCP server.
- `src/ozagia/eval/`: retrieval eval, break/fix agent eval, index bench.
- `docs/`: design docs, `docs/plans/` (dated plans), `docs/results/`.

## Rules

- The core uses the standard library only. Parsers (`tree-sitter-*`) and
  `pyright` are optional extras; code that needs them reports when they are
  missing instead of failing at import.
- ozagia never imports a target project. Project knowledge goes in that
  project's `.ozagia.toml` and adapter (see `adapters.py`). Ocelot's adapter
  lives in the Ocelot repo; `tests/fixtures/ocelot_adapter.py` is a copy used
  as a realistic fixture.
- Edits go through `edit/txn.py`: preview by default, base hash check, atomic
  write, backup under the state dir. No text-patch fallbacks.
- Never write into the target repository outside its state dir, except the
  edit the caller asked for.
- Live agent drivers spend model tokens or run models: they need `--live`
  and `OZAGIA_EVAL_LIVE=1`, and a person's approval.

## Checks

```
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

Live language-server tests: `OZAGIA_LIVE_LSP=1`.
Index performance on a real repository: `python -m ozagia.eval.bench --root <repo> --cold --check`.
