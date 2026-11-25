# Graph engine stateful multi-turn: live A/B on flask (2026-11-25)

Status: **on this sample the stateful engine with `known` deltas beats the
stateless control at repository scale: −6.4% total model tokens (81,058 vs
86,589), with the same 18/18 turn acceptance, the same 22 tool calls, and −8.9%
wall time.** The win is not uniform. Symbol-outline tasks saved 1.5–2.1%; a
file-outline refresh saved 17.0%. Turning speculation on erased most of the gain
(81,922, −5.4% vs control, +1.1% vs `engine_known`) because the 8 prepared
payloads were never fetched (0 hits). This is one local 27B model, one pinned
`pallets/flask` checkout, two repeats.

This result measures the claim one-shot tests cannot measure. The one-shot
comparison found the optimized engine roughly tied/slightly worse than the
stateless control on flask (+6.5% total tokens,
[`graph_engine_live_broader_2026-11-25.md`](graph_engine_live_broader_2026-11-25.md)).
Its intended advantage is stateful multi-turn use: `known` deltas, session
reuse, and speculation. The one-shot numbers are summarized separately below
and are **not** directly comparable in absolute tokens.

Raw measurements:
[`graph_engine_live_multiturn_2026-11-25_flask.json`](graph_engine_live_multiturn_2026-11-25_flask.json)
(18 flask sessions) and
[`graph_engine_live_multiturn_2026-11-25_tiny.json`](graph_engine_live_multiturn_2026-11-25_tiny.json)
(6 tiny sessions).

## Run context

| Item | Value |
| --- | --- |
| Model alias requested | `vllm/qwen3.8-27b` (provider-qualified) |
| Server model id | `qwen3.8-27b` (the only id returned by `/v1/models`) |
| Model root | `/app/models/Swift-1.5-Qwen3.8-27B-W4A16-HyperQwen` |
| Host | `queen` (`matt@192.168.40.215`) |
| Transport | local SSH tunnel `127.0.0.1:18020` → queen's `127.0.0.1:18020` |
| Endpoint key | queen's `~/.config/opencode/hyperqwen-api-key`, copied to a `0600` temp file; never printed, logged or committed |
| Repository | `/home/matt/ozma/deep-review-corpus/repositories/flask` at `22d924701a6ae2e4cd01e9a15bbaf3946094af65` |
| Snapshot | `.ozagia/graph.json`, 1,625 nodes / 4,252 edges, session freshness `fresh`; the checkout was not edited (only its state dir was used) |
| Tiny fixture | `/tmp/opencode/engine-live` at `05d8f7f344a74d8a6e16e908afa80949faceb88e`, 23 nodes / 54 edges |
| Engine | local `ozagia engine serve`, loopback only, port 8841 (flask) and 8842 (tiny) |
| Sampling | `temperature=0`, `top_p=1`, `seed=0`, `enable_thinking=false`, at most 6 tool calls per turn, 320 output tokens per model call, 600 for finalization |
| Budget | 1,200 estimated tokens per graph call on flask, 800 on tiny |
| Repeats/order | 2 repeats, counterbalanced: repeat 1 `control, engine_known, engine_known_spec`; repeat 2 reversed. Strictly sequential. |
| Sample | 3 flask tasks × 3 turns × 3 arms × 2 repeats = 54 turns; 1 tiny task × 3 turns × 3 arms × 2 repeats = 18 turns |
| Hosted fallback | none |

## Arms

All three arms use the same short system prompt, the same base tool schema, and
the same turn prompts. Only the transport differs:

| Arm | Session | `known` | Speculation |
| --- | --- | --- | --- |
| `control` | fresh `hello`/`bye` for every graph call | not exposed | off |
| `engine_known` | one session for all three turns | client sends remembered `{key,hash}` on an `outline` refresh | off (`spec=false` on `find`) |
| `engine_known_spec` | one session for all three turns | same | on (`spec=true` on `find`) |

The model-facing schema does **not** expose `known` to the model. The client
(the driver, standing in for Ozma's context assembler) remembers every
`{key, hash}` row it has already shown the model and injects `known` when the
model refreshes an outline. This is the intended split: the model asks for a
refresh; the client knows what the model has already seen.

The control arm does what a stateless caller must do: it sends a fresh
`outline` request and re-serializes the full rows. The engine arm sends the same
outline request with the remembered hashes and receives only `u` key handles.

## Tasks and fixed checks

Each task is a single conversation with three turns. Prompts, required keys and
checks are identical for every arm. The checks were not weakened after the
pilot. The only pre-run prompt change was a tool-argument clarification in turn
3 (`keys` is a list; use the symbol key from the outline), applied to all arms
after the pilot showed a wasted `impact` call with `key`. It does not favour an
arm.

| Task | Turn 1 | Turn 2 | Turn 3 |
| --- | --- | --- | --- |
| `multiturn_get_root_path` | `find get_root_path` then `outline`; report symbol, file, signature, summary. Checks: `get_root_path`, `src/flask/helpers.py`, `def get_root_path(import_name: str) -> str:`, summary contains “root path”. | Refresh the same outline once; answer the file and the 577–631 span from the outline already in the conversation. | Impact of `get_root_path` at depth 2; check `Scaffold.__init__`, `tests/test_helpers.py`, non-empty summary. |
| `multiturn_send_from_directory` | `find send_from_directory` then `outline`; report symbol, file, signature, summary. Checks: symbol/file, signature starts `def send_from_directory(`, summary contains “send a file”. | Refresh; answer the file and the 533–574 span. | Impact at depth 2; check `Flask.send_static_file`, `Blueprint.send_static_file`, `tests/test_helpers.py`, non-empty summary. |
| `multiturn_helpers_file` | `outline file:src/flask/helpers.py`; report the file and whether the outline contains `get_root_path` and `send_from_directory` (18 rows). | Refresh the same file outline once; answer the two start lines (577 and 533) from the outline already in the conversation. | Impact of `send_from_directory` at depth 2; same checks as above. |
| `multiturn_average` (tiny) | `find average` then `outline`; report symbol, file, signature, summary. | Refresh; answer the file and the 6–13 span. | Impact at depth 2; check `scaled_average`, `summarize`, `tests/test_metrics.py`. |

The turn-2 refresh is the only place `known` can apply: the model is asked to
refresh the outline it already has, and to answer from the copy in the
conversation rather than re-reading source.

## Results: flask

All three arms accepted **18/18 turns (6/6 tasks)**. The per-task token counts
below are two repeats each.

| Arm | Accepted | Prompt | Completion | **Total** | vs control | Tool calls | Tool-result tokens | Engine `t` | Engine wire (B) | Wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `control` | 18/18 | 83,918 | 2,671 | **86,589** | — | 22 | 14,250 | 15,728 | 59,866 | 39.52 s |
| `engine_known` | 18/18 | 78,362 | 2,696 | **81,058** | **−6.39%** | 22 | 13,088 | 13,862 | 53,018 | 36.02 s |
| `engine_known_spec` | 18/18 | 79,226 | 2,696 | **81,922** | −5.39% | 22 | 13,120 | 13,906 | 53,156 | 36.17 s |

Engine `t` and wire count engine responses; model usage is the tokenizer's
count. They are different units. `engine_known_spec` prepared 8 payloads and
had 0 hits.

### Per task

| Task | `control` | `engine_known` | vs control | `engine_known_spec` | vs control |
| --- | ---: | ---: | ---: | ---: | ---: |
| `multiturn_get_root_path` | 31,400 | 30,932 | −1.49% | 31,358 | −0.13% |
| `multiturn_send_from_directory` | 28,998 | 28,392 | −2.09% | 28,830 | −0.58% |
| `multiturn_helpers_file` | 26,191 | 21,734 | **−17.02%** | 21,734 | −17.02% |
| **Total** | **86,589** | **81,058** | **−6.39%** | **81,922** | −5.39% |

The file task dominates the aggregate. A single-symbol outline is only one row,
so its `known` refresh saves 57 engine tokens (84 → 27). The 18-row file
outline saves 495 engine tokens (714 → 219). On symbol tasks the engine win is
therefore small but consistent: −78 prompt tokens on turn 2 and a smaller
turn-3 context.

### Per turn

Summed over the three flask tasks and two repeats (six sessions per turn):

| Turn | `control` | `engine_known` | vs control | `engine_known_spec` | vs control |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 (find/outline) | 21,510 | 21,510 | 0.00% | 21,826 | +1.47% |
| 2 (refresh) | 26,064 | 24,212 | −7.11% | 24,528 | −5.89% |
| 3 (impact) | 39,015 | 35,336 | −9.43% | 35,568 | −8.83% |
| **Total** | **86,589** | **81,058** | **−6.39%** | **81,922** | −5.39% |

Turn 1 is identical for control and `engine_known`, as it must be: no client
state exists yet. All savings start at the refresh and then persist through
turn 3, because the smaller refresh message stays in the conversation and is
re-sent on both turn-3 model calls. That compounding is the stateful effect.

Per-turn detail (sum of the six sessions for that turn):

| Arm / turn | Prompt | Completion | Total | Tool-result tokens | Engine `t` | Engine wire (B) | Known / unchanged | Wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control T1 | 20,762 | 748 | 21,510 | 5,858 | 6,480 | 24,732 | 0 / 0 | 11.68 s |
| control T2 | 25,576 | 488 | 26,064 | 1,698 | 2,026 | 7,574 | 0 / 0 | 10.34 s |
| control T3 | 37,580 | 1,435 | 39,015 | 6,694 | 7,222 | 27,560 | 0 / 0 | 17.50 s |
| `engine_known` T1 | 20,762 | 748 | 21,510 | 5,858 | 6,306 | 24,114 | 0 / 0 | 11.07 s |
| `engine_known` T2 | 23,724 | 488 | 24,212 | 536 | 550 | 2,136 | 40 / 40 | 9.39 s |
| `engine_known` T3 | 33,876 | 1,460 | 35,336 | 6,694 | 7,006 | 26,768 | 0 / 0 | 15.56 s |
| spec T1 | 21,078 | 748 | 21,826 | 5,890 | 6,350 | 24,252 | 0 / 0 | 11.23 s |
| spec T2 | 24,040 | 488 | 24,528 | 536 | 550 | 2,136 | 40 / 40 | 9.31 s |
| spec T3 | 34,108 | 1,460 | 35,568 | 6,694 | 7,006 | 26,768 | 0 / 0 | 15.64 s |

The T2 tool-result drop (1,698 → 536) is the `known` delta; the larger T3
prompt drop is that smaller message being re-sent. T1 is untouched by
`engine_known`; spec T1 adds the `q` handles.

Representative single sessions (repeat 1; repeat 2 is identical except one
control completion):

| Session / turn | `control` | `engine_known` | `engine_known_spec` |
| --- | ---: | ---: | ---: |
| get_root_path T1 | 4,598 | 4,598 | 4,676 |
| get_root_path T2 | 4,606 | 4,528 | 4,606 |
| get_root_path T3 | 6,496 | 6,340 | 6,397 |
| helpers_file T1 | 1,962 | 1,962 | 1,962 |
| helpers_file T2 | 4,200 | 3,452 | 3,452 |
| helpers_file T3 | 6,915 | 5,453 | 5,453 |

## Results: tiny fixture

| Arm | Accepted | Total | vs control | Tool calls | Known / unchanged | Spec prepared/hits |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `control` | 6/6 | 15,158 | — | 8 | 0 / 0 | 0/0 |
| `engine_known` | 6/6 | 14,828 | −2.18% | 8 | 2 / 2 | 0/0 |
| `engine_known_spec` | 6/6 | 15,256 | +0.65% | 8 | 2 / 2 | 4/0 |

Per turn: control 4,560 / 4,488 / 6,110; `engine_known` 4,560 / 4,378 / 5,890;
spec 4,716 / 4,534 / 6,006. The tiny fixture reproduces the same shape at a
smaller scale. Speculation loses the known gain here too.

## `known` and speculation behaviour

Live counts over 6 flask sessions (3 tasks × 2 repeats):

| Arm | `known` pairs sent | Unchanged rows returned | Prepared | Fetched | Hits | Wasted (at stats read) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `control` | 0 | 0 | 0 | 0 | 0 | 0 |
| `engine_known` | 40 | 40 | 0 | 0 | 0 | 0 |
| `engine_known_spec` | 40 | 40 | 8 | 0 | 0 | 0 |

The 40 pairs are 1 + 1 + 18 per repeat: one row for each symbol outline and 18
for the file outline. Every sent hash matched, so every refresh returned only
`u` handles. The model answered every turn-2 question (file and start lines)
from the outline in its conversation; no arm re-read source.

Deterministic probes, no model:

| Probe | Full | `known` delta | Saved |
| --- | ---: | ---: | ---: |
| `outline get_root_path` (flask) | 84 t / 319 B | 27 t / 103 B | 57 t / 216 B |
| `outline file:src/flask/helpers.py` | 714 t / 2,662 B | 219 t / 856 B | 495 t / 1,806 B |
| `outline average` (tiny) | 64 t / 239 B | 24 t / 91 B | 40 t / 148 B |

For the file refresh, the client request grows from 87 to 1,576 bytes (18
hashes), so total traffic is 2,749 → 2,432 bytes (−317 B) even though the
response shrinks by 1,806 B. The engine wire figures in the results table count
responses only; they do not include the client's `known` request.

Speculation was exercised deterministically but not by the model. The flask
probe fetched both prepared handles after a 250 ms wait: handles 2,
`ready_at_response=0`, both fetch states `ready`, prepared 2, fetched 2, hits 2,
misses 0, `t=1,615`, wire 6,150 B. In the live sessions the model never called
`fetch`, and no payload was ready inline at response time, so prepared work was
unused. `wasted` reads 0 because the session stats were read before `bye`; the
`bye` cancels the 8 unused entries. **Do not read `wasted=0` as evidence that
speculation paid off.** Its measurable cost is the `q` handles added to the
`find` response: +78–80 model prompt tokens on each spec `find`, and +864 total
tokens versus `engine_known` over the run.

## One-shot versus multi-turn

| Setting | Control | Optimized engine | Difference |
| --- | ---: | ---: | ---: |
| One-shot flask (2026-11-25 broader sample) | 114,899 | 122,405 | **+6.5%** |
| One-shot tiny (same sample) | 17,762 | 18,118 | +2.0% |
| Multi-turn flask (this sample, `engine_known`) | 86,589 | 81,058 | **−6.4%** |
| Multi-turn tiny (this sample, `engine_known`) | 15,158 | 14,828 | −2.2% |

The one-shot row is the prior sample on the same pinned repository and model
root, not a rerun. The task sets and session lengths differ, so absolute totals
are not comparable; the sign is what matters. A one-shot task never asks for
the same rows twice, so `known` has nothing to reuse. The multi-turn tasks do,
and there the engine wins on this sample.

## Where the tokens went

1. **A one-row symbol outline is too small for `known` to matter.** 84 → 27
   engine tokens saves about 78 prompt tokens on the refresh, which is 1.5–2.1%
   of a ~15k-token symbol session.
2. **A multi-row file outline is where `known` pays.** 714 → 219 engine tokens
   removes 452 tool-result tokens from the refresh; that message is re-sent on
   both turn-3 calls, so turn 3 also drops by ~1,500 prompt tokens.
3. **`find` cannot use `known`.** `find` is the largest repeated payload
   (1,064–1,199 tool-result tokens at budget 1,200), but `FIND_COLS` does not
   expose `h` and `_find` never calls `_known_rows` (only `outline` and
   `context` do). A client therefore cannot delta a `find` result. This is a
   product gap, not a harness choice; see below.
4. **Speculation is all cost and no benefit in this workload.** The model does
   not fetch, and inline delivery requires a payload ready at response time,
   which did not happen. The `q` handles make every spec `find` slightly more
   expensive.

## Product gap found (not fixed)

`known` deltas are implemented for `outline` and `context` only:

- `_known_rows` is called by `_outline` (`src/ozagia/core/graph_engine.py:2166`)
  and `_context` (`:2280`), not by `_find` (`:2111`–`:2133`).
- `FIND_COLS` (`:95`) omits `h`, so a `find` response gives the client no hash
  to echo back even if `_find` later accepted `known`.

The engine's documented `known` scope already says “an outline or context
item”, so the docs are not wrong. But the largest repeated result cannot be
delta-reduced, which caps the stateful advantage. A follow-up could add hashes
to `find` rows (or a separate `find` handle) and route `find` through
`_known_rows`, with a regression test that a repeated `find` with matching
`known` returns `u`. No product code was changed by this evaluation.

## Limitations

- One local W4A16-quantized 27B model, one host, one real repository, one tiny
  fixture, two repeats, no human adjudication.
- Repeats were nearly deterministic at temperature 0/seed 0 (most token counts
  identical); the one observed difference was a control completion on the file
  task’s turn 3 (227 vs 264 tokens). Near-zero variance here is not evidence of
  stability across prompts or models.
- The multi-turn tasks were written for this comparison; their fixed checks
  cover file, signature, span, caller, and test evidence, but not every answer
  a human would accept. They were not weakened after the pilot; the only change
  was the turn-3 `keys`-argument clarification applied to all arms.
- The engine win is concentrated in the file-outline task. Two symbol tasks show
  1.5–2.1% savings. The aggregate −6.4% should not be read as a general
  per-task effect.
- The client, not the model, decides when to send `known`. A generic LLM client
  that does not track hashes gets none of this benefit; the model-facing schema
  intentionally hides `known`.
- `engine t` is `len(JSON)/4`; model `usage` is the tokenizer's count. Cached
  prompt tokens are reported but not modelled separately.
- `wasted` is read before `bye`, so unused prepared work is not counted as
  waste. The speculation cost is measured from the response and token deltas,
  not from that ledger field.
- The finalization budget (600) and per-call budget (1,200) are harness
  settings, fixed before the measured run.
- The key existed only in a `0600` temp file and was never printed or
  committed; the tunnel, key and temp files were removed after the run.

## Remaining work

- Repeat on a second real repository (for example `httpx`) and a second model.
- Test a workload with several file outlines refreshed repeatedly, where
  `known` should have the largest effect.
- Exercise `fetch`/inline delivery live with a workload that makes a payload
  ready before the response.
- Decide whether `find` should expose hashes and support `known` deltas; this is
  the largest remaining stateful win.
- Measure whether caching the per-turn tool schema/tool result across a longer
  session (more than three turns) changes the ranking.

## Checks run

```bash
.venv/bin/ruff check scripts/eval_graph_engine_live.py \
  tests/test_graph_engine_live_driver.py
# All checks passed

.venv/bin/python -m pytest -q tests/test_graph_engine_live_driver.py
# 7 passed
```

No full suite was run. No product code was changed.

## Live commands

Key read from a `0600` file into `OZAGIA_LIVE_API_KEY`; engine tokens read from
their `0600` files; neither value is printed.

```bash
# Tunnel
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:18020:127.0.0.1:18020 matt@192.168.40.215

# Engine token (state dir only) and loopback server
.venv/bin/python -m ozagia engine token \
  --root /home/matt/ozma/deep-review-corpus/repositories/flask
.venv/bin/python -m ozagia engine serve \
  --root /home/matt/ozma/deep-review-corpus/repositories/flask --port 8841
.venv/bin/python -m ozagia engine serve \
  --root /tmp/opencode/engine-live --port 8842

# Flask, 3 tasks x 3 turns x 3 arms x 2 repeats
OZAGIA_EVAL_LIVE=1 OZAGIA_LIVE_API_KEY="$(cat /tmp/opencode/graph-multiturn/key)" \
  .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --suite multiturn --engine-url http://127.0.0.1:8841 \
  --engine-token-file /home/matt/ozma/deep-review-corpus/repositories/flask/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --fixture /home/matt/ozma/deep-review-corpus/repositories/flask \
  --task-set flask --arm all --repeat 2 --counterbalance \
  --out /tmp/opencode/graph-multiturn/flask.json

# Tiny fixture
OZAGIA_EVAL_LIVE=1 OZAGIA_LIVE_API_KEY="$(cat /tmp/opencode/graph-multiturn/key)" \
  .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --suite multiturn --engine-url http://127.0.0.1:8842 \
  --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --fixture /tmp/opencode/engine-live \
  --task-set tiny --arm all --repeat 2 --counterbalance \
  --out /tmp/opencode/graph-multiturn/tiny.json
```

The checkout was not edited. `git status` in the flask checkout shows only the
untracked `.ozagia/` state directory; the snapshot is the pinned
`22d92470` build.
