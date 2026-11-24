# Self-graph v1 contracts (M0)

Status: frozen contract layer for WP0, 2026-11-17. This document records the
rules implemented by `src/ozagia/core/contracts.py` and the generated surface
inventory in `src/ozagia/core/inventory.py`. It describes no production
surface: there is no new CLI command, MCP tool, daemon operation, graph
rewrite, target-project import, network call or model call in this change.
Fixtures live in `tests/fixtures/selfgraph/`.

## What "version 1" means

Contract version is `1.0`; a version is `major.minor`, and `"1"` is accepted
and normalized to `"1.0"`. Only major `1` is accepted: `parse_version("1.7")`
is valid, `parse_version("2.0")` raises `UnsupportedVersionError`
(`contract.unsupported_version`). Unknown non-`x-` fields raise
`UnknownFieldError`; `x-` fields are preserved extensions. Schema fingerprints
exclude extensions; instance hashes include them unless the caller declares a
specific `x-` field transport-only.

### Deprecation and compatibility policy

A reader accepts any `1.x` document whose fields it knows; a new non-`x-` field
in `1.1` is **not** readable by a strict `1.0` reader, so minor versions are not
a licence to add core fields silently. Deprecation window: one minor release; a
field is deprecated in `1.n`, still accepted and hashed, and may be removed in
`1.n+1`, updating the fingerprint, fixtures and this document. There is no
reader-side migration in v1: a next-major reader needs an explicit converter,
and unqualified graph keys are never migrated or rewritten here.

## Canonical JSON

`canonical_json` is Ozagia's canonical form, **not RFC 8785/JCS**. The value
model is exactly `dict` with `str` keys, `list`, `str`, `bool`, `None`, and
`int` in the signed 64-bit range; floats (including NaN/infinities), bytes,
tuples, sets and other types are rejected. Keys are sorted by Unicode code
point, separators are `,` and `:`, output is UTF-8 with `ensure_ascii=False`,
and there is no trailing newline. `content_hash` is SHA-256 over those bytes.

```text
canonical_json({"z": [3, {"b": 2, "a": 1}], "a": {"d": 4, "c": 3}})
  -> {"a":{"c":3,"d":4},"z":[3,{"a":1,"b":2}]}
canonical_json({"message": "héllo ☃"})
  -> {"message":"héllo ☃"}          # not \u escapes
```

Negative examples: floats, non-string keys and out-of-range ints raise
`contract.float_not_allowed`, `contract.invalid_key` and
`contract.int_out_of_range`. Two distinct keys that normalize to the same NFC
form raise `contract.duplicate_key`; keys are otherwise hashed as supplied.
Lone surrogates raise `contract.invalid_unicode`. Exact vectors, including
empty objects, scalar ordering, escapes and int64 bounds, are in
`tests/fixtures/selfgraph/canonical_vectors.json`.

## Schema and instance records

### Schema and `schema_fingerprint`

A schema record has exactly `namespace`, `version`, `types`, `required`,
`constraints` and `sensitivity` (plus `x-` extensions). `sensitivity` is one of
`public/internal/private/secret`. `required` is sorted before hashing, so
declaration order does not change identity. The fingerprint is SHA-256 over
canonical JSON of exactly `{namespace, version, types, required, constraints,
sensitivity}`. Positive: `required=["b","a"]` and `required=["a","b"]` share a fingerprint,
and an `x-owner` extension does not change it. Negative: a different
`sensitivity` or `constraints` does change it; `version="2.0"` or an extra
non-`x-` field raises.

### Instance and `instance_hash`

An instance record has `schema` (`{namespace, version}`), `scope`, `snapshot`,
`inputs` (list of strings), `provenance`, `data`, and optionally `omissions`,
`created_at`, `cursor`, `trace_id`; `x-` extensions are allowed. Size and the
content hash are computed beside the instance, never inside it, so the hash
cannot include itself.

