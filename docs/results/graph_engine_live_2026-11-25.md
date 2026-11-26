# Graph engine HTTP transport: live-model evaluation (2026-11-25)

Status: **the live gate is satisfied for the engine HTTP transport, but the
engine arm still loses the model-task comparison in this sample.** The first
sample exposed a real interface defect: the engine omitted repeated column
headers, so the model could not parse later `shape` rows and called `shape` six
times. That defect is fixed (columns are now included on every response by
default), the model-facing tool schema is smaller, and the sample was rerun.
The fix removed the repeated `shape` calls, but the engine arm still used more
model tokens than the stateless control and failed both of its
`repository_shape` runs (4/6 task runs accepted) because the model spent the
freed budget on extra `fetch` calls and returned dotted module names. This is a
small measured sample, not a general model-task claim.

> **Follow-up (2026-11-25):** after this sample, the model-facing interface was
> changed (conditional `fetch`, inline ready speculation, a smaller optimized
> schema and explicit shape file paths) and rerun with the same tasks/checks.
> The optimized arm accepted 6/6, used 18,118 model tokens versus 17,762 for the
> control and 35,752 for the legacy engine interface, and matched the control's
> 20 tool calls. See
> [`graph_engine_live_2026-11-25_optimized.md`](graph_engine_live_2026-11-25_optimized.md).
> The conclusion below (the old engine arm still lost) is unchanged for the old
> interface.

## Change under test

1. **Default columns.** `GraphEngine` now includes per-op `c` columns on every
   compact tabular response by default. Callers that track columns can opt into
   the old per-session reuse with `cols: false` or an explicit `compact: true`;
   the first reuse response teaches the columns and later reuse requests with
   the same columns omit them. `cols: true` forces them back. The rest of the
   v2 wire remains compact (short keys, hard budgets, spec/fetch).
2. **Smaller model-facing schema.** The live driver's engine tool now exposes
   only `op`, `q`, `key`, `keys`, `budget`, `spec`, `fetch` and `h`
   (`additionalProperties: false`). `session`, `known`, `wait_ms` and `stats`
   are transport/driver details and were removed from the model-facing schema;
   the driver still manages sessions, fetch waits and stats internally.

## Run context

| Item | Value |
| --- | --- |
| Model id | `qwen3.8-27b` (vLLM, OpenAI-compatible chat) |
| Model root | `/app/models/Swift-1.5-Qwen3.8-27B-W4A16-HyperQwen` |
| Context length | 131,072 tokens |
| Host | `queen` (`matt@192.168.40.215`), 24 CPUs, Python 3.12.3 |
| GPU | NVIDIA GeForce RTX 3090, 24,576 MiB, driver 610.57.04 |
| GPU evidence | `VLLM::EngineCore` used 22,818 MiB; the host's other GPU is a 6,144 MiB GTX 1660 SUPER |
| vLLM shape | `--max-num-seqs 4`, `--gpu-memory-utilization 0.93`, speculative `dflash` (7 tokens), prefix caching, auto tool choice with `qwen3_coder` |
| Transport | local SSH tunnel `127.0.0.1:18020` to queen's `127.0.0.1:18020` |
| Endpoint key | `/tmp/opencode/queen/key`, mode `0600`; read by the driver, never printed |
| Engine token | `/tmp/opencode/engine-live/.ozagia/engine/token`, mode `0600`; never printed |

The host query was read-only:
`nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader`
and `nvidia-smi --query-compute-apps=pid,process_name,used_memory`.

## Fixture

`/tmp/opencode/engine-live`, outside this repository, is a tiny git repository
with 10 tracked files: `pkg/__init__.py`, `pkg/core.py`, `pkg/metrics.py`,
`pkg/report.py`, three test files, `README.md`, `pyproject.toml`, and
`.gitignore`. The graph had 24 nodes and 55 edges (Commit 2, Doc 1, File 10,
Repo 1, Symbol 5, Test 5). Repo shape reported 4 source files, 3 test files,
4 modules, and 7 Python files.

The snapshot was built with the current CLI, parse-only; no target code was
imported or executed and no network was used by the index:

```bash
cd /home/matt/ozma/ozagia
.venv/bin/python -m ozagia index --root /tmp/opencode/engine-live --json
```

The final `index` check reported `"action": "fresh"`. The fixture was committed,
so the engine could verify HEAD and the working-tree digest. A `.gitignore`
excludes `.ozagia/`, logs and caches; ignored files still contribute to the
freshness digest, so nothing may be written inside the fixture during a run.

