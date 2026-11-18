# OzAGIa self-graph and general A/B experiments — overnight run

Status: in progress. The planned stopping time is 07:00 AEDT on 18 November
2026. The user subsequently removed that cutoff and requested work resume.
The user authorized local-model evaluations. Runs use OpenCode and the
local `vllm/qwen3.8-27b` endpoint; no hosted-model fallback is authorized.

The experimental plan, controls and limitations are in
[`../plans/2026-11-17_self_graph_ab_testing.md`](../plans/2026-11-17_self_graph_ab_testing.md).
The main checkout has pre-existing staged and unstaged work. It is not an
immutable evaluation target; all model tasks use separate, pinned checkouts.

## Offline baseline on the working checkout

The combined contract, inventory, capabilities, self-graph, task-preflight,
services, task-profile and critical-path test suite passed: **271 tests** in
56.04 seconds. This is a regression result, not model or A/B evidence.

Five sequential in-process `self_graph.build_self_graph()` calls took 1.6370,
1.6010, 1.6122, 1.6440 and 1.6298 seconds (median 1.6298 seconds). The
reported self-graph hash was identical in all five calls. The view contained
94 capabilities, 535 nodes and 1,247 edges and explicitly reported that its
coverage was not exhaustive. The audit reported three daemon protocol
documentation gaps (`hint`, `secrets`, `toolchain`), which were independently
confirmed as missing docstring entries on the merged baseline. These are
observations from a dirty working checkout and are not an immutable benchmark.
No CPU contention control or statistical comparison was performed.

## First evidence-backed changes

Profiling attributed most build time to repeated Python AST parsing while
probing implementation symbols. An isolated Luna worktree added a bounded,
locked, content-hash-validated symbol cache; content is re-read and hashed on
each lookup so changes do not depend on modification times. In a fresh Python
process on the working checkout after integration, the first and second builds
took 0.746 and 0.159 seconds and had the same self-graph hash as the earlier
builds. This is an exploratory before/after observation, not a randomized
benchmark; first-build measurements include different OS cache conditions.
Concurrent malformed-file coverage tests verify the cache's size bound, and
an edit-invalidation test verifies that changed source is reparsed.

The same change documents the three previously missing daemon operations and
rejects malformed falsey provenance fields instead of silently normalizing
them to empty lists. After integration the audit reported zero findings, and
76 targeted self-graph, services, inventory and capabilities tests passed;
Ruff and `git diff --check` passed. These changes are in the working tree,
without disturbing the user's pre-existing staged changes.

A subsequent fixture-based audit found and fixed two evidence-integrity
defects: a declared MCP tool could hide a missing-handler finding, and a
preflight report with modified content could retain an invalid embedded hash
in its provenance. The audit now reports the handler contradiction separately,
and preflight envelope creation rejects a mismatched hash. The integrated
changes passed 190 self-graph, preflight, service and contract tests.

The scheduling audit also found that a known 500 ms dependency prefix followed
by an unknown-duration span lost its dependency-only lower bound in `what_if`.
The scheduler now reports the existing 500 ms lower bound while preserving the
unknown overall makespan. All 69 critical-path and task-profile tests passed.

The resumed audit found that the five-second staleness memo could label a
structural answer fresh immediately after a tracked source edit. Structural
queries, definition enrichment and proactive graph context now request an
uncached freshness check; unknown and stale checks withhold graph results.
The isolated measurement for a forced check had a 13.84 ms median on that
fixture, compared with under 0.01 ms for a memo hit. This is a safety/latency
tradeoff, not a broad benchmark. The integrated change passed 324 intent,
context, memory and infrastructure tests.

## Graph preparation follow-up

An offline preparation follow-up measured three isolated runs: median cold
build plus daemon readiness was 6.818 s, unchanged warm preparation 0.868 s,
and one-file incremental refresh 1.164 s. The first lazy tag/symbol graph
build cost a median 2.960 s outside the initial preparation measurement.
These are fixture measurements, not end-to-end A/B results. Details are in
`graph_intent_prep_profile_2026-11-18.md`.

## Live evaluation

The general graph-intent pilot completed three paired tasks with one repetition
per arm. Both arms completed the requested answers; predefined frozen
assertions scored 12/13 in each arm after review. The missing baseline point
concerned a line number the prompt did not require; the graph arm's missing
point concerned an unsupported coverage caveat. Total
baseline wall time was 29.56 s; graph-assisted time was 43.63 s including
13.80 s graph preparation. Baseline used 17 tool calls and graph-assisted 11.
Task 3 made four graph queries and took longer in the graph arm. These are six
sessions and cannot support a general performance claim; the full methods,
per-task results, invalid attempts and artifact hashes are retained at
`/tmp/ozagia-ab-exp/final-report.md` and `final-results.json`.

One task exposed misleading graph telemetry: the tool reported `0/552 files
parsed` on a warm cache. `parsed` counts files newly parsed during that refresh,
not total cached coverage; reused files are omitted from the displayed count.
The model interpreted the message as a coverage limitation. The tool now reports
parsed, reused, failed, scanned and cached counts separately; a real warm-cache
regression verifies this, and 262 intent tests passed. The graph arm's assertion
score was corrected from 13/13 to 12/13 because its stated caveat did not follow
from the metric; the baseline remains 12/13. The pilot's raw report records what
the model saw;
it should not be interpreted as proof that the cached graph had no tags.

