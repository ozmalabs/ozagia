# Model-facing maps (M3)

Status: implemented subset of M3, 2026-11-18, in `src/ozagia/model_api/`.
This is a **read-only** model boundary: a map is a bounded view over owners
that already exist. It is not a new graph store, a transport, a permission, a
model call or a protocol authority. The later M4 edit/interaction path is not
implemented here.

The package is standard-library only. It does not import a target project,
refresh a snapshot, run a test, start a daemon, install anything, call a model
or write outside `<root>/.ozagia/`. The only write is bounded cache metadata
under `<root>/.ozagia/model_api/cache/`.

## What is implemented

| Module | Responsibility |
| --- | --- |
| `schema.py` | v1 request/map schemas, instance validation, canonical hashes, page cursors, fact projections and the plain renderer. |
| `policy.py` | Host-owned root/session/model/map/path/budget checks. |
| `project.py` | Bounded projectors for `graph`, `outline`, `capabilities` and `work`. |
| `cache.py` | Inspectable cache keys and entries under the repository state directory. |
| `negotiate.py` | Deterministic capability/support matrix, representation choice, explicit fallback and redacted trace. |
| `errors.py` | Typed service errors plus stable model-API reasons. |

Initial map types:

- **`graph`** — a bounded slice of the existing DevMemory snapshot
  (`ozagia.core.memory`). Nodes carry a bounded type/key/label/attribute
  projection; relations are snapshot edges whose endpoints are on the page.
- **`outline`** — signatures/headings from `DevMemory.outline_target`, never a
  source body. It accepts source/doc outline targets; JSON/YAML/TOML and other
  value-bearing formats are refused because their outline can expose values.
- **`capabilities`** — the WP1 catalogue joined to observed surfaces through
  `ozagia.core.self_graph`. Coverage is explicitly not exhaustive; readiness
  and permission are not observed. `audit.graph_truncated` repeats any
  self-graph projection truncation.
- **`work`** — the WP2 `ozagia.core.evidence` join over a goal, criteria, run
  manifests, test record and interpreter candidates. It stores hashes and
  redacted metadata, not prompts, secrets, task output or raw logs.

Every map carries source identities, repository/checkout/snapshot identity
(where the host can derive it), freshness, evidence and trust labels,
omissions, truncation, page bounds and an explicit state. A fresh view is
never inferred from a stale parent: graph/map freshness comes from the
owner's own observation.

## Canonical schema and hash decisions

These decisions are frozen for this slice and are tested by
`tests/test_model_api.py`.

- **Canonical JSON** is the v1 reference form from `core/contracts.py`: sorted
  keys, compact separators, UTF-8, no floats, signed 64-bit integers, no lone
  surrogates. Text and plain output are renderers, not authorities.
- **Request schema** is `{namespace: "ozagia.model_api.request", version:
  "1.0"}`. Only major 1 is accepted. Unknown non-`x-` fields are errors.
- **Map schema** is a normalised `core/contracts.validate_schema` record with
  namespace `ozagia.model_api.<map>` and version `1.0`. Its fingerprint is
  `core/contracts.schema_fingerprint` over exactly `namespace`, `version`,
  `types`, `required`, `constraints` and `sensitivity`.
- **Map instance** is a normalised `core/contracts.validate_instance` record:
  `schema`, `scope`, `snapshot`, `inputs`, `provenance`, `data`, optional
  `omissions`, `cursor`, `trace_id`, `created_at`.
- **Map content hash** is `core/contracts.instance_hash`; `created_at`,
  `cursor` and `trace_id` are transport-only and never enter the hash. The
  schema fingerprint is stored in `provenance.schema_fingerprint` and checked
  again by `validate_map`.
- **Rejection rules:** wrong namespace/type, unknown data field, wrong required
  set, wrong trust/evidence/freshness vocabulary, floats, non-canonical
  values, a page over the hard bound or a mismatch between the schema
  fingerprint and the v1 schema are refused. A map from another type is never
  reinterpreted.

Map pages are capped by caller budgets and hard bounds:

| Budget | Default | Hard maximum |
| --- | --- | --- |
| map JSON bytes | 64 KiB | 256 KiB |
| items per page | 25 | 200 |
| graph neighbourhood depth | 2 | 4 |

A page over the byte budget is trimmed deterministically (relations first,
then items) and labelled with the shared truncation envelope
`{truncated, omitted, next_cursor, reason}`. If even the metadata cannot fit,
the request fails with `budget_exceeded` / `map_over_budget`; it never returns
an unmarked partial page.

### Request shape

```json
{
  "schema": {"namespace": "ozagia.model_api.request", "version": "1.0"},
  "request_version": "1.0",
  "map": "graph",
  "scope": {
    "root": "/abs/checkout",
    "repo": "repo:acme/demo",
    "checkout": "checkout:acme/demo@0123456789ab",
    "snapshot": "snapshot:acme/demo@0123456789ab@<hash>",
    "session": "session-label",
    "model": "model-label",
    "paths": ["src/pkg"]
  },
  "target": {"path": "pkg/core.py"},
  "budgets": {"max_bytes": 65536, "max_items": 25, "max_depth": 2},
  "cursor": "v1:25:0123456789ab",
  "trace_id": "trace-label",
  "format": "structured",
  "strict": false
}
```

