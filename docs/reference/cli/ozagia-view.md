# `ozagia-view`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/view_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/view_cli.py` (`ozagia-view` entry point).

# `ozagia-view`

render a graph snapshot through a data-defined aperture

```
usage: ozagia-view [-h] [--aperture APERTURE] [--format {json,html}]
                   [--root ROOT] [--graph GRAPH] [--out OUT] [--depth DEPTH]
                   [--node NODE] [--list-apertures]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--aperture` |  |  | `architecture` | aperture id (default: architecture) |
| `--format` |  |  | `json` | output format (default: json) |
| `--root` |  |  |  | repository root (default: the current git root) |
| `--graph` |  |  |  | graph JSON (default: <root>/.ozagia/graph.json) |
| `--out` |  |  |  | write to this file instead of stdout |
| `--depth` | int |  |  | scope depth from --node or the aperture's root |
| `--node` |  |  |  | scope root node id (overrides the aperture) |
| `--list-apertures` |  |  |  | list available apertures and exit |
