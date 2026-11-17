# Model-facing maps (M3) and structured edit/interaction (M4)

Status: implemented subsets of M3 and M4, 2026-11-18, in
`src/ozagia/model_api/`. The M3 map boundary is **read-only**: a map is a
bounded view over owners that already exist. It is not a new graph store, a
transport, a permission, a model call or a protocol authority. M4 adds one
bounded structured edit proposal and one more-context request; the edit is
previewed first and only reaches the existing transaction after host
authorization, while the interaction is still only a bounded map read.

The package is standard-library only. It does not import a target project,
refresh a snapshot, run a test, start a daemon, install anything, call a model
or write outside `<root>/.ozagia/` except the single explicit authorized edit
(the target file, with its backup under `<root>/.ozagia/edits/backups/`).
Other state writes are bounded cache and action metadata under
`<root>/.ozagia/model_api/`.

## What is implemented

| Module | Responsibility |
| --- | --- |
| `schema.py` | v1 request/map schemas, instance validation, canonical hashes, page cursors, fact projections and the plain renderer. |
| `policy.py` | Host-owned root/session/model/map/path/budget checks. |
| `project.py` | Bounded projectors for `graph`, `outline`, `capabilities` and `work`. |
| `cache.py` | Inspectable cache keys and entries under the repository state directory. |
| `negotiate.py` | Deterministic capability/support matrix, representation choice, explicit fallback and redacted trace. |
| `errors.py` | Typed service errors plus stable model-API reasons. |
| `edit.py` | One M4 `replace_body` proposal, deterministic parsing, host policy and base checks, preview/apply through the existing Python edit transaction, idempotency ledger and the read-only evidence join. |
| `interact.py` | One M4 more-context request translated to an existing map request; no authority and no scope widening. |

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

`dev_maps` is declared in the static `_TOOLS` table and in the WP1 catalogue as
`cap:graph.model_maps`, so `ozagia capabilities` and the self-graph audit list
`mcp:tool:dev_maps` like any other built-in surface. The handler is loaded only
on the coding/combined full surface; the graph surface and research mode do not
expose it. For `dev_maps`, the map cache is the only write, under
`.ozagia/model_api/cache`.

## M4: structured edit and interaction

M4 is a narrow layer over the existing edit transaction and the M3 maps. It
adds no new edit engine, no patch fallback, no command runner, no model call
and no authority.

### Edit proposal

```json
{
  "schema": {"namespace": "ozagia.model_api.edit_proposal", "version": "1.0"},
  "proposal_version": "1.0",
  "action_id": "act-0001",
  "node": {"kind": "symbol", "key": "sym:pkg/core.py::add"},
  "operation": "replace_body",
  "body": "return a + b\n",
  "base_sha256": "<64 lowercase hex>",
  "snapshot": "snapshot:acme/demo@0123456789ab@<graph hash>",
  "scope": {"root": "/abs/checkout", "session": "s1", "model": "m1",
            "repo": "repo:acme/demo",
            "checkout": "checkout:acme/demo@0123456789ab"},
  "checks": ["test:tests/test_core.py::test_add"],
  "claims": [{"text": "the add helper still behaves the same"}]
}
```

- The only operation is `replace_body` on a canonical Python symbol node
  `sym:<repo-relative .py>::<qualname>`. The target file and qualname are
  derived from the node key; there is no separate line range, file, diff,
  patch, hunk, command, adapter engine or second operation.
- `body` is function/class body source. It is never evaluated, imported or
  executed. A model-supplied `command` or other unknown field is refused.
- `base_sha256` is over the file's raw bytes (BOM included). A preview is
  refused when it no longer matches.
- `snapshot` and `checkout` are optional. If present they must match the
  host-supplied expected identity; a host that cannot verify one refuses the
  proposal rather than inventing a match.
- `scope` may match or narrow the host policy. It cannot switch the root,
  session, model, repo or allowed path, and a proposal does not get to add
  `paths`. `session`/`model` labels are not authentication.
