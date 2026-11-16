"""Epistemic holdings: how a reviewer holds a Source.

A ``Holding`` is an authored stance, not a truth verdict.  The same ``Source``
can have several holdings (a book may be an authorial interpretation for one
claim and the source of a formal definition for another).  Defaults are
deliberately conservative: a book is an authorial argument unless specific
evidence is extracted, a note is personal context, and a website is mutable
background.  Format is never treated as authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ozagia.research.graph import ResearchError

__all__ = [
    "AUTHORITY_LEVELS",
    "EPISTEMIC_ROLES",
    "HOLDING_STATUSES",
    "REVIEW_STATUSES",
    "SOURCE_TYPES",
    "HoldingError",
    "check_holding",
    "claim_holding",
    "holding_report",
    "list_source_types",
    "record_holding",
]

EPISTEMIC_ROLES = (
    "evidence",
    # ``reported_evidence`` is the article format's default stance: the work
    # reports evidence without that evidence being confirmed or reviewed here.
    "reported_evidence",
    "interpretation",
    "hypothesis",
    "method",
    "background",
    "normative",
    "speculation",
    "mixed",
    "personal_note",
    "primary_source",
    "secondary_source",
    # Research-artifact and institutional stances. A plan is a commitment made
    # before results; it is not a report of results. Legal and governance
    # material constrains action without being empirical evidence.
    "plan",
    "legal",
    "governance",
    "review_artifact",
    "integrity_event",
    "primary_data",
    "synthetic",
    "candidate",
    "technical_claim",
    "context",
    "evaluation",
    # A user's own stated stance, sourced to a conversation.
    "user_position",
)
AUTHORITY_LEVELS = (
    "peer_reviewed",
    "editorial",
    "institutional",
    "self_published",
    "personal",
    "unknown",
    # Normative and curated authorities. These describe the issuing process,
    # not whether the content is true.
    "regulatory",
    "legal",
    "standard_body",
    "repository",
)
REVIEW_STATUSES = ("peer_reviewed", "editorial", "none", "unknown")
HOLDING_STATUSES = ("proposed", "accepted", "rejected")

# Conservative format defaults.  Only a reviewer may upgrade a holding to
# evidence, and a book upgrade needs a recorded rationale and scope.
SOURCE_TYPES: dict[str, dict[str, Any]] = {
    "book": {
        "label": "Book",
        "epistemic_role": "interpretation",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": [
            "treat_as_author_argument",
            "not_as_behavioural_evidence_without_extraction",
        ],
        "required_metadata": ["title", "authors", "year", "publisher"],
        "notes": (
            "A book is usually a mixture of evidence and the author's "
            "interpretation. Treat it as an authorial argument until specific "
            "evidence is extracted and scoped."
        ),
    },
    "note": {
        "label": "Personal note",
        "epistemic_role": "personal_note",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": ["not_external_evidence"],
        "required_metadata": ["title", "author", "date"],
        "notes": (
            "A note records personal context or a reading trace. It is not "
            "external evidence unless separately linked to supporting sources."
        ),
    },
    "website": {
        "label": "Website",
        "epistemic_role": "background",
        "authority": "unknown",
        "review_status": "unknown",
        "use_constraints": ["check_accessed_date", "archive_snapshot"],
        "required_metadata": ["url", "accessed_at"],
        "notes": (
            "Websites are date-sensitive and mutable. Record the URL, access "
            "date and an archive URL where possible."
        ),
    },
    "article": {
        "label": "Article",
        "epistemic_role": "reported_evidence",
        "authority": "unknown",
        "review_status": "unknown",
        "use_constraints": ["check_peer_review_status"],
        "required_metadata": ["title", "authors", "year", "venue"],
        "notes": (
            "An article may report evidence, but do not assume peer review. "
            "Record the venue and review status separately from the format."
        ),
    },
    "dataset": {
        "label": "Dataset",
        "epistemic_role": "primary_source",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["check_provenance", "record_version"],
        "required_metadata": ["title", "creator", "year"],
        "notes": (
            "Datasets can be primary material, but provenance, collection "
            "method, version and licence determine what they can support."
        ),
    },
    "software": {
        "label": "Software",
        "epistemic_role": "method",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["record_version", "check_license"],
        "required_metadata": ["title", "version", "url"],
        "notes": (
            "Software is a method or instrument, not evidence about the world. "
            "Record the version and configuration used."
        ),
    },
    "standard": {
        "label": "Standard",
        "epistemic_role": "normative",
        "authority": "institutional",
        "review_status": "editorial",
        "use_constraints": ["check_version", "check_scope"],
        "required_metadata": ["title", "identifier", "year"],
        "notes": (
            "A standard prescribes or defines; it is normative rather than "
            "empirical evidence."
        ),
    },
    "report": {
        "label": "Report",
        "epistemic_role": "reported_evidence",
        "authority": "institutional",
        "review_status": "unknown",
        "use_constraints": ["check_institution", "check_date"],
        "required_metadata": ["title", "authors", "year", "institution"],
        "notes": (
            "Reports vary from peer-reviewed assessments to advocacy. Check "
            "the issuing institution, review process and date."
        ),
    },
    "thesis": {
        "label": "Thesis",
        "epistemic_role": "interpretation",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": ["treat_as_author_argument", "check_examination_status"],
        "required_metadata": ["title", "author", "year", "institution"],
        "notes": (
            "A thesis is examined but not peer reviewed in the journal sense. "
            "Treat its overall account as an authorial interpretation."
        ),
    },
    "preprint": {
        "label": "Preprint",
        "epistemic_role": "reported_evidence",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["check_version", "check_peer_review_status"],
        "required_metadata": ["title", "authors", "year", "repository"],
        "notes": (
            "A preprint has no journal peer review by default. Check for later "
            "versions and a published peer-reviewed record."
        ),
    },
    "interview": {
        "label": "Interview",
        "epistemic_role": "primary_source",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": ["check_transcript", "check_consent"],
        "required_metadata": ["title", "interviewee", "date"],
        "notes": (
            "An interview is primary testimony by a person. Check the "
            "transcript, context and consent before quoting it."
        ),
    },
    "lecture": {
        "label": "Lecture",
        "epistemic_role": "background",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": ["check_date", "check_citation"],
        "required_metadata": ["title", "speaker", "date"],
        "notes": (
            "A lecture is teaching or commentary. It may explain a position "
            "but is not usually a citable evidence record."
        ),
    },
    "video": {
        "label": "Video",
        "epistemic_role": "background",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["check_accessed_date", "archive_snapshot"],
        "required_metadata": ["title", "creator", "url", "accessed_at"],
        "notes": (
            "Video is mutable and often edited. Record the access date and an "
            "archive where possible; do not infer evidence from presentation."
        ),
    },
    "chapter": {
        "label": "Book chapter",
        "epistemic_role": "interpretation",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": [
            "treat_as_author_argument",
            "not_as_behavioural_evidence_without_extraction",
        ],
        "required_metadata": ["title", "authors", "book_title", "year", "publisher"],
        "notes": (
            "A chapter inherits the edited-book setting. Treat its overall "
            "account as an authorial interpretation unless evidence is extracted."
        ),
    },
    # ------------------------------------------------------------------ #
    # Research artifacts: plans, notebooks and curated technical artifacts
    # ------------------------------------------------------------------ #
    "protocol": {
        "label": "Protocol",
        "epistemic_role": "plan",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": [
            "not_result_evidence",
            "check_version",
            "check_registration",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A protocol states what will be done before results exist. It "
            "constrains interpretation but is not evidence that the protocol "
            "was followed or that any result holds."
        ),
    },
    "preregistration": {
        "label": "Preregistration",
        "epistemic_role": "plan",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": [
            "not_result_evidence",
            "check_registration",
            "check_version",
        ],
        "required_metadata": ["title", "registration_id"],
        "notes": (
            "A preregistration fixes hypotheses and analyses in advance. It is "
            "a plan; it does not report an outcome and cannot be held as "
            "result evidence."
        ),
    },
    "registered_report": {
        "label": "Registered report",
        "epistemic_role": "plan",
        "authority": "institutional",
        "review_status": "unknown",
        "use_constraints": [
            "not_result_evidence",
            "check_stage",
            "check_version",
        ],
        "required_metadata": ["title", "registration_id"],
        "notes": (
            "A registered report begins as a reviewed plan. Until results are "
            "reported, hold it as a plan rather than as evidence for an outcome."
        ),
    },
    "analysis_plan": {
        "label": "Analysis plan",
        "epistemic_role": "plan",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["not_result_evidence", "check_version"],
        "required_metadata": ["title", "version"],
        "notes": (
            "An analysis plan describes intended methods. It is a plan and "
            "does not establish that the analysis was run or what it found."
        ),
    },
    "lab_notebook": {
        "label": "Lab notebook",
        "epistemic_role": "primary_source",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "record_pages",
            "not_external_evidence_without_extraction",
        ],
        "required_metadata": ["title", "date"],
        "notes": (
            "A lab notebook is a primary record of work as it happened. It "
            "needs page-level provenance and extraction before it supports a "
            "specific claim."
        ),
    },
    "field_notes": {
        "label": "Field notes",
        "epistemic_role": "primary_source",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "check_context",
            "not_external_evidence_without_extraction",
        ],
        "required_metadata": ["title", "date"],
        "notes": (
            "Field notes are situated primary observations. Record the context "
            "of collection and extract observations before relying on them."
        ),
    },
    "computational_notebook": {
        "label": "Computational notebook",
        "epistemic_role": "method",
        "authority": "repository",
        "review_status": "none",
        "use_constraints": [
            "record_version",
            "record_environment",
            "check_license",
            "check_checksum",
        ],
        "required_metadata": ["title", "version", "environment"],
        "notes": (
            "A computational notebook is an executable method. Record its "
            "version, environment and licence; a render alone is not a result "
            "record."
        ),
    },
    "workflow": {
        "label": "Workflow",
        "epistemic_role": "method",
        "authority": "repository",
        "review_status": "none",
        "use_constraints": [
            "record_version",
            "record_environment",
            "check_license",
            "check_checksum",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A workflow defines a processing method. Version, environment and "
            "licence determine whether a reported result can be reproduced."
        ),
    },
    "database": {
        "label": "Database",
        "epistemic_role": "primary_data",
        "authority": "repository",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "record_version",
            "check_license",
            "check_checksum",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A database is organised primary material. Provenance, version, "
            "licence and checksum determine what can be queried or supported."
        ),
    },
    "model": {
        "label": "Model",
        "epistemic_role": "method",
        "authority": "repository",
        "review_status": "none",
        "use_constraints": [
            "record_version",
            "check_license",
            "check_checksum",
            "check_training_data",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A model is a method or instrument. Its outputs are synthetic "
            "artifacts until independently validated against observation."
        ),
    },
    "benchmark": {
        "label": "Benchmark",
        "epistemic_role": "evaluation",
        "authority": "repository",
        "review_status": "none",
        "use_constraints": [
            "record_version",
            "check_license",
            "check_checksum",
            "check_scope",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A benchmark defines a measurement instrument for evaluation. It "
            "is not evidence that a system works outside the benchmark's scope."
        ),
    },
    "ontology": {
        "label": "Ontology",
        "epistemic_role": "method",
        "authority": "repository",
        "review_status": "none",
        "use_constraints": [
            "record_version",
            "check_license",
            "check_checksum",
            "check_scope",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "An ontology is a controlled vocabulary used to classify material. "
            "Hold it as a definitional method unless the ontology itself is "
            "the object of study."
        ),
    },
    # ------------------------------------------------------------------ #
    # Normative and institutional sources
    # ------------------------------------------------------------------ #
    "regulation": {
        "label": "Regulation",
        "epistemic_role": "normative",
        "authority": "regulatory",
        "review_status": "none",
        "use_constraints": [
            "check_jurisdiction",
            "check_issuing_body",
            "check_version",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "identifier", "jurisdiction"],
        "notes": (
            "A regulation prescribes conduct. It can support a legal or policy "
            "premise, not an empirical claim about how the world behaves."
        ),
    },
    "statute": {
        "label": "Statute",
        "epistemic_role": "legal",
        "authority": "legal",
        "review_status": "none",
        "use_constraints": [
            "check_jurisdiction",
            "check_version",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "identifier", "jurisdiction"],
        "notes": (
            "A statute is enacted law. Record the jurisdiction and version in "
            "force; it is normative, not empirical evidence."
        ),
    },
    "court_case": {
        "label": "Court case",
        "epistemic_role": "legal",
        "authority": "legal",
        "review_status": "none",
        "use_constraints": [
            "check_jurisdiction",
            "check_citation",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "identifier", "jurisdiction"],
        "notes": (
            "A court case is a legal determination within a jurisdiction. It "
            "binds or persuades legally; it does not establish empirical facts."
        ),
    },
    "guideline": {
        "label": "Guideline",
        "epistemic_role": "normative",
        "authority": "standard_body",
        "review_status": "unknown",
        "use_constraints": [
            "check_issuing_body",
            "check_version",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "issuing_body"],
        "notes": (
            "A guideline recommends practice. Record the issuing body and "
            "version; recommendations are normative interpretations of evidence."
        ),
    },
    "consensus_statement": {
        "label": "Consensus statement",
        "epistemic_role": "interpretation",
        "authority": "standard_body",
        "review_status": "unknown",
        "use_constraints": [
            "check_issuing_body",
            "check_date",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "issuing_body"],
        "notes": (
            "A consensus statement records an expert group's interpretation. "
            "It may summarise evidence but is not itself empirical evidence."
        ),
    },
    "patent": {
        "label": "Patent",
        "epistemic_role": "technical_claim",
        "authority": "legal",
        "review_status": "editorial",
        "use_constraints": [
            "not_empirical_evidence",
            "check_jurisdiction",
            "check_status",
        ],
        "required_metadata": ["title", "identifier", "jurisdiction"],
        "notes": (
            "A patent claims an invention and its scope. A granted claim is not "
            "evidence that the invention works or performs as described."
        ),
    },
    "ethics_approval": {
        "label": "Ethics approval",
        "epistemic_role": "governance",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": [
            "check_identifier",
            "check_scope",
            "check_date",
        ],
        "required_metadata": ["title", "identifier", "institution"],
        "notes": (
            "Ethics approval records that a body permitted a study under stated "
            "conditions. It is governance context, not evidence for a finding."
        ),
    },
    "consent_form": {
        "label": "Consent form",
        "epistemic_role": "governance",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": [
            "check_consent",
            "check_scope",
            "check_version",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A consent form records what participants agreed to. Check its "
            "version and scope before using linked human material."
        ),
    },
    "data_use_agreement": {
        "label": "Data use agreement",
        "epistemic_role": "governance",
        "authority": "legal",
        "review_status": "none",
        "use_constraints": [
            "check_scope",
            "check_version",
            "check_license",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A data use agreement constrains how material may be used. It is "
            "governance context and can prohibit some forms of reuse."
        ),
    },
    "funding": {
        "label": "Funding record",
        "epistemic_role": "context",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": ["not_evidence_of_quality", "check_identifier"],
        "required_metadata": ["title", "funder"],
        "notes": (
            "A funding record is context about who supported a work. Funding "
            "does not establish the quality or truth of a claim."
        ),
    },
    "conflict_of_interest": {
        "label": "Conflict of interest",
        "epistemic_role": "context",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["not_evidence_of_quality", "check_scope"],
        "required_metadata": ["title"],
        "notes": (
            "A conflict-of-interest declaration is context for interpreting a "
            "work. It is not evidence for or against the work's claims."
        ),
    },
    # ------------------------------------------------------------------ #
    # Meta-science records
    # ------------------------------------------------------------------ #
    "peer_review": {
        "label": "Peer review",
        "epistemic_role": "review_artifact",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["check_target", "not_evidence_about_the_world"],
        "required_metadata": ["title"],
        "notes": (
            "A peer-review report is evidence about the review process, not "
            "about the reviewed claim. Record the target work it addresses."
        ),
    },
    "editorial_decision": {
        "label": "Editorial decision",
        "epistemic_role": "review_artifact",
        "authority": "editorial",
        "review_status": "none",
        "use_constraints": ["check_target", "not_evidence_about_the_world"],
        "required_metadata": ["title"],
        "notes": (
            "An editorial decision records a venue's handling of a work. It is "
            "a review artifact and needs the target manuscript or version."
        ),
    },
    "author_response": {
        "label": "Author response",
        "epistemic_role": "review_artifact",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["check_target", "not_evidence_about_the_world"],
        "required_metadata": ["title"],
        "notes": (
            "An author response is part of a review exchange. It argues about "
            "a target work and is not independent evidence for its claims."
        ),
    },
    "correction": {
        "label": "Correction",
        "epistemic_role": "integrity_event",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": ["check_target", "check_supersedes"],
        "required_metadata": ["title"],
        "notes": (
            "A correction changes a published record. Link it to the target "
            "work and check which version a claim relied on."
        ),
    },
    "retraction_notice": {
        "label": "Retraction notice",
        "epistemic_role": "integrity_event",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": ["check_target", "check_supersedes", "not_evidence"],
        "required_metadata": ["title"],
        "notes": (
            "A retraction notice marks a work as withdrawn. It is an integrity "
            "event, not evidence for the retracted work's claims."
        ),
    },
    "conference_abstract": {
        "label": "Conference abstract",
        "epistemic_role": "reported_evidence",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_peer_review_status",
            "not_full_paper",
            "check_version",
        ],
        "required_metadata": ["title", "authors", "year"],
        "notes": (
            "A conference abstract reports work in brief and may be unreviewed. "
            "Check for a full paper before relying on details."
        ),
    },
    "poster": {
        "label": "Poster",
        "epistemic_role": "reported_evidence",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": ["check_peer_review_status", "not_full_paper"],
        "required_metadata": ["title", "authors", "year"],
        "notes": (
            "A poster is presented material, often preliminary. It reports "
            "work without establishing review or a complete record."
        ),
    },
    "slides": {
        "label": "Slides",
        "epistemic_role": "reported_evidence",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_peer_review_status",
            "not_full_paper",
            "check_date",
        ],
        "required_metadata": ["title", "creator", "date"],
        "notes": (
            "Slides are presentation material and may omit methods and limits. "
            "Check the date and any accompanying paper."
        ),
    },
    # ------------------------------------------------------------------ #
    # Human, grey and humanities sources
    # ------------------------------------------------------------------ #
    "oral_history": {
        "label": "Oral history",
        "epistemic_role": "primary_source",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": [
            "check_consent",
            "check_context",
            "check_transcript",
        ],
        "required_metadata": ["title", "interviewee", "date"],
        "notes": (
            "An oral history is a situated personal account. Record consent, "
            "interview context and the transcript before quoting it."
        ),
    },
    "focus_group": {
        "label": "Focus group",
        "epistemic_role": "primary_source",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": [
            "check_consent",
            "check_context",
            "check_transcript",
        ],
        "required_metadata": ["title", "date"],
        "notes": (
            "A focus group records group interaction. Consent, moderation "
            "context and transcription affect what it can support."
        ),
    },
    "survey": {
        "label": "Survey",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_instrument",
            "check_sampling",
            "check_consent",
        ],
        "required_metadata": ["title", "year"],
        "notes": (
            "A survey records responses collected with an instrument. Record "
            "sampling, instrument and consent; summaries are not self-validating."
        ),
    },
    "questionnaire": {
        "label": "Questionnaire",
        "epistemic_role": "method",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_instrument",
            "check_sampling",
            "check_consent",
        ],
        "required_metadata": ["title", "version"],
        "notes": (
            "A questionnaire is a measurement instrument. Wording, version and "
            "mode of administration determine what responses mean."
        ),
    },
    "news": {
        "label": "News article",
        "epistemic_role": "background",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": [
            "check_accessed_date",
            "archive_snapshot",
            "not_primary_evidence",
        ],
        "required_metadata": ["title", "outlet", "date"],
        "notes": (
            "A news article reports events, often through intermediaries. "
            "Record access and archive details; prefer the underlying source."
        ),
    },
    "blog": {
        "label": "Blog post",
        "epistemic_role": "background",
        "authority": "self_published",
        "review_status": "none",
        "use_constraints": [
            "check_accessed_date",
            "archive_snapshot",
            "unverified",
        ],
        "required_metadata": ["title", "url", "date"],
        "notes": (
            "A blog post is self-published background. It may contain useful "
            "analysis but is unverified without independent support."
        ),
    },
    "press_release": {
        "label": "Press release",
        "epistemic_role": "background",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": [
            "check_accessed_date",
            "archive_snapshot",
            "not_primary_evidence",
        ],
        "required_metadata": ["title", "institution", "date"],
        "notes": (
            "A press release promotes an institution's account. It is "
            "background; check the underlying paper or data before citing it."
        ),
    },
    "wiki": {
        "label": "Wiki page",
        "epistemic_role": "background",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_accessed_date",
            "archive_snapshot",
            "unverified",
        ],
        "required_metadata": ["title", "url"],
        "notes": (
            "A wiki page is mutable and collectively edited. Record access and "
            "archive details, and follow its citations to primary sources."
        ),
    },
    "forum": {
        "label": "Forum post",
        "epistemic_role": "background",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_accessed_date",
            "archive_snapshot",
            "unverified",
        ],
        "required_metadata": ["title", "url"],
        "notes": (
            "A forum post is an unverified discussion contribution. Record "
            "access and archive details; do not treat it as a settled record."
        ),
    },
    "social_post": {
        "label": "Social media post",
        "epistemic_role": "background",
        "authority": "self_published",
        "review_status": "none",
        "use_constraints": [
            "check_accessed_date",
            "archive_snapshot",
            "unverified",
        ],
        "required_metadata": ["title", "url", "date"],
        "notes": (
            "A social post is mutable, self-published and unverified. Capture "
            "its access date and an archive before relying on its wording."
        ),
    },
    "podcast": {
        "label": "Podcast",
        "epistemic_role": "reported_evidence",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_transcript",
            "check_date",
            "check_accessed_date",
        ],
        "required_metadata": ["title", "date"],
        "notes": (
            "A podcast reports discussion in audio form. Use a transcript and "
            "record the episode date and access details."
        ),
    },
    "webinar": {
        "label": "Webinar",
        "epistemic_role": "background",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": ["check_transcript", "check_date"],
        "required_metadata": ["title", "date"],
        "notes": (
            "A webinar is presented and often promotional material. It is "
            "background unless a transcript and supporting record are available."
        ),
    },
    "archival_document": {
        "label": "Archival document",
        "epistemic_role": "primary_source",
        "authority": "institutional",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "check_custody",
            "check_date",
        ],
        "required_metadata": ["title", "date", "archive"],
        "notes": (
            "An archival document is a primary record held by an archive. "
            "Record collection, box or item reference and custody."
        ),
    },
    "letter": {
        "label": "Letter",
        "epistemic_role": "primary_source",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "check_consent",
            "check_date",
        ],
        "required_metadata": ["title", "author", "date"],
        "notes": (
            "A letter is personal primary correspondence. Consent, provenance "
            "and context qualify what it can support."
        ),
    },
    "reference_work": {
        "label": "Reference work",
        "epistemic_role": "secondary_source",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": ["check_scope", "check_edition"],
        "required_metadata": ["title", "year"],
        "notes": (
            "A reference work synthesises other sources. Treat it as a "
            "secondary source and follow its citations for evidence."
        ),
    },
    "textbook": {
        "label": "Textbook",
        "epistemic_role": "secondary_source",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": ["check_scope", "check_edition"],
        "required_metadata": ["title", "authors", "year"],
        "notes": (
            "A textbook presents a settled teaching account. It is secondary "
            "material and can simplify or omit contested evidence."
        ),
    },
    "encyclopedia": {
        "label": "Encyclopedia",
        "epistemic_role": "secondary_source",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": ["check_scope", "check_edition"],
        "required_metadata": ["title", "year"],
        "notes": (
            "An encyclopedia entry is an edited secondary summary. Use it for "
            "orientation and follow its citations to primary sources."
        ),
    },
    "fiction": {
        "label": "Fiction",
        "epistemic_role": "primary_source",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": [
            "interpret_as_primary_text",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "author", "year"],
        "notes": (
            "Fiction can be primary material for humanities interpretation. It "
            "is not empirical evidence about the world unless separately studied."
        ),
    },
    "film": {
        "label": "Film",
        "epistemic_role": "primary_source",
        "authority": "editorial",
        "review_status": "editorial",
        "use_constraints": [
            "interpret_as_primary_text",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "year"],
        "notes": (
            "A film is a primary text for interpretation. Its depicted events "
            "are not empirical evidence unless studied as such."
        ),
    },
    "art": {
        "label": "Artwork",
        "epistemic_role": "primary_source",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "interpret_as_primary_text",
            "not_empirical_evidence",
        ],
        "required_metadata": ["title", "creator"],
        "notes": (
            "An artwork is a primary object for interpretation. It does not "
            "report empirical findings."
        ),
    },
    # ------------------------------------------------------------------ #
    # Physical and synthetic material
    # ------------------------------------------------------------------ #
    "specimen": {
        "label": "Specimen",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_chain_of_custody",
            "check_calibration",
        ],
        "required_metadata": ["title"],
        "notes": (
            "A specimen is physical primary material. Record chain of custody "
            "and the conditions under which it was collected and stored."
        ),
    },
    "sample": {
        "label": "Sample",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_chain_of_custody",
            "check_calibration",
        ],
        "required_metadata": ["title"],
        "notes": (
            "A sample is a selected portion of material. Sampling method and "
            "chain of custody determine what measurements can support."
        ),
    },
    "assay": {
        "label": "Assay",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_calibration",
            "check_instrument",
        ],
        "required_metadata": ["title", "instrument"],
        "notes": (
            "An assay is a measurement procedure and its result. Record the "
            "instrument, calibration and controls before treating values as data."
        ),
    },
    "instrument_output": {
        "label": "Instrument output",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_instrument",
            "check_calibration",
        ],
        "required_metadata": ["title", "instrument"],
        "notes": (
            "Instrument output is raw measurement material. The instrument, "
            "settings and calibration determine what the values mean."
        ),
    },
    "image": {
        "label": "Image",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "check_calibration",
        ],
        "required_metadata": ["title"],
        "notes": (
            "An image is primary material only with provenance and acquisition "
            "context; editing and processing must be recorded."
        ),
    },
    "spectrum": {
        "label": "Spectrum",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_instrument",
            "check_calibration",
        ],
        "required_metadata": ["title"],
        "notes": (
            "A spectrum is instrument-derived primary data. Record the "
            "instrument, acquisition parameters and calibration."
        ),
    },
    "sequence": {
        "label": "Sequence",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "record_version",
        ],
        "required_metadata": ["title"],
        "notes": (
            "A sequence is primary data with a provenance chain. Record the "
            "reference assembly or version and any processing applied."
        ),
    },
    "structure": {
        "label": "Structure",
        "epistemic_role": "primary_data",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "check_provenance",
            "check_method",
        ],
        "required_metadata": ["title"],
        "notes": (
            "A structure is a determined or modelled arrangement. Record the "
            "determination method; models are not the same as observations."
        ),
    },
    "synthetic_data": {
        "label": "Synthetic data",
        "epistemic_role": "synthetic",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "not_evidence_by_default",
            "record_generation_method",
            "check_provenance",
        ],
        "required_metadata": ["title", "generator"],
        "notes": (
            "Synthetic data is generated, not observed. It can exercise a "
            "method but is never evidence about the world by default."
        ),
    },
    "model_output": {
        "label": "Model output",
        "epistemic_role": "synthetic",
        "authority": "unknown",
        "review_status": "none",
        "use_constraints": [
            "not_evidence_by_default",
            "record_model",
            "record_version",
        ],
        "required_metadata": ["title", "model"],
        "notes": (
            "Model output is generated material. It is synthetic until "
            "validated against independent observation; record model and version."
        ),
    },
    "conversation": {
        "label": "Conversation",
        "epistemic_role": "user_position",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": ["user_stance", "not_external_evidence"],
        "required_metadata": ["session_id", "date"],
        "notes": (
            "A stance or correction the user stated in a conversation with "
            "an agent. It records what the user holds, with its date; it is "
            "not sourced evidence and is reported apart from evidence."
        ),
    },
    "other": {
        "label": "Other source",
        "epistemic_role": "background",
        "authority": "unknown",
        "review_status": "unknown",
        "use_constraints": ["check_source_identity"],
        "required_metadata": ["title"],
        "notes": (
            "An unclassified source has no format-based authority. Record its "
            "identity and a reviewer rationale before relying on it."
        ),
    },
}

_SPEC_FIELDS = (
    "label",
    "epistemic_role",
    "authority",
    "review_status",
    "use_constraints",
    "required_metadata",
    "notes",
)
_LOCATOR_FIELDS = ("path", "root_id", "sha256", "url")
_METADATA_CONTAINERS = ("metadata", "source_metadata", "bibliographic")
_REFERENCE_METADATA_FIELDS = ("publisher", "year", "title", "authors", "url", "venue")
# Metadata that may also be recorded flat on a Source rather than inside its
# ``metadata`` mapping.  Registration stores it under ``metadata``; hand-recorded
# Sources may be flatter, so the checks accept both without requiring a field.
_SOURCE_METADATA_FIELDS = (
    *_LOCATOR_FIELDS,
    "accessed_at",
    "archive_url",
    "publisher",
    "year",
    "version",
    "license",
    "checksum",
    "retrieved_at",
    "jurisdiction",
    "issuing_body",
    "scope",
    "provenance",
    "independence",
    "conflict_of_interest",
    "consent",
    "chain_of_custody",
    "instrument",
    "calibration",
    "repository",
    "commit",
    "environment",
    "status",
    "supersedes",
    "target_id",
    "target",
    "target_title",
    "target_doi",
    "target_reference_id",
    "transcript_url",
    "sampling",
    "context",
)

# Format families used by the holding checks.  Membership is deliberately
# explicit so a newly added type never inherits a check by accident.
_PLAN_SOURCE_TYPES = frozenset(
    {"protocol", "preregistration", "registered_report", "analysis_plan"}
)
_NORMATIVE_SOURCE_TYPES = frozenset(
    {
        "standard",
        "regulation",
        "statute",
        "court_case",
        "guideline",
        "consensus_statement",
        "patent",
    }
)
_SYNTHETIC_SOURCE_TYPES = frozenset({"synthetic_data", "model_output"})
_VERSIONED_ARTIFACT_TYPES = frozenset(
    {
        "dataset",
        "database",
        "software",
        "model",
        "benchmark",
        "workflow",
        "computational_notebook",
    }
)
_ARTIFACT_METADATA_FIELDS = ("version", "license", "checksum")
_NORMATIVE_METADATA_FIELDS: dict[str, tuple[str, ...]] = {
    "standard": ("issuing_body",),
    "regulation": ("jurisdiction", "issuing_body"),
    "statute": ("jurisdiction", "issuing_body"),
    "court_case": ("jurisdiction",),
    "guideline": ("issuing_body",),
    "consensus_statement": ("issuing_body",),
    "patent": ("jurisdiction",),
}
_HUMAN_SOURCE_TYPES = frozenset(
    {"interview", "oral_history", "focus_group", "survey", "questionnaire"}
)
_HUMAN_CONTEXT_FIELDS = (
    "context",
    "interviewer",
    "outlet",
    "venue",
    "transcript_url",
    "sampling",
    "instrument",
)
_MUTABLE_WEB_TYPES = frozenset(
    {"news", "blog", "press_release", "wiki", "forum", "social_post"}
)
_REVIEW_ARTIFACT_TYPES = frozenset(
    {
        "peer_review",
        "editorial_decision",
        "author_response",
        "correction",
        "retraction_notice",
    }
)
_TARGET_FIELDS = (
    "target_id",
    "target",
    "target_title",
    "target_doi",
    "target_reference_id",
    "supersedes",
)
_PHYSICAL_METADATA_FIELDS: dict[str, tuple[str, ...]] = {
    "specimen": ("chain_of_custody",),
    "sample": ("chain_of_custody",),
    "assay": ("calibration", "instrument"),
    "instrument_output": ("instrument", "calibration"),
}


class HoldingError(ResearchError):
    """A holding record, format default or claim context is malformed."""


# --------------------------------------------------------------------------- #
# small deterministic helpers
# --------------------------------------------------------------------------- #
def _as_state(state: Any) -> Mapping[str, Any]:
    if isinstance(state, Mapping):
        return state
    getter = getattr(state, "state", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    raise HoldingError("holding operations need a graph state mapping")


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node
        for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _node_by_id(state: Mapping[str, Any], node_id: Any) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node.get("id") == node_id), None)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HoldingError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _token(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().casefold()
    for character in ("_", "-", ".", "/"):
        text = text.replace(character, " ")
    return "_".join(text.split())


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return bool(value)
    return True


def _collapse(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _string_items(value: Any, field: str) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise HoldingError(f"{field} must be a list of non-empty strings")
    items: list[str] = []
    for index, item in enumerate(value):
        text = _nonempty_string(item, f"{field}[{index}]")
        if text not in items:
            items.append(text)
    return items


def _json_safe(value: Any, field: str) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise HoldingError(f"{field} must be JSON-serializable: {exc}") from exc


def _source_type(value: Any) -> str:
    text = _token(_nonempty_string(value, "source_type"))
    if text not in SOURCE_TYPES:
        raise HoldingError(
            f"source_type must be one of {sorted(SOURCE_TYPES)}, not {value!r}"
        )
    return text


def _epistemic_role(value: Any) -> str:
    role = _token(_nonempty_string(value, "epistemic_role"))
    if role not in EPISTEMIC_ROLES:
        raise HoldingError(
            f"epistemic_role must be one of {list(EPISTEMIC_ROLES)}, not {value!r}"
        )
    return role


def _authority(value: Any) -> str:
    authority = _token(_nonempty_string(value, "authority"))
    if authority not in AUTHORITY_LEVELS:
        raise HoldingError(
            f"authority must be one of {list(AUTHORITY_LEVELS)}, not {value!r}"
        )
    return authority


def _review_status(value: Any) -> str:
    status = _token(_nonempty_string(value, "review_status"))
    if status not in REVIEW_STATUSES:
        raise HoldingError(
            f"review_status must be one of {list(REVIEW_STATUSES)}, not {value!r}"
        )
    return status


def _holding_status(value: Any) -> str:
    status = _token(_nonempty_string(value, "status"))
    if status not in HOLDING_STATUSES:
        raise HoldingError(
            f"status must be one of {list(HOLDING_STATUSES)}, not {value!r}"
        )
    return status


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HoldingError("confidence must be a number between 0 and 1")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise HoldingError("confidence must be between 0 and 1")
    return number


def _spec_copy(source_type: str) -> dict[str, Any]:
    spec = SOURCE_TYPES[source_type]
    return {
        "source_type": source_type,
        "label": spec["label"],
        "epistemic_role": spec["epistemic_role"],
        "authority": spec["authority"],
        "review_status": spec["review_status"],
        "use_constraints": list(spec["use_constraints"]),
        "required_metadata": list(spec["required_metadata"]),
        "notes": spec["notes"],
    }


def list_source_types() -> list[dict[str, Any]]:
    """Return every source format and its conservative defaults, sorted by type."""
    return [_spec_copy(source_type) for source_type in sorted(SOURCE_TYPES)]


# --------------------------------------------------------------------------- #
# node lookup
# --------------------------------------------------------------------------- #
def _require_source(state: Mapping[str, Any], source_id: Any) -> Mapping[str, Any]:
    node_id = _nonempty_string(source_id, "source_id")
    node = _node_by_id(state, node_id)
    if node is None:
        raise HoldingError(f"unknown source id: {node_id}")
    if node.get("type") != "Source":
        raise HoldingError(f"{node_id} is a {node.get('type')}, not a Source")
    return node


def _require_holding(state: Mapping[str, Any], holding_id: Any) -> Mapping[str, Any]:
    node_id = _nonempty_string(holding_id, "holding_id")
    node = _node_by_id(state, node_id)
    if node is None:
        raise HoldingError(f"unknown holding id: {node_id}")
    if node.get("type") != "Holding":
        raise HoldingError(f"{node_id} is a {node.get('type')}, not a Holding")
    return node


def _require_claim(state: Mapping[str, Any], claim_id: Any) -> Mapping[str, Any]:
    node_id = _nonempty_string(claim_id, "claim_id")
    node = _node_by_id(state, node_id)
    if node is None:
        raise HoldingError(f"unknown claim id: {node_id}")
    if node.get("type") != "Claim":
        raise HoldingError(f"{node_id} is a {node.get('type')}, not a Claim")
    return node


def _held_as_edges(state: Mapping[str, Any], source_id: str) -> list[Mapping[str, Any]]:
    return sorted(
        (
            edge
            for edge in _edges(state)
            if edge.get("type") == "held_as" and edge.get("src") == source_id
        ),
        key=lambda edge: str(edge.get("id") or ""),
    )


def _holdings_for_source(
    state: Mapping[str, Any], source_id: str
) -> list[Mapping[str, Any]]:
    linked = {
        str(edge.get("tgt"))
        for edge in _held_as_edges(state, source_id)
        if isinstance(edge.get("tgt"), str)
    }
    matched = {
        str(node["id"])
        for node in _nodes(state)
        if node.get("type") == "Holding" and node.get("source_id") == source_id
    }
    ids = sorted(linked | matched)
    return [
        node
        for node_id in ids
        if (node := _node_by_id(state, node_id)) is not None
        and node.get("type") == "Holding"
    ]


# --------------------------------------------------------------------------- #
# metadata helpers
# --------------------------------------------------------------------------- #
def _source_reference_nodes(
    state: Mapping[str, Any], source_id: str
) -> list[Mapping[str, Any]]:
    reference_ids = {
        str(edge.get("tgt"))
        for edge in _edges(state)
        if edge.get("type") == "manifestation_of"
        and edge.get("src") == source_id
        and isinstance(edge.get("tgt"), str)
    }
    source = _node_by_id(state, source_id)
    if source is not None and isinstance(source.get("reference_id"), str):
        reference_ids.add(source["reference_id"])
    references = []
    for reference_id in sorted(reference_ids):
        node = _node_by_id(state, reference_id)
        if node is not None and node.get("type") == "Reference":
            references.append(node)
    return references


def _source_metadata(state: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Merge Source-level metadata with its locator and linked Reference."""
    values: dict[str, Any] = {}
    for container in _METADATA_CONTAINERS:
        raw = source.get(container)
        if isinstance(raw, Mapping):
            values.update(raw)
    locator = source.get("locator")
    if isinstance(locator, Mapping):
        locator_metadata = locator.get("metadata")
        if isinstance(locator_metadata, Mapping):
            for field, value in locator_metadata.items():
                values.setdefault(field, value)
    for field in _SOURCE_METADATA_FIELDS:
        if field not in values and field in source:
            values[field] = source[field]
    for reference in _source_reference_nodes(state, str(source["id"])):
        for field in _REFERENCE_METADATA_FIELDS:
            if not _present(values.get(field)) and _present(reference.get(field)):
                values[field] = reference[field]
        # Reference records use ``venue`` for a book's publisher.
        if not _present(values.get("publisher")) and _present(reference.get("venue")):
            values["publisher"] = reference["venue"]
    archive = values.get("archive")
    if not _present(values.get("archive_url")) and isinstance(archive, Mapping):
        values["archive_url"] = archive.get("url")
    return values


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def record_holding(
    graph: Any,
    *,
    source_id: str,
    source_type: str,
    epistemic_role: str | None = None,
    authority: str | None = None,
    review_status: str | None = None,
    use_constraints: Sequence[str] | None = None,
    scope: str | None = None,
    rationale: str | None = None,
    reviewer: str | None = None,
    status: str = "proposed",
    confidence: float | None = None,
) -> dict[str, Any]:
    """Record one reviewer-authored holding and link its Source to it.

    Omitted fields take the conservative default for ``source_type``.  An
    explicit empty ``use_constraints`` list means no constraints; ``None``
    takes the format default.  Multiple holdings per Source are allowed.
    """
    state = _as_state(graph)
    source = _require_source(state, source_id)
    canonical_type = _source_type(source_type)
    existing_type = source.get("source_type")
    if _present(existing_type) and _token(existing_type) != canonical_type:
        raise HoldingError(
            f"Source {source['id']} is recorded as {existing_type!r}, "
            f"not {canonical_type!r}"
        )

    spec = SOURCE_TYPES[canonical_type]
    role = (
        _epistemic_role(epistemic_role)
        if epistemic_role is not None
        else spec["epistemic_role"]
    )
    canonical_authority = (
        _authority(authority) if authority is not None else spec["authority"]
    )
    canonical_review = (
        _review_status(review_status)
        if review_status is not None
        else spec["review_status"]
    )
    if use_constraints is None:
        constraints = list(spec["use_constraints"])
    else:
        constraints = _string_items(use_constraints, "use_constraints")
    canonical_status = _holding_status(status)
    scope = _optional_string(scope, "scope")
    rationale = _optional_string(rationale, "rationale")
    reviewer = _optional_string(reviewer, "reviewer")
    if canonical_status == "accepted" and reviewer is None:
        raise HoldingError("an accepted holding requires a reviewer")
    score = _confidence(confidence) if confidence is not None else None

    fields: dict[str, Any] = {
        "source_id": source["id"],
        "source_type": canonical_type,
        "epistemic_role": role,
        "authority": canonical_authority,
        "review_status": canonical_review,
        "use_constraints": constraints,
        "status": canonical_status,
        "truth_assessed": False,
    }
    if scope is not None:
        fields["scope"] = scope
    if rationale is not None:
        fields["rationale"] = rationale
    if reviewer is not None:
        fields["reviewer"] = reviewer
    if score is not None:
        fields["confidence"] = score
    _json_safe(fields, "holding")

    holding = graph.record("Holding", **fields)
    edge = graph.link(
        source["id"],
        "held_as",
        holding["id"],
        provenance={
            "kind": "epistemic_holding",
            "source_type": canonical_type,
            "epistemic_role": role,
            "status": canonical_status,
            "reviewer": reviewer,
        },
        rationale=rationale,
    )
    return {
        "holding": holding,
        "source": dict(source),
        "edge": edge,
        "created": True,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def _first_text(*values: Any) -> str:
    return next((_collapse(value) for value in values if _present(value)), "")


def _missing_metadata(
    metadata: Mapping[str, Any], fields: Sequence[str]
) -> list[str]:
    return [field for field in fields if not _present(metadata.get(field))]


def _holding_rationale(
    holding: Mapping[str, Any], edge: Mapping[str, Any] | None
) -> str:
    provenance = edge.get("provenance") if edge is not None else None
    return _first_text(
        holding.get("rationale"),
        edge.get("rationale") if edge is not None else None,
        provenance.get("rationale") if isinstance(provenance, Mapping) else None,
    )


def _holding_scope(holding: Mapping[str, Any], edge: Mapping[str, Any] | None) -> str:
    provenance = edge.get("provenance") if edge is not None else None
    return _first_text(
        holding.get("scope"),
        provenance.get("scope") if isinstance(provenance, Mapping) else None,
    )


def _check_holding_node(
    state: Mapping[str, Any], holding: Mapping[str, Any]
) -> dict[str, Any]:
    holding_id = str(holding["id"])
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    source_id = holding.get("source_id")
    source = _node_by_id(state, source_id) if isinstance(source_id, str) else None
    edges = _held_as_edges(state, source_id) if isinstance(source_id, str) else []
    edge = next(
        (item for item in edges if item.get("tgt") == holding_id),
        None,
    )
    if source is None or source.get("type") != "Source":
        issues.append({
            "code": "holding_source_missing",
            "holding_id": holding_id,
            "source_id": source_id,
            "reason": f"Holding source {source_id!r} does not exist as a Source.",
        })
    if edge is None:
        issues.append({
            "code": "holding_link_missing",
            "holding_id": holding_id,
            "source_id": source_id,
            "reason": "Holding has no held_as edge from its Source.",
        })

    source_type = _token(holding.get("source_type"))
    role = _token(holding.get("epistemic_role"))
    status = _token(holding.get("status"))
    rationale = _holding_rationale(holding, edge)
    scope = _holding_scope(holding, edge)
    metadata = _source_metadata(state, source) if source is not None else {}

    if source_type not in SOURCE_TYPES:
        issues.append({
            "code": "holding_source_type_invalid",
            "holding_id": holding_id,
            "source_type": holding.get("source_type"),
            "reason": "Holding has an unrecognized source_type.",
        })
    if status not in HOLDING_STATUSES:
        issues.append({
            "code": "holding_status_invalid",
            "holding_id": holding_id,
            "status": holding.get("status"),
            "reason": "Holding status is missing or unrecognized.",
        })
    if holding.get("truth_assessed") is True:
        issues.append({
            "code": "truth_assessed_claimed",
            "holding_id": holding_id,
            "reason": "Holding must retain truth_assessed=False.",
        })

    if source_type == "book":
        if role == "evidence" and not (rationale and scope):
            warnings.append({
                "code": "book_held_as_evidence_without_rationale_or_scope",
                "holding_id": holding_id,
                "reason": (
                    "A book is held as evidence without both a rationale and a "
                    "scope; record what evidence was extracted and where it applies."
                ),
            })
        missing = [field for field in ("publisher", "year") if not _present(metadata.get(field))]
        if missing:
            warnings.append({
                "code": "book_metadata_incomplete",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Book metadata is missing "
                    + " and ".join(missing)
                    + "; the edition cannot be identified reliably."
                ),
            })
    if source_type == "note" and role == "evidence":
        issues.append({
            "code": "note_held_as_evidence",
            "holding_id": holding_id,
            "reason": (
                "A personal note is held as evidence. Notes are personal "
                "context, not external evidence, unless separately linked to support."
            ),
        })
    if source_type == "website":
        missing = [
            field
            for field in ("accessed_at", "archive_url")
            if not _present(metadata.get(field))
        ]
        if missing:
            warnings.append({
                "code": "website_missing_archive_metadata",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Website metadata is missing "
                    + " and ".join(missing)
                    + "; a mutable page needs an access date and archive."
                ),
            })

    if source_type in _PLAN_SOURCE_TYPES and role in {"evidence", "reported_evidence"}:
        issues.append({
            "code": "plan_held_as_result_evidence",
            "holding_id": holding_id,
            "source_type": source_type,
            "reason": (
                f"A {source_type.replace('_', ' ')} is held as result evidence. "
                "Plans and protocols commit to what will be done; they do not "
                "report an outcome and cannot be held as result evidence."
            ),
        })
    if source_type in _NORMATIVE_SOURCE_TYPES and role in {
        "evidence",
        "reported_evidence",
    }:
        issues.append({
            "code": "normative_held_as_empirical_evidence",
            "holding_id": holding_id,
            "source_type": source_type,
            "reason": (
                f"A {source_type.replace('_', ' ')} is held as empirical "
                "evidence. Normative and legal material constrains or defines; "
                "it is not evidence about how the world behaves."
            ),
        })
    if source_type in _SYNTHETIC_SOURCE_TYPES and role in {
        "evidence",
        "reported_evidence",
    }:
        issues.append({
            "code": "synthetic_held_as_evidence",
            "holding_id": holding_id,
            "source_type": source_type,
            "reason": (
                "Synthetic or model-generated material is held as evidence. It "
                "is generated, not observed, and cannot be evidence without "
                "independent validation."
            ),
        })

    if source_type in _VERSIONED_ARTIFACT_TYPES:
        missing = _missing_metadata(metadata, _ARTIFACT_METADATA_FIELDS)
        if missing:
            warnings.append({
                "code": "artifact_metadata_incomplete",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Technical artifact metadata is missing "
                    + " and ".join(missing)
                    + "; record the version, licence and checksum so the "
                    "artifact can be identified and reused."
                ),
            })
    normative_fields = _NORMATIVE_METADATA_FIELDS.get(source_type, ())
    if normative_fields:
        missing = _missing_metadata(metadata, normative_fields)
        if missing:
            warnings.append({
                "code": "normative_metadata_incomplete",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Normative source metadata is missing "
                    + " and ".join(missing)
                    + "; record the jurisdiction or issuing body that gives "
                    "the source its scope."
                ),
            })
    if source_type in _HUMAN_SOURCE_TYPES:
        missing: list[str] = []
        if not _present(metadata.get("consent")):
            missing.append("consent")
        if not any(
            _present(metadata.get(field)) for field in _HUMAN_CONTEXT_FIELDS
        ):
            missing.append("context")
        if missing:
            warnings.append({
                "code": "human_source_missing_consent_or_context",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Human-participant material is missing "
                    + " and ".join(missing)
                    + "; record consent and the collection context or "
                    "instrument before relying on it."
                ),
            })
    if source_type in _MUTABLE_WEB_TYPES:
        missing = _missing_metadata(metadata, ("accessed_at", "archive_url"))
        if missing:
            warnings.append({
                "code": "mutable_source_missing_access_metadata",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Mutable web material is missing "
                    + " and ".join(missing)
                    + "; record the access date and an archive snapshot."
                ),
            })
    if source_type in _REVIEW_ARTIFACT_TYPES:
        if not any(_present(metadata.get(field)) for field in _TARGET_FIELDS):
            warnings.append({
                "code": "review_artifact_missing_target",
                "holding_id": holding_id,
                "missing": ["target"],
                "reason": (
                    "A review or integrity artifact is missing the target work "
                    "it addresses; record the target identifier or title."
                ),
            })
    physical_fields = _PHYSICAL_METADATA_FIELDS.get(source_type, ())
    if physical_fields:
        missing = _missing_metadata(metadata, physical_fields)
        if missing:
            warnings.append({
                "code": "physical_source_metadata_incomplete",
                "holding_id": holding_id,
                "missing": missing,
                "reason": (
                    "Physical or instrument-derived material is missing "
                    + " and ".join(missing)
                    + "; record custody, calibration or instrument details "
                    "before treating the values as data."
                ),
            })

    if status == "accepted" and not _present(holding.get("reviewer")):
        issues.append({
            "code": "accepted_without_reviewer",
            "holding_id": holding_id,
            "reason": "An accepted holding needs a named reviewer.",
        })
    if status == "rejected" and not rationale:
        warnings.append({
            "code": "rejected_without_rationale",
            "holding_id": holding_id,
            "reason": "A rejected holding has no recorded rationale.",
        })

    return {
        "holding_id": holding_id,
        "source_id": source_id if isinstance(source_id, str) else None,
        "source_type": source_type or None,
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "holdings": 1,
            "held_as_edges": len(edges),
            "issues": len(issues),
            "warnings": len(warnings),
        },
        "truth_assessed": False,
    }


