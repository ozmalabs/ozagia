# Application profiling and telemetry

`ozagia profile` reads profile or telemetry artifacts that another tool already
produced and joins their active frames to the repository graph. It is offline
and read-only over the target: it does not import the project, start a
profiler, execute the target, call a model or use the network. The only
repository write is an explicit `--out` report directory; a missing tag cache
is built under the state directory.

This is different from the **session profile** (`ozagia-session-profile`, the
OpenCode Profile tab) and the **task profile** (`ozagia-tasks report`):
those describe agent work and task execution. This feature describes a
project's application code, based on measurements an external profiler or
telemetry pipeline recorded.

```sh
ozagia profile ingest app.prof --root .
ozagia profile ingest app.speedscope --root . --baseline before.speedscope
ozagia profile compare before.speedscope after.speedscope --root .
ozagia profile ingest app.prom --root .
ozagia profile run --allow-exec --tool cprofile -- python app.py
ozagia profile formats
```

The package also exposes `ozagia-profile` on a fresh installation:

```sh
python -m ozagia.profile_cli ingest app.prof --root .
ozagia-profile ingest app.prof --root .
```

An MCP client can call `dev_profile` with a workspace-relative artifact path
and a hotspot limit. The tool returns the same measurements and graph join as
`--json`, bounded to the requested rows; it never writes the report.

## Supported artifacts

| Format | Typical producer | Notes |
|---|---|---|
| `cprofile` | `python -m cProfile -o app.prof app.py`, `pstats` dumps | Function self/cumulative time, call counts and caller edges. It is a deterministic profiler, not a CPU sampler. |
| `speedscope` | `py-spy record -f speedscope -o app.speedscope -- python app.py`; pyinstrument/speedscope exports | Sampled stacks, and evented open/close stacks. `unit: none` remains sample counts unless `--interval-us` is supplied. |
| `chrome_cpuprofile` | `node --cpu-prof app.js`, Chrome DevTools exports | Chrome/Node CPU profiles. `(root)`/`(program)` frames are dropped; `(idle)` is excluded and counted separately. |
| `collapsed` | `perf script \| stackcollapse-perf.pl`, folded py-spy/perf output | One stack per line, `frame;frame weight`. Weights are samples by default; use `--weight-unit` or `--interval-us` when they are durations. |
| `otlp_json` | OpenTelemetry Collector JSON trace export | Service spans, parent/child relationships, durations, status and a bounded allowlist of `code.*`/HTTP/RPC/DB attributes. |
| `otlp_metrics` | OpenTelemetry Collector JSON metrics export | Gauges, sums/counters, histograms and summaries; histogram quantiles are bucket upper bounds. |
| `prometheus_text` | Prometheus/OpenMetrics text exposition (`/metrics`) | Counters, gauges, histograms and summaries, including `_bucket`/`_sum`/`_count` families. |

`--format auto` inspects the content, not only the extension. A `.json` file
that is not a recognized profile is rejected; the reader does not guess.

### Producing useful artifacts

```sh
# Python function profile
python -m cProfile -o app.prof -m your.module

# Python sampling profile with stacks (preferred for hot paths)
py-spy record -f speedscope -o app.speedscope -- python -m your.module

# Node
node --cpu-prof --cpu-prof-dir . app.js

# Linux native/perf, folded
perf script | stackcollapse-perf.pl > app.folded

# OTLP: export traces and metrics from your collector as JSON
# Prometheus text
curl -s http://localhost:9090/metrics > app.prom
```

`ingest` does not run these tools for you; it accepts their output. The
separate guided-collection command can run one explicitly approved profiler
command under the existing resource policy (below).

## Normalization

One artifact becomes one versioned `ozagia.app_profile` v1.0 record:

- all durations are integer **microseconds**;
- sampled profiles with no stated time unit keep integer **sample counts**
  (`sample_unit: samples`), and the report says so;
- frames are keyed by `(path, line, name)`, never by a process-local address;
- missing fields are `null`/unknown where the artifact distinguishes them; no
  measurement is invented;
- files outside the repository, synthetic paths (`<string>`, `node:internal`)
  and unmatched names are marked `external`, `unresolved` or `ambiguous`;
- inputs are bounded to 64 MiB, with per-table caps and explicit omissions;
- the source artifact's SHA-256, size, format and tool are recorded;
- metric values are decimal strings, not JSON floats, so a profile hash cannot
  silently lose precision; histogram buckets are normalized to cumulative
  counts and quantiles are reported as bucket upper bounds.

A report contains a `profile_hash` over the normalized record and a
`report_hash` over the report (excluding the hash field itself), so the same
input and graph produce the same report.

## Graph join

The join uses the existing tag cache and symbol graph:

1. an active frame with a file and line maps to the **innermost definition**
   whose line range contains it;