`instance_hash` excludes `created_at`, `cursor` and `trace_id` always, plus any
`x-` field named in `transport_only`. Positive: changing `created_at` does not
change the hash. Positive: adding `x-build="ci-42"` changes the hash unless
`transport_only=("x-build",)`. Negative: `transport_only=("data",)` raises so a
caller cannot hide core data; an unknown non-`x-` field raises.

## Identities

IDs are `kind:body`; kinds are `cap`, `impl`, `iface`, `run`, `goal`, `repo`,
`checkout`, `snapshot`, `project`. Components are lowercase ASCII, 1–128
characters, start with `[a-z0-9]`, and otherwise contain `[a-z0-9._-]`. A
literal `/`, `:`, `@` or `%` inside a component is escaped as `%2F`, `%3A`,
`%40` or `%25`; escapes use uppercase hex and must be canonical (`%61` for `a`
is an error). Rendered IDs are at most 512 characters; local project keys are
at most 256.

| Form | Rule |
| --- | --- |
| `cap:graph.find`, `impl:...`, `iface:...` | dotted lowercase name |
| `run:<id>`, `goal:<id>` | one escaped opaque component |
| `repo:<owner>/<name>` | exactly two components; owner may contain an escaped slash |
| `checkout:<repo>@<sha256(realpath)[:12]>` | 12 lowercase hex |
| `snapshot:<checkout>@<graph meta hash>` | 12–64 lowercase hex |
| `project:<repo>@<checkout>:<snapshot>/<local key>` | projection only; key verbatim |

Repository identity comes from the normalized remote, not a local path.
`repo_id_from_origin` accepts `https`/`http`/`ssh`/`git`/`git+ssh` URLs and
scp-like `git@host:owner/name`; it ignores host/user, lowercases the path,
removes one trailing `.git`, and keeps nested groups in the owner component:
`ssh://git@host/group/sub/repo.git` -> `repo:group%2Fsub/repo`. Local paths and
`file:` URLs are rejected as unstable. A move changes the checkout ID, not the
repo ID, and is reported as a move, not a match.

`qualify(repo, checkout, snapshot, key)` validates that the checkout belongs to
the repo and the snapshot to the checkout, then prefixes the key. Positive:
`qualify(..., "sym:src/a.py::X")` ends in `/sym:src/a.py::X`. Negative: a
checkout from another repo raises. Parsing a `project:` ID validates syntax
only: it cannot prove that a checkout hash belongs to the named repo. Only
`qualify` (or the full checkout/snapshot IDs) checks ownership, so a parsed
project ID must never be treated as authorization. `assert_unique_ids` raises
`IDCollisionError` on duplicate canonical identities. Initial graph keys
(`file:`, `sym:`, `test:`, `doc:`, `text:`, `commit:`, `failure:`, unqualified
`repo:`, adapter keys) are unchanged; `repo:` as a key and as an identity are
distinct namespaces.

## Vocabulary, readiness and clocks

| Concept | Values |
| --- | --- |
| freshness | `current`, `stale`, `unknown`, `absent` |
| evidence | `declared`, `inferred`, `observed`, `verified` |
| result | `declared`, `pending`, `passed`, `failed`, `unknown`, `invalid`, `not_assessed` |
| readiness dimension | `installed`, `configured`, `reachable`, `trusted`, `permitted` |
| readiness value | `true`, `false`, `"unknown"` |

`"unknown"` is not `false` and not a missing key. `readiness_map` returns all
five dimensions, filling missing ones with `"unknown"`.

`join_freshness(parent, child)` uses dominance
`absent > unknown > stale > current` and never upgrades a child:
`join_freshness("stale", "current") == "stale"`; a current child of a stale
parent is not current. Clocks are injected: `Fact`/`Relation` require an
explicit `observed_at`, and the contract layer never reads a clock. `clock`
validates `system`, `monotonic`, `injected` or `unknown`. Durations use
monotonic deltas; wall clock is only for cross-process ordering, and a
timestamp difference alone never proves an edge. The trace record is WP3.