`scope.root` is only checked against the host policy; it never selects a
different root. Absolute paths and `..` are refused in request paths. A
request may narrow a path scope but not widen it. `format` is `structured`
(v1 JSON) or `plain` (L0-style text). `strict=true` turns an unavailable
source or a representation fallback into a typed error instead of a fallback.

Targets by map:

- `graph`: `key` (local graph key), `path` (repository-relative file/directory
  path), `type` (node type). With no target the page is a stable type-ordered
  snapshot slice.
- `outline`: `target` or `targets` (at most 8), never both.
- `capabilities`: optional canonical `capability` id.
- `work`: required `goal` (bare `g1` is accepted and qualified), optional
  `runs` (at most 8) and `tests` (at most 16).

### Response shape

An `ok` response carries `map`, `status` (`ok`/`degraded`), `format`,
`map_hash`, `schema_fingerprint`, the selected representation, the same
`facts` and `omissions` in every representation, the negotiation matrix,
redacted trace, cache metadata, source identity and `authority.granted=false`.
A structured representation contains the validated instance. A plain
representation contains deterministic text rendered from the same fact list.

Errors are `ok: false` with a `core/contracts.ServiceError` plus a stable
model-API `reason`. Typical mapping:

| Condition | Service code | Reason |
| --- | --- | --- |
| malformed request/field | `invalid_request` / `invalid_value` / `unknown_field` | `malformed_*` |
| unknown map type | `unsupported` | `unsupported_map` |
| different major version | `unsupported_version` | `unsupported_version` |
| root/repo/checkout/session/model mismatch | `forbidden` | `cross_scope`, `checkout_mismatch`, `model_not_allowed`, ... |
| stale snapshot reference | `stale` | `snapshot_mismatch`, `snapshot_unverifiable` |
| cursor from another source/scope | `stale` | `cursor_scope_mismatch` |
| source absent/unknown | `unavailable` / `unknown` | `source_absent`, `source_unknown` |
| page over budget | `budget_exceeded` | `map_over_budget`, `budget_over_policy` |

Exception messages are not copied into errors; an unexpected exception becomes
a bounded `internal` failure naming only the exception type.

## Host policy and trust

`HostPolicy` is constructed by the host adapter. It owns:

- the resolved repository root (a request cannot switch it);
- the primary session/model and the labels the same host explicitly allows;
- the allowed map types and path prefixes;
- whether structured output is allowed and whether the cache is enabled;
- host budget ceilings (never below the v1 defaults, never above the hard
  bounds);
- an optional host-selected snapshot path (the MCP server passes its own
  snapshot so `--dump` remains honoured).

The host's existing authorization is unchanged. A map cannot grant permission,
and a negotiation result cannot elevate a capability. Project content is
labelled `untrusted-data`; the model must treat it as data, not instructions.
Session/model labels in MCP arguments partition a cache and are **not**
authentication.

Requested checkout/snapshot identities are verified against the observed
source before a cache read or projection. A repository identity is derived
from the host policy, else the normalized Git origin; a checkout is
`checkout:<repo>@sha256(realpath)[:12]`; a snapshot uses the graph metadata
hash. If no repository identity can be derived, the snapshot state is
`unknown` rather than an invented match.

## Pagination

Cursors are opaque v1 tokens `v1:<offset>:<12-hex digest>`. The digest covers
map type, source signature, effective scope, target and budgets. Reusing a
cursor under another model/session/root/path scope, or after the source
changes, fails with `stale` / `cursor_scope_mismatch` instead of reading
another task's page. `next_cursor` is present only when more items remain; a
byte-trimmed page reports `reason="bytes"`.

## Cache metadata and invalidation

Cache entries live at:

```
<root>/.ozagia/model_api/cache/<sha256(key)>.json
```

The key includes:

```
{map, schema fingerprint, source signature, scope(root, repo, checkout,
 snapshot, paths, session, model), target, budgets, cursor, host policy hash}
```

The source signature includes the source content/hash, repository/checkout
identity and freshness inputs: graph file hash, graph metadata hash and
staleness facts for graph/outline; the catalogue/graph/observations/audit
hashes for capabilities; the evidence report hash plus project snapshot
identity for work.

A read verifies the stored key, map type, schema fingerprint, source
signature, exact scope, model, session, instance schema and instance hash. Any
mismatch is a **miss**, not a hit. A missing/corrupt/foreign entry is never
served. Therefore:

- changing the snapshot or working tree changes the key and invalidates the
  entry;
- switching model or session changes the key and invalidates the entry;
- replaying a key under another root cannot return another repository's map.

Writes are atomic (`mkstemp` in the cache directory, `fsync`, `os.replace`),
mode `0600`, pruned to at most 128 entries, and refused if `.ozagia` or the
cache directory is a symlink escaping the repository. Entries contain only
canonical JSON, the bounded map page and scope/source metadata; no prompt,
secret, raw log or model output. A cache is an optimisation, never proof that
a source is current; freshness is re-derived on every request.

