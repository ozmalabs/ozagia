# Graph engine model-facing interface: live A/B follow-up (2026-11-25)

Status: **the optimized model-facing interface beat the previous engine arm
decisively and narrowed the stateless-control gap to +2.0% total model tokens,
with the same 6/6 acceptance and the same 20 tool calls. It did not beat the
control on tokens.** This is a follow-up to
[`graph_engine_live_2026-11-25.md`](graph_engine_live_2026-11-25.md); the fixture,
tasks and fixed checks are the same, with one prompt clarification recorded below.
The sample is still one local 27B model, one tiny fixture and six runs per arm.

> **Broader follow-up (2026-11-25):** the same three arms and checks were rerun
> on the pinned `pallets/flask` checkout at `22d92470` (3 adapted tasks × 2
> repeats, 1,200-token budget). The optimized arm accepted 6/6 but used
> **+6.5% total tokens** versus the control (122,405 vs 114,899), the same 34
> tool calls; the legacy engine used −5.4% versus the control on that sample.
> The tiny fixture reproduced the 17,762 / 35,752 / 18,118 totals exactly, so
> **the +2.0% tiny-fixture gap does not generalize to the real repository.**
> See
> [`graph_engine_live_broader_2026-11-25.md`](graph_engine_live_broader_2026-11-25.md).

## Change under test

The previous engine arm used 36,585 model tokens versus 19,626 for the control
and accepted 4/6 versus 6/6. Its losses were:

1. a larger system prompt and tool schema than the control;
2. `fetch` advertised even when the session had no handles, which produced
   invalid or useless `fetch` calls after `shape`;
3. ready speculative payloads required a separate `fetch` round-trip; and
4. a format-sensitive `repository_shape` answer (dotted module names instead of
   repository-relative file paths).

This follow-up changes four model-facing things, without changing transport,
freshness, read-only or supervision behaviour:

1. **Conditional fetch.** The optimized driver exposes `fetch` (and the `fetch`
   argument) only while the engine session holds speculative handles. The engine
   also returns a compact `{"e": "no_handles", "why": "fetch only q handles
   returned by find; do not retry"}` instead of a generic error.
2. **Inline ready speculation.** When `spec` is requested and a prepared payload
   is already ready at response time, the engine delivers it under `q.i`,
   consumes the handle and counts it as fetched/hit, so a model does not need a
   separate fetch. The payload is only inlined while the response stays within
   the requested budget. `inline: false` restores fetch-only delivery.
3. **Smaller model-facing schema.** The optimized driver exposes only
   `op/q/key/keys/budget` plus conditional `fetch`, and omits `spec`, `h` and
   `context` that these tasks do not use. The driver still forces `spec=true`
   on `find`.
4. **Unambiguous shape paths.** `shape` now includes a flat `mods` list of
   repository-relative module file paths (for example `pkg/core.py`) alongside
   the existing rows. The task prompt now asks for "Python module file paths
   (repository-relative, for example `pkg/core.py`)". The fixed check still
   requires `pkg/core.py`, `pkg/metrics.py` and `pkg/report.py`; it was not
   weakened.

The product `graph` tool description was shortened from a sentence listing
hello/session/details to: `Read-only code graph: find, outline, expand, impact,
tests, context, shape, fetch. Compact rows, token budgets, known deltas.`

## Run context

| Item | Value |
| --- | --- |
| Model alias requested | `vllm/qwen3.8-27b` (provider-qualified) |
| Server model id | `qwen3.8-27b` (the only id returned by `/v1/models`) |
| Model root | `/app/models/Swift-1.5-Qwen3.8-27B-W4A16-HyperQwen` |
| Host | `queen` (`matt@192.168.40.215`); same endpoint and model root as the prior sample (GPU not re-queried this run) |
| Transport | local SSH tunnel `127.0.0.1:18020` to queen's `127.0.0.1:18020` |
| Endpoint key | queen's `~/.config/opencode/hyperqwen-api-key`, copied to a `0600` temp file; never printed or logged |
| Engine | local `ozagia engine serve --port 0` on this checkout, ephemeral port `44639`, loopback only |
| Sampling | `temperature=0`, `top_p=1`, `seed=0`, `enable_thinking=false`, six tool calls, 320 output tokens per turn, 220 for finalization |
| Order | counterbalanced: repeat 1 `control, engine, engine_opt`; repeat 2 `engine_opt, engine, control` |
| Repeats | 3 tasks × 2 repeats per arm = 6 runs per arm, strictly sequential |

