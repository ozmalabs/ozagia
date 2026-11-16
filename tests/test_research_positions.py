from __future__ import annotations

import json

import pytest

from ozagia.research import mcp
from ozagia.research.graph import ResearchError, ResearchGraph
from ozagia.research.service import ResearchService


def _session(tmp_path, roots=None):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "What does the book argue?",
                            "roots": roots or []})["investigation_id"]
    return svc, sid


def _claim(svc, sid, wording, **fields):
    return svc.call("record", {"session_id": sid, "kind": "Claim",
                               "fields": {"wording": wording, **fields}})


def test_record_position_is_a_conversation_holding(tmp_path):
    svc, sid = _session(tmp_path)
    claim = _claim(svc, sid, "Attention is a separate faculty.")
    result = svc.call("position_record", {
        "session_id": sid,
        "stance": "The chapter treats attention as precision weighting, not a faculty.",
        "conversation_session_id": "opencode-42",
        "topic": "attention as precision weighting",
        "quote": "attention is precision weighting",
        "correction_of": "Agent summarised chapter 3 as defending a faculty view.",
        "concerns": [claim["id"]],
        "documents": ["notes/chapter3.md"],
        "stated_at": "2026-11-10T09:30:00Z",
    })
    position = result["position"]
    assert position["label"] == "user position"
    assert position["date"] == "2026-11-10"
    assert position["position_status"] == "active"
    assert position["quote"] == "attention is precision weighting"
    assert result["source"]["source_type"] == "conversation"
    assert result["source"]["locator"] == {
        "kind": "conversation", "conversation_session_id": "opencode-42"}

    state = ResearchGraph.load(sid, root=tmp_path).state()
    holding = next(n for n in state["nodes"] if n["id"] == position["id"])
    assert holding["type"] == "Holding"
    assert holding["epistemic_role"] == "user_position"
    assert holding["source_type"] == "conversation"
    assert holding["truth_assessed"] is False
    edges = {(e["type"], e["src"], e["tgt"]) for e in state["edges"]}
    assert ("held_as", result["source"]["id"], position["id"]) in edges
    assert ("concerns", position["id"], claim["id"]) in edges

    checked = svc.call("holding_check", {"session_id": sid,
                                         "holding_id": position["id"]})
    assert checked["ok"] is True and checked["warnings"] == []

    # A second position from the same conversation reuses its Source.
    again = svc.call("position_record", {
        "session_id": sid, "stance": "Chapter 4 is a digression.",
        "conversation_session_id": "opencode-42", "topic": "chapter 4"})
    assert again["source"]["id"] == result["source"]["id"]


def test_position_needs_something_to_be_found_by(tmp_path):
    svc, sid = _session(tmp_path)
    with pytest.raises(ResearchError, match="topic, concerns or documents"):
        svc.call("position_record", {"session_id": sid, "stance": "No.",
                                     "conversation_session_id": "c1"})
    with pytest.raises(ResearchError, match="unknown record"):
        svc.call("position_record", {"session_id": sid, "stance": "No.",
                                     "conversation_session_id": "c1",
                                     "concerns": ["claim-0099"]})
    with pytest.raises(ResearchError, match="ISO 8601"):
        svc.call("position_record", {"session_id": sid, "stance": "No.",
                                     "topic": "t", "conversation_session_id": "c1",
                                     "stated_at": "last tuesday"})


def test_supersede_and_retract_keep_history(tmp_path):
    svc, sid = _session(tmp_path)
    claim = _claim(svc, sid, "The effect replicates.")
    first = svc.call("position_record", {
        "session_id": sid, "stance": "The effect does not replicate.",
        "conversation_session_id": "s1", "topic": "replication of the effect",
        "concerns": [claim["id"]]})["position"]
    second = svc.call("position_supersede", {
        "session_id": sid, "position_id": first["id"],
        "stance": "It replicates only in the original lab's population.",
        "conversation_session_id": "s2"})
    assert second["superseded"]["position_status"] == "superseded"
    assert second["superseded"]["superseded_by"] == second["position"]["id"]
    assert second["superseded"]["superseded_in"] == "s2"
    # Topic and concerns carry over.
    assert second["position"]["topic"] == "replication of the effect"
    assert second["position"]["concerns"] == [claim["id"]]
    assert second["position"]["supersedes"] == first["id"]

    with pytest.raises(ResearchError, match="only an active position"):
        svc.call("position_supersede", {
            "session_id": sid, "position_id": first["id"],
            "stance": "x", "conversation_session_id": "s3"})

    listed = svc.call("position_list", {"session_id": sid})
    assert [p["id"] for p in listed["positions"]] == [second["position"]["id"]]
    assert listed["status_counts"] == {"active": 1, "superseded": 1, "retracted": 0}

    retracted = svc.call("position_retract", {
        "session_id": sid, "position_id": second["position"]["id"],
        "conversation_session_id": "s3", "note": "I was thinking of another study."})
    assert retracted["position"]["position_status"] == "retracted"
    assert retracted["position"]["retracted_in"] == "s3"
    with pytest.raises(ResearchError, match="already retracted"):
        svc.call("position_retract", {
            "session_id": sid, "position_id": second["position"]["id"],
            "conversation_session_id": "s3"})

    everything = svc.call("position_list", {"session_id": sid, "status": "all"})
    assert everything["count"] == 2
    assert svc.call("position_list", {"session_id": sid})["count"] == 0
    by_claim = svc.call("position_list", {"session_id": sid, "status": "all",
                                          "node_id": claim["id"]})
    assert by_claim["count"] == 2

    state = ResearchGraph.load(sid, root=tmp_path).state()
    edges = {(e["type"], e["src"], e["tgt"]) for e in state["edges"]}
    assert ("supersedes", second["position"]["id"], first["id"]) in edges
    amendments = [e for e in state["events"] if e["type"] == "node_amended"]
    assert [e["node_id"] for e in amendments] == [first["id"], second["position"]["id"]]