No compaction event exists in this slice, so "invalidate on relevant
compaction" is not implemented; a compaction should change the snapshot
content/source hash, which invalidates naturally, or callers can disable the
cache.

## Negotiation

`negotiate` returns a matrix with one row per map capability plus the
requested representation decision, keyed by model, session, capability and
version. Values are exactly `supported`, `unsupported` or `unknown`.

- **configured** support is an explicit host statement (API argument or MCP
  adapter configuration).
- **observed** support is only a real observation supplied by the host; it is
  never inferred from a model family, brand or acknowledgement. Unknown stays
  unknown.
- `plain` is always host-renderable. `structured` is selected only when the
  host configured it; a configured `supported` with observed `unknown` is
  reported as `configured_supported_observed_unknown`, not as observed
  support.
- A fallback is explicit (`configured_unknown`, `configured_unsupported`,
  `observed_unsupported`, `policy_disallows_structured`) and returns plain
  output with the same facts and omissions. `strict=true` refuses the fallback
  instead.
- Historical L1/L2 support is **not claimed, implemented or inferred**.
  `protocol_levels.claimed` is empty; only `plain` and `structured-v1` are
  listed as implemented.

The trace contains the trace id, map/model/session labels, per-capability
support labels, representation choice, fallback reason and a budget hash. It
has no prompt, raw log, model output or secret value. A `redacted: true` marker
is explicit.

## Plain and structured equivalence

`schema.map_facts(instance)` is the only fact list. Structured output embeds
the instance and exposes that list; plain output renders exactly that list plus
the same source identities and truncation envelope. Tests compare top-level
`facts` and `omissions` across formats (including a truncated page). Plain
output adds no facts and hides none.

## MCP access

`dev_maps` is an additive read-only tool on the full MCP surface:

| Argument | Meaning |
| --- | --- |
| `map` | `graph`, `outline`, `capabilities` or `work` (default `graph`). |
| `target` | graph key/path, outline target, or capability id. |
| `goal`, `runs`, `tests` | work-map goal and optional bounded references. |
| `format` | `structured` (default) or `plain`. |
| `limit`, `cursor` | page size (default 25, max 200) and opaque cursor. |
| `session`, `model` | cache-partition labels; not authentication. |
| `trace_id`, `strict` | bounded redacted trace label and fail-instead-of-fallback. |

The tool uses the server workspace as the policy root and the server's own
snapshot path, so `--dump` continues to work. It returns only this checkout's
data and writes only `.ozagia/model_api/cache` metadata.

`dev_maps` is registered at runtime rather than in the static `_TOOLS` table.
The WP1 capability catalogue is outside this package's file ownership, so no
`cap:` declaration was added. The static self-graph audit therefore does not
list `mcp:tool:dev_maps`; this is an **explicit inventory coverage gap**, not
a claim that the tool is declared. `docs/memory_mcp.md` documents the tool.

## Explicit unknowns and deferred work

- No live model, model call, network call, daemon start, install, test run or
  target-project import is performed or tested. No observed model support is
  claimed.
- M4 structured edit/interaction proposals, host validation to
  `edit/txn.py`, and model-generated success verdicts are **not implemented**.
- Capability readiness, trust and permission are not observed by the
  capabilities map. Implementation/test/doc checks remain static.
- The graph map reads an existing snapshot; it never refreshes, reingests or
  builds one. An absent or stale snapshot is explicit.
- The work map joins recorded evidence only. It does not re-evaluate whether a
  criterion's evidence is still current, and it does not prove that a test
  passed. It does not record which interpreter ran a test.
- Outline output is signatures/headings plus a best-effort secret-pattern
  redactor. It is not a content security boundary; value-bearing formats are
  refused, but a source docstring can still contain prose.
- The cache stores bounded map pages and metadata unencrypted under the state
  directory. It is inspectable by anyone who can read that directory. There is
  no compaction hook, cross-process lock, daemon-side cache or shared cache.
- Map scope includes the absolute root path; maps are host-internal artifacts.
- No CLI surface, daemon operation, durable map record or retention policy is
  added in this slice.
- The MCP `session`/`model` labels are cache partitions, not identities.
  Multi-user or multi-workspace isolation requires the host to run separate
  servers/policies.

## Tests and checks

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q \
  tests/test_model_api.py tests/test_capabilities.py tests/test_self_graph.py \
  tests/test_evidence.py tests/test_contracts.py tests/test_graph_surface.py \
  tests/test_mcp_server.py
.venv/bin/ruff check src tests
```

`tests/test_model_api.py` covers: schema/hash round trips, plain/structured
fact and omission agreement, source identities and freshness, outline
redaction/no-body, capabilities coverage unknowns, work redaction and absent
states, malformed/oversized/unknown-version refusals, cross-root/repo/checkout/
model and stale-snapshot refusals, stale-source labelling, cache hit/miss and
source invalidation, model/session cache partitioning, foreign-scope cache
rejection, symlink escape refusal, cursor scope/source binding, byte trimming,
negotiation/fallback/strict behavior, state-directory-only writes, MCP
`dev_maps`, and tampered map/cache rejection.
