# `ozagia-secrets`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/secrets_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/secrets_cli.py` (`ozagia-secrets` entry point).

# `ozagia-secrets`

secret references, provider status and redacted checks

```
usage: ozagia-secrets [-h]
                      {providers,list,check,forget,capture,capture-text,doctor} ...
```

## `ozagia-secrets providers`

```
usage: python -m ozagia.secrets_cli providers [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--json` |  |  |  | emit deterministic JSON |

## `ozagia-secrets list`

```
usage: python -m ozagia.secrets_cli list [-h] [--root ROOT]
                                         [--session SESSION]
                                         [--harness HARNESS] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--session` |  |  |  | list this session's memory-only captures (daemon) |
| `--harness` |  |  |  | harness for --session (default: cli) |
| `--json` |  |  |  | emit deterministic JSON |

## `ozagia-secrets check`

```
usage: python -m ozagia.secrets_cli check [-h] [--root ROOT]
                                          [--session SESSION]
                                          [--harness HARNESS] [--json]
                                          REF [REF ...]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `REF` |  | one or more | yes | one or more secret:// references |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--session` |  |  |  | check for this daemon session (needed for secret://session refs) |
| `--harness` |  |  |  | harness for --session (default: cli) |
| `--json` |  |  |  | emit deterministic JSON |

## `ozagia-secrets forget`

```
usage: python -m ozagia.secrets_cli forget [-h] [--root ROOT]
                                           [--session SESSION]
                                           [--harness HARNESS] [--json]
                                           REF
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `REF` |  |  | yes | a secret://session/... reference |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--session` |  |  |  | the capture's session (default: cli) |
| `--harness` |  |  |  | harness for --session (default: cli) |
| `--json` |  |  |  | emit deterministic JSON |

## `ozagia-secrets capture`

```
usage: python -m ozagia.secrets_cli capture [-h] [--root ROOT]
                                            [--session SESSION]
                                            [--harness HARNESS]
                                            [--label LABEL] [--ttl S] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--session` |  |  |  | the capture's session (default: cli) |
| `--harness` |  |  |  | harness for --session (default: cli) |
| `--label` |  |  |  | optional bounded label |
| `--ttl` | float |  |  | lifetime in seconds (capped at 24 hours) |
| `--json` |  |  |  | emit deterministic JSON |

## `ozagia-secrets capture-text`

```
usage: python -m ozagia.secrets_cli capture-text [-h] [--root ROOT]
                                                 [--session SESSION]
                                                 [--harness HARNESS] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--session` |  |  |  | the capture's session (default: cli) |
| `--harness` |  |  |  | harness for --session (default: cli) |
| `--json` |  |  |  | emit deterministic JSON |

## `ozagia-secrets doctor`

```
usage: python -m ozagia.secrets_cli doctor [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: current directory) |
| `--json` |  |  |  | emit deterministic JSON |
