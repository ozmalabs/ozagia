"""Source-format metadata profiles and explicit source registration.

Stage B separates *what a source is* from *how it is held*. A format profile
declares the metadata a book, note, website or other work should carry; a
``Source`` remains the local or cached artifact and a ``Reference`` its
bibliographic work. Registration records declared metadata only: it reads no
files, fetches no URLs and makes no truth or authority judgement.

The epistemic defaults belong to :mod:`ozagia.research.holdings`; this module
only asks that recorder for a ``Holding`` and reports when it is unavailable.
"""
from __future__ import annotations

import hashlib
import importlib
import re
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from ozagia.research import references as refs
from ozagia.research.graph import ResearchError

__all__ = [
    "COMMON_OPTIONAL_METADATA",
    "SOURCE_FORMATS",
    "SourceFormat",
    "SourceFormatError",
    "get_source_format",
    "list_source_formats",
    "normalise_source_metadata",
    "register_source",
]


class SourceFormatError(ResearchError):
    """A source type, metadata field or registration request is invalid."""


@dataclass(frozen=True)
class SourceFormat:
    """One source format's metadata contract and reference behaviour."""

    source_type: str
    required_metadata: tuple[str, ...]
    optional_metadata: tuple[str, ...]
    reference_type: str | None
    bibliographic: bool
    notes: str
    required_any: tuple[tuple[str, ...], ...] = ()

    @property
    def metadata_fields(self) -> tuple[str, ...]:
        return self.required_metadata + self.optional_metadata + tuple(
            name for group in self.required_any for name in group
        )

    def __getitem__(self, key: str) -> Any:
        if key not in {field.name for field in dataclass_fields(self)}:
            raise KeyError(key)
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in {field.name for field in dataclass_fields(self)} else default

    def keys(self) -> list[str]:
        return [field.name for field in dataclass_fields(self)]

    def items(self) -> list[tuple[str, Any]]:
        return [(key, self[key]) for key in self.keys()]

    def values(self) -> list[Any]:
        return [self[key] for key in self.keys()]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "required_metadata": list(self.required_metadata),
            "optional_metadata": list(self.optional_metadata),
            "reference_type": self.reference_type,
            "bibliographic": self.bibliographic,
            "notes": self.notes,
            "required_any": [list(group) for group in self.required_any],
        }


_CITATION_FIELDS = ("citation_key", "citation_keys")

# Fields every source format accepts. Type-specific metadata extends this set;
# a required field is omitted from ``optional_metadata`` but remains accepted.
COMMON_OPTIONAL_METADATA: tuple[str, ...] = (
    "version",
    "retrieved_at",
    "license",
    "checksum",
    "archive_url",
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
    "target_doi",
    "transcript",
    "medium",
    "acquisition",
    "processing",
    "identifier",
    "date",
    "authors",
    "year",
    "title",
    "url",
    "accessed_at",
    "published_at",
    "site",
    "publisher",
    "edition",
    "isbn",
    "doi",
    "language",
    "pages",
    "notebook",
    "page",
    "confidentiality",
    "venue",
    "event",
    "pmid",
    "arxiv",
    "description",
    "notes",
)


def _optional_metadata(
    extra: Iterable[str],
    required: Iterable[str] = (),
    required_any: Iterable[Iterable[str]] = (),
) -> tuple[str, ...]:
    """Return type-specific optional fields followed by the common fields.

    Required fields and fields named by a ``required_any`` group are not
    repeated in the optional list, but every listed name remains accepted.
    """
    excluded = set(required)
    for group in required_any:
        excluded.update(group)
    ordered: list[str] = []
    for field in (*tuple(extra), *COMMON_OPTIONAL_METADATA):
        if field in excluded or field in ordered:
            continue
        ordered.append(field)
    return tuple(ordered)


def _source_format(
    source_type: str,
    required: tuple[str, ...],
    extra: tuple[str, ...],
    reference_type: str | None,
    bibliographic: bool,
    notes: str,
    *,
    required_any: tuple[tuple[str, ...], ...] = (),
) -> SourceFormat:
    return SourceFormat(
        source_type,
        required,
        _optional_metadata(extra, required, required_any),
        reference_type,
        bibliographic,
        notes,
        required_any,
    )


