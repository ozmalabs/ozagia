# `ozagia-toolchain`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/toolchain_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/toolchain_cli.py` (`ozagia-toolchain` entry point).

# `ozagia-toolchain`

external-tool discovery, capability readiness and install previews (never installs)

```
usage: ozagia-toolchain [-h] {env,list,plan,explain} ...
```

## `ozagia-toolchain env`

```
usage: python -m ozagia.toolchain_cli env [-h] [--root ROOT] [--json | --text]
                                          [--no-sudo]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | emit JSON |
| `--text` |  |  |  | emit text (default) |
| `--no-sudo` |  |  |  | do not run the sudo -n true usability check |

## `ozagia-toolchain list`

```
usage: python -m ozagia.toolchain_cli list [-h] [--json | --text]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | emit JSON |
| `--text` |  |  |  | emit text (default) |

## `ozagia-toolchain plan`

```
usage: python -m ozagia.toolchain_cli plan [-h] [--root ROOT]
                                           [--capability ID] [--path P]
                                           [--json | --text] [--no-versions]
                                           [--no-sudo] [--require-ready]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--capability` |  |  | [] | capability or group id, e.g. pdf or documents (repeatable) |
| `--path` |  |  | [] | file path whose extension infers a capability (repeatable) |
| `--json` |  |  |  | emit JSON |
| `--text` |  |  |  | emit text (default) |
| `--no-versions` |  |  |  | do not run bounded --version probes |
| `--no-sudo` |  |  |  | do not run the sudo -n true usability check |
| `--require-ready` |  |  |  | exit 1 when a requested capability has no available tool |

## `ozagia-toolchain explain`

```
usage: python -m ozagia.toolchain_cli explain [-h] [--root ROOT] [--json |
                                              --text] [--no-versions]
                                              [--no-sudo]
                                              TOOL
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `TOOL` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | emit JSON |
| `--text` |  |  |  | emit text (default) |
| `--no-versions` |  |  |  | — |
| `--no-sudo` |  |  |  | — |
