# Optional Ocelot join for the research projection

Status: implemented in `src/ozagia/research/ocelot_projection.py`, 2026-11-18.
It is the optional M5 Ocelot piece over the read-only research projection. It
is additive, opt-in and read-only: the base projection in
`core/research_projection.py` does not call Ocelot and its schema/hash is
unchanged unless `attach_ocelot` is called.

## Operation

The single operation is **source-anchored claim-shape analogy**:

1. Select claims from an existing `ozagia.research_projection` v1.0 document.
2. Keep claims that cite at least one `Source`.
3. Put each claim in an anchor made from its exact cited-source identity and
   its epistemic class. Source identity prefers the source's recorded SHA-256,
   then its observed SHA-256, then a session-local reference or source id.
4. Send one worker row per claim to the existing Ocelot analogy worker through
   `ozagia.core.ocelot_code.cluster_findings`. Each claim is one worker run;
   the target is the anchor. Ocelot returns candidate structural analogies
   only within an anchor.
5. Attach a versioned section with the engine/version, input and output hashes,
   verifier state, candidate clusters/analogies, coverage, omissions and
   explicit unknowns.

Anchors are source-and-class boundaries, not a claim that two claims say the
same thing. Exact groups (same anchor) are a deterministic fallback; candidate
clusters are the Ocelot analogies. Neither is a support edge, a truth verdict or
a consensus statement.

### Why this reuses the existing worker and not the review reconciler

`research.ocelot_reconcile` is the review-finding reconciler. It compares
findings made by different reviewers about the *same* claim/reference target and
preserves dissent in a reconciliation. This operation compares *different
claims*; using that reconciler would either require a fake target or duplicate
its review semantics.

The external protocol is still the existing worker
(`examples/ocelot_reconcile_worker.py`). `core.ocelot_code.cluster_findings` is
the public adapter that resolves the optional checkout, runs the worker
in-process or as a subprocess, filters unknown ids and splits clusters that
cross its exact key. We call it with explicit rows:

```json
{
  "finding_id": "c0001",
  "run_id": "r0001",
  "target_type": "ClaimAnchor",
  "target_id": "<anchor sha256>",
  "family": "other",
  "severity": "note",
  "wording": "epistemic_class=formal; claim_type=theorem; status=open; ..."
}
```

The `wording` is a structural summary only. Claim wording, excerpts, URLs,
rationale and prompts are never sent or returned. No new external protocol is
invented, and no Ocelot module is imported by normal `build_projection` use.
With a resolvable checkout, the default path runs the existing optional worker
in-process or as a subprocess, exactly as `cluster_findings` already does; the
adapter itself writes nothing, never installs or downloads Ocelot, and never
writes the research state or the target checkout.

## Usage

```python
from ozagia.core import research_projection as RP

report = RP.build_projection(root)              # no Ocelot call
joined = RP.attach_ocelot(report)               # resolve; no install/download
joined["ocelot"]["state"]                       # ready | not_installed | ...
```

Direct module use is equivalent:

```python
from ozagia.research import ocelot_projection as OP

joined = OP.attach_ocelot(
    report,
    ocelot_root=None,             # optional explicit checkout
    mode="auto",                  # auto | inprocess | subprocess
    timeout=120,
    expected_engine_id="ocelot:analogy_search",
    expected_version="1.4.0",     # optional pin; unknown/mismatch is invalid
    expected_input_hash=None,     # optional exact input-hash check
    max_claims=OP.MAX_CLAIMS,
)
```

For offline tests or a host-owned engine, inject a callable. It receives the
request mapping and returns a worker-shaped mapping:

```python
def engine(request):
    return {
        "state": "ready",
        "available": True,
        "engine": "ocelot:analogy_search",
        "engine_version": "1.4.0",
        "input_hash": request["input_hash"],   # optional; checked if present
        "clusters": [["c0001", "c0002"]],
        "analogies": [
            {"source_finding": "c0001", "target_finding": "c0002",
             "order_n": 4},
        ],
    }

joined = OP.attach_ocelot(report, engine=engine)
```

The injected engine is a seam, not a new protocol: it returns the same safe
fields the worker exposes. Unknown ids, self-links, cross-anchor links and
duplicate analogies are filtered and counted in `omissions`.

## Section schema

The section is added under the top-level `ocelot` key of the joined document.
It is not the base `engines.ocelot` field; that field remains the base
projection's explicit unknown.

