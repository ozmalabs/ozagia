# Read-only research projection (M5/WP7)

Status: implemented in `src/ozagia/core/research_projection.py`, 2026-11-18.
It is the read-only research slice of M5: retained research records become
bounded facts about claims, sources, assumptions and evidence. It is not a
truth assessment, a live research client, or the Ocelot adapter.

## What it answers

Given a repository root and, optionally, explicit research session ids:

- Which retained research sessions exist, and are they readable/valid?
- What claims, sources, assumptions and evidence are recorded?
- Which sources have a SHA-256, and does a selected local file still match it?
- How is each source held (epistemic role, authority, review status, scope)?
- Which inputs are stale, absent, unverified or explicitly unknown?

It does not answer whether a claim is true, whether a source supports it,
whether a model candidate is faithful, or whether a paper's formalization is
correct.

## Inputs and state

| Input | Default | Rule |
| --- | --- | --- |
| root | required | repository whose state directory is read |
| sessions | `None` (discover) | at most 8 ids; explicit `[]` selects none; ids match `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}` |
| observed_at | current time | injected integer timestamp or short string |
| verify_sources | `True` | hash selected local source files; never fetch |

Sessions are read from `<state>/research/<id>.json`, where `<state>` is
`<root>/.ozagia` unless `OZAGIA_STATE_DIR` names another directory. An
explicitly named legacy session under `<root>/var/ocelot_research/<id>.json`
is read as `storage="legacy"`. Discovery lists only the current state
directory. A symlinked session file is not opened. Files over 8 MiB are
reported invalid rather than partially read.

`ResearchGraph.load` is used, so the same schema and event-log validation as
the research tools applies. The projection never records, amends, links,
exports or writes a graph.

## Report

```json
{
  "schema": {"namespace": "ozagia.research_projection", "version": "1.0"},
  "projection_version": "1.0",
  "root": "/repo",
  "mode": "read_only",
  "selection": "explicit | discovered",
  "state_dir": "/repo/.ozagia/research",
  "sessions": [{"id": "research-...", "state": "present",
                "file_sha256": "...", "schema_version": 7,
                "freshness": "current", "counts": {"claims": 1}}],
  "claims": [{"session": "research-...", "id": "claim-0001",
              "claim_type": "empirical_observation", "status": "open",
              "wording_sha256": "...", "source_ids": ["source-0001"],
              "assumption_ids": ["assumption-0001"],
              "support_state": "declared_links_only",
              "evidence": "declared", "freshness": "current",
              "truth_assessed": false, "trust": {"...": "..."}}],
  "sources": [{"session": "research-...", "id": "source-0001",
               "recorded_sha256": "...", "observed_sha256": "...",
               "hash_state": "match", "freshness": "current",
               "evidence": "observed", "holdings": [{"id": "holding-0001"}]}],
  "assumptions": [{"id": "assumption-0001", "evidence": "declared",
                   "truth_assessed": false}],
  "evidence": [{"id": "evidence-0001", "evidence": "declared",
                "truth_assessed": false}],
  "counts": {"sessions": 1, "claims": 1, "sources": 1},
  "provenance": {"reader": "ozagia.core.research_projection",
                 "files": [{"id": "...", "sha256": "...", "bytes": 1}]},
  "engines": {"ocelot": {"state": "unknown", "reason": "deferred..."}},
  "not_assessed": ["..."],
  "deferred": ["..."],
  "omissions": [],
  "truncated": {"truncated": false, "omitted": 0,
                "next_cursor": null, "reason": null},
  "truth_assessed": false,
  "hash": "sha256:..."
}
```

`hash` is `contracts.content_hash` of the report without `hash`.
`projection_hash(report)` and `projection_json(report)` are the stable
helpers. Output is canonical JSON: sorted keys, no floats, no server clock
except an explicitly injected `observed_at`.

Session `state` is `present`, `absent`, `invalid` or `unauthorized`. A session
that is not on disk is retained with `freshness="absent"` and a reason; it is
never silently dropped or upgraded. An unreadable or unsupported-schema file
is `invalid` with `freshness="unknown"`.

## Claims and evidence

Claims are projected from recorded `Claim` nodes. The projection returns:

- `claim_type`, recorded `status`, model-candidate/parse flags;
- a SHA-256 and character count of the wording, never the wording itself;
- cited `Source`/`Reference`/`Evidence` ids and `assumes`/`derived_from` ids;
- relation counts and `support_state` (`declared_links_only` or `unlinked`);
- per-source freshness and the trust labels derived from `Holding` records;
- `evidence="declared"` and `truth_assessed=false`.

Assumptions and `Evidence` nodes are also `declared`. A recorded
`supports`/`opposes`/`derived_from` edge is author/reviewer provenance, not an
observation that the support is true. No wording, `raw_excerpt`, rationale,
scope prose, proof text, citation key or URL query is returned. This is an
allowlist projection, not a redactor applied to arbitrary text.