## Facts, relations, errors and truncation

`Fact` carries subject, value, source, optional owner, observed time, optional
subject version, freshness, evidence and sorted scope. `Relation` additionally
carries type, `from`/`to` endpoints and endpoint versions. Both serialize
canonically and bound values to 8 KiB, depth 8, 512 nodes and 32 scope entries.
Negative: a 9 KiB value or depth 9 raises `BoundsError`; bad vocabulary raises.

`ServiceError` uses a closed code vocabulary (`invalid_request`, `not_found`,
`unavailable`, `unauthorized`, `forbidden`, `conflict`, `stale`, `unknown`,
`timeout`, `cancelled`, `budget_exceeded`, `truncated`, `unsupported`,
`invalid_state`, `precondition_failed`, `internal`, and the listed input
codes), one bounded message line and bounded canonical details. Free-text
codes are rejected.

The omission envelope is exactly `truncated`, `omitted`, `next_cursor`,
`reason`. Untruncated means `omitted=0`, no cursor and no reason. Truncated
requires a reason from `limit`, `bytes`, `depth`, `budget`, `scope`,
`unavailable` or `filtered`; a cursor is at most 256 characters. Negative:
`truncated=true` without a reason raises; `truncated=false, omitted=1` raises.

## Budgets and registry truth

Frozen defaults for the future map projector: 64 KiB JSON, 200 nodes, depth 2.
Prefetch defaults: 4 concurrent tasks, 2 s CPU and 8 MiB IO, cancellable. The
hard upper bound a caller may raise a map to is **not** frozen here (open
decision). The explicit built-in capability catalogue in `core/capabilities.py`
will be authoritative for declared capabilities in WP1; CLI/MCP/daemon/
toolchain/status are observed surfaces joined read-only. Unknown coverage is a
first-class value. A deleted run or worktree becomes `absent` with a reason;
it is never silently dropped or treated as current. Live-eval thresholds are
deferred to M6.

## Generated surface inventory

`collect_inventory(root=None)` returns a sorted report with schema
`ozagia.self_graph.inventory@1.0`, tool version/source, surfaces, gaps,
coverage and counts. `inventory_hash(report)` is `contracts.content_hash`.
When `root` is supplied, its project configuration is parsed read-only and
each declared adapter reference becomes an `adapter:declared:<module:attr>`
surface (`declared=true`, `observed=false`, `coverage="unknown"`,
`imported=false`); the modules are never imported. With no root, no declared
adapters, or an unreadable/invalid config, the blanket `adapter:registry`
unknown surface is emitted; a config error also adds an `adapter:config`
unverified gap. `details.source` is `ProjectConfig.source` (the active config
path), falling back to `ProjectConfig.adapter_base` and then the resolved root.
An injected adapter observation is a caller-supplied runtime view and takes
precedence over declarations; the two are never merged.

It introspects, without side effects: `pyproject.toml` scripts and entry
points (declared, not imported); `build_parser()` walked without running a
command; the MCP surfaces — full `_TOOLS` + `DevMemoryServer._handlers`, graph
`GRAPH_TOOLS` + `GraphSurface.handlers`, and engine `ENGINE_TOOLS` +
`GraphEngine.handle` — read from AST without instantiating a server, graph
surface, engine or snapshot; daemon `op` names parsed
from the docstring with the module's `PROTOCOL_VERSION`; and
`settings.schema()`, a read-only registry.