```json
{
  "schema": {"namespace": "ozagia.ocelot.research_projection",
             "version": "1.0"},
  "requested": true,
  "available": true,
  "state": "ready",
  "reason_code": null,
  "reason": "Ocelot completed the structural comparison; ...",
  "mode": "auto",
  "actual_mode": "subprocess",
  "operation": {
    "id": "source_anchored_claim_analogy",
    "version": "1.0",
    "read_only": true,
    "worker_protocol": "ocelot_reconcile_worker/findings-v1",
    "input_scope": "claims with at least one cited source",
    "cross_class_links": false,
    "creates_support_edges": false
  },
  "engine": {
    "id": "ocelot:analogy_search",
    "version": "1.4.0",
    "version_state": "resolved",
    "source": "sibling",
    "id_supported": true,
    "constraint": {"expected_id": null, "expected_version": "1.4.0",
                   "satisfied": true}
  },
  "verifier": {"state": "not_verified", "method": null,
                "reason": "no verifier ran; ...", "truth_assessed": false},
  "inputs": {
    "hash_algorithm": "sha256",
    "projection_hash": "<base projection hash>",
    "observed_projection_hash": "<recomputed base hash>",
    "hash_state": "match",
    "claim_set_hash": "<sha256>",
    "payload_hash": "<sha256>",
    "input_hash": "<sha256>",
    "selected_keys": ["research-session-a/claim-0001", "..."],
    "clustered_keys": ["research-session-a/claim-0001", "..."],
    "expected_input_hash": null,
    "expected_input_hash_state": "not_checked",
    "engine_input_hash_state": "not_reported"
  },
  "output": {"state": "produced", "hash": "<sha256>", ...},
  "candidate_clusters": ["..."],
  "analogies": ["..."],
  "exact_groups": ["..."],
  "coverage": {"claims_total": 8, "claims_selected": 8,
               "claims_anchored": 6, "claims_without_sources": 2,
               "anchors": 3, "candidate_clusters": 3, "analogies": 3},
  "score": {"state": "unavailable", "value": null, "source": null,
            "calibrated": false, "reason": "..."},
  "truth_assessed": false,
  "support_edges_created": false,
  "epistemic_boundary": {"empirical": "...", "conditional": "...",
                          "formal": "...", "unknown": "...",
                          "cross_class_links": false,
                          "evidence_kinds_merged": false},
  "omissions": ["..."],
  "truncated": {"truncated": false, "omitted": 0,
                "next_cursor": null, "reason": null},
  "unknown": ["..."],
  "deferred": ["..."]
}
```

### Engine and version

- `engine.id` is the engine reported by the worker (normally
  `ocelot:analogy_search`) or the injected engine id.
- `engine.version` is the resolved checkout version when the checkout declares
  one (for example `pyproject.toml`), otherwise `null` with
  `version_state="unknown"`.
- `engine.source` is `environment`, `sibling`, `installed`, `explicit` or
  `injected`. The checkout path is not returned.
- `expected_engine_id` / `expected_version` are optional pins. A mismatch, or a
  pinned version that the engine cannot report, is an `invalid` section with
  `reason_code` `engine_mismatch` or `version_mismatch`. No output is accepted.
- `engine.id_supported` reports whether the id is the documented analogy engine.
  It does not by itself invalidate another Ocelot engine.

### Input and output hashes

| Field | Covers |
| --- | --- |
| `inputs.projection_hash` | the base projection's declared `hash` |
| `inputs.observed_projection_hash` | recomputed base hash; must match on a valid input |
| `inputs.claim_set_hash` | selected claim keys and allowlisted structural fingerprints (type, status, support state, freshness, ids, counts, wording hash) |
| `inputs.payload_hash` | the exact worker request rows |
| `inputs.input_hash` | `projection_hash` + `claim_set_hash` + `payload_hash` |
| `output.hash` | the output body: `input_hash`, candidate clusters, analogies, exact groups, coverage and score |

All hashes are lowercase SHA-256 hex. `inputs.selected_keys` is the exact sorted
list of selected projection record keys (`<session>/<claim-id>`). A caller can
pin `expected_input_hash`; a mismatch is `invalid` (`input_hash_mismatch`) and
the engine is not called. An injected engine may return its own `input_hash`;
a mismatch is `invalid` (`engine_input_hash_mismatch`) and its output is
discarded.

The joined document's top-level `hash` is recomputed for every non-`invalid`
attach result. An `invalid` result leaves the base `hash` unchanged; it must
not be treated as a re-sealed projection.

### Verifier

`verifier.state` is always `not_verified` in this slice. No proof checker,
test runner, human reviewer or semantic verifier ran. An engine cannot promote
itself: `truth_assessed`, `verified`, `verification.state="verified"` and
support-edge claims in the engine output are ignored and listed in
`output.rejected_engine_claims`. `output.engine_reported_truth_assessed` records
that the engine made the claim; it does not accept it.

