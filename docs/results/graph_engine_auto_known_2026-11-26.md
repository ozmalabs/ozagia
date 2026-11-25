# Graph engine auto-known: model-facing deltas without client hashes (2026-11-26)

Status: **auto-known is on by default for the MCP `engine` surface and
`ozagia engine serve`, and reproduces the explicit `known` saving without any
client-side hash bookkeeping.** On the repeated-find task the serving engine
returned `u` key handles from its own session memory and the model used them:
on `pallets/flask`, 2 repeats (6 turns per arm) used **28,276** model tokens for
`engine_auto`, **28,284** for explicit client-sent `known`, and **36,068** for
the stateless control, all 6/6 turn acceptance with 6 tool calls each
(**−21.6%** for auto-known vs control). On the tiny fixture the same run used
**12,846 / 12,846 / 14,720** (**−12.7%**), 6/6 each. The two delta arms had
identical engine tokens and wire bytes; the 8-token flask difference is
completion sampling noise, not a protocol difference.

Raw measurements:
[`graph_engine_auto_known_2026-11-26_flask.json`](graph_engine_auto_known_2026-11-26_flask.json)
and
[`graph_engine_auto_known_2026-11-26_tiny.json`](graph_engine_auto_known_2026-11-26_tiny.json).

## Semantics

- `GraphEngine(auto_known=False)` is the direct/core default. An omitted
  `known` still means "full rows" for a direct caller.
- The MCP `engine` surface (`DevMemoryServer(..., surface="engine")`) and
  `ozagia engine serve` construct the engine with `auto_known=True`.
- With auto-known on, a `find`, `outline` or `context` request that omits
  `known` (or sends `null`) is compared with the hashes the session remembered
  for the same `(op, key)` when it served the earlier rows. Matching items come
  back as `u` key handles; changed or new items reserialize with a fresh `h`.
- The first response for an op has nothing remembered, so it is always full.
- An explicitly supplied `known`, **including `[]`**, disables auto-known for
  that request and is used exactly as before. `known: []` is the documented
  full-refresh opt-out; the model-facing description says so.
- Hash identity is still `(op, key)`: a find hash never satisfies an outline or
  context row, and an outline hash never displaces the find hash.
- Paging, budget trimming, `n.offset` accounting, unchanged ordering and the
  1,000-entry per-session hash memory are unchanged. `u` handles count as
  served items when a page is trimmed.
- `hello` with `detail` reports `ak` (auto-known on/off) so a client can
  discover the mode; `stats()` reports `auto_known` too.

## Run context

| Item | Value |
| --- | --- |
| Model | `qwen3.8-27b` (the only id returned by `/v1/models`) |
| Model root | `/app/models/Swift-1.5-Qwen3.8-27B-W4A16-HyperQwen` |
| Host | `queen` (`matt@192.168.40.215`) |
| Transport | local SSH tunnel `127.0.0.1:18020` → queen's `127.0.0.1:18020`, recreated for this run |
| Endpoint key | queen's `~/.config/opencode/hyperqwen-api-key`, copied to a `0600` temp file; never printed, logged or committed |
| Repository | `/home/matt/ozma/deep-review-corpus/repositories/flask` at `22d924701a6ae2e4cd01e9a15bbaf3946094af65` |
| Snapshot | `.ozagia/graph.json`, 1,625 nodes / 4,252 edges, session freshness `fresh` |
| Tiny fixture | `/tmp/opencode/engine-live` at `05d8f7f344a74d8a6e16e908afa80949faceb88e`, 23 nodes / 54 edges |
| Engine | local `ozagia engine serve` (auto-known default), loopback only, port 8841 (flask) and 8842 (tiny) |
| Sampling | `temperature=0`, `top_p=1`, `seed=0`, `enable_thinking=false`, at most 6 tool calls per turn, 320 output tokens per model call, 600 for finalization |
| Budget | 1,200 estimated tokens per graph call on flask, 800 on tiny |
| Repeats/order | 2 repeats, counterbalanced: repeat 1 `control, engine_known, engine_auto`; repeat 2 reversed. Strictly sequential. |
| Sample | 1 task × 3 turns × 3 arms × 2 repeats = 18 turns per repository |

## Arms

All arms use the same system prompt, the same optimized tool schema and the
same turn prompts. Only `known` transport differs:

| Arm | Session | `known` on the wire | Speculation |
| --- | --- | --- | --- |
| `control` | fresh `hello`/`bye` per call | not sent | off |
| `engine_known` | one session for all three turns | client remembers `{op,key,hash}` and sends it on the refresh | off |
| `engine_auto` | one session for all three turns | never sent; the engine applies its remembered hashes | off |

The shared system prompt gained one sentence for this run: "If a repeated
result lists only unchanged keys under u, reuse the rows already in the
conversation." The production tool description and MCP `initialize`
instructions now carry the same guidance. The control arm never emits `u`, so
the sentence is inert there; it is present in every arm to keep the model
prompt identical across arms. Absolute totals are therefore not directly
comparable with the 2026-11-25 multi-turn runs (different prompt and a
single-task sample); the within-run comparison is the measurement.

## Results: flask

Task `multiturn_find_refresh`: turn 1 finds `get_root_path` (20 rows), turn 2
repeats the same find and answers from the row already in the conversation,
turn 3 asks impact at depth 2. Checks and prompts are unchanged from
[`graph_engine_live_multiturn_2026-11-25.md`](graph_engine_live_multiturn_2026-11-25.md).

| Arm | Accepted | Tool calls | `known` sent | `u` handles | Engine t (model) | Engine wire (model) | Model tokens | vs control |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `control` | 6/6 | 6 | 0 | 0 | 7,062 | 27,092 B | 36,068 | — |
| `engine_known` | 6/6 | 6 | 40 | 40 | 5,212 | 20,008 B | 28,284 | −21.6% |
| `engine_auto` | 6/6 | 6 | 0 | 40 | 5,212 | 20,008 B | 28,276 | −21.6% |

The delta arms are identical on prompt tokens (27,524) and differ by 8
completion tokens. Cached-prompt counts differ (10,368 vs 12,096) because the
provider's cache accounting varied between arms, not because the payloads
differ.

## Results: tiny fixture

Same task against `average` (2 rows after trimming).

| Arm | Accepted | Tool calls | `known` sent | `u` handles | Engine t (model) | Engine wire (model) | Model tokens | vs control |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `control` | 6/6 | 6 | 0 | 0 | 1,826 | 6,926 B | 14,720 | — |
| `engine_known` | 6/6 | 6 | 14 | 14 | 1,392 | 5,294 B | 12,846 | −12.7% |
| `engine_auto` | 6/6 | 6 | 0 | 14 | 1,392 | 5,294 B | 12,846 | −12.7% |

## Deterministic probe (live engine, no model)

`run_known_probe` now also measures a no-`known` repeat against the same
session. Numbers are from the serving engines used above (auto-known on):

| Repo | Op | Full | Explicit `known` | Auto-known | Auto request growth |
| --- | --- | --- | --- | --- | --- |
| flask | outline `get_root_path` | 84 t / 319 B | 27 t / 103 B | 27 t / 103 B | 0 B |
| flask | find `get_root_path` (20) | 1,189 t / 4,572 B | 300 t / 1,177 B | 300 t / 1,177 B | 0 B |
| tiny | outline `average` | 64 t / 239 B | 24 t / 91 B | 24 t / 91 B | 0 B |
| tiny | find `average` (2) | 103 t / 390 B | 33 t / 125 B | 33 t / 125 B | 0 B |

