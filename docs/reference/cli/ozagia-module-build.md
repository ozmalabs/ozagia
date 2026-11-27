# `ozagia-module-build`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/module_build_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/module_build_cli.py` (`ozagia-module-build` entry point).

# `ozagia-module-build`

preview-first module scaffolding from a repo-scan report

```
usage: ozagia-module-build [-h] {preview,apply,list} ...
```

## `ozagia-module-build preview`

```
usage: python -m ozagia.module_build_cli preview [-h] [--root ROOT]
                                                 [--report REPORT]
                                                 [--opportunity ID]
                                                 [--dest DEST] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: the current directory) |
| `--report` |  |  |  | repo-scan report JSON (default: a conventional path under .ozagia/repo_scan.json, .ozagia/reports/repo_scan.json, .ozagia/repo_scan/latest.json) |
| `--opportunity` |  |  |  | opportunity id to scaffold (required when the report has more than one) |
| `--dest` |  |  |  | destination relative to the root (default: modules/<id>) |
| `--json` |  |  |  | emit structured JSON |

## `ozagia-module-build apply`

```
usage: python -m ozagia.module_build_cli apply [-h] [--root ROOT]
                                               [--report REPORT]
                                               [--opportunity ID] [--dest DEST]
                                               [--json] --confirm TOKEN
                                               [--force]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | repository root (default: the current directory) |
| `--report` |  |  |  | repo-scan report JSON (default: a conventional path under .ozagia/repo_scan.json, .ozagia/reports/repo_scan.json, .ozagia/repo_scan/latest.json) |
| `--opportunity` |  |  |  | opportunity id to scaffold (required when the report has more than one) |
| `--dest` |  |  |  | destination relative to the root (default: modules/<id>) |
| `--json` |  |  |  | emit structured JSON |
| `--confirm` |  |  |  | one-time token printed by preview |
| `--force` |  |  |  | replace existing target files (the token must still match and is still one-time) |

## `ozagia-module-build list`

```
usage: python -m ozagia.module_build_cli list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  | `.` | — |
| `--json` |  |  |  | — |
