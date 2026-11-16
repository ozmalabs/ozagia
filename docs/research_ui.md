# Read-only research UI views

`ozagia.core.research_ui` presents an existing research investigation without
changing it. It loads `.ozagia/research/<id>.json`, projects recorded nodes and
edges into one of six views, and emits a deterministic JSON model or a
self-contained HTML page. It does not build or refresh the graph, download an
artifact, call a model, use the network or assess truth.

The presentation layer is deliberately a projection. A recorded `supports`
edge is shown as recorded; the view does not decide whether it supports
anything. A `Reconciliation` node's consensus and dissent rows are copied
exactly; the view does not derive agreement. A missing retraction record is
shown as `unknown`, not as `none`.

## Usage

```sh
PYTHONPATH=src python -m ozagia.research_ui_cli \
  --root /path/to/workspace \
  --investigation research-20261116-120000-abcd1234 \
  --view library \
  --format json

PYTHONPATH=src python -m ozagia.research_ui_cli \
  --root /path/to/workspace \
  --investigation research-20261116-120000-abcd1234 \
  --view consensus \
  --format html \
  --out consensus.html
```

Options:

- `--root R` is the workspace root. The state directory is `<root>/.ozagia`
  unless `$OZAGIA_STATE_DIR` names another directory, as usual.
- `--investigation ID` is required. The id must be a valid research graph id
  and its file must exist.
- `--view` is one of `library`, `fetch`, `screening`, `evidence`, `consensus`
  or `citation`.
- `--format` is `json` (default) or `html`.
- `--out FILE` writes atomically to `FILE`. Without it, output goes to stdout.

Exit status is `0` on success and `2` for a missing or invalid graph, an
unknown view, or a malformed state. The only write is an explicit `--out`
file. The graph file and its cache are opened read-only.

The Python API is:

```python
from ozagia.core.research_ui import (
    build_research_view, load_state, render_html, render_json,
)

state, graph_path = load_state("research-...", root=".")
model = build_research_view(state, "library", root=".", source=graph_path)
json_text = render_json(model)
html_text = render_html(model)
```

`build_research_view` also accepts a `ResearchGraph`, a graph bundle or a
plain state mapping. It raises `ResearchUIError` for a malformed state or an
unknown view.

## Envelope

Every view is one versioned JSON object:

| Field | Meaning |
| --- | --- |
| `schema` | View model schema, currently `1`. |
| `schema_name` | `ozagia.research_ui.view`. |
| `view` | One of the six view names. |
| `title` | Human-readable view title. |
| `investigation_id` | The research investigation id. |
| `columns` | Ordered column definitions for the table and HTML. |
| `nodes` | Compact `{id, type, label}` records involved in the view. |
| `rows` | The primary table. Each row carries `truth_assessed: false`. |
| `summary` | Counts and names derived only from recorded fields. |
| `provenance` | Generator, graph schema, source path, root and record counts. |
| `omitted` | Rows omitted by the row cap, plus the graph projection's omissions. |
| `not_assessed` | Explicit list of things this view did not evaluate. |
| `truth_assessed` | Always `false`. |
| `note` | A short read-only projection warning. |

Graph-shaped views also include `graph`, produced by
`ozagia.core.view.build_view`. It uses the existing `evidence` and `consensus`
apertures, or a small citation aperture defined next to the view. Unresolved
node and edge types become `"other"`; they are never silently dropped.
`omitted.graph` reports any scope or cap omission from that projection.

Row order is fixed by id and record kind. JSON uses sorted keys, two-space
indentation and `ensure_ascii=False`. The same state and root always produce
the same bytes. The view never embeds a generated-at timestamp, so a diff of
two runs reflects record changes, not the clock.

## Views

### `library`

Projects `Reference` nodes and reference-shaped edges.

- `reference_id`, `title`, `citation_key`, `citation_keys`
- `identity` from `reference_identity` (`doi:`, `arxiv:`, `key:` or `title:`),
  or `null` when nothing identifies the record
- `identifiers`, `year`, `authors`, `venue`, `reference_type`
- `metadata_status`, `metadata_provenance`, `source_locators`, `conflicts`
- `retraction_status`: `retracted`, `concern`, `corrected`, `none` or
  `unknown`; `retraction` keeps provider, `checked_at` and notices
