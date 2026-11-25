# `ozagia-review`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/code_review_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/code_review_cli.py` (`ozagia-review` entry point).

# `ozagia-review`

deterministic, graph-aware review of a BASE..HEAD diff

```
usage: ozagia-review [-h] [--root ROOT] --range BASE..HEAD [--graph GRAPH]
                     [--json] [--out OUT] [--fail-on {blocking,major,never}]
                     [--max-file-bytes MAX_FILE_BYTES] [--memory-mb MEMORY_MB]
                     [--disk-mb DISK_MB] [--worktree-base WORKTREE_BASE]
                     [--ocelot] [--ocelot-root OCELOT_ROOT]
                     [--ocelot-mode {auto,inprocess,subprocess}]
                     [--ocelot-timeout OCELOT_TIMEOUT] [--ocelot-impact]
                     [--ocelot-patterns] [--library-hints |
                     --no-library-hints]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the current directory) |
| `--range` |  |  |  | commit range to review |
| `--graph` |  |  |  | graph snapshot (default: <root>/.ozagia/graph.json) |
| `--json` |  |  |  | emit the packet as JSON |
| `--out` |  |  |  | write the output atomically to this path |
| `--fail-on` |  |  | `blocking` | exit 1 at or above this severity (default: blocking) |
| `--max-file-bytes` | int |  |  | large-file threshold (default: 1000000) |
| `--memory-mb` | float |  |  | expected test/build memory for admission (default: 2048) |
| `--disk-mb` | float |  |  | expected test/build disk for admission (default: 4096) |
| `--worktree-base` |  |  | `main` | branch expected for the main worktree (default: main) |
| `--ocelot` |  |  |  | add optional Ocelot structural clusters (default: off; no install or download) |
| `--ocelot-root` |  |  |  | explicit Ocelot checkout (default: resolve one) |
| `--ocelot-mode` |  |  | `auto` | Ocelot worker transport (default: auto) |
| `--ocelot-timeout` | int |  | 120 | Ocelot worker timeout in seconds (default: 120) |
| `--ocelot-impact` |  |  |  | add the optional ocelot.impact change-shape section (candidate test sets only) |
| `--ocelot-patterns` |  |  |  | add the optional ocelot.patterns candidate architecture-pattern section |
| `--library-hints` |  |  |  | include the advisory library_hints packet section (default: on) |
| `--no-library-hints` |  |  |  | omit the advisory library_hints packet section |
