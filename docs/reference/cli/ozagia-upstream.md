# `ozagia-upstream`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/upstream_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/upstream_cli.py` (`ozagia-upstream` entry point).

# `ozagia-upstream`

consent-based, redacted local field-improvement bundles; nothing is uploaded

```
usage: ozagia-upstream [-h] {preview,write,issue} ...
```

## `ozagia-upstream preview`

```
usage: python -m ozagia.upstream_cli preview [-h] [--root ROOT] --title TITLE
                                             [--summary SUMMARY]
                                             [--evidence PATH [PATH ...]]
                                             [--no-environment] [--no-config]
                                             [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--title` |  |  |  | — |
| `--summary` |  |  |  | — |
| `--evidence` |  | one or more | [] | explicit log report, code-review packet, task run, repo-scan report or status JSON; repeatable |
| `--no-environment` |  |  |  | — |
| `--no-config` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-upstream write`

```
usage: python -m ozagia.upstream_cli write [-h] [--root ROOT] --title TITLE
                                           [--summary SUMMARY]
                                           [--evidence PATH [PATH ...]]
                                           --out DIR_OR_ZIP [--consent]
                                           [--no-environment] [--no-config]
                                           [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--title` |  |  |  | — |
| `--summary` |  |  |  | — |
| `--evidence` |  | one or more | [] | — |
| `--out` |  |  |  | new or empty directory, or a .zip file |
| `--consent` |  |  |  | explicitly consent to writing this local bundle |
| `--no-environment` |  |  |  | — |
| `--no-config` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-upstream issue`

```
usage: python -m ozagia.upstream_cli issue [-h] [--root ROOT] [--title TITLE]
                                           [--summary SUMMARY]
                                           [--evidence PATH [PATH ...]]
                                           --out FILE [--no-environment]
                                           [--no-config] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--title` |  |  | `ozagia field report` | — |
| `--summary` |  |  |  | — |
| `--evidence` |  | one or more | [] | — |
| `--out` |  |  |  | — |
| `--no-environment` |  |  |  | — |
| `--no-config` |  |  |  | — |
| `--json` |  |  |  | — |
