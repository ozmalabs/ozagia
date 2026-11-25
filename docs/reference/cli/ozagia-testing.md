# `ozagia-testing`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/testing_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/testing_cli.py` (`ozagia-testing` entry point).

# `ozagia-testing`

inspect the test policy and plan affected tests

```
usage: ozagia-testing [-h] {policy,plan} ...
```

## `ozagia-testing policy`

```
usage: ozagia-testing policy [-h] [--task CLASS] [--path PATH] [--captured]
                             [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--task` |  |  |  | task class for [testing.tasks] overrides |
| `--path` |  |  |  | repository path for [testing.paths] overrides (repeatable) |
| `--captured` |  |  |  | resolve the interactive captured-pytest default |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-testing plan`

```
usage: ozagia-testing plan [-h] [--base BASE] [--head HEAD] [--task CLASS]
                           [--changed PATH] [--max-tests N] [--graph PATH]
                           [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--base` |  |  | `HEAD` | — |
| `--head` |  |  |  | — |
| `--task` |  |  |  | — |
| `--changed` |  |  |  | explicit changed path (repeatable) |
| `--max-tests` | int |  |  | — |
| `--graph` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
