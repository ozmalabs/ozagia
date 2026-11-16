# Optional research mode

Ozagia can hold source-backed claims, evidence, assumptions, notation,
derivation links and model candidates alongside its coding tools. Research
tools are opt-in; the default MCP server remains coding-only.

```bash
ozagia-mcp --root /path/to/workspace --mode research
ozagia-mcp --root /path/to/workspace --mode combined --autobuild
```

`OZAGIA_MODE` also selects `coding`, `research`, or `combined`. Research-only
does not require a development graph snapshot. Combined mode exposes both tool
sets, including `research_dev_context`, which retrieves coding and research
context separately for a session and task. Retrieval results are not asserted
to support one another; review and explicitly link relevant evidence.

Start with `research_init` and configure explicit source roots and files. Use
`research_paper_preview` / `research_paper_ingest` for selected TeX or Markdown
sources; recorded spans preserve hashes and candidate provenance. Graph nodes
and relationships can be inspected, linked and traced. The paper reader
extracts bounded formula candidates and selected scoped declarations; it does
not establish the truth of claims or completely interpret arbitrary papers.

`research_ref_preview` / `research_ref_import` parse a selected BibTeX, BBL,
CSL-JSON or RIS file into bibliographic `Reference` nodes. A `Reference` is the
work; a `Source` is a local or cached artifact of it. `paper_preview` and
`paper_ingest` parse an embedded `thebibliography`, or an explicitly selected
`bibliography_path`, and link each claim's `\cite` keys to References. The link
is author-written citation metadata, not evidence of support. `research_ref_check`
reports unresolved keys, duplicates, metadata gaps and recorded retractions;
`research_ref_export` writes BibTeX or CSL-JSON. Citation keys can be linked
later with `research_ref_link_citations`.

`research_ref_resolve` and `research_fetch_preview` use documented open
metadata/API providers (arXiv, Crossref, OpenAlex, Unpaywall and Europe PMC).
`research_fetch_apply` downloads one selected open-access artifact into a
content-addressed cache under `.ozagia/research/cache`, verifies its hash and
records a `Source` linked to its Reference. Fetch is open-access only; it does
not bypass paywalls. PDF/TEI reading is limited to structured text and
references; `research_tei_preview` reads selected TEI/JATS and can record its
bibliography.

External providers, including unofficial scrapers, are never enabled by
default. A separate package can register one through
`OZAGIA_RESEARCH_PROVIDERS` or the `ozagia.research.providers` entry-point
group, and an operation must name it (`research_external_search`,
`research_ref_resolve`, or `research_fetch_preview`). See
[optional external providers](research/providers.md).

`research_search` uses SQLite FTS5 BM25 over selected local passages when
available, with the older lexical index as fallback. `research_cite_context`
extracts sentence/line citation contexts as unclassified candidates;
`research_evidence_table` projects claims and evidence into Markdown or CSV.
`research_screen` records reviewable title/abstract or full-text decisions, and
`research_screening_report` counts them without inferring inclusion.

`research_pdf_preview` extracts page text from a selected PDF or a cached
fetched Source. GROBID is used when `OZAGIA_GROBID_URL` is set; otherwise an
installed PyMuPDF or Poppler `pdftotext` is used. Sparse text is retried with
optional OCR (`ocrmypdf` or Tesseract); results are marked `degraded_math` and
PDF equations are not reconstructed. `research_pdf_claim_preview` extracts
formal, abstract, contribution and definition candidates, and
`research_pdf_claim_ingest` records a PDF Source, unresolved claims and GROBID
references. Citation hints are resolved best-effort to References with method,
confidence and `requires_review`; unresolved hints remain visible. GROBID's TEI
bibliography can be recorded as References. See
[PDF extraction](research/pdf.md).

`research_semantic_build` / `research_semantic_search` provide an optional
SQLite vector sidecar and hybrid reciprocal-rank fusion with FTS5. The default
`hash-bow` projection is a deterministic lexical baseline, not a semantic
model. A local `sentence-transformers` model can be named explicitly or with
`OZAGIA_EMBED_MODEL`; it is loaded offline and is never downloaded implicitly.
An injected embedder is supported by the Python API.

`research_ref_citation_fetch` asks OpenAlex for bounded references or citing
works and records `cites_work` edges between References with provider and
response provenance. `research_ref_citation_neighborhood` and
`research_ref_citation_path` traverse only recorded edges. A provider citation
is a bibliographic relation, not evidence that either work supports a claim.

`research_manuscript_check` reports unresolved citations, retracted references,
claims without source locators and unused references. `research_manuscript_bundle`
writes `manuscript.md`, `references.bib`, `claims.csv` and `checks.json` under
the state directory (or an explicit output directory). Pandoc conversion runs
only when requested, with list arguments and no shell; Pandoc is optional.