SOURCE_FORMATS: dict[str, SourceFormat] = {
    # ------------------------------------------------------------------ #
    # General bibliographic works
    # ------------------------------------------------------------------ #
    "book": _source_format(
        "book",
        ("title", "authors"),
        ("year", "publisher", "edition", "isbn", "doi", "language", "pages"),
        "book",
        True,
        "A book is an authored work that may mix evidence and interpretation; "
        "the default holding is authorial interpretation, not established fact.",
    ),
    "note": _source_format(
        "note",
        (),
        ("author", "date", "notebook", "page", "confidentiality"),
        None,
        False,
        "A note is personal context, not external evidence, and normally needs "
        "no bibliographic Reference.",
        required_any=(("title", "context"),),
    ),
    "website": _source_format(
        "website",
        ("url", "accessed_at"),
        ("title", "site", "author", "published_at", "archive_url", "license"),
        "web",
        True,
        "Websites are mutable and date-sensitive; record the access date and, "
        "where possible, an archive URL.",
    ),
    "article": _source_format(
        "article",
        ("title", "authors", "year"),
        (
            "venue", "volume", "issue", "pages", "doi", "url", "publisher",
            "language", "abstract", "license", "arxiv", "pmid", "pmcid",
        ),
        "article",
        True,
        "An article is reported work; registration does not establish peer "
        "review, methodological quality or support for a claim.",
    ),
    "dataset": _source_format(
        "dataset",
        ("title", "year"),
        (
            "authors", "creator", "publisher", "repository", "doi", "url", "version",
            "license", "accessed_at", "description", "checksum", "provenance",
        ),
        "dataset",
        True,
        "A dataset is raw or processed material; its collection method, "
        "version and licence determine what it can support.",
    ),
    "software": _source_format(
        "software",
        ("title",),
        (
            "authors", "creator", "year", "version", "publisher", "repository", "doi",
            "url", "license", "language", "description", "commit", "environment",
        ),
        "software",
        True,
        "Software is an executable artifact; record the version and repository "
        "so a result can be reproduced.",
    ),
    "standard": _source_format(
        "standard",
        ("title", "identifier"),
        (
            "organization", "publisher", "year", "edition", "url", "doi",
            "status", "language", "issuing_body", "jurisdiction", "version",
        ),
        "standard",
        True,
        "A standard is a normative specification, not evidence that an "
        "implementation or practice conforms to it.",
    ),
    "report": _source_format(
        "report",
        ("title", "authors", "year"),
        (
            "institution", "publisher", "number", "doi", "url", "language",
            "abstract", "issuing_body",
        ),
        "report",
        True,
        "Reports may be unpublished or internally reviewed; record the issuing "
        "body and review status rather than assuming peer review.",
    ),
    "thesis": _source_format(
        "thesis",
        ("title", "authors", "year"),
        ("institution", "degree", "doi", "url", "language", "abstract"),
        "thesis",
        True,
        "A thesis is examined student work; the degree and institution qualify "
        "its scope.",
    ),
    "preprint": _source_format(
        "preprint",
        ("title", "authors", "year"),
        (
            "venue", "repository", "doi", "arxiv", "url", "license", "abstract",
            "version", "status",
        ),
        "preprint",
        True,
        "A preprint has not necessarily been peer reviewed; do not present it "
        "as a published article.",
    ),
    "interview": _source_format(
        "interview",
        ("title",),
        (
            "interviewee", "interviewer", "date", "outlet", "venue", "url",
            "transcript_url", "language", "confidentiality", "consent",
            "participants", "transcript",
        ),
        "interview",
        True,
        "An interview is a situated account; record interviewer, date and any "
        "confidentiality constraint.",
    ),
    "lecture": _source_format(
        "lecture",
        ("title",),
        (
            "speaker", "date", "institution", "venue", "course", "url",
            "slides_url", "language", "transcript",
        ),
        "lecture",
        True,
        "A lecture is presented material and may be didactic or preliminary; "
        "record the venue and date.",
    ),
    "video": _source_format(
        "video",
        ("title", "url"),
        (
            "creator", "author", "site", "published_at", "accessed_at",
            "duration", "archive_url", "license", "transcript",
        ),
        "video",
        True,
        "Video is mutable and edited; record the access date and an archive "
        "where possible.",
    ),
    "chapter": _source_format(
        "chapter",
        ("title", "authors", "year"),
        (
            "book_title", "editors", "pages", "publisher", "edition", "isbn",
            "doi", "url", "language",
        ),
        "chapter",
        True,
        "A chapter is part of a larger book; record the containing work so the "
        "citation is unambiguous.",
    ),
    "other": _source_format(
        "other",
        ("title",),
        (
            "authors", "year", "publisher", "doi", "isbn", "url", "language",
            "abstract", "license",
        ),
        "misc",
        True,
        "Other work is an explicit escape hatch; its authority must be stated "
        "by the reviewer rather than inferred from the label.",
    ),
    # ------------------------------------------------------------------ #
    # Research artifacts: plans, notebooks and technical artifacts
    # ------------------------------------------------------------------ #
    "protocol": _source_format(
        "protocol",
        (),
        (
            "version", "date", "registry_id", "identifier", "issuing_body",
            "repository", "doi", "url", "status", "supersedes",
        ),
        "misc",
        True,
        "A protocol states intended methods before results exist; it is plan "
        "and method, not evidence that the plan was executed or worked.",
        required_any=(("title", "context"),),
    ),
    "preregistration": _source_format(
        "preregistration",
        (),
        (
            "version", "date", "registry_id", "identifier", "issuing_body",
            "repository", "doi", "url", "status", "supersedes",
        ),
        "misc",
        True,
        "A preregistration fixes hypotheses and analysis choices in advance; "
        "it is a plan, not a result.",
        required_any=(("title", "context"),),
    ),
    "registered_report": _source_format(
        "registered_report",
        (),
        (
            "version", "date", "registry_id", "identifier", "issuing_body",
            "repository", "doi", "url", "status", "supersedes",
        ),
        "report",
        True,
        "A registered report is a reviewed plan; treat the plan and any later "
        "results as separate records.",
        required_any=(("title", "context"),),
    ),
    "analysis_plan": _source_format(
        "analysis_plan",
        (),
        (
            "version", "date", "registry_id", "identifier", "repository",
            "doi", "url", "status", "supersedes", "instrument", "processing",
        ),
        "misc",
        True,
        "An analysis plan declares intended transformations and tests; it is "
        "not evidence that the analysis was run as written.",
        required_any=(("title", "context"),),
    ),
    "lab_notebook": _source_format(
        "lab_notebook",
        (),
        (
            "author", "date", "notebook", "page", "confidentiality",
            "participants", "instrument", "acquisition", "processing",
        ),
        None,
        False,
        "A lab notebook is primary personal context; record the author, date "
        "and page so an entry can be located and attributed.",
        required_any=(("title", "context"),),
    ),
    "field_notes": _source_format(
        "field_notes",
        (),
        (
            "author", "date", "notebook", "page", "confidentiality",
            "site", "participants", "instrument", "medium", "acquisition",
        ),
        None,
        False,
        "Field notes are situated primary observations; record the site, date "
        "and any consent constraint before quoting them.",
        required_any=(("title", "context"),),
    ),
    "computational_notebook": _source_format(
        "computational_notebook",
        ("title",),
        (
            "authors", "version", "repository", "commit", "environment",
            "url", "license", "language", "description", "notebook",
        ),
        "software",
        True,
        "A computational notebook is executable method; record the version, "
        "repository and environment needed to reproduce it.",
    ),
    "workflow": _source_format(
        "workflow",
        ("title",),
        (
            "authors", "version", "repository", "commit", "environment",
            "url", "license", "language", "description", "container",
        ),
        "software",
        True,
        "A workflow is a reproducible method; record its version, repository "
        "and execution environment.",
    ),
    "database": _source_format(
        "database",
        ("title",),
        (
            "authors", "version", "license", "checksum", "repository", "url",
            "doi", "description", "provenance", "accessed_at", "status",
            "supersedes",
        ),
        "dataset",
        True,
        "A database is a versioned primary-data container; record its version, "
        "licence and checksum.",
    ),
    "model": _source_format(
        "model",
        ("title",),
        (
            "authors", "version", "license", "checksum", "repository", "url",
            "doi", "description", "environment", "calibration", "status",
            "supersedes",
        ),
        "software",
        True,
        "A model is an executable or mathematical artifact; version, licence "
        "and checksum identify what was actually used.",
    ),
    "benchmark": _source_format(
        "benchmark",
        ("title",),
        (
            "authors", "version", "license", "checksum", "repository", "url",
            "doi", "description", "environment", "calibration", "status",
            "supersedes",
        ),
        "dataset",
        True,
        "A benchmark defines an evaluation procedure and examples; record the "
        "version, licence and checksum used for a comparison.",
    ),
    "ontology": _source_format(
        "ontology",
        ("title",),
        (
            "authors", "version", "license", "checksum", "repository", "url",
            "doi", "description", "status", "supersedes", "scope",
        ),
        "misc",
        True,
        "An ontology fixes terms and relations; it is a normative vocabulary, "
        "not evidence that the world matches it.",
    ),
    # ------------------------------------------------------------------ #
    # Normative and institutional sources
    # ------------------------------------------------------------------ #
    "regulation": _source_format(
        "regulation",
        ("title",),
        (
            "citation", "identifier", "date", "version", "status", "url",
            "supersedes", "scope",
        ),
        "misc",
        True,
        "A regulation is normative and legally situated; record its issuing "
        "body or jurisdiction before relying on it.",
        required_any=(("issuing_body", "jurisdiction"),),
    ),
    "statute": _source_format(
        "statute",
        ("title",),
        (
            "citation", "identifier", "date", "version", "status", "url",
            "supersedes", "scope", "edition",
        ),
        "misc",
        True,
        "A statute is enacted law; record the citation, issuing body or "
        "jurisdiction and the version in force.",
        required_any=(("issuing_body", "jurisdiction"),),
    ),
    "court_case": _source_format(
        "court_case",
        ("title",),
        (
            "citation", "identifier", "date", "version", "status", "url",
            "supersedes", "docket", "scope",
        ),
        "misc",
        True,
        "A court case is a legal decision with a jurisdiction and citation; it "
        "binds only within its legal scope.",
        required_any=(("issuing_body", "jurisdiction"),),
    ),
    "guideline": _source_format(
        "guideline",
        ("title", "issuing_body"),
        (
            "version", "date", "identifier", "citation", "url", "doi",
            "status", "supersedes", "jurisdiction", "scope",
        ),
        "report",
        True,
        "A guideline is institutional interpretation or instruction, not "
        "empirical evidence that following it works.",
    ),
    "consensus_statement": _source_format(
        "consensus_statement",
        ("title", "issuing_body"),
        (
            "version", "date", "identifier", "citation", "url", "doi",
            "status", "supersedes", "jurisdiction", "scope", "panel",
        ),
        "report",
        True,
        "A consensus statement records agreement among a panel; record the "
        "issuing body and do not treat consensus as proof.",
    ),
    "patent": _source_format(
        "patent",
        ("title",),
        (
            "jurisdiction", "date", "version", "status", "url", "supersedes",
            "application_number", "publication_number", "citation",
        ),
        "misc",
        True,
        "A patent claims a technical monopoly; it is a legal and technical "
        "claim, not empirical evidence that the invention works.",
        required_any=(("identifier", "issuing_body"),),
    ),
    "ethics_approval": _source_format(
        "ethics_approval",
        ("title",),
        (
            "institution", "identifier", "date", "version", "status", "url",
            "jurisdiction", "scope", "supersedes",
        ),
        "misc",
        True,
        "An ethics approval is a governance record; record the institution, "
        "approval identifier and date.",
    ),
    "consent_form": _source_format(
        "consent_form",
        ("title",),
        (
            "institution", "identifier", "date", "version", "status", "url",
            "scope", "confidentiality", "consent", "supersedes",
        ),
        "misc",
        True,
        "A consent form is a governance artifact; it documents what was "
        "agreed, not what happened in the study.",
    ),
    "data_use_agreement": _source_format(
        "data_use_agreement",
        ("title",),
        (
            "institution", "identifier", "date", "version", "status", "url",
            "scope", "license", "consent", "confidentiality", "supersedes",
        ),
        "misc",
        True,
        "A data-use agreement constrains permitted use; record its scope, "
        "version and licensing terms.",
    ),
    "funding": _source_format(
        "funding",
        ("title",),
        (
            "institution", "funder", "identifier", "date", "version", "status",
            "url", "scope", "conflict_of_interest", "supersedes",
        ),
        "misc",
        True,
        "A funding record is context for independence and conflicts; it is not "
        "evidence for a claim.",
    ),
    "conflict_of_interest": _source_format(
        "conflict_of_interest",
        ("title",),
        (
            "institution", "funder", "identifier", "date", "version", "status",
            "url", "scope", "independence", "conflict_of_interest", "supersedes",
        ),
        "misc",
        True,
        "A conflict-of-interest declaration is context; it qualifies how other "
        "evidence should be read rather than adding evidence.",
    ),
    # ------------------------------------------------------------------ #
    # Meta-science and review artifacts
    # ------------------------------------------------------------------ #
    "peer_review": _source_format(
        "peer_review",
        ("title",),
        (
            "venue", "date", "reviewer", "recommendation", "version", "status",
            "url", "confidentiality",
        ),
        "misc",
        True,
        "A peer-review report is a review artifact about another work; it is "
        "not itself the reviewed evidence.",
        required_any=(("target_id", "target_doi"),),
    ),
    "editorial_decision": _source_format(
        "editorial_decision",
        ("title",),
        (
            "venue", "date", "editor", "decision", "version", "status", "url",
            "confidentiality",
        ),
        "misc",
        True,
        "An editorial decision is a review artifact; record the target work "
        "and decision rather than treating it as evidence.",
        required_any=(("target_id", "target_doi"),),
    ),
    "author_response": _source_format(
        "author_response",
        ("title",),
        (
            "venue", "date", "authors", "version", "status", "url",
            "confidentiality",
        ),
        "misc",
        True,
        "An author response is a situated reply to review; it documents the "
        "exchange, not the truth of the work.",
        required_any=(("target_id", "target_doi"),),
    ),
    "correction": _source_format(
        "correction",
        ("title",),
        (
            "identifier", "date", "version", "status", "url", "description",
            "supersedes", "venue",
        ),
        "misc",
        True,
        "A correction is an integrity event about a target work; record the "
        "target identifier and the corrected scope.",
        required_any=(("target_id", "target_doi"),),
    ),
    "retraction_notice": _source_format(
        "retraction_notice",
        ("title",),
        (
            "identifier", "date", "version", "status", "url", "description",
            "reason", "supersedes", "venue",
        ),
        "misc",
        True,
        "A retraction notice is an integrity event; record the target work and "
        "do not count the retracted work as current evidence.",
        required_any=(("target_id", "target_doi"),),
    ),
    "conference_abstract": _source_format(
        "conference_abstract",
        ("title",),
        (
            "authors", "year", "event", "venue", "conference", "doi", "url",
            "abstract", "license", "slides_url",
        ),
        "inproceedings",
        True,
        "A conference abstract is a short report with little methodological "
        "detail; record authors or the event it belongs to.",
        required_any=(("authors", "event", "venue"),),
    ),
    "poster": _source_format(
        "poster",
        ("title",),
        (
            "authors", "year", "event", "venue", "conference", "url",
            "abstract", "license", "medium",
        ),
        "misc",
        True,
        "A poster is presented work and may be preliminary; record authors, "
        "the event and the date.",
        required_any=(("authors", "event", "venue"),),
    ),
    "slides": _source_format(
        "slides",
        ("title",),
        (
            "authors", "year", "event", "venue", "conference", "url",
            "slides_url", "license", "transcript",
        ),
        "misc",
        True,
        "Slides are presented material; record the event and date, and treat "
        "them as a report rather than a full method.",
        required_any=(("authors", "event", "venue"),),
    ),
    # ------------------------------------------------------------------ #
    # Human, grey and secondary sources
    # ------------------------------------------------------------------ #
    "oral_history": _source_format(
        "oral_history",
        ("title",),
        (
            "interviewee", "participants", "interviewer", "date", "consent",
            "transcript", "transcript_url", "url", "language",
            "confidentiality", "repository",
        ),
        "interview",
        True,
        "An oral history is primary testimony; record consent, context and any "
        "confidentiality constraint.",
    ),
    "focus_group": _source_format(
        "focus_group",
        ("title",),
        (
            "participants", "facilitator", "date", "consent", "transcript",
            "transcript_url", "url", "language", "confidentiality",
        ),
        "misc",
        True,
        "A focus group is facilitated primary testimony; record participants, "
        "consent and the discussion context.",
    ),
    "survey": _source_format(
        "survey",
        ("title",),
        (
            "authors", "date", "version", "instrument", "participants",
            "consent", "url", "license", "description", "repository", "doi",
        ),
        "misc",
        True,
        "A survey is a method and primary-data record; record the instrument, "
        "sampling and consent context.",
    ),
    "questionnaire": _source_format(
        "questionnaire",
        ("title",),
        (
            "authors", "date", "version", "instrument", "participants",
            "consent", "url", "license", "description", "repository", "doi",
        ),
        "misc",
        True,
        "A questionnaire is an instrument; record its version, administration "
        "and consent context.",
    ),
    "news": _source_format(
        "news",
        ("url", "accessed_at"),
        (
            "title", "author", "site", "publisher", "published_at",
            "archive_url", "language", "license", "description",
        ),
        "web",
        True,
        "News is mutable background; record the access date and an archive "
        "rather than assuming the current page matches the cited one.",
    ),
    "blog": _source_format(
        "blog",
        ("url", "accessed_at"),
        (
            "title", "author", "site", "publisher", "published_at",
            "archive_url", "language", "license", "description",
        ),
        "web",
        True,
        "A blog is self-published background; record the access date and an "
        "archive where possible.",
    ),
    "press_release": _source_format(
        "press_release",
        ("url", "accessed_at"),
        (
            "title", "author", "site", "publisher", "institution",
            "published_at", "archive_url", "language", "license", "description",
        ),
        "web",
        True,
        "A press release is institutional promotion; record the issuer, access "
        "date and archive, and do not treat it as the underlying report.",
    ),
    "wiki": _source_format(
        "wiki",
        ("url", "accessed_at"),
        (
            "title", "author", "site", "publisher", "published_at",
            "archive_url", "language", "license", "version", "revision",
            "description",
        ),
        "web",
        True,
        "A wiki page is mutable and collaboratively edited; record the access "
        "date, archive and page version.",
    ),
    "forum": _source_format(
        "forum",
        ("url", "accessed_at"),
        (
            "title", "author", "site", "publisher", "published_at",
            "archive_url", "language", "license", "thread", "description",
        ),
        "web",
        True,
        "A forum post is unverified background; record the access date, archive "
        "and thread context.",
    ),
    "social_post": _source_format(
        "social_post",
        ("url", "accessed_at"),
        (
            "title", "author", "site", "platform", "publisher", "published_at",
            "archive_url", "language", "license", "description",
        ),
        "web",
        True,
        "A social post is unverified background and may be deleted or edited; "
        "record the access date and archive.",
    ),
    "podcast": _source_format(
        "podcast",
        ("title",),
        (
            "creator", "site", "publisher", "date", "url", "transcript",
            "transcript_url", "duration", "episode", "language", "license",
            "archive_url",
        ),
        "audio",
        True,
        "A podcast is reported conversation; record the episode date, a URL "
        "and a transcript where available.",
        required_any=(("url", "date"),),
    ),
    "webinar": _source_format(
        "webinar",
        ("title",),
        (
            "creator", "speaker", "site", "publisher", "date", "url",
            "transcript", "transcript_url", "duration", "event", "language",
            "license", "archive_url",
        ),
        "video",
        True,
        "A webinar is presented material; record the event, date, a URL and a "
        "transcript where available.",
        required_any=(("url", "date"),),
    ),
    "archival_document": _source_format(
        "archival_document",
        ("title",),
        (
            "archive", "collection", "date", "identifier", "url", "repository",
            "language", "medium", "description", "provenance", "confidentiality",
        ),
        "misc",
        True,
        "An archival document is primary material; record the archive, "
        "collection and date needed to locate it.",
    ),
    "letter": _source_format(
        "letter",
        ("title",),
        (
            "authors", "author", "recipient", "archive", "collection", "date",
            "identifier", "url", "repository", "language", "medium",
            "description", "provenance", "confidentiality",
        ),
        "misc",
        True,
        "A letter is primary correspondence; record author, recipient, archive "
        "and date wherever they are known.",
    ),
    "reference_work": _source_format(
        "reference_work",
        ("title", "authors"),
        (
            "year", "publisher", "edition", "isbn", "doi", "url", "language",
            "pages", "volume", "description",
        ),
        "book",
        True,
        "A reference work is secondary synthesis; use it to locate primary "
        "sources rather than as evidence in itself.",
    ),
    "textbook": _source_format(
        "textbook",
        ("title", "authors"),
        (
            "year", "publisher", "edition", "isbn", "doi", "url", "language",
            "pages", "description",
        ),
        "book",
        True,
        "A textbook is didactic secondary synthesis; record the edition and "
        "year because content changes between them.",
    ),
    "encyclopedia": _source_format(
        "encyclopedia",
        ("title", "authors"),
        (
            "year", "publisher", "edition", "isbn", "doi", "url", "language",
            "pages", "entry", "description",
        ),
        "book",
        True,
        "An encyclopedia entry is tertiary background; record the edition and "
        "entry author where available.",
    ),
    "fiction": _source_format(
        "fiction",
        ("title",),
        (
            "authors", "creator", "year", "publisher", "edition", "isbn", "doi",
            "url", "language", "pages", "genre", "medium", "description",
        ),
        "book",
        True,
        "Fiction is a primary cultural text for interpretation, not empirical "
        "evidence about the world.",
        required_any=(("creator", "medium"),),
    ),
    "film": _source_format(
        "film",
        ("title",),
        (
            "creator", "director", "studio", "year", "publisher", "url",
            "language", "duration", "medium", "description",
        ),
        "video",
        True,
        "A film is a primary cultural text; record director, year and the "
        "version or medium viewed.",
        required_any=(("creator", "medium"),),
    ),
    "art": _source_format(
        "art",
        ("title",),
        (
            "creator", "year", "publisher", "collection", "medium", "url",
            "language", "description", "provenance",
        ),
        "misc",
        True,
        "A work of art is a primary cultural object; record creator, medium and "
        "the collection or provenance when available.",
        required_any=(("creator", "medium"),),
    ),
    # ------------------------------------------------------------------ #
    # Physical and synthetic primary data
    # ------------------------------------------------------------------ #
    "specimen": _source_format(
        "specimen",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "confidentiality",
        ),
        None,
        False,
        "A specimen is primary material; record its identifier, chain of "
        "custody, instrument and calibration.",
        required_any=(("title", "identifier"),),
    ),
    "sample": _source_format(
        "sample",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "confidentiality",
        ),
        None,
        False,
        "A sample is primary material; record its identifier, chain of custody "
        "and the conditions under which it was taken.",
        required_any=(("title", "identifier"),),
    ),
    "assay": _source_format(
        "assay",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "protocol", "confidentiality",
        ),
        None,
        False,
        "An assay is a measurement procedure and result; record the instrument, "
        "calibration and processing used.",
        required_any=(("title", "identifier"),),
    ),
    "instrument_output": _source_format(
        "instrument_output",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "confidentiality",
        ),
        None,
        False,
        "Instrument output is raw primary data; record the instrument, "
        "calibration, acquisition and processing chain.",
        required_any=(("title", "identifier"),),
    ),
    "image": _source_format(
        "image",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "creator", "license", "confidentiality",
        ),
        None,
        False,
        "An image is primary visual material; record the capture instrument, "
        "processing and chain of custody.",
        required_any=(("title", "identifier"),),
    ),
    "spectrum": _source_format(
        "spectrum",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "confidentiality",
        ),
        None,
        False,
        "A spectrum is instrument-derived primary data; record calibration and "
        "the processing that produced the plotted values.",
        required_any=(("title", "identifier"),),
    ),
    "sequence": _source_format(
        "sequence",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "version", "checksum", "license", "confidentiality",
        ),
        None,
        False,
        "A sequence is primary material; record the identifier, version, "
        "instrument and processing pipeline.",
        required_any=(("title", "identifier"),),
    ),
    "structure": _source_format(
        "structure",
        (),
        (
            "instrument", "calibration", "chain_of_custody", "date",
            "repository", "provenance", "acquisition", "processing", "medium",
            "description", "version", "checksum", "license", "confidentiality",
        ),
        None,
        False,
        "A structure is a derived primary model; record the method, instrument, "
        "version and validation status.",
        required_any=(("title", "identifier"),),
    ),
    "synthetic_data": _source_format(
        "synthetic_data",
        ("title",),
        (
            "model", "version", "prompt_hash", "license", "checksum",
            "repository", "url", "doi", "description", "provenance",
            "environment", "date", "supersedes",
        ),
        "dataset",
        True,
        "Synthetic data is generated material; record the model, version and "
        "prompt hash, and do not treat it as observed evidence.",
    ),
    "model_output": _source_format(
        "model_output",
        ("title",),
        (
            "model", "version", "prompt_hash", "license", "checksum",
            "repository", "url", "description", "provenance", "environment",
            "date", "instrument", "supersedes",
        ),
        None,
        False,
        "Model output is generated material; record the model, version and "
        "prompt hash, and do not treat it as observed evidence.",
    ),
}

