from __future__ import annotations

import pytest

from ozagia.research import mcp
from ozagia.research.graph import ResearchError, ResearchGraph
from ozagia.research.service import ResearchService

DRAFT_V1 = """# Draft

## Introduction

We show the method is robust in all settings.

## Methods

Participants were sampled by convenience.

## Results

Effect sizes are large.
"""

DRAFT_V2 = """# Draft

## Introduction

We show the method is robust in the tested settings.

## Methods

Participants were sampled by convenience.

## Results

Effect sizes are large (d = 0.9, 95% CI 0.6-1.2).
"""


def _session(tmp_path):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "Review my draft"})["investigation_id"]
    return svc, sid


def _seed(tmp_path, svc, sid):
    (tmp_path / "draft.md").write_text(DRAFT_V1, encoding="utf-8")
    return svc.call("review_finding_add", {
        "session_id": sid,
        "conversation_session_id": "kiro-1",
        "document": "draft.md",
        "findings": [
            {"anchor": "introduction", "severity": "major",
             "text": "Overclaims generality.",
             "quote": "robust in all settings"},
            {"anchor": "doc:draft.md#methods", "severity": "blocking",
             "text": "Convenience sampling is not justified."},
            {"anchor": "draft/results", "severity": "minor",
             "text": "Report confidence intervals."},
            {"document": "notes.md", "severity": "note",
             "text": "Consider a figure."},
        ],
    })["findings"]


def test_ledger_lifecycle_across_sessions(tmp_path):
    svc, sid = _session(tmp_path)
    intro, methods, results, figure = _seed(tmp_path, svc, sid)
    assert intro["status"] == "open"
    assert intro["key"] == "doc:draft.md#introduction"
    assert methods["anchor"] == "methods"
    # A path alias is stored as the section's slug.
    assert results["anchor"] == "results"
    assert intro["baseline"]["quote_present"] is True
    assert "baseline" not in figure  # notes.md does not exist
    assert intro["history"][0]["conversation_session_id"] == "kiro-1"

    # A later session: a fresh service over the same state dir.
    later = ResearchService(tmp_path)
    changed = later.call("review_finding_status", {
        "session_id": sid, "finding_id": intro["id"], "status": "addressed",
        "note": "Scoped to tested settings.", "conversation_session_id": "kiro-2"})
    assert changed["previous_status"] == "open"
    later.call("review_finding_status", {
        "session_id": sid, "finding_id": methods["id"], "status": "disputed",
        "note": "Convenience sampling is standard for pilots.",
        "conversation_session_id": "kiro-2"})
    later.call("review_finding_status", {
        "session_id": sid, "finding_id": figure["id"], "status": "wont-fix",
        "note": "No room.", "conversation_session_id": "kiro-2"})
    # Reopen in a third session.
    reopened = ResearchService(tmp_path).call("review_finding_status", {
        "session_id": sid, "finding_id": intro["id"], "status": "open",
        "note": "Abstract still overclaims.", "conversation_session_id": "kiro-3"})
    history = reopened["finding"]["history"]
    assert [(h["status"], h["from"], h["conversation_session_id"]) for h in history] == [
        ("open", None, "kiro-1"), ("addressed", "open", "kiro-2"),
        ("open", "addressed", "kiro-3")]
    assert history[1]["note"] == "Scoped to tested settings."

    with pytest.raises(ResearchError, match="already open"):
        svc.call("review_finding_status", {
            "session_id": sid, "finding_id": intro["id"], "status": "open",
            "note": "x", "conversation_session_id": "kiro-3"})
    with pytest.raises(ResearchError, match="status must be one of"):
        svc.call("review_finding_status", {
            "session_id": sid, "finding_id": intro["id"], "status": "done",
            "note": "x", "conversation_session_id": "kiro-3"})

    state = ResearchGraph.load(sid, root=tmp_path).state()
    node = next(n for n in state["nodes"] if n["id"] == figure["id"])
    assert node["status"] == "wont_fix"
    amended = [e for e in state["events"] if e["type"] == "node_amended"]
    assert len(amended) == 4
    assert ResearchGraph.load(sid, root=tmp_path).check()["ok"] is False  # no Frame
    assert not any(issue["code"] == "ledger_finding_field_missing"
                   for issue in ResearchGraph.load(sid, root=tmp_path).check()["issues"])


def test_outstanding_groups_open_findings_by_document(tmp_path):
    svc, sid = _session(tmp_path)
    intro, methods, results, figure = _seed(tmp_path, svc, sid)
    svc.call("review_finding_status", {
        "session_id": sid, "finding_id": methods["id"], "status": "addressed",
        "note": "Justified in 2.1.", "conversation_session_id": "s2"})

    report = svc.call("review_outstanding", {"session_id": sid})
    assert report["statuses"] == ["open"]
    assert report["count"] == 3
    assert [group["document"] for group in report["documents"]] == ["draft.md", "notes.md"]
    draft = report["documents"][0]
    # Most severe first.
    assert [f["id"] for f in draft["findings"]] == [intro["id"], results["id"]]
    assert draft["by_severity"]["major"] == 1 and draft["by_severity"]["minor"] == 1
    assert report["status_counts"] == {"open": 3, "addressed": 1, "wont_fix": 0,
                                       "disputed": 0}

    only_notes = svc.call("review_outstanding", {"session_id": sid,
                                                 "document": "notes.md"})
    assert [g["document"] for g in only_notes["documents"]] == ["notes.md"]
    addressed = svc.call("review_outstanding", {"session_id": sid,
                                                "statuses": ["addressed"]})
    assert [f["id"] for f in addressed["documents"][0]["findings"]] == [methods["id"]]


