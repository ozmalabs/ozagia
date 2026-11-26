# Ozma integration and API intelligence plan

Status: third delivery implemented, verified 2026-11-24. The read-only
context-provider bridge, OpenAPI mapping resolution, doctor/status diagnostics
and multi-repository fixture are implemented; the HTTP surface, general
filesystem watcher and live agent benchmark remain follow-on work. The three
delivery-status sections below record the tranches.

## Analysis and boundaries

Ozma-ai's Python backend has an OpenAI-compatible gateway, context assembly, task APIs and repository-group/contract APIs. Ozagia already exposes `DevMemory.context`, snapshot staleness, incremental reingest, MCP and adapters. A useful first integration can compose these without importing a target repository or adding dependencies to ozagia core. Existing context budgets are approximate, the snapshot check has limits, and API specifications do not prove runtime implementation or consumer links.

## Delivery sequence

1. Implement a versioned, read-only Python context-provider boundary over an existing snapshot. Return bounded context and snapshot metadata; refuse stale snapshots by default, allow explicit opt-in, report limitations, and avoid implicit indexing or model calls. Verify with fixture snapshots, stale state and argument validation.
2. Add local read-only API contract discovery for selected OpenAPI JSON specs, including endpoints, schema references, explicit unsupported cases and source provenance; expose it through a usable entry point. Test malformed inputs and containment. Do not infer implementation/consumer links.
3. Improve operational diagnosis of snapshot and optional capabilities using existing status data. Clearly distinguish absent, stale and unknown.
4. Review the result and run relevant tests and lint. Do not modify ozma-ai or trigger live model evaluations; establish a consumer-side bridge after its contract and deployment requirements are agreed.

## Follow-on work

- Add explicit, provenance-bearing producer/consumer/test mappings and a multi-repository fixture; support YAML through an optional parser only after defining parser and reference safety.
- Integrate an opt-in adapter at ozma-ai `backend/ozma/context/assembler.py:assemble`, which already accepts a repository root. Render a labeled, bounded block and account for it in assembled token estimates; deduplicate with existing file-reference and compression blocks. Handle missing package, snapshot, stale or unknown state as a reported fallback. Enforce timeout at the caller, and compare four benchmark arms including cold/warm preparation costs. Live agent runs require the approval and gates in AGENTS.md.
- Consider versioned read-only HTTP and MCP APIs after authentication, root isolation, pagination, freshness and compatibility contracts are designed. Do not proxy arbitrary mutation or execute generator commands.
- Expand freshness checks for unknown tree state and process concurrent changes; assess whether watcher and storage changes are needed through measured scale.

## First delivery status (historical)

This records the first tranche and is superseded by the third delivery below.

The context-provider boundary, snapshot verification status, and selected JSON
OpenAPI indexing utility are implemented with focused tests. The API indexer
is an adapter building block, not automatic discovery, and does not establish
consumer/test links. The provider is not yet connected to the Ozma assembler.
A full doctor command, multi-service fixture, HTTP surface and live benchmark
remain follow-on work. The existing `stale` boolean is preserved for callers;
use the new freshness value where an unverified snapshot must be distinguished.

## API exposure decision

The existing stdio MCP interface already provides local graph operations. Keep
the Ozma connection local with bounded workers; do not expose a network
listener as part of this delivery. Before adding HTTP, define a
versioned, read-only contract with authenticated principals, server-side
allowlisted repository identities (never arbitrary client-supplied paths),
root containment after symlink resolution, explicit snapshot/freshness status,
request size and time limits, stable pagination bound to a snapshot version,
structured errors and response size limits. Per-principal isolation, stale
snapshots, malformed input, pagination during refresh, and non-loopback
configuration need integration tests. Mutations, arbitrary commands and
untrusted adapter execution are outside that surface. Authentication and
multi-user repository ownership belong to the hosting application; simply
binding a FastAPI route does not provide them.

A filesystem watcher is deferred until a measured need: reconcile against disk
at a safe query/session boundary, coalesce modifications, treat watcher overflow
as unknown, and compare incremental output with a clean rebuild. Git digest
verification is a point-in-time check, not an atomic checkout snapshot.

## Second delivery status (historical)

This records the second tranche and is superseded by the third delivery below.

The Ozma assembler now has an opt-in integration; the provider, doctor command,
explicit API mapping library and a multi-repository fixture have focused tests.
The mappings preserve supplied assertions but API labels are not yet linked to
indexed OpenAPI nodes; defining and validating a shared repository/operation
namespace remains necessary. An offline four-arm context experiment is
available in `evaluation/ozma_context/compare_assembler.py`; it is not an
agent-outcome benchmark. The HTTP surface, YAML parser, general watcher and
live agent evaluation remain follow-on work. The bridge's provider calls share
one worker; timeout does not cancel an in-progress query. Ozagia's index
configuration remains process-global, so use one configured repository per
Ozma process. The provider declines to certify freshness when the loaded
digest scope differs from the target repository configuration.

## Third delivery status

Explicit mapping references now resolve to unique namespaced OpenAPI operation
nodes; unresolved and ambiguous references are rejected. Adapter graph
construction and reingest have an end-to-end fixture. Optional OpenAPI YAML
uses the `openapi-yaml` extra and rejects aliases, unsupported tags and
oversized or excessively deep inputs. Reference URLs are never fetched.

The Ozma integration uses separate isolated Python workers per repository,
with bounded concurrency and process timeout. The provider compares checkout
identity and tree digest around context retrieval. This detects ordinary
concurrent changes but cannot provide an atomic filesystem snapshot. The
fixture runner records cold and warm preparation and four context arms; the
results and the real-repository timing caveats are in
[the fixture report](../results/ozma_context_fixture.md). The HTTP and watcher
assessment above remains the decision: no network listener or filesystem
watcher until the corresponding requirements and measured need justify one.

A concrete two-session local-model smoke test is prepared in
[the live-evaluation candidate](2026-11-16_live_eval_candidate.md). Running it
requires a person's approval and the live gates described in `AGENTS.md`.

## Approved live smoke test

The approved two-arm Ocelot smoke test was run once per arm. Both passed the
task check; the graph-intent run took longer and reported more input tokens in
this sample. See [the pilot report](../results/ocelot_graph_intent_pilot_2026-11-16.md)
for measurements and limitations. It is not evidence of average agent benefit.
