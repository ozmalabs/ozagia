# `ozagia-tasks`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/tasks_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/tasks_cli.py` (`ozagia-tasks` entry point).

# `ozagia-tasks`

validate, preview, run and report bounded tasks

```
usage: ozagia-tasks [-h] {validate,run,list,show,report} ...
```

## `ozagia-tasks validate`

```
usage: ozagia-tasks validate [-h] [--json] spec
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `spec` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | — |

## `ozagia-tasks run`

```
usage: ozagia-tasks run [-h] [--input K=V] [--dry-run] [--allow-exec]
                        [--executor {local}] [--local] [--approve]
                        [--root ROOT] [--json]
                        spec
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `spec` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--input` |  |  |  | an input value; repeatable |
| `--dry-run` |  |  |  | validate and preview even with --allow-exec |
| `--allow-exec` |  |  |  | execute steps through the injected or local executor |
| `--executor` |  |  |  | built-in executor to use with --allow-exec (local = read-only tools and planned pytest) |
| `--local` |  |  |  | alias for --executor local |
| `--approve` |  |  |  | approve mutating steps whose approval is 'ask' |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-tasks list`

```
usage: ozagia-tasks list [-h] [--root ROOT] [--limit LIMIT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--limit` | int |  | 50 | — |
| `--json` |  |  |  | — |

## `ozagia-tasks show`

```
usage: ozagia-tasks show [-h] [--root ROOT] [--json] [run]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `run` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-tasks report`

```
usage: ozagia-tasks report [-h] [--root ROOT] [--goal GOAL]
                           [--session SESSION] [--evaluation EVALUATION]
                           [--snapshot SNAPSHOT]
                           [--criterion-freshness ID=STATE]
                           [--max-bytes MAX_BYTES] [--json]
                           [run]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `run` |  | ? | no | run id (default: the newest recorded run) |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--goal` |  |  |  | goal id to join (goal:g1 or g1) |
| `--session` |  |  |  | session id to join |
| `--evaluation` |  |  |  | retained evaluation id to join |
| `--snapshot` |  |  | `unknown` | snapshot identity recorded in the profile |
| `--criterion-freshness` |  |  |  | injected criterion freshness; repeatable |
| `--max-bytes` | int |  |  | — |
| `--json` |  |  |  | — |
