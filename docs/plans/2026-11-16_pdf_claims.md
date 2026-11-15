# PDF claim extraction

**Date:** 2026-11-16. **Status:** stages A-C landed and integrated.

`pdf_preview` can extract page text or GROBID TEI, but it does not turn a PDF
into reviewable claims. This plan adds PDF claim candidates and an ingestion
path so a paper review can start from a PDF.

PDF extraction is lossy. Claims are candidates with source locators and an
extraction method; equations, tables and layout are marked degraded. No claim is
treated as verified or true.

## Principles

- Prefer GROBID TEI sections; fall back to page text only when structured
  sections are unavailable.
- Extract formal statements first: theorem, lemma, proposition, corollary,
  definition, claim, hypothesis, conjecture.
- Extract abstract/conclusion contribution candidates second, with lower
  confidence and explicit method labels.
- Preserve page/section/offset locators, raw excerpt, SHA-256, citation hints
  and `math_degraded`.
- Never fabricate a citation link from a bracketed number. Store citation hints
  as unresolved text unless a safe mapping exists.
- Record claims as unresolved candidates with `semantics_verified=False` and
  `truth_assessed=False`.
- Deduplicate against existing claims for the same PDF hash.

## Stage A: extractor (`research/pdf_claims.py`)

API:

```python
candidate_claims(extraction, *, max_claims=256) -> dict
preview_pdf_claims(graph, *, root_id, path, pdf_extractor=None, ...) -> dict
ingest_pdf_claims(graph, *, root_id, path, pdf_extractor=None,
                  max_claims=256, reviewer=None) -> dict
```

Extraction input is the result of `research.pdf.extract_pdf`:
`pages`, `sections`, `references`, `warnings`, `sha256`, `backend`.

Claim methods:

- `section_formal_statement`: a section whose heading is a formal statement
  kind; confidence high.
- `inline_formal_statement`: a formal statement marker inside page/section text.
- `abstract_claim`: cue sentences in abstract/conclusion/discussion.
- `contribution_claim`: “we propose / we show / our results” sentences.
- `definition`: “we define X as …” or “X is defined as …”.

Candidate fields: wording, raw_excerpt, claim_type, label, section, page,
source_locator, extraction_method, confidence, citation_hints, math_degraded,
semantics_verified=False.

Acceptance:

- A TEI fixture with theorem/definition sections yields high-confidence claims.
- Page-only text yields inline formal and abstract/contribution candidates with
  page/offset locators.
- References/bibliography sections are skipped.
- Long statements are bounded; duplicate wording is removed; max_claims
  truncation is reported.
- No PDF text is treated as a proof or formula interpretation.

## Stage B: ingestion

- Record a `Source` for the selected PDF (root/path/SHA/backend).
- Record `Claim` nodes linking `cites` to the Source.
- Record GROBID TEI references as `Reference` nodes with source provenance.
- Record citation hints on claims without inventing `cites` edges to references.
- Reuse an existing Source/claims for a duplicate PDF hash.
- Return source, claims, references, extraction metadata and warnings.

## Stage C: integration

- Service/MCP: `pdf_claim_preview`, `pdf_claim_ingest`.
- `pdf_preview` gains an optional `extract_claims` flag.
- `paper_review_start` accepts `.pdf` and uses PDF claim ingestion before
  creating the review plan/packet.
- Docs: `docs/research/pdf.md` and links.
- Tests with fake extractors; no live PDF/GROBID dependency.

## Results

Landed 2026-11-16.

**A — extractor.** `research/pdf_claims.py` extracts section formal statements,
inline formal statements, abstract claims, contribution claims and definitions
with page/section/offset locators, SHA-256, citation hints, confidence and
`math_degraded`. 11 tests passed.

**B — ingestion.** `ingest_pdf_claims` records a PDF Source, unresolved Claims
linked by `cites`, and GROBID TEI References; duplicate hashes reuse the Source
and existing candidates. Citation hints do not become reference edges.

**C — integration.** Service/MCP: `pdf_claim_preview`, `pdf_claim_ingest`,
and `pdf_preview` with `extract_claims`. `paper_review_start` accepts `.pdf`
and uses PDF claim ingestion before creating the plan and packet. A service-level
test covers preview → ingest → duplicate reuse → PDF paper review → bundle.

**Checks.** `1215 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No OCR.
- No equation reconstruction or formula truth claims.
- No automatic citation-key resolution from numeric brackets.
- No claim extraction from scanned/image-only PDFs without a text backend.
- No truth assessment.
