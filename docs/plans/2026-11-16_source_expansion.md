# Source format expansion: research artifacts, normative, meta and primary sources

**Date:** 2026-11-16. **Status:** stages A-C landed and integrated.

This expands the 15-format source registry into the full research source
taxonomy. Format remains separate from authority: each new type gets
format-specific metadata and a conservative default holding.

## Source type set

Existing: book, chapter, note, website, article, dataset, software, standard,
report, thesis, preprint, interview, lecture, video, other.

Add:

- **Research artifacts:** protocol, preregistration, registered_report,
  analysis_plan, lab_notebook, field_notes, computational_notebook, workflow,
  database, model, benchmark, ontology.
- **Normative/institutional:** regulation, statute, court_case, guideline,
  consensus_statement, patent, ethics_approval, consent_form,
  data_use_agreement, funding, conflict_of_interest.
- **Meta-science:** peer_review, editorial_decision, author_response,
  correction, retraction_notice, conference_abstract, poster, slides.
- **Human/grey:** oral_history, focus_group, survey, questionnaire, news, blog,
  press_release, wiki, forum, social_post, podcast, webinar, archival_document,
  letter, reference_work, textbook, encyclopedia, fiction, film, art.
- **Physical/synthetic:** specimen, sample, assay, instrument_output, image,
  spectrum, sequence, structure, synthetic_data, model_output.

## New holding roles and authorities

Roles: plan, normative, legal, governance, review_artifact, integrity_event,
primary_data, synthetic, candidate, technical_claim, context, evaluation.
Authorities: regulatory, legal, standard_body, repository.

Defaults by family:

- protocol/preregistration/registered_report/analysis_plan -> plan/method,
  constraint `not_result_evidence`.
- lab_notebook/field_notes -> primary_source/personal context.
- computational_notebook/workflow -> method; version/environment required.
- database/model/benchmark/ontology -> primary_data/method/evaluation; version,
  license and checksum are important.
- regulation/statute/court_case -> normative/legal; jurisdiction required.
- guideline/consensus_statement -> normative/interpretation; issuing body.
- patent -> technical_claim/legal, not empirical evidence.
- ethics_approval/consent_form/data_use_agreement -> governance.
- funding/conflict_of_interest -> context, not evidence.
- peer_review/editorial_decision/author_response -> review_artifact.
- correction/retraction_notice -> integrity_event.
- conference_abstract/poster/slides -> reported_evidence, review status none.
- oral_history/focus_group/interview -> primary_source; consent/context.
- survey/questionnaire -> method/primary_data; instrument/sampling.
- news/blog/press_release -> background; access/archive.
- wiki/forum/social_post -> background/unverified.
- podcast/webinar -> reported_evidence/background; transcript/date.
- archival_document/letter -> primary_source.
- reference_work/textbook/encyclopedia -> secondary_source/background.
- fiction/film/art -> primary_source for humanities; interpretation.
- specimen/sample/assay/instrument_output/image/spectrum/sequence/structure ->
  primary_data; chain of custody/calibration.
- synthetic_data/model_output -> synthetic, never evidence by default.

## Stage A: holdings expansion (`research/holdings.py`)

Acceptance:

- All new source types have defaults.
- New roles/authorities are valid.
- Checks flag:
  - protocol/preregistration held as result evidence;
  - standards/regulation/law held as empirical evidence;
  - synthetic/model output held as evidence;
  - dataset/software/model missing version/license/checksum;
  - legal/standard sources missing jurisdiction/issuing body;
  - interviews/surveys missing consent/context;
  - news/blog/social missing access/archive;
  - peer review/correction/retraction missing target metadata;
  - specimen/instrument missing chain/calibration.
- Reports and claim context remain deterministic; multiple holdings allowed.

## Stage B: metadata expansion (`research/source_formats.py`)

Acceptance:

- All new types have required/optional metadata profiles.
- Common optional fields include version, retrieved_at, license, checksum,
  archive_url, jurisdiction, issuing_body, scope, provenance, independence,
  conflict_of_interest, consent, chain_of_custody, instrument, calibration,
  repository, commit, environment, status, supersedes.
- `register_source` records version/license/retrieved_at/checksum/status on the
  Source, links `supersedes` when supplied, and records the default Holding.
- Required fields are type-appropriate; unknown fields still rejected.
- Book year remains optional; author/title required.

## Stage C: integration and checks

- Compare `holdings.SOURCE_TYPES` and `source_formats.SOURCE_FORMATS` for exact
  coverage.
- Service/MCP already expose `source_types`, `source_register`, `holding_*`,
  `document_claim_*`; verify new types work through them.
- `paper_review_start` can use the new formats where a parser exists
  (notes/HTML/EPUB/PDF); metadata-only sources can still be registered.
- Docs: `docs/research/sources.md` update.
- Service-level tests: protocol, dataset, regulation, peer review, interview,
  synthetic output, version/supersession, and holding checks.
- Full `pytest` and `ruff`.

## Results

Landed 2026-11-16.

**A — holdings.** `holdings.py` now covers 76 source types with conservative
defaults and 23 epistemic roles / 10 authority levels. New checks cover plans
held as results, normative/legal/synthetic sources held as empirical evidence,
artifact metadata, consent/context, mutable-source access, review-artifact
targets and physical-source chain/calibration. 150 tests passed.

**B — metadata.** `source_formats.py` covers the same 76 types with common
provenance fields (version, license, checksum, jurisdiction, issuing body,
consent, chain of custody, repository, commit, environment, status, supersedes,
etc.). Registration mirrors version/license/checksum/status onto Source and
links supersessions. 25 tests passed.

**C — integration.** Registry coverage is tested for exact equality. Service
tests cover protocol, dataset, regulation, peer review, interview and synthetic
sources, holding upgrades, artifact metadata warnings and version/supersession
edges. `paper_review_start` accepts EPUB/HTML/TXT as well as TeX/Markdown/PDF.

**Checks.** `1627 passed, 37 skipped`; `ruff check src tests` passes.

## Non-goals

- No automatic metadata lookup or web fetch.
- No DRM/EPUB handling.
- No automatic authority assignment beyond conservative defaults.
- No claim that a normative source is empirical evidence.
- No automatic truth verdict.