def test_rereview_proposes_without_closing(tmp_path):
    svc, sid = _session(tmp_path)
    intro, methods, results, figure = _seed(tmp_path, svc, sid)
    rewritten = DRAFT_V2.replace("## Methods\n\nParticipants were sampled by convenience.\n\n", "")
    proposals = svc.call("review_rereview", {"session_id": sid, "document": "draft.md",
                                             "text": rewritten})
    assert proposals["auto_closed"] is False
    by_id = {p["finding_id"]: p for p in proposals["proposals"]}
    assert by_id[intro["id"]]["verdict"] == "may_be_addressed"
    assert by_id[intro["id"]]["reason"] == "quote_gone"
    assert by_id[methods["id"]]["reason"] == "anchor_missing"
    assert by_id[results["id"]]["reason"] == "anchor_changed"
    assert figure["id"] not in by_id  # another document
    # Most severe first.
    assert proposals["proposals"][0]["finding_id"] == methods["id"]

    # Nothing was closed.
    report = svc.call("review_outstanding", {"session_id": sid, "document": "draft.md"})
    assert report["count"] == 3

    # Unchanged text: everything is likely still open.
    same = svc.call("review_rereview", {"session_id": sid, "document": "draft.md",
                                        "text": DRAFT_V1})
    assert {p["reason"] for p in same["proposals"]} == {"anchor_unchanged"}
    assert {p["verdict"] for p in same["proposals"]} == {"likely_open"}

    # Without text, the workspace file is read.
    (tmp_path / "draft.md").write_text(DRAFT_V2, encoding="utf-8")
    from_file = svc.call("review_rereview", {"session_id": sid, "document": "draft.md"})
    by_id = {p["finding_id"]: p for p in from_file["proposals"]}
    assert by_id[methods["id"]]["reason"] == "anchor_unchanged"
    assert by_id[results["id"]]["verdict"] == "may_be_addressed"

    with pytest.raises(ResearchError, match="readable in the workspace"):
        svc.call("review_rereview", {"session_id": sid, "document": "../outside.md"})


def test_rereview_keeps_open_when_section_changes_but_quote_remains(tmp_path):
    svc, sid = _session(tmp_path)
    finding = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": "paper.tex",
        "document_text": "\\section{Claims}\nOur bound is tight.\n",
        "findings": [{"anchor": "claims", "severity": "major",
                      "text": "Tightness is not shown.", "quote": "bound is tight"}],
    })["findings"][0]
    assert finding["key"] == "doc:paper.tex#claims"
    result = svc.call("review_rereview", {
        "session_id": sid, "document": "paper.tex",
        "text": "\\section{Claims}\nOur bound is tight. See Lemma 2.\n"})
    assert result["proposals"][0]["reason"] == "anchor_changed_quote_present"
    assert result["proposals"][0]["verdict"] == "likely_open"

    unknown = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": "other.txt",
        "findings": [{"severity": "note", "text": "Typo somewhere."}]})["findings"][0]
    result = svc.call("review_rereview", {"session_id": sid, "document": "other.txt",
                                          "text": "changed"})
    assert result["proposals"][0]["finding_id"] == unknown["id"]
    assert result["proposals"][0]["verdict"] == "unknown"


def test_findings_import_from_a_review_run_once(tmp_path):
    svc, sid = _session(tmp_path)
    claim = svc.call("record", {"session_id": sid, "kind": "Claim", "fields": {
        "wording": "The method is robust.", "claim_type": "hypothesis",
        "status": "open", "source_locator": {"path": "paper.md", "start_line": 5}}})
    plan = svc.call("review_plan_create", {
        "session_id": sid, "question": "Is it robust?", "roles": ["methods"],
        "models": ["model-a"], "min_reviewers": 1,
        "target_id": claim["id"]})["plan"]
    run = svc.call("review_run_record", {
        "session_id": sid, "plan_id": plan["id"], "reviewer_id": "methods:a",
        "role": "methods", "model": "model-a", "packet_sha256": "0" * 64,
        "output": {"summary": "s", "findings": [
            {"wording": "No baseline comparison.", "severity": "major",
             "issue_type": "methods", "claim_id": claim["id"]},
            {"wording": "Unclear wording.", "severity": "minor",
             "issue_type": "clarity"},
        ]}})
    with pytest.raises(ResearchError, match="needs a document"):
        svc.call("review_finding_add", {
            "session_id": sid, "conversation_session_id": "s1",
            "run_id": run["run"]["id"]})
    added = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1",
        "run_id": run["run"]["id"], "document": "review-notes.md"})
    assert added["count"] == 2
    first, second = added["findings"]
    assert first["document"] == "paper.md"  # from the claim's source locator
    assert second["document"] == "review-notes.md"  # the default
    assert first["review_finding_id"] == run["findings"][0]["id"]
    assert first["review_run_id"] == run["run"]["id"]
    assert first["text"] == "No baseline comparison."

    again = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s2",
        "run_id": run["run"]["id"], "document": "review-notes.md"})
    assert again["count"] == 0
    assert again["skipped_already_in_ledger"] == 2

    state = ResearchGraph.load(sid, root=tmp_path).state()
    edges = {(e["type"], e["src"], e["tgt"]) for e in state["edges"]}
    assert ("derived_from", first["id"], run["findings"][0]["id"]) in edges
    assert ("about", first["id"], claim["id"]) in edges


def test_ledger_tools_are_registered():
    names = {tool["name"] for tool in mcp.research_tools()}
    assert {"research_review_finding_add", "research_review_finding_status",
            "research_review_outstanding", "research_review_rereview"} <= names
