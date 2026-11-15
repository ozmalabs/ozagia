# Source formats and epistemic holdings

Not every source should be held the same way. A peer-reviewed article, a book,
a personal note and a website have different metadata, different failure modes
and different default authority.

A book can contain strict science, but it is often a mixture of evidence and
the author's interpretation. The default holding for a book is therefore
**authorial interpretation**, not established fact. A reviewer can upgrade it
to evidence only with a recorded scope and rationale, and specific extracted
evidence remains preferable.

## Source types

`research_source_types` lists 76 supported formats and defaults:

- **Books/reference:** book, chapter, reference_work, textbook, encyclopedia.
- **Research artifacts:** protocol, preregistration, registered_report,
  analysis_plan, lab_notebook, field_notes, computational_notebook, workflow,
  database, model, benchmark, ontology.
- **Articles/preprints:** article, preprint, thesis, report, conference_abstract,
  poster, slides, lecture, video, podcast, webinar.
- **Normative/institutional:** standard, regulation, statute, court_case,
  guideline, consensus_statement, patent, ethics_approval, consent_form,
  data_use_agreement, funding, conflict_of_interest.
- **Meta-science:** peer_review, editorial_decision, author_response,
  correction, retraction_notice.
- **Human/grey:** interview, oral_history, focus_group, survey, questionnaire,
  news, blog, press_release, wiki, forum, social_post, archival_document,
  letter, note, website, fiction, film, art.
- **Data/material:** dataset, software, specimen, sample, assay,
  instrument_output, image, spectrum, sequence, structure, synthetic_data,
  model_output.
- **Other.**

Format profiles define required and optional metadata. Examples:

```text
book:     title, authors, year/publisher, edition, ISBN, DOI, language, pages
note:     title or context, author, date, notebook, page, confidentiality
website:  url, accessed_at, site, author, published_at, archive_url, license
article:  title, authors, year, venue, DOI/PMID/arXiv
dataset:  title, repository, DOI, version, license
software: name, repository, version/commit, license
```

`research_source_register` records a `Reference` where appropriate, a `Source`
artifact and a `Holding`. Notes normally have no bibliographic Reference.

## Holdings

A `Holding` records how a reviewer holds a source:

- `epistemic_role`: evidence, interpretation, hypothesis, method, background,
  normative, legal, governance, plan, review_artifact, integrity_event,
  primary_source, primary_data, secondary_source, reported_evidence,
  technical_claim, synthetic, candidate, context, evaluation, speculation,
  mixed, personal_note
- `authority`: peer_reviewed, editorial, institutional, regulatory, legal,
  standard_body, repository, self_published, personal, unknown
- `review_status`: peer_reviewed, editorial, none, unknown
- `use_constraints`: free-form constraints such as
  `treat_as_author_argument`, `not_external_evidence`, `check_accessed_date`,
  `archive_snapshot`, `not_result_evidence`
- scope, rationale, reviewer, status (`proposed`, `accepted`, `rejected`),
  confidence

Multiple holdings per Source are allowed. For example, a book can be held as
an interpretation for its thesis and as a source of a formal definition for a
different claim.

`research_holding_check` flags:

- a book held as evidence without a scope/rationale;
- a note held as evidence;
- a website without an access date or archive;
- a book without publisher/year metadata;
- a protocol/preregistration/analysis plan held as result evidence;
- standards, regulation, law or patents held as empirical evidence;
- synthetic data or model output held as evidence;
- datasets, software, models, benchmarks or notebooks missing
  version/license/checksum metadata;
- legal/standard sources missing jurisdiction or issuing body;
- interviews, surveys or focus groups missing consent/context;
- news, blogs, press releases, wikis, forums or social posts missing access
  date/archive;
- peer review, editorial decisions, corrections or retraction notices missing a
  target;
- specimens, samples, assays or instrument output missing chain-of-custody,
  calibration or instrument metadata;
- an accepted holding without a reviewer.

Metadata includes common provenance fields: version, retrieved_at, license,
checksum, archive_url, jurisdiction, issuing_body, scope, provenance,
independence, conflict_of_interest, consent, chain_of_custody, instrument,
calibration, repository, commit, environment, status and supersedes.

`research_claim_holding` shows the holdings and defaults for the sources a
claim cites.

## Document ingestion

`research_document_claim_preview` parses selected files without recording:

- Markdown/text notes (`.md`, `.markdown`, `.txt`);
- HTML/website snapshots (`.html`, `.htm`);
- EPUB books (`.epub`).

`research_document_claim_ingest` records the Source, Reference (when
appropriate), default Holding and claim candidates. Notes default to
`personal_note`; websites to `background` with access-date/archive constraints;
books to `interpretation`.

`paper_review_start` accepts `.epub`, `.html`, `.htm` and `.txt` as well as
TeX/Markdown/PDF, so a book, website snapshot or note can go through the same
structured review.

## Limits

- Format metadata is user-supplied; no automatic bibliographic lookup.
- EPUB parsing does not handle DRM.
- HTML parsing does not run JavaScript or fetch remote pages.
- A default holding is a conservative starting point, not a verdict.
- Upgrading a source to evidence requires a recorded rationale; the graph still
  does not decide whether the evidence is true.
