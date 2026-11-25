# MCP tools — research surface

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/integrations/mcp/server.py`, `src/ozagia/integrations/mcp/graph_surface.py`, `src/ozagia/integrations/mcp/sre.py`, `src/ozagia/research/mcp.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

107 tools.

> Tools from `research/mcp.py.research_tools()`, plus `research_fetch`.

### `research_alert_ack`

Acknowledge a living-review alert.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `alert_id` | string | yes |  | — |
| `reviewer` | string | yes |  | — |
| `note` | string |  |  | — |
### `research_alert_record`

Record a living-review alert against a target.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `payload` |  |  |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
### `research_alert_report`

Report living-review alerts by kind and status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `status` | string |  |  | — |
### `research_alert_scan`

Scan recorded metadata for retractions, unresolved citations and new citations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `review_plan_id` | string |  |  | — |
| `since` | string |  |  | — |
| `reference_ids` | array |  |  | — |
### `research_appraisal_table`

Export risk-of-bias and certainty rows as Markdown or CSV.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_argument_brief`

Build a deterministic steelman/red-team/synthesis brief for a claim.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `mode` | string |  |  | — |
### `research_argument_check`

Check premise evidence, citations, objection replies and falsifiers.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `argument_id` | string | yes |  | — |
### `research_argument_record`

Record a candidate steelman/red-team/synthesis argument with evidence obligations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `mode` | string | yes |  | — |
| `thesis` | string | yes |  | — |
| `premises` | array | yes |  | — |
| `assumptions` | array |  |  | — |
| `objections` | array |  |  | — |
| `falsifiers` | array |  |  | — |
| `confidence` | number | yes |  | — |
| `reviewer` | string | yes |  | — |
| `model_runs` |  |  |  | — |
| `notes` | string |  |  | — |
### `research_argument_report`

Return an argument with its linked evidence and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `argument_id` | string | yes |  | — |
### `research_certainty_record`

Record a reviewer certainty rating with reasons and adjustments.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `rating` | string | yes |  | — |
| `reasons` | array | yes |  | — |
| `reviewer` | string | yes |  | — |
| `framework` | string |  |  | — |
| `downgrades` | array |  |  | — |
| `upgrades` | array |  |  | — |
| `notes` | string |  |  | — |
### `research_certainty_report`

Report certainty ratings, risk-of-bias summaries and warnings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `target_id` | string |  |  | — |
### `research_check`

Check research records and provenance; does not certify truth.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_check_lean`

Check generated Lean declarations with the local Lean executable.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string |  |  | — |
### `research_citation_resolution_report`

Report resolved and unresolved citation hints by method.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string |  |  | — |
### `research_citation_resolve`

Resolve a claim's numeric, author-year or DOI/arXiv citation hints to references.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `apply` | boolean |  |  | — |
| `threshold` | number |  |  | — |
### `research_cite_context`

Extract sentence/line citation contexts as candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `record` | boolean |  |  | — |
### `research_claim_holding`

Show the holdings and defaults for the sources a claim relies on.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
### `research_context`

Find cited local research context, plus any user positions on the same topic or records (reported apart from evidence).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `limit` | integer |  |  | — |
| `node_ids` | array |  |  | — |
### `research_contradiction_check`

Check a recorded contradiction's claims, edges and status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `contradiction_id` | string | yes |  | — |
### `research_contradiction_record`

Record a reviewed contradiction candidate between two claims.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_a_id` | string | yes |  | — |
| `claim_b_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `rationale` | string | yes |  | — |
| `evidence_ids` | array |  |  | — |
| `reviewer` | string | yes |  | — |
| `status` | string |  |  | — |
### `research_contradiction_report`