## Empirical, conditional and formal claims

| Recorded `claim_type` | `epistemic_class` | Meaning here |
| --- | --- | --- |
| `empirical_observation` | `empirical` | a recorded observation; no support or truth asserted |
| `hypothesis`, `inference` | `conditional` | conditional or inferential; not observed and not proved |
| `theorem`, `definition` | `formal` | a formal statement type; not machine-checked and not a derivation |
| anything else | `unknown` | never promoted |

The class is part of the anchor hash, so Ocelot can never link an empirical
claim to a formal or conditional claim. A cross-class engine cluster is split
into its anchors; a cross-class analogy is dropped and counted in `omissions`.
The section always has `epistemic_boundary.cross_class_links=false`,
`epistemic_boundary.evidence_kinds_merged=false`, `truth_assessed=false` and
`support_edges_created=false`. No claim status, support state or evidence kind
is merged or promoted because two records cluster.

## Absence and failure states

The adapter never raises because Ocelot is absent, misconfigured or fails, and
never installs or downloads Ocelot.

| `state` | Meaning | Output |
| --- | --- | --- |
| `ready` | the engine ran; candidate clusters/analogies were accepted | `produced` (possibly zero candidates) |
| `not_installed` | no checkout resolved; no download attempted | `fallback_only`: exact groups only |
| `unavailable` | an explicit checkout is missing, resolution was incomplete, or the engine failed | `fallback_only`: exact groups only |
| `invalid` | bad mode/timeout/limit, bad projection or expected input hash, invalid engine output, engine id/version mismatch | `not_produced`; no candidates |
| `no_claims` | no selected claim cites a source | `no_output` |

An absent Ocelot does not fail the research projection and does not change the
base records. The section records a bounded `reason`/`reason_code`; engine error
detail is redacted, path-stripped and bounded, never a raw log or a checkout
path. The default engine path uses the existing Ocelot resolution order:
explicit `ocelot_root`, `$OZAGIA_OCELOT`, a sibling checkout, then a managed
install. No install is attempted.

## Bounds

| Bound | Value |
| --- | --- |
| Claims selected per attach | 120 |
| Source ids used per claim | 16 (from the base projection) |
| Candidate clusters | 40 |
| Exact groups | 40 |
| Analogies | 120 |
| Attached section | 64 KiB canonical JSON |
| Source identity | recorded hash, observed hash, reference or session-local source id |
| Claim wording sent | none; only a structural summary |

Truncation and exclusion are explicit in `coverage`, `omissions` and
`truncated`. Selected claim keys stay exact; the output lists are trimmed first
by count and then by bytes. Trimming never promotes a claim, creates a support
edge or changes a base projection record.

## What this does not establish

- That a claim is true, that a source supports it, or that a formal derivation
  is valid.
- That two clustered claims say the same thing; the comparison is structural.
- That an Ocelot analogy is causal, a consensus or a review verdict.
- That the source-anchored claim set is complete. Only claims with a cited
  source and within the base projection bounds are compared.
- That an Ocelot checkout or version is trusted or current. Resolution is
  offline; an install is always a separate explicit pinned action.
- That empirical, conditional and formal records share an evidence kind. They
  never do in this adapter.

## Explicit unknowns and deferred work

Unknowns reported in every section include: source identity for omitted or
unhashed sources, possible source-id truncation in the base projection, base
projection detail trimming by bytes, an unmanaged checkout's version, the lack
of a calibrated score, the lack of semantic comparison and the absence of any
truth/support verdict.

Deferred: formal proof/Lean checking, an Ocelot version compatibility matrix,
calibrated scoring, clustering assumptions/evidence/sources as first-class
shapes, write-back or support-edge creation, and cross-repository merges.

## Tests

```sh
PYTHONPATH=$PWD/src /home/matt/ozmadev/ozagia/.venv/bin/python -m pytest -q \
  tests/test_ocelot_research.py tests/test_research_projection.py \
  tests/test_research_ocelot_reconcile.py tests/test_ocelot_code.py \
  tests/test_ocelot_install.py
/home/matt/ozmadev/ozagia/.venv/bin/ruff check src tests
```

`tests/test_ocelot_research.py` injects fake engines and a fake worker. It
covers successful candidate clusters, absent engine, explicit missing checkout,
projection-hash tamper, expected and engine input-hash mismatch, version/engine
id mismatch, rejected truth/verification claims, empirical/conditional/formal
separation, secret-canary absence, base-projection immutability, determinism and
invalid-request states. No test resolves, installs or executes a real Ocelot
checkout, and no test uses the network or a model.