## Engine transport startup and verification

```bash
.venv/bin/python -m ozagia engine token --root /tmp/opencode/engine-live
.venv/bin/python -m ozagia engine serve --root /tmp/opencode/engine-live --port 0
```

Observed before the model run:

```text
token_mode=600
healthz_status=200
{"ok":true,"service":"ozagia.engine.http","version":"ozagia.graph_engine.http/1","graph":"ozagia.graph_engine/2","read_only":true}
unauth_graph_status=401
{"ok":false,"protocol":"ozagia.graph_engine.http/1","error":{"code":"unauthorized","message":"missing or invalid bearer token"}}
hello_fresh=fresh protocol=ozagia.graph_engine/2 sid_present=True
find_rows=2 first_key=sym:pkg/metrics.py::average engine_t=95
```

The post-fix server also confirmed the new column behavior directly:

```text
shape1 c ['sec', 'k', 'v'] rows 18 t 214
shape2 c ['sec', 'k', 'v'] rows 18 t 214
find1 c ['k', 'n', 'f', 'sp', 'sig', 'sum', 'sc'] rows 7 t 255
find2 c ['k', 'n', 'f', 'sp', 'sig', 'sum', 'sc'] rows 7 t 255
opt-out first c? True second c? False
```

`/healthz` is open and read-only, `/v1/graph` requires the bearer token, and
authenticated `hello` + `find` works over the transport. Authentication was not
bypassed by the driver.

## Live driver

`scripts/eval_graph_engine_live.py` requires both `--live` and
`OZAGIA_EVAL_LIVE=1`; without either it exits 2 before reading a model. It reads
the model base URL/key from `OZAGIA_LIVE_BASE_URL`/`OZAGIA_LIVE_API_KEY`, or the
0600 key file; it reads the engine token from `OZAGIA_ENGINE_TOKEN` or the 0600
file. It never prints either secret.

The model sees exactly one tool, `graph`. The post-fix engine schema is:

```json
{"type":"object",
 "properties":{"op":{"type":"string",
                    "enum":["find","outline","expand","impact","tests",
                            "context","shape","fetch"]},
               "q":{"type":"string"},
               "key":{"type":"string"},
               "keys":{"type":"array","items":{"type":"string"}},
               "budget":{"type":"integer"},
               "spec":{"type":"boolean"},
               "fetch":{"type":"array","items":{"type":"string"}},
               "h":{"type":"string"}},
 "required":["op"],
 "additionalProperties":false}
```

The control arm exposes one-shot read ops (`find`, `outline`, `expand`,
`impact`, `tests`, `shape`) with `op/q/key/keys/budget` and the same
`additionalProperties: false`. The driver executes every tool call over
`POST /v1/graph`, creates, refreshes and closes the engine session itself, and
retries once on an unknown session. Transport, auth and model errors abort the
run; no synthetic result is written.

Model request settings: `temperature=0`, `top_p=1`, `seed=0`,
`enable_thinking=false`, at most 6 tool calls, 320 output tokens per turn and
220 for a finalization turn. Requests were strictly sequential; no concurrent
model requests were made against the 3090. Usage is the server's
`prompt_tokens`/`completion_tokens`/`total_tokens`, independent of OpenCode. The
server reported `reasoning_tokens=0` for every request. After the tool-call
budget, one finalization turn is sent without tools so the model can only answer
in text.

Exact post-fix run:

```bash
OZAGIA_EVAL_LIVE=1 .venv/bin/python scripts/eval_graph_engine_live.py \
  --live \
  --engine-url http://127.0.0.1:33437 \
  --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
  --repeat 2 \
  --out /tmp/opencode/engine-live-results/eval-postfix.json
```

The pre-fix sample was produced with the same fixture, model, prompts and
sampling settings before the column/schema change (engine URL `:36677`):

```bash
OZAGIA_EVAL_LIVE=1 .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --engine-url http://127.0.0.1:36677 \
  --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
  --repeat 2 --out /tmp/opencode/engine-live-results/eval.json
```

Measurement JSON and raw driver logs are under
`/tmp/opencode/engine-live-results/` (outside the fixture and this repository).
`eval.json` is the pre-fix sample; `eval-postfix.json` is the post-fix sample.

## Tasks and fixed checks

The three tasks and their fixed acceptance checks were identical in both
samples:

