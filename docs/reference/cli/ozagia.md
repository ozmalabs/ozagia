# `ozagia`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/cli.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Synopsis from the module docstring:

```
The ozagia command: the graph, the daemon, the integrations and the agent.

    ozagia doctor [--root R] [--json]
    ozagia index [--root R] [--rebuild]
    ozagia agent [--workdir W] [--prompt TEXT | --prompt-file F] [--model M]
                 [--mcp coding|research|combined | --research]
                 [--opencode-config PATH]
                 [--repo-map [--repo-map-tokens N]]
                 [--mesh | --no-mesh]
                 [--clone [--base REV]] [--keep-plugin] [--timeout S]
                 [--python P] [--runs-dir D] [-- <extra opencode args>]
    ozagia daemon serve|ensure|ping|stats|shutdown ... (python -m ozagia.daemon)
    ozagia bg run|wait|list|log|stop ...          (ozagia.daemon.jobcli)
    ozagia mcp ...                                (ozagia.integrations.mcp.server)
    ozagia eval ...                               (python -m ozagia.eval.agent)
    ozagia profile ingest|compare|formats ...     (offline profiler/telemetry
                                                   artifacts joined to the graph)
    ozagia check ...                              (python -m ozagia.check)
    ozagia install opencode|claude|codex|kiro [--workdir W] [...]
    ozagia self info|check|upgrade|uninstall ... (ozagia.selfinstall)
    ozagia [DIR]                                  the TUI on DIR (default: the
                                                  repository of the cwd), as
                                                  `ozagia agent --workdir DIR`
    ozagia job wait ...                           (explicit live job polling)
    ozagia ops record|resume ...                  (append-only ops receipts)
    ozagia live import|diff|save ...              (supplied snapshots only)
    ozagia iac check ...                          (offline Terraform checks)
    ozagia env [--root R] [--for PATH ...] [--json] [--no-cache]
                                                  (Python interpreters and what
                                                  they can import,
                                                  docs/environments.md)
    ozagia env list|show ...                      (declarative environment profiles)
    ozagia relay plan|run|tunnel ...              (nested command/network relays)
    ozagia runtime status|list ...                (configured services and tunnels)
    ozagia service start|run|stop|restart|status|logs NAME
    ozagia process start|run -- ARGV ...          (explicit local processes)
    ozagia tunnel start|stop|status|list ...      (tunnel config vs live state)
    ozagia preflight plan ...                     (dry-run emulator plan)
    ozagia config schema|get|values|set|migrate ... (settings registry and
                                                   .ozagia.toml migration; see
                                                   docs/settings.md)
    ozagia hooks install|uninstall|status ...      (opt-in commit/push hooks;
                                                   docs/worktree.md)
    ozagia who PATH [--root R] [--json]           (sessions that edited PATH)
    ozagia goal add|list|show|check|claim|drop|block|from-review ...
                                                  (graph-addressed goals;
                                                  ozagia.goals_cli)
    ozagia replace PATTERN REPLACEMENT [--glob G ...] [--key K ...] [--regex]
                   [--flags F] [--expect N] [--apply --plan-id ID] ...
                                                  (one pattern across files;
                                                  preview unless --apply)
    ozagia review-repo [--root R] [--json] [--out F] [--section S ...]
                       [--compare PACKET] [--fail-on blocking|major|never]
                       [--index] [--budget-s S] [--save] [--model-pass]
                                                  (deterministic whole-repo
                                                  review, docs/repo_review.md)
    ozagia mesh status|peers|register|send|inbox|lease|worktree|merge|
                 link|delegation ...
                                                  (local session peers,
                                                  messages, advisory leases,
                                                  links and delegations;
                                                  docs/session_mesh.md)
    ozagia capabilities list|show|audit [--json] [--limit N] [--cursor C]
                                                  (read-only declared
                                                  capabilities and observed
                                                  surfaces, docs/capabilities.md)
    ozagia engine serve|token [--root R] [--host H] [--port P] [--allow-remote]
                                                  (local read-only graph HTTP
                                                  transport; docs/graph_engine.md)
    ozagia task-preflight GOAL [--json] [--limit N] [--cursor C] [--root R]
                               [--checkout DIR ...] [--scope ENTRY ...]
                               [--base REV] [--timeout-s S]
                                                  (read-only goal/task
                                                  preflight; docs/task_preflight.md)
    ozagia overview|shape [--root R] [--json | --markdown] [--out PATH]
                    [--budget-s S] [--max-modules N] [--check PATH]
                                                  (one deterministic reference
                                                  page; docs/repo_shape.md)
    ozagia --version

`daemon`, `bg`, `mcp`, `eval` and `check` pass every argument after their name to
those modules' `main`, unchanged. See docs/harness.md.
```

Source: `src/ozagia/cli.py` (`ozagia` entry point).

# `ozagia`

The ozagia command: the graph, the daemon, the integrations and the agent.

```
usage: ozagia [-h] [--version] COMMAND ...
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--version` |  | optional value |  | show program's version number and exit |

## `ozagia doctor`

```
usage: ozagia doctor [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print structured diagnostics |

## `ozagia index`

```
usage: ozagia index [-h] [--root ROOT] [--rebuild] [--dump DUMP] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository (default: OZAGIA_ROOT or the git top level of the cwd) |
| `--rebuild` |  |  |  | build from scratch |
| `--dump` |  |  |  | snapshot path (default: <state dir>/graph.json) |
| `--json` |  |  |  | print the report as JSON |

## `ozagia agent`

```
usage: ozagia agent [-h] [--workdir WORKDIR] [--prompt PROMPT |
                    --prompt-file PROMPT_FILE] [--model MODEL]
                    [--opencode-config PATH] [--clone] [--base BASE]
                    [--keep-plugin] [--timeout TIMEOUT] [--python PYTHON]
                    [--opencode OPENCODE] [--mcp {coding,research,combined}]
                    [--research] [--runs-dir RUNS_DIR] [--no-managed-tmp]
                    [--tmp-ttl D] [--clean-logs] [--resource-grant ID]
                    [--approve-grant] [--allow-unbounded-grant]
                    [--allow-keep-grant] [--grant-reason GRANT_REASON]
                    [--repo-map] [--repo-map-tokens N] [--mesh | --no-mesh]
                    [--mesh-model {none,read,coordinate,supervise}]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--workdir`, `--root` |  |  |  | repository (default: OZAGIA_ROOT or the git top level of the cwd) |
