# PDF OCR and citation resolution

**Date:** 2026-11-16. **Status:** stages A-C landed and integrated.

PDF claim extraction currently depends on an existing text layer. This plan
adds optional OCR for image-only/low-text PDFs and best-effort resolution of
numeric, author-year and DOI/arXiv citation hints to recorded References.

Both are evidence-producing aids, not truth claims. OCR text is marked as OCR;
citation links carry method, confidence and `requires_review`.

## Principles

- OCR only when needed: empty or sparse text pages, or an explicit OCR request.
- Prefer an existing text layer; never OCR a good text PDF by default.
- OCR is a local external tool (`ocrmypdf`, `tesseract` + Poppler or PyMuPDF).
  No network, no bundled model.
- Citation resolution is a candidate link. Numeric brackets depend on
  bibliography order and are lower confidence than DOI/arXiv matches.
- Unresolved hints stay visible; no fabricated references.
- Resolution never creates `supports` edges.

## Stage A: OCR (`research/ocr.py`)

API:

```python
ocr_pdf(data, *, backend="auto", lang="eng", dpi=300,
        runner=None, pymupdf_module=None, max_pages=None) -> dict
text_is_sparse(pages, min_chars=20) -> bool
```

Backends:

- `ocrmypdf`: run `ocrmypdf --skip-text --output-type pdf <in> <out>`, then
  extract text with PyMuPDF or `pdftotext`.
- `tesseract`: render pages with PyMuPDF if available, otherwise
  `pdftoppm -r <dpi> -png`; run `tesseract <image> stdout -l <lang>`.
- `auto`: try `ocrmypdf`, then `tesseract`; report a typed unavailability error
  if neither is installed.

Return page records with 1-based numbers, OCR marker, language, backend,
warnings and `degraded_math=True`. Bound pages and bytes.

Acceptance:

- Injectable runners test both backends without real OCR tools.
- Sparse text detection works.
- Missing backends raise clearly.
- No network; no claim extraction inside OCR.

## Stage B: citation resolution (`research/citation_resolution.py`)

API:

```python
resolve_claim_citations(graph, claim_id, *, apply=True,
                        threshold=0.6) -> dict
citation_resolution_report(state, claim_id=None) -> dict
resolve_text_citations(text, references) -> list[dict]
```

Resolution methods:

- DOI/arXiv literal in text -> exact identifier match, confidence high.
- Numeric `[12]`, `[1,3-5]` -> Reference with matching bibliography index for
  the same PDF Source, confidence medium; requires review.
- Author-year `(Smith, 2020)`, `Smith et al. (2020)` -> last-name/year match,
  confidence medium/low; requires review.
- Unmatched hints remain unresolved.

Edges:

- `Claim --cites--> Reference` with provenance containing method, hint,
  confidence and `requires_review`.
- Duplicate edges are reused, not repeated.
- No `supports`/`opposes` edges.

Acceptance:

- Numeric ranges expand correctly.
- Bibliography order is read from `bibliography_index` on References.
- Author-year matching is conservative and reports ambiguity.
- Apply=False reads only.
- Report counts resolved/unresolved by method.

## Stage C: integration

- `pdf.py` gains `ocr="auto"|True|False` fallback after a sparse extraction and
  an explicit `backend="ocr"`.
- `pdf_claims.ingest_pdf_claims` records bibliography indices and attempts
  citation resolution by default (`resolve_citations=True`), returning a
  resolution summary.
- Service/MCP: `pdf_preview` OCR args, `pdf_claim_ingest` `resolve_citations`,
  `citation_resolve`, `citation_resolution_report`.
- Docs: update `docs/research/pdf.md`.
- Tests use fake OCR runners and fake PDF extractors; no external tools.

## Results

Landed 2026-11-16.

**A — OCR.** `research/ocr.py` supports `ocrmypdf` and `tesseract`, sparse-text
detection, page/lang provenance, bounded pages and injectable runners. `pdf.py`
now defaults to `ocr="auto"` for sparse pages and supports explicit
`backend="ocr"`. 12 tests passed.

**B — citations.** `research/citation_resolution.py` resolves DOI/arXiv
literals, numeric bibliography indices and author-year hints, with confidence,
ambiguity, `requires_review` and `cites` provenance. 13 tests passed.

**C — integration.** PDF ingestion records bibliography indices and attempts
resolution by default. Service/MCP add `citation_resolve`,
`citation_resolution_report`, OCR arguments on `pdf_preview`, and
`resolve_citations` on `pdf_claim_ingest`. Tests cover sparse-text OCR fallback,
explicit OCR, numeric resolution through PDF ingestion, duplicate reuse and
paper-review bundles.

**Checks.** `1242 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No guarantee that OCR text is correct.
- No equation reconstruction.
- No automatic acceptance of numeric citation matches without review.
- No network OCR/model service.
- No truth or support verdicts.