def test_context_surfaces_positions_apart_from_evidence(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "chapter3.md").write_text(
        "# Chapter 3\n\nPrecision weighting modulates prediction errors.\n",
        encoding="utf-8")
    roots = [{"root_id": "book", "path": str(root), "files": ["chapter3.md"]}]
    svc, sid = _session(tmp_path, roots)
    source = svc.call("record", {"session_id": sid, "kind": "Source", "fields": {
        "title": "Chapter 3", "locator": {"root_id": "book", "path": "chapter3.md"}}})
    on_topic = svc.call("position_record", {
        "session_id": sid, "stance": "Attention is precision weighting here.",
        "conversation_session_id": "c1", "topic": "attention and precision",
        "stated_at": "2026-10-01T12:00:00Z"})["position"]
    on_source = svc.call("position_record", {
        "session_id": sid, "stance": "Chapter 3's figures are illustrative only.",
        "conversation_session_id": "c1", "topic": "figures",
        "concerns": [source["id"]]})["position"]
    unrelated = svc.call("position_record", {
        "session_id": sid, "stance": "Tumour growth models are out of scope.",
        "conversation_session_id": "c1", "topic": "oncology growth curves"})["position"]

    ctx = svc.call("context", {"session_id": sid, "query": "precision weighting"})
    assert ctx["results"], "the passage is retrieved as sourced context"
    section = ctx["user_positions"]
    assert section["separate_from_evidence"] is True
    assert section["label"] == "user position"
    ids = [item["id"] for item in section["items"]]
    assert on_topic["id"] in ids
    assert on_source["id"] in ids
    assert unrelated["id"] not in ids
    item = next(i for i in section["items"] if i["id"] == on_topic["id"])
    assert item["date"] == "2026-10-01"
    assert item["label"] == "user position"
    reasons = {r["kind"] for i in section["items"] for r in i["match"]["reasons"]}
    assert {"topic_similarity", "concerns_retrieved_source"} <= reasons
    # Positions never appear among the sourced results.
    assert all("stance" not in json.dumps(result) for result in ctx["results"])

    # A retracted position is no longer surfaced.
    svc.call("position_retract", {"session_id": sid, "position_id": on_topic["id"],
                                  "conversation_session_id": "c2"})
    ctx = svc.call("context", {"session_id": sid, "query": "precision weighting"})
    assert on_topic["id"] not in [i["id"] for i in ctx["user_positions"]["items"]]

    # Listing a record surfaces positions about it whatever the query.
    ctx = svc.call("context", {"session_id": sid, "query": "zzz",
                               "node_ids": [source["id"]]})
    assert [i["id"] for i in ctx["user_positions"]["items"]] == [on_source["id"]]


def test_positions_persist_and_are_exposed_as_mcp_tools(tmp_path):
    svc, sid = _session(tmp_path)
    recorded = svc.call("position_record", {
        "session_id": sid, "stance": "The definition is operational.",
        "conversation_session_id": "c1", "topic": "definition"})["position"]
    reloaded = ResearchService(tmp_path).call("position_list", {"session_id": sid})
    assert [p["id"] for p in reloaded["positions"]] == [recorded["id"]]

    names = {tool["name"] for tool in mcp.research_tools()}
    assert {"research_position_record", "research_position_supersede",
            "research_position_retract", "research_position_list"} <= names
    via_mcp = mcp.call_research_tool("research_position_list",
                                     {"session_id": sid}, tmp_path)
    assert via_mcp["count"] == 1