- `checks` are selected `test:` keys for the evidence join; M4 never runs
  them. `claims` are bounded free text kept as `declared` data. Neither field
  grants permission or changes the operation.

Bounds are hard, not caller-raisable:

| Value | Bound |
| --- | --- |
| proposal JSON | 64 KiB canonical UTF-8 |
| proposal depth / nodes | 8 / 512 |
| body | 16 KiB UTF-8 |
| action id | 128 characters, `[A-Za-z0-9][A-Za-z0-9_.:@-]*` |
| checks / claims | 16 / 8 |
| claim text | 300 characters |
| selected target file | existing adapter limit, 2 MiB |

Unknown non-`x-` fields, duplicate JSON keys, floats, NaN/infinity, a BOM,
control characters, lone CRs, absolute paths and `..` components fail closed.
An `x-` extension is preserved in the proposal hash but is never interpreted
as an operation, command or authority.

Typed failures use the shared service codes plus one stable reason:

| Condition | Service code | Reason |
| --- | --- | --- |
| malformed JSON/shape/action/body/base/snapshot/scope | `invalid_request` / `invalid_value` / `unknown_field` | `malformed_proposal`, `proposal_schema_mismatch` |
| wrong schema or proposal version | `unsupported_version` | `unsupported_version` |
| non-symbol node, non-Python file, unsupported operation | `unsupported` | `unsupported_operation` |
| non-canonical symbol key/kind | `invalid_request` | `node_mismatch` |
| absolute path, `..`, state-dir symlink escape | `forbidden` | `path_escape` |
| payload/body/check/claim-list over bound | `budget_exceeded` | `proposal_over_budget` |
| file bytes no longer match `base_sha256` | `stale` | `changed_base` |
| root/repo/path outside policy | `forbidden` | `cross_scope` |
| session/model not allowed | `forbidden` | `session_not_allowed`, `model_not_allowed` |
| snapshot/checkout absent or mismatched | `unknown` / `stale` | `scope_unverifiable`, `snapshot_mismatch`, `checkout_mismatch` |
| symbol/file not found | `not_found` | `target_not_found` |
| edited source would not parse | `invalid_value` | `parse_failed` |

### Preview, authorization and the existing transaction

`handle_edit(proposal, policy, ...)` previews by default. A preview reads the
file through `ozagia.edit.txn.read_source`, checks the base hash, and calls the
built-in Python adapter (`ozagia.edit.code_edit.apply_edit_script`) with
`write=False` and exactly one `replace_body` op. It writes no file and creates
no backup. The response carries the node, operation, base/new hashes, bounded
diff view, selected checks, claims, effective scope and `authority.granted=false`.

The preview also returns a `preview_hash` over the proposal hash, host root,
session/model, repo/checkout/snapshot, file, node, operation, base hash and body
hash. `apply=True` requires:

1. a host-constructed `EditAuthorization(granted=True, actor=..., reason=...)`;
2. the exact `preview_hash`; and
3. the current file bytes still matching `base_sha256`.

Missing authorization is `forbidden` / `write_declined`; a missing or different
preview is `preview_required` or `stale` / `preview_mismatch`. The apply then
re-reads the file and calls the same adapter with `write=True`. That path keeps
the existing `edit/txn.py` semantics: backup under
`<root>/.ozagia/edits/backups/`, temp file in the target directory, `fsync`,
a re-read base check immediately before `os.replace`, and an atomic write. M4
does not change those semantics and has no text-patch fallback. It also refuses
to write when the state directory or the edit backup directory is a symlink
escaping the root.

An applied response still says `authority.granted=false`; its
`transaction.authorized=true` records that the *host* authorized the write, not
that a model or proposal has permission. This layer does not add a permission
prompt, MCP tool, daemon operation or CLI command. A host adapter must construct
`EditAuthorization` from its existing edit permissions.

