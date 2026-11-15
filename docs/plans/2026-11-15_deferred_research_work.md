# Deferred research work: PDF, semantic, citation graph, manuscript

**Date:** 2026-11-15. **Status:** stages A-D landed and integrated.

The reference/acquisition/review slices landed in
[`2026-11-15_reference_layer.md`](2026-11-15_reference_layer.md). This plan
covers the work deliberately deferred there. The modules are independent so
they can be implemented in parallel by separate agents; shared service/MCP/docs
integration happens after their APIs are reviewed.

## Stage A: PDF extraction (`research/pdf.py`)

**Goal:** turn a selected or fetched PDF into source-located text without
pretending layout or math is faithful.

- Prefer GROBID when `OZAGIA_GROBID_URL` is set: POST the PDF to
  `/api/processFulltextDocument`, parse the returned TEI with
  `research/tei.py`.
- Optional local backends: `pymupdf` if installed, then `pdftotext`.
- Return `{format, sha256, backend, pages, title, references, warnings,
  degraded_math, truth_assessed: False}`.
- Reject DTD/entity-bearing TEI, cap input size, and report unavailable
  backends clearly instead of silently returning empty text.
- No claim extraction; references from GROBID TEI may be recorded by the
  caller.

**Acceptance:** fake GROBID response parses through the TEI fixture; page text
from an injected runner is located and hashed; missing backends raise a typed
error; no new mandatory dependency.

## Stage B: Semantic/vector search (`research/semantic.py`)

**Goal:** optional semantic retrieval alongside FTS5, without a vector
database or a mandatory model.

- SQLite sidecar with passage vectors, model name and content fingerprint.
- Embedding sources: an injected callable, or an optional local
  `sentence-transformers` model named by `OZAGIA_EMBED_MODEL`. A deterministic
  hashed bag-of-words fallback is allowed but must be labelled `hash-bow`, not
  "semantic".
- Cosine similarity in Python; reciprocal-rank fusion with the existing FTS5
  results.
- Return engine/model labels, scores and source citations; no truth claim.
- No external API keys by default and no network calls in tests.

**Acceptance:** deterministic fallback indexes and retrieves; injected
embedder works; hybrid fusion de-duplicates passages and keeps FTS5 citations;
missing optional model reports clearly; no mandatory dependency.

## Stage C: Citation-graph traversal (`research/citation_graph.py`)

**Goal:** fetch provider-asserted references/citations and traverse the local
`cites_work` graph.

- OpenAlex first: `referenced_works` and cited-by works, bounded by `limit`.
- Record related works as `Reference` candidates, reusing
  `references.find_reference`/`record_reference`; add `cites_work` edges with
  provider, retrieval time, response hash and direction provenance.
- Direction: for references of A, `A --cites_work--> B`; for works citing A,
  `B --cites_work--> A`.
- Local neighbourhood/path traversal over recorded edges; no bulk mirroring.
- External provider traversal only when explicitly named; no support edges.

**Acceptance:** fake OpenAlex payloads produce bounded references/citations and
correct edge direction; repeated fetch does not duplicate edges; local
neighbourhood traversal returns deterministic nodes/edges; provider provenance
is retained.

## Stage D: Manuscript bundle (`research/manuscript.py`)

**Goal:** export a source-backed manuscript bundle and check its citations.

- Input: Markdown/LaTeX body with `[@key]` or `\cite{key}` markers, plus a
  title. Output a bundle under the state dir containing:
  - `manuscript.md`
  - `references.bib`
  - `claims.csv`
  - `checks.json`
- Checks: unresolved cite keys, retracted references, claims without a source
  locator, and references recorded but unused.
- Optional Pandoc/Quarto conversion via an injected runner or an installed
  `pandoc` executable; no shell interpolation, list arguments only.
- No automatic claim support; the bundle reports what is missing.

**Acceptance:** bundle contents are deterministic; citations resolve to
References; retracted/unresolved keys are reported; Pandoc is optional and
tested with a fake runner; writing defaults to the state dir.

## Agent ownership

| Stage | Agent scope | Must not edit |
|---|---|---|
| A | `src/ozagia/research/pdf.py`, `tests/test_research_pdf.py` | shared service/MCP/docs |
| B | `src/ozagia/research/semantic.py`, `tests/test_research_semantic.py` | shared service/MCP/docs |
| C | `src/ozagia/research/citation_graph.py`, `tests/test_research_citation_graph.py` | shared service/MCP/docs |
| D | `src/ozagia/research/manuscript.py`, `tests/test_research_manuscript.py` | shared service/MCP/docs |

Agents may read all existing modules. Integration (service operations, MCP
schemas, `docs/research.md`, `README.md`, `pyproject.toml` extras) is done after
review to avoid concurrent edits.

## Integration after agents

- Add service operations: `pdf_preview`, `semantic_search`,
  `ref_citation_fetch`, `ref_citation_neighborhood`, `manuscript_bundle`,
  `manuscript_check`.
- Add MCP schemas and CLI choices.
- Add optional extras only when the code genuinely needs them; keep the core
  standard library only.
- Document model/provider opt-ins and the no-paywall/no-truth boundaries.
- Run `ruff check src tests` and the full pytest suite.

## Results

Landed 2026-11-15.

**A — PDF.** `research/pdf.py` supports GROBID via `OZAGIA_GROBID_URL`,
optional PyMuPDF and Poppler `pdftotext`, with page records, SHA-256,
`degraded_math`, warnings and TEI references. `research_pdf_preview` reads a
selected `.pdf` or a cached fetched Source and can record GROBID references.
13 tests passed, 1 optional-PyMuPDF test skipped.

**B — Semantic.** `research/semantic.py` builds a SQLite vector sidecar and
fuses cosine ranking with FTS5 via weighted reciprocal-rank fusion. The default
`hash-bow` is explicitly labelled as a lexical baseline; a local
`sentence-transformers` model is opt-in and offline-only. 21 tests passed.

**C — Citation graph.** `research/citation_graph.py` fetches bounded OpenAlex
references/citing works, records `cites_work` edges with provider provenance,
and traverses the local graph. Direction and idempotence are tested. 10 tests
passed.

**D — Manuscript.** `research/manuscript.py` writes `manuscript.md`,
`references.bib`, `claims.csv` and `checks.json`, reports unresolved/retracted
citations and unsourced claims, and runs optional list-argument Pandoc
conversion. 8 tests passed.

**Integration.** Service/MCP operations added: `pdf_preview`,
`semantic_build`, `semantic_search`, `ref_citation_fetch`,
`ref_citation_neighborhood`, `ref_citation_path`, `manuscript_check` and
`manuscript_bundle`. A service-level integration test covers PDF (selected and
cached), hybrid search, citation traversal and manuscript export with a fake
Pandoc runner.

**Checks.** `946 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No bundled Google Scholar or paywall-bypass adapter.
- No claim extraction from PDFs and no claim that PDF math is faithful.
- No mandatory embedding model, vector service or network call.
- No automatic `supports` edge from a citation or a language model.
- No bulk citation-graph mirroring.