def check_holding(state: Any, holding_id: str) -> dict[str, Any]:
    """Check one holding's link, status and format-specific risks."""
    current = _as_state(state)
    holding = _require_holding(current, holding_id)
    return _check_holding_node(current, holding)


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def _group_counts(
    holdings: Sequence[Mapping[str, Any]], field: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for holding in holdings:
        key = _token(holding.get(field)) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def holding_report(state: Any, source_id: str | None = None) -> dict[str, Any]:
    """Return holdings, format defaults and descriptive counts; no truth verdict."""
    current = _as_state(state)
    source: Mapping[str, Any] | None = None
    if source_id is not None:
        source = _require_source(current, source_id)
    holdings = sorted(
        (
            node
            for node in _nodes(current)
            if node.get("type") == "Holding"
            and (
                source_id is None
                or node.get("source_id") == source_id
                or node.get("id")
                in {
                    str(edge.get("tgt"))
                    for edge in _held_as_edges(current, str(source_id))
                }
            )
        ),
        key=lambda node: str(node["id"]),
    )
    source_types = sorted({
        _token(holding.get("source_type"))
        for holding in holdings
        if _token(holding.get("source_type")) in SOURCE_TYPES
    })
    result: dict[str, Any] = {
        "holdings": [dict(holding) for holding in holdings],
        "defaults": {
            source_type: _spec_copy(source_type) for source_type in source_types
        },
        "counts": {
            "holdings": len(holdings),
            "source_type": _group_counts(holdings, "source_type"),
            "epistemic_role": _group_counts(holdings, "epistemic_role"),
            "authority": _group_counts(holdings, "authority"),
            "status": _group_counts(holdings, "status"),
        },
        "truth_assessed": False,
    }
    if source is not None:
        result["source_id"] = source["id"]
        result["source"] = dict(source)
    return result


# --------------------------------------------------------------------------- #
# claim context
# --------------------------------------------------------------------------- #
def _source_locator_values(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, Mapping):
            values.append(value)
        elif isinstance(value, str) and value.strip():
            values.append({"path": value.strip()})

    add(node.get("locator"))
    add(node.get("source_locator"))
    metadata = node.get("metadata")
    if isinstance(metadata, Mapping):
        add(metadata.get("locator"))
        values.append(metadata)
    values.append({
        field: node[field] for field in _LOCATOR_FIELDS if _present(node.get(field))
    })
    return [value for value in values if value]


def _claim_locator_sources(
    state: Mapping[str, Any], claim: Mapping[str, Any]
) -> tuple[set[str], set[str]]:
    source_ids: set[str] = set()
    reference_ids: set[str] = set()
    raw_locator = claim.get("source_locator")
    if isinstance(raw_locator, str) and raw_locator.strip():
        locator: Mapping[str, Any] = {"path": raw_locator.strip()}
    elif isinstance(raw_locator, Mapping):
        locator = raw_locator
    else:
        fallback = claim.get("locator")
        locator = fallback if isinstance(fallback, Mapping) else {}
    explicit = locator.get("source_id") or claim.get("source_id")
    if isinstance(explicit, str) and explicit.strip():
        source_ids.add(explicit.strip())
    listed = locator.get("source_ids") or claim.get("source_ids")
    if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes, bytearray)):
        source_ids.update(
            item.strip() for item in listed if isinstance(item, str) and item.strip()
        )
    reference = locator.get("reference_id")
    if isinstance(reference, str) and reference.strip():
        reference_ids.add(reference.strip())

    lookup = {
        field: locator.get(field)
        for field in _LOCATOR_FIELDS
        if _present(locator.get(field))
    }
    if lookup:
        for node in _nodes(state):
            if node.get("type") != "Source":
                continue
            if any(
                _present(candidate.get(field)) and candidate.get(field) == value
                for candidate in _source_locator_values(node)
                for field, value in lookup.items()
            ):
                source_ids.add(str(node["id"]))
    return source_ids, reference_ids


