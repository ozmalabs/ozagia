# `ozagia-task-library`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/task_library_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/task_library_cli.py` (`ozagia-task-library` entry point).

# `ozagia-task-library`

browse, validate and copy built-in task classes

```
usage: ozagia-task-library [-h] {list,show,validate,catalog,copy} ...
```

## `ozagia-task-library list`

```
usage: ozagia-task-library list [-h] [--domain DOMAIN] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--domain` |  |  |  | filter by domain: research, coding or ops |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-task-library show`

```
usage: ozagia-task-library show [-h] [--root ROOT] [--json] task_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `task_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-task-library validate`

```
usage: ozagia-task-library validate [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-task-library catalog`

```
usage: ozagia-task-library catalog [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-task-library copy`

```
usage: ozagia-task-library copy [-h] --to PATH [--dry-run | --apply] [--force]
                                [--root ROOT] [--json]
                                task_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `task_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--to` |  |  |  | destination .toml file or directory inside ozagia/tasks |
| `--dry-run` |  |  |  | preview only; this is the default |
| `--apply` |  |  |  | write the file; explicit confirmation |
| `--force` |  |  |  | overwrite an existing file with different content |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
