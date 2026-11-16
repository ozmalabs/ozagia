"""Review findings as a ledger that closes across sessions.

A review run (``ReviewRun``/``ReviewFinding``) records what one reviewer said
once. A user who asks for a re-review wants the other view: which issues in
this document are still outstanding. A ``LedgerFinding`` is that tracked
issue:

- ``document`` (a workspace-relative path or other name) and an optional
  ``anchor`` (a Markdown/TeX section slug, a ``doc:<rel>#<slug>`` key, or a
  path alias such as ``methods/sampling``) plus an optional ``quote``;
- ``severity`` (``blocking``, ``major``, ``minor``, ``note``), ``text``;
- ``status``: ``open``, ``addressed``, ``wont_fix`` or ``disputed``;
- ``history``: every status change with its note, time and conversation
  session id.

Status changes are graph amendments, so the event log keeps each step. When
a document version is available at recording time, the ledger stores the
anchored section's hash; ``rereview`` compares a new version against it and
proposes which open findings may now be addressed. It never changes a
status itself.
"""
from __future__ import annotations

import hashlib
import posixpath
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from ozagia.core.sections import (
    SectionError,
    doc_sections,
    has_sections,
    parse_section_key,
    resolve_section,
    section_key,
    section_offsets,
)
from ozagia.research.graph import LEDGER_SEVERITIES as SEVERITIES
from ozagia.research.graph import LEDGER_STATUSES, ResearchError

__all__ = [
    "LEDGER_STATUSES",
    "LedgerError",
    "add_findings",
    "document_key",
    "normalise_document",
    "outstanding",
    "rereview",
    "set_status",
]

_STATUS_ALIASES = {"wont-fix": "wont_fix", "wontfix": "wont_fix", "won't fix": "wont_fix"}
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
MAX_FINDINGS = 500
# Targets of the graph's ``about`` edge.
_TARGET_TYPES = frozenset({"Claim", "Evidence", "Reference", "Argument", "Source"})
ReadDocument = Callable[[str], "str | None"]


class LedgerError(ResearchError):
    """A ledger finding, status change or re-review request is invalid."""


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _node(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node["id"] == node_id), None)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{field} must be a non-empty string")
    return value.strip()


def _optional(value: Any, field: str) -> str | None:
    return None if value is None else _text(value, field)


def document_key(value: Any) -> str:
    """The comparable form of a document path: ``./a//b.md`` is ``a/b.md``."""
    text = str(value or "").strip()
    if not text:
        return ""
    return posixpath.normpath(text)


def normalise_document(value: Any, field: str = "document") -> str:
    """A non-empty document path in normal form, for storing and lookups."""
    key = document_key(_text(value, field))
    if key in ("", "."):
        raise LedgerError(f"{field} must name a document")
    return key


def _optional_document(value: Any, field: str = "document") -> str | None:
    return None if value is None else normalise_document(value, field)


def _status(value: Any) -> str:
    text = _text(value, "status").casefold()
    text = _STATUS_ALIASES.get(text, text)
    if text not in LEDGER_STATUSES:
        raise LedgerError(f"status must be one of {list(LEDGER_STATUSES)}")
    return text


def _severity(value: Any) -> str:
    text = _text(value, "severity").casefold()
    if text not in _SEVERITY_RANK:
        raise LedgerError(f"severity must be one of {list(SEVERITIES)}")
    return text


