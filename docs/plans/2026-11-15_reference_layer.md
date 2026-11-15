# Reference layer, acquisition and evidence review

**Date:** 2026-11-15. **Status:** slices 1-3 landed; acquisition and review
adapters are read-only and remain optional.

The research graph is good at source-hashed claims, formulas and mechanical
checks, but it has no bibliographic identity: a `Source` is a local file,
`\cite{key}` is a string, paper ingestion accepts only TeX/Markdown, and
retrieval is term counting. This plan adds the missing plumbing without
turning Ozagia into a reference manager, a PDF reader or a citation-style
engine.

## Principles

- A bibliographic **Reference** is not a local **Source**. The work, its
  versions and the artifact read are separate identities.
- Imported metadata and extracted text are unverified inputs. They never
  establish truth or support by themselves.
- Full text stays out of the append-only graph; passages live in a SQLite FTS5
  sidecar keyed by content hash.
- Prefer open-access provider APIs and arXiv/PMC structured sources over PDF.
  A PDF is the last resort, and degraded math is reported as degraded.
- Third-party MCP servers and tools may discover or read. Only Ozagia
  operations write graph records, so there is one audit trail.
- The core remains standard library only. Network, PDF and Zotero adapters are
  optional and injectable.
- Scraping and other unofficial services are never bundled or enabled by
  default. An external package may register a provider through an explicit
  opt-in registry; Ozagia calls it only when the user names it. Users remain
  responsible for the provider's terms and rate limits.

## Slice 1: offline reference layer

**Goal:** import a paper's bibliography, resolve `\cite` keys, export a
bibliography, and mechanically check citation integrity. No network.

- Add `Reference` node type and `manifestation_of`, `cites_work`, `same_as`
  and `screens` edges. Extend `cites` to target `Reference`.
- Add append-only `ResearchGraph.amend()` so enrichment adds an event rather
  than rewriting history. Schema version 2 accepts version-1 graphs.
- `research/references.py`:
  - BibTeX/BibLaTeX parser with `@string`, `crossref`, brace/quote values.
  - `.bbl` / `thebibliography` parser (offline, unverified metadata).
  - CSL-JSON and RIS import.
  - BibTeX and CSL-JSON export; stable citekey generation and aliases.
  - DOI/arXiv/PMID/PMCID/ISBN/OpenAlex normalization and dedupe.
  - `check_references()`: unresolved keys, duplicate identities, missing
    metadata, retractions, sources without references.
- Extend `paper_preview`/`paper_ingest` to parse the source bibliography (or an
  explicitly selected `.bib`/`.bbl` via `bibliography_path`), record
  `Reference` nodes, and link each claim's citation keys to them.
- New operations: `ref_preview`, `ref_import`, `ref_record`, `ref_export`,
  `ref_check`.

**Acceptance:** a `.tex` fixture with `thebibliography` produces claims linked
to `Reference` nodes; a `.bib` fixture round-trips; unresolved and retracted
references are reported; existing research tests still pass.

## Slice 2: open-access acquisition

**Goal:** resolve an identifier to metadata and fetch an open-access artifact
into a content-addressed cache, with provenance and no paywall bypass.

- `research/fetch.py`:
  - Metadata adapters: arXiv Atom, Crossref, OpenAlex, Unpaywall, Europe PMC.
  - Two-phase `preview_fetch()` / `apply_fetch()`.
  - Source priority: arXiv e-print TeX, PMC/Europe PMC JATS, HTML, PDF.
  - Content-addressed cache under the state dir; SHA-256, media type, license,
    URL, provider and retrieval time recorded on the `Source`.
  - SSRF guard: HTTP(S) only, no credentials/private addresses, redirect and
    size limits.
- `research/providers.py`: opt-in external provider registry. Default providers
  are the documented open APIs only. External packages register through the
  `ozagia.research.providers` entry-point group, or an explicit
  `OZAGIA_RESEARCH_PROVIDERS=module:attr` list; nothing is auto-enabled.
  A Google Scholar or other scraping adapter can live in a separate package
  and is used only when named.
- `research/zotero.py`: read-only Zotero local-API item import; Better BibTeX
  citekeys when present. No writes to the library.
