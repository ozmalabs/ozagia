# Graph engine prototype measurement (2026-11-25)

Status: prototype wire measurement. **It is not a model-task evaluation and
does not show that an agent solves tasks better or faster.** It counts
estimated response tokens (`len(JSON) / 4`), round-trips and speculation
outcomes on a small offline fixture. The background run sleeps 150 ms after
`find` to simulate model thinking time.

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
the v2 compact wire (`ozagia.graph_engine/2`) and its in-process background
speculation worker.

## Single turn

| Surface | Round-trips | Estimated tokens |
| --- | ---: | ---: |
| full MCP (`dev_context`, `dev_repo_map`, `dev_symbol_callers`, `dev_outline`, `dev_tests`) | 5 | 1,707 |
| graph surface (`find`, `node`, `expand`, `impact`) | 4 | 637 |
| engine sync v2 (inline worker) | 4 | **374** |
| engine background (daemon worker) | 4 | **374** |

The wire size is unchanged by moving preparation to the background; the
difference is *when* preparation happens. The background `find` response
returned with both prepared handles still pending (`q.r = 0`). After the
simulated 150 ms think time, both were ready; `fetch` was a hit with no graph
work.

Speculation ledger for the background run:

```json
{"prepared": 2, "fetched": 2, "wasted": 0, "evicted": 0,
 "cancelled": 0, "worker_ms": 1.0, "hits": 2, "misses": 0,
 "hit_rate": 1.0}
```

The synchronous inline run had the same 374 tokens but prepared inside the
`find` handler (`ready = 2` at response time, `worker_ms = 0.6`). Both runs
measured roughly 10–14 ms in the `find` handler on this tiny fixture; the
difference is noise, not a latency claim. The
background handler does not *wait* for preparation; the test suite proves that
with a manual worker, and the measurement shows pending-at-response followed by
ready-before-fetch.

## Multi-turn

The stateful run uses one engine session:

1. discover: `hello`, `find` with `spec: true`, sleep 150 ms, `fetch` both
   prepared handles;
2. known outline: `outline` with the fetched outline hash in `known`;
3. follow-up: `impact` (which includes covering tests) on the same session.

The stateless graph surface does the equivalent work without a session or
`known` cache: `find`, `node`, `expand`, a repeated `node`, then `impact`.

| Turn | Engine background | Engine sync v2 | Graph surface |
| --- | ---: | ---: | ---: |
| discover | 332 / 3 | 332 / 3 | 348 / 3 |
| known outline | **10 / 1** | 10 / 1 | 55 / 1 |
| follow-up impact/tests | 236 / 1 | 236 / 1 | 281 / 1 |
| **cumulative** | **578 / 5** | **578 / 5** | **684 / 5** |

Background multi-turn speculation: `ready_at_response = 0`,
`ready_before_fetch = 2`, `prepared = 2`, `fetched = 2`, `worker_ms = 0.2`,
`hit_rate = 1.0`. The known outline saves 45 estimated tokens versus the
stateless repeat (`10` vs `55`).

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

`find` carries compact handles/counts only:
`{"q": {"h": ["q1", "q2"], "r": 0, "p": 4}, "t": 130}`. The full ledger is
opt-in with `stats: true` and is not repeated on every response.

## Speculation readiness and accounting

The worker enqueues one job per `spec: true` response, selects at most two
candidates, and prepares them in a single in-process daemon thread. The handler
returns without waiting. In the background measurement:

- `ready_at_response`: 0 of 2 handles;
- `ready_before_fetch`: 2 of 2 after the simulated think time;
- `prepared / fetched / wasted`: 2 / 2 / 0;
- `worker_ms`: about 1 single-turn, 0.2 multi-turn on this fixture;
- `hit_rate`: 1.0;
- `cancelled`: 0.

A pending fetch with a short `wait_ms` is covered by tests: it returns a compact
pending result on timeout, and a later fetch can still hit. Snapshot/scope/
generation changes, session eviction, `bye` and engine close discard or cancel
entries and are covered by tests.

## What this does and does not show

- It shows that preparation can complete while the caller is "thinking"; the
  response does not wait, and the later fetch is a hit.
- It does **not** show model latency or task improvement. The simulated sleep
  is not a model turn, and the fixture is tiny.
- The background `find` handler was not faster in wall-clock on this fixture;
  the GIL and thread startup can cost as much as the tiny graph work it
  prepares. The intended benefit is overlapping *model-side* waiting on larger
  work, not winning a microbenchmark.
- It does **not** measure hit rate across real repositories or think durations.
- The token estimate is `len(JSON) / 4`; a real tokenizer will differ.

## Remaining overhead

- `hello` still costs 33 tokens per session; a long-lived transport could
  amortize it.
- `fetch` of two prepared payloads costs 169 tokens; a background worker moves
  when the work happens, not the wire size.
- `impact` remains the largest engine response (236 tokens); it needs a stricter
  budget/pagination or transport streaming.
- The worker is one daemon thread in the same process, GIL-bound, with no
  process isolation. One in-flight graph call can overrun the 300 ms worker
  deadline because it is not preempted.
- No hit-rate evidence on larger repositories or real think times yet.

Next: a daemon/process worker for true parallelism beyond the GIL, then a
larger-repository hit-rate/waste evaluation before changing defaults.
