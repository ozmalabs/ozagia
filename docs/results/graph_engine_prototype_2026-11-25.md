# Graph engine prototype measurement (2026-11-25)

Status: prototype wire measurement. **It is not a model-task evaluation and
does not show that an agent solves tasks better or faster.** It counts
estimated response tokens (`len(JSON) / 4`) and round-trips for the same
representative task on a small offline fixture, plus a multi-turn run that
exercises the stateful `known`/session path.

Reproduce:

```bash
.venv/bin/python scripts/measure_graph_engine.py
```

## Fixture and task

A throwaway four-source/two-test Git repository (`pkg/core.py`,
`pkg/user.py`, `tests/test_core.py`, `tests/test_user.py`, `README.md`).
The task is:

> Trace how `add` is used: find the symbol, inspect its outline and callers,
> and list the tests that cover it.

Each surface runs the calls a client would naturally choose for that task; no
surface is given an artificial token budget below its default. The engine uses
the v2 compact wire (`ozagia.graph_engine/2`).

## Single turn

| Surface | Round-trips | Estimated tokens | Relative to engine |
| --- | ---: | ---: | --- |
| full MCP (`dev_context`, `dev_repo_map`, `dev_symbol_callers`, `dev_outline`, `dev_tests`) | 5 | 1,707 | 4.6× |
| graph surface (`find`, `node`, `expand`, `impact`) | 4 | 637 | 1.70× |
| engine (`graph`: `hello`, `find` + spec, `fetch`, `tests`) | 4 | **374** | — |

On this fixture the engine is now clearly smaller than the graph surface:
263 fewer estimated tokens (~41%) with the same number of round-trips. The
handshake is 33 tokens and no longer dominates the sequence. The full `dev_*`
sequence remains much larger because it spreads orientation across several
rich tools.

## Multi-turn

The stateful run uses one engine session:

1. discover: `hello`, `find` with `spec: true`, `fetch` both prepared handles;
2. known outline: `outline` with the fetched outline hash in `known`;
3. follow-up: `impact` (which includes covering tests) on the same session.

The stateless graph surface does the equivalent work without a session or
`known` cache: `find`, `node`, `expand`, a repeated `node`, then `impact`.

| Turn | Engine tokens / calls | Graph tokens / calls |
| --- | ---: | ---: |
| discover | 332 / 3 (`hello`, `find`, `fetch`) | 348 / 3 (`find`, `node`, `expand`) |
| known outline | **10 / 1** (`unchanged` only) | 55 / 1 (repeat `node`) |
| follow-up impact/tests | 236 / 1 | 281 / 1 |
| **cumulative** | **578 / 5** | **684 / 5** |

The engine saves 106 estimated tokens (~15.5%) across the multi-turn sequence
with the same round-trip count. The largest single saving is the known outline
(10 vs 55 tokens); compact column reuse and the smaller handshake account for
the rest. The follow-up `impact` is still the dominant cost.

## Per-call breakdown

| Surface | Call | Estimated tokens |
| --- | --- | ---: |
| full | `dev_context` (budget 6,000) | 1,030 |
| full | `dev_repo_map` (budget 1,500) | 346 |
| full | `dev_symbol_callers` | 218 |
| full | `dev_outline` | 53 |
| full | `dev_tests` | 60 |
| graph | `find` | 165 |
| graph | `node` outline | 55 |
| graph | `expand` callers | 136 |
| graph | `impact` | 281 |
| engine | `hello` (default) | 33 |
| engine | `find` + `spec: true` | 130 |
| engine | `fetch` two prepared handles | 169 |
| engine | `tests` | 42 |
| engine | `outline` with matching `known` | 10 |
| engine | follow-up `impact` | 236 |

The `find` response carries compact handles/counts only:
`{"q": {"h": ["q1", "q2"], "r": 2, "p": 4}, "t": 130}`. The full
prepared/fetched/wasted/evicted ledger is opt-in with `stats: true`; it is not
repeated on every response.

## Speculation in the measured run

`find` planned six candidates and prepared two synchronously:

```json
{"q": {"h": ["q1", "q2"], "r": 2, "p": 4}}
```

`fetch` returned both prepared payloads (outline and callers) without repeating
graph work. **Preparation is synchronous and happens before the response is
returned; there is no background worker or overlap with model work in this
slice.** The ledger began at `prepared=2`, and `fetch` advanced `fetched` to 2.

## What this does and does not show

- It shows the v2 compact handshake, tabular rows, column reuse, `known` delta,
  one speculation/fetch cycle and a session-reusing follow-up on a tiny
  fixture.
- It does **not** measure model tokens consumed, model latency, task success,
  generalization to larger repositories, or speculative hit rate.
- The single-turn result is now clearly better than the graph surface on this
  fixture, but the fixture is small and task-specific. A task that only needs
  one `dev_context` call can still be cheaper on the full surface.
- The token estimate is `len(JSON) / 4`, matching the rest of ozagia; a real
  tokenizer will differ.

## Remaining overhead

- `hello` still costs 33 tokens per session; a long-lived transport could
  amortize it further.
- The two-handle `fetch` payload (169 tokens) is the cost of carrying
  speculation on the wire; a background worker would not reduce its tokens, it
  would only move when the work happens.
- `impact` remains the largest engine response (236 tokens); no background
  worker changes that payload, only a stricter budget or a transport that
  streams/paginates it.
- The prototype still has no true overlap with model work and no hit-rate
  evidence on larger repositories.

Next: a daemon/process worker for true overlap, then a hit-rate/waste
evaluation on larger repositories before changing any default.