| Task | Prompt | Checks |
| --- | --- | --- |
| `definition_callers_tests` | Find the symbol named average, its direct callers, and the tests that cover it. Reply with only `{"symbol": string, "callers": [string], "tests": [string]}`. | `symbol` contains `average`; `callers` contains `scaled_average` and `summarize`; `tests` contains `test_average` |
| `impact_expansion` | Compute the impact of the symbol add to depth 2. Reply with only `{"symbol": string, "symbols": [string], "tests": [string], "summary": string}`. | `symbol` contains `add`; `symbols` contains `double`, `average`, `scaled_average`, `summarize`; `tests` contains `test_add`; `summary` non-empty |
| `repository_shape` | Describe the repository shape: name, source file count, test file count, Python module paths, and the test command. Reply with only `{"name": string, "source_files": integer, "test_files": integer, "modules": [string], "test_command": string}`. | `name` contains `engine-live`; `source_files == 4`; `test_files == 3`; `modules` contains `pkg/core.py`, `pkg/metrics.py`, `pkg/report.py`; `test_command` contains `pytest` |

A run is accepted only if the final content is one valid JSON object, contains
all required keys, and passes every check. The engine arm used one session per
task, driver-managed `hello`/`bye`, and forced `spec=true` on `find` when the
model did not set it. The control arm used a fresh `hello` and `bye` around each
model call, with no session, `known`, `spec` or `fetch` reuse.

System prompts (the instruction-text difference; the tool schemas also differ):

```text
engine:  You have exactly one tool, graph: a read-only code graph. Ops: find,
         outline, expand, impact, tests, context, shape, fetch. The transport
         creates and closes the session for you; do not call hello or bye. Use
         budgets of at most 800. find may return speculative q handles; fetch
         them with wait_ms 200 when useful. After at most six tool calls, reply
         with only the JSON object the user asks for.

control: You have exactly one tool, graph: a read-only code graph. Ops: find,
         outline, expand, impact, tests, shape. Each call is one-shot. Use
         budgets of at most 800. After at most six tool calls, reply with only
         the JSON object the user asks for.
```

## Results

### Pre-fix sample (columns omitted on repeats, 11-field schema)

`eval.json`, 2026-11-25T12:01:23+1100. All 6 engine runs and 6 control runs were
valid JSON and accepted. The engine arm's `shape` task called `shape` six times
per run.

| Arm | Runs | Accepted | Total model tokens | Tool calls | Model calls | Wall | Engine `t` (all / visible) | Shape calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| engine | 6 | 6 | 43,299 | 28 | 34 | 25.201 s | 6,720 / 5,262 | 6 per shape run |
| control | 6 | 6 | 19,626 | 18 | 22 | 14.939 s | 4,188 / 3,540 | 2 per shape run |

Pre-fix shape task, per run: 6 tool calls, 11,628 model tokens, 6.079/5.634 s
wall, engine `t` 1,497, tool-result 1,175 estimated tokens. The engine's 6
`shape` calls were the visible defect.

### Post-fix sample (columns every response, 8-field schema)

`eval-postfix.json`, 2026-11-25T12:11:19+1100. All 12 runs were valid JSON.
The control arm was accepted 6/6; the engine arm was accepted 4/6 because both
`repository_shape` runs returned dotted module names (`pkg.core` etc.) instead
of the file paths the fixed check requires.

| Arm | Task | Run 1 tool calls / tokens / wall | Run 2 tool calls / tokens / wall | Engine `t` (r1/r2) | Accepted |
| --- | --- | --- | --- | ---: | ---: |
| engine | definition/callers/tests | 4 / 4,303 / 3.011 s | 4 / 4,303 / 2.924 s | 932 / 932 | 2/2 |
| engine | impact/expansion | 3 / 3,900 / 3.043 s | 3 / 3,900 / 2.999 s | 916 / 916 | 2/2 |
| engine | repository shape | 6 / 10,090 / 6.322 s | 6 / 10,089 / 5.895 s | 913 / 913 | 0/2 |
| control | definition/callers/tests | 4 / 3,941 / 2.791 s | 4 / 3,941 / 2.802 s | 823 / 823 | 2/2 |
| control | impact/expansion | 3 / 3,572 / 3.052 s | 3 / 3,572 / 3.053 s | 771 / 771 | 2/2 |
| control | repository shape | 2 / 2,300 / 1.617 s | 2 / 2,300 / 1.586 s | 500 / 500 | 2/2 |

Post-fix engine shape output (both runs):

```json
{"name":"engine-live","source_files":4,"test_files":3,
 "modules":["pkg","pkg.core","pkg.metrics","pkg.report"],
 "test_command":"pytest -q"}
```