Report contradiction candidates and recorded contradictions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_contradiction_scan`

Find deterministic contradiction candidates across claims and extractions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `max_pairs` | integer |  |  | — |
### `research_document_claim_ingest`

Record a note, HTML/website or EPUB Source, holding and claim candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `source_type` | string |  |  | — |
| `title` | string |  |  | — |
| `metadata` | object |  |  | — |
| `max_claims` | integer |  |  | — |
| `reviewer` | string |  |  | — |
| `holding` |  |  |  | — |
| `resolve_citations` | boolean |  |  | — |
### `research_document_claim_preview`

Parse a selected note, HTML/website snapshot or EPUB book into claim candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `source_type` | string |  |  | — |
| `title` | string |  |  | — |
| `metadata` | object |  |  | — |
| `max_claims` | integer |  |  | — |
### `research_evidence_table`

Project claims and evidence into a Markdown or CSV table.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_execute`

Run explicit graph-bound native substrate terms.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string | yes |  | — |
| `env` | object |  |  | — |
| `max_depth` | integer |  |  | — |
| `max_nodes` | integer |  |  | — |
### `research_export`

Export an auditable ledger and JSON bundle.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_export_lean`

Export explicit Boolean terms as Lean definitions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string |  |  | — |
### `research_external_search`

Search through an explicitly named external provider; results are unverified candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `provider` | string | yes |  | — |
| `limit` | integer |  |  | — |
### `research_extraction_check`

Check an extraction's target, fields, numerics and locator.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `extraction_id` | string | yes |  | — |
### `research_extraction_record`

Record structured study fields and effect sizes for a Reference or Claim.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string |  |  | — |
| `claim_id` | string |  |  | — |
| `fields` | object | yes |  | — |
| `source_locator` | object |  |  | — |
| `extractor` | string | yes |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
### `research_extraction_report`

Return an extraction with its target and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `extraction_id` | string | yes |  | — |
### `research_extraction_table`

Export structured extractions as Markdown or CSV.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_fetch`

