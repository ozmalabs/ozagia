# `ozagia-library-hints`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/library_hints_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/library_hints_cli.py` (`ozagia-library-hints` entry point).

# `ozagia-library-hints`

advisory library-first hints for a diff or changed list

```
usage: ozagia-library-hints [-h] {detect,explain,feedback} ...
```

## `ozagia-library-hints detect`

```
usage: python -m ozagia.library_hints_cli detect [-h] [--root ROOT]
                                                 (--diff BASE..HEAD |
                                                 --changed PATH [PATH ...])
                                                 [--max MAX_PER_EDIT]
                                                 [--mode {off,gentle,strict}]
                                                 [--ecosystem {python,javascript,rust}]
                                                 [--json] [--out OUT]
                                                 [--record | --no-record]
                                                 [--session SESSION]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the current directory) |
| `--diff` |  |  |  | committed range to inspect |
| `--changed` |  | one or more |  | explicit changed paths in the working tree |
| `--max` | int |  |  | hints per file (default: hints.max_per_edit) |
| `--mode` |  |  |  | override hints.library for this run |
| `--ecosystem` |  |  |  | limit to an ecosystem (repeatable) |
| `--json` |  |  |  | emit JSON |
| `--out` |  |  |  | also write the output atomically to this path |
| `--record` |  |  |  | record events |
| `--no-record` |  |  |  | do not record events |
| `--session` |  |  | `cli` | session label for recorded events |

## `ozagia-library-hints explain`

```
usage: python -m ozagia.library_hints_cli explain [-h] [--root ROOT] [--json]
                                                  [--limit LIMIT]
                                                  SYMBOL
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `SYMBOL` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--limit` | int |  | 10 | events shown (default: 10) |

## `ozagia-library-hints feedback`

```
usage: python -m ozagia.library_hints_cli feedback [-h] [--root ROOT] [--json]
                                                   [--session SESSION]
                                                   HINT_ID {accepted,ignored}
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `HINT_ID` |  |  | yes | — |
| `verdict` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--session` |  |  | `cli` | — |