The fixed check requires `pkg/core.py`, `pkg/metrics.py`, `pkg/report.py`, so
this fails even though the dotted names are arguably import-style module paths.
The control returned the file paths and passed. If dotted names were accepted,
the engine would be 6/6; the token conclusion below would not change.

### Cumulative before/after

| Metric | Pre-fix engine | Post-fix engine | Post-fix control |
| --- | ---: | ---: | ---: |
| Accepted / valid JSON (6 runs) | 6 / 6 | 4 / 6 | 6 / 6 |
| Prompt tokens | 41,750 | 34,838 | 18,364 |
| Completion tokens | 1,549 | 1,747 | 1,262 |
| Total model tokens | 43,299 | **36,585** | **19,626** |
| Tool calls | 28 | 26 | 18 |
| Model calls | 34 | 32 | 22 |
| Wall time | 25.201 s | 24.194 s | 14.901 s |
| Engine `t`, all / model-visible | 6,720 / 5,262 | 5,522 / 4,064 | 4,188 / 3,540 |
| Tool-result estimated tokens | 4,956 | 3,828 | 3,344 |
| Model-visible wire bytes | 19,808 | 15,278 | 13,362 |
| Speculation prepared/fetched/hits/wasted | 8 / 0 / 0 / 0 | 12 / 4 / 4 / 0 | 0 / 0 / 0 / 0 |
| Shape calls per shape run | 6 | **1** | 2 |

The fix worked on its own terms: repeated `shape` calls fell from 6 to 1, and
engine tokens fell from 43,299 to 36,585 (−15.5%), with model-visible wire down
22.9% and shape task engine `t` down 39.0%. But the engine arm still used
**86.4% more total model tokens** than the control (36,585 vs 19,626), 44.4%
more tool calls, and 62.4% more wall time. The freed budget went into four
`fetch` calls and one `find` after the single `shape` call; the first fetch had
no handles, and only the later fetches hit the prepared handles. The model
finalized after six tool calls, and the shape answer used dotted module names.

Excluding the shape task, the two arms are close: engine 16,406 model tokens
versus control 15,026 (+9.2%), 14 versus 14 tool calls, 11.977 s versus 11.698 s
wall, 2,578 versus 2,544 tool-result estimated tokens (+1.3%), and 10,298 versus
10,162 model-visible wire bytes (+1.3%). The remaining gap is the larger engine
schema/system context, not tool-result size. The shape task's `fetch` misuse
dominates the cumulative loss.

## Speculation

Pre-fix: the 6 engine runs prepared 8 handles and fetched none; model-driven
hits/waste were 0/0. Post-fix: 12 handles prepared, 4 fetched, 4 hits, 0 misses,
0 wasted (`hit_rate=1.0` for the fetches that happened). All model-driven `find`
responses reported `q.r=0` (not ready at response time); the later fetches were
ready and hit. The model-driven fetches occurred in the shape task, where they
were not useful, not in the definition/impact tasks.

The deterministic probe exercised the hit path on the running engine: `hello`,
`find average` with `spec=true`, 200 ms think, `fetch` with `wait_ms=250`,
`hello` with `stats=true`, `bye`.

| Probe call | Engine `t` | Wire bytes |
| --- | ---: | ---: |
| hello | 31 | 117 |
| find | 105 | 395 |
| fetch | 234 | 875 |
| hello + stats | 207 | 744 |
| bye | 5 | 15 |
| **total** | **582** | **2,146** |

Probe counters: 2 handles, `ready_at_response=0`, both fetch states `ready`
(a returned fetch payload with no `fs` is recorded as ready), prepared 2,
fetched 2, hits 2, misses 0, wasted 0, evicted 0, cancelled 0, worker_ms 0.3,
hit_rate 1.0. The background speculation worker was in use, and both prepared
payloads were ready before the fetch.

## Run history and failures

1. **First exploratory run was correctly withheld as stale.** I wrote the
   driver log inside the fixture root, which changed the working-tree digest.
   `hello` reported `stale` and every structural response was withheld. The
   engine failed closed. Logs/results were moved outside the fixture, the graph
   was re-indexed, and the server was restarted. That run is not part of any
   sample below.
2. **First checker was stricter than the prompt.** The initial checks required
   file paths in `symbol` and `tests`; all six JSONs were valid and contained
   the right names but failed those formatting checks. Checks were aligned to
   the prompt; model, task prompts, fixture and sampling settings were unchanged.
