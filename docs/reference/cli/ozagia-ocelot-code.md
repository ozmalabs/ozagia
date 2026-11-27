# `ozagia-ocelot-code`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/ocelot_code_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/ocelot_code_cli.py` (`ozagia-ocelot-code` entry point).

# `ozagia-ocelot-code`

optional Ocelot structural clustering for code findings; Ocelot is never installed or downloaded

```
usage: ozagia-ocelot-code [-h] {cluster,impact,patterns,status} ...
```

## `ozagia-ocelot-code cluster`

```
usage: python -m ozagia.ocelot_code_cli cluster [-h] --root ROOT
                                                --findings FINDINGS
                                                [--ocelot-root OCELOT_ROOT]
                                                [--mode {auto,inprocess,subprocess}]
                                                [--timeout TIMEOUT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root used for sibling resolution |
| `--findings` |  |  |  | JSON list, review packet or scan report |
| `--ocelot-root` |  |  |  | explicit Ocelot checkout (default: resolve one) |
| `--mode` |  |  | `auto` | worker transport (default: auto) |
| `--timeout` | int |  | 120 | worker timeout in seconds (default: 120) |
| `--json` |  |  |  | emit the result as sorted JSON |

## `ozagia-ocelot-code impact`

```
usage: python -m ozagia.ocelot_code_cli impact [-h] --root ROOT
                                               [--diff BASE..HEAD]
                                               [--changed PATH] [--graph GRAPH]
                                               [--test-plan]
                                               [--ocelot-root OCELOT_ROOT]
                                               [--mode {auto,inprocess,subprocess}]
                                               [--timeout TIMEOUT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--diff` |  |  |  | change range to shape (repeatable) |
| `--changed` |  |  |  | explicit repository-relative changed path (repeatable) |
| `--graph` |  |  |  | graph snapshot (default: <root>/.ozagia/graph.json) |
| `--test-plan` |  |  |  | attach core.testing.plan selections; never runs |
| `--ocelot-root` |  |  |  | explicit Ocelot checkout (default: resolve one) |
| `--mode` |  |  | `auto` | worker transport (default: auto) |
| `--timeout` | int |  | 120 | worker timeout in seconds (default: 120) |
| `--json` |  |  |  | emit the result as sorted JSON |

## `ozagia-ocelot-code patterns`

```
usage: python -m ozagia.ocelot_code_cli patterns [-h] (--report REPORT |
                                                 --root ROOT) [--graph GRAPH]
                                                 [--ocelot-root OCELOT_ROOT]
                                                 [--mode {auto,inprocess,subprocess}]
                                                 [--timeout TIMEOUT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--report` |  |  |  | scan report or list-shaped JSON with opportunities |
| `--root` |  |  |  | repository root; uses graph file shapes with a file-list fallback |
| `--graph` |  |  |  | graph snapshot (default: <root>/.ozagia/graph.json) |
| `--ocelot-root` |  |  |  | explicit Ocelot checkout (default: resolve one) |
| `--mode` |  |  | `auto` | worker transport (default: auto) |
| `--timeout` | int |  | 120 | worker timeout in seconds (default: 120) |
| `--json` |  |  |  | emit the result as sorted JSON |

## `ozagia-ocelot-code status`

```
usage: python -m ozagia.ocelot_code_cli status [-h] [--root ROOT] [--dest DEST]
                                               [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository whose sibling checkout is considered |
| `--dest` |  |  |  | install root to search (default: user data dir) |
| `--json` |  |  |  | — |