2. a frame with no line maps by exact qualname, then by a unique name; several
   matches are reported as `ambiguous`;
3. mapped symbols get direct caller/callee counts and a bounded two-hop caller
   impact (`impact_symbols`, `impact_files`) from
   `ozagia.core.symbol_graph`;
4. frames outside the tag corpus stay visible by name and path.

Reference resolution is syntactic. Python references resolve at local, import
or unique-name confidence; non-Python definitions are mapped by file and line
but their caller/impact edges are not inferred. The report states this; it
does not turn a syntactic absence into proof that a function is unused.

An existing tag cache is used as-is. A missing cache is built once under the
state directory; `--refresh` re-stats and updates it. If tag extraction is
unavailable or the cache cannot be read, the report still works with no graph
claims and says so.

## What the report answers

The report is designed for problem solving and performance/scaling work:

- **Hotspots** — self, inclusive, calls/hits and share of measured self time,
  ordered by `--sort self|inclusive|calls|impact`;
- **Hot paths** — the most expensive sampled stacks, cProfile caller→callee
  pairs, or span parent→child pairs;
- **Files and modules** — measured self/inclusive totals and how much of each
  is graph-mapped;
- **Telemetry** — service/operation counts, total/p50/p95 span durations,
  error status and the mapped frame;
- **Metrics** — counters, gauges, histograms and summaries by service/label,
  with histogram/summary quantiles and optional code-location mapping;
- **Coverage** — frames and self weight that are mapped, ambiguous, external,
  unresolved or not assessed;
- **Baseline comparison** — `--baseline` joins two artifacts by symbol or
  `file::name` (never by line) and reports self-time regressions, improvements
  and unmatched rows. Different units are refused rather than mixed.

Results are evidence, not a verdict:

- self time is not wall-clock time, and profiler overhead is not modelled;
- sampled stacks are statistical; a short profile can miss rare paths;
- cProfile's cumulative time double-counts recursion as the profiler reports it;
- OTLP spans can overlap or be sampled by the collector; trace totals are not
  automatically service wall time;
- one artifact has no load sweep, queueing model or causal experiment. The
  `not_assessed` list in the report says so explicitly.

## `--out`

```sh
ozagia profile ingest app.speedscope --root . --out .ozagia/profiles/app
```

Writes `profile.json` and `profile.md` atomically into the chosen directory.
The Markdown is a readable summary; JSON is the stable input for agents,
reviews or later comparisons. `--include-profile` adds the full normalized
profile to the JSON.

## Guided collection

`ozagia profile run` is a separate, opt-in execution path:

```sh
ozagia profile run --allow-exec --tool cprofile -- python app.py
ozagia profile run --allow-exec --tool pyspy -- python -m your.module
ozagia profile run --allow-exec --tool node -- node app.js
```

It requires `--allow-exec`, runs an argv list (never a shell), keeps the
working directory inside the repository, applies the existing memory/file
rlimits (the default file limit is 64 MiB) and a wall-clock timeout, and checks
resource admission before the child starts. `pyspy` requires `py-spy` on `PATH` or `--tool-path`; `node`
requires Node. The artifact, command log and report are written under `--out`
(default: `<state dir>/profiles/run-<stamp>`). The run record stores the
artifact hash, exit status, timeout, redacted argv, limits and admission
summary. A non-zero exit still produces a report when the artifact exists; the
command exits 1 and the report says the command failed.

The command is data-driven: no environment or file content is uploaded, and
there is no built-in attach to an already-running process.

## Deep review

Pass one or more profile reports (or artifacts) to deep review:

```sh
ozagia deep-review --profile-report .ozagia/profiles/app/profile.json
```

Measured hotspots and baseline regressions become `performance` findings with
`evidence_kind: observed` and evidence source `ozagia.core.profile`. Telemetry
error spans become `failure-modes` findings. Metric inventory is attached to
the performance/scalability manifest sections. The review still labels the
interpretation unverified: a hotspot is a measured cost, not a proven defect.

## Current limits

- Guided collection currently supports cProfile, py-spy and Node; it does not
  attach to a live process. Feed other tools' artifacts to `ingest`.
- No Go `pprof` protobuf, Python coverage, live Prometheus query API, Jaeger
  API client or OTLP HTTP/gRPC collector client yet.
- OTLP/Prometheus inputs are file snapshots; no automatic link from trace IDs to
  repository events or agent sessions.
- Histogram quantiles use bucket upper bounds; they are exact labels, not
  interpolated values.
- The graph join does not perform type inference, dynamic dispatch or
  cross-process call stitching.
- A profile is not a task critical path. Use `ozagia-tasks report` for agent
  task timing and `ozagia eval` for model/tool comparisons.

The next step, when needed, is an opt-in collector that runs an allowlisted
command under the existing resource policy and records the command, tool
version and artifact hash. It must remain separate from this read-only path.