A claim's freshness is the join of its session record freshness and its cited
sources' freshness, using `contracts.join_freshness`. No source means
`unknown`. A stale source cannot leave a claim `current`.

## Source hashes and freshness

A source is verified only when all of the following hold:

1. it records a 64-character SHA-256;
2. it records a relative path and a `root_id`;
3. that root is in the graph's explicit research roots;
4. the path is selected in the root manifest (when one is recorded);
5. the path resolves inside the root without symlink escape;
6. it is a regular file of at most 2 MiB.

The file is read and hashed read-only. The result is:

| State | Meaning |
| --- | --- |
| `match` / `current` | recorded and observed SHA-256 agree |
| `mismatch` / `stale` | the selected file changed |
| `absent` / `absent` | the selected file is gone |
| `not_recorded` / `unknown` | no valid SHA-256 was recorded |
| `unverified` / `unknown` | remote URL, unselected path, unsafe path, missing root or size/read failure |

A remote URL is never fetched. Its URL is represented by scheme, host and a
SHA-256; query, credentials and path are not returned. `verify_sources=False`
reports every source as `unverified` without reading it.

## Trust labels

`Holding` labels are copied as allowlisted values: `epistemic_role`,
`authority`, `review_status`, `status`, bounded `use_constraints`, scope and
rationale *presence* (not text), reviewer, integer `confidence_pct`, and
`truth_assessed=false`. `recorded_truth_assessed` preserves a malformed
recorded value for inspection, but it never makes the projection verified.
Warnings/issues from `research.holdings.claim_holding` are counts and codes,
not free text. An unheld source stays `unheld` with format defaults or
`defaults_only`; the projection does not invent a holding.

## Bounds and omissions

| Bound | Value |
| --- | --- |
| Sessions per projection | 8 |
| Claims / sources per session | 50 / 50 |
| Assumptions / evidence per session | 30 / 50 |
| Holdings shown per source/claim | 8 |
| Ids shown per relation | 16 |
| Session file read | 8 MiB |
| Source file hash | 2 MiB |
| Report | 96 KiB (deterministic detail trimming) |
| Preflight join view | 12 claims / 12 sources / 8 assumptions / 8 evidence; 56 KiB canonical JSON |
| Facts returned to preflight | 48 |

Truncation is explicit in `omissions` and the `truncated` envelope. Trimming
detail never upgrades a claim or source: readiness still reports the unknown
inputs it cannot see. Source and claim lists are sorted by ids so repeated
projections over unchanged state are byte-identical.

## Preflight join

`core.task_preflight.build_preflight(..., research_sessions=[...])` adds the
optional `research` dimension unless the caller omits the input. It uses:

- `projection_readiness(report)`: `false` only for an unreadable/unauthorized
  session; `unknown` for absent/invalid sessions or any recorded claim;
- `preflight_join(report)`: a smaller deterministic view (12 claims, 12
  sources, 8 assumptions, 8 evidence records by default);
- `projection_facts(report)`: bounded `contracts.Fact` records. Claim,
  assumption and evidence facts always carry `evidence="declared"`.

A recorded claim cannot make the preflight green. A selected research session
with no claim, assumption or evidence record and current sources can be
`true`; otherwise the research dimension caps the assessment at `unknown`.

## Ocelot (deferred)

`research.engines.ocelot` is always:

```json
{"state": "unknown",
 "reason": "the Ocelot adapter is deferred in this read-only slice; no Ocelot module was imported or executed"}
```

This slice does not import, install, execute or query Ocelot, and does not
treat its absence as an empty successful result. A future adapter must record
the engine/version, input hashes, output, verifier and whether a result is an
empirical support or a formal derivation before this field can become
anything other than `unknown`.

## No live actions

The module has no network client, fetch call, model/OCR/parser call, daemon
call, subprocess, target-project import or write path. Tests monkeypatch the
fetch and reference-sync entry points to fail and compare file sizes/mtimes
before and after. Determinism is checked by calling twice and by subprocess
hash-seed checks in the preflight suite.

## Explicit unknowns

- Claims, assumptions and evidence are recorded declarations, not observed
  facts; no truth assessment is performed.
- A `supports`/`opposes` link is authored provenance, not evidence.
- Only selected local files with a recorded hash are verified; remote sources
  and unselected paths stay unknown.
- Model candidates, formulas, OCR and document parsers were not executed.
- Holdings and metadata are user-supplied; the projection checks structure and
  surface warning/issue codes, not factual accuracy.
- Session discovery covers `<state>/research`; legacy sessions are read only
  when named explicitly.
- Ocelot is absent and represented as an explicit unknown.
- There is no CLI/MCP surface for this projection in this slice; it is a Python
  API consumed by the optional preflight join.
