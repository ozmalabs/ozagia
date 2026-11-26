# `ozagia-log-inspect`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/log_inspect_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/log_inspect_cli.py` (`ozagia-log-inspect` entry point).

# `ozagia-log-inspect`

bounded, redacted inspection of explicitly listed local logs (OpenCode, daemon journal, eval results, JSONL/text)

```
usage: ozagia-log-inspect [-h] --log PATH [--root ROOT] [--since ISO]
                          [--limit LIMIT] [--json] [--out OUT]
                          [--task-spec FILE]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--log` |  |  |  | explicit input file; repeatable (.gz supported) |
| `--root` |  |  |  | resolve relative --log paths against this directory; it is not scanned |
| `--since` |  |  |  | drop timestamped records older than this ISO time (untimestamped records are kept) |
| `--limit` | int |  | 20 | maximum findings shown (default 20, max 100) |
| `--json` |  |  |  | emit the report as JSON |
| `--out` |  |  |  | also write the text or JSON report to this path |
| `--task-spec` |  |  |  | write a bounded, read-only task specification describing this inspection; it is not run |