Each surface records `id`, `kind` (`cli`/`mcp`/`daemon`/`entrypoint`/
`settings`/`adapter`), `name`, `source`, `version`, `declared`, `observed` and
`coverage`. CLI and settings entries use `details.observation="runtime"` and
`coverage="observed"`. Full MCP entries use `mcp:tool:<name>`, graph entries
use `mcp:graph:<name>`, and the engine entry uses `mcp:tool:graph`; they carry
`details.surface` (`full`/`graph`/`engine`) and
`details.observation="static-ast"`, and are `coverage="unknown"` because the
runtime map depends on mode and surface selection. Daemon entries use the
protocol version (currently `"1"`). `gaps.declared_not_observed` and
`gaps.observed_not_declared` report contradictory or missing surfaces;
`gaps.unverified` records declared surfaces that could not be cross-checked
(daemon operations, entry points, adapters and adapter load errors). The report
always has `coverage.exhaustive: false`; it never claims completeness. Output
is identical across repeated runs and `PYTHONHASHSEED` values: all maps and
lists are sorted, no clock or random value enters the report, and the inventory
describes the running Ozagia package.

Limits: both MCP maps are static AST views, not runtime maps after conditional
loaders; daemon operations are declarations; entry points are not imported;
adapter declarations are parsed read-only and never imported. Coverage is
therefore `unknown`, not a claim of absence.

## Historical protocol concepts

| Concept | Decision | Why / v1 boundary |
| --- | --- | --- |
| Maps, GRAPH, OUTLINE, INTERACT | Adapted later | v1 freezes canonical JSON, scoped instances and transport fields; the bounded read-only projector and expand/lookup requests are M3, not built here. |
| EDIT, TRANSACTION | Adapted later | One structured proposal will feed `edit/txn.py`; base hash, parse guards, preview and authorization stay authoritative. No new patch fallback. |
| WORK-UNIT, PLAN, TASK, CLAIM | Adapted selectively | Goals, tasks, jobs and evidence stay the owners; a model assertion is not an executed test or proof. |
| Capability registry and handshake | Adopted in shape | Host-owned declared capabilities plus separately observed readiness; no model acknowledgement can elevate access or prove competence. |
| L0/L1/L2 and teaching | Deferred | Plain and structured adapters come before teaching; native support is never inferred from a model family. |
| MEMORY, cached references, deltas | Adapted later | Scoped map caching with content hashes and invalidation; references outside the scope require re-fetch. Durable records and protocol caches stay separate. |
| CONTROL/COMMUNICATE, IFC | Adapted as labels only | Origin/trust labels are preserved, but labels do not isolate a probabilistic model; authority stays in the host. |
| WIRE/dialects, L4, routing, specialist dispatch | Deferred | No evidence yet that a compressed wire or routing layer improves verified outcomes; canonical forms and evals come first. |
| Ocelot / formal claims | Deferred to an adapter | The evidence spine must exist first; verifier, premises and result must be explicit, and formal proof stays distinct from recorded or executed evidence. |

## Not yet implemented

No capability registry or self-graph view, map projector, model-side API,
cache/negotiation/budget enforcement, service envelope, preflight, evidence
store or task profile; no daemon cross-check, inventory MCP/CLI surface,
snapshot rewrite, target import, network probe, model call, install or daemon
start.

## Open decisions

1. **Hard map bound.** Defaults are frozen; the caller-raisable bytes/nodes/
   depth hard limit is not. WP5 must set it and test refusal.
2. **Host collisions.** `repo:<owner>/<name>` ignores the origin host, as the
   M0 table proposed, so `github.com/acme/x` and `gitlab.com/acme/x` collide;
   decide whether v2 adds a host component or a registry mapping.
3. **Nested-group casing.** Origin normalization lowercases the path; decide
   whether case-sensitive hosts need a preserve-case policy.
4. **Graph meta hash.** Snapshot IDs accept 12–64 hex; fix the width/algorithm.
5. **Project key length.** 256 characters is frozen; confirm adapter keys fit.
6. **Daemon dispatch.** The op list is parsed from the docstring; cross-checking
   the server's dispatch table without a daemon is deferred to WP1.
7. **Entry points.** Targets are declared but not imported; decide whether WP1
   verifies importability in a subprocess-isolated check.