`research_review_plan_create` records a structured review plan with roles,
models, criteria and a minimum reviewer count. `research_review_packet` builds a
deterministic evidence packet; `research_review_run_record` records one
independent model/role run and its validated findings. `research_review_agreement`
reports Fleiss' kappa, Jaccard, severity agreement, consensus and dissent;
`research_review_reconcile` preserves dissent in a recorded reconciliation.
`research_argument_brief` / `research_argument_record` / `research_argument_check`
build steelman, red-team or synthesis arguments with premise/evidence
obligations. Multi-model execution is a separate harness driver so the core
never calls a model. See [structured review](research/review.md).

`research_position_record` stores a user's stated stance or correction as a
`Holding` sourced to the conversation (session id, date, an optional quote the
user approved), linked to the records and documents it concerns;
`research_position_supersede`, `research_position_retract` and
`research_position_list` manage it. `research_context` reports matching active
positions with their dates in a separate `user_positions` block, never among
the sourced results. `research_review_finding_add` keeps review findings in a
ledger (document, section anchor, severity, status, history with session ids);
`research_review_finding_status` changes a status with a note,
`research_review_outstanding` lists open findings by document and
`research_review_rereview` proposes which may be addressed in a new version by
checking whether their anchored text changed. See
[positions and the review ledger](research/positions.md).

`research_extraction_record` / `research_extraction_table` store comparable
study fields and effect sizes with source locators. `research_risk_of_bias_record`
and `research_certainty_record` record reviewer appraisals and GRADE-like
ratings. `research_contradiction_scan` reports deterministic contradiction
candidates and `research_alert_scan` reports retractions, unresolved citations
and new citation edges recorded since a review plan. These are candidates and
alerts, not verdicts; scans read metadata only. See
[synthesis](research/synthesis.md).

`research_theory_record` records statements, assumptions, predictions and
status. `research_theory_rivalry` and `research_theory_compatibility` record
rivals and graded compatibility (`compatible`, `partially_compatible`,
`incompatible`, `orthogonal`). `research_theory_compare` reports shared and
unique evidence, overlap, predictions/tests and an explicit discernment status
including `shared_only`, `unique_but_not_discriminating` and
`discriminating_evidence_present`. `research_theory_scan` reports incomplete
theories and non-discriminating rivalries. See [theory competition](research/theory.md).

`research_source_register` records books, notes, websites, articles, datasets,
software and other formats with format-specific metadata. `research_holding_record`
records how a source is held: evidence, interpretation, hypothesis, method,
background, personal note and related stances. Books default to authorial
interpretation rather than established fact; notes default to personal context;
websites default to background with access/archive constraints.
`research_document_claim_preview` / `research_document_claim_ingest` parse
Markdown/text notes, HTML website snapshots and EPUB books into Source,
Reference, Holding and claim candidates. See
[source formats](research/sources.md).

`research_paper_review_start` ingests a selected paper, creates a review plan
targeting its Source and builds a paper-level packet. The harness
`run_paper_review` runs independent analysis roles plus advocate/critic/
synthesizer argument runs per main claim, reconciles findings and can write a
bundle containing `review.md`, `review.json`, claims/evidence CSVs, BibTeX,
full arguments and checks. `research_paper_review_report` and
`research_paper_review_bundle` expose the structured result. See
[paper review](research/paper_review.md).

`research_formula_resolve` compares interpretations using the graph's scoped
symbol context; `research_formula_model` can use the resulting selection to
record a native model with links to its context. Missing or conflicting
evidence remains visible. Models are interpretations, and the native execution
backend operates only on its supported Term subset. `research_export_lean`
exports supported definitions; `research_check_lean` uses a locally installed
Lean (`OZAGIA_LEAN` or `PATH`) to check generated declarations. That does not
prove fidelity to the source paper.

The graph, source index, paper ingestion, PDF backends and Lean export use the
Python standard library or optional local executables. PyMuPDF
(`pip install pymupdf`), `sentence-transformers`, Poppler `pdftotext`, GROBID and
Pandoc are optional and are reported as unavailable when absent. Install
`ozagia[research]` to enable optional SymPy/Lark parse proposals. Native projection, graph-context resolution and native
execution additionally require a separately installed compatible `substrate_rs`
runtime. Its absence is reported when those operations are invoked; it is not
required to use coding mode or maintain a research ledger. A converter is not
bundled: optional LaTeXML XML is read only when a selected artifact is supplied.
For a local Ocelot source checkout, the backend can be installed into the same
environment with `python -m pip install /path/to/ocelot/runners/dsl` (which builds
the Rust extension); Ozagia then imports the installed runtime, not the target
project. Backend provenance is reported separately and is not independently
verified against an Ocelot source tree.

Research sessions are kept under `.ozagia/research` in the workspace (honoring
`OZAGIA_STATE_DIR`); existing Ocelot sessions in `var/ocelot_research` are read
as legacy sessions. Only configured and explicitly selected source files are
opened. Source references are checked against hashes, and external source
repositories are not modified.

The [real-paper port evaluation](results/research_paper_real_paper.json)
records a source-hashed example of scoped notation, two explicit native
domain choices and Lean checks. It is a single demonstration rather than a
coverage estimate. Reproduce it with `python -m ozagia.eval.research_paper
--paper /path/to/main.tex`; it requires the example paper, native backend and
Lean to exercise every part.