def _claim_sources(
    state: Mapping[str, Any], claim: Mapping[str, Any]
) -> tuple[list[Mapping[str, Any]], list[str]]:
    claim_id = str(claim["id"])
    source_ids, reference_ids = _claim_locator_sources(state, claim)
    for edge in _edges(state):
        if edge.get("type") != "cites" or edge.get("src") != claim_id:
            continue
        target = _node_by_id(state, edge.get("tgt"))
        if target is None:
            continue
        if target.get("type") == "Source":
            source_ids.add(str(target["id"]))
        elif target.get("type") == "Reference":
            reference_ids.add(str(target["id"]))

    for reference_id in sorted(reference_ids):
        for edge in _edges(state):
            if (
                edge.get("type") == "manifestation_of"
                and edge.get("tgt") == reference_id
                and isinstance(edge.get("src"), str)
            ):
                source_ids.add(edge["src"])
    for node in _nodes(state):
        if (
            node.get("type") == "Source"
            and node.get("reference_id") in reference_ids
        ):
            source_ids.add(str(node["id"]))

    sources = [
        node
        for node_id in sorted(source_ids)
        if (node := _node_by_id(state, node_id)) is not None
        and node.get("type") == "Source"
    ]
    return sources, sorted(reference_ids)


def _default_context(sources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if sources:
        source = sources[0]
        source_type = _token(source.get("source_type"))
        spec = _spec_copy(source_type) if source_type in SOURCE_TYPES else None
        if spec is not None:
            return {
                "source_id": source["id"],
                "source_type": source_type,
                "epistemic_role": spec["epistemic_role"],
                "authority": spec["authority"],
                "review_status": spec["review_status"],
                "use_constraints": spec["use_constraints"],
                "required_metadata": spec["required_metadata"],
                "reason": (
                    "No Holding is recorded for this claim's cited Source. "
                    "Use the conservative format defaults and record a reviewer's "
                    "stance before relying on it as evidence."
                ),
                "truth_assessed": False,
            }
    return {
        "source_id": sources[0]["id"] if sources else None,
        "source_type": None,
        "epistemic_role": "background",
        "authority": "unknown",
        "review_status": "unknown",
        "use_constraints": [
            "record_holding_before_use",
            "not_external_evidence",
        ],
        "required_metadata": [],
        "reason": (
            "No Source or Holding is recorded for this claim. Do not treat the "
            "claim's provenance as established; record a Source and a Holding first."
        ),
        "truth_assessed": False,
    }


def claim_holding(state: Any, claim_id: str) -> dict[str, Any]:
    """Return the holdings of the Sources a claim cites.

    Cited Sources are found from explicit ``cites`` edges and from the claim's
    ``source_locator`` (path, root id, hash, URL or source id).  A cited
    Reference is followed to its manifesting Source(s).  When no holdings are
    recorded the result carries a conservative default instead of inventing one.
    """
    current = _as_state(state)
    claim = _require_claim(current, claim_id)
    sources, reference_ids = _claim_sources(current, claim)
    contexts: list[dict[str, Any]] = []
    unheld: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for source in sources:
        holdings = _holdings_for_source(current, str(source["id"]))
        if not holdings:
            source_type = _token(source.get("source_type"))
            unheld.append({
                "source_id": source["id"],
                "source_type": source_type or None,
                "defaults": (
                    _spec_copy(source_type) if source_type in SOURCE_TYPES else None
                ),
                "reason": "The claim cites this Source but it has no Holding.",
            })
            warnings.append({
                "code": "claim_source_without_holding",
                "claim_id": str(claim["id"]),
                "source_id": source["id"],
                "reason": (
                    "The claim cites a Source with no recorded Holding; its "
                    "epistemic role is not established."
                ),
            })
            continue
        for holding in holdings:
            check = _check_holding_node(current, holding)
            edge = next(
                (
                    item
                    for item in _held_as_edges(current, str(source["id"]))
                    if item.get("tgt") == holding["id"]
                ),
                None,
            )
            source_type = _token(source.get("source_type")) or _token(
                holding.get("source_type")
            )
            contexts.append({
                "claim_id": str(claim["id"]),
                "source": dict(source),
                "holding": dict(holding),
                "held_as": dict(edge) if edge is not None else None,
                "defaults": (
                    _spec_copy(source_type) if source_type in SOURCE_TYPES else None
                ),
                "warnings": [dict(item) for item in check["warnings"]],
                "issues": [dict(item) for item in check["issues"]],
                "checks": check,
            })
            for warning in check["warnings"]:
                warnings.append({
                    "claim_id": str(claim["id"]),
                    "source_id": source["id"],
                    **warning,
                })
            for issue in check["issues"]:
                record = {
                    "claim_id": str(claim["id"]),
                    "source_id": source["id"],
                    **issue,
                }
                issues.append(record)
                # Holding-level issues also describe a risk in how this claim
                # uses the source, so surface them in the claim warnings too.
                warnings.append({**record, "severity": "issue"})

    contexts.sort(
        key=lambda item: (
            str(item["source"].get("id") or ""),
            str(item["holding"].get("id") or ""),
        )
    )
    warnings.sort(
        key=lambda item: (
            str(item.get("source_id") or ""),
            str(item.get("holding_id") or ""),
            str(item.get("code") or ""),
        )
    )
    issues.sort(
        key=lambda item: (
            str(item.get("source_id") or ""),
            str(item.get("holding_id") or ""),
            str(item.get("code") or ""),
        )
    )
    return {
        "claim": dict(claim),
        "reference_ids": reference_ids,
        "sources": [dict(source) for source in sources],
        "contexts": contexts,
        "unheld_sources": unheld,
        "default": _default_context(sources) if not contexts else None,
        "warnings": warnings,
        "issues": issues,
        "counts": {
            "sources": len(sources),
            "holdings": len(contexts),
            "contexts": len(contexts),
            "unheld_sources": len(unheld),
            "warnings": len(warnings),
            "issues": len(issues),
        },
        "truth_assessed": False,
    }
