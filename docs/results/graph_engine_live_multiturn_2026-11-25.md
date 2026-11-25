# Graph engine stateful multi-turn: live A/B on flask (2026-11-25)

Status: **on this sample the stateful engine with `known` deltas beats the
stateless control at repository scale: −6.4% total model tokens (81,058 vs
86,589), with the same 18/18 turn acceptance, the same 22 tool calls, and −8.9%
wall time.** The win is not uniform. Symbol-outline tasks saved 1.5–2.1%; a
file-outline refresh saved 17.0%. Turning speculation on erased most of the gain
(81,922, −5.4% vs control, +1.1% vs `engine_known`) because the 8 prepared
payloads were never fetched (0 hits). This is one local 27B model, one pinned
`pallets/flask` checkout, two repeats.

**Follow-up (same day): `find` now participates in the `known` protocol.** A
repeated find with matching hashes returns only `u` key handles. A new
three-turn task (`multiturn_find_refresh`) repeated a 20-row `find` and saved
**−21.8% total model tokens** on flask (`engine_known` 28,088 vs control
35,900) with 6/6 turn acceptance; on the tiny fixture it saved −12.8% (12,612
vs 14,468) with 6/6. The deterministic 20-row flask find response fell from
1,189 to 300 engine tokens and 4,572 to 1,177 wire bytes; net traffic including
the larger `known` request was still −1,587 bytes. Details are in
[`#repeated-find-known-delta-follow-up`](#repeated-find-known-delta-follow-up).

**Follow-up (2026-11-26): `known` identity is op-scoped.** The engine now
remembers hashes per `(op, key)`, so a `find` hash and an `outline` (or
`context`) hash for the same key no longer collide. Clients may send
`{op, key, hash}`; an entry with no `op` keeps the legacy key-only rule
(compare the supplied hash with the requested op's current row shape, so the
last find hash still matches a repeated find after an outline). The live
driver now stores `{op, key, hash}` and injects only the current op's entries.
The deterministic 20-row synthetic repeated-find measurement is 1,390 t /
5,571 B full versus 158 t / 640 B delta; the op-tagged request grows from 75
to 1,676 B (legacy: 1,396 B), leaving a net saving of 3,330 B (legacy:
3,610 B). The extra 280 B is request wire only; `known` is not shown to the
model. **Live re-measurement: unmeasured** in this follow-up (no local
`18020` tunnel/API key was available and live model runs need approval).
The earlier −21.8% model-token result should therefore be treated as the last
live measurement, not rerun evidence for the op-scoped client. See
[`#op-scoped-known-identity-2026-11-26`](#op-scoped-known-identity-2026-11-26).

**Follow-up (2026-11-26, auto-known):** the MCP `engine` surface and
`ozagia engine serve` now apply the session's remembered hashes when a
repeated `find`/`outline`/`context` omits `known`; `known: []` forces full
rows and direct `GraphEngine` callers keep the old default. A fresh live A/B
on the repeated-find task (flask, 2 repeats, 6 turns per arm) measured
**28,276** model tokens for auto-known, **28,284** for explicit client-sent
`known` and **36,068** for the stateless control, all 6/6 accepted with the
same 6 tool calls. Auto-known therefore reproduces the explicit delta saving
(−21.6% vs control) without any client-side hash bookkeeping. See
[`graph_engine_auto_known_2026-11-26.md`](graph_engine_auto_known_2026-11-26.md).

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
(6 tiny sessions). The repeated-find follow-up is
[`graph_engine_live_multiturn_find_2026-11-25_flask.json`](graph_engine_live_multiturn_find_2026-11-25_flask.json)
and
[`graph_engine_live_multiturn_find_2026-11-25_tiny.json`](graph_engine_live_multiturn_find_2026-11-25_tiny.json)
(6 sessions each, 3 turns × 3 arms × 2 repeats).

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
| `engine_known` | one session for all three turns | client sends remembered `{key,hash}` on a repeated `find`/`outline`/`context` | off (`spec=false` on `find`) |
| `engine_known_spec` | one session for all three turns | same | on (`spec=true` on `find`) |

The model-facing schema does **not** expose `known` to the model. The client
(the driver, standing in for Ozma's context assembler) remembers every
`{key, hash}` row it has already shown the model from `find`, `outline` and
`context`, and injects `known` when the model repeats that request. This is the
intended split: the model asks for a refresh; the client knows what the model
has already seen.

The control arm does what a stateless caller must do: it sends a fresh request
and re-serializes the full rows. The engine arm sends the same request with the
remembered hashes and receives only `u` key handles.

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
| `multiturn_find_refresh` (follow-up) | `find get_root_path`, `kinds=["sym"]`; report symbol, file and signature from the find row (20 rows). | Repeat exactly the same find once; answer symbol/file/signature from the row already in the conversation. | Impact of `get_root_path` at depth 2; same checks as `multiturn_get_root_path`. |
| `multiturn_average` (tiny) | `find average` then `outline`; report symbol, file, signature, summary. | Refresh; answer the file and the 6–13 span. | Impact at depth 2; check `scaled_average`, `summarize`, `tests/test_metrics.py`. |
| `multiturn_find_refresh` (tiny, follow-up) | `find average`, `kinds=["sym"]`; report symbol/file/signature (2 after trimming). | Repeat the same find once; answer from the conversation. | Impact of `average` at depth 2; same checks as above. |

The turn-2 refresh is the only place `known` can apply: the model is asked to
refresh the result it already has, and to answer from the copy in the
conversation rather than re-reading source. The repeated-find task was added
after the first measurement; it does not change the original three tasks or
their fixed checks.

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
| Multi-turn flask repeated-find follow-up | 35,900 | 28,088 | **−21.8%** |
| Multi-turn tiny repeated-find follow-up | 14,468 | 12,612 | −12.8% |

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
3. **A repeated find is the largest recyclable payload.** Now that `find` rows
   carry `h`, the 20-row flask find is 1,189 engine tokens and 4,572 bytes at
   budget 1,200; the repeated-find delta is 300 tokens and 1,177 bytes. Before
   this change the client had no hash to send, so every repeat paid full price.
4. **Speculation is all cost and no benefit in this workload.** The model does
   not fetch, and inline delivery requires a payload ready at response time,
   which did not happen. The `q` handles make every spec `find` slightly more
   expensive.

## Repeated-find `known` delta (follow-up)

One three-turn task, `multiturn_find_refresh`, run on the same flask checkout
and tiny fixture, same model and sampling, 3 arms × 2 repeats (6 sessions and
18 turns per repository). Turn 1 is a `find`; turn 2 repeats exactly that
`find`; turn 3 asks the usual depth-2 impact question. The model answers
turn 2 from the find row already in its conversation. The client remembers the
20 flask `{key,hash}` pairs (7 on tiny) from turn 1 and injects them on the
repeat.

### Flask

| Arm | Accepted | Prompt | Completion | **Total** | vs control | Tool calls | Tool-result tokens | Engine `t` | Engine wire (B) | Known / unchanged |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `control` | 6/6 | 35,132 | 768 | **35,900** | — | 6 | 6,774 | 7,278 | 27,879 | 0 / 0 |
| `engine_known` | 6/6 | 27,296 | 792 | **28,088** | **−21.76%** | 6 | 5,004 | 5,274 | 20,240 | 40 / 40 |
| `engine_known_spec` | 6/6 | 27,672 | 816 | **28,488** | −20.65% | 6 | 5,024 | 5,304 | 20,324 | 40 / 40 |

Per turn (sum of two repeats):

| Turn | `control` | `engine_known` | vs control | `engine_known_spec` | vs control |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 (`find`) | 5,140 | 5,140 | 0.00% | 5,202 | +1.21% |
| 2 (repeat `find`) | 12,016 | 9,404 | **−21.74%** | 9,564 | −20.41% |
| 3 (`impact`) | 18,744 | 13,544 | **−27.74%** | 13,722 | −26.86% |
| **Total** | **35,900** | **28,088** | **−21.76%** | **28,488** | **−20.65%** |

The turn-2 tool result fell from 1,135 to 250 tokens per session. As before,
the smaller refresh message is re-sent on every turn-3 model call, so turn 3
drops even more than turn 2. Turn-1 model totals are identical for `control`
and `engine_known`, as they must be; speculation adds its usual `q` handles.

### Tiny fixture

| Arm | Accepted | Total | vs control | Tool calls | Known / unchanged |
| --- | ---: | ---: | ---: | ---: | ---: |
| `control` | 6/6 | 14,468 | — | 6 | 0 / 0 |
| `engine_known` | 6/6 | 12,612 | **−12.83%** | 6 | 14 / 14 |
| `engine_known_spec` | 6/6 | 13,084 | −9.56% | 6 | 14 / 14 |

Per turn for `engine_known`: 2,704 / 4,144 / 5,764 versus control
2,704 / 4,776 / 6,988. The task's find returned 7 rows per repeat (the
symbol-only probe below returned 2).

### Deterministic find probe (live engine, no model)

The multiturn run now records a find probe next to the outline probe:

| Probe | Full | `known` delta | Saved | Request bytes | Net traffic |
| --- | ---: | ---: | ---: | ---: | ---: |
| `find get_root_path` (flask, 20 rows) | 1,189 t / 4,572 B | 300 t / 1,177 B | 889 t / 3,395 B | 78 → 1,887 | **−1,587 B** |
| `find average` (tiny, 2 rows) | 103 t / 390 B | 33 t / 125 B | 70 t / 265 B | 71 → 217 | **−120 B** |
| `outline get_root_path` (flask) | 84 t / 319 B | 27 t / 103 B | 57 t / 216 B | — | — |
| `outline average` (tiny) | 64 t / 239 B | 24 t / 91 B | 40 t / 148 B | — | — |

Net traffic subtracts the larger `known` request from the response saving. On
flask the request grows by 1,809 bytes but the response shrinks by 3,395, so
the delta is still a clear win. The existing engine-wire columns count
responses only.

### Cost of the hash column

`FIND_COLS` now ends in `h`, so every find row carries a 16-hex hash: +19 bytes
and about +5 estimated tokens per row. At budget 1,200 the 20-row flask find is
1,189 estimated tokens and still returns all 20 rows. Budget-tight pages can
return slightly fewer rows than before this change; the 500-token synthetic
page test pins that trimming plus `u` handles still advance `n` without
skipping the rows that were cut.

## Op-scoped `known` identity (2026-11-26)

The repeated-find run exposed one remaining stateful limitation: the client
remembered one hash per key, so seeing an `outline` for a key after a `find`
could overwrite the find hash. The engine's session map had the same key-only
shape. The protocol now accepts `known` entries as `{key, hash, op?}` and
remembers hashes per `(op, key)`:

- entries tagged with the current op (`find`, `outline` or `context`) only
  match that op's remembered row shape; another op's entry is ignored;
- entries with no `op` keep the legacy key-only rule: the supplied hash is
  compared with the requested op's current row hash. Because the hash covers
  the op-specific serialization, the last find hash still matches a repeated
  find after an outline of the same key, while an outline hash never satisfies
  a find row (and vice versa);
- response rows still carry `h`, and `u` handles belong to the response op.
  A client should store `{op, key, hash}` (`op` is the request op and `hash`
  the row's `h`); the live driver now does exactly that.

Deterministic synthetic measurement (20-row find, no model, fixed fixture):

| Case | Full | `known` delta | Request | Net traffic |
| --- | ---: | ---: | ---: | ---: |
| Op-scoped `{op,key,hash}` | 1,390 t / 5,571 B | 158 t / 640 B | 75 → 1,676 B | **−3,330 B** |
| Legacy `{key,hash}` | 1,390 t / 5,571 B | 158 t / 640 B | 75 → 1,396 B | **−3,610 B** |

The op tag costs 280 request bytes for 20 rows and does not change the response.
It is engine wire only, so the model-facing token saving is expected to match
the earlier result rather than improve it. The previous live 21.8% saving was
not rerun: no local `18020` tunnel or model API key was available, and live
model runs require approval. Mark the live op-scoped result **unmeasured**;
do not cite the synthetic table as a model-token gain.

Speculation remains a separate concern. In these live workloads it prepared
payloads that the model never fetched and added `q` handles to find responses;
the measured effect was a net token cost (+864 tokens versus `engine_known` on
the original flask run, +400 on the repeated-find run). **Recommendation: do
not enable speculation by default.** Keep `spec`/`fetch` opt-in until a
workload shows prepared payloads actually being consumed.

## Product gap found, then fixed

The original run found that `known` deltas were implemented for `outline` and
`context` only:

- `_known_rows` was called by `_outline` and `_context`, not by `_find`;
- `FIND_COLS` omitted `h`, so a find response gave the client no hash to echo
  back even if `_find` accepted `known`.

Both are fixed:

- `FIND_COLS` ends with `h`; `_find` builds its rows through `_known_rows`, so
  matching rows return only `u` handles and changed/stale hashes reserialize
  with the current `h`.
- The internal page order (`c`/`u` per item) is kept until budget fitting is
  done, so `n.offset` advances by the number of items actually served and a
  trimmed page with unchanged handles does not skip the cut rows.
- The live driver remembers find hashes and injects `known` on a repeated
  `find`; `run_known_probe` now measures the find delta deterministically.

Failure modes and paging are covered by tests in `tests/test_graph_engine.py`
(`test_find_rows_carry_stable_hashes_without_known`,
`test_known_find_is_a_handle_and_change_reserializes`,
`test_known_find_trimmed_page_advances_without_skipping`,
`test_known_find_repeat_wire_saving_is_measurable`,
`test_live_driver_known_probe_measures_find_delta`).

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
- The repeated-find saving is measured on one task shape that repeats an
  identical query, and the client sends 20 `known` pairs to save a 20-row
  response. On a 2-row tiny result the net saving is only 120 bytes; small
  pages can lose most of the benefit to request overhead.
- Hashes are per item as serialized, not per key identity. The engine now keys
  its memory by `(op, key)` and accepts `{op, key, hash}`, so find and outline
  hashes for one key coexist. A client that still stores only `{key, hash}`
  can lose the find hash when it later shows itself an outline; that is the
  documented legacy fallback, not a server collision. The measured find task
  used only find rows, so all 20 pairs matched with either shape.
- The op-scoped client was measured deterministically, not live. The last live
  repeated-find result (21.8% on flask) predates the op tag and was not rerun;
  the op tag adds engine request bytes but is not shown to the model.
- Adding `h` to find rows costs about 5 estimated tokens per row. The 20-row
  flask find still fits at the 1,200 budget, but a budget-tight page may return
  one or two fewer rows than the pre-hash build.
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
- Test a workload with several file outlines and finds refreshed repeatedly,
  where `known` should have the largest effect.
- Exercise `fetch`/inline delivery live with a workload that makes a payload
  ready before the response. Until then keep speculation off by default.
- Re-run the repeated-find live task with the op-scoped driver when the
  queen tunnel and model key are available; the deterministic saving is known,
  the model-token saving is not.
- Decide whether a shorter find hash (or a compact page handle) is worth the
  smaller request when many rows are known. A 12-hex hash would cut the find
  request by about 20%; the response saving is unchanged. The op tag adds
  another 12 B per known row.
- Measure whether caching the per-turn tool schema/tool result across a longer
  session (more than three turns) changes the ranking.

## Checks run

```bash
.venv/bin/ruff check src/ozagia/core/graph_engine.py \
  scripts/eval_graph_engine_live.py tests/test_graph_engine.py \
  tests/test_graph_engine_live_driver.py tests/test_engine_http.py
# All checks passed

.venv/bin/python -m pytest -q tests/test_graph_engine.py \
  tests/test_graph_engine_live_driver.py tests/test_engine_http.py
# 77 passed
```

Op-scoped follow-up (2026-11-26) reran the same focused checks: 77 passed and
ruff clean. No full suite was run and REPO_SHAPE was not regenerated.

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

# Follow-up: repeated find, flask and tiny (1 task x 3 turns x 3 arms x 2)
OZAGIA_EVAL_LIVE=1 OZAGIA_LIVE_API_KEY="$(cat /tmp/opencode/graph-multiturn/key)" \
  .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --suite multiturn --engine-url http://127.0.0.1:8841 \
  --engine-token-file /home/matt/ozma/deep-review-corpus/repositories/flask/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --fixture /home/matt/ozma/deep-review-corpus/repositories/flask \
  --task-set flask --task multiturn_find_refresh --arm all --repeat 2 \
  --counterbalance --out /tmp/opencode/graph-multiturn/find_refresh.json

OZAGIA_EVAL_LIVE=1 OZAGIA_LIVE_API_KEY="$(cat /tmp/opencode/graph-multiturn/key)" \
  .venv/bin/python scripts/eval_graph_engine_live.py \
  --live --suite multiturn --engine-url http://127.0.0.1:8842 \
  --engine-token-file /tmp/opencode/engine-live/.ozagia/engine/token \
  --base-url http://127.0.0.1:18020 --model qwen3.8-27b \
  --fixture /tmp/opencode/engine-live \
  --task-set tiny --task multiturn_find_refresh --arm all --repeat 2 \
  --counterbalance --out /tmp/opencode/graph-multiturn/find_refresh_tiny.json
```

The checkout was not edited. `git status` in the flask checkout shows only the
untracked `.ozagia/` state directory; the snapshot is the pinned
`22d92470` build. The follow-up also wrote only its `.ozagia/engine/token`
inside that state directory.