3. **Pre-fix defect: omitted columns caused six `shape` calls.** The pre-fix
   engine returned `c` on the first `shape` response and omitted it on repeats.
   The model could not parse the later rows and repeated `shape` six times
   (11,628 tokens per run). This is the defect fixed by the default-columns
   change.
4. **Post-fix: one `shape` call, but extra `fetch`/`find` calls.** Both post-fix
   shape runs called `shape` once, then issued four `fetch` calls and one
   `find` before finalizing at the six-call budget. The first `fetch` came
   before any `find` and had no handles; the later fetches hit prepared handles
   but did not improve the answer. The engine returned clear errors for the
   invalid handles; the model did not recover. This is a model/tool-interface
   interaction, not a transport failure.
5. **Post-fix shape check failure is format-sensitive.** The model returned
   dotted import names (`pkg.core`) rather than the shape report's file paths
   (`pkg/core.py`). The fixed check requires file paths. The engine transport
   returned the correct file paths in the shape rows.

### Fail-closed checks

Two checks used no model tokens and confirmed that errors become a clear
`LiveError`, not a result:

```text
engine client with a wrong token: engine graph returned HTTP 401 (unauthorized)
model client with a wrong key:    model returned HTTP 401 (Unauthorized)
```

## Limitations

- One local model, one tiny synthetic fixture, 6 model task runs per arm per
  sample, one host/GPU, no concurrency, no human adjudication.
- The 27B model is W4A16-quantized and runs with speculative decoding and
  prefix caching. Results are not transferable to other models or hardware.
  Temperature 0 produced identical final answers on repeat, but vLLM is not
  claimed to be bitwise deterministic across versions.
- Acceptance is a fixed entity/field check on a small JSON answer, not a
  correctness, review-quality or agent-outcome judgment. The shape check's
  file-path requirement makes dotted import names fail; that ambiguity is
  reported rather than hidden.
- The arms differ in system prompt, tool schema and session lifecycle. The
  engine arm used one session, driver-managed `hello`/`bye`, and forced
  `spec=true` on `find`; the control used fresh `hello`/`bye` per call with no
  `session`/`known`/`spec`/`fetch`. The measured differences are therefore not
  a pure transport effect.
- Arm order was not counterbalanced (engine first, then control). The model
  server uses prefix caching; the post-fix engine arm recorded 864 cached prompt
  tokens, the control 0. Total token counts are unaffected, but latency may be.
- `engine t` is the engine's `len(JSON)/4` estimate; model `usage` is the
  tokenizer's count. They are not the same unit.
- The post-fix shape task's four `fetch`/one `find` calls make the
  cumulative loss larger than the per-task wire sizes alone would suggest.
  This is a model behavior observation in one sample, not a stable property of
  the interface.

## Live gate and remaining work

**Live gate for the engine HTTP transport: satisfied for this sample.** The
loopback server returned `/healthz` 200, rejected an unauthenticated
`/v1/graph` with 401, authenticated `hello` reported `fresh`, repeated `shape`
and `find` responses now carry `c` and are parseable, `cols:false` still opts
into reuse, 12/12 post-fix runs were valid JSON, the speculation probe hit 2/2
prepared handles, and transport/auth/model errors aborted rather than
fabricating results.

**The engine arm still loses the model-task comparison in this sample.** It
uses 86.4% more model tokens than the stateless control, mostly because the
model spends its tool budget on invalid `fetch` calls after `shape`; the column
defect itself is fixed. A broader comparison is still needed.

Remaining work:

- Investigate why the model calls `fetch` without handles from `find`; consider
  a stronger tool description or a driver-side precondition hint, then rerun
  without changing the fixture or checks.
- Run multiple models and larger repositories with randomized arm order and
  more repeats, and add human adjudication.
- Equalize system prompts and tool schemas where possible, or measure the
  schema/prompt cost separately from the transport cost.
- Exercise `known` deltas, `fetch` hits, stale-snapshot withholding, auth
  failure, saturation and malformed requests under load.
- Decide whether the shape check should accept dotted import paths as well as
  file paths, and pre-register that decision before the next comparison.
- Add a deterministic same-call-sequence wire benchmark so transport overhead
  can be compared without model behavior differences.

## Checks run

```bash
.venv/bin/ruff check src/ozagia/core/graph_engine.py \
  scripts/eval_graph_engine_live.py tests/test_graph_engine.py
.venv/bin/python -m pytest -q tests/test_graph_engine.py tests/test_engine_http.py
# 53 passed

.venv/bin/python -m pytest -q tests/test_engine_http.py
# 15 passed (focused rerun)
```
