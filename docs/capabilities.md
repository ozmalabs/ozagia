# Capabilities and the self-graph (M1)

Status: WP1 implementation, 2026-11-17. This is a read-only inspection
surface. There is no new graph store, no daemon operation, no snapshot rewrite,
no target-project import, no network call, no model call, no install and no
probe that executes target code. It implements the M1 capability registry and
self-graph view that `docs/self_graph_contracts.md` listed as not yet present;
the frozen v1 contract layer itself is unchanged.

## What the catalogue is

`src/ozagia/core/capabilities.py` is the **explicit, authoritative declaration**
of what Ozagia says it can do. It is a hand-written, versioned data table, not a
scan of importable code: a scan cannot distinguish a supported capability from
an internal helper and silently turns an omission into an absence. The module is
pure and standard-library only; building it does not read the environment,
start a process or write a file.

Every record carries at least:

| Field | Meaning |
| --- | --- |
| `id` | A `cap:` identity validated by `core/contracts.py`. |
| `domain` | One of the fourteen domains below. |
| `purpose` | One bounded sentence. |
| `owner` | The module that owns the behaviour, e.g. `ozagia.core.memory`. |
| `side_effect` | `none`, `read`, `write`, `execute`, `network` or `model`. |
| `exposure` | A non-empty subset of `cli`, `mcp`, `daemon`, `internal`. `internal` is exclusive. |
| `impl` | One or more `impl:` identities; the body is a dotted module path with an optional symbol. |
| `surfaces` | Observed surface ids, exactly as `core/inventory.py` names them. |
| `prerequisites` | Declarative prerequisites, never probes. |
| `limits` | Documented limits. |
| `authorization` | The existing gate or the statement that the operation is read-only. |
| `tests` / `docs` | `test:` / `doc:` local graph-key references. |

Domains: `graph`, `edit`, `test`, `goals`, `tasks`, `environment`, `resources`,
`toolchain`, `status`, `federation`, `research`, `runtime`, `actions`, `eval`.

`cap:` and `impl:` ids are contract identities: they are parsed and
canonicalised by `core/contracts.py`. `test:` and `doc:` are deliberately **not**
contract ids: the v1 contract layer reserves them as unqualified local graph
keys, so the catalogue only validates them as relative repository paths with an
explicit `tests/` or `docs/` prefix. A catalogue record cannot claim the
readiness or permission of a capability; those are separate observations and
are not implemented here.

A CLI declaration covers its dotted descendants: `cli:ozagia.goal` covers
`cli:ozagia.goal.add`, `cli:ozagia.goal.list`, and so on. A trailing `:*`
family declaration is accepted for future adapter surfaces. MCP, daemon,
toolchain and status declarations are exact.

## Surface join

`src/ozagia/core/self_graph.py` joins the catalogue to observations. It reads,
without side effects:

* the generated inventory: CLI parser walk, both MCP tool lists and handler maps
  (read by AST, no server instantiated), the daemon protocol docstring,
  settings registry, entry points and read-only adapter declarations;
* the static toolchain registry (`core.toolchain.registry()`, data only);
* the section names emitted by `core.status.status` (source AST only; the
  function is not called because it may probe the daemon);
* `ozagia.daemon.protocol.OPERATIONS`, cross-checked against the documented
  daemon operations.

The projection has nodes and edges:

| Edge | From -> to | Meaning |
| --- | --- | --- |
| `implements` | capability -> implementation | declared implementation |
| `exposes` | capability -> surface | declared user/daemon surface |
| `backs` | implementation -> surface | inferred implementation relation |
| `requires` | capability or surface -> dependency | declared prerequisite |
| `verified_by` | capability -> test | declared test reference |
| `documented_by` | capability or implementation -> doc | declared documentation |
| `tested_by` | implementation -> test | inferred implementation/test relation |

Every node and edge has a state: `observed`, `declared`, `missing`,
`undeclared`, `unknown` or `absent`. Edges also carry freshness and evidence
using the v1 vocabulary, a sorted `via` list of capabilities and provenance
records (source, method, capability, observation). The graph body is hashed with
`contracts.content_hash`; the full projection is deterministic and contains no
clock.

An implementation id is resolved statically: the longest module prefix becomes
a source file, and the remaining symbol must be a top-level function/class or a
class method. A module-only id is observed when its file exists. Modules are
never imported, so a missing optional extra cannot fail the projection.

A `test:` or `doc:` reference is `observed` when the relative path exists in
the Ozagia source tree, `missing` when it does not, and `unknown` when Ozagia is
installed without a source tree.

## Audit semantics

The audit reports both directions required by M1:

* **Declared but not observed** — a capability declares a surface that the
  observed surface set does not contain. This is the missing documented handler
  direction (`declared_surface_not_observed`).
* **Observed but not declared** — a surface exists in the runtime/static
  inventory and no capability declares it
  (`observed_surface_undeclared`), with the inventory source, kind and coverage
  in its provenance.

Each finding carries `kind`, `severity`, `state`, `subject`, `reason` and
`provenance`. Duplicate findings are merged and their provenance lists every
affected capability.

Partial and error states survive:

* an inventory or emitter error produces an `observation_unavailable` finding
  and audit state `partial`; it never crashes the projection;
* an unreadable MCP handler map is `unknown`, never `missing`; the same applies
  to an unreadable graph handler map, daemon operation list, toolchain registry
  or status source;
* a declared surface in a family whose observation is unknown becomes
  `declared_surface_unknown`;