- `research/tei.py`: GROBID/PMC TEI parser for title, sections, references and
  citation targets. Structured text only; no claim truth.
- New operations: `ref_resolve`, `fetch_preview`, `fetch_apply`,
  `zotero_import`, `tei_preview`.

**Acceptance:** mocked provider responses produce fetch candidates; applying a
candidate writes a hashed cached artifact and records `Source`; private URLs
are rejected; a TEI fixture yields sections and references; Zotero fixture
items map to references without modifying the library.

## Slice 3: retrieval, evidence and review

**Goal:** search the selected corpus with BM25, expose citation contexts, and
project claims into reviewable evidence tables and screening counts.

- `research/search.py`: SQLite FTS5 index over selected passages, BM25 ranking,
  snippets and filters; fallback to the existing lexical index when FTS5 is
  unavailable.
- Citation-context extraction from TeX `\cite` and Markdown `[@key]` with
  sentence/line locators; candidate only.
- `research/review.py`:
  - Evidence table projection (Markdown/CSV) from claims, evidence and
    references.
  - Screening decisions (`Decision --screens--> Reference`) with
    title/abstract and full-text stages, reasons and reviewer; PRISMA-style
    counts.
  - Retraction/concern warnings from recorded metadata.
- New operations: `search`, `cite_context`, `evidence_table`, `screen`,
  `screening_report`.

**Acceptance:** FTS5 search returns cited snippets; citation contexts resolve
keys without asserting support; an evidence table includes source locators;
screening counts add up per stage.

## Non-goals

- No Zotero/library writes, no reference-manager UI, no CSL rendering.
- No Sci-Hub, bundled Google Scholar scraper or paywall bypass. An external,
  explicitly enabled provider may wrap an unofficial service; it is not part
  of Ozagia's install or default provider set.
- No automatic `supports` edge from a citation context or an LLM judgement.
- No vector database before FTS5 is measured.
- No claim that an imported PDF/TeX statement is true or that its math was
  parsed faithfully.

## Checks

```
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## Results

Landed 2026-11-15.

**Slice 1.** `Reference` is a graph node; `manifestation_of`, `cites_work`,
`same_as` and `screens` are edges; `cites` accepts a `Reference` target.
`ResearchGraph.amend()` adds append-only enrichment events, and schema version
2 accepts version-1 graphs. `research/references.py` parses BibTeX/BibLaTeX,
`.bbl`/`thebibliography`, CSL-JSON and RIS; normalizes DOI/arXiv/PMID/PMCID/
ISBN/OpenAlex identifiers; deduplicates; exports BibTeX and CSL-JSON; checks
unresolved keys, duplicates and recorded retractions. Paper ingestion records
References and links claim citation keys.

**Slice 2.** `research/fetch.py` resolves arXiv, Crossref, OpenAlex, Unpaywall
and Europe PMC metadata and previews/applies open-access artifacts into a
content-addressed state cache with SHA-256, license, provider and retrieval
time. The fetch URL guard rejects non-HTTP, credentialed and private-network
URLs and caps response size. `research/zotero.py` reads the local API
read-only. `research/tei.py` parses TEI/JATS sections, references and citation
targets. `research/providers.py` is the explicit opt-in registry; no external
or unofficial provider is enabled by default.

**Slice 3.** `research/search.py` builds a SQLite FTS5 index and ranks with
BM25, falling back to lexical retrieval when FTS5 is unavailable.
`citation_contexts()` extracts sentence/line candidates and never creates a
support edge. `research/review.py` projects evidence tables to Markdown/CSV,
records screening decisions and reports PRISMA-style counts.

**Checks.** `890 passed, 36 skipped`; `ruff check src tests` passes. New tests
cover parsing, dedupe/amendment, citation links, fetch guards and cache
records, Zotero mapping, TEI, provider opt-in, FTS5 search, evidence tables and
screening.

**Deliberately deferred.** PDF text/layout extraction (the TEI adapter accepts
GROBID/PMC output; it does not run GROBID), semantic/vector retrieval,
provider-backed citation-graph traversal, manuscript export through Pandoc/Quarto,
and any bundled or default-enabled scraper. A Google Scholar adapter belongs in
a separate package and must be named explicitly; see
[providers](../research/providers.md).