def _ledger(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [node for node in _nodes(state) if node.get("type") == "LedgerFinding"]


# --------------------------------------------------------------------------- #
# anchors
# --------------------------------------------------------------------------- #
def _split_anchor(document: str | None, anchor: str | None) -> tuple[str | None, str | None]:
    """(document, fragment) from a slug, a path alias or a ``doc:`` key."""
    if anchor is None:
        return document, None
    parsed = parse_section_key(anchor)
    if parsed is not None:
        rel, fragment = parsed
        rel = normalise_document(rel)
        if document is not None and document != rel:
            raise LedgerError(f"anchor {anchor!r} names {rel}, not {document}")
        return rel, fragment
    return document, anchor.lstrip("#").strip() or None


def _anchored_text(document: str, fragment: str | None, text: str) -> dict[str, Any]:
    """The anchored span of ``text``: a section, or the whole document."""
    if fragment is None:
        return {"found": True, "text": text, "slug": None}
    if not has_sections(document):
        return {"found": False, "text": None, "slug": None,
                "reason": "document type has no sections"}
    try:
        section = resolve_section(doc_sections(document, text), fragment)
    except SectionError as exc:
        return {"found": False, "text": None, "slug": None, "reason": str(exc)}
    start, end = section_offsets(text, section)
    return {"found": True, "text": text[start:end], "slug": section.slug,
            "lines": [section.start, section.end]}


def _baseline(document: str, fragment: str | None, quote: str | None,
              text: str) -> dict[str, Any]:
    span = _anchored_text(document, fragment, text)
    base: dict[str, Any] = {"document_sha256": _sha(text), "anchor_found": span["found"]}
    if span["found"]:
        base["anchor_sha256"] = _sha(span["text"])
        if span.get("lines"):
            base["anchor_lines"] = span["lines"]
        if quote is not None:
            base["quote_present"] = _collapse(quote) in _collapse(span["text"])
    return base


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def _history_entry(status: str, previous: str | None, note: str | None,
                   session: str) -> dict[str, Any]:
    entry = {"status": status, "from": previous, "at": _utc(),
             "conversation_session_id": session}
    if note is not None:
        entry["note"] = note
    return entry


def _prepare(
    state: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    default_document: str | None,
    session: str,
    read_document: ReadDocument | None,
    texts: dict[str, str | None],
    origin: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate one finding into node fields and ``about`` targets."""
    document = _optional_document(raw.get("document")) or default_document
    anchor = _optional(raw.get("anchor") or raw.get("key"), "anchor")
    document, fragment = _split_anchor(document, anchor)
    if document is None:
        raise LedgerError("a ledger finding needs a document (or a doc: anchor key)")
    severity = _severity(raw.get("severity"))
    text = _text(raw.get("text") or raw.get("wording"), "text")
    quote = _optional(raw.get("quote"), "quote")
    targets = list(dict.fromkeys(
        str(raw[name]) for name in ("target_id", "claim_id", "reference_id",
                                    "argument_id") if raw.get(name)))
    for target in targets:
        target_node = _node(state, target)
        if target_node is None:
            raise LedgerError(f"unknown target record: {target}")
        if target_node.get("type") not in _TARGET_TYPES:
            raise LedgerError(
                f"a finding cannot be about a {target_node.get('type')} ({target})")
    fields: dict[str, Any] = {
        "document": document,
        "severity": severity,
        "text": text,
        "status": "open",
        "history": [_history_entry("open", None, "recorded", session)],
        "recorded_in": session,
        "truth_assessed": False,
    }
    if fragment is not None:
        fields["anchor"] = fragment
        if has_sections(document):
            fields["key"] = section_key(document, fragment)
    if quote is not None:
        fields["quote"] = quote
    for name in ("issue_type", "recommendation"):
        value = _optional(raw.get(name), name)
        if value is not None:
            fields[name] = value
    if origin:
        fields.update(origin)
    if document not in texts:
        texts[document] = read_document(document) if read_document else None
    current = texts[document]
    if current is not None:
        base = _baseline(document, fragment, quote, current)
        fields["baseline"] = base
        if base["anchor_found"] and fragment is not None and has_sections(document):
            span = _anchored_text(document, fragment, current)
            fields["anchor"] = span["slug"]
            fields["key"] = section_key(document, span["slug"])
    return fields, targets


def _commit(graph: Any, fields: dict[str, Any], targets: list[str]) -> dict[str, Any]:
    node = graph.record("LedgerFinding", **fields)
    review_finding = fields.get("review_finding_id")
    if review_finding:
        graph.link(node["id"], "derived_from", review_finding,
                   provenance={"kind": "review_ledger_import"},
                   rationale="The ledger entry tracks this recorded review finding.")
    for target in targets:
        graph.link(node["id"], "about", target,
                   provenance={"kind": "review_ledger_target"},
                   rationale="The finding refers to this record; it is not support.")
    return node


def _from_run(state: Mapping[str, Any], run_id: str) -> list[tuple[dict, dict]]:
    run = _node(state, run_id)
    if run is None or run.get("type") != "ReviewRun":
        raise LedgerError(f"unknown review run: {run_id}")
    imported = {node.get("review_finding_id") for node in _ledger(state)}
    out = []
    for node in _nodes(state):
        if (node.get("type") != "ReviewFinding" or node.get("run_id") != run_id
                or node["id"] in imported):
            continue
        raw: dict[str, Any] = {key: node[key] for key in (
            "wording", "severity", "quote", "issue_type", "recommendation",
            "claim_id", "reference_id", "argument_id") if node.get(key)}
        claim = _node(state, str(node.get("claim_id") or ""))
        locator = (claim or {}).get("source_locator") or (claim or {}).get("locator")
        if isinstance(locator, Mapping) and isinstance(locator.get("path"), str):
            raw["document"] = locator["path"]
        out.append((raw, {"review_finding_id": node["id"], "review_run_id": run_id,
                          "review_plan_id": node.get("plan_id")}))
    return out


def add_findings(
    graph: Any,
    *,
    conversation_session_id: str,
    findings: Sequence[Mapping[str, Any]] | None = None,
    run_id: str | None = None,
    document: str | None = None,
    document_text: str | None = None,
    read_document: ReadDocument | None = None,
) -> dict[str, Any]:
    """Add findings to the ledger, given explicitly or from a recorded run.

    ``run_id`` imports that run's ``ReviewFinding`` nodes once each (a second
    import skips those already in the ledger). ``document`` is the default
    document; an imported finding about a claim uses the claim's source path
    when no default is given. ``document_text`` (or ``read_document``) gives
    the current version, used as the baseline for ``rereview``.
    """
    session = _text(conversation_session_id, "conversation_session_id")
    document = _optional_document(document)
    if findings is None and run_id is None:
        raise LedgerError("give findings, a run_id, or both")
    items: list[tuple[Mapping[str, Any], dict | None]] = []
    if findings is not None:
        if isinstance(findings, (str, bytes)) or not isinstance(findings, Sequence):
            raise LedgerError("findings must be a list of objects")
        for index, raw in enumerate(findings):
            if not isinstance(raw, Mapping):
                raise LedgerError(f"findings[{index}] must be an object")
            items.append((raw, None))
    skipped = 0
    if run_id is not None:
        state = graph.state()
        pending = _from_run(state, _text(run_id, "run_id"))
        total = sum(1 for node in _nodes(state) if node.get("type") == "ReviewFinding"
                    and node.get("run_id") == run_id)
        skipped = total - len(pending)
        items.extend(pending)
    if len(items) > MAX_FINDINGS:
        raise LedgerError(f"at most {MAX_FINDINGS} findings per call")
    texts: dict[str, str | None] = {}
    if document is not None and document_text is not None:
        texts[document] = document_text
    # Validate every finding before recording any, so a bad item leaves the
    # ledger unchanged.
    state = graph.state()
    prepared = [
        _prepare(state, raw, default_document=document, session=session,
                 read_document=read_document, texts=texts, origin=origin)
        for raw, origin in items
    ]
    recorded = [_commit(graph, fields, targets) for fields, targets in prepared]
    return {"findings": recorded, "count": len(recorded),
            "skipped_already_in_ledger": skipped, "truth_assessed": False}


def set_status(
    graph: Any,
    *,
    finding_id: str,
    status: str,
    note: str,
    conversation_session_id: str,
) -> dict[str, Any]:
    """Change a ledger finding's status and append the change to its history."""
    state = graph.state()
    node_id = _text(finding_id, "finding_id")
    node = _node(state, node_id)
    if node is None or node.get("type") != "LedgerFinding":
        raise LedgerError(f"unknown ledger finding: {node_id}")
    new = _status(status)
    note = _text(note, "note")
    session = _text(conversation_session_id, "conversation_session_id")
    previous = str(node.get("status"))
    if new == previous:
        raise LedgerError(f"{node_id} is already {new}")
    history = [*list(node.get("history") or []),
               _history_entry(new, previous, note, session)]
    updated = graph.amend(node_id, status=new, history=history)
    return {"finding": updated, "previous_status": previous,
            "truth_assessed": False}


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def _sort_key(node: Mapping[str, Any]) -> tuple[int, str]:
    return (_SEVERITY_RANK.get(str(node.get("severity")), len(_SEVERITY_RANK)),
            str(node["id"]))


def _statuses(value: Any) -> list[str]:
    if value is None:
        return ["open"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence):
        raise LedgerError("statuses must be a list")
    return list(dict.fromkeys(_status(item) for item in value))


def _summary(node: Mapping[str, Any]) -> dict[str, Any]:
    history = list(node.get("history") or [])
    out = {key: node[key] for key in (
        "id", "document", "anchor", "key", "quote", "severity", "text", "status",
        "issue_type", "recommendation", "review_finding_id", "review_run_id")
        if node.get(key) is not None}
    out["recorded_at"] = node.get("created_at")
    out["last_change"] = history[-1] if history else None
    out["history"] = history
    return out


def outstanding(
    state: Mapping[str, Any],
    *,
    document: str | None = None,
    statuses: Any = None,
) -> dict[str, Any]:
    """Ledger findings in ``statuses`` (default: open), grouped by document."""
    wanted = _statuses(statuses)
    document = _optional_document(document)
    ledger = _ledger(state)
    counts = {name: 0 for name in LEDGER_STATUSES}
    for node in ledger:
        counts[str(node.get("status"))] = counts.get(str(node.get("status")), 0) + 1
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for node in ledger:
        if node.get("status") in wanted and (
                document is None or document_key(node.get("document")) == document):
            groups.setdefault(document_key(node.get("document")), []).append(node)
    documents = []
    for name in sorted(groups):
        items = sorted(groups[name], key=_sort_key)
        # A malformed node (an unknown severity from a hand-made record) is
        # counted under its own value rather than breaking the report.
        by_severity = {severity: 0 for severity in SEVERITIES}
        for item in items:
            severity = str(item.get("severity"))
            by_severity[severity] = by_severity.get(severity, 0) + 1
        documents.append({"document": name, "count": len(items),
                          "by_severity": by_severity,
                          "findings": [_summary(item) for item in items]})
    return {"statuses": wanted, "documents": documents,
            "count": sum(group["count"] for group in documents),
            "status_counts": counts, "truth_assessed": False}


def _proposal(node: Mapping[str, Any], text: str, document_sha: str) -> dict[str, Any]:
    document = document_key(node["document"])
    fragment = node.get("anchor")
    quote = node.get("quote")
    base = node.get("baseline") if isinstance(node.get("baseline"), Mapping) else {}
    item: dict[str, Any] = {
        "finding_id": node["id"], "severity": node.get("severity"),
        "text": node.get("text"), "anchor": fragment, "key": node.get("key"),
        "status": node.get("status"),
    }
    # The same bytes as the baseline: nothing can have been addressed.
    if base.get("document_sha256") == document_sha:
        item.update(anchor_changed=False, verdict="likely_open",
                    reason="document_unchanged",
                    detail="the document is identical to the recorded version")
        return item
    span = _anchored_text(document, fragment, text)
    if not span["found"]:
        if base.get("anchor_found") is False:
            item.update(verdict="unknown", reason="anchor_missing_at_baseline",
                        detail="the anchor did not resolve when the finding was "
                               "recorded either; read the document")
        else:
            item.update(verdict="may_be_addressed", reason="anchor_missing",
                        detail=span.get("reason") or "the anchored section is gone")
        return item
    current = span["text"]
    changed = (base.get("anchor_sha256") != _sha(current)
               if base.get("anchor_sha256") else None)
    quote_present = (_collapse(quote) in _collapse(current)) if quote else None
    item["anchor_changed"] = changed
    if quote_present is not None:
        item["quote_present"] = quote_present
    # A quote that was not in the span at baseline says nothing by its absence.
    quote_absent_at_baseline = base.get("quote_present") is False
    if quote_present is False and quote_absent_at_baseline:
        if changed is False:
            item.update(verdict="likely_open", reason="anchor_unchanged",
                        detail="the anchored text is unchanged")
        else:
            item.update(verdict="unknown", reason="quote_absent_at_baseline",
                        detail="the quote was not in the span when the finding "
                               "was recorded; read the span")
    elif quote_present is False:
        item.update(verdict="may_be_addressed", reason="quote_gone",
                    detail="the quoted text is no longer in the anchored span")
    elif changed and quote_present:
        item.update(verdict="likely_open", reason="anchor_changed_quote_present",
                    detail="the span changed but the quoted text is still there")
    elif changed:
        item.update(verdict="may_be_addressed", reason="anchor_changed",
                    detail=("the anchored text changed since the finding was recorded"
                            if fragment is not None else
                            "the finding has no anchor and the document changed"))
    elif changed is False:
        item.update(verdict="likely_open", reason="anchor_unchanged",
                    detail="the anchored text is unchanged")
    elif quote_present:
        item.update(verdict="likely_open", reason="quote_present",
                    detail="no baseline hash; the quoted text is still there")
    else:
        item.update(verdict="unknown", reason="no_baseline",
                    detail="no baseline hash or quote was recorded; read the span")
    return item


def rereview(
    state: Mapping[str, Any],
    *,
    document: str,
    text: str,
    statuses: Any = None,
) -> dict[str, Any]:
    """Propose which outstanding findings a new document version may address.

    Nothing is recorded: a proposal is a prompt to read the anchored span and
    then mark the finding with ``set_status``.
    """
    document = normalise_document(document)
    if not isinstance(text, str):
        raise LedgerError("text must be a string")
    wanted = _statuses(statuses)
    nodes = sorted((node for node in _ledger(state)
                    if document_key(node.get("document")) == document
                    and node.get("status") in wanted), key=_sort_key)
    document_sha = _sha(text)
    proposals = [_proposal(node, text, document_sha) for node in nodes]
    counts: dict[str, int] = {}
    for item in proposals:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    return {"document": document, "document_sha256": document_sha,
            "statuses": wanted, "proposals": proposals, "verdict_counts": counts,
            "auto_closed": False,
            "note": "Proposals only. Read each anchored span, then mark the "
                    "finding with research_review_finding_status.",
            "truth_assessed": False}