- `versions`: references linked by `same_as`, with title, citekey and identity
- `supersedes`, `superseded_by`, `source_ids`, `cited_by`,
  `work_citations`, `work_cited_by`
- `duplicate_of` and `flags` (`retracted`, `concern`, `corrected`, `duplicate`)
- `missing`: required display fields that are absent

The model also carries top-level `duplicates` groups (by reference identity and
by citation key) and `unresolved_keys`: citation keys recorded on a node that
have no `Reference` record. These are projections of
`ozagia.research.references.check_references`, not new checks.

### `fetch`

Projects recorded `Source` nodes and reference metadata; it never resolves a
URL or reads an artifact's content.

- `row_kind`: `artifact`, `candidate`, `source` or `unresolved_reference`
- `provider`, `license`, `sha256`, `url`, `media_type`, `version`,
  `retrieved_at`, `size`, `external`, `unofficial`
- `cache_relative`, `cache_path`, `cache_present` and `status`
- `status`: `cached`, `missing_cache`, `cache_path_unchecked`,
  `invalid_cache_path`, `artifact_recorded`, `recorded_source`,
  `metadata_candidate`, `candidate_without_url`, `identifier_only` or
  `no_identifier`

`artifact` rows come from a `Source` whose recorded locator has
`kind = "cached_fetch"`. There is no separate persisted `Fetch` node type:
`research_fetch_preview` records nothing, so the view can only show the
recorded `Source` and `Reference.open_access` metadata it finds. `candidate`
rows come from a recorded `Reference.open_access` metadata block; the provider
is reported as `recorded_open_access` because the block does not carry an
independent provider field. `source` rows are other recorded artifacts (for
example a local paper import). `unresolved_reference` rows make a missing fetch
decision visible instead of leaving a reference absent.

Cache presence is a local `Path.is_file()` observation of the recorded relative
path under `<state>/research/cache`. It does not hash the file again. A path
that is absolute or contains `..` is reported as `invalid_cache_path` and is
not joined to the cache root.

### `screening`

Projects `Decision` nodes with `screening = true` and their `screens` edges.

- `decision_id`, `reference_id`, `reference_title`, `citation_key`
- `stage` (`title_abstract` or `full_text`), `decision`, `reason`, `reviewer`,
  `criteria`, `created_at`
- `supersedes`, `superseded_by`, `latest_for_stage`
- `missing`

`summary` and the top-level `prisma` block carry the same PRISMA-style counts
as `ozagia.research.review.screening_report`: records identified, title/abstract
screened/included/excluded/maybe, full-text assessed/included/excluded/maybe,
included/excluded totals and unresolved references. Latest-decision selection
is replayed in `(created_at, id)` order, so the result does not depend on the
input node order. A superseded decision remains in `rows`; it is not hidden.

### `evidence`

Projects claim-to-evidence relationships and the locators recorded on them.

- `claim_id`, `claim_wording`, `claim_status`, `claim_type`
- `relation` (`supports`, `opposes`, `cites`, `derived_from`), `direction`,
  `edge_id`, `edge_rationale`, `edge_provenance`
- `evidence_id`, `evidence_kind`, `evidence_wording`, `evidence_status`
- `claim_locator`, `evidence_locator`, `locator`, `locator_label`
- `reference_ids`, `citation_keys`, `reference_citation_keys`
- `status`: `recorded_relation`, `no_recorded_evidence` or
  `unlinked_evidence`

Claims with no recorded evidence get an explicit `no_recorded_evidence` row.
Recorded `Evidence` and `Observation` nodes that no claim reaches get an
`unlinked_evidence` row. A claim is counted as having evidence only for a
recorded `supports`, `opposes` or `derived_from` relation; `cites` is counted
separately as a citation, not evidence. A locator is copied from
`source_locator`, `locator` or the first `source_locators` entry.
`locator_label` renders a path with line range, a URL, or a shortened SHA-256.

### `consensus`

Projects `ReviewPlan`, `ReviewRun`, `ReviewFinding` and `Reconciliation`
records with their `addresses`, `implements`, `reviews`, `finds`, `about`,
`agrees_with`, `disagrees_with` and `reconciles` edges.

- Plan rows include question, kind, status, roles, models, rounds, blinding,
  minimum reviewers, adjudication, target ids, run ids, finding ids and
  reconciliation ids.