The explicit arm's request grew to 2,127 B on the flask find (20 op-tagged
pairs) versus the ~79 B no-`known` request the engine_auto arm sends, so total
traffic saving is **3,395 B auto** versus **1,347 B explicit**. The response
payloads and engine token counts are identical. A local-engine (non-HTTP) probe
gave the same response counts with slightly different wire sizes because the
session's column state differed; only the live-run numbers are tabulated here.

## Model-comprehension risk

- **An all-`u` answer is a new response shape.** A model that does not know the
  convention could read `{"u": [...]}` as "no results". Mitigation: the tool
  description, the MCP `initialize` instructions and this run's system prompt
  all state that `u` lists unchanged keys already in the conversation. On this
  sample the model never issued an extra recovery call: both delta arms used
  exactly one tool call per turn.
- **Cross-query reuse.** Auto-known matches `(op, key)` hashes, not the query
  string. A different query returning a key whose row content is unchanged will
  also produce `u`; the content the model saw is identical, but the model may
  not connect the handle to the earlier response. Send `known: []` when a full
  re-serialize is wanted.
- **Silent staleness.** Auto-known trusts the session memory. If a client shows
  rows from one session but asks in another, no hashes match and rows are full;
  if the snapshot is stale, the engine withholds structure before any delta is
  applied. A changed snapshot starts a new session scope.
- **Verbose/debug callers** still see the same `u`/row split; `verbose: true`
  expands rows but does not disable auto-known. Use `known: []` for a debug
  dump.
- This is one local 27B model, one repository and one repeated-find task with
  two repeats. It is evidence that the interaction works and saves tokens on
  this task, not a general comprehension claim.

## Limitations

- Only the repeated-find task was run. The earlier multi-turn outline/file
  tasks were not rerun with auto-known.
- Speculation was off in all three arms. The earlier result that speculation
  is a net cost stands; nothing here changes it.
- Completion tokens vary by a few tokens per repeat; the flask auto/known 8
  token gap is not material.
- The model prompt differs from the 2026-11-25 runs by the one `u` sentence, so
  cross-run absolute totals should not be compared.

## Checks run

```
.venv/bin/python -m pytest -q tests/test_graph_engine.py tests/test_engine_client.py \
    tests/test_engine_http.py tests/test_graph_engine_live_driver.py
# 100 passed
.venv/bin/ruff check src tests
```

The deterministic tests also cover: second identical find/outline/context
returns handles with no caller `known`; explicit `known: []` returns full rows;
changed content reserializes with a new hash; cross-op same key stays distinct;
paging advances over `u` handles; `auto_known=False` and the direct-engine
default preserve full rows; the MCP surface and `load_engine` default on; the
client's `known=False` sends `known: []`; and the live driver's `engine_auto`
arm never sends `known`.

## Live commands

```bash
# tunnel and 0600 key (never printed)
ssh -N -L 18020:127.0.0.1:18020 matt@192.168.40.215
mkdir -p /tmp/opencode/queen && chmod 700 /tmp/opencode/queen
ssh matt@192.168.40.215 'cat ~/.config/opencode/hyperqwen-api-key' \
  > /tmp/opencode/queen/key && chmod 600 /tmp/opencode/queen/key

# serving engines (auto-known on by default)
.venv/bin/python -m ozagia.engine_http serve \
  --root /home/matt/ozma/deep-review-corpus/repositories/flask --port 8841
.venv/bin/python -m ozagia.engine_http serve \
  --root /tmp/opencode/engine-live --port 8842

# flask comparison; repeat with --task-set tiny / --engine-url ...:8842
OZAGIA_EVAL_LIVE=1 .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --suite multiturn --task-set flask \
  --task multiturn_find_refresh --arm known --repeat 2 --counterbalance \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --engine-url http://127.0.0.1:8841 \
  --engine-token-file /home/matt/ozma/deep-review-corpus/repositories/flask/.ozagia/engine/token \
  --fixture /home/matt/ozma/deep-review-corpus/repositories/flask \
  --out /tmp/opencode/auto_known_flask.json
```
