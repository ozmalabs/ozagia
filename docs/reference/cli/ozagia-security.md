# `ozagia-security`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/security_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/security_cli.py` (`ozagia-security` entry point).

# `ozagia-security`

advisory, offline security and antipattern scan

```
usage: ozagia-security [-h] {scan,tools,secrets,explain} ...
```

## `ozagia-security scan`

```
usage: python -m ozagia.security_cli scan [-h] [--root ROOT] [--graph GRAPH]
                                          [--tool NAME] [--allow-network]
                                          [--fail-on {blocking,high,medium,low,never}]
                                          [--staged | --diff BASE..HEAD]
                                          [--json] [--out OUT]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: the current directory) |
| `--graph` |  |  |  | graph snapshot (default: <root>/.ozagia/graph.json) |
| `--tool` |  |  |  | run one explicit tool from the fixed registry (repeatable); nothing is installed |
| `--allow-network` |  |  |  | allow tools that refresh a remote database; offline by default |
| `--fail-on` |  |  |  | exit 1 at or above this severity (default: [security].fail_on or high) |
| `--staged` |  |  |  | also scan staged content for secrets (the commit gate) |
| `--diff` |  |  |  | also scan added lines in a committed range for secrets |
| `--json` |  |  |  | emit the report as deterministic JSON |
| `--out` |  |  |  | write the same output atomically to this path |

## `ozagia-security tools`

Takes no arguments of its own in this parser.

## `ozagia-security secrets`

```
usage: python -m ozagia.security_cli secrets [-h] [--root ROOT] (--staged |
                                             --diff BASE..HEAD)
                                             [--allow-secret sha256:FINGERPRINT]
                                             [--json] [--out OUT]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: the current directory) |
| `--staged` |  |  |  | scan staged content (the commit gate) |
| `--diff` |  |  |  | scan added lines in a committed range |
| `--allow-secret` |  |  |  | allow one finding once by fingerprint; the decision is recorded |
| `--json` |  |  |  | emit the report as deterministic JSON |
| `--out` |  |  |  | write the same output atomically to this path |

## `ozagia-security explain`

```
usage: python -m ozagia.security_cli explain [-h] [--root ROOT]
                                             [--graph GRAPH] [--report REPORT]
                                             [--tool NAME] [--allow-network]
                                             [--json]
                                             FINDING_ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `FINDING_ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root for a fresh scan |
| `--graph` |  |  |  | graph snapshot for a fresh scan |
| `--report` |  |  |  | read the finding from this saved JSON report instead of scanning again |
| `--tool` |  |  |  | run one explicit tool while re-scanning |
| `--allow-network` |  |  |  | allow network-requiring tools while re-scanning |
| `--json` |  |  |  | emit the finding as JSON |