Fetch the text of a paper whose web fetch failed (404/403, a PDF the fetch tool can't read, a publisher landing page). Resolves open-access copies (arXiv, DOI via Crossref/OpenAlex/Unpaywall, PubMed/PMC via Europe PMC, Semantic Scholar, OpenReview, citation_pdf_url on a landing page), downloads with a size cap, extracts PDF or JATS text, caches it by content hash and returns it page by page with a stable source: key. Pass offset=next_offset for the next part, refresh=true to resolve again instead of using the cache. Open access only; network is off with OZAGIA_RESEARCH_OFFLINE=1 (cached copies are still served).

| parameter | type | required | default | description |
|---|---|---|---|---|
| `url` | string | yes |  | The URL that failed. |
| `max_chars` | integer |  |  | Characters to return (default 20000, at most 100000). |
| `offset` | integer |  |  | Character offset into the document text (default 0). |
| `refresh` | boolean |  |  | Resolve the URL again instead of answering from the cache (default false). |
### `research_fetch_apply`

Download one selected artifact, hash it and record a Source.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `candidate` | object | yes |  | — |
### `research_fetch_preview`

List open-access artifact candidates for a Reference or identifier.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string |  |  | — |
| `identifier` | string |  |  | — |
| `reference` | object |  |  | — |
| `email` | string |  |  | — |
| `provider` | string |  |  | — |
### `research_formula_model`

Translate a formula and attach it as a reviewable claim model.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `formula` | string | yes |  | — |
| `domain_assumptions` | object |  |  | — |
| `context_scope_id` | string |  |  | — |
| `bindings` | object |  |  | — |
| `assumption_ids` | array |  |  | — |
| `residuals` | array |  |  | — |
| `reviewer` | string | yes |  | — |
| `review_note` | string | yes |  | — |
### `research_formula_parse_candidates`

Optionally parse supplied LaTeX formulas with SymPy/Lark; returns unverified candidates without execution or recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `formulas` | array | yes |  | — |
### `research_formula_resolve`

Use scoped research graph context to compare formula interpretations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `formula` | string | yes |  | — |
| `scope_id` | string | yes |  | — |
### `research_formula_translate`

Translate a supported formula into a native executable Term.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `formula` | string | yes |  | — |
| `domain_assumptions` | object |  |  | — |
### `research_holding_check`

Check a holding's source, defaults and format-specific warnings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `holding_id` | string | yes |  | — |
### `research_holding_record`

Record how a source should be held: evidence, interpretation, hypothesis, note, background, etc.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_id` | string | yes |  | — |
| `source_type` | string | yes |  | — |
| `epistemic_role` | string |  |  | — |
| `authority` | string |  |  | — |
| `review_status` | string |  |  | — |
| `use_constraints` | array |  |  | — |
| `scope` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
| `confidence` | number |  |  | — |
### `research_holding_report`

Report holdings by source type, epistemic role, authority and status.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_id` | string |  |  | — |
### `research_init`

Create an investigation.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `question` | string | yes |  | — |
| `mode` | string |  |  | — |
| `roots` | array |  |  | — |
### `research_inspect`

Inspect a session or a record and its links.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string |  |  | — |
| `transitive` | boolean |  |  | — |
### `research_link`

Link two research records.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `src` | string | yes |  | — |
| `relation` | string | yes |  | — |
| `tgt` | string | yes |  | — |
| `fields` | object |  |  | — |
### `research_manuscript_bundle`

Write a manuscript bundle with BibTeX, claims CSV and checks; optionally run Pandoc.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `title` | string | yes |  | — |
| `body` | string | yes |  | — |
| `output_dir` | string |  |  | — |
| `run_pandoc` | boolean |  |  | — |
| `pandoc_format` | string |  |  | — |
### `research_manuscript_check`

Check a manuscript body for unresolved citations, retracted references and unsourced claims.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `body` | string | yes |  | — |
| `require_claim_sources` | boolean |  |  | — |
### `research_model_candidate`

Attach an explicit native Term model candidate to a sourced claim.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `claim_id` | string | yes |  | — |
| `term` | object | yes |  | — |
| `bindings` | object |  |  | — |
| `assumption_ids` | array |  |  | — |
| `residuals` | array | yes |  | — |
| `reviewer` | string | yes |  | — |
| `review_note` | string | yes |  | — |
| `provenance` | object |  |  | — |
### `research_native_graph`

Project the research graph and optionally evaluate a native DSL term.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | object |  |  | — |
| `env` | object |  |  | — |
### `research_paper_ingest`

Record a selected paper's claims and references in the research graph.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `max_formula_candidates` | integer |  |  | — |
| `formula_offset` | integer |  |  | — |
| `latexml_path` | string |  |  | — |
| `bibliography_path` | string |  |  | — |
### `research_paper_preview`

Read a selected paper into source-linked claim candidates without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `max_formula_candidates` | integer |  |  | — |
| `formula_offset` | integer |  |  | — |
| `latexml_path` | string |  |  | — |
| `bibliography_path` | string |  |  | — |
### `research_paper_review_bundle`

Write review.md, review.json, claims/evidence CSVs, BibTeX, arguments and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `output_dir` | string |  |  | — |
### `research_paper_review_packet`

Build a complete deterministic packet for reviewing a paper Source.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_id` | string | yes |  | — |
| `plan_id` | string |  |  | — |
| `manuscript_body` | string |  |  | — |
| `max_claims` | integer |  |  | — |
### `research_paper_review_report`

Build the structured paper review report from recorded runs, findings, arguments and checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
### `research_paper_review_start`

Ingest a selected paper, create a review plan targeting its Source, and build the review packet.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `question` | string | yes |  | — |
| `roles` | array | yes |  | — |
| `models` | array | yes |  | — |
| `kind` | string |  |  | — |
| `bibliography_path` | string |  |  | — |
| `criteria` |  |  |  | — |
| `min_reviewers` | integer |  |  | — |
| `reviewer` | string |  |  | — |
### `research_pdf_claim_ingest`

Record PDF Source, claim candidates, GROBID references and best-effort citation links.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `backend` | string |  |  | — |
| `max_claims` | integer |  |  | — |
| `reviewer` | string |  |  | — |
| `resolve_citations` | boolean |  |  | — |
### `research_pdf_claim_preview`

Extract formal, abstract, contribution and definition claim candidates from a selected PDF without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `backend` | string |  |  | — |
| `max_claims` | integer |  |  | — |
### `research_pdf_preview`

Extract page text, OCR or GROBID TEI from a PDF; optionally extract claim candidates.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string |  |  | — |
| `path` | string |  |  | — |
| `source_id` | string |  |  | — |
| `backend` | string |  |  | — |
| `record_references` | boolean |  |  | — |
| `extract_claims` | boolean |  |  | — |
| `ocr` |  |  |  | — |
| `ocr_lang` | string |  |  | — |
| `ocr_dpi` | integer |  |  | — |
### `research_position_list`

List user positions by status (active, superseded, retracted, all), record or document.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `status` | string |  |  | — |
| `node_id` | string |  |  | — |
| `document` | string |  |  | — |
### `research_position_record`

Record the user's stated stance or correction as a user position sourced to this conversation, linked to the topic, records and documents it concerns. Store only what the user stated or approved.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `stance` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `topic` | string |  |  | — |
| `quote` | string |  |  | — |
| `stated_at` | string |  |  | — |
| `concerns` | array |  |  | — |
| `documents` | array |  |  | — |
| `correction_of` | string |  |  | — |
### `research_position_retract`

Retract an active user position; the record and its history remain.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `position_id` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `note` | string |  |  | — |
### `research_position_supersede`

Record a new user position that replaces an active one; topic, concerns and documents carry over when omitted.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `position_id` | string | yes |  | — |
| `stance` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `topic` | string |  |  | — |
| `quote` | string |  |  | — |
| `stated_at` | string |  |  | — |
| `concerns` | array |  |  | — |
| `documents` | array |  |  | — |
| `correction_of` | string |  |  | — |
### `research_record`

Append a research record.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `kind` | string | yes |  | — |
| `fields` | object | yes |  | — |
### `research_ref_check`

Check citation keys, duplicates, metadata gaps and recorded retractions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_ref_citation_fetch`

Fetch bounded OpenAlex references or citing works and record cites_work edges.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `direction` | string |  |  | — |
| `limit` | integer |  |  | — |
| `apply` | boolean |  |  | — |
### `research_ref_citation_neighborhood`

Traverse recorded cites_work edges around a Reference.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `depth` | integer |  |  | — |
| `direction` | string |  |  | — |
### `research_ref_citation_path`

Find a directed cites_work path between two References.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `src` | string | yes |  | — |
| `tgt` | string | yes |  | — |
| `max_depth` | integer |  |  | — |
### `research_ref_export`

Export recorded references as BibTeX or CSL-JSON.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `format` | string |  |  | — |
### `research_ref_import`

Record a selected bibliography as bibliographic Reference nodes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
### `research_ref_link_citations`

Link a record's stored citation keys to existing Reference nodes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string | yes |  | — |
### `research_ref_preview`

Parse a selected BibTeX, BBL, CSL-JSON or RIS file without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
### `research_ref_providers`

List external providers registered for opt-in use; none are enabled by default.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_ref_record`

Record or enrich one explicit bibliographic Reference.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference` | object | yes |  | — |
| `source_locator` | object |  |  | — |
### `research_ref_resolve`

Resolve DOI/arXiv/PMID/PMCID metadata from an open provider without recording.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `identifier` | string | yes |  | — |
| `email` | string |  |  | — |
| `provider` | string |  |  | — |
| `record` | boolean |  |  | — |
### `research_review_agreement`

Report Fleiss kappa, Jaccard, consensus and dissent across runs; optional embedding clusters.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `embedding_model` | string |  |  | — |
| `embedding_url` | string |  |  | — |
| `cluster_threshold` | number |  |  | — |
### `research_review_finding_add`

Add review findings to the cross-session ledger, explicitly or from a recorded review run; each has a document, anchor, severity and text.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
| `findings` | array |  |  | — |
| `run_id` | string |  |  | — |
| `document` | string |  |  | — |
| `document_text` | string |  |  | — |
### `research_review_finding_status`

Mark a ledger finding open, addressed, wont_fix or disputed with a note; the change is kept in its history.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `finding_id` | string | yes |  | — |
| `status` | string | yes |  | — |
| `note` | string | yes |  | — |
| `conversation_session_id` | string | yes |  | — |
### `research_review_outstanding`

List still-open ledger findings grouped by document, most severe first.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `document` | string |  |  | — |
| `statuses` | array |  |  | — |
### `research_review_packet`

Build a deterministic evidence packet for a review target.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string |  |  | — |
| `claim_id` | string |  |  | — |
| `reference_id` | string |  |  | — |
| `manuscript_body` | string |  |  | — |
### `research_review_plan_create`

Record a structured review plan with roles, models and criteria.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `question` | string | yes |  | — |
| `kind` | string |  |  | — |
| `roles` | array | yes |  | — |
| `models` | array | yes |  | — |
| `criteria` |  |  |  | — |
| `exclusion_criteria` |  |  |  | — |
| `sources` |  |  |  | — |
| `reviewers` | array |  |  | — |
| `rounds` | array |  |  | — |
| `blinding` | boolean |  |  | — |
| `min_reviewers` | integer |  |  | — |
| `adjudication` | string |  |  | — |
| `target_id` | string |  |  | — |
### `research_review_reconcile`

Record a reconciliation while preserving dissent; optional embedding or Ocelot structural clusters.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `method` | string |  |  | — |
| `min_reviewers` | integer |  |  | — |
| `adjudicator` |  |  |  | — |
| `rationale` | string |  |  | — |
| `run_ids` | array |  |  | — |
| `embedding_model` | string |  |  | — |
| `embedding_url` | string |  |  | — |
| `cluster_threshold` | number |  |  | — |
| `ocelot` | boolean |  |  | — |
| `ocelot_root` | string |  |  | — |
| `ocelot_python` | string |  |  | — |
| `ocelot_timeout` | integer |  |  | — |
| `ocelot_mode` | string |  |  | — |
### `research_review_report`

Return the plan, runs, findings and latest reconciliation.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
### `research_review_rereview`

Given a new version of a document, propose which open findings may now be addressed by checking whether their anchored text changed; never closes findings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `document` | string | yes |  | — |
| `text` | string |  |  | — |
| `statuses` | array |  |  | — |
### `research_review_run_record`

Record one independent model/role review run and its validated findings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `reviewer_id` | string | yes |  | — |
| `role` | string | yes |  | — |
| `model` | string | yes |  | — |
| `packet_sha256` | string | yes |  | — |
| `output` | object | yes |  | — |
| `provider` | string |  |  | — |
| `round` | string |  |  | — |
| `prompt_sha256` | string |  |  | — |
| `context_sha256` | string |  |  | — |
| `params` |  |  |  | — |
| `tokens` |  |  |  | — |
| `cost` | number |  |  | — |
| `status` | string |  |  | — |
| `target_id` | string |  |  | — |
### `research_review_search_log`

Record a search query and counts for a review plan.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `plan_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `provider` | string | yes |  | — |
| `executed_at` | string |  |  | — |
| `filters` |  |  |  | — |
| `result_count` | integer |  |  | — |
| `deduped_count` | integer |  |  | — |
| `export_sha256` | string |  |  | — |
| `notes` | string |  |  | — |
### `research_review_validate`

Validate one model review output against IDs, citations and packet quotes.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `output` | object | yes |  | — |
| `packet` | object |  |  | — |
### `research_risk_of_bias_check`

Check a risk-of-bias assessment's target, domains and support.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `assessment_id` | string | yes |  | — |
### `research_risk_of_bias_record`

Record one reviewer's risk-of-bias domains and overall judgement.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `tool` | string | yes |  | — |
| `domains` | array | yes |  | — |
| `overall` | string | yes |  | — |
| `reviewer` | string | yes |  | — |
| `notes` | string |  |  | — |
### `research_screen`

Record a reviewable title/abstract or full-text screening decision.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `reference_id` | string | yes |  | — |
| `stage` | string | yes |  | — |
| `decision` | string | yes |  | — |
| `reason` | string | yes |  | — |
| `reviewer` | string | yes |  | — |
| `criteria` |  |  |  | — |
### `research_screening_report`

Count recorded screening decisions in a PRISMA-style report.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_search`

BM25 search over selected local research passages.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `limit` | integer |  |  | — |
| `root_id` | string |  |  | — |
| `path` | string |  |  | — |
### `research_semantic_build`

Build the optional local vector sidecar; hash-bow is the default, not a semantic model.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `db_path` | string |  |  | — |
| `model` | string |  |  | — |
| `dim` | integer |  |  | — |
### `research_semantic_search`

Hybrid vector/FTS5 search over selected passages; semantic retrieval is optional.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `query` | string | yes |  | — |
| `limit` | integer |  |  | — |
| `path` | string |  |  | — |
| `db_path` | string |  |  | — |
| `model` | string |  |  | — |
| `alpha` | number |  |  | — |
| `hybrid` | boolean |  |  | — |
### `research_source_register`

Register a book, note, website, article, dataset or other source with format metadata and a default holding.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `source_type` | string | yes |  | — |
| `metadata` | object | yes |  | — |
| `root_id` | string |  |  | — |
| `path` | string |  |  | — |
| `url` | string |  |  | — |
| `sha256` | string |  |  | — |
| `reference` |  |  |  | — |
| `holding` |  |  |  | — |
| `reviewer` | string |  |  | — |
### `research_source_types`

List supported source formats and their default epistemic holdings.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_tei_preview`

Parse a selected TEI/JATS document into sections, references and citation targets.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `root_id` | string | yes |  | — |
| `path` | string | yes |  | — |
| `record_references` | boolean |  |  | — |
### `research_theory_compare`

Compare shared/unique evidence, compatibility and discernment between two theories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_a` | string | yes |  | — |
| `theory_b` | string | yes |  | — |
### `research_theory_compatibility`

Record a graded compatibility relation between two theories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_a` | string | yes |  | — |
| `theory_b` | string | yes |  | — |
| `degree` | string | yes |  | — |
| `scope` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
| `evidence_ids` | array |  |  | — |
### `research_theory_compatibility_matrix`

Return a deterministic theory compatibility matrix.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_theory_link_evidence`

Link a theory to supporting, challenging, discriminating or explained records.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_id` | string | yes |  | — |
| `target_id` | string | yes |  | — |
| `relation` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
### `research_theory_record`

Record a theory with assumptions and predictions.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `name` | string | yes |  | — |
| `statement` | string | yes |  | — |
| `scope` | string | yes |  | — |
| `mechanism` | string |  |  | — |
| `assumptions` | array |  |  | — |
| `predictions` | array |  |  | — |
| `assumption_ids` | array |  |  | — |
| `prediction_ids` | array |  |  | — |
| `reviewer` | string |  |  | — |
| `status` | string |  |  | — |
| `notes` | string |  |  | — |
### `research_theory_report`

Return one theory or the full theory ledger with checks.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_id` | string |  |  | — |
### `research_theory_rivalry`

Record a rival relation between two theories.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `theory_a` | string | yes |  | — |
| `theory_b` | string | yes |  | — |
| `scope` | string |  |  | — |
| `rationale` | string |  |  | — |
| `reviewer` | string |  |  | — |
### `research_theory_scan`

Find incomplete theories, untested predictions and non-discriminating rivalries.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
### `research_trace`

Trace directed research derivations.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `node_id` | string | yes |  | — |
| `max_depth` | integer |  |  | — |
### `research_zotero_import`

Read items from the Zotero local API without writing to the library.

| parameter | type | required | default | description |
|---|---|---|---|---|
| `session_id` | string | yes |  | — |
| `base_url` | string |  |  | — |
| `collection` | string |  |  | — |
| `limit` | integer |  |  | — |
| `apply` | boolean |  |  | — |