| `--prompt` |  |  |  | run non-interactively with this prompt (sent on stdin, never in opencode's argv) |
| `--prompt-file` |  |  |  | read the prompt from a file (its contents go to stdin; the path is not passed to opencode) |
| `--model`, `-m` |  |  |  | provider/model, e.g. vllm/qwen3.8-27b |
| `--opencode-config` |  |  |  | layer provider settings from this OpenCode JSON config (provider, providers, model, small_model only) into the generated harness config; --model wins. Plugins, MCP servers, permissions, agents and tools are never copied, and the global config is not read implicitly |
| `--clone` |  |  |  | run in an isolated clone under the temp dir (kept) |
| `--base` |  |  |  | with --clone: commit to check out (default HEAD) |
| `--keep-plugin` |  |  |  | leave .opencode/plugins/ozagia.js in the workdir |
| `--timeout` | float |  |  | seconds before the session is killed (default: none) |
| `--python` |  |  |  | interpreter that `python` names in the agent's shell commands |
| `--opencode` |  |  |  | opencode executable (default: $OZAGIA_OPENCODE, which the installer sets to its pinned OpenCode; `system` or unset: `opencode` on PATH) |
| `--mcp` |  |  |  | also give the session the ozagia MCP server in this mode (research: the research tools; combined: coding and research) |
| `--research` |  | optional value |  | same as --mcp combined |
| `--runs-dir` |  |  |  | where run roots (logs, clones) go (default: <tmp>/ozagia_agent) |
| `--no-managed-tmp` |  |  |  | do not register the run root in the managed temp registry |
| `--tmp-ttl` | ozagia.cli._tmp_ttl |  |  | managed run-root lifetime (default: 7 days; a bare number is days, or use an s/m/h/d suffix) |
| `--clean-logs` |  |  |  | after a successful run with no kept clone, delete the run root and scratch workspace (default: keep the logs) |
| `--resource-grant` |  |  |  | claim this resource grant for the session; repeatable |
| `--approve-grant` |  |  |  | host approval for the declared resource grants |
| `--allow-unbounded-grant` |  |  |  | also allow an admin or unbounded resource grant |
| `--allow-keep-grant` |  |  |  | also allow a grant whose cleanup keeps resources |
| `--grant-reason` |  |  |  | host reason recorded in each resource-grant claim |
| `--repo-map` |  |  |  | send a bounded, task-ranked repository map in the first model request (off by default) |
| `--repo-map-tokens` | ozagia.cli._repo_map_budget |  |  | repo-map token budget with --repo-map, 200..8000 (default: 1500) |
| `--mesh` |  |  |  | register this agent run in the local session mesh and pass a bounded metadata summary to the model context (default: [mesh] enabled or OZAGIA_MESH) |
| `--no-mesh` |  |  |  | do not join the session mesh, even when [mesh] enabled or OZAGIA_MESH says so |
| `--mesh-model` |  |  |  | model exposure for the session mesh (default: [mesh] model, OZAGIA_MESH_MODEL, else read); coordinate adds the bounded ozagia_mesh tool |

## `ozagia daemon`

Delegates every argument to `python -m ozagia.daemon.__main__` (its own `--help` shows the flags).

## `ozagia bg`

Delegates every argument to `python -m ozagia.daemon.jobcli` (its own `--help` shows the flags).

## `ozagia mcp`

Delegates every argument to `python -m ozagia.integrations.mcp.server` (its own `--help` shows the flags).
Reference: [ozagia-mcp](cli/ozagia-mcp.md).

## `ozagia eval`

Delegates every argument to `python -m ozagia.eval.agent` (its own `--help` shows the flags).
Reference: [ozagia-eval](cli/ozagia-eval.md).

## `ozagia check`

Delegates every argument to `python -m ozagia.check` (its own `--help` shows the flags).

## `ozagia self`

Delegates every argument to `python -m ozagia.selfinstall` (its own `--help` shows the flags).

## `ozagia config`

Delegates every argument to `python -m ozagia.settings_cli` (its own `--help` shows the flags).
Reference: [ozagia-settings](cli/ozagia-settings.md).

## `ozagia hooks`

Delegates every argument to `python -m ozagia.hooks_cli` (its own `--help` shows the flags).
Reference: [ozagia-hooks](cli/ozagia-hooks.md).

## `ozagia federation`

Delegates every argument to `python -m ozagia.federation_cli` (its own `--help` shows the flags).

## `ozagia profile`

Delegates every argument to `python -m ozagia.profile_cli` (its own `--help` shows the flags).
Reference: [ozagia-profile](cli/ozagia-profile.md).

## `ozagia profile-run`

Takes no arguments of its own in this parser.

## `ozagia install`

```
usage: ozagia install [-h] [--workdir WORKDIR] {opencode,claude,codex,kiro}
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `target` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--workdir` |  |  | `.` | project directory (default: .) |

## `ozagia job`

```
usage: ozagia job [-h] {wait} ...
```

### `ozagia job wait`

```
usage: ozagia job wait [-h] --provider {ssm,codebuild,codepipeline,kubectl}
                       --job-id JOB_ID [--resource RESOURCE]
                       [--instance-id INSTANCE_ID] [--region REGION]
                       [--namespace NAMESPACE] [--executable EXECUTABLE]
                       [--allow-live] [--timeout-s TIMEOUT_S]
                       [--poll-s POLL_S] [--max-lines MAX_LINES] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--provider` |  |  |  | — |
| `--job-id` |  |  |  | — |
| `--resource` |  |  |  | CodePipeline pipeline name |
| `--instance-id` |  |  |  | SSM instance id (SSM provider) |
| `--region` |  |  |  | explicit AWS region |
| `--namespace` |  |  |  | explicit Kubernetes namespace |
| `--executable` |  |  |  | explicit aws/kubectl path or name |
| `--allow-live` |  |  |  | explicitly enable the live read-only provider |
| `--timeout-s` | float |  | 300.0 | — |
| `--poll-s` | float |  | 2.0 | — |
| `--max-lines` | int |  | 10 | — |
| `--json` |  |  |  | — |

## `ozagia ops`

```
usage: ozagia ops [-h] {record,resume} ...
```

### `ozagia ops record`

```
usage: ozagia ops record [-h] [--record RECORD] [--record-file RECORD_FILE]
                         [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--record` |  |  |  | JSON object, or - for stdin |
| `--record-file` |  |  |  | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia ops resume`

```
usage: ozagia ops resume [-h] [--root ROOT] [--limit LIMIT]
                         [--environment ENVIRONMENT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--limit` | int |  | 20 | — |
| `--environment` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia live`

```
usage: ozagia live [-h] {import,save,diff} ...
```

### `ozagia live import`

```
usage: ozagia live import [-h] [--before BEFORE] [--before-file BEFORE_FILE]
                          [--after AFTER] [--after-file AFTER_FILE]
                          [--provenance PROVENANCE] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--before` |  |  |  | — |
| `--before-file` |  |  |  | — |
| `--after` |  |  |  | — |
| `--after-file` |  |  |  | — |
| `--provenance` |  |  | `supplied-artifact` | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia live save`

```
usage: ozagia live save [-h] [--before BEFORE] [--before-file BEFORE_FILE]
                        [--after AFTER] [--after-file AFTER_FILE]
                        [--provenance PROVENANCE] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--before` |  |  |  | — |
| `--before-file` |  |  |  | — |
| `--after` |  |  |  | — |
| `--after-file` |  |  |  | — |
| `--provenance` |  |  | `supplied-artifact` | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia live diff`

```
usage: ozagia live diff [-h] [--before BEFORE] [--before-file BEFORE_FILE]
                        [--after AFTER] [--after-file AFTER_FILE]
                        [--provenance PROVENANCE] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--before` |  |  |  | — |
| `--before-file` |  |  |  | — |
| `--after` |  |  |  | — |
| `--after-file` |  |  |  | — |
| `--provenance` |  |  | `supplied-artifact` | — |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia iac`

```
usage: ozagia iac [-h] {check} ...
```

### `ozagia iac check`

```
usage: ozagia iac check [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia env`

```
usage: ozagia env [-h] [--root ENV_ROOT] [--for PATH [PATH ...]] [--json]
                  [--no-cache]
                  {list,show} ...
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--for` |  | one or more |  | files or directories (root-relative) to find an interpreter for |
| `--json` |  |  |  | print JSON |
| `--no-cache` |  |  |  | probe every interpreter, read and write no cache |

### `ozagia env list`

```
usage: ozagia env list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia env show`

```
usage: ozagia env show [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia relay`

```
usage: ozagia relay [-h] {plan,run,tunnel} ...
```

### `ozagia relay plan`

```
usage: ozagia relay plan [-h] --spec SPEC [--allow-live] [--allow-network]
                         [--ephemeral] [--timeout-s TIMEOUT_S] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--spec` |  |  |  | JSON chain spec, or - for stdin |
| `--allow-live` |  |  |  | — |
| `--allow-network` |  |  |  | — |
| `--ephemeral` |  |  |  | — |
| `--timeout-s` | float |  | 300.0 | — |
| `--json` |  |  |  | — |

### `ozagia relay run`

```
usage: ozagia relay run [-h] --spec SPEC [--allow-live] [--allow-network]
                        [--ephemeral] [--timeout-s TIMEOUT_S] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--spec` |  |  |  | JSON chain spec, or - for stdin |
| `--allow-live` |  |  |  | — |
| `--allow-network` |  |  |  | — |
| `--ephemeral` |  |  |  | — |
| `--timeout-s` | float |  | 300.0 | — |
| `--json` |  |  |  | — |

### `ozagia relay tunnel`

```
usage: ozagia relay tunnel [-h] --spec SPEC --target TARGET
                           [--local-port LOCAL_PORT] [--allow-live]
                           [--allow-network] [--ephemeral] [--hold]
                           [--timeout-s TIMEOUT_S] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--spec` |  |  |  | JSON chain spec, or - for stdin |
| `--target` |  |  |  | HOST:PORT |
| `--local-port` | int |  | 0 | — |
| `--allow-live` |  |  |  | — |
| `--allow-network` |  |  |  | — |
| `--ephemeral` |  |  |  | — |
| `--hold` |  |  |  | — |
| `--timeout-s` | float |  | 30.0 | — |
| `--json` |  |  |  | — |

## `ozagia runtime`

```
usage: ozagia runtime [-h] {status,list} ...
```

### `ozagia runtime status`

```
usage: ozagia runtime status [-h] [--root ROOT] [--json] [name]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia runtime list`

```
usage: ozagia runtime list [-h] [--root ROOT] [--json] [name]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia service`

```
usage: ozagia service [-h] {list,start,run,stop,restart,status,logs} ...
```

### `ozagia service list`

```
usage: ozagia service list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia service start`

```
usage: ozagia service start [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia service run`

```
usage: ozagia service run [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia service stop`

```
usage: ozagia service stop [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia service restart`

```
usage: ozagia service restart [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia service status`

```
usage: ozagia service status [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia service logs`

```
usage: ozagia service logs [-h] [--root ROOT] [--json] [--max-bytes MAX_BYTES]
                           name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--max-bytes` | int |  | 4096 | — |

## `ozagia process`

```
usage: ozagia process [-h] {start,run,list,status,stop,logs} ...
```

### `ozagia process start`

```
usage: ozagia process start [-h] [--workdir WORKDIR] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--workdir` |  |  |  | repository-relative working directory |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia process run`

```
usage: ozagia process run [-h] [--workdir WORKDIR] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--workdir` |  |  |  | repository-relative working directory |
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia process list`

```
usage: ozagia process list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia process status`

```
usage: ozagia process status [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia process stop`

```
usage: ozagia process stop [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia process logs`

```
usage: ozagia process logs [-h] [--root ROOT] [--json] [--max-bytes MAX_BYTES]
                           name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |
| `--max-bytes` | int |  | 4096 | — |

## `ozagia tunnel`

```
usage: ozagia tunnel [-h] {status,list,start,stop} ...
```

### `ozagia tunnel status`

```
usage: ozagia tunnel status [-h] [--root ROOT] [--json] [name]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia tunnel list`

```
usage: ozagia tunnel list [-h] [--root ROOT] [--json] [name]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

### `ozagia tunnel start`

```
usage: ozagia tunnel start [-h] [--root ROOT] [--allow-live] [--allow-network]
                           [--once] [--json]
                           name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--allow-live` |  |  |  | — |
| `--allow-network` |  |  |  | — |
| `--once` |  |  |  | open, report and close it immediately |
| `--json` |  |  |  | — |

### `ozagia tunnel stop`

```
usage: ozagia tunnel stop [-h] [--root ROOT] [--json] name
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `name` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--json` |  |  |  | — |

## `ozagia goal`

```
usage: ozagia goal [-h] {add,list,show,check,claim,drop,block,from-review} ...
```

### `ozagia goal add`

```
usage: ozagia goal add [-h] [--root ROOT] [--json] [--title TITLE] [--why WHY]
                       [--target TARGET] [--scope SCOPE]
                       [--criterion CRITERION] [--file FILE]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--title` |  |  |  | — |
| `--why` |  |  |  | free text |
| `--target` |  |  | [] | graph key the work is about (repeatable) |
| `--scope` |  |  | [] | graph key or path glob the work may change (repeatable) |
| `--criterion` |  |  | [] | kind:args, e.g. test_passes:tests/t.py::test_x (repeatable) |
| `--file` |  |  |  | goal.toml with the same fields |

### `ozagia goal list`

```
usage: ozagia goal list [-h] [--root ROOT] [--json] [--all] [--status STATUS]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | include dropped goals |
| `--status` |  |  |  | only this status |

### `ozagia goal show`

```
usage: ozagia goal show [-h] [--root ROOT] [--json] id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia goal check`

```
usage: ozagia goal check [-h] [--root ROOT] [--json] [--no-refresh] [ids ...]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ids` |  | zero or more | no | goal ids (default: every goal not dropped) |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--no-refresh` |  |  |  | do not take pending file changes into the in-memory graph |

### `ozagia goal claim`

```
usage: ozagia goal claim [-h] [--root ROOT] [--json] --harness HARNESS
                         --session SESSION [--release]
                         id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | — |
| `--session` |  |  |  | — |
| `--release` |  |  |  | release the claim |

### `ozagia goal drop`

```
usage: ozagia goal drop [-h] [--root ROOT] [--json] [--reason REASON] id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--reason` |  |  | `` | — |

### `ozagia goal block`

```
usage: ozagia goal block [-h] [--root ROOT] [--json] [--reason REASON]
                         [--clear]
                         id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--reason` |  |  | `` | — |
| `--clear` |  |  |  | unblock (back to open) |

### `ozagia goal from-review`

```
usage: ozagia goal from-review [-h] [--root ROOT] [--json]
                               [PACKET] FINDING_ID [[PACKET] FINDING_ID ...]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `[PACKET] FINDING_ID` |  | one or more | yes | the finding id, optionally after an ozagia.repo_review.packet.v1 JSON file under <state dir>/reviews/ (default: the reviews dir's latest.json) |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

## `ozagia who`

```
usage: ozagia who [-h] [--root ROOT] [--json] path
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `path` |  |  | yes | file (relative to the cwd, or absolute) |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print the report as JSON |

## `ozagia replace`

```
usage: ozagia replace [-h] [--glob GLOB] [--key KEY] [--regex] [--flags FLAGS]
                      [--expect EXPECT] [--max-files MAX_FILES]
                      [--max-matches MAX_MATCHES] [--apply]
                      [--plan-id PLAN_ID] [--any-suffix]
                      [--allow-parse-errors] [--no-gitignore] [--task TASK]
                      [--root ROOT] [--json]
                      pattern replacement
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `pattern` |  |  | yes | — |
| `replacement` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--glob` |  |  |  | root-relative glob (repeatable; ** crosses dirs) |
| `--key` |  |  |  | node key whose span restricts matches (repeatable) |
| `--regex` |  |  |  | PATTERN is a Python re; REPLACEMENT may use \1, \g<name> |
| `--flags` |  |  |  | regex flags: letters i,m,s,x,a |
| `--expect` | int |  |  | refuse unless there are exactly N matches |
| `--max-files` | int |  |  | — |
| `--max-matches` | int |  |  | — |
| `--apply` |  |  |  | write the change (needs --plan-id) |
| `--plan-id` |  |  |  | the preview's plan_id; --apply refuses unless the plan is still the preview's |
| `--any-suffix` |  |  |  | every file, not only text suffixes (dotfiles too) |
| `--allow-parse-errors` |  |  |  | — |
| `--no-gitignore` |  |  |  | also files .gitignore excludes |
| `--task` |  |  |  | journal task label |
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print the result as JSON |

## `ozagia review-repo`

```
usage: ozagia review-repo [-h] [--root ROOT] [--json] [--out OUT]
                          [--section {architecture,tests,health,churn,docs,security,ops,dependencies}]
                          [--compare PACKET]
                          [--fail-on {blocking,major,never}] [--index]
                          [--graph GRAPH] [--budget-s BUDGET_S] [--save]
                          [--model-pass]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print the packet as JSON |
| `--out` |  |  |  | also write the output atomically to this path (a .json path gets the JSON packet) |
| `--section` |  |  |  | review only this section (repeatable; default: all) |
| `--compare` |  |  |  | previous packet JSON: report new, resolved and unchanged finding ids |
| `--fail-on` |  |  | `blocking` | exit 1 at or above this severity (default: blocking) |
| `--index` |  |  |  | build or refresh the graph snapshot first |
| `--graph` |  |  |  | graph snapshot (default: <state dir>/graph.json) |
| `--budget-s` | float |  | 60.0 | wall-clock budget for the sections (default: 60) |
| `--save` |  |  |  | also save the packet under <state dir>/reviews/ and as latest.json (atomic), as the MCP tool does |
| `--model-pass` |  |  |  | also write a compact packet for model reviewers under <state dir>/reviews/ (no model is called) |

## `ozagia deep-review`

```
usage: ozagia deep-review [-h] [--root ROOT] [--out OUT]
                          [--profile {full,security,performance,failure-modes,data-structures,scalability,architecture,tests,dependencies,operations,docs,change,review-change}]
                          [--index] [--security-tool NAME]
                          [--security-network] [--local-advisories PATH]
                          [--license-policy PATH] [--supply-chain-policy PATH]
                          [--range BASE..HEAD] [--profile-report PATH]
                          [--run-bench JSON_ARGV]
                          [--bench-timeout-s BENCH_TIMEOUT_S]
                          [--baseline BASELINE]
                          [--fail-on {blocking,major,never}]
                          [--budget-s BUDGET_S] [--max-findings MAX_FINDINGS]
                          [--focus {security,performance,failure-modes,data-structures,scalability,architecture,tests-and-coverage,dependencies,operations,documents-and-api,correctness}]
                          [--model-pass] [--live] [--model MODEL]
                          [--max-tokens MAX_TOKENS] [--base-url BASE_URL]
                          [--model-role {security,performance,failures,data,scalability,architecture,tests}]
                          [--model-budget-s MODEL_BUDGET_S]
                          [--model-max-requests MODEL_MAX_REQUESTS]
                          [--model-token-budget MODEL_TOKEN_BUDGET]
                          [--model-cost-budget MODEL_COST_BUDGET] [--ocelot]
                          [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--out` |  |  |  | report directory (default: <root>/.ozagia/reviews/deep-<UTC stamp>) |
| `--profile` |  |  | `full` | full, one subject profile, or 'change' (alias 'review-change') for a full-profile review of --range, defaulting to HEAD~1..HEAD |
| `--index` |  |  |  | refresh graph before review |
| `--security-tool` |  |  |  | explicitly run one allowlisted security scanner; repeatable (default: no external scanners) |
| `--security-network` |  |  |  | explicitly allow a requested scanner that requires network access (default: refuse) |
| `--local-advisories` |  |  |  | match exact locked versions against this local JSON advisory dataset; no download, ranges or clean verdict |
| `--license-policy` |  |  |  | evaluate declared lockfile licenses against a local allow/deny policy; ambiguous values remain unknown |
| `--supply-chain-policy` |  |  |  | apply an explicit local supply-chain policy JSON to the offline dependency checks; no network |
| `--range` |  |  |  | include deterministic review of a commit range |
| `--profile-report` |  |  |  | register a measured profile report (or artifact) as performance/scalability evidence; repeatable |
| `--run-bench` |  |  |  | explicitly run a caller-supplied argv vector, encoded as a JSON string array; repeatable, no shell |
| `--bench-timeout-s` | float |  | 30.0 | maximum seconds per benchmark command (default: 30; also bounded by --budget-s) |
| `--baseline` |  |  |  | previous report directory |
| `--fail-on` |  |  | `never` | — |
| `--budget-s` | float |  | 60.0 | — |
| `--max-findings` | int |  |  | — |
| `--focus` |  |  |  | prioritize this subject within its severity; repeatable |
| `--model-pass` |  |  |  | run optional independent model reviewers; requires --live and OZAGIA_EVAL_LIVE=1 |
| `--live` |  |  |  | explicitly enable model calls (also requires OZAGIA_EVAL_LIVE=1) |
| `--model` |  |  |  | provider model name for --model-pass |
| `--max-tokens` | int |  |  | maximum completion tokens per model reviewer |
| `--base-url` |  |  |  | OpenAI-compatible API base URL |
| `--model-role` |  |  |  | — |
| `--model-budget-s` | float |  |  | overall wall-clock budget for the model pass (default: 120 seconds) |
| `--model-max-requests` | int |  |  | maximum number of model reviewer requests (default: 32) |
| `--model-token-budget` | int |  |  | optional total token cap across model reviewer runs when the provider reports usage |
| `--model-cost-budget` | float |  |  | optional total cost cap in USD across model reviewer runs when the provider reports usage |
| `--ocelot` |  |  |  | opt in to Ocelot clustering of model candidates (no install, network or model call; exact fallback) |
| `--json` |  |  |  | — |

## `ozagia preflight`

```
usage: ozagia preflight [-h] {plan} ...
```

### `ozagia preflight plan`

```
usage: ozagia preflight plan [-h] --root ROOT [--dependency DEPENDENCY]
                             --emulator-bin EMULATOR_BIN
                             [--provider-version PROVIDER_VERSION] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | — |
| `--dependency` |  |  |  | ROOT:ROOT (repeatable) |
| `--emulator-bin` |  |  |  | — |
| `--provider-version` |  |  |  | NAME=VERSION (repeatable) |
| `--json` |  |  |  | — |

## `ozagia capabilities`

```
usage: ozagia capabilities [-h] {list,show,audit} ...
```

### `ozagia capabilities list`

```
usage: ozagia capabilities list [-h] [--json] [--limit LIMIT]
                                [--cursor CURSOR]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |
| `--limit` | int |  | 25 | page size (default 25, max 200) |
| `--cursor` |  |  |  | opaque page cursor from a previous call |

### `ozagia capabilities show`

```
usage: ozagia capabilities show [-h] [--json] [--limit LIMIT]
                                [--cursor CURSOR]
                                id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `id` |  |  | yes | capability id, e.g. cap:graph.context |

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |
| `--limit` | int |  | 25 | page size (default 25, max 200) |
| `--cursor` |  |  |  | opaque page cursor from a previous call |

### `ozagia capabilities audit`

```
usage: ozagia capabilities audit [-h] [--json] [--limit LIMIT]
                                 [--cursor CURSOR]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |
| `--limit` | int |  | 25 | page size (default 25, max 200) |
| `--cursor` |  |  |  | opaque page cursor from a previous call |

## `ozagia engine`

```
usage: ozagia engine [-h] {serve,token} ...
```

### `ozagia engine serve`

```
usage: ozagia engine serve [-h] [--root ROOT] [--host HOST] [--port PORT]
                           [--allow-remote] [--no-auto-known] [--ready-json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the current checkout) |
| `--host` |  |  | `127.0.0.1` | bind host (default: 127.0.0.1; loopback only) |
| `--port` | int |  | 8765 | bind port (default: 8765; 0 chooses one) |
| `--allow-remote` |  |  |  | allow a non-loopback bind; HTTP has no TLS and the caller owns the network/auth perimeter |
| `--no-auto-known` |  |  |  | return full find/outline/context rows when a request omits known (default: remember hashes in the session and return u handles) |

| `--ready-json` |  |  |  | print one versioned JSON readiness record after binding; omit human startup lines |
### `ozagia engine token`

```
usage: ozagia engine token [-h] [--root ROOT]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the current checkout) |

## `ozagia task-preflight`

```
usage: ozagia task-preflight [-h] [--json] [--limit LIMIT] [--cursor CURSOR]
                             [--root ROOT] [--checkout DIR] [--scope ENTRY]
                             [--base BASE] [--timeout-s TIMEOUT_S]
                             goal
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `goal` |  |  | yes | goal id (g1 or goal:g1) |

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |
| `--limit` | int |  | 25 | criteria page size (default 25, max 200) |
| `--cursor` |  |  |  | opaque criteria page cursor from a previous call |
| `--root` |  |  |  | repository root |
| `--checkout` |  |  |  | additional checkout to include (repeatable) |
| `--scope` |  |  |  | scope entry (repeatable; default: the goal's) |
| `--base` |  |  | `HEAD` | revision for changed-file discovery (default HEAD) |
| `--timeout-s` | int |  | 30 | caller-owned preflight timeout in seconds (default 30, max 600) |

## `ozagia overview`

```
usage: ozagia overview [-h] [--root ROOT] [--json] [--markdown] [--out PATH]
                       [--budget-s BUDGET_S] [--max-modules MAX_MODULES]
                       [--check PATH]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print the stable JSON report |
| `--markdown` |  |  |  | print Markdown (the default) |
| `--out` |  |  |  | write the report atomically to PATH (the only write) |
| `--budget-s` | float |  | 10.0 | wall-clock budget (default 10 s) |
| `--max-modules` | int |  | 40 | maximum modules listed (default 40) |
| `--check` |  |  |  | compare PATH with freshly generated output; exit 1 on drift |

## `ozagia shape`

```
usage: ozagia overview [-h] [--root ROOT] [--json] [--markdown] [--out PATH]
                       [--budget-s BUDGET_S] [--max-modules MAX_MODULES]
                       [--check PATH]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print the stable JSON report |
| `--markdown` |  |  |  | print Markdown (the default) |
| `--out` |  |  |  | write the report atomically to PATH (the only write) |
| `--budget-s` | float |  | 10.0 | wall-clock budget (default 10 s) |
| `--max-modules` | int |  | 40 | maximum modules listed (default 40) |
| `--check` |  |  |  | compare PATH with freshly generated output; exit 1 on drift |

## `ozagia languages`

```
usage: ozagia languages [-h] {list,detect,ensure} ...
```

### `ozagia languages list`

```
usage: ozagia languages list [-h] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |

### `ozagia languages detect`

```
usage: ozagia languages detect [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | checkout to inspect (default: the repo root) |
| `--json` |  |  |  | print JSON |

### `ozagia languages ensure`

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

## `ozagia supply-chain`

```
usage: ozagia supply-chain [-h] {check,rules,explain} ...
```

### `ozagia supply-chain check`

```
usage: ozagia supply-chain check [-h] [--root ROOT] [--json] [--policy POLICY]
                                 [--budget-s BUDGET_S]
                                 [--max-findings MAX_FINDINGS]
                                 [--fail-on {none,note,low,medium,high,critical}]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | checkout to inspect (default: the repo root) |
| `--json` |  |  |  | print JSON |
| `--policy` |  |  |  | explicit supply-chain policy JSON path |
| `--budget-s` | float |  | 30.0 | total time budget in seconds (default 30) |
| `--max-findings` | int |  | 200 | finding cap (default 200) |
| `--fail-on` |  |  | `none` | exit 1 when a finding reaches this severity |

### `ozagia supply-chain rules`

```
usage: ozagia supply-chain rules [-h] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |

### `ozagia supply-chain explain`

```
usage: ozagia supply-chain explain [-h] [--json] RULE
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `RULE` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--json` |  |  |  | print JSON |

## `ozagia migration`

```
usage: ozagia migration [-h] {status,tasks,scaffold} ...
```

### `ozagia migration status`

```
usage: ozagia migration status [-h] [--root ROOT] [--json | --markdown]
                               [--no-record]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the repo root) |
| `--json` |  |  |  | print JSON |
| `--markdown` |  |  |  | print a Markdown report |
| `--no-record` |  |  |  | do not write the report under the state directory |

### `ozagia migration tasks`

```
usage: ozagia migration tasks [-h] [--root ROOT] [--limit LIMIT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the repo root) |
| `--limit` | int |  | 50 | maximum tasks (default 50, max 500) |
| `--json` |  |  |  | print JSON |

### `ozagia migration scaffold`

```
usage: ozagia migration scaffold [-h] --target TARGET [--write]
                                 [--base-sha BASE_SHA] [--root ROOT] [--json]
                                 symbol
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `symbol` |  |  | yes | public Python symbol key |

| option | type | values | default | help |
|---|---|---|---|---|
| `--target` |  |  |  | existing .rs file inside the Rust tree |
| `--write` |  |  |  | append the skeleton through the edit transaction (default: preview only) |
| `--base-sha` |  |  |  | refuse the write unless the target still has this sha256 |
| `--root` |  |  |  | repository root (default: the repo root) |
| `--json` |  |  |  | print JSON |

## `ozagia tmp`

```
usage: ozagia tmp [-h]
                  {list,status,audit,create,adopt,release,pin,unpin,touch,run,clean,gc,clean-legacy,path} ...
```

### `ozagia tmp list`

```
usage: ozagia tmp list [-h] [--root ROOT] [--json] [--all]
                       [--kind {scratch,agent,check,eval,engine,cache,other}]
                       [--session SESSION] [--owned | --unowned]
                       [--sort {allocated,apparent,files,inodes,age}]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | include released and expired records |
| `--kind` |  |  |  | only this kind |
| `--session` |  |  |  | only this session id, or HARNESS:ID |
| `--owned` |  |  |  | only owned entries |
| `--unowned` |  |  |  | only unowned entries |
| `--sort` |  |  |  | order: largest allocated/apparent/files/inodes, or least recently used (age); default created-at |

### `ozagia tmp status`

```
usage: ozagia tmp status [-h] [--root ROOT] [--json] [--top N]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--top` | int |  |  | also list the N largest entries by allocated bytes |

### `ozagia tmp audit`

```
usage: ozagia tmp audit [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia tmp create`

```
usage: ozagia tmp create [-h] [--root ROOT] [--json] --reason REASON
                         [--kind {scratch,agent,check,eval,engine,cache,other}]
                         [--session S] [--harness H] [--ttl-s TTL_S]
                         [--path PATH] [--max-total-bytes B] [--max-inodes N]
                         [--max-session-bytes B] [--max-session-inodes N]
                         [--min-free B] [--model NAME] [--run-id ID]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--reason` |  |  |  | short label (at most 200 characters) |
| `--kind` |  |  | `scratch` | — |
| `--session` |  |  |  | session id (never invented when omitted) |
| `--harness` |  |  |  | harness name for --session |
| `--ttl-s` | int |  |  | expiry in seconds (60 to 90 days) |
| `--path` |  |  |  | create at PATH instead of the default location |
| `--max-total-bytes` | int |  |  | refuse/gc when total allocated bytes exceed B |
| `--max-inodes` | int |  |  | refuse/gc when total inodes exceed N |
| `--max-session-bytes` | int |  |  | refuse/gc when one session exceeds B bytes |
| `--max-session-inodes` | int |  |  | refuse/gc when one session exceeds N inodes |
| `--min-free` | int |  |  | refuse/gc when the filesystem has fewer than B free bytes |
| `--model` |  |  |  | optional model label for provenance |
| `--run-id` |  |  |  | optional run identifier for provenance |

### `ozagia tmp adopt`

```
usage: ozagia tmp adopt [-h] [--root ROOT] [--json] --reason REASON
                        [--kind {scratch,agent,check,eval,engine,cache,other}]
                        [--session S] [--harness H] [--ttl-s TTL_S] [--owned]
                        [--mark] [--model NAME] [--run-id ID]
                        PATH
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `PATH` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--reason` |  |  |  | short label (at most 200 characters) |
| `--kind` |  |  | `other` | — |
| `--session` |  |  |  | session id (never invented when omitted) |
| `--harness` |  |  |  | harness name for --session |
| `--ttl-s` | int |  |  | expiry in seconds (60 to 90 days) |
| `--owned` |  |  |  | allow cleanup of this entry |
| `--mark` |  |  |  | write the ownership marker (needs --owned) |
| `--model` |  |  |  | optional model label for provenance |
| `--run-id` |  |  |  | optional run identifier for provenance |

### `ozagia tmp release`

```
usage: ozagia tmp release [-h] [--root ROOT] [--json] [--delete] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--delete` |  |  |  | delete an owned, marker-matched tree |

### `ozagia tmp pin`

```
usage: ozagia tmp pin [-h] [--root ROOT] [--json] [--until TIME] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--until` | ozagia.temp_cli._timestamp |  |  | optional epoch second or ISO-8601 pin metadata |

### `ozagia tmp unpin`

```
usage: ozagia tmp unpin [-h] [--root ROOT] [--json] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia tmp touch`

```
usage: ozagia tmp touch [-h] [--root ROOT] [--json] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia tmp run`

```
usage: ozagia tmp run [-h] [--root ROOT] [--json] --reason REASON
                      [--kind {scratch,agent,check,eval,engine,cache,other}]
                      [--ttl-s TTL_S] [--keep] [--timeout S]
                      [--max-total-bytes B] [--max-inodes N]
                      [--max-session-bytes B] [--max-session-inodes N]
                      [--min-free B]
                      ...
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `CMD` |  | the remainder | yes | command and arguments after -- |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--reason` |  |  |  | short label (at most 200 characters) |
| `--kind` |  |  | `scratch` | — |
| `--ttl-s` | int |  |  | expiry in seconds (60 to 90 days) |
| `--keep` |  |  |  | keep the entry even when the command succeeds |
| `--timeout` | ozagia.temp_cli._positive_seconds |  |  | kill the command after S seconds (exit 124, keep the entry) |
| `--max-total-bytes` | int |  |  | refuse/gc when total allocated bytes exceed B |
| `--max-inodes` | int |  |  | refuse/gc when total inodes exceed N |
| `--max-session-bytes` | int |  |  | refuse/gc when one session exceeds B bytes |
| `--max-session-inodes` | int |  |  | refuse/gc when one session exceeds N inodes |
| `--min-free` | int |  |  | refuse/gc when the filesystem has fewer than B free bytes |

### `ozagia tmp clean`

```
usage: ozagia tmp clean [-h] [--root ROOT] [--json] [--apply] [--expired]
                        [--older-than D] [--max-total-bytes B] [--id ID]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--apply` |  |  |  | perform the plan (the default is a dry run) |
| `--expired` |  |  |  | select entries whose TTL has elapsed |
| `--older-than` | ozagia.temp_cli._duration |  |  | select entries unused for D (bare = days; s/m/h/d suffixes accepted) |
| `--max-total-bytes` | int |  |  | delete oldest eligible entries until the total is at most B bytes |
| `--id` |  |  |  | restrict to ID (repeatable) |

### `ozagia tmp gc`

```
usage: ozagia tmp gc [-h] [--root ROOT] [--json] [--apply]
                     [--max-total-bytes B] [--max-inodes N]
                     [--max-session-bytes B] [--max-session-inodes N]
                     [--min-free B] [--older-than D]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--apply` |  |  |  | perform the plan (the default is a dry run) |
| `--max-total-bytes` | int |  |  | refuse/gc when total allocated bytes exceed B |
| `--max-inodes` | int |  |  | refuse/gc when total inodes exceed N |
| `--max-session-bytes` | int |  |  | refuse/gc when one session exceeds B bytes |
| `--max-session-inodes` | int |  |  | refuse/gc when one session exceeds N inodes |
| `--min-free` | int |  |  | refuse/gc when the filesystem has fewer than B free bytes |
| `--older-than` | ozagia.temp_cli._duration |  |  | only entries unused for D (bare = days; s/m/h/d suffixes accepted) |

### `ozagia tmp clean-legacy`

```
usage: ozagia tmp clean-legacy [-h] [--root ROOT] [--json] [--apply]
                               [--older-than D] [--include-caches]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--apply` |  |  |  | perform the plan (the default is a dry run) |
| `--older-than` | ozagia.temp_cli._duration |  |  | only artifacts older than D (bare = days; s/m/h/d suffixes accepted; default 1 day) |
| `--include-caches` |  |  |  | also consider regenerable benchmark cache roots |

### `ozagia tmp path`

```
usage: ozagia tmp path [-h] [--root ROOT] [--json]
                       [--kind {scratch,agent,check,eval,engine,cache,other}]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--kind` |  |  | `scratch` | — |

## `ozagia assets`

```
usage: ozagia assets [-h]
                     {list,status,scan,discover,register,adopt,verify,pin,unpin,forget,gc,policy} ...
```

### `ozagia assets list`

```
usage: ozagia assets list [-h] [--root ROOT] [--json]
                          [--owner {ozagia,ocelot,huggingface,user,upstream,external}]
                          [--kind {dataset,model,corpus,install,cache,tool,other}]
                          [--state {present,missing,partial,verified,mismatch,unverified}]
                          [--pinned]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--owner` |  |  |  | only this owner |
| `--kind` |  |  |  | only this kind |
| `--state` |  |  |  | only this state |
| `--pinned` |  |  |  | only pinned assets |

### `ozagia assets status`

```
usage: ozagia assets status [-h] [--root ROOT] [--json] [--path P] [--top N]
                            [ID]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--path` |  |  |  | show the asset recorded at this path |
| `--top` | ozagia.assets_cli._non_negative |  |  | also list the N largest assets |

### `ozagia assets scan`

```
usage: ozagia assets scan [-h] [--root ROOT] [--json] [--path P [P ...]]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--path` |  | one or more |  | explicit path to inspect for a marker (repeatable; the home directory is refused) |

### `ozagia assets discover`

```
usage: ozagia assets discover [-h] [--root ROOT] [--json] [--limit N]
                              [--no-research]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--limit` | ozagia.assets_cli._non_negative |  |  | most candidate entries to examine |
| `--no-research` |  |  |  | skip the research cache/fetch roots |

### `ozagia assets register`

```
usage: ozagia assets register [-h] [--root ROOT] [--json] --path P
                              --kind {dataset,model,corpus,install,cache,tool,other}
                              --owner {ozagia,ocelot,huggingface,user,upstream,external}
                              [--scope {user,repo,shared,system}]
                              [--source-url URL]
                              [--source-kind {url,huggingface,git,local,manifest,unknown}]
                              [--dataset D] [--revision R] [--commit C]
                              [--version V] [--sha256 S] [--license SPDX]
                              [--pinned] [--deletable | --no-delete] [--mark]
                              [--id ID] [--from-hf-metadata] [--reason TEXT]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--path` |  |  |  | — |
| `--kind` |  |  |  | — |
| `--owner` |  |  |  | — |
| `--scope` |  |  | `user` | — |
| `--source-url` |  |  |  | — |
| `--source-kind` |  |  |  | — |
| `--dataset` |  |  |  | dataset identifier (for example a Hugging Face repository) |
| `--revision` |  |  |  | — |
| `--commit` |  |  |  | — |
| `--version` |  |  |  | — |
| `--sha256` |  |  |  | explicit SHA-256 of a single file |
| `--license` |  |  |  | explicit licence identifier; never inferred |
| `--pinned` |  |  |  | never a GC candidate |
| `--deletable` |  |  |  | the owner permits explicit deletion |
| `--no-delete` |  |  |  | explicitly record that deletion is not permitted (the default) |
| `--mark` |  |  |  | write .ozagia-asset.json (owned assets only) |
| `--id` |  |  |  | — |
| `--from-hf-metadata` |  |  |  | read <path>/.cache/huggingface/download/**/*.metadata for the revision and per-file etags; never opens or hashes the data |
| `--reason` |  |  | `` | short label, never contents or secrets |

### `ozagia assets adopt`

```
usage: ozagia assets adopt [-h] [--root ROOT] [--json]
                           --kind {dataset,model,corpus,install,cache,tool,other}
                           [--owner {ozagia,ocelot,huggingface,user,upstream,external}]
                           [--scope {user,repo,shared,system}] [--reason TEXT]
                           [--mark]
                           PATH
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `PATH` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--kind` |  |  |  | — |
| `--owner` |  |  | `ozagia` | — |
| `--scope` |  |  | `user` | — |
| `--reason` |  |  | `` | — |
| `--mark` |  |  |  | write .ozagia-asset.json (owner=ozagia only) |

### `ozagia assets verify`

```
usage: ozagia assets verify [-h] [--root ROOT] [--json] [--all] [--hash] [ID]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | verify every asset |
| `--hash` |  |  |  | also read and hash the file or declared digests |

### `ozagia assets pin`

```
usage: ozagia assets pin [-h] [--root ROOT] [--json] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia assets unpin`

```
usage: ozagia assets unpin [-h] [--root ROOT] [--json] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia assets forget`

```
usage: ozagia assets forget [-h] [--root ROOT] [--json] ID
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `ID` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia assets gc`

```
usage: ozagia assets gc [-h] [--root ROOT] [--json] [--max-bytes B]
                        [--max-inodes N] [--older-than D] [--apply]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--max-bytes` | ozagia.assets_cli._non_negative |  |  | — |
| `--max-inodes` | ozagia.assets_cli._non_negative |  |  | — |
| `--older-than` | ozagia.assets_cli._duration |  |  | only unused for D (bare = days; s/m/h/d suffixes accepted) |
| `--apply` |  |  |  | refused in this stage; the plan is dry-run only |

### `ozagia assets policy`

```
usage: ozagia assets policy [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

## `ozagia mesh`

```
usage: ozagia mesh [-h]
                   {status,peers,register,unregister,heartbeat,send,inbox,read,ack,lease,worktree,merge,link,delegation,ozma,prune} ...
```

### `ozagia mesh status`

```
usage: ozagia mesh status [-h] [--root ROOT] [--json] [--all] [--strict]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | include ended, dead and foreign sessions |
| `--strict` |  |  |  | exit 1 when a stale, dead or orphaned entry is present |

### `ozagia mesh peers`

```
usage: ozagia mesh peers [-h] [--root ROOT] [--json] [--all] [--strict]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | include ended, dead and foreign sessions |
| `--strict` |  |  |  | exit 1 when a stale, dead or orphaned entry is present |

### `ozagia mesh register`

```
usage: ozagia mesh register [-h] [--root ROOT] [--json] [--harness HARNESS]
                            [--session SESSION] [--pid PID]
                            [--worktree WORKTREE] [--branch BRANCH]
                            [--exposure {none,read,coordinate,supervise}]
                            [--source {cli,harness,host,model,other}]
                            [--capability CAPABILITY]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--pid` | int |  |  | — |
| `--worktree` |  |  |  | — |
| `--branch` |  |  |  | — |
| `--exposure` |  |  | `none` | — |
| `--source` |  |  | `cli` | — |
| `--capability` |  |  |  | — |

### `ozagia mesh unregister`

```
usage: ozagia mesh unregister [-h] [--root ROOT] [--json] [--harness HARNESS]
                              [--session SESSION] [--release-leases]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--release-leases` |  |  |  | release active leases held by this session |

### `ozagia mesh heartbeat`

```
usage: ozagia mesh heartbeat [-h] [--root ROOT] [--json] [--harness HARNESS]
                             [--session SESSION] [--pid PID]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--pid` | int |  |  | — |

### `ozagia mesh send`

```
usage: ozagia mesh send [-h] [--root ROOT] [--json] [--harness HARNESS]
                        [--session SESSION] (--to H:S | --broadcast)
                        --kind {hello,request,offer,accept,reject,status,release,note,block,unblock,handoff}
                        --subject SUBJECT [--body BODY | --body-file F]
                        [--in-reply-to IN_REPLY_TO] [--lease LEASE]
                        [--ttl TTL]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--to` |  |  |  | — |
| `--broadcast` |  |  |  | — |
| `--kind` |  |  |  | — |
| `--subject` |  |  |  | — |
| `--body` |  |  |  | — |
| `--body-file` |  |  |  | — |
| `--in-reply-to` |  |  |  | — |
| `--lease` |  |  |  | — |
| `--ttl` | int |  | 3600 | — |

### `ozagia mesh inbox`

```
usage: ozagia mesh inbox [-h] [--root ROOT] [--json] [--harness HARNESS]
                         [--session SESSION] [--unread] [--delegation ID]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--unread` |  |  |  | — |
| `--delegation` |  |  |  | — |

### `ozagia mesh read`

```
usage: ozagia mesh read [-h] [--root ROOT] [--json] [--harness HARNESS]
                        [--session SESSION] [--peek] [--delegation ID]
                        msg_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `msg_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--peek` |  |  |  | do not mark the message read |
| `--delegation` |  |  |  | — |

### `ozagia mesh ack`

```
usage: ozagia mesh ack [-h] [--root ROOT] [--json] [--harness HARNESS]
                       [--session SESSION]
                       msg_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `msg_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |

### `ozagia mesh lease`

```
usage: ozagia mesh lease [-h] {list,status,acquire,renew,release,takeover} ...
```

#### `ozagia mesh lease list`

```
usage: ozagia mesh lease list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | ==SUPPRESS== |

#### `ozagia mesh lease status`

```
usage: ozagia mesh lease status [-h] [--root ROOT] [--json] [lease_id]
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `lease_id` |  | ? | no | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

#### `ozagia mesh lease acquire`

```
usage: ozagia mesh lease acquire [-h] [--root ROOT] [--json]
                                 [--harness HARNESS] [--session SESSION]
                                 --kind {worktree,branch,merge,path,grant,other}
                                 --id RESOURCE --mode {read,write,merge}
                                 [--ttl TTL] [--reason REASON]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--kind` |  |  |  | — |
| `--id` |  |  |  | — |
| `--mode` |  |  |  | — |
| `--ttl` | int |  | 900 | — |
| `--reason` |  |  | `` | — |

#### `ozagia mesh lease renew`

```
usage: ozagia mesh lease renew [-h] [--root ROOT] [--json] [--harness HARNESS]
                               [--session SESSION] [--ttl TTL]
                               lease_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `lease_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--ttl` | int |  |  | — |

#### `ozagia mesh lease release`

```
usage: ozagia mesh lease release [-h] [--root ROOT] [--json]
                                 [--harness HARNESS] [--session SESSION]
                                 [--delegation ID]
                                 lease_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `lease_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--delegation` |  |  |  | — |

#### `ozagia mesh lease takeover`

```
usage: ozagia mesh lease takeover [-h] [--root ROOT] [--json]
                                  [--harness HARNESS] [--session SESSION]
                                  [--confirm] --reason REASON [--ttl TTL]
                                  lease_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `lease_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--confirm` |  |  |  | required; takeover is deliberately explicit |
| `--reason` |  |  |  | — |
| `--ttl` | int |  | 900 | — |

### `ozagia mesh worktree`

```
usage: ozagia mesh worktree [-h] {request,offer} ...
```

#### `ozagia mesh worktree request`

```
usage: ozagia mesh worktree request [-h] [--root ROOT] [--json]
                                    [--harness HARNESS] [--session SESSION]
                                    [--branch BRANCH] [--path PATH]
                                    [--reason REASON]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--branch` |  |  |  | — |
| `--path` |  |  |  | — |
| `--reason` |  |  | `` | — |

#### `ozagia mesh worktree offer`

```
usage: ozagia mesh worktree offer [-h] [--root ROOT] [--json]
                                  [--harness HARNESS] [--session SESSION]
                                  [--branch BRANCH] [--path PATH] [--lease]
                                  [--to H:S | --broadcast]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--branch` |  |  |  | — |
| `--path` |  |  |  | — |
| `--lease` |  |  |  | also acquire read leases |
| `--to` |  |  |  | — |
| `--broadcast` |  |  |  | — |

### `ozagia mesh merge`

```
usage: ozagia mesh merge [-h] {request} ...
```

#### `ozagia mesh merge request`

```
usage: ozagia mesh merge request [-h] [--root ROOT] [--json]
                                 [--harness HARNESS] [--session SESSION]
                                 --target BRANCH|PATH [--ttl TTL]
                                 [--reason REASON]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--target` |  |  |  | — |
| `--ttl` | int |  | 900 | — |
| `--reason` |  |  | `` | — |

### `ozagia mesh link`

```
usage: ozagia mesh link [-h] {request,accept,decline,revoke,list,show} ...
```

#### `ozagia mesh link request`

```
usage: ozagia mesh link request [-h] [--root ROOT] [--json]
                                [--harness HARNESS] [--session SESSION]
                                --to H:S --relation {peer,sub,super}
                                [--ttl TTL] [--reason REASON]
                                [--idempotency-key K]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--to` |  |  |  | — |
| `--relation` |  |  |  | — |
| `--ttl` | int |  | 3600 | — |
| `--reason` |  |  | `` | — |
| `--idempotency-key` |  |  |  | — |

#### `ozagia mesh link accept`

```
usage: ozagia mesh link accept [-h] [--root ROOT] [--json] [--harness HARNESS]
                               [--session SESSION]
                               link_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `link_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |

#### `ozagia mesh link decline`

```
usage: ozagia mesh link decline [-h] [--root ROOT] [--json]
                                [--harness HARNESS] [--session SESSION]
                                [--reason REASON]
                                link_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `link_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--reason` |  |  | `` | — |

#### `ozagia mesh link revoke`

```
usage: ozagia mesh link revoke [-h] [--root ROOT] [--json] [--harness HARNESS]
                               [--session SESSION] [--reason REASON]
                               link_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `link_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--reason` |  |  | `` | — |

#### `ozagia mesh link list`

```
usage: ozagia mesh link list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

#### `ozagia mesh link show`

```
usage: ozagia mesh link show [-h] [--root ROOT] [--json] link_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `link_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia mesh delegation`

```
usage: ozagia mesh delegation [-h] {grant,revoke,list,show} ...
```

#### `ozagia mesh delegation grant`

```
usage: ozagia mesh delegation grant [-h] [--root ROOT] [--json]
                                    [--harness HARNESS] [--session SESSION]
                                    --to H:S
                                    --action {lease_release,mailbox_read,merge_on_behalf}
                                    [--lease ID] [--mailbox] [--ttl TTL]
                                    [--max-uses MAX_USES] [--reason REASON]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--to` |  |  |  | — |
| `--action` |  |  |  | — |
| `--lease` |  |  |  | — |
| `--mailbox` |  |  |  | — |
| `--ttl` | int |  | 3600 | — |
| `--max-uses` | int |  | 1 | — |
| `--reason` |  |  | `` | — |

#### `ozagia mesh delegation revoke`

```
usage: ozagia mesh delegation revoke [-h] [--root ROOT] [--json]
                                     [--harness HARNESS] [--session SESSION]
                                     [--reason REASON]
                                     delegation_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `delegation_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--reason` |  |  | `` | — |

#### `ozagia mesh delegation list`

```
usage: ozagia mesh delegation list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

#### `ozagia mesh delegation show`

```
usage: ozagia mesh delegation show [-h] [--root ROOT] [--json] delegation_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `delegation_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia mesh ozma`

```
usage: ozagia mesh ozma [-h]
                        {status,map,check,outbox,inbox,read,ack,flight,handoff,block,unblock,blockers,wake,claims,enqueue,claim,sync} ...
```

#### `ozagia mesh ozma status`

```
usage: ozagia mesh ozma status [-h] [--root ROOT] [--json] [--harness HARNESS]
                               [--session SESSION]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |

#### `ozagia mesh ozma map`

```
usage: ozagia mesh ozma map [-h] [--root ROOT] [--json] [--harness HARNESS]
                            [--session SESSION] [--save]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--save` |  |  |  | write the local mapping record |

#### `ozagia mesh ozma check`

```
usage: ozagia mesh ozma check [-h] [--root ROOT] [--json] [--harness HARNESS]
                              [--session SESSION] [--require-ready] [--live]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--require-ready` |  |  |  | exit 1 when the configured mode is not ready |
| `--live` |  |  |  | request a live check; unavailable in this slice and never contacts the network |

#### `ozagia mesh ozma outbox`

```
usage: ozagia mesh ozma outbox [-h] [--root ROOT] [--json] [--all]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--all` |  |  |  | include sent and acked messages |

#### `ozagia mesh ozma inbox`

```
usage: ozagia mesh ozma inbox [-h] [--root ROOT] [--json] [--harness HARNESS]
                              [--session SESSION] [--unread]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--unread` |  |  |  | — |

#### `ozagia mesh ozma read`

```
usage: ozagia mesh ozma read [-h] [--root ROOT] [--json] msg_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `msg_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

#### `ozagia mesh ozma ack`

```
usage: ozagia mesh ozma ack [-h] [--root ROOT] [--json] [--harness HARNESS]
                            [--session SESSION] [--accepted | --rejected]
                            [--reason REASON]
                            msg_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `msg_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--accepted` |  |  |  | also queue a typed ack{accepted:true} (requires host/coordinate) |
| `--rejected` |  |  |  | also queue a typed ack{accepted:false} (requires host/coordinate) |
| `--reason` |  |  | `` | — |

#### `ozagia mesh ozma flight`

```
usage: ozagia mesh ozma flight [-h] [--root ROOT] [--json] [--harness HARNESS]
                               [--session SESSION] [--limit LIMIT]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--limit` | int |  | 64 | most members to list (at most 64) |

#### `ozagia mesh ozma handoff`

```
usage: ozagia mesh ozma handoff [-h] {list,show,accept,reject,complete} ...
```

##### `ozagia mesh ozma handoff list`

```
usage: ozagia mesh ozma handoff list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

##### `ozagia mesh ozma handoff show`

```
usage: ozagia mesh ozma handoff show [-h] [--root ROOT] [--json] handoff_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `handoff_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

##### `ozagia mesh ozma handoff accept`

```
usage: ozagia mesh ozma handoff accept [-h] [--root ROOT] [--json]
                                       [--harness HARNESS] [--session SESSION]
                                       [--reason REASON]
                                       handoff_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `handoff_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--reason` |  |  | `` | — |

##### `ozagia mesh ozma handoff reject`

```
usage: ozagia mesh ozma handoff reject [-h] [--root ROOT] [--json]
                                       [--harness HARNESS] [--session SESSION]
                                       [--reason REASON]
                                       handoff_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `handoff_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--reason` |  |  | `` | — |

##### `ozagia mesh ozma handoff complete`

```
usage: ozagia mesh ozma handoff complete [-h] [--root ROOT] [--json]
                                         [--harness HARNESS]
                                         [--session SESSION] [--reason REASON]
                                         handoff_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `handoff_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--reason` |  |  | `` | — |

#### `ozagia mesh ozma block`

```
usage: ozagia mesh ozma block [-h] [--root ROOT] [--json] --blocked ID
                              --blocker ID [--workunit W] [--resource R]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--blocked` |  |  |  | — |
| `--blocker` |  |  |  | — |
| `--workunit` |  |  |  | — |
| `--resource` |  |  |  | — |

#### `ozagia mesh ozma unblock`

```
usage: ozagia mesh ozma unblock [-h] [--root ROOT] [--json] [--id ID]
                                [--workunit W]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--id` |  |  |  | — |
| `--workunit` |  |  |  | — |

#### `ozagia mesh ozma blockers`

```
usage: ozagia mesh ozma blockers [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

#### `ozagia mesh ozma wake`

```
usage: ozagia mesh ozma wake [-h] {list,approve,revoke,claim} ...
```

##### `ozagia mesh ozma wake list`

```
usage: ozagia mesh ozma wake list [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

##### `ozagia mesh ozma wake approve`

```
usage: ozagia mesh ozma wake approve [-h] [--root ROOT] [--json]
                                     [--harness HARNESS] [--session SESSION]
                                     --mode {message-triggered,scheduled}
                                     --budget-turns N [--ttl S] [--agent ID]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--mode` |  |  |  | — |
| `--budget-turns` | int |  |  | — |
| `--ttl` | int |  | 3600 | — |
| `--agent` |  |  |  | — |

##### `ozagia mesh ozma wake revoke`

```
usage: ozagia mesh ozma wake revoke [-h] [--root ROOT] [--json]
                                    [--harness HARNESS] [--session SESSION]
                                    approval_id
```

| argument | type | values | required | help |
|---|---|---|---|---|
| `approval_id` |  |  | yes | — |

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |

##### `ozagia mesh ozma wake claim`

```
usage: ozagia mesh ozma wake claim [-h] [--root ROOT] [--json]
                                   [--harness HARNESS] [--session SESSION]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |

#### `ozagia mesh ozma claims`

```
usage: ozagia mesh ozma claims [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

#### `ozagia mesh ozma enqueue`

```
usage: ozagia mesh ozma enqueue [-h] [--root ROOT] [--json]
                                [--harness HARNESS] [--session SESSION]
                                --message ID [--channel C]
                                [--offer-as {response,handoff}]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--message` |  |  |  | — |
| `--channel` |  |  |  | — |
| `--offer-as` |  |  |  | — |

#### `ozagia mesh ozma claim`

```
usage: ozagia mesh ozma claim [-h] [--root ROOT] [--json] [--harness HARNESS]
                              [--session SESSION] --lease ID [--merge]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--lease` |  |  |  | — |
| `--merge` |  |  |  | mark the claim advisory (never merge authority) |

#### `ozagia mesh ozma sync`

```
usage: ozagia mesh ozma sync [-h] [--root ROOT] [--json]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |

### `ozagia mesh prune`

```
usage: ozagia mesh prune [-h] [--root ROOT] [--json] [--harness HARNESS]
                         [--session SESSION] (--expired |
                         --delivered-before S) [--apply]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root |
| `--json` |  |  |  | print JSON |
| `--harness` |  |  |  | harness name (default: OZAGIA_MESH_HARNESS) |
| `--session` |  |  |  | session id (default: OZAGIA_MESH_SESSION) |
| `--expired` |  |  |  | — |
| `--delivered-before` | float |  |  | — |
| `--apply` |  |  |  | delete the planned messages |
| `--root-ignored` |  |  |  | ==SUPPRESS== |
