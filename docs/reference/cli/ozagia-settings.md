# `ozagia-settings`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/settings_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/settings_cli.py` (`ozagia-settings` entry point).

# `ozagia-settings`

inspect the typed settings registry and preview writes

```
usage: ozagia-settings [-h] {schema,get,values,set,migrate} ...
```

## `ozagia-settings schema`

```
usage: ozagia-settings schema [-h] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | — |

## `ozagia-settings get`

```
usage: ozagia-settings get [-h] [--root ROOT] [--json] key
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `key` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-settings values`

```
usage: ozagia-settings values [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-settings set`

```
usage: ozagia-settings set [-h] [--scope {repo,user}] [--root ROOT]
                           [--dry-run] [--json]
                           key value
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `key` |  |  | yes | — |
| `value` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--scope` |  |  | `repo` | — |
| `--root` |  |  |  | — |
| `--dry-run` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-settings migrate`

```
usage: ozagia-settings migrate [-h] [--root ROOT] [--dry-run | --apply]
                               [--confirm TOKEN] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--dry-run` |  |  |  | preview only (the default) |
| `--apply` |  |  |  | write the previewed values; needs --confirm |
| `--confirm` |  |  |  | the token printed by a previous dry run |
| `--json` |  |  |  | — |
