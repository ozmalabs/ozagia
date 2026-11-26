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
usage: ozagia-resources grants [-h] {list,show,check,claim,release,status} ...
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

### `ozagia-resources grants claim`

```
usage: ozagia-resources grants claim [-h] --reason REASON [--task TASK]
                                     [--session SESSION] [--approve]
                                     [--allow-unbounded] [--allow-keep]
                                     [--max-usd MAX_USD]
                                     [--max-minutes MAX_MINUTES] [--root ROOT]
                                     [--json]
                                     id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--reason` |  |  |  | why this claim is needed (recorded) |
| `--task` |  |  |  | — |
| `--session` |  |  |  | — |
| `--approve` |  |  |  | explicit host approval for this claim |
| `--allow-unbounded` |  |  |  | explicitly allow an admin/unbounded grant |
| `--allow-keep` |  |  |  | explicitly allow a keep cleanup policy |
| `--max-usd` | float |  |  | — |
| `--max-minutes` | float |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia-resources grants release`

```
usage: ozagia-resources grants release [-h] [--apply]
                                       [--observed-usd OBSERVED_USD]
                                       [--observed-minutes OBSERVED_MINUTES]
                                       [--cleanup {done,failed,unknown}]
                                       [--root ROOT] [--json]
                                       claim
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `claim` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--apply` |  |  |  | close the claim; without it this is a preview |
| `--observed-usd` | float |  |  | — |
| `--observed-minutes` | float |  |  | — |
| `--cleanup` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia-resources grants status`

```
usage: ozagia-resources grants status [-h]
                                      [--status {active,released,expired,orphaned}]
                                      [--grant GRANT_ID] [--root ROOT]
                                      [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--status` |  |  |  | — |
| `--grant` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