The user-provided alias `vllm/qwen3.8-27b` resolved to the server's
`qwen3.8-27b` model id; no hosted fallback was used.

The previous `/tmp/opencode/engine-live` fixture was absent when this run
started, so it was recreated from the previous result's description: 10 tracked
files (`pkg/__init__.py`, `pkg/core.py`, `pkg/metrics.py`, `pkg/report.py`,
three pytest files, `README.md`, `pyproject.toml`, `.gitignore`), git-committed,
parse-only index. It reports 23 nodes / 54 edges (previous sample: 24 / 55),
4 source files, 3 test files, 4 modules and 7 Python files. The symbols,
callers, tests, counts and checks are unchanged; it is not byte-identical to the
lost fixture.

## Overhead profile

The driver now records its own model-facing overhead, independent of usage
counters. Tokens are `ceil(chars / 4)`.

| Arm interface | System chars/tokens | Tool schema chars/tokens | Fixed per-turn chars/tokens |
| --- | ---: | ---: | ---: |
| control (short prompt, six one-shot ops) | 148 / 37 | 387 / 97 | 535 / 134 |
| engine legacy (old prompt, eight ops + spec/fetch/h) | 392 / 98 | 516 / 129 | 908 / 227 |
| engine optimized, no handles | 148 / 37 | 387 / 97 | 535 / 134 |
| engine optimized, fetch exposed | 148 / 37 | 446 / 112 | 594 / 149 |

The avoidable legacy overhead was 373 characters / 93 estimated tokens per
model call (system and schema). The optimized interface matches the control
exactly until a `find` leaves handles; then `fetch` adds 59 characters / 15
estimated tokens per turn. The optimized system prompt and base schema are now
the same as the control's, so the remaining measured difference is session
state, speculation and the dynamic fetch property.

## Tasks and checks

The three tasks and fixed checks are unchanged from the previous result:

| Task | Prompt | Checks |
| --- | --- | --- |
| `definition_callers_tests` | Find the symbol named average, its direct callers, and the tests that cover it. Reply with only `{"symbol": string, "callers": [string], "tests": [string]}`. | `symbol` contains `average`; `callers` contains `scaled_average` and `summarize`; `tests` contains `test_average` |
| `impact_expansion` | Compute the impact of the symbol add to depth 2. Reply with only `{"symbol": string, "symbols": [string], "tests": [string], "summary": string}`. | `symbol` contains `add`; `symbols` contains `double`, `average`, `scaled_average`, `summarize`; `tests` contains `test_add`; `summary` non-empty |
| `repository_shape` | Describe the repository shape: name, source file count, test file count, Python module file paths (repository-relative, for example `pkg/core.py`), and the test command. Reply with only `{"name": string, "source_files": integer, "test_files": integer, "modules": [string], "test_command": string}`. | `name` contains `engine-live`; `source_files == 4`; `test_files == 3`; `modules` contains `pkg/core.py`, `pkg/metrics.py`, `pkg/report.py`; `test_command` contains `pytest` |

The only task-text change is the shape format clarification. It does not relax
the check; it asks for the same file paths the check already requires. All three
arms ran with that clarified prompt, so the comparison between them is direct.

## Results

`eval-opt.json`, generated `2026-11-25T20:11:47+1100`. All 18 runs returned valid
JSON; all three arms were accepted 6/6.

| Arm | Accepted | Total tokens | Prompt | Completion | Tool calls | Model calls | Wall | Engine `t` (all / visible) | Tool-result tokens | Model-visible wire bytes | Shape calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 6/6 | 17,762 | 16,524 | 1,238 | 20 | 20 | 13.684 s | 4,472 / 3,752 | 3,548 | 14,168 | 2 (1/run) |
| engine (legacy interface) | 6/6 | 35,752 | 34,098 | 1,654 | 26 | 32 | 24.175 s | 5,410 / 3,952 | 3,734 | 14,892 | 2 (1/run) |
| engine optimized | 6/6 | **18,118** | 16,788 | 1,330 | 20 | 20 | 14.147 s | 5,270 / 3,812 | 3,600 | 14,372 | 2 (1/run) |

Per-task totals (two repeats each):

| Arm | Task | Tokens | Tool calls | Wall | Engine `t` | Model-visible wire | Ops |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| control | definition/callers/tests | 6,138 | 10 | 4.874 s | 1,754 | 5,286 | find, impact, tests, impact, tests (×2) |
| control | impact/expansion | 6,906 | 6 | 5.504 s | 1,614 | 5,276 | impact, find, impact (×2) |
| control | repository shape | 4,718 | 4 | 3.306 s | 1,104 | 3,606 | shape, find (×2) |
| engine | definition/callers/tests | 8,586 | 8 | 5.919 s | 1,866 | 5,232 | find, impact, tests, impact (×2) |
| engine | impact/expansion | 7,870 | 6 | 6.320 s | 1,904 | 5,344 | impact, find, impact (×2) |
| engine | repository shape | **19,296** | **12** | 11.936 s | 1,640 | 4,316 | shape, fetch, find, fetch, find, find (×2) |
| engine optimized | definition/callers/tests | 6,210 | 10 | 4.741 s | 1,900 | 5,354 | find, impact, tests, impact, tests (×2) |
| engine optimized | impact/expansion | 7,112 | 6 | 6.077 s | 1,904 | 5,344 | impact, find, impact (×2) |
| engine optimized | repository shape | **4,796** | **4** | 3.329 s | 1,466 | 3,674 | shape, find (×2) |

Differences:

| Comparison | Total tokens | Tool calls | Wall | Shape task |
| --- | ---: | ---: | ---: | ---: |
| legacy vs control | +101.3% | +30.0% | +76.7% | legacy 19,296 vs control 4,718 (+309.0%) |
| optimized vs legacy | −49.3% | −23.1% | −41.5% | optimized 4,796 vs legacy 19,296 (−75.1%) |
| optimized vs control | **+2.0%** | 0% | +3.4% | optimized 4,796 vs control 4,718 (+1.7%) |

Breaking the optimized/control difference down: prompt tokens +264 (+1.6%),
completion tokens +92 (+7.4%), total +356 (+2.0%). Model-visible engine wire was
+204 bytes (+1.4%) and tool-result estimated tokens +52 (+1.5%). The engine's
own `t` estimate was +17.8% (5,270 vs 4,472), mostly because speculative `q`
handles and inline-capable metadata appear in the optimized responses; the
rendered wire difference is small.

The shape task is the clear interface win: legacy spent 19,296 tokens and 12
calls across the two runs; optimized spent 4,796 and 4 calls. After `shape`
returned no handles, optimized never advertised `fetch`, so the model moved
directly to `find` and answered. Legacy still called `fetch` immediately after
`shape`, got the compact `no_handles` response, then issued `find`, another
`fetch`, and two more `find`s. The `no_handles` response alone did not stop
retries in the legacy interface; hiding the unavailable op did.

## Speculation

| Arm | Prepared | Ready at response | Fetched | Hits | Misses | Wasted (pre-`bye` snapshot) | Worker ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 |
| engine legacy | 12 | 0 | 0 | 0 | 0 | 0 | 2.0 |
| engine optimized | 12 | 0 | 0 | 0 | 0 | 0 | 2.7 |

All model-driven `find` responses reported `q.r=0` (no payload ready at response
time), so inline delivery was not exercised by the live model. The engine unit
tests exercise inline ready delivery, budget withholding and `inline:false`
deterministically. The optimized arm exposed `fetch` after `find` but the model
chose not to call it; it had already found the answer. Legacy made four `fetch`
calls total, none produced a ready or pending item (`fetch_states` was empty),
and stats report `fetched=0`. The pre-`bye` ledger does not include close-time
waste; handles still pending/ready when the session closed were discarded by the
existing close path.

The deterministic probe still hits the fetch path on the running engine:

| Probe call | Engine `t` | Wire bytes |
| --- | ---: | ---: |
| hello | 31 | 117 |
| find (`spec=true`) | 102 | 382 |
| fetch (200 ms think, `wait_ms=250`) | 232 | 868 |
| hello + stats | 207 | 744 |
| bye | 5 | 15 |
| **total** | **577** | **2,126** |

Probe counters: 2 handles, `ready_at_response=0`, both fetch states `ready`,
prepared 2, fetched 2, hits 2, misses 0, wasted 0, evicted 0, cancelled 0,
worker_ms 0.3, hit_rate 1.0.

## Conclusion

**The optimized interface narrowed the control gap from +101.3% to +2.0% and
matched the control on acceptance (6/6) and tool calls (20). It beat the previous
engine interface decisively on tokens (−49.3%), calls (−23.1%) and wall time
(−41.5%). It did not beat the stateless control on total tokens, prompt tokens,
wall time or engine wire.**

The largest avoidable difference was the legacy system/schema overhead (93
estimated tokens per call) plus the unnecessary `fetch` calls after `shape`.
The optimized arm pays only 15 extra estimated tokens per turn while handles
exist, and it never exposed `fetch` when no handles existed. Six runs per arm is
still a small sample; the +2.0% gap is within the sort of per-run variation this
fixture can show, and the conclusion is "narrowed, not conclusively beat".

## Limitations

- One local W4A16-quantized 27B model, one tiny recreated fixture, one host, six
  task runs per arm, no human adjudication.
- The fixture is structurally equivalent but not byte-identical to the lost
  original (23 nodes / 54 edges vs 24 / 55).
- The shape prompt was clarified to ask for repository-relative file paths. The
  check was unchanged, but this means the legacy arm's 6/6 is not a pure
  interface effect versus the previous 4/6 sample.
- The control and optimized arms now share the same short system prompt and the
  same base schema; the legacy arm deliberately keeps the old longer prompt and
  eight-op schema. The measured legacy/optimized difference therefore includes
  the prompt/schema change by design.
- The optimized schema changes between turns as handles appear; model servers
  that cache tool schemas aggressively may behave differently.
- Inline ready delivery was not exercised by the live model because no payload
  was ready at response time. It is covered by deterministic tests only.
- Pre-`bye` stats do not capture close-time waste for unfetched prepared
  handles.
- `engine t` is `len(JSON)/4`; model `usage` is the tokenizer's count. They are
  different units.

## Remaining work

- Repeat with multiple models and larger, real repositories, and retain the
  counterbalanced order.
- Exercise inline delivery live with a think time or workload that makes a
  payload ready before the response.
- Test the optimized interface in the TUI/Desktop and other MCP hosts, not only
  the live driver.
- Decide whether `fetch` should ever be exposed for merely pending handles, or
  only for ready/inline-capable work.
- Add a deterministic same-call-sequence wire benchmark so schema/transport
  overhead can be compared without model behavior differences.

## Checks run

```bash
.venv/bin/python -m pytest -q tests/test_graph_engine.py \
  tests/test_engine_http.py tests/test_graph_engine_live_driver.py
# 61 passed

.venv/bin/ruff check src/ozagia/core/graph_engine.py \
  tests/test_graph_engine.py tests/test_graph_engine_live_driver.py \
  scripts/eval_graph_engine_live.py
# All checks passed
```

The live run command (key and engine token were read from `0600` files and never
printed):

```bash
OZAGIA_EVAL_LIVE=1 .venv/bin/python scripts/eval_graph_engine_live.py \
  --live \
  --engine-url http://127.0.0.1:44639 \
  --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 \
  --model qwen3.8-27b \
  --arm all --repeat 2 --counterbalance \
  --out /tmp/opencode/engine-live-results/eval-opt.json
```

Raw measurement JSON and driver stderr are under
`/tmp/opencode/engine-live-results/` (outside this repository).