* a daemon operation that is dispatched in `server.py` but omitted from the
  protocol docstring is synthesized from `protocol.OPERATIONS` and reported as
  the coverage finding `daemon_operation_undocumented`, not as a missing
  capability. A documented operation absent from `OPERATIONS` is an error.

Audit state is `current` when the observations could be made, `partial` when an
emitter or a finding is unknown, and `unknown` when every emitter is unknown.
Findings do not change the process exit status: the report was produced, so the
command returns `0`.

### Scope and exclusions

`mcp`, `daemon`, `cli`, `toolchain` and `status` surfaces are audited for
declarations. `settings`, `entrypoint` and `adapter` surfaces are counted but not
treated as capability declarations: configuration keys and packaging targets
are not capabilities, and adapters are project-supplied. CLI namespace parents
(a parser with subcommands) are counted but not reported as undeclared leaves.
`coverage.exhaustive` is always `false`.

## Commands

```sh
ozagia capabilities list  [--json] [--limit N] [--cursor C]
ozagia capabilities show  ID [--json] [--limit N] [--cursor C]
ozagia capabilities audit [--json] [--limit N] [--cursor C]
```

Examples:

```sh
ozagia capabilities list --limit 10
ozagia capabilities show cap:graph.context
ozagia capabilities audit --json
ozagia capabilities audit --json > /tmp/a.json
ozagia capabilities audit --json > /tmp/b.json
cmp /tmp/a.json /tmp/b.json
```

`--limit` defaults to 25 and is capped at 200. `--cursor` is the decimal offset
printed in `truncation.next_cursor`; a truncated page reports `truncated`,
`omitted`, `next_cursor` and `reason` using the v1 truncation envelope. `list`
pages capabilities; `show` pages the edges that mention one capability; `audit`
pages findings. `show` needs a canonical `cap:` id and returns `2` for an
unknown id; malformed limits and cursors also return `2`. No command refreshes
the graph, starts a daemon or writes anything.

The MCP tool `dev_capabilities` exposes the same three actions with the same
arguments (`action`, `id`, `limit`, `cursor`). It is added to the full coding
surface (not the graph surface), is read-only, bounded, and never starts a
daemon or imports target code. Its row is in `docs/memory_mcp.md`.

`ozagia.daemon.protocol.OPERATIONS` is a declarative, alphabetically sorted
tuple of the existing dispatch op strings: `after`, `goals`, `hello`, `hint`,
`job_events`, `job_list`, `job_log`, `job_ps`, `job_start`, `job_stop`,
`job_wait`, `owner_check`, `path_check`, `ping`, `profile_event`, `read_check`,
`secrets`, `session_event`, `shutdown`, `stats`, `structural`, `tool`,
`toolchain`, `who`, `working_set`. It adds no operation and changes no
dispatch behaviour; tests compare it with the `op` branches in
`daemon/server.py` and `jobs.OPS`.

## Determinism and bounds

The catalogue is sorted by canonical id; inventory and supplemental surfaces
are sorted by kind and id; nodes are sorted by kind and id; edges by type,
source and target; findings by kind, subject and reason. No clock, random value
or PID enters the report. `catalogue_hash`, `graph.hash`, `audit.hash` and the
observation `hash` are SHA-256 over canonical JSON. Identical inputs produce
identical bytes across runs and `PYTHONHASHSEED` values.

The full projection is capped at 2,000 nodes and 4,000 edges (roughly 500
nodes and 1,200 edges at the time of writing), with a truncation envelope if
either cap is exceeded. The CLI/MCP views never return the full graph in
`audit`; they return summary counts and a page of findings. `show` returns the
edges for one capability, paged.

## Explicit unknowns and coverage gaps

* **Readiness is not observed.** The catalogue says what is declared and which
  surfaces exist; it does not claim `installed`, `configured`, `reachable`,
  `trusted` or `permitted` for any capability. Those stay with the existing
  owners (`core/status.py`, `core/toolchain.py`, `core/environments.py`).
* **MCP maps are static AST views.** The runtime map depends on mode, surface
  selection, adapter tools, SRE tools and dynamically loaded research tools.
  Adapter tools are reported as unknown coverage/out of declaration scope;
  research tools are internal in the catalogue and named as a documented
  unknown rather than guessed from `_TOOLS`.
* **Daemon operations are code-level declarations, not a live probe.** The
  audit does not start or ping a daemon. The protocol docstring currently
  omits `hint`, `secrets` and `toolchain`; those are synthesized from
  `OPERATIONS` and reported as `daemon_operation_undocumented` coverage
  findings.
* **Implementation checks are static.** A symbol present in the source AST is
  evidence that the module defines it, not that the runtime path is correct,
  reachable or safe. Dynamic dispatch, decorators and adapter-provided
  implementations are outside this check.
* **Test/doc checks are path existence only.** A referenced test file is not
  run; a referenced document is not parsed. A reference proves a link was
  declared, not that the test passes or the text still describes the code.
* **Settings, entry points and adapters are excluded from declaration
  coverage.** They are counted in `coverage.excluded_surface_kinds` with a
  reason; unknown coverage is not treated as absence.
* **The catalogue is not exhaustive.** `coverage.exhaustive` is false. An
  undeclared surface is a real finding; an absent observation is a stated
  unknown, not proof that a capability is missing.

## Not implemented here

Capability readiness (installed, configured, reachable, trusted or permitted)
is not observed. The service envelope, task preflight, read-only evidence
collector, durable evidence store, offline task profile and read-only
model-side maps exist as separate packages; this catalogue only declares and
joins their surfaces. Automatic evidence recording, live prefetch delivery into
a model turn, MCP/daemon wiring for structured edits and any daemon operation
for capabilities remain unimplemented.
