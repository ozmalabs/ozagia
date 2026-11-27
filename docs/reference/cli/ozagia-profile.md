# `ozagia-profile`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/profile_cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/profile_cli.py` (`ozagia-profile` entry point).

# `ozagia-profile`

Join profiler/telemetry artifacts to the repository graph

```
usage: ozagia-profile [-h] {ingest,run,compare,formats} ...
```

## `ozagia-profile ingest`

```
usage: ozagia-profile ingest [-h] [--root ROOT]
                             [--format {auto,cprofile,speedscope,chrome_cpuprofile,collapsed,otlp_json,otlp_metrics,prometheus_text}]
                             [--weight-unit {auto,samples,microseconds,milliseconds,seconds}]
                             [--interval-us INTERVAL_US] [--baseline BASELINE]
                             [--top TOP] [--sort {self,inclusive,calls,impact}]
                             [--no-graph] [--refresh] [--include-profile]
                             [--out OUT] [--json]
                             path
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `path` |  |  | yes | profile artifact |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--format` |  |  | `auto` | artifact format (default: detect from content) |
| `--weight-unit` |  |  | `auto` | collapsed-stack weight unit |
| `--interval-us` | int |  |  | microseconds per sample when the artifact uses counts |
| `--baseline` |  |  |  | earlier artifact to compare against |
| `--top` | int |  | 25 | hotspots to report (1..200; default 25) |
| `--sort` |  |  | `self` | hotspot order (default: self) |
| `--no-graph` |  |  |  | skip the tag-cache/symbol-graph join |
| `--refresh` |  |  |  | refresh the tag cache before joining |
| `--include-profile` |  |  |  | include the normalized profile in JSON output |
| `--out` |  |  |  | write profile.json and profile.md to this directory |
| `--json` |  |  |  | emit JSON |

## `ozagia-profile run`

```
usage: ozagia-profile run [-h] [--root ROOT] --tool {cprofile,pyspy,node}
                          [--allow-exec] [--out OUT] [--timeout-s TIMEOUT_S]
                          [--memory-mb MEMORY_MB] [--disk-mb DISK_MB]
                          [--cwd CWD] [--tool-path TOOL_PATH] [--rate RATE]
                          [--format {auto,cprofile,speedscope,chrome_cpuprofile,collapsed,otlp_json,otlp_metrics,prometheus_text}]
                          [--top TOP] [--sort {self,inclusive,calls,impact}]
                          [--no-graph] [--refresh] [--include-profile] [--json]
                          ...
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `profiler_command` |  | the remainder | yes | profiler command after -- |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--tool` |  |  |  | cprofile, py-spy or node |
| `--allow-exec` |  |  |  | approve executing the command |
| `--out` |  |  |  | artifact/report directory (default: state profiles/run-<stamp>) |
| `--timeout-s` | int |  | 60 | wall-clock timeout in seconds (1..3600) |
| `--memory-mb` | int |  |  | override the policy memory limit |
| `--disk-mb` | int |  |  | override the policy disk admission estimate |
| `--cwd` |  |  |  | working directory inside the repository |
| `--tool-path` |  |  |  | explicit profiler/interpreter path |
| `--rate` | int |  |  | py-spy sampling rate in Hz |
| `--format` |  |  | `auto` | artifact format for the collected output |
| `--top` | int |  | 25 | hotspots to report (1..200) |
| `--sort` |  |  | `self` | — |
| `--no-graph` |  |  |  | — |
| `--refresh` |  |  |  | — |
| `--include-profile` |  |  |  | — |
| `--json` |  |  |  | emit JSON |

## `ozagia-profile compare`

```
usage: ozagia-profile compare [-h] [--root ROOT]
                              [--format {auto,cprofile,speedscope,chrome_cpuprofile,collapsed,otlp_json,otlp_metrics,prometheus_text}]
                              [--weight-unit {auto,samples,microseconds,milliseconds,seconds}]
                              [--interval-us INTERVAL_US] [--top TOP]
                              [--no-graph] [--refresh] [--json]
                              baseline current
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `baseline` |  |  | yes | earlier artifact |
| `current` |  |  | yes | later artifact |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--format` |  |  | `auto` | — |
| `--weight-unit` |  |  | `auto` | — |
| `--interval-us` | int |  |  | — |
| `--top` | int |  | 10 | regressions/improvements to show (default 10) |
| `--no-graph` |  |  |  | — |
| `--refresh` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia-profile formats`

```
usage: ozagia-profile formats [-h] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | — |
