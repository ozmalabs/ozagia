# Graph engine speculation: corpus measurement (2026-11-25)

Status: prototype wire/readiness measurement on acquired local corpus
checkouts. It is **not** a model-task gain, does not call a model, does not use
the network, and does not execute target code. It builds graph snapshots in each
checkout's own `.ozagia` state.

## Commands

```bash
# Default process worker, 150 ms simulated think time, 1,200-token budget
.venv/bin/python scripts/measure_graph_engine_corpus.py

# One checkout, or a longer simulated think
.venv/bin/python scripts/measure_graph_engine_corpus.py --repo flask
.venv/bin/python scripts/measure_graph_engine_corpus.py --repo ozagia --think-ms 1500

# Thread fallback
OZAGIA_ENGINE_SPEC_PROCESS=0 .venv/bin/python scripts/measure_graph_engine_corpus.py --repo fd
```

The script defaults to `fd`, `flask`, `httpx` and this repository (`ozagia`).
`express` is JavaScript-only and the graph builder indexes Python/Rust/Lua
sources here, so it was not used as a symbol task. No target project is
imported or run.

## Corpora and warm/build cost

| Repo | Build s | Warm find ms | Nodes | Edges | Snapshot bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| fd | 0.11 | 51.8 | 583 | 1,523 | 313,134 |
| flask | 0.17 | 26.6 | 1,625 | 4,252 | 849,824 |
| httpx | 0.15 | 32.8 | 1,341 | 3,169 | 640,358 |
| ozagia | 3.82 | 1,146.6 | 18,450 | 59,773 | 10,815,442 |

Build is a cold `build_index(..., include_docs="features", include_git=False)`
inside each checkout's `.ozagia`. Warm find is a first symbol search before the
engine task; it also warms tag/search caches that a forked child inherits.

## Task and result

Each run: `hello`, `find` with `spec: true`, 150 ms think, `fetch` (wait 50 ms),
`expand` callees, `impact` depth 2. The stateless graph comparison is the same
work through `find`, `node`, two `expand`s and `impact`.

| Repo | Engine tokens / RT | Graph tokens / RT | Ready at response | Ready before fetch | Hit / miss | Worker ms | Hit rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| fd | 1,432 / 5 | 3,952 / 5 | 0 / 2 | 2 / 2 | 2 / 0 | 49.5 | 1.0 |
| flask | 2,979 / 5 | 10,311 / 5 | 0 / 2 | 2 / 2 | 2 / 0 | 24.3 | 1.0 |
| httpx | 3,214 / 5 | 12,185 / 5 | 0 / 2 | 2 / 2 | 2 / 0 | 24.6 | 1.0 |
| ozagia (150 ms) | 2,672 / 5 | 12,055 / 5 | 0 / 2 | **0 / 2** | **0 / 2** | **1,171.0** | **0.0** |
| ozagia (1,500 ms) | 3,131 / 5 | 12,055 / 5 | 0 / 2 | 2 / 2 | 2 / 0 | 1,116.4 | 1.0 |

All successful process runs used `worker_mode=process`, `prepared=2`,
`wasted=0`, `cancelled=0`, and `freshness=fresh`; no withheld results occurred.
The thread fallback on `fd` produced the same 1,432 tokens, `ready_before=2`,
and `hit_rate=1.0` with `worker_mode=thread`.

### The ozagia miss

On the 18k-node checkout, the second speculative candidate took about 1.2 s.
The 150 ms think window plus 50 ms fetch wait therefore returned two `pending`
states and recorded two misses. The entries were ready later in the follow-up
(`ready_after_followup=2`), and a 1,500 ms simulated think produced two hits.
This is the non-preemptible long-call limit: the worker checks its 300 ms
deadline between candidates, not inside one graph call. It is not a wire
failure or a token regression; it is why the next step needs preemption or a
supervised process/daemon worker.

## Process lifecycle and fallback

- One forked child per engine when fork is available (`OZAGIA_ENGINE_SPEC_PROCESS=auto`).
- The child builds a speculation-disabled engine from the inherited snapshot and
  sends `PreparedSpec` messages. It does not touch parent sessions.
- A small parent listener thread receives IPC and applies results only after
  revalidating session id, snapshot, freshness, scope, generation and expiry.
- `OZAGIA_ENGINE_SPEC_PROCESS=0` uses the daemon-thread worker.
- If fork is unavailable, the child cannot start, or it dies, outstanding jobs
  are cancelled and later jobs use the thread fallback.
- `bye` cancels one session; engine close stops the listener, terminates/joins
  the child if necessary, clears the queue and closes the pipe.
- Tests cover a real child job, fetch-after-ready, reaping on close, child crash
  fallback, changed generation discard, no writes, and both process/thread paths.

## Limits

- No live model was used. The local OpenAI-compatible endpoint on
  `127.0.0.1:8000` refused the connection, so the live-session part is blocked.
  A live run requires `--live`, `OZAGIA_EVAL_LIVE=1` and explicit human approval;
  neither a local model nor a hosted fallback was started.
- Fork inherits a snapshot at first job; later parent graph changes are not
  shared into the child. Parent apply validation remains authoritative.
- The child has no separate CPU/memory budget. The parent listener is only IPC
  and application, not graph computation.
- One long graph call is not preempted and can overrun both the think window and
  the worker deadline.
- The corpus is a few local checkouts with simulated think time, not real model
  latency or a broad sample. The measurements are wire/readiness, not task
  quality.
- Ozma transport is explicitly out of this slice.

## Remaining work

- Supervise/restart the child and add real resource limits; consider a
  long-lived daemon rather than one fork per engine.
- Preempt or cancel a single long graph call, or split expensive operations.
- Evaluate hit/waste with real model think times and more repositories.
- Keep the thread/inline workers for platforms without safe fork.
