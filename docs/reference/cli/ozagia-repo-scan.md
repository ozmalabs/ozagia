# `ozagia-repo-scan`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/repo_scan_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/repo_scan_cli.py` (`ozagia-repo-scan` entry point).

# `ozagia-repo-scan`

advisory, read-only repository scan

```
usage: ozagia-repo-scan [-h] {scan,list-categories} ...
```

## `ozagia-repo-scan scan`

```
usage: python -m ozagia.repo_scan_cli scan [-h] [--root ROOT] [--since N]
                                           [--top N] [--json] [--out OUT]
                                           [--ocelot]
                                           [--ocelot-root OCELOT_ROOT]
                                           [--ocelot-mode {auto,inprocess,subprocess}]
                                           [--ocelot-timeout OCELOT_TIMEOUT]
                                           [--ocelot-patterns]
                                           [--no-library-hints]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: the current directory) |
| `--since` | int |  | 30 | churn/stale window in days (default: 30) |
| `--top` | int |  | 25 | maximum opportunities to show (default: 25) |
| `--json` |  |  |  | emit the report as deterministic JSON |
| `--out` |  |  |  | write the same output atomically to this path |
| `--ocelot` |  |  |  | add optional Ocelot structural clusters (default: off; no install or download) |
| `--ocelot-root` |  |  |  | explicit Ocelot checkout (default: resolve one) |
| `--ocelot-mode` |  |  | `auto` | Ocelot worker transport (default: auto) |
| `--ocelot-timeout` | int |  | 120 | Ocelot worker timeout in seconds (default: 120) |
| `--ocelot-patterns` |  |  |  | add the optional ocelot.patterns candidate architecture-pattern section |
| `--no-library-hints` |  |  |  | skip the bounded library-reuse category |

## `ozagia-repo-scan list-categories`

Takes no arguments of its own in this parser.