_ALIASES: dict[str, tuple[str, ...]] = {
    "author": ("authors",),
    "authors": ("author",),
    "editor": ("editors",),
    "editors": ("editor",),
    "journal": ("venue",),
    "booktitle": ("book_title",),
    "accessed": ("accessed_at",),
    "access_date": ("accessed_at",),
    "date_accessed": ("accessed_at",),
    "archive": ("archive_url",),
    "published": ("published_at",),
    "publication_date": ("published_at",),
    "date_published": ("published_at",),
    "retrieved": ("retrieved_at",),
    "date_retrieved": ("retrieved_at",),
    "licence": ("license",),
    "registration_id": ("registry_id",),
    "target": ("target_id",),
    "custody": ("chain_of_custody",),
    "coi": ("conflict_of_interest",),
    "interviewees": ("interviewee",),
    "lecturer": ("speaker",),
    "lecturers": ("speaker",),
    "creators": ("creator",),
}

_LIST_FIELDS = frozenset(
    {"authors", "editors", "citation_keys", "participants"}
)
_DATE_FIELDS = frozenset({"date", "published_at", "accessed_at", "retrieved_at"})
_URL_FIELDS = frozenset({"url", "archive_url", "transcript_url", "slides_url"})
_IDENTIFIER_FIELDS = {
    "doi": refs.normalize_doi,
    "isbn": refs.normalize_isbn,
    "arxiv": refs.normalize_arxiv,
    "pmid": refs.normalize_pmid,
    "pmcid": refs.normalize_pmcid,
}
_SOURCE_NODE_FIELDS = ("version", "retrieved_at", "license", "checksum", "status")
_YEAR_RE = re.compile(r"(?<!\d)((?:1[0-9]{3}|2[0-9]{3}))(?!\d)")
_HOLDING_FIELDS = frozenset(
    {
        "source_id",
        "source_type",
        "epistemic_role",
        "authority",
        "review_status",
        "use_constraints",
        "scope",
        "rationale",
        "reviewer",
        "status",
        "confidence",
    }
)
_DEFAULT_VENUE_FIELDS = (
    "publisher",
    "venue",
    "issuing_body",
    "institution",
    "repository",
    "site",
    "outlet",
    "event",
)
_VENUE_FIELDS: dict[str, tuple[str, ...]] = {
    "book": ("publisher",),
    "chapter": ("book_title", "publisher"),
    "website": ("site",),
    "article": ("venue", "publisher"),
    "dataset": ("repository", "publisher"),
    "software": ("repository", "publisher"),
    "standard": ("organization", "publisher"),
    "report": ("institution", "publisher"),
    "thesis": ("institution",),
    "preprint": ("venue", "repository"),
    "interview": ("outlet", "venue"),
    "lecture": ("institution", "venue"),
    "video": ("site",),
    "other": ("publisher", "venue"),
    "protocol": ("registry_id", "issuing_body", "repository"),
    "preregistration": ("registry_id", "issuing_body", "repository"),
    "registered_report": ("registry_id", "issuing_body", "venue"),
    "analysis_plan": ("repository", "issuing_body"),
    "computational_notebook": ("repository", "publisher"),
    "workflow": ("repository", "publisher"),
    "database": ("repository", "publisher"),
    "model": ("repository", "publisher"),
    "benchmark": ("repository", "publisher"),
    "ontology": ("repository", "publisher"),
    "regulation": ("issuing_body", "jurisdiction", "publisher"),
    "statute": ("issuing_body", "jurisdiction", "publisher"),
    "court_case": ("issuing_body", "jurisdiction", "publisher"),
    "guideline": ("issuing_body", "publisher"),
    "consensus_statement": ("issuing_body", "publisher"),
    "patent": ("issuing_body", "publisher"),
    "ethics_approval": ("institution",),
    "consent_form": ("institution",),
    "data_use_agreement": ("institution",),
    "funding": ("funder", "institution"),
    "conflict_of_interest": ("funder", "institution"),
    "peer_review": ("venue", "publisher"),
    "editorial_decision": ("venue", "publisher"),
    "author_response": ("venue", "publisher"),
    "correction": ("venue", "publisher"),
    "retraction_notice": ("venue", "publisher"),
    "conference_abstract": ("event", "venue", "conference"),
    "poster": ("event", "venue", "conference"),
    "slides": ("event", "venue", "conference"),
    "oral_history": ("repository", "publisher"),
    "focus_group": ("publisher", "venue"),
    "survey": ("repository", "publisher"),
    "questionnaire": ("repository", "publisher"),
    "news": ("site", "publisher"),
    "blog": ("site", "publisher"),
    "press_release": ("institution", "publisher"),
    "wiki": ("site", "publisher"),
    "forum": ("site", "publisher"),
    "social_post": ("platform", "site", "publisher"),
    "podcast": ("site", "publisher"),
    "webinar": ("event", "site", "publisher"),
    "archival_document": ("archive", "collection", "repository"),
    "letter": ("archive", "collection", "repository"),
    "reference_work": ("publisher",),
    "textbook": ("publisher",),
    "encyclopedia": ("publisher",),
    "fiction": ("publisher",),
    "film": ("studio", "publisher"),
    "art": ("collection", "publisher"),
    "specimen": ("repository", "publisher"),
    "sample": ("repository", "publisher"),
    "assay": ("repository", "publisher"),
    "instrument_output": ("repository", "publisher"),
    "image": ("repository", "publisher"),
    "spectrum": ("repository", "publisher"),
    "sequence": ("repository", "publisher"),
    "structure": ("repository", "publisher"),
    "synthetic_data": ("repository", "publisher"),
    "model_output": ("repository", "publisher"),
}


