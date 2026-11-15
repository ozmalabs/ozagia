# Source formats and epistemic holdings

**Date:** 2026-11-16. **Status:** stages A-C landed and integrated.

The graph currently treats every local artifact as a `Source` and every cited
work as a `Reference`. It does not distinguish a peer-reviewed article from a
book thesis, a personal note or a website, and it has no place to record how a
source should be held.

That distinction matters most for books. A book can contain strict science, but
it is often a mixture of evidence and the author's interpretation. The right
default is not “asserted fact” but “authorial argument/hypothesis”, usable as a
useful interpretation unless specific evidence is extracted. Notes and websites
need similarly explicit defaults.

## Principles

- Format is not authority. A book, note, website, article and dataset each have
  different metadata and different default epistemic risks.
- A holding is a reviewer-recorded stance: evidence, interpretation, hypothesis,
  method, background, normative claim, speculation, mixed, personal note,
  primary/secondary source.
- Defaults are conservative; a reviewer can upgrade a book to evidence only
  with a recorded rationale/scope.
- Multiple holdings can exist for one Source (for example, a book held as
  interpretation for one claim and as a source of a formal definition for
  another).
- Notes are personal context, not external evidence, unless explicitly linked
  to external support.
- Websites are date-sensitive and mutable; record URL, access date and archive
  where possible.
- No automatic truth verdict.

## Graph types

Schema version 6 adds `Holding` and `held_as` (Source -> Holding).

Holding fields: source_id, source_type, epistemic_role, authority,
review_status, use_constraints, scope, rationale, reviewer, status,
truth_assessed.

## Stage A: holdings (`research/holdings.py`)

API:

```python
record_holding(graph, *, source_id, source_type, epistemic_role=None,
               authority=None, review_status=None, use_constraints=None,
               scope=None, rationale=None, reviewer=None, status="proposed")
check_holding(state, holding_id)
holding_report(state, source_id=None)
claim_holding(state, claim_id)
list_source_types()
```

Defaults:

- book -> interpretation/mixed, editorial, `treat_as_author_argument`.
- note -> personal_note, personal, `not_external_evidence`.
- website -> background, unknown, `check_accessed_date`, `archive_snapshot`.
- article -> reported_evidence, unknown review status (do not assume peer review).
- dataset/software/standard/report/thesis/preprint/interview/lecture/video.

Acceptance:

- Source existence and format validation.
- Book held as evidence without rationale/scope warns.
- Note held as evidence is flagged.
- Website missing accessed date/archive warns.
- Accepted holding requires a reviewer.
- Multiple holdings per Source are allowed; report and claim context are
  deterministic.

## Stage B: format metadata (`research/source_formats.py`)

API:

```python
normalise_source_metadata(source_type, metadata) -> dict
register_source(graph, *, source_type, metadata, root_id=None, path=None,
                url=None, sha256=None, reference=None, holding=None,
                reviewer=None) -> dict
```

Format profiles:

- book: title, authors, year, publisher, edition, isbn, doi, language, pages.
- note: title/author/date/context/notebook/page/confidentiality.
- website: url, site, author, published_at, accessed_at, archive_url, license.
- article, dataset, software, standard, report, thesis, preprint, interview,
  lecture, video.

Acceptance:

- Required fields per format; DOI/ISBN/URL/date normalisation.
- `register_source` creates/amends a bibliographic `Reference` when
  appropriate, a `Source` artifact, and a `Holding` with defaults/overrides.
- Notes may omit a Reference; books/websites should carry one when metadata is
  sufficient.
- No file or network reads.

## Stage C: document text and claims (`research/document_claims.py`)

API:

```python
parse_note(text, *, title=None) -> dict
parse_html(data, *, url=None) -> dict
parse_epub(data, *, max_bytes=...) -> dict
candidate_claims_from_document(document, *, max_claims=256) -> dict
```

Acceptance:

- Notes: Markdown headings -> sections; plain text -> one section.
- HTML: stdlib parser; title, h1-h3 sections, paragraphs, skip scripts/styles/
  nav/footer; URL/accessed metadata.
- EPUB: ZIP container/OPF/spine; title/authors; chapter XHTML; zip-bomb and
  path-traversal guards.
- Candidate claims reuse the PDF claim extractor over the parsed
  sections/pages, with format-specific backend labels.
- No network reads.

## Integration

- Service/MCP: `holding_record`, `holding_check`, `holding_report`,
  `claim_holding`, `source_register`, `document_claim_preview`,
  `document_claim_ingest`.
- `source_register` supports book/note/website metadata and default holdings.
- `document_claim_ingest` records Source/Claims/Holding for selected note,
  HTML/website snapshot or EPUB book.
- Docs: `docs/research/sources.md` and links.
- Tests with local fixtures; no network.

## Results

Landed 2026-11-16.

**A — holdings.** `research/holdings.py` defines 15 source types and default
epistemic stances. Books default to authorial interpretation, notes to personal
context, websites to background with access/archive constraints. Multiple
holdings per Source, checks, reports and claim holding context. 38 tests passed.

**B — formats.** `research/source_formats.py` defines metadata profiles,
normalisation and `register_source` (Reference + Source + Holding, duplicate
reuse). 25 tests passed.

**C — documents.** `research/document_claims.py` parses Markdown notes,
HTML/website snapshots and EPUB books, with ZIP/entity/path guards, then reuses
PDF claim candidates. 17 tests passed.

**Integration.** `document_ingest.py` records Source/Reference/Holding/claims
for notes, websites and EPUBs. Schema version 6 adds `Holding` and `held_as`.
Service/MCP operations: `source_types`, `source_register`, `holding_record`,
`holding_check`, `holding_report`, `claim_holding`,
`document_claim_preview`, `document_claim_ingest`. `paper_review_start` accepts
EPUB/HTML/TXT as well as TeX/Markdown/PDF. A service-level test covers book
registration, default book interpretation, note/website/EPUB ingestion and claim
holding context.

**Checks.** `1353 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No automatic format detection by content alone.
- No claim that a book thesis is true or behaviorally established.
- No web crawling or network fetch in this stage (URL metadata only).
- No OCR/EPUB DRM handling.
- No automatic upgrade of a holding to evidence.
