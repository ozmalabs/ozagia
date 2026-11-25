# Graph engine speculation: corpus measurement (2026-11-25)

Status: prototype wire/readiness measurement on acquired local corpus
checkouts. It is **not** a model-task gain, does not call a model, does not use
the network, and does not execute target code. It builds graph snapshots in each
checkout's own `.ozagia` state. A later supervision/preemption slice is
test-covered in `tests/test_graph_engine.py` but was not re-run against this
corpus here.

## Commands

```bash
# Default process worker, 150 ms simulated think time, 1,200-token budget
.venv/bin/python scripts/measure_graph_engine_corpus.py

# One checkout, or a longer simulated think
.venv/bin/python scripts/measure_graph_engine_corpus.py --repo flask
.venv/bin/python scripts/measure_graph_engine_corpus.py --repo ozagia --think-ms 1500

# Thread fallback
OZAGIA_ENGINE_SPEC_PROCESS=0 .venv/bin/python scripts/measure_graph_engine_corpus.py --repo fd

# Supervisor, restart, preemption and resource-limit tests (no model/network)
.venv/bin/python -m pytest -q tests/test_graph_engine.py -k \
  "process or thread or restart or timeout or resource or close"
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
This is the non-preemptible long-call limit of the measured run: the worker
checked its 300 ms deadline between candidates, not inside one graph call. The
supervised worker now has a hard per-job watchdog that kills and replaces the
child at the worker budget plus grace; that path is test-covered, but this
corpus measurement still reflects the earlier unsupervised behaviour and was
not re-run. It is not a wire failure or a token regression.

## Process lifecycle and fallback

- One forked child per engine when fork is available (`OZAGIA_ENGINE_SPEC_PROCESS=auto`),
  plus a parent IPC listener and a supervisor thread.
- The child builds a speculation-disabled engine from the inherited snapshot and
  sends `PreparedSpec` messages. It does not touch parent sessions.
- A small parent listener thread receives IPC and applies results only after
  revalidating session id, snapshot, freshness, scope, generation and expiry.
  Every job is tagged with the child epoch; a late result from a replaced child
  is ignored rather than applied twice.
- `OZAGIA_ENGINE_SPEC_PROCESS=0` uses the daemon-thread worker.
- If fork is unavailable, the child cannot be started, or it dies, outstanding
  jobs are cancelled and later jobs use the thread fallback. A child that can be
  restarted is replaced with bounded exponential backoff.
- `bye` cancels one session; engine close stops the supervisor, terminates/joins
  the child if necessary, joins the listener, clears the queue and closes the
  pipe.
- Tests cover a real child job, fetch-after-ready, reaping on close, child
  crash/restart and stats, restart exhaustion to the thread fallback, hard
  timeout preemption, resource-limit reporting, duplicate/stale discard,
  close during a job/restart, changed generation discard, no writes, and both
  process/thread paths.

## Supervision and preemption (not previously measured)

This slice adds supervision and process-granularity preemption. The following
values are test-covered behaviour, not a new corpus measurement:

| Property | Value |
| --- | --- |
| Idle heartbeat | child sends every 500 ms; 3 s stale threshold |
| Restart backoff | 50 ms base, doubling, cap 2 s (`OZAGIA_ENGINE_SPEC_BACKOFF_MS`) |
| Consecutive restarts | 3 default, then thread fallback (`OZAGIA_ENGINE_SPEC_RESTARTS`) |
| Hard per-job watchdog | worker budget + 250 ms default (`OZAGIA_ENGINE_SPEC_GRACE_MS`) |
| Address-space limit | 2 GiB per child (`OZAGIA_ENGINE_SPEC_MEM_MB`) |
| Cumulative CPU limit | 300 s per child (`OZAGIA_ENGINE_SPEC_CPU_S`) |
| Stats | `st.w`: mode, restarts, crashes, kills, timeouts, queue, job, deadline, last error, limits |

On a deadline overrun the supervisor kills the child, cancels that child's
outstanding handles as `preempted`, schedules a replacement, and the parent
continues serving normally. Counts and the last error are visible with
`stats: true`; no graph content is included. Because `RLIMIT_CPU` is cumulative
for a child's lifetime (not per job), a CPU-limit death is an expected restart,
not a per-job accounting signal.

## Limits

- No live model was used. The local OpenAI-compatible endpoint on
  `127.0.0.1:8000` refused the connection, so the live-session part is blocked.
  A live run requires `--live`, `OZAGIA_EVAL_LIVE=1` and explicit human approval;
  neither a local model nor a hosted fallback was started.
- Fork inherits a snapshot at first job; later parent graph changes are not
  shared into the child. Parent apply validation remains authoritative.
- Limits are per child, not per job. `RLIMIT_AS` bounds address space;
  `RLIMIT_CPU` accumulates over the child's lifetime and is reset only by
  replacing the child. A platform without `resource` reports `unsupported`
  and still runs the job.
- Preemption is at process granularity. One graph call is not interrupted
  in-process: the whole child is killed and later work gets a fresh child.
  The thread fallback cannot preempt a running call at all.
- The first child is forked before the supervisor/listener threads start, but
  a restart forks from a process that still has threads. The child only builds
  a read-only engine; Python may still warn that fork in a multi-threaded
  process can deadlock. A long-lived daemon/forkserver would avoid this and is
  future work.
- After the bounded restart budget is exhausted, speculation continues on the
  thread fallback with no hard timeout for that engine.
- The corpus is a few local checkouts with simulated think time, not real model
  latency or a broad sample. The measurements are wire/readiness, not task
  quality.
- Ozma transport is explicitly out of this slice.

## Remaining work

- Consider a long-lived daemon rather than one fork per engine, and tune the
  hard timeout against real large-repository fetches.
- Split expensive operations or add in-call cancellation; process replacement
  loses that child's in-flight work.
- Evaluate hit/waste and timeout/preemption counts with real model think times
  and more repositories.
- Keep the thread/inline workers for platforms without safe fork.