def _collapse(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def _source_type(value: Any) -> str:
    text = _collapse(value).casefold()
    if text not in SOURCE_FORMATS:
        expected = ", ".join(SOURCE_FORMATS)
        raise SourceFormatError(
            f"unknown source_type {value!r}; expected one of: {expected}"
        )
    return text


def get_source_format(source_type: Any) -> SourceFormat:
    """Return the profile for ``source_type`` or raise ``SourceFormatError``."""
    return SOURCE_FORMATS[_source_type(source_type)]


def list_source_formats() -> list[str]:
    """Return the known source type names in profile order."""
    return list(SOURCE_FORMATS)


def _canonical_key(raw: Any, allowed: set[str]) -> str:
    if not isinstance(raw, str):
        raise SourceFormatError("metadata field names must be strings")
    key = re.sub(r"[\s-]+", "_", raw.strip().casefold())
    if key in allowed:
        return key
    for candidate in _ALIASES.get(key, ()):
        if candidate in allowed:
            return candidate
    return key


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parts = re.split(r"\s*(?:;|\n|\s+and\s+)\s*", text)
    elif isinstance(value, Mapping) or not isinstance(value, Iterable):
        raise SourceFormatError(f"{field} must be a string or a list of strings")
    else:
        parts = []
        for item in value:
            if isinstance(item, Mapping) or isinstance(item, (list, tuple, set)):
                raise SourceFormatError(f"{field} entries must be strings")
            parts.extend(re.split(r"\s*(?:;|\n)\s*", _collapse(item)))
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = _collapse(part)
        if not text:
            continue
        folded = text.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(text)
    return result


def _scalar(value: Any, field: str) -> str:
    if isinstance(value, Mapping) or isinstance(value, (list, tuple, set)):
        raise SourceFormatError(f"{field} must be a scalar value")
    return _collapse(value)


def _year(value: Any, field: str = "year") -> str:
    if isinstance(value, bool):
        raise SourceFormatError(f"{field} must be a four-digit year")
    match = _YEAR_RE.search(_collapse(value))
    if not match:
        raise SourceFormatError(f"{field} must be a four-digit year")
    return match.group(1)


def _date(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        moment = value.astimezone(timezone.utc) if value.tzinfo else value
        if moment.time() == time(0, 0):
            return moment.date().isoformat()
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    text = _collapse(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{4}", text):
        return _year(text, field)
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year, month = (int(part) for part in text.split("-"))
        try:
            return date(year, month, 1).strftime("%Y-%m")
        except ValueError as exc:
            raise SourceFormatError(f"{field} is not a valid date") from exc
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise SourceFormatError(f"{field} is not a valid date") from exc
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        moment = None
    if moment is not None:
        if moment.tzinfo:
            moment = moment.astimezone(timezone.utc)
        if moment.time() == time(0, 0):
            return moment.date().isoformat()
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    for pattern in (
        "%Y/%m/%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
        "%B %d %Y", "%b %d %Y", "%B %Y", "%b %Y",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern in {"%B %Y", "%b %Y"}:
            return parsed.strftime("%Y-%m")
        return parsed.date().isoformat()
    raise SourceFormatError(
        f"{field} must be an ISO date (YYYY, YYYY-MM, YYYY-MM-DD or an ISO datetime)"
    )


def _url(value: Any, field: str) -> str:
    text = _collapse(value)
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif text.casefold().startswith("www."):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise SourceFormatError(f"{field} must be an absolute http(s) URL") from exc
    if not parts.scheme:
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?:[/:?#]|$)", text):
            text = "https://" + text
            try:
                parts = urlsplit(text)
            except ValueError as exc:
                raise SourceFormatError(f"{field} must be an absolute http(s) URL") from exc
        else:
            raise SourceFormatError(f"{field} must be an absolute http(s) URL")
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise SourceFormatError(f"{field} must be an absolute http(s) URL")
    try:
        host = parts.hostname
    except ValueError as exc:
        raise SourceFormatError(f"{field} must include a host") from exc
    if not host:
        raise SourceFormatError(f"{field} must include a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceFormatError(f"{field} has an invalid port") from exc
    host = host.casefold()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def _pages(value: Any) -> str:
    text = _collapse(value)
    return re.sub(r"(?<=\d)\s*[\u2013\u2014]\s*(?=\d)", "-", text)


def _normalise_field(field: str, value: Any) -> Any:
    if field in _LIST_FIELDS:
        return _string_list(value, field)
    if field == "year":
        return _year(value)
    if field in _DATE_FIELDS:
        return _date(value, field)
    if field in _URL_FIELDS:
        return _url(value, field)
    if field in _IDENTIFIER_FIELDS or field == "target_doi":
        normalizer = refs.normalize_doi if field == "target_doi" else _IDENTIFIER_FIELDS[field]
        normalized = normalizer(value)
        if not normalized:
            label = "DOI" if field == "target_doi" else field.upper()
            raise SourceFormatError(f"{field} is not a valid {label}")
        return normalized
    if field == "pages":
        return _pages(value)
    return _scalar(value, field)


def normalise_source_metadata(source_type: Any, metadata: Any) -> dict[str, Any]:
    """Validate and normalise declared metadata for one source format.

    Unknown fields are rejected so a typo cannot silently disappear. Required
    fields must be present and non-empty after normalisation. Optional empty
    strings and lists are omitted from the result. The function performs no
    file or network access.
    """
    profile = get_source_format(source_type)
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise SourceFormatError("metadata must be an object")
    allowed = set(profile.metadata_fields)
    if profile.bibliographic:
        allowed.update(_CITATION_FIELDS)
    result: dict[str, Any] = {}
    for raw_key, raw_value in metadata.items():
        key = _canonical_key(raw_key, allowed)
        if key not in allowed:
            raise SourceFormatError(
                f"unknown metadata field for {profile.source_type}: {raw_key!r}"
            )
        value = _normalise_field(key, raw_value)
        if value is None or value == "" or value == []:
            continue
        if key in result:
            if key in _LIST_FIELDS:
                combined = list(result[key]) + list(value)
                result[key] = _string_list(combined, key)
                continue
            if result[key] != value:
                raise SourceFormatError(
                    f"conflicting values for metadata field {key!r}"
                )
            continue
        result[key] = value
    missing = [field for field in profile.required_metadata if not result.get(field)]
    if missing:
        raise SourceFormatError(
            f"{profile.source_type} requires metadata: {', '.join(missing)}"
        )
    for group in profile.required_any:
        if not any(result.get(field) for field in group):
            raise SourceFormatError(
                f"{profile.source_type} requires {' or '.join(group)}"
            )
    return result


# --------------------------------------------------------------------------- #
# graph registration
# --------------------------------------------------------------------------- #
def _optional_text(value: Any) -> str | None:
    text = _collapse(value)
    return text or None


def _source_node_fields(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return the metadata fields mirrored on the Source node."""
    return {
        field: metadata[field]
        for field in _SOURCE_NODE_FIELDS
        if metadata.get(field) not in (None, "", [])
    }


def _find_source(
    state: Mapping[str, Any],
    *,
    url: str | None,
    root_id: str | None,
    path: str | None,
    sha256: str | None,
) -> dict[str, Any] | None:
    for node in state.get("nodes", []):
        if not isinstance(node, Mapping) or node.get("type") != "Source":
            continue
        locator = node.get("locator")
        if not isinstance(locator, Mapping):
            continue
        if url and _collapse(locator.get("url")) == url:
            return dict(node)
        locator_sha = _collapse(locator.get("sha256"))
        if sha256 and locator_sha and locator_sha.casefold() == sha256.casefold():
            locator_path = _collapse(locator.get("path"))
            if path is None or locator_path in {"", path}:
                return dict(node)
        if path and _collapse(locator.get("path")) == path:
            if root_id and _collapse(locator.get("root_id")) not in {"", root_id}:
                continue
            if sha256 and locator_sha and locator_sha.casefold() != sha256.casefold():
                continue
            return dict(node)
    return None


def _merge_source(
    graph: Any,
    existing: Mapping[str, Any],
    *,
    source_type: str,
    metadata: Mapping[str, Any],
    locator: Mapping[str, Any],
    name: str,
    title: str,
    source_fields: Mapping[str, Any],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    existing_metadata = existing.get("metadata")
    merged_metadata = dict(existing_metadata) if isinstance(existing_metadata, Mapping) else {}
    changed_metadata = {
        key: value for key, value in metadata.items()
        if merged_metadata.get(key) not in (None, "", []) and merged_metadata.get(key) != value
    }
    if changed_metadata:
        warnings.append({
            "code": "source_metadata_updated",
            "reason": "Re-registration replaced metadata fields: "
                      + ", ".join(sorted(changed_metadata)),
        })
    merged_metadata.update(metadata)
    if merged_metadata != (existing_metadata or {}):
        changes["metadata"] = merged_metadata

    existing_locator = existing.get("locator")
    merged_locator = dict(existing_locator) if isinstance(existing_locator, Mapping) else {}
    merged_locator.update(locator)
    if merged_locator.get("metadata") != merged_metadata:
        merged_locator["metadata"] = merged_metadata
    if isinstance(existing_locator, Mapping) and existing_locator.get("kind"):
        merged_locator["kind"] = existing_locator["kind"]
    if merged_locator != (existing_locator or {}):
        changes["locator"] = merged_locator

    existing_type = _collapse(existing.get("source_type")).casefold()
    if existing_type and existing_type != source_type:
        warnings.append({
            "code": "source_type_mismatch",
            "reason": f"Same locator already recorded as {existing_type!r}; "
                      f"kept it instead of {source_type!r}.",
        })
    elif not existing_type:
        changes["source_type"] = source_type
    if title and not existing.get("title"):
        changes["title"] = title
    if name and not existing.get("name"):
        changes["name"] = name
    for field, value in source_fields.items():
        if existing.get(field) != value:
            changes[field] = value
    if "truth_assessed" not in existing:
        changes["truth_assessed"] = False
    if not changes:
        return dict(existing)
    return graph.amend(existing["id"], **changes)


def _venue(profile: SourceFormat, metadata: Mapping[str, Any]) -> str:
    for field in _VENUE_FIELDS.get(profile.source_type, _DEFAULT_VENUE_FIELDS):
        value = _collapse(metadata.get(field))
        if value:
            return value
    return ""


def _reference_payload(
    profile: SourceFormat,
    metadata: Mapping[str, Any],
    explicit: Any,
) -> dict[str, Any] | None:
    if explicit is not None and not isinstance(explicit, Mapping):
        raise SourceFormatError("reference must be an object")
    if explicit is None and not profile.bibliographic:
        return None
    payload: dict[str, Any] = dict(explicit or {})
    title = _collapse(metadata.get("title")) or _collapse(metadata.get("context"))
    if title and not _collapse(payload.get("title")):
        payload["title"] = title
    if not payload.get("authors"):
        authors = metadata.get("authors")
        if isinstance(authors, str):
            authors = [authors]
        if authors:
            payload["authors"] = list(authors)
        elif _collapse(metadata.get("author")):
            payload["authors"] = [_collapse(metadata["author"])]
        elif _collapse(metadata.get("creator")):
            payload["authors"] = [_collapse(metadata["creator"])]
        elif _collapse(metadata.get("interviewee")):
            payload["authors"] = [_collapse(metadata["interviewee"])]
        elif _collapse(metadata.get("speaker")):
            payload["authors"] = [_collapse(metadata["speaker"])]
        elif _collapse(metadata.get("director")):
            payload["authors"] = [_collapse(metadata["director"])]
    if metadata.get("year") and not payload.get("year"):
        payload["year"] = metadata["year"]
    elif not payload.get("year"):
        for field in ("published_at", "date"):
            match = _YEAR_RE.search(_collapse(metadata.get(field)))
            if match:
                payload["year"] = match.group(1)
                break
    venue = _venue(profile, metadata)
    if venue and not _collapse(payload.get("venue")):
        payload["venue"] = venue
    if metadata.get("url") and not _collapse(payload.get("url")):
        payload["url"] = metadata["url"]
    if metadata.get("license") and not _collapse(payload.get("license")):
        payload["license"] = metadata["license"]
    if metadata.get("abstract") and not _collapse(payload.get("abstract")):
        payload["abstract"] = metadata["abstract"]

    identifiers: dict[str, Any] = {
        key: metadata[key]
        for key in _IDENTIFIER_FIELDS
        if metadata.get(key)
    }
    explicit_identifiers = payload.get("identifiers")
    if isinstance(explicit_identifiers, Mapping):
        for key, value in explicit_identifiers.items():
            if value:
                identifiers[key] = value
    if identifiers:
        payload["identifiers"] = identifiers

    if metadata.get("citation_key") and not payload.get("citation_key"):
        payload["citation_key"] = metadata["citation_key"]
    keys = _string_list(payload.get("citation_keys"), "citation_keys")
    if metadata.get("citation_key"):
        keys.insert(0, metadata["citation_key"])
    keys.extend(metadata.get("citation_keys") or [])
    if keys:
        payload["citation_keys"] = list(dict.fromkeys(_collapse(key) for key in keys if _collapse(key)))

    payload["reference_type"] = profile.reference_type or payload.get("reference_type") or "misc"
    if not _collapse(payload.get("metadata_status")):
        payload["metadata_status"] = "source_metadata"
    if (
        not _collapse(payload.get("title"))
        and not _collapse(payload.get("raw"))
        and not _collapse(payload.get("citation_key"))
        and not payload.get("citation_keys")
    ):
        if metadata.get("url"):
            payload["raw"] = metadata["url"]
        else:
            return None
    return payload


def _used_citation_keys(graph: Any) -> set[str]:
    used: set[str] = set()
    for node in graph.state().get("nodes", []):
        if node.get("type") != "Reference":
            continue
        if node.get("citation_key"):
            used.add(_collapse(node["citation_key"]).casefold())
        for key in node.get("citation_keys") or []:
            used.add(_collapse(key).casefold())
    return used


def _unique_citation_key(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = base + suffix
        if candidate not in used:
            return candidate
    number = 2
    while f"{base}-{number}" in used:
        number += 1
    return f"{base}-{number}"


def _matching_reference(graph: Any, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Find an existing Reference by identifier, URL, title/year or raw text.

    ``references.find_reference`` prefers a node's citation key when choosing
    its identity, so a keyless incoming entry cannot match a previously keyed
    title. Registration checks the bibliographic work directly instead.
    """
    incoming_ids = refs.normalize_identifiers(payload.get("identifiers"))
    title = refs.normalize_title(payload.get("title"))
    year = _collapse(payload.get("year"))
    url = _collapse(payload.get("url"))
    raw = _collapse(payload.get("raw"))
    for node in graph.state().get("nodes", []):
        if node.get("type") != "Reference":
            continue
        node_ids = refs.normalize_identifiers(node.get("identifiers"))
        if incoming_ids:
            if any(node_ids.get(key) == value for key, value in incoming_ids.items()):
                return dict(node)
            if any(
                node_ids.get(key) and node_ids[key] != value
                for key, value in incoming_ids.items()
            ):
                continue
        node_url = _collapse(node.get("url"))
        if url and node_url:
            if url == node_url:
                return dict(node)
            continue
        node_title = refs.normalize_title(node.get("title"))
        node_year = _collapse(node.get("year"))
        if title and node_title == title and (not year or not node_year or year == node_year):
            return dict(node)
        if raw and _collapse(node.get("raw")) == raw:
            return dict(node)
    return None


def _ensure_reference_key(graph: Any, payload: dict[str, Any]) -> None:
    """Give an otherwise keyless Reference a stable key before recording it.

    ``record_reference`` generates a fresh key for an incoming entry when none
    is supplied, which would prevent a repeated source registration from
    finding the Reference it recorded last time. Reuse the key of an existing
    same-work Reference, otherwise generate one against the keys already taken.
    """
    if payload.get("citation_key") or payload.get("citation_keys"):
        return
    existing = _matching_reference(graph, payload)
    if existing is not None and _collapse(existing.get("citation_key")):
        key = _collapse(existing["citation_key"])
        keys = _string_list(existing.get("citation_keys"), "citation_keys") or [key]
        payload["citation_key"] = key
        payload["citation_keys"] = keys
        return
    used = _used_citation_keys(graph)
    if _collapse(payload.get("title")):
        key = refs.generate_citation_key(payload, used)
    else:
        raw = _collapse(payload.get("raw")).casefold()
        base = "src" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        key = _unique_citation_key(base, used)
    payload["citation_key"] = key
    payload["citation_keys"] = [key]


def _link_once(graph: Any, source_id: str, relation: str, target_id: str) -> None:
    edges = graph.state().get("edges", [])
    if any(
        edge.get("type") == relation
        and edge.get("src") == source_id
        and edge.get("tgt") == target_id
        for edge in edges
    ):
        return
    graph.link(source_id, relation, target_id)


def _holding_node(
    graph: Any,
    outcome: Any,
    warnings: list[dict[str, str]],
) -> dict[str, Any] | None:
    node: Any = outcome
    if isinstance(outcome, Mapping):
        extra = outcome.get("warnings")
        if isinstance(extra, list):
            warnings.extend(
                warning for warning in extra
                if isinstance(warning, Mapping) and warning.get("code")
            )
        if isinstance(outcome.get("holding"), Mapping):
            node = outcome["holding"]
        elif outcome.get("holding") is None and "holding_id" in outcome:
            node = outcome["holding_id"]
        elif "id" not in outcome and "type" not in outcome:
            node = None
    if isinstance(node, str):
        existing = next(
            (
                item for item in graph.state().get("nodes", [])
                if item.get("id") == node
            ),
            None,
        )
        node = existing
    if isinstance(node, Mapping):
        return dict(node)
    if outcome is not None:
        warnings.append({
            "code": "holding_unrecognised",
            "reason": "The holding recorder returned no usable Holding record.",
        })
    return None


def _record_holding(
    graph: Any,
    source_id: str,
    source_type: str,
    *,
    holding: Any,
    reviewer: str | None,
    holding_recorder: Any,
    warnings: list[dict[str, str]],
) -> dict[str, Any] | None:
    if holding is not None and not isinstance(holding, Mapping):
        raise SourceFormatError("holding must be an object of Holding overrides")
    overrides = dict(holding or {})
    unknown = sorted(set(overrides) - _HOLDING_FIELDS)
    if unknown:
        raise SourceFormatError(f"unknown holding field(s): {', '.join(unknown)}")
    if overrides.get("source_id") not in {None, source_id}:
        raise SourceFormatError("holding.source_id must match the registered source")
    if overrides.get("source_type") not in {None, source_type}:
        raise SourceFormatError("holding.source_type must match the source format")
    overrides.pop("source_id", None)
    overrides.pop("source_type", None)
    reviewer_text = _optional_text(reviewer)
    if reviewer_text:
        overrides.setdefault("reviewer", reviewer_text)

    recorder = holding_recorder
    if recorder is None:
        try:
            holdings_module = importlib.import_module("ozagia.research.holdings")
        except ImportError as exc:
            warnings.append({
                "code": "holding_unavailable",
                "reason": f"Holding recording is unavailable: {_collapse(exc) or 'module not found'}",
            })
            return None
        recorder = getattr(holdings_module, "record_holding", None)
        if not callable(recorder):
            warnings.append({
                "code": "holding_unavailable",
                "reason": "holdings.record_holding is not available",
            })
            return None
    if not callable(recorder):
        raise SourceFormatError("holding_recorder must be callable")

    outcome = recorder(
        graph,
        source_id=source_id,
        source_type=source_type,
        **overrides,
    )
    node = _holding_node(graph, outcome, warnings)
    if node is None:
        if outcome is None:
            warnings.append({
                "code": "holding_not_recorded",
                "reason": "The holding recorder returned no Holding.",
            })
        return None
    holding_id = node.get("id")
    if isinstance(holding_id, str):
        current = next(
            (
                item for item in graph.state().get("nodes", [])
                if item.get("id") == holding_id and item.get("type") == "Holding"
            ),
            None,
        )
        if current is not None:
            _link_once(graph, source_id, "held_as", holding_id)
    return node


def register_source(
    graph: Any,
    *,
    source_type: str,
    metadata: Mapping[str, Any],
    root_id: str | None = None,
    path: str | None = None,
    url: str | None = None,
    sha256: str | None = None,
    reference: Mapping[str, Any] | None = None,
    holding: Mapping[str, Any] | None = None,
    reviewer: str | None = None,
    holding_recorder: Any = None,
) -> dict[str, Any]:
    """Record one Source, its bibliographic Reference and a default Holding.

    ``metadata`` is validated against the format profile. Version, retrieval
    date, licence, checksum and status are mirrored on the Source node as well
    as stored in its metadata. An existing Source with the same path/hash
    locator or URL is reused and enriched rather than duplicated. A
    non-bibliographic note may omit its Reference. When ``supersedes`` names an
    existing node, the Source is linked to it; a missing target is rejected.
    The function never reads a file or fetches a URL; ``root_id``, ``path``,
    ``url`` and ``sha256`` are locator data only.
    """
    profile = get_source_format(source_type)
    source_type = profile.source_type
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise SourceFormatError("metadata must be an object")
    prepared = dict(metadata)
    if (
        url is not None
        and "url" in profile.metadata_fields
        and not _collapse(prepared.get("url"))
    ):
        prepared["url"] = url
    normalised = normalise_source_metadata(source_type, prepared)

    supersedes_id = _optional_text(normalised.get("supersedes"))
    if supersedes_id is not None:
        target = next(
            (
                node for node in graph.state().get("nodes", [])
                if isinstance(node, Mapping) and node.get("id") == supersedes_id
            ),
            None,
        )
        if target is None:
            raise SourceFormatError(
                f"supersedes target does not exist: {supersedes_id}"
            )
    source_fields = _source_node_fields(normalised)

    resolved_url = _optional_text(url)
    if resolved_url is not None:
        resolved_url = _url(resolved_url, "url")
    metadata_url = normalised.get("url")
    if resolved_url and metadata_url and resolved_url != metadata_url:
        split_url = resolved_url
        resolved_url = metadata_url
        url_warning = {
            "code": "source_url_mismatch",
            "reason": f"Locator URL {split_url!r} differs from metadata URL "
                      f"{metadata_url!r}; kept the metadata URL.",
        }
    else:
        resolved_url = resolved_url or metadata_url
        url_warning = None

    root_text = _optional_text(root_id)
    path_text = _optional_text(path)
    sha_text = _optional_text(sha256)

    locator: dict[str, Any] = {"kind": "source", "source_type": source_type}
    if root_text:
        locator["root_id"] = root_text
    if path_text:
        locator["path"] = path_text
    if resolved_url:
        locator["url"] = resolved_url
    if sha_text:
        locator["sha256"] = sha_text.casefold()
    locator["metadata"] = dict(normalised)

    title = _collapse(normalised.get("title")) or _collapse(normalised.get("context"))
    name = (
        title
        or _collapse(normalised.get("identifier"))
        or (Path(path_text).name if path_text else "")
        or resolved_url
        or f"{source_type} source"
    )

    warnings: list[dict[str, str]] = []
    if url_warning:
        warnings.append(url_warning)
    payload = _reference_payload(profile, normalised, reference)
    if payload is not None and resolved_url and not _collapse(payload.get("url")):
        payload["url"] = resolved_url
    if payload is None and profile.bibliographic:
        warnings.append({
            "code": "reference_metadata_insufficient",
            "reason": "Metadata cannot identify a bibliographic Reference; "
                      "record a title, citation key, URL or an explicit reference.",
        })

    reference_node = None
    if payload is not None:
        _ensure_reference_key(graph, payload)
        try:
            recorded = refs.record_reference(
                graph,
                payload,
                provenance={
                    "kind": "source_registration",
                    "source_type": source_type,
                },
                source_locator={
                    key: value for key, value in locator.items() if key != "metadata"
                },
            )
        except ResearchError as exc:
            raise SourceFormatError(f"invalid bibliographic reference: {exc}") from exc
        reference_node = recorded["reference"]

    state = graph.state()
    existing = _find_source(
        state,
        url=resolved_url,
        root_id=root_text,
        path=path_text,
        sha256=sha_text,
    )
    if existing is None:
        source = graph.record(
            "Source",
            name=name,
            title=title,
            source_type=source_type,
            locator=locator,
            metadata=dict(normalised),
            paper_import=False,
            truth_assessed=False,
            **source_fields,
        )
    else:
        source = _merge_source(
            graph,
            existing,
            source_type=source_type,
            metadata=normalised,
            locator=locator,
            name=name,
            title=title,
            source_fields=source_fields,
            warnings=warnings,
        )

    if reference_node is not None:
        _link_once(graph, source["id"], "manifestation_of", reference_node["id"])
    if supersedes_id is not None:
        if source["id"] == supersedes_id:
            raise SourceFormatError("a source cannot supersede itself")
        _link_once(graph, source["id"], "supersedes", supersedes_id)

    held = _record_holding(
        graph,
        source["id"],
        source_type,
        holding=holding,
        reviewer=reviewer,
        holding_recorder=holding_recorder,
        warnings=warnings,
    )
    return {
        "source": source,
        "reference": reference_node,
        "holding": held,
        "metadata": source.get("metadata", normalised),
        "warnings": warnings,
        "truth_assessed": False,
    }
