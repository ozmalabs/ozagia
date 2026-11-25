# `ozagia-languages`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/languages_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/languages_cli.py` (`ozagia-languages` entry point).

# `ozagia-languages`

per-language support packages, detection and installs

```
usage: ozagia-languages [-h] {list,detect,ensure} ...
```

## `ozagia-languages list`

```
usage: ozagia languages list [-h] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |

## `ozagia-languages detect`

```
usage: ozagia languages detect [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | checkout to inspect (default: the repo root) |
| `--json` |  |  |  | print JSON |

## `ozagia-languages ensure`

```
usage: ozagia languages ensure [-h] [--root ROOT] [--json] [--no-install]
                               [--no-network] [--require-ready]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | checkout to inspect (default: the repo root) |
| `--json` |  |  |  | print JSON |
| `--no-install` |  |  |  | report missing components without installing anything |
| `--no-network` |  |  |  | refuse installs that need the network |
| `--require-ready` |  |  |  | exit 1 when a required component is still missing |
