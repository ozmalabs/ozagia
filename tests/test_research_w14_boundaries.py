"""W14 review fixes: the position/evidence boundary, ledger integrity,
document identity, schema version and workspace read scope."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.research import review_ledger
from ozagia.research.graph import (
    LEDGER_SEVERITIES,
    SCHEMA_VERSION,
    ResearchError,
    ResearchGraph,
)
from ozagia.research.service import ResearchService, _open_beneath
from ozagia.research.structured_review import SEVERITIES

PAPER = """# Paper

## Claims

Our bound is tight.

## Methods

We sample by convenience.
"""


def _session(tmp_path, roots=None):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "Q?", "roots": roots or []})["investigation_id"]
    return svc, sid


def _position(svc, sid, **extra):
    return svc.call("position_record", {
        "session_id": sid, "stance": "The bound is not tight.",
        "conversation_session_id": "conv-1", "topic": "tightness", **extra})


def _claim(svc, sid, wording="The bound is tight."):
    return svc.call("record", {"session_id": sid, "kind": "Claim",
                               "fields": {"wording": wording}})


def _codes(issues):
    return {issue["code"] for issue in issues}


# --------------------------------------------------------------------------- #
# 1. a conversation Source is never evidence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relation, conversation_is_src", [
    ("supports", True), ("opposes", True), ("cites", False)])
def test_conversation_source_cannot_be_an_evidence_endpoint(
        tmp_path, relation, conversation_is_src):
    svc, sid = _session(tmp_path)
    claim = _claim(svc, sid)
    source = _position(svc, sid, concerns=[claim["id"]])["source"]
    src, tgt = ((source["id"], claim["id"]) if conversation_is_src
                else (claim["id"], source["id"]))
    with pytest.raises(ResearchError, match="conversation Source cannot be an endpoint"):
        svc.call("link", {"session_id": sid, "src": src, "relation": relation,
                          "tgt": tgt})


def test_conversation_source_holds_only_user_positions(tmp_path):
    svc, sid = _session(tmp_path)
    source = _position(svc, sid)["source"]
    # Through the holdings API.
    with pytest.raises(ResearchError, match="only be held as user_position"):
        svc.call("holding_record", {
            "session_id": sid, "source_id": source["id"],
            "source_type": "conversation", "epistemic_role": "evidence",
            "status": "accepted", "reviewer": "me"})
    # Through the generic record op.
    with pytest.raises(ResearchError, match="conversation can only hold"):
        svc.call("record", {"session_id": sid, "kind": "Holding", "fields": {
            "source_id": source["id"], "source_type": "conversation",
            "epistemic_role": "evidence", "authority": "personal"}})
    # A user_position role needs a conversation Source.
    other = svc.call("record", {"session_id": sid, "kind": "Source", "fields": {
        "title": "Book", "source_type": "book", "locator": {"isbn": "1"}}})
    with pytest.raises(ResearchError, match="held by a conversation Source"):
        svc.call("record", {"session_id": sid, "kind": "Holding", "fields": {
            "source_id": other["id"], "source_type": "book",
            "epistemic_role": "user_position", "authority": "personal"}})
    # held_as from a conversation to an evidence Holding of another source.
    evidence = svc.call("holding_record", {
        "session_id": sid, "source_id": other["id"], "source_type": "book",
        "epistemic_role": "evidence"})["holding"]
    with pytest.raises(ResearchError, match="only hold user_position"):
        svc.call("link", {"session_id": sid, "src": source["id"],
                          "relation": "held_as", "tgt": evidence["id"]})


def test_check_and_holding_check_flag_a_conversation_used_as_evidence(tmp_path):
    """A graph written before the rule (or edited by hand) is reported."""
    svc, sid = _session(tmp_path)
    claim = _claim(svc, sid)
    result = _position(svc, sid)
    source, holding = result["source"], result["position"]
    graph = ResearchGraph.load(sid, root=tmp_path)
    assert not _codes(graph.check()["issues"]) & {
        "conversation_source_used_as_evidence", "conversation_held_as_non_position"}
    # Bypass link/amend validation, as an older build's write would.
    graph._data["edges"].append({"id": "edge-999999", "type": "supports",
                                 "src": source["id"], "tgt": claim["id"]})
    node = next(n for n in graph._data["nodes"] if n["id"] == holding["id"])
    node["epistemic_role"] = "evidence"
    codes = _codes(graph.check()["issues"])
    assert "conversation_source_used_as_evidence" in codes
    assert "conversation_held_as_non_position" in codes

    from ozagia.research.holdings import check_holding
    checked = check_holding(graph.state(), holding["id"])
    assert checked["ok"] is False
    assert {"conversation_source_used_as_evidence",
            "conversation_held_as_non_position"} <= _codes(checked["issues"])


# --------------------------------------------------------------------------- #
# 3, 4. positions stay out of recorded_claims; is_position is not forgeable
# --------------------------------------------------------------------------- #
def test_context_recorded_claims_omit_positions(tmp_path):
    svc, sid = _session(tmp_path)
    claim = _claim(svc, sid)
    position = _position(svc, sid, concerns=[claim["id"]])["position"]
    ctx = svc.call("context", {"session_id": sid, "query": "tightness"})
    assert position["id"] in [i["id"] for i in ctx["user_positions"]["items"]]
    recorded = next(c for c in ctx["recorded_claims"] if c["node"]["id"] == claim["id"])
    assert recorded["related_nodes"] == []
    assert all(e["type"] != "concerns" for e in recorded["edges"])
    assert recorded["user_positions_omitted"] == 1


def test_a_flagged_evidence_holding_is_not_a_position(tmp_path):
    svc, sid = _session(tmp_path)
    book = svc.call("record", {"session_id": sid, "kind": "Source", "fields": {
        "title": "Book", "source_type": "book", "locator": {"isbn": "1"}}})
    forged = svc.call("record", {"session_id": sid, "kind": "Holding", "fields": {
        "source_id": book["id"], "source_type": "book", "epistemic_role": "evidence",
        "authority": "scholarly", "position": True, "position_status": "active",
        "stance": "tightness is shown", "topic": "tightness"}})
    assert svc.call("position_list", {"session_id": sid, "status": "all"})["count"] == 0
    ctx = svc.call("context", {"session_id": sid, "query": "tightness"})
    assert forged["id"] not in [i["id"] for i in ctx["user_positions"]["items"]]
    with pytest.raises(ResearchError, match="unknown user position"):
        svc.call("position_retract", {"session_id": sid, "position_id": forged["id"],
                                      "conversation_session_id": "c"})


# --------------------------------------------------------------------------- #
# 2. rereview on unchanged text
# --------------------------------------------------------------------------- #
def _seed_unresolved(tmp_path, svc, sid):
    (tmp_path / "paper.md").write_text(PAPER, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    findings = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": "paper.md",
        "findings": [
            {"anchor": "nosuch", "severity": "major", "text": "Bad anchor."},
            {"anchor": "claims", "severity": "major", "text": "Absent quote.",
             "quote": "never written here"},
            {"anchor": "claims", "severity": "minor", "text": "Valid."},
        ]})["findings"]
    txt = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": "notes.txt",
        "findings": [{"anchor": "intro", "severity": "note", "text": "Sectionless."}],
    })["findings"][0]
    return (*findings, txt)


def test_rereview_unchanged_text_keeps_everything_open(tmp_path):
    svc, sid = _session(tmp_path)
    bad_anchor, absent_quote, valid, txt = _seed_unresolved(tmp_path, svc, sid)
    assert bad_anchor["baseline"]["anchor_found"] is False
    assert absent_quote["baseline"]["quote_present"] is False

    same = svc.call("review_rereview", {"session_id": sid, "document": "paper.md",
                                        "text": PAPER})
    assert {p["verdict"] for p in same["proposals"]} == {"likely_open"}
    assert {p["reason"] for p in same["proposals"]} == {"document_unchanged"}
    notes = svc.call("review_rereview", {"session_id": sid, "document": "notes.txt"})
    assert notes["proposals"][0]["verdict"] == "likely_open"


def test_rereview_missing_at_baseline_maps_to_unknown(tmp_path):
    svc, sid = _session(tmp_path)
    bad_anchor, absent_quote, valid, txt = _seed_unresolved(tmp_path, svc, sid)
    edited = PAPER.replace("We sample by convenience.", "We sample at random.")
    by_id = {p["finding_id"]: p for p in svc.call("review_rereview", {
        "session_id": sid, "document": "paper.md", "text": edited})["proposals"]}
    assert (by_id[bad_anchor["id"]]["verdict"],
            by_id[bad_anchor["id"]]["reason"]) == ("unknown", "anchor_missing_at_baseline")
    # The claims section is unchanged, so its findings stay open.
    assert by_id[absent_quote["id"]]["verdict"] == "likely_open"
    assert by_id[valid["id"]]["reason"] == "anchor_unchanged"

    rewritten = PAPER.replace("Our bound is tight.", "Our bound is tight (Lemma 2).")
    by_id = {p["finding_id"]: p for p in svc.call("review_rereview", {
        "session_id": sid, "document": "paper.md", "text": rewritten})["proposals"]}
    assert (by_id[absent_quote["id"]]["verdict"],
            by_id[absent_quote["id"]]["reason"]) == ("unknown", "quote_absent_at_baseline")
    assert by_id[valid["id"]]["reason"] == "anchor_changed"

    notes = svc.call("review_rereview", {"session_id": sid, "document": "notes.txt",
                                         "text": "edited notes\n"})
    assert notes["proposals"][0]["verdict"] == "unknown"


# --------------------------------------------------------------------------- #
# 5. LedgerFinding integrity
# --------------------------------------------------------------------------- #
def test_ledger_severities_match_structured_review():
    assert tuple(LEDGER_SEVERITIES) == tuple(SEVERITIES)


@pytest.mark.parametrize("fields, message", [
    ({"status": "open", "severity": "bogus"}, "severity"),
    ({"status": "done", "severity": "major"}, "status"),
    ({"status": "addressed", "severity": "major", "history": None}, "history"),
    ({"status": "addressed", "severity": "major",
      "history": [{"status": "open"}]}, "history does not end"),
])
def test_record_rejects_malformed_ledger_findings(tmp_path, fields, message):
    svc, sid = _session(tmp_path)
    base = {"document": "paper.md", "text": "x",
            "history": [{"status": fields["status"]}]}
    base.update(fields)
    if base["history"] is None:
        del base["history"]
    with pytest.raises(ResearchError, match=message):
        svc.call("record", {"session_id": sid, "kind": "LedgerFinding",
                            "fields": base})


def test_check_flags_and_outstanding_tolerates_a_malformed_finding(tmp_path):
    svc, sid = _session(tmp_path)
    svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": "paper.md",
        "findings": [{"severity": "major", "text": "Fine."}]})
    graph = ResearchGraph.load(sid, root=tmp_path)
    graph._data["nodes"].append({
        "id": "ledgerfinding-0099", "type": "LedgerFinding", "created_at": "x",
        "document": "paper.md", "severity": "bogus", "text": "Bad.",
        "status": "addressed"})
    codes = _codes(graph.check()["issues"])
    assert {"ledger_finding_severity_invalid", "ledger_finding_history_invalid"} <= codes
    graph._data["nodes"][-1]["status"] = "open"
    report = review_ledger.outstanding(graph.state())
    assert report["count"] == 2
    assert report["documents"][0]["by_severity"]["bogus"] == 1
    assert report["documents"][0]["findings"][-1]["id"] == "ledgerfinding-0099"


# --------------------------------------------------------------------------- #
# 6. document identity
# --------------------------------------------------------------------------- #
def test_ledger_document_paths_are_normalised(tmp_path):
    svc, sid = _session(tmp_path)
    (tmp_path / "paper.md").write_text(PAPER, encoding="utf-8")
    finding = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": "./paper.md",
        "findings": [{"anchor": "doc:paper.md#claims", "severity": "major",
                      "text": "Tightness."}]})["findings"][0]
    assert finding["document"] == "paper.md"
    assert finding["baseline"]["anchor_found"] is True
    for name in ("paper.md", "./paper.md", "sub/../paper.md"):
        result = svc.call("review_rereview", {"session_id": sid, "document": name,
                                              "text": PAPER})
        assert [p["finding_id"] for p in result["proposals"]] == [finding["id"]]
        report = svc.call("review_outstanding", {"session_id": sid, "document": name})
        assert report["count"] == 1


def test_position_documents_are_workspace_relative_and_match_by_root(tmp_path):
    notes = tmp_path / "notes"
    other = tmp_path / "other"
    for directory, body in ((notes, "Prediction errors are weighted."),
                            (other, "Unrelated material about gardens.")):
        directory.mkdir()
        (directory / "chapter3.md").write_text(
            f"# Chapter 3\n\n{body}\n", encoding="utf-8")
    roots = [{"root_id": "notes", "path": str(notes), "files": ["chapter3.md"]},
             {"root_id": "other", "path": str(other), "files": ["chapter3.md"]}]
    svc, sid = _session(tmp_path, roots)
    by_doc = _position(svc, sid, topic="zebra", documents=["./notes/chapter3.md"])
    assert by_doc["position"]["documents"] == ["notes/chapter3.md"]
    # A Source in the other root with the same root-relative path.
    source = svc.call("record", {"session_id": sid, "kind": "Source", "fields": {
        "title": "Other", "locator": {"root_id": "other", "path": "chapter3.md"}}})
    by_source = _position(svc, sid, topic="giraffe", concerns=[source["id"]])

    ctx = svc.call("context", {"session_id": sid, "query": "prediction errors"})
    assert {r["citation"]["root_id"] for r in ctx["results"]} == {"notes"}
    items = {i["id"]: i for i in ctx["user_positions"]["items"]}
    # Root-relative citation chapter3.md is the workspace document
    # notes/chapter3.md.
    assert {r["kind"] for r in items[by_doc["position"]["id"]]["match"]["reasons"]} \
        == {"names_retrieved_document"}
    # Same relative path, different root: no match.
    assert by_source["position"]["id"] not in items
    listed = svc.call("position_list", {"session_id": sid,
                                        "document": "notes/./chapter3.md"})
    assert [p["id"] for p in listed["positions"]] == [by_doc["position"]["id"]]


# --------------------------------------------------------------------------- #
# 7. schema version
# --------------------------------------------------------------------------- #
def _graph_file(tmp_path, sid) -> Path:
    return ResearchGraph.load(sid, root=tmp_path).path


def test_pre_w14_graph_loads_and_is_upgraded(tmp_path):
    svc, sid = _session(tmp_path)
    _claim(svc, sid)
    path = _graph_file(tmp_path, sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 6
    path.write_text(json.dumps(data), encoding="utf-8")

    assert ResearchGraph.load(sid, root=tmp_path).check()["node_count"] == 2
    _position(svc, sid)
    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == SCHEMA_VERSION == 7


def test_newer_schema_gives_a_clear_error(tmp_path):
    svc, sid = _session(tmp_path)
    path = _graph_file(tmp_path, sid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ResearchError, match="newer than this build.*upgrade ozagia"):
        ResearchGraph.load(sid, root=tmp_path)


# --------------------------------------------------------------------------- #
# 8. workspace read scope
# --------------------------------------------------------------------------- #
def test_workspace_reads_exclude_state_dotfiles_and_escapes(tmp_path):
    svc, sid = _session(tmp_path)
    read = svc._read_workspace_document
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "a.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(outside)
    (tmp_path / "inside.md").symlink_to(tmp_path / "docs" / "a.md")
    (tmp_path / "state.json").symlink_to(_graph_file(tmp_path, sid))

    assert read("docs/a.md") == "# A\n"
    assert read("./docs/a.md") == "# A\n"
    assert read("inside.md") == "# A\n"  # a symlink that stays inside is fine
    graph_rel = _graph_file(tmp_path, sid).relative_to(tmp_path).as_posix()
    for name in (".env", ".hidden/a.md", graph_rel, "state.json", "escape.md",
                 "../" + outside.name, str(outside), "docs", ""):
        assert read(name) is None, name

    # The ledger cannot probe a dotfile with a guessed quote.
    finding = svc.call("review_finding_add", {
        "session_id": sid, "conversation_session_id": "s1", "document": ".env",
        "findings": [{"severity": "note", "text": "x", "quote": "SECRET"}]})
    assert "baseline" not in finding["findings"][0]


def test_open_beneath_refuses_a_symlinked_component(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.md").write_text("ok", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    fd = _open_beneath(tmp_path, Path("real/f.md"))
    with open(fd, "rb") as stream:
        assert stream.read() == b"ok"
    # As if "real" were swapped for a symlink after resolution.
    with pytest.raises(OSError):
        _open_beneath(tmp_path, Path("link/f.md"))
    with pytest.raises(OSError):
        _open_beneath(tmp_path, Path("link"))