- Run rows include plan, reviewer, role, model, provider, round, status,
  summary, recommendation, confidence, finding count, target ids, warnings,
  tokens, cost and output hash.
- Finding rows include wording, severity, issue type, quote, recommendation,
  confidence, citation keys, claim/reference/evidence/argument ids, target ids,
  and both directions of agreement and disagreement edges.
- Reconciliation rows include method, minimum reviewers, run ids, metrics,
  clusters, status, adjudicator and rationale.

The top-level `consensus` and `dissent` arrays copy each recorded reconciliation
cluster, adding `reconciliation_id` and `plan_id`. They are never inferred from
finding wording, model output or reviewer overlap. `summary` reports counts,
plans without runs, plans without a reconciliation, runs without a plan and
findings without a run.

### `citation`

Projects `cites`, `cites_work`, `same_as`, `manifestation_of` and `supersedes`
edges between references and related records.

- `edge_id`, `edge_type`, `scope`, source and target ids, types, titles,
  citekeys and retraction statuses
- `provenance`, `rationale`, `created_at`

Top-level `retractions` lists references whose recorded status is `retracted`,
`concern` or `corrected`, with provider, checked-at, notices, citing records,
work citation links and alert ids. Top-level `neighbourhoods` gives every
reference's recorded `cites_work` outgoing and incoming ids, total degree,
depth `1` and `linked`/`isolated` status. `summary` counts edge types, statuses,
linked and isolated references and the maximum degree.

`graph` is a small custom aperture over `Reference` and `Source`, scoped to the
citation edge types above.

## Provenance and limits

`provenance` states `derived: true`, the generator, view schema, graph schema,
source file path, root and record counts by node and edge type. It does not
claim a provider fetch, model run or freshness check occurred.

Every view carries `not_assessed`. Common entries are truth, support and
freshness; each view adds its own limits (for example, retraction status
beyond `checked_at`, provider citation accuracy, reviewer quality, or whether a
recorded locator still matches the source text).

Rows are capped at `MAX_ROWS = 5000` after deterministic sorting. Any overflow
appears in `omitted.rows` with reason `row cap`. Graph projections carry their
own scope/cap counts under `omitted.graph`. Nothing else is dropped: unknown
record types and fields remain visible in `rows` or the graph projection's
`other` group.

## HTML

`--format html` writes one file with inline CSS and no external stylesheet,
font, image, script or network request. It renders the summary, the primary
table, compact records, extra view-specific tables, the graph projection (when
present), the not-assessed list, omissions, provenance and the full JSON model.
All values are HTML-escaped. There is no JavaScript, so recorded text cannot
execute.

## What remains

This slice is read-only and does not add write controls:

- **Editing.** No reference merge, retraction correction, screening revision,
  finding adjudication or reconciliation edit. Those stay behind the existing
  service operations and confirmation; a future UI can add a preview/apply
  form over `ResearchService.call`.
- **Zotero UI.** The Zotero adapter remains a project-side import path. This
  view does not browse a Zotero library, show collection membership, or push
  changes back.
- **Provider fetch buttons.** The fetch view shows recorded candidates and
  artifacts; it does not offer a "resolve", "preview" or "apply" button. Live
  resolution and download remain explicit `research_ref_resolve`,
  `research_fetch_preview` and `research_fetch_apply` calls with their own
  approval and network policy.
- **Manuscript view.** The plan's manuscript checks, bundles and unresolved
  citations are not part of this workstream. `research_manuscript_check` and
  `research_manuscript_bundle` already exist and can be projected later with
  the same envelope.
- **Graph interaction.** The HTML graph section is a static reading aid. It
  does not add the aperture picker, node inspector, filters or "open in editor"
  actions of the planned GUI plugin.
- **Live review.** No model calls, provider calls or reviewer execution. The
  consensus view only shows recorded plans, runs, findings and reconciliations.

## Tests

```sh
PYTHONPATH=src python -m pytest -q tests/test_research_ui.py
ruff check src tests
```

The tests cover a synthetic graph with each view, an empty investigation, a
missing investigation, an unresolved citation key, retraction and concern,
duplicate identities, versions, cached and missing fetch metadata,
consensus/dissent preservation, deterministic JSON independent of input node
order, HTML escaping and CLI JSON/HTML/`--out` behaviour. They use no network
and no model.
