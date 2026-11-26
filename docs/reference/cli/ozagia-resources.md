# `ozagia-resources`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/resources_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/resources_cli.py` (`ozagia-resources` entry point).

# `ozagia-resources`

read-only host resources, history and admission checks

```
usage: ozagia-resources [-h] {status,check,history,explain,grants} ...
```

## `ozagia-resources status`

```
usage: ozagia-resources status [-h] [--root ROOT] [--path PATH] [--top TOP]
                               [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--path` |  |  |  | also show this filesystem |
| `--top` | int |  | 10 | — |
| `--json` |  |  |  | — |

## `ozagia-resources check`

```
usage: ozagia-resources check [-h] [--for {job,task,worktree}]
                              [--memory-mb MEMORY_MB] [--disk-mb DISK_MB]
                              [--path PATH] [--key KEY]
                              [--freeing MEM_MB:ETA_S] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--for` |  |  | `job` | — |
| `--memory-mb` | float |  |  | — |
| `--disk-mb` | float |  |  | — |
| `--path` |  |  |  | — |
| `--key` |  |  |  | history key (e.g. a command key or task class) |
| `--freeing` |  |  |  | an ozagia job expected to free memory; repeatable |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-resources history`

```
usage: ozagia-resources history [-h] [--key KEY] [--limit LIMIT] [--root ROOT]
                                [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--key` |  |  |  | — |
| `--limit` | int |  | 2000 | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-resources explain`

```
usage: ozagia-resources explain [-h] [--for {job,task,worktree}]
                                [--memory-mb MEMORY_MB] [--disk-mb DISK_MB]
                                [--path PATH] [--key KEY] [--root ROOT]
                                [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--for` |  |  | `job` | — |
| `--memory-mb` | float |  |  | — |
| `--disk-mb` | float |  |  | — |
| `--path` |  |  |  | — |
| `--key` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-resources grants`

```
usage: ozagia-resources grants [-h] {list,show,check} ...
```

### `ozagia-resources grants list`

```
usage: ozagia-resources grants list [-h]
                                    [--kind {vm,cloud,host,service,container}]
                                    [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--kind` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia-resources grants show`

```
usage: ozagia-resources grants show [-h] [--root ROOT] [--json] id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia-resources grants check`

```
usage: ozagia-resources grants check [-h] [--live] [--probe-versions]
                                     [--require-ready] [--root ROOT] [--json]
                                     id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--live` |  |  |  | allow the injected live identity check |
| `--probe-versions` |  |  |  | run bounded qemu/virsh --version probes |
| `--require-ready` |  |  |  | exit 1 when the grant is not ready |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