### Action idempotency

A successful apply is recorded under
`<root>/.ozagia/model_api/actions/<sha256(action_id)>.json`. The entry stores
the action/proposal/preview hashes, file/node/operation, base/new hashes,
backup path, caller-injected `applied_at`, authorization actor/reason, selected
checks, claims and scope. It does **not** store the body or the diff.

Replaying the same `action_id` with the same proposal, preview and effective
scope returns the recorded outcome (`replayed=true`, `applied=false`) without a
second write or backup. Reusing the id for a different proposal or effective
scope is `conflict` / `action_conflict`. A failed apply is not recorded as
applied, so a retry after the base is restored or a new preview is legitimate. Entries are canonical
JSON, mode `0600`, capped at 128 files/64 KiB each and pruned oldest-first.
There is no cross-process lock; concurrent applies are last-writer-wins at the
ledger, while the transaction's own base check protects the file. A ledger
write failure after a successful edit is reported as an applied action with
`ledger.state="failed"` and a warning; it is not hidden.

### Action evidence

`action_evidence(action, root=..., goal=..., checks=..., sources=...,
observed_at=...)` joins the applied action and its selected test keys to
`ozagia.core.evidence.collect_evidence`. It reads the goal store, run
manifests, the working-set test record and interpreter candidates. It does not
run a test, execute a command, refresh the graph, write a goal/task/evidence
record or invent an interpreter. It returns the evidence report plus a bounded
action/check/claim section and a local assessment.

- A check result is taken from the recorded test outcome. A `passed`/`failed`
  result counts for this action only when the record's time (epoch
  milliseconds) is at or after the caller-injected `applied_at`; otherwise it
  is `unknown` with `stale`/`unknown` freshness. A test `error` is `unknown`,
  not a failed assertion. A missing row is `not_assessed`.
- Claims always stay `result=declared`, `freshness=unknown`,
  `evidence=declared`, `authority=none`. They never make an assessment pass.
- The local assessment is `not_assessed` for a preview or an edit with no
  checks, `declared` when only claims exist, `failed` when an observed selected
  check failed, `unknown` when a check is unknown/stale/unlinked or the file no
  longer matches the recorded new hash, and `passed` only when the action was
  applied/replayed, every selected check passed with current observed evidence,
  and the file is still at the recorded hash.
- `passed` here is a bounded evidence status, not proof that the edited bytes
  were executed: the test record does not record the bytes or interpreter it
  ran. The response says so in `not_assessed`. Goal-level green remains the
  job of `ozagia.core.task_preflight`; the M4 fixture refreshes the graph,
  records a post-action test result, and then builds a preflight report with
  deterministic fake providers.
- `handle_edit(..., evidence_goal=...)` attaches the same evidence block to an
  applied response. An evidence-collection failure is attached as a typed
  error without turning the already-applied edit into a failure.

### Interaction request

```json
{
  "schema": {"namespace": "ozagia.model_api.interact", "version": "1.0"},
  "request_version": "1.0",
  "request_id": "ctx-0001",
  "reason": "need the callers of add",
  "map": "graph",
  "scope": {"root": "/abs/checkout", "session": "s1", "model": "m1",
            "repo": "repo:acme/demo"},
  "target": {"key": "sym:pkg/core.py::add"},
  "budgets": {"max_items": 5, "max_depth": 2},
  "format": "structured"
}
```

`parse_interact_request` bounds the request to 32 KiB, depth 6 and 256 nodes,
refuses duplicate keys/floats/unknown fields and translates it into one v1
`schema.make_request`. `request_interact` then uses the ordinary
`request_map` path, so the same host policy checks apply: one of the host's
allowed map types, one target/page cursor, the same root/session/model, path
prefixes, budgets and snapshot verification. It cannot request a new
projector, several maps in one call, a command, a mutation or a wider scope.
The response is the ordinary map response plus `kind="interact"` and an
`interaction` block with the request id, bounded reason, map, target, cursor
and `authority.granted=false`. A malformed or refused interaction returns the
normal typed error with a plain-tool fallback; it never becomes an empty
successful map.

