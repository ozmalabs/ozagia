# `ozagia-models`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/models_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/models_cli.py` (`ozagia-models` entry point).

# `ozagia-models`

inspect available models and resolve a task's model

```
usage: ozagia-models [-h]
                     {list,show,resolve,sources,sync,import,graph,similar,match} ...
```

## `ozagia-models list`

```
usage: ozagia-models list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-models show`

```
usage: ozagia-models show [-h] [--root ROOT] [--json] [--source SOURCE |
                          --no-sources] [--with-metadata]
                          model_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `model_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--source` |  |  | [] | cached metadata source (repeatable; default: all active) |
| `--no-sources` |  |  |  | disable cached metadata and use curated profiles only |
| `--with-metadata` |  |  |  | print provenance and the cached metadata for this model |

## `ozagia-models resolve`

```
usage: ozagia-models resolve [-h] [--task CLASS] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--task` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-models sources`

```
usage: ozagia-models sources [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-models sync`

```
usage: ozagia-models sync [-h] [--root ROOT] [--allow-network]
                          [--timeout TIMEOUT] [--max-bytes MAX_BYTES] [--json]
                          source
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `source` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--allow-network` |  |  |  | — |
| `--timeout` | float |  | 60.0 | — |
| `--max-bytes` | int |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-models import`

```
usage: ozagia-models import [-h] [--root ROOT] [--json] source path
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `source` |  |  | yes | — |
| `path` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-models graph`

```
usage: ozagia-models graph [-h] [--root ROOT] [--json] [--max-nodes MAX_NODES]
                           [--max-edges MAX_EDGES] [--top-k TOP_K]
                           [--threshold THRESHOLD] [--source SOURCE |
                           --no-sources]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--max-nodes` | int |  | 500 | — |
| `--max-edges` | int |  | 2000 | — |
| `--top-k` | int |  | 3 | — |
| `--threshold` | float |  | 0.55 | — |
| `--source` |  |  | [] | cached metadata source (repeatable; default: all active) |
| `--no-sources` |  |  |  | disable cached metadata and use curated profiles only |

## `ozagia-models similar`

```
usage: ozagia-models similar [-h] [--limit LIMIT] [--root ROOT] [--json]
                             [--source SOURCE | --no-sources]
                             model_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `model_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--limit` | int |  | 5 | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--source` |  |  | [] | cached metadata source (repeatable; default: all active) |
| `--no-sources` |  |  |  | disable cached metadata and use curated profiles only |

## `ozagia-models match`

```
usage: ozagia-models match [-h] [--task CLASS] [--capability X]
                           [--tool-format F] [--local] [--min-context N]
                           [--max-cost N] [--size CLASS] [--type TYPE]
                           [--modality M] [--limit LIMIT] [--root ROOT] [--json]
                           [--source SOURCE | --no-sources]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--task` |  |  |  | — |
| `--capability` |  |  | [] | — |
| `--tool-format` |  |  | [] | — |
| `--local` |  |  |  | — |
| `--min-context` | int |  |  | — |
| `--max-cost` | float |  |  | — |
| `--size` |  |  |  | — |
| `--type` |  |  |  | — |
| `--modality` |  |  | [] | — |
| `--limit` | int |  | 5 | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--source` |  |  | [] | cached metadata source (repeatable; default: all active) |
| `--no-sources` |  |  |  | disable cached metadata and use curated profiles only |
