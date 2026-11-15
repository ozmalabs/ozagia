# PDF text and claim extraction

Ozagia reads PDFs through GROBID when `OZAGIA_GROBID_URL` is set, otherwise
through optional PyMuPDF or Poppler `pdftotext`. Text extraction is lossy:
tables, figures and equations are marked `degraded_math`.

## OCR

`ocr="auto"` is the default: when the extracted pages are empty or sparse,
Ozagia retries with OCR. Explicit `backend="ocr"` forces it. Backends are
`ocrmypdf` first, then `tesseract` with PyMuPDF-rendered pages or Poppler
`pdftoppm`. OCR results are marked `ocr=true` with the OCR backend and language.
Missing tools are reported as warnings rather than silently returning an empty
paper.

No OCR tool is bundled. Install `ocrmypdf` or Tesseract plus Poppler/PyMuPDF,
and check the language data available on the machine.

## Text preview

`research_pdf_preview` accepts a selected `.pdf` or a cached fetched Source and
returns page text or GROBID TEI sections, references and warnings. With
`extract_claims=true` it also returns claim candidates.

## Claim candidates

`research_pdf_claim_preview` extracts candidates without recording:

- **section formal statements** — sections headed Theorem, Lemma, Proposition,
  Corollary, Definition, Claim, Hypothesis or Conjecture;
- **inline formal statements** — the same markers inside page or section text;
- **abstract claims** — cue sentences in abstract/conclusion/discussion;
- **contribution claims** — “we propose / we show / our results” sentences;
- **definitions** — “we define X as …” or “X is defined as …”.

Each candidate carries a page/section/offset locator, PDF SHA-256, extraction
method, confidence, raw excerpt, citation hints and `math_degraded`. References
and bibliography sections are skipped. Candidates are unresolved and
`semantics_verified=false`; PDF text is not treated as a proof or formula
interpretation.

Citation hints such as `[12]` or `(Author, 2020)` are stored as text. PDF
ingestion attempts resolution automatically:

- DOI/arXiv literals match identifiers exactly;
- numeric brackets and ranges match `bibliography_index` from the same PDF,
  with medium confidence;
- author-year hints match last name and year, with medium/low confidence and
  ambiguity reporting.

Resolved links are `Claim --cites--> Reference` with method, confidence,
hint and `requires_review` provenance. Low-confidence matches stay candidates;
unresolved hints remain visible. `research_citation_resolve` can re-run
resolution for a claim, and `research_citation_resolution_report` counts
resolved/unresolved hints.

## Ingestion

`research_pdf_claim_ingest` records:

- a `Source` for the selected PDF;
- unresolved `Claim` nodes linked by `cites` to the Source;
- GROBID TEI references as `Reference` nodes with PDF provenance.

Re-running the same PDF hash reuses the Source and existing candidate claims.
This is the path used by `paper_review_start` for `.pdf` papers.

## Limits

- OCR text is approximate; equations, tables and columns can be wrong.
- No equation reconstruction or formula truth claims.
- Numeric citation resolution depends on bibliography order; it is marked
  `requires_review`.
- No automatic claim verification.
- Multiple columns and tables can be reordered by the text/OCR backend.