## Explicit unknowns and deferred work

- No live model, model call, network call, daemon start, install, test run or
  target-project import is performed or tested. No observed model support is
  claimed.
- M4 supports exactly one Python operation (`replace_body`) on one canonical
  symbol in one `.py` file. There is no rename/delete/import/insert/span
  operation, no other language, no multi-file proposal, no adapter edit engine
  and no raw diff/patch fallback.
- M4 has no MCP tool, CLI command, daemon operation or permission prompt of its
  own. The host adapter must construct `EditAuthorization` from its existing
  edit policy; this doc does not claim that the MCP or daemon surfaces are
  already wired to it.
- M4 verifies the symbol target against the canonical node key and the
  working file. It does not prove that the symbol existed in the named graph
  snapshot; snapshot identity is checked only against a host-supplied expected
  identity, and the graph is not refreshed.
- A preview returns a bounded (16 KiB) unified diff of the target file so the
  host and model can review the change. That diff is project content; M4 does
  not apply an additional secret redactor to source lines. A host with stricter
  content rules should omit or further redact `diff_view`.
- The action ledger is a local idempotency cache, not durable evidence. It has
  no cross-process lock, retention beyond 128 entries, crash recovery or
  encryption; it stores metadata and hashes, never the body.
- `action_evidence` does not execute a check. A recorded test result is not
  proof that the edited bytes or the selected interpreter were used, and a
  model claim is never a verdict. Goal green remains a `task_preflight`
  assessment, not an M4 success claim.
- Capability readiness, trust and permission are not observed by the
  capabilities map. Implementation/test/doc checks remain static.
- The graph map reads an existing snapshot; it never refreshes, reingests or
  builds one. An absent or stale snapshot is explicit. M4 does not refresh the
  graph after an edit; the host must do that separately before relying on a
  new symbol/test node.
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
  added for maps in this slice.
- The MCP `session`/`model` labels are cache partitions, not identities.
  Multi-user or multi-workspace isolation requires the host to run separate
  servers/policies.

## Tests and checks

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q \
  tests/test_model_api_edit.py tests/test_model_api.py \
  tests/test_capabilities.py tests/test_self_graph.py \
  tests/test_evidence.py tests/test_contracts.py tests/test_graph_surface.py \
  tests/test_mcp_server.py
.venv/bin/ruff check src tests
```

`tests/test_model_api_edit.py` covers: deterministic parse/hash round trips,
preview/apply through the existing transaction, backup and state-directory
containment, stale base, declined and missing authorization, preview
binding/mismatch, a concurrent-change partial failure and retry, duplicate-id
idempotency and conflicting action ids, snapshot/checkout/root/scope
mismatches, wrong versions, unsupported operations and node kinds, path
escapes, oversized/depth/duplicate-key payloads, unparseable bodies, no
command execution, a state-directory symlink escape, claims-only and
unavailable/stale checks, action-ledger metadata, and the end-to-end fake-model
flow (proposal → preview → authorized transaction → graph refresh → captured
test record → evidence join → task-preflight goal assessment).

`tests/test_model_api.py` covers: schema/hash round trips, plain/structured
fact and omission agreement, source identities and freshness, outline
redaction/no-body, capabilities coverage unknowns, work redaction and absent
states, malformed/oversized/unknown-version refusals, cross-root/repo/checkout/
model and stale-snapshot refusals, stale-source labelling, cache hit/miss and
source invalidation, model/session cache partitioning, foreign-scope cache
rejection, symlink escape refusal, cursor scope/source binding, byte trimming,
negotiation/fallback/strict behavior, state-directory-only writes, MCP
`dev_maps`, and tampered map/cache rejection.
