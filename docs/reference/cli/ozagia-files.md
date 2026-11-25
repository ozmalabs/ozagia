# `ozagia-files`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/files_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/files_cli.py` (`ozagia-files` entry point).

# `ozagia-files`

track created files and route Markdown without deleting

```
usage: ozagia-files [-h]
                    {list,scan,explain,orphans,cleanup,trash,restore,route} ...
```

## `ozagia-files list`

```
usage: ozagia-files list [-h] [--classification CLASSIFICATION]
                         [--status STATUS] [--limit LIMIT] [--root ROOT]
                         [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--classification` |  |  |  | — |
| `--status` |  |  | `active` | — |
| `--limit` | int |  | 200 | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files scan`

```
usage: ozagia-files scan [-h] [--session SESSION] [--task TASK]
                         [--reason REASON] [--tool TOOL] [--root ROOT]
                         [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--session` |  |  |  | — |
| `--task` |  |  |  | — |
| `--reason` |  |  | `unknown` | — |
| `--tool` |  |  | `scan` | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files explain`

```
usage: ozagia-files explain [-h] [--root ROOT] [--json] path
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `path` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files orphans`

```
usage: ozagia-files orphans [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files cleanup`

```
usage: ozagia-files cleanup [-h] [--older-than-days OLDER_THAN_DAYS]
                            [--classification CLASSIFICATION] [--apply]
                            [--confirm CONFIRM] [--force]
                            [--include-referenced] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--older-than-days` | int |  | 30 | — |
| `--classification` |  |  |  | — |
| `--apply` |  |  |  | move planned files to trash (never delete) |
| `--confirm` |  |  |  | one-time token from the dry-run plan |
| `--force` |  |  |  | record a forced move of a referenced/unknown file |
| `--include-referenced` |  |  |  | dry-run also lists referenced files (apply needs --force) |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files trash`

```
usage: ozagia-files trash [-h] {list,purge} ...
```

### `ozagia-files trash list`

```
usage: ozagia-files trash list [-h] [--limit LIMIT] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--limit` | int |  | 200 | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia-files trash purge`

```
usage: ozagia-files trash purge [-h] [--older-than-days OLDER_THAN_DAYS]
                                [--confirm CONFIRM] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--older-than-days` | int |  |  | only runs this old (default: files.retention_days) |
| `--confirm` |  |  |  | one-time token from the dry-run purge plan |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files restore`

```
usage: ozagia-files restore [-h] [--root ROOT] [--json] trash_id [paths ...]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `trash_id` |  |  | yes | trash id, directory or manifest path |
| `paths` |  | zero or more | no | only these repository-relative paths |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-files route`

```
usage: ozagia-files route [-h] [--content-file CONTENT_FILE]
                          [--session SESSION] [--root ROOT] [--json]
                          path
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `path` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--content-file` |  |  |  | — |
| `--session` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
