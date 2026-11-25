# `ozagia-hooks`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/hooks_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/hooks_cli.py` (`ozagia-hooks` entry point).

# `ozagia-hooks`

opt-in Git hooks for the worktree commit/push policy

```
usage: ozagia-hooks [-h] {install,uninstall,status} ...
```

## `ozagia-hooks install`

```
usage: ozagia-hooks install [-h] [--pre-commit] [--pre-push] [--all] [--force]
                            [--python PYTHON] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--pre-commit` |  |  |  | the pre-commit hook (commit policy) |
| `--pre-push` |  |  |  | the pre-push hook (push policy) |
| `--all` |  |  |  | both hooks |
| `--force` |  |  |  | replace a hook that was not installed by ozagia |
| `--python` |  |  |  | interpreter for the hook (default: the interpreter running ozagia, or $OZAGIA_HOOK_PYTHON) |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-hooks uninstall`

```
usage: ozagia-hooks uninstall [-h] [--pre-commit] [--pre-push] [--all]
                              [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--pre-commit` |  |  |  | the pre-commit hook (commit policy) |
| `--pre-push` |  |  |  | the pre-push hook (push policy) |
| `--all` |  |  |  | both hooks |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-hooks status`

```
usage: ozagia-hooks status [-h] [--pre-commit] [--pre-push] [--all]
                           [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--pre-commit` |  |  |  | the pre-commit hook (commit policy) |
| `--pre-push` |  |  |  | the pre-push hook (push policy) |
| `--all` |  |  |  | both hooks |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
