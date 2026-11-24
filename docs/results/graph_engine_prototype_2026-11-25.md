# Graph engine prototype measurement (2026-11-25)

Status: prototype wire measurement. **It is not a model-task evaluation and
does not show that an agent solves tasks better or faster.** It counts
estimated response tokens (`len(JSON) / 4`) and round-trips for the same
representative task on a small offline fixture.

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
surface is given an artificial token budget below its default.

## Result

| Surface | Round-trips | Estimated tokens | Relative to engine |
| --- | ---: | ---: | --- |
| full MCP (`dev_context`, `dev_repo_map`, `dev_symbol_callers`, `dev_outline`, `dev_tests`) | 5 | 1,705 | 2.9× |
| graph surface (`find`, `node`, `expand`, `impact`) | 4 | 636 | 1.09× |
| engine surface (`graph`: `hello`, `find` + spec, `fetch`, `tests`) | 4 | **584** | — |

The engine used 52 fewer estimated tokens than the graph surface (~8%) and 4
round-trips, versus 5 for the `dev_*` sequence. The handshake cost 133 tokens;
without it the sequence is 451. The graph surface is already compact, so the
engine's gain over it is modest on a single small task. The engine's intended
advantage is stateful multi-turn use (`known` hashes, `next` paging, reused
speculation), which this measurement does not quantify across turns.

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
| engine | `hello` | 133 |
| engine | `find` + `spec: true` | 174 |
| engine | `fetch` (2 prepared handles) | 212 |
| engine | `tests` | 66 |

## Speculation in the measured run

`find` planned six candidates and prepared two synchronously:

```json
{"handles": ["q1", "q2"], "ready": 2, "pending": 4}
```

`fetch` returned both prepared payloads (outline and callers) without repeating
graph work. **The preparation is synchronous and happens before the response
is returned; there is no background worker or overlap with model work in this
slice.** The ledger started at `prepared=2`, `fetched=2`.

## What this does and does not show

- It shows the engine's compact row encoding, `hello` overhead and one
  speculation round-trip on a tiny fixture.
- It does **not** measure model tokens consumed, model latency, task success,
  generalization to larger repositories, or speculative hit rate.
- It does **not** show that the engine is always smaller: a task that only needs
  one `dev_context` call can be cheaper on the full surface.
- The token estimate is `len(JSON) / 4`, matching the rest of ozagia; a real
  tokenizer will differ.

Next: a daemon/process worker for true overlap, then a hit-rate/waste
evaluation on larger repositories before changing any default.
