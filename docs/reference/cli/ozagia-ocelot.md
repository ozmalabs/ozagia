# `ozagia-ocelot`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/ocelot/__main__.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/integrations/ocelot/__main__.py` (`ozagia-ocelot` entry point).

# `ozagia-ocelot`

resolve, install and verify a separate Ocelot checkout; Ocelot is not bundled and is never downloaded implicitly

```
usage: ozagia-ocelot [-h] {resolve,status,install,verify} ...
```

## `ozagia-ocelot resolve`

```
usage: python -m ozagia.integrations.ocelot resolve [-h] [--root ROOT]
                                                    [--dest DEST] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository whose sibling checkout is considered |
| `--dest` |  |  |  | install root to search (default: user data dir) |
| `--json` |  |  |  | — |

## `ozagia-ocelot status`

```
usage: python -m ozagia.integrations.ocelot status [-h] [--root ROOT]
                                                   [--dest DEST] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--dest` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-ocelot install`

```
usage: python -m ozagia.integrations.ocelot install [-h] --version VERSION
                                                    --url URL --sha256 SHA256
                                                    [--dest DEST] [--apply]
                                                    [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--version` |  |  |  | Ocelot version; also the install directory name |
| `--url` |  |  |  | HTTPS .tar.gz/.tgz/.zip archive URL |
| `--sha256` |  |  |  | pinned SHA-256 of the archive (64 hexadecimal characters) |
| `--dest` |  |  |  | install root (default: ~/.local/share/ozagia/ocelot) |
| `--apply` |  |  |  | download and install; without it this is a dry run |
| `--json` |  |  |  | — |

## `ozagia-ocelot verify`

```
usage: python -m ozagia.integrations.ocelot verify [-h] [--version VERSION]
                                                   [--dest DEST]
                                                   [--sha256 SHA256] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--version` |  |  |  | installed version (default: newest installed) |
| `--dest` |  |  |  | — |
| `--sha256` |  |  |  | also require this archive checksum |
| `--json` |  |  |  | — |
