# `ozagia-supply-chain`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/supply_chain_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/supply_chain_cli.py` (`ozagia-supply-chain` entry point).

# `ozagia-supply-chain`

offline supply-chain attack detection (never fetches, installs or executes project code)

```
usage: ozagia-supply-chain [-h] {check,rules,explain} ...
```

## `ozagia-supply-chain check`

```
usage: ozagia supply-chain check [-h] [--root ROOT] [--json] [--policy POLICY]
                                 [--budget-s BUDGET_S]
                                 [--max-findings MAX_FINDINGS]
                                 [--fail-on {none,note,low,medium,high,critical}]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--policy` |  |  |  | — |
| `--budget-s` | float |  | 30.0 | — |
| `--max-findings` | int |  | 200 | — |
| `--fail-on` |  |  | `none` | — |

## `ozagia-supply-chain rules`

```
usage: ozagia supply-chain rules [-h] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | — |

## `ozagia-supply-chain explain`

```
usage: ozagia supply-chain explain [-h] [--json] RULE
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `RULE` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | — |
