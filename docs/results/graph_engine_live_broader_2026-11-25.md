# Graph engine live comparison at repository scale (2026-11-25)

Status: **the optimized model-facing engine does not beat the stateless control
on a real Python repository in this sample.** On `flask` it used **+6.5% total
model tokens** (122,405 vs 114,899), the same 6/6 acceptance and the same 34 tool
calls, and was +6.6% slower. The previous engine interface used **−5.4% tokens**
versus the control on the same sample. On the tiny fixture the optimized engine
reproduced the earlier result exactly (+2.0% tokens vs control, −49.3% vs the
legacy interface). **Repository scale changes the conclusion: the +2.0% tiny-
fixture gap does not generalize, and the ranking of the two engine interfaces
reverses.**

This is a follow-up to
[`graph_engine_live_2026-11-25.md`](graph_engine_live_2026-11-25.md) and
[`graph_engine_live_2026-11-25_optimized.md`](graph_engine_live_2026-11-25_optimized.md).
The tiny-fixture numbers are kept separate below. One local model, one host, one
real repository, two runs per task per arm.

## What was measured

Three arms, same driver, strictly sequential:

| Arm | Interface |
| --- | --- |
| `control` | stateless: fresh `hello`/`bye` per call, no session state, no speculation |
| `engine` | the previous model-facing engine interface (legacy prompt/schema, unconditional `fetch`) |
| `engine_opt` | conditional `fetch`, smaller schema, inline-ready speculation |

Two task sets:

1. **Tiny fixture** (`/tmp/opencode/engine-live`, 23 nodes / 54 edges): the same
   three tasks and the same checks as the optimized result. The run reproduced
   the previous totals exactly (17,762 / 35,752 / 18,118 tokens), so the
   harness and fixture are comparable.
2. **Real repository**: `pallets/flask`, pinned at
   `22d924701a6ae2e4cd01e9a15bbaf3946094af65`, acquired from
   `https://codeload.github.com/pallets/flask/tar.gz/22d924701a6ae2e4cd01e9a15bbaf3946094af65`
   (tree sha256 `80612ce705e9315a7b8fffafc2c64707afd4451a92f3236e97dc8acd781920e0`,
   1,625 nodes / 4,252 edges). The snapshot was built read-only under the
   checkout's `.ozagia`; the repository tree was not edited.

The three real-repository tasks were adapted from the tiny tasks. The checks are
fixed in the driver and were not weakened:

| Task | Prompt (short) | Fixed checks |
| --- | --- | --- |
| `definition_callers_tests` | find `get_root_path`, direct callers, test files | symbol contains `get_root_path`; callers contain `Scaffold.__init__`; tests contain `tests/test_helpers.py` |
| `impact_expansion` | impact of `send_from_directory` to depth 2 | symbol contains `send_from_directory`; symbols contain `Flask.send_static_file` and `Blueprint.send_static_file`; tests contain `tests/test_helpers.py`; summary non-empty |
| `repository_shape` | name, source/test file counts, module paths, test command | name contains `flask`; source_files == 77; test_files == 30; modules contain `src/flask/app.py`, `src/flask/helpers.py`, `src/flask/blueprints.py`; test command contains `pytest` |

The real-repository prompts ask for repository-relative paths and test files,
matching what the engine returns. The tiny prompts and checks are unchanged.
Only `flask` was used; the `pytest` and `gin` holdouts were not indexed or tuned
on.

## Run context

| Item | Value |
| --- | --- |
| Model alias requested | `vllm/qwen3.8-27b` |
| Server model id | `qwen3.8-27b` (the only id returned by `/v1/models`) |
| Model root | `/app/models/Swift-1.5-Qwen3.8-27B-W4A16-HyperQwen` |
| Host | `queen` (`matt@192.168.40.215`) |
| Transport | local SSH tunnel `127.0.0.1:18020` → queen's `127.0.0.1:18020` |
| Endpoint key | queen's `~/.config/opencode/hyperqwen-api-key`, copied to a `0600` temp file; never printed, logged or committed |
| Engines | local `ozagia engine serve --port 0`, loopback only; tiny snapshot and flask snapshot in separate processes |
| Sampling | `temperature=0`, `top_p=1`, `seed=0`, `enable_thinking=false`, six tool calls, 320 output tokens per tool turn |
| Finalization | 220 tokens for tiny; 600 tokens for flask (see the finalization note below) |
| Order | counterbalanced: repeat 1 `control, engine, engine_opt`; repeat 2 `engine_opt, engine, control` |
| Repeats | 3 tasks × 2 repeats per arm = 6 runs per arm per repository, strictly sequential |
| Budget | 800 estimated tokens per call on tiny; 1,200 on flask (documented workaround for the trimming bug below) |

Only one model was available on queen that served OpenAI tool calls. No second
local model was tested, and no hosted model was used.

## Results

Raw measurements: `/tmp/opencode/engine-live-broader/tiny.json` and
`/tmp/opencode/engine-live-broader/flask-final.json`.

### Tiny fixture (unchanged comparison)

All 18 runs returned valid JSON; all arms accepted 6/6.

| Arm | Accepted | Total tokens | Prompt | Completion | Tool calls | Model calls | Wall | Engine `t` (all / visible) | Tool-result tokens | Model-visible wire bytes | Shape calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 6/6 | 17,762 | 16,524 | 1,238 | 20 | 20 | 13.795 s | 4,472 / 3,752 | 3,548 | 14,168 | 2 |
| engine (legacy) | 6/6 | 35,752 | 34,098 | 1,654 | 26 | 32 | 24.071 s | 5,410 / 3,952 | 3,734 | 14,892 | 2 |
| engine optimized | 6/6 | 18,118 | 16,788 | 1,330 | 20 | 20 | 14.287 s | 5,270 / 3,812 | 3,600 | 14,372 | 2 |

| Comparison | Total tokens | Tool calls | Wall |
| --- | ---: | ---: | ---: |
| legacy vs control | +101.3% | +30.0% | +74.5% |
| optimized vs legacy | −49.3% | −23.1% | −40.6% |
| optimized vs control | **+2.0%** | 0% | +3.6% |

These totals match the optimized result exactly, so the tiny fixture is a
regression check rather than a new sample.

### Flask (real repository)

All 18 runs returned valid JSON; all arms accepted 6/6.

| Arm | Accepted | Total tokens | Prompt | Completion | Tool calls | Model calls | Wall | Engine `t` (all / visible) | Tool-result tokens | Model-visible wire bytes | Shape calls / fetch calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 6/6 | 114,899 | 112,255 | 2,644 | 34 | 38 | 42.825 s | 23,189 / 21,931 | 20,995 | 83,919 | 4 / 0 |
| engine (legacy) | 6/6 | **108,750** | 105,674 | 3,076 | 32 | 40 | 49.327 s | 19,168 / 17,698 | 16,940 | 67,724 | 2 / 8 |
| engine optimized | 6/6 | 122,405 | 119,570 | 2,835 | 34 | 38 | 45.650 s | 26,018 / 24,548 | 23,502 | 93,952 | 4 / 0 |

| Comparison | Total tokens | Tool calls | Wall |
| --- | ---: | ---: | ---: |
| legacy vs control | **−5.4%** | −5.9% | +15.2% |
| optimized vs legacy | +12.6% | +6.3% | −7.5% |
| optimized vs control | **+6.5%** | 0% | +6.6% |

Per-task totals (two repeats each; tokens are the model's usage, not engine `t`):

| Arm | Task | Tokens | Tool calls | Wall | Tool-result tokens | Model-visible wire | Ops |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| control | definition/callers/tests | 32,674 | 12 | 11.111 s | 6,874 | 26,424 | find, impact, tests, impact, tests, expand (×2) |
| control | impact/expansion | 28,016 | 10 | 11.601 s | 6,516 | 25,034 | impact, find, impact, tests, tests (×2) |
| control | repository shape | 54,209 | 12 | 20.113 s | 8,541 | 32,461 | shape, shape, find, find, find, find (×2) |
| engine | definition/callers/tests | 49,468 | 12 | 17.630 s | 6,926 | 26,614 | find, impact, tests, impact, fetch, expand (×2) |
| engine | impact/expansion | **22,308** | 8 | 11.893 s | 6,504 | 24,982 | impact, find, impact, tests (×2) |
| engine | repository shape | **36,974** | 12 | 19.804 s | 4,268 | 16,128 | shape, fetch, fetch, find, fetch, find (×2) |
| engine optimized | definition/callers/tests | 38,770 | 12 | 14.723 s | 9,236 | 35,526 | find, impact, tests, impact, tests, find (×2) |
| engine optimized | impact/expansion | 28,221 | 10 | 11.989 s | 6,538 | 25,104 | impact, find, impact, tests, tests (×2) |
| engine optimized | repository shape | 55,414 | 12 | 18.938 s | 8,774 | 33,322 | shape, shape, find, find, find, find (×2) |

The optimized arm's loss is concentrated in the definition task (+18.7% vs
control). It chose a second `find` where control chose `expand`, producing a
larger tool result; this is model behavior inside the allowed op set, not a
schema-overhead effect. The impact task is a near tie (+0.7%); the shape task is
+2.2%. Legacy's real-repository advantage is concentrated in the shape and impact
tasks, where its `fetch` calls after `shape` are consistent with compact
`no_handles`/pending responses instead of another full `shape` or `find`, even
though its fixed prompt/schema is larger.

### Finalization note

The first flask run used the tiny fixture's 220-token finalization budget. In
that run `engine_opt` failed `repository_shape` twice with an unterminated JSON
answer (662 characters, six tool calls), while control passed. The optimized
answer listed 20 module paths; control's answer was shorter. Raising the
finalization budget to **600 tokens for all three arms** removed that
output-length artifact and produced the 6/6 table above. The acceptance checks
were not changed. The 220-token run is retained as
`/tmp/opencode/engine-live-broader/flask.json` for transparency; it is not the
primary comparison. Its totals were 115,364 / 108,750 / 122,379 model tokens and
6/6, 6/6, 4/6 acceptance for control / legacy / optimized.

## Speculation and engine probes

| Arm / repository | Prepared | Ready at response | Fetched | Hits | Misses | Evicted | Wasted (pre-`bye`) | Worker ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| tiny / control | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 |
| tiny / engine | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 2.4 |
| tiny / engine_opt | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 1.8 |
| flask / control | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 |
| flask / engine | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 6.6 |
| flask / engine_opt | 28 | 0 | 0 | 0 | 0 | 0 | 0 | 11.1 |

The live model did not call `fetch` in the optimized arm on either repository,
and no payload was ready at response time, so inline ready delivery was again
not exercised live. Legacy made eight `fetch` calls on flask; none produced a
ready or pending item in the ledger (`fetch_states` was empty).

The deterministic probe did hit the fetch path on the flask engine:

| Probe call | Engine `t` | Wire bytes |
| --- | ---: | ---: |
| hello | 32 | 118 |
| find `get_root_path` (`spec=true`) | 1,179 | 4,546 |
| fetch (200 ms think, `wait_ms=250`) | 193 | 728 |
| hello + stats | 208 | 745 |
| bye | 5 | 15 |
| **total** | **1,617** | **6,152** |

Probe counters: 2 handles, `ready_at_response=0`, both fetch states `ready`,
prepared 2, fetched 2, hits 2, misses 0, wasted 0, evicted 0, cancelled 0,
worker_ms 0.3, hit_rate 1.0. The tiny probe is unchanged (2 handles, 2 ready
fetches, `t=577`, 2,126 wire bytes).

## Transport, freshness and authentication

No transport, stale-snapshot, authentication or tool-schema failures occurred
during the 36 measured runs (18 tiny + 18 flask). The flask snapshot was `fresh`
for every session. The tunnel and the flask engine process were restarted twice
during setup/canary work; those interruptions are not part of the measured runs.
No hosted fallback was used.

## Engine bug found (not fixed in this change; fixed in the follow-up below)

**`find` at an 800-token budget can return no rows on a large result.** Direct
probe against the running flask engine:

| Budget | Engine `t` | Rows | `x` |
| ---: | ---: | ---: | --- |
| 800 | 3 | 0 | true |
| 900 | 3 | 0 | true |
| 1,000 | 3 | 0 | true |
| 1,100 | 1,078 | 20 | true |
| 1,200 | 1,157 | 20 | false |

The tiny fixture's `find average` still returned its rows at 800. The trimmer in
`GraphEngine._fit` shortens long text cells to `max(48, len // 2) + "..."`.
When many rows all reach the 51-character floor, the loop can no longer make
progress; it runs to its 1,000-iteration cap and returns
`{"truncated": true}` with the rows discarded. This is a clear transport/engine
bug independent of the model-facing interface. It affects every arm equally.
The real-repository run used a 1,200-token budget as a workaround instead of
changing product code mid-comparison. No engine product code was changed by this
evaluation. Recommended follow-up: make the trimmer always shrink (for example
floor below the selection threshold) and add a regression test that a 20-row
`find` at budget 800 keeps a usable prefix.

## Truncation fix (2026-11-25 follow-up)

The `_fit` no-progress bug described above is fixed in
`src/ozagia/core/graph_engine.py`. No model was run for this follow-up; the
numbers below are deterministic engine probes on the same flask snapshot.

**Root cause.** `_fit` shortened the longest text cell to
`max(48, len // 2) + "..."`. A 51-character value became `48 chars + "..."`,
again 51 characters. `_longest_text_cell` kept selecting the same cell, the
loop consumed its 1,000-iteration cap without shrinking anything, and the
fallback replaced the whole response with `{"truncated": true}`.

**New behavior.** `_shorten_text` caps the kept prefix so every step is
strictly shorter than the input; `_longest_text_cell` only selects cells above
the 48-character floor, so a floored cell is not selected again. The fitter
shortens the longest text cell first and only then drops rows. If not even one
row fits, it returns an explicit `budget_too_small` error (with a
larger-budget retry in `next` when that fits) instead of an empty truncated
object.

Direct probe against the existing snapshot
(`/home/matt/ozma/deep-review-corpus/repositories/flask`, 1,625 nodes /
4,252 edges, freshness `fresh`, loaded in-process from `.ozagia/graph.json`),
query `get_root_path`, same budgets:

| Budget | Before: `t` / rows / `x` | After: `t` / rows / `x` / omitted |
| ---: | ---: | ---: |
| 800 | 3 / 0 / true | **786 / 15 / true / 5** |
| 900 | 3 / 0 / true | **868 / 17 / true / 3** |
| 1,000 | 3 / 0 / true | **960 / 19 / true / 1** |
| 1,100 | 1,078 / 20 / true | 1,078 / 20 / true / 0 |
| 1,200 | 1,157 / 20 / false | 1,157 / 20 / false / 0 |

The 1,100 and 1,200 rows reproduce the earlier probe exactly, so this is the
same payload and the change is confined to the fitter. The 800/900/1,000
budgets now return a usable prefix plus a `next` continuation for the omitted
rows.

Regression tests added to `tests/test_graph_engine.py` (synthetic 20-row
long-signature result, no model):

- `test_text_truncation_always_shrinks_and_never_stalls_at_the_floor`
- `test_find_keeps_rows_or_reports_budget_too_small` (800/900/1,000 keep rows
  or a compact `budget_too_small`; 1,100/1,200 keep rows)
- `test_fit_never_returns_a_bare_truncated_object`
- `test_find_low_budget_is_bounded_and_parseable`

```bash
.venv/bin/ruff check src/ozagia/core/graph_engine.py tests/test_graph_engine.py
# All checks passed

.venv/bin/python -m pytest -q tests/test_graph_engine.py tests/test_engine_http.py
# 61 passed
```

## Conclusion

**On the real repository the optimized engine loses the control on total model
tokens (+6.5%), ties it on acceptance (6/6) and tool calls (34), and is slightly
slower (+6.6%). It also loses the legacy engine on tokens (+12.6%), while using
two more tool calls and less wall time. The tiny-fixture conclusion does not
hold at repository scale.**

- Tiny: optimized vs control **+2.0%**, 6/6 vs 6/6, 20 vs 20 calls; optimized
  vs legacy **−49.3%**.
- Flask: optimized vs control **+6.5%**, 6/6 vs 6/6, 34 vs 34 calls; optimized
  vs legacy **+12.6%**; legacy vs control **−5.4%**.

The reversal is not explained by fixed prompt/schema overhead: the optimized and
control arms have the same short prompt and base schema (536 chars / 134
estimated tokens at the 1,200-token setting), and the optimized arm pays only
the conditional `fetch` property while handles exist. The measured difference is
task-level tool choice and tool-result size. On flask, legacy's unconditional
`fetch` happened to produce cheaper follow-up calls than control/optimized
`shape`/`find` repetition. On tiny, the same unconditional `fetch` produced the
19,296-token shape blow-up. One repository does not settle which behavior is
typical; it does refute the claim that the tiny-fixture +2.0% gap generalizes as
stated.

## Limitations

- One local W4A16-quantized 27B model, one host, one real repository, two runs
  per task per arm, no human adjudication.
- The real-repository tasks were adapted from the tiny tasks and have different
  fixed checks; their absolute token totals are not comparable to the tiny
  totals. The tiny checks and prompts are unchanged.
- The model's tool choice varies by task even at temperature 0/seed 0 (for
  example the control shape task differed by 465 tokens between repeats). The
  per-task deltas are two-run observations, not stable effect sizes.
- The 220-token finalization budget was too small for the optimized shape
  answer on flask; the primary flask run uses 600 tokens for all arms. This is a
  harness setting, not a check change, but it means the finalization budget is
  part of the reported configuration.
- The optimized arm never called `fetch` live, so conditional-fetch behavior and
  inline ready delivery were not exercised by the model. Deterministic probes
  still cover the fetch path.
- The 800-token trimming bug was a workaround in this run, not a product fix at
  the time; it is fixed separately in the follow-up section. The measured runs
  still used 1,200.
- `engine t` is `len(JSON)/4`; model `usage` is the tokenizer's count. They are
  different units.
- The key existed only in a `0600` temp file and was never printed or committed;
  the tunnel, key and canaries were removed after the run.

## Remaining work

- Repeat on a second real Python repository (for example `httpx`) and ideally a
  second model before drawing a general scale conclusion.
- The `_fit` no-progress bug is fixed (regression tests and a deterministic
  flask probe); rerunning the flask model comparison at 800 tokens is still
  open.
- Exercise conditional `fetch` live with a workload that makes a payload ready
  before the response.
- Add a deterministic same-call-sequence wire benchmark so tool-result size can
  be compared without model tool-choice variation.
- Decide whether `find` should return a smaller default row set on large
  repositories, independent of budget.

## Checks run

```bash
.venv/bin/ruff check scripts/eval_graph_engine_live.py
# All checks passed

.venv/bin/python -m pytest -q tests/test_graph_engine_live_driver.py \
  tests/test_graph_engine.py tests/test_engine_http.py
# 61 passed
```

Live commands (key read from a `0600` file into `OZAGIA_LIVE_API_KEY`; engine
tokens read from their `0600` files; neither value is printed):

```bash
OZAGIA_EVAL_LIVE=1 OZAGIA_LIVE_API_KEY="$(cat /tmp/opencode/engine-live-broader/key)" \
  .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --engine-url http://127.0.0.1:44147 \
  --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --fixture /tmp/opencode/engine-live --task-set tiny \
  --arm all --repeat 2 --counterbalance \
  --out /tmp/opencode/engine-live-broader/tiny.json

OZAGIA_EVAL_LIVE=1 OZAGIA_LIVE_API_KEY="$(cat /tmp/opencode/engine-live-broader/key)" \
  .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --engine-url http://127.0.0.1:38729 \
  --engine-token-file /home/matt/ozma/deep-review-corpus/repositories/flask/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --fixture /home/matt/ozma/deep-review-corpus/repositories/flask \
  --task-set flask --arm all --repeat 2 --counterbalance \
  --final-tokens 600 \
  --out /tmp/opencode/engine-live-broader/flask-final.json
```

The snapshot build command (read-only over the checkout; only `.ozagia` was
written):

```bash
.venv/bin/python -m ozagia index \
  --root /home/matt/ozma/deep-review-corpus/repositories/flask --json
# action=fresh, 1625 nodes, 4252 edges
```