This pilot measures the broad graph-intent plugin, not the self-graph alone.
The narrow self-graph comparison completed 24 sessions with no process errors,
timeouts or recorded unsafe attempts. Its raw driver-time medians were 71.14 s
baseline and 41.80 s with the capability MCP tool (12 sessions each). These
numbers are descriptive, not a general speedup claim. Preparation and per-task
variation are included in the scored report. The two pilots should not be
combined: `graph_intent` includes additional services and graph preparation.

An independent audit verified that raw model/tool/token counts reproduce the
OpenCode event records and that the same capability CLI commands are available
to both self-graph arms. It found that the runner's original answer extraction
combined intermediate and final text, and that its score adapter did not gate
timeouts, driver errors or final JSON format. Post-run final-message extraction
and blinded scoring were performed separately; raw records are retained. The recorded first evidence
event must not be called first *correct* evidence without independent review.

At the 07:00 AEDT cutoff all 24 self-graph sessions had finished and no further
inference was running. Final-message extraction found **9/24** strictly valid
JSON-only responses; the other 15 used Markdown fences around parseable JSON.
An independent blinded Luna review described all responses as addressing the
task qualitatively, found no confidently critical false claim in a rapid review,
and noted that some evidence was weak or only cited capability IDs. That is not
a human verification. After the cutoff was removed, blind per-assertion Luna
scoring was completed before unmasking the arms. Baseline passed 12/12 sessions
and 42/42 points; the self-graph arm passed 11/12 with one uncertain, scoring
41/42 points. Three unsupported claim instances appeared in the self-graph arm;
none were found in baseline. Mean setup-inclusive times were 69.3 s baseline
and 56.7 s self-graph, with treatment faster in 9/12 pairs and a 57.2 s slower
outlier. This is a small agent-scored pilot, not human verification or a
general performance guarantee. Full scores, hashes and per-pair results are
in `/tmp/ozagia-self-graph-ab-full-cold-20261118/final_report.md`.

Follow-up on those unsupported claims found two ambiguity sources in actual
MCP results: per-kind counts covered all inventory kinds while the audit covers
only selected kinds, and a catalogue lookup miss did not explicitly distinguish
itself from a repository-wide search. Additive metadata now identifies audited
and all-kind totals and states the scope of an unknown-capability lookup. This
does not establish the absence of implementations or guarantee that a model
will interpret the information correctly. The integrated change passed 37
self-graph/capability and 60 MCP/CLI tests.

The planned expanded general A/B run was not started: an
independent audit found that its proposed runner inherited global configuration
and did not reliably pin subprocess imports or measurement boundaries. Those
validity and isolation issues must be fixed before using that runner.

After the pilot, OzAGIa added structured-JSON fence recovery. Replaying it
against the 24 extracted final answers yields 24 valid requested shapes: the
original nine strict JSON responses, 14 standalone fenced responses, and one
fenced response with separately retained introductory prose. This measures
recoverable formatting only; it does not change the original strict-format
result or establish factual correctness.

Follow-up fuzzing found the standard JSON decoder accepted duplicate keys and
non-finite values in structured model responses. The structured-output
consumers now reject those ambiguous/non-standard values, including numeric
overflow, while preserving valid fenced JSON and surrounding prose recovery.
All 24 saved pilot answers still parse under the stricter decoder; 160 focused
structured-output, native and review-driver tests passed.

The first general graph-intent pilot used disposable copies without `.git`.
All three attempted structural queries correctly reported graph freshness as
unknown, so those paired runs are **invalid for evaluating graph retrieval**.
They are retained as a harness diagnostic, and the same frozen tasks are being
repeated in Git-backed disposable repositories with verified freshness.

The Git-backed retry exposed a product defect: installing OzAGIa's own
OpenCode companion plugins after snapshot construction changed the working-tree
digest, making an otherwise unchanged graph stale. A real-installer regression
reproduced it. The fix excludes only byte-for-byte pristine generated plugin
outputs, allowing the profile plugin's interpreter substitution; user edits,
other plugin files and tracked changes still affect freshness. Integrated
verification passed 102 memory, reingest and OpenCode harness tests.

A real completed session had 94 OpenCode tool parts in its message database
but no host tool events in the persisted profile. The messages match the 2.3
terminal-tool field shapes, and a synthetic 2.3 event stream passes through
the plugin and profile store. The available records do not distinguish live
event non-delivery from dropped best-effort writes. Bounded, content-free
capture diagnostics were added to the plugin and stored profile so a future
session can distinguish these cases; 25 session-profile tests passed. This
does not retroactively recover the missing events or establish a critical path.

A subsequently authorized one-session local-model diagnostic used a fresh
Git-backed graph and the daemon socket. The plugin connected (`hello` and one
daemon `read`); OpenCode recorded one completed read tool call. The profiler
received eight step events and zero `session.message.content.updated` events,
and consequently recorded zero host tools. This confirms a live subscription
gap for that session, but does not by itself establish which alternative event
types are delivered. The first diagnostic attempt, which omitted the daemon
socket, is retained separately as invalid. Summary (no raw content):
`/tmp/ozagia-profile-diagnostic-valid-20261118/summary.json`.

After the tool-lifecycle subscription fix, another isolated local-Qwen
session completed one real `read` call. The OpenCode message database, host
profile and lifecycle counters each recorded one completed tool call; the
profile recorded its duration, and the graph was fresh before and after. The
content-free summary is at
`/tmp/ozagia-profile-lifecycle-audit-20261118/summary.json`. This establishes
end-to-end capture for one call, not full coverage across tool types or failure
and concurrency cases. The diagnostic's separate daemon-journal counter used
the wrong event field and is not evidence of a missing event.
