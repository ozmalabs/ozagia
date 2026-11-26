# `ozagia-eval`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/eval/agent.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/eval/agent.py` (`ozagia-eval` entry point).

# `ozagia-eval`

ozagia.eval.agent — end-to-end task-success harness for coding agents.

```
usage: ozagia-eval [-h] {validate,run,report} ...
```

## `ozagia-eval validate`

```
usage: generate_docs.py validate [-h] [--tasks TASKS] [--repo REPO]
                                 [--base BASE] [--task-ids TASK_IDS]
                                 [--python PYTHON] [--run-root RUN_ROOT]
                                 [--keep] [--out-json OUT_JSON]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--tasks` |  |  | `/home/matt/ozmadev/ozagia/evaluation/agent_e2e/tasks.json` | — |
| `--repo` |  |  | `/home/matt/ozmadev/ozagia` | git repo to create worktrees from |
| `--base` |  |  |  | base commit (default: tasks file) |
| `--task-ids` |  |  |  | comma-separated subset |
| `--python` |  |  |  | python for the checks and the agent's `python` (default: the interpreter `ozagia env --for` picks for the checks' test files in --repo) |
| `--run-root` |  |  |  | scratch root (default: a new dir under /tmp/ozagia_eval) |
| `--keep` |  |  |  | keep worktrees |
| `--out-json` |  |  |  | — |

## `ozagia-eval run`

```
usage: generate_docs.py run [-h] [--tasks TASKS] [--repo REPO] [--base BASE]
                            [--task-ids TASK_IDS] [--python PYTHON]
                            [--run-root RUN_ROOT] [--keep]
                            [--out-json OUT_JSON]
                            [--driver {claude,fake,native,noop,opencode}]
                            [--model MODEL] [--arms ARMS] [--repeats REPEATS]
                            [--timeout-s TIMEOUT_S] [--live]
                            [--max-budget-usd MAX_BUDGET_USD]
                            [--max-total-usd MAX_TOTAL_USD] [--out-md OUT_MD]
                            [--skip-validate | --validation VALIDATION]
                            [--amortize N] [--no-repeat-index]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--tasks` |  |  | `/home/matt/ozmadev/ozagia/evaluation/agent_e2e/tasks.json` | — |
| `--repo` |  |  | `/home/matt/ozmadev/ozagia` | git repo to create worktrees from |
| `--base` |  |  |  | base commit (default: tasks file) |
| `--task-ids` |  |  |  | comma-separated subset |
| `--python` |  |  |  | python for the checks and the agent's `python` (default: the interpreter `ozagia env --for` picks for the checks' test files in --repo) |
| `--run-root` |  |  |  | scratch root (default: a new dir under /tmp/ozagia_eval) |
| `--keep` |  |  |  | keep worktrees |
| `--out-json` |  |  |  | — |
| `--driver` |  |  |  | agent backend (default: $OZAGIA_EVAL_DRIVER) |
| `--model` |  |  |  | backend model, e.g. vllm/qwen3.8-27b for opencode (default: the backend's own configured default; for native: $OZAGIA_NATIVE_MODEL) |
| `--arms` |  |  |  | comma-separated subset of baseline,dev_memory,graph_intent (default: every arm the driver supports; graph_intent needs opencode/native/fake/noop) |
| `--repeats` | int |  | 1 | — |
| `--timeout-s` | int |  | 900 | wall-clock limit per driver session |
| `--live` |  |  |  | required, with OZAGIA_EVAL_LIVE=1, for claude, opencode and native |
| `--max-budget-usd` | float |  |  | claude only: passed as --max-budget-usd (per session) |
| `--max-total-usd` | float |  |  | any backend: stop once reported session cost exceeds this |
| `--out-md` |  |  |  | — |
| `--skip-validate` |  |  |  | start without validating the selected tasks first (by default `run` refuses with exit 2 when a check does not pass on base or does not fail after the break); invalid runs are then decided by an import probe |
| `--validation` |  |  |  | reuse a `validate --out-json` report instead of validating again; it must have the same base, every selected task unchanged and the same check interpreter, else `run` refuses with exit 2 |
| `--amortize` | int |  |  | N of the amortized view (pay-once / N + per task; default: the number of tasks run) |
| `--no-repeat-index` |  |  |  | skip the repeat index cost (snapshot load + refresh + tag cache) measured once per run root |

## `ozagia-eval report`

```
usage: generate_docs.py report [-h] --in IN_JSON [--logs-root LOGS_ROOT]
                               [--amortize N] [--repo REPO] [--out-md OUT_MD]
                               [--out-json OUT_JSON]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--in` |  |  |  | run JSON |
| `--logs-root` |  |  |  | where the run logs are now: a run root (with logs/) or a logs dir (default: each run's recorded log_dir) |
| `--amortize` | int |  |  | N of the amortized view (default: the number of tasks) |
| `--repo` |  |  |  | the source repo, for first-party names and the environment detail when classifying runs recorded before failure classes (default: none) |
| `--out-md` |  |  |  | — |
| `--out-json` |  |  |  | — |
