# `ozagia-ocelot-workspace`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/ocelot/workspace_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/integrations/ocelot/workspace_cli.py` (`ozagia-ocelot-workspace` entry point).

# `ozagia-ocelot-workspace`

query an indexed Ocelot checkout: seed impact, a scoped test plan, agent context, the Rust/Python/seed bridge, research links, validation, preview-first seed/boot editing, and a read-only engine/worktree doctor

```
usage: ozagia-ocelot-workspace [-h]
                               {status,doctor,seed,seeds,research,engine,validate,claim,edit,boot-edit,context,plan} ...
```

## `ozagia-ocelot-workspace status`

```
usage: ozagia-ocelot-workspace status [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |

## `ozagia-ocelot-workspace doctor`

```
usage: ozagia-ocelot-workspace doctor [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |

## `ozagia-ocelot-workspace seed`

```
usage: ozagia-ocelot-workspace seed [-h] [--root ROOT] [--json]
                                    [--include {summary,closure,tests,docs,sources,terms,all}]
                                    ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | seed id |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |
| `--include` |  |  | `summary` | detail level (default: summary) |

## `ozagia-ocelot-workspace seeds`

```
usage: ozagia-ocelot-workspace seeds [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |

## `ozagia-ocelot-workspace research`

```
usage: ozagia-ocelot-workspace research [-h] [--root ROOT] [--json] [TARGET]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `TARGET` |  | ? | no | document path, block number or topic substring |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |

## `ozagia-ocelot-workspace engine`

```
usage: ozagia-ocelot-workspace engine [-h] [--root ROOT] [--json]
                                      [--op {primitive,rust_impact,term_census,rs_parity}]
                                      [--target TARGET]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |
| `--op` |  |  | `primitive` | query kind (default: primitive) |
| `--target` |  |  |  | primitive name or Rust path for primitive/impact |

## `ozagia-ocelot-workspace validate`

```
usage: ozagia-ocelot-workspace validate [-h] [--root ROOT] [--json] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | seed id |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |

## `ozagia-ocelot-workspace claim`

```
usage: ozagia-ocelot-workspace claim [-h] [--root ROOT] [--json] LO HI
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `LO` | int |  | yes | first seed number |
| `HI` | int |  | yes | last seed number |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |

## `ozagia-ocelot-workspace edit`

```
usage: ozagia-ocelot-workspace edit [-h] [--root ROOT] [--json] --ops FILE
                                    [--write] [--base-sha256 HASH]
                                    ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | seed id |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |
| `--ops` |  |  |  | JSON list of semantic operations |
| `--write` |  |  |  | apply the edit atomically (default: preview only) |
| `--base-sha256` |  |  |  | refuse if the seed no longer has this hash |

## `ozagia-ocelot-workspace boot-edit`

```
usage: ozagia-ocelot-workspace boot-edit [-h] [--root ROOT] [--json] --ops FILE
                                         [--write] [--base-sha256 HASH]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |
| `--ops` |  |  |  | JSON list of boot/registry operations |
| `--write` |  |  |  | apply atomically (default: preview only) |
| `--base-sha256` |  |  |  | single-file base hash guard |

## `ozagia-ocelot-workspace context`

```
usage: ozagia-ocelot-workspace context [-h] [--root ROOT] [--json]
                                       [--changed PATH [PATH ...]] [--base REF]
                                       [--head REF] [--seed ID [ID ...]]
                                       [--max-tests N]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |
| `--changed` |  | one or more | [] | changed path, relative to the checkout |
| `--base` |  |  |  | git base revision |
| `--head` |  |  |  | git head revision |
| `--seed` |  | one or more | [] | changed seed id (repeatable) |
| `--max-tests` | int |  | 20 | show at most N test paths (default: 20) |

## `ozagia-ocelot-workspace plan`

```
usage: ozagia-ocelot-workspace plan [-h] [--root ROOT] [--json]
                                    [--changed PATH [PATH ...]] [--base REF]
                                    [--head REF] [--seed ID [ID ...]]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | Ocelot checkout (default: current repository) |
| `--json` |  |  |  | print JSON instead of text |
| `--changed` |  | one or more | [] | changed path, relative to the checkout (repeatable) |
| `--base` |  |  |  | git base revision (default with --head: HEAD) |
| `--head` |  |  |  | git head revision (default with --base: HEAD) |
| `--seed` |  | one or more | [] | changed seed id (repeatable) |
