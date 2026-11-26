# `ozagia-research-ui`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/research_ui_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/research_ui_cli.py` (`ozagia-research-ui` entry point).

# `ozagia-research-ui`

render a recorded research investigation as a read-only JSON or HTML view

```
usage: ozagia-research-ui [-h] [--root ROOT] --investigation ID
                          --view {library,fetch,screening,evidence,consensus,citation}
                          [--format {json,html}] [--out OUT]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the current git root) |
| `--investigation` |  |  |  | research investigation id |
| `--view` |  |  |  | view to project |
| `--format` |  |  | `json` | output format (default: json) |
| `--out` |  |  |  | write to this file instead of stdout |
