"""Explicit adapter-declared repository bridges."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.core import federation, repo_bridges, repo_relations
from ozagia.core.memory import Builder
from ozagia.federation_cli import main


def _node(node_id: int, kind: str, key: str, **attrs) -> dict:
    return {"id": node_id, "type": kind, "key": key, "attrs": attrs}


def _member(alias: str, nodes: list[dict]) -> dict:
    return {"alias": alias, "label": alias, "root": f"/work/{alias}",
            "snapshot": {"version": 2, "nodes": nodes, "edges": []},
            "error": None}


def _bridge(node_id: int, key: str, **overrides) -> dict:
    attrs = {
        "source_repo": "bigville",
        "target_repo": "ocelot",
        "relation": "dynamic_import",
        "provenance": "Bigville's ocelot backend imports Ocelot's adapter",
        "evidence": [{"path": "bigville/backends/ocelot.py", "line": 15}],
        "symbol": "worlds.bigville_ocelot_adapter",
        "explicit": True,
    }
    attrs.update(overrides)
    return _node(node_id, "RepoBridge", key, **attrs)


def _analyze(tmp_path: Path, members: list[dict]) -> dict:
    return repo_relations.analyze(
        tmp_path, members,
        member_states={member["alias"]: "fresh" for member in members})


def _snapshot(root: Path, name: str, nodes: list[dict] | None = None) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    (state / "graph.json").write_text(json.dumps({
        "version": 2,
        "nodes": [{"id": 0, "type": "Repo", "key": "repo:",
                   "attrs": {"name": name, "root": str(root), "head": "head"}},
                  *(nodes or [])],
        "edges": [],
    }), encoding="utf-8")


def _manifest(root: Path, members: list[object]) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    (state / "federation.json").write_text(json.dumps({
        "version": 1, "members": members, "links": [],
    }), encoding="utf-8")


def test_helper_emits_a_bounded_explicit_bridge_node():
    builder = Builder()

    node_id = repo_bridges.add_repo_bridge(
        builder,
        source_repo="bigville",
        target_repo="ocelot",
        relation="Dynamic Import",
        provenance="  Bigville's ocelot backend imports Ocelot's adapter  ",
        evidence=("bigville/backends/ocelot.py",
                  {"path": "tests/test_bigville_ocelot_fresh_process.py",
                   "line": 62},
                  "bigville/backends/ocelot.py"),
        symbol="worlds.bigville_ocelot_adapter",
    )

    assert builder.count("RepoBridge") == 1
    node = builder.nodes[node_id]
    assert node["type"] == "RepoBridge"
    assert node["key"] == (
        "repo_bridge:bigville::ocelot::dynamic_import::"
        "worlds.bigville_ocelot_adapter")
    assert node["attrs"] == {
        "source_repo": "bigville",
        "target_repo": "ocelot",
        "relation": "dynamic_import",
        "provenance": "Bigville's ocelot backend imports Ocelot's adapter",
        "evidence": [
            "bigville/backends/ocelot.py",
            {"path": "tests/test_bigville_ocelot_fresh_process.py", "line": 62},
        ],
        "explicit": True,
        "symbol": "worlds.bigville_ocelot_adapter",
    }

    repeated = repo_bridges.add_repo_bridge(
        builder, source_repo="bigville", target_repo="ocelot",
        relation="dynamic_import",
        provenance="Bigville's ocelot backend imports Ocelot's adapter",
        evidence=("bigville/backends/ocelot.py",),
        symbol="worlds.bigville_ocelot_adapter")
    assert repeated == node_id
    assert builder.count("RepoBridge") == 1


def test_symbol_separator_is_encoded_in_the_stable_key():
    builder = Builder()

    node_id = repo_bridges.add_repo_bridge(
        builder, source_repo="bigville", target_repo="ocelot",
        relation="protocol", provenance="declared protocol binding",
        symbol="crate::Adapter")

    assert builder.nodes[node_id]["key"] == (
        "repo_bridge:bigville::ocelot::protocol::crate%3A%3AAdapter")
    assert builder.nodes[node_id]["attrs"]["symbol"] == "crate::Adapter"


@pytest.mark.parametrize(("overrides", "message"), [
    ({"source_repo": None}, "source_repo must be a non-empty string"),
    ({"target_repo": ""}, "target_repo must be a non-empty string"),
    ({"target_repo": "bigville"}, "must differ"),
    ({"relation": ""}, "relation must be a non-empty string"),
    ({"provenance": " "}, "provenance must be a non-empty string"),
    ({"provenance": "x" * (repo_bridges.MAX_TEXT + 1)}, "exceeds"),
    ({"symbol": ""}, "symbol must be a non-empty string"),
    ({"evidence": "bigville/backends/ocelot.py"},
     "evidence must be an iterable"),
    ({"evidence": [{"line": 1}]}, "evidence\\[0\\]\\.path"),
    ({"evidence": [{"path": "x.py", "line": 0}]}, "positive integer"),
    ({"evidence": [{"path": "x.py", "pointer": "#/x"}]}, "unknown fields"),
    ({"evidence": ["x.py"] * (repo_bridges.MAX_EVIDENCE + 1)},
     "more than"),
])
def test_helper_rejects_missing_or_unbounded_fields(overrides, message):
    fields = {
        "source_repo": "bigville",
        "target_repo": "ocelot",
        "relation": "dynamic_import",
        "provenance": "declared integration",
    }
    fields.update(overrides)

    with pytest.raises(repo_bridges.BridgeValidationError, match=message):
        repo_bridges.add_repo_bridge(Builder(), **fields)


def test_explicit_false_and_missing_provenance_are_not_edges(tmp_path):
    inferred = _bridge(1, "repo_bridge:inferred", explicit=False)
    missing = _bridge(2, "repo_bridge:missing", provenance="")
    report = _analyze(tmp_path, [
        _member("bigville", [inferred, missing]),
        _member("ocelot", []),
    ])

    assert report["bridges"]["state"] == "partial"
    assert report["bridges"]["edges"] == []
    assert report["edges"] == []
    assert report["dependencies"] == []
    assert report["counts"]["bridge_edges"] == 0
    assert report["bridges"]["counts"] == {
        "nodes": 2, "edges": 0, "unpaired": 0, "errors": 2}
    assert any("explicit must be true" in error
               for error in report["bridges"]["errors"])
    assert any("provenance must be a non-empty string" in error
               for error in report["bridges"]["errors"])
    assert report["state"] == "partial"


def test_explicit_bridge_is_an_integration_edge_outside_the_dependency_view(
        tmp_path):
    builder = Builder()
    repo_bridges.add_repo_bridge(
        builder, source_repo="bigville", target_repo="ocelot",
        relation="dynamic_import",
        provenance="Bigville's ocelot backend imports Ocelot's adapter",
        evidence=[{"path": "bigville/backends/ocelot.py", "line": 15}],
        symbol="worlds.bigville_ocelot_adapter")
    report = _analyze(tmp_path, [
        _member("bigville", builder.nodes),
        _member("ocelot", []),
    ])

    expected = {
        "source": "bigville", "target": "ocelot",
        "relation": "dynamic_import", "kind": "integration",
        "origin": "adapter_bridge", "explicit": True,
        "provenance": ["Bigville's ocelot backend imports Ocelot's adapter"],
        "evidence": [{"path": "bigville/backends/ocelot.py", "line": 15}],
        "members": ["bigville"],
        "symbol": "worlds.bigville_ocelot_adapter",
    }
    assert report["bridges"]["state"] == "ok"
    assert report["bridges"]["edges"] == [expected]
    assert report["edges"] == [expected]
    assert report["dependencies"] == []
    assert report["cycles"] == []
    assert set(report["order"]) == {"bigville", "ocelot"}
    assert report["levels"] == {"bigville": 0, "ocelot": 0}
    assert report["reverse_dependencies"] == {"bigville": [], "ocelot": []}
    assert report["counts"]["bridge_edges"] == 1
    assert report["state"] == "ok"


@pytest.mark.parametrize(("target", "members"), [
    ("external-tool", ["bigville", "ocelot"]),
    ("ocelot", ["bigville"]),
])
def test_unknown_or_unselected_targets_are_reported_not_guessed(
        tmp_path, target, members):
    report = _analyze(tmp_path, [
        _member("bigville", [_bridge(1, "repo_bridge:target",
                                     target_repo=target)]),
        *[_member(alias, []) for alias in members if alias != "bigville"],
    ])

    assert report["bridges"]["state"] == "partial"
    assert report["bridges"]["edges"] == []
    assert report["edges"] == []
    assert report["bridges"]["unpaired"] == [{
        "member": "bigville", "source": "bigville", "target": target,
        "relation": "dynamic_import", "reason": "target_not_selected",
        "provenance": "Bigville's ocelot backend imports Ocelot's adapter",
        "evidence": [{"path": "bigville/backends/ocelot.py", "line": 15}],
        "symbol": "worlds.bigville_ocelot_adapter",
    }]
    assert {member["id"] for member in report["members"]} == set(members)
    assert report["state"] == "partial"


def test_duplicate_bridges_are_deduped_and_evidence_merged(tmp_path):
    first = _bridge(1, "repo_bridge:first")
    second = _bridge(2, "repo_bridge:second")
    second["attrs"] = dict(second["attrs"],
                           evidence=[{"path": "tests/test_bigville_ocelot_fresh_process.py",
                                      "line": 62}])
    report = _analyze(tmp_path, [
        _member("bigville", [first]),
        _member("ocelot", [second]),
    ])

    assert report["bridges"]["state"] == "ok"
    assert report["bridges"]["counts"] == {
        "nodes": 2, "edges": 1, "unpaired": 0, "errors": 0}
    edge = report["bridges"]["edges"][0]
    assert edge["members"] == ["bigville", "ocelot"]
    assert edge["provenance"] == [
        "Bigville's ocelot backend imports Ocelot's adapter"]
    assert edge["evidence"] == [
        {"path": "bigville/backends/ocelot.py", "line": 15},
        {"path": "tests/test_bigville_ocelot_fresh_process.py", "line": 62},
    ]


def test_merged_bridge_provenance_and_evidence_are_bounded(tmp_path):
    nodes = [_bridge(index, f"repo_bridge:{index}",
                     provenance=f"declaration {index}",
                     evidence=[f"src/{index}.py"])
             for index in range(repo_relations.MAX_BRIDGE_EVIDENCE + 2)]
    members = [_member("bigville", nodes), _member("ocelot", [])]
    report = _analyze(tmp_path, members)

    assert report["bridges"]["state"] == "partial"
    assert report["bridges"]["truncated"] is True
    assert report["truncated"] is True
    assert report["bridges"]["counts"]["nodes"] == 12
    assert report["bridges"]["counts"]["edges"] == 1
    edge = report["bridges"]["edges"][0]
    sorted_provenance = sorted(f"declaration {index}" for index in range(12))
    sorted_evidence = sorted(f"src/{index}.py" for index in range(12))
    assert edge["provenance"] == sorted_provenance[
        :repo_relations.MAX_BRIDGE_PROVENANCE]
    assert edge["evidence"] == sorted_evidence[
        :repo_relations.MAX_BRIDGE_EVIDENCE]
    assert len(edge["provenance"]) == 10
    assert len(edge["evidence"]) == 10
    assert report["bridges"]["errors"] == []
    assert len(report["bridges"]["notes"]) == 2
    assert any("provenance list truncated from 12 to 10" in note
               for note in report["bridges"]["notes"])
    assert any("evidence list truncated from 12 to 10" in note
               for note in report["bridges"]["notes"])
    assert report["notes"] == report["bridges"]["notes"]

    # Reversing declaration order must keep the same sorted first ten entries,
    # notes and edge shape.
    reversed_report = _analyze(
        tmp_path, [_member("bigville", list(reversed(nodes))),
                   _member("ocelot", [])])
    assert reversed_report["bridges"]["edges"] == report["bridges"]["edges"]
    assert reversed_report["bridges"]["notes"] == report["bridges"]["notes"]
    assert reversed_report["notes"] == report["notes"]


def test_mutual_bridges_do_not_create_a_dependency_cycle(tmp_path):
    report = _analyze(tmp_path, [
        _member("bigville", [_bridge(1, "repo_bridge:forward")]),
        _member("ocelot", [_bridge(
            1, "repo_bridge:reverse", source_repo="ocelot",
            target_repo="bigville", relation="protocol",
            provenance="Ocelot exposes the adapter protocol Bigville loads",
            evidence=["worlds/bigville_ocelot_adapter.py"])]),
    ])

    assert report["bridges"]["state"] == "ok"
    assert [(edge["source"], edge["target"], edge["relation"])
            for edge in report["bridges"]["edges"]] == [
        ("bigville", "ocelot", "dynamic_import"),
        ("ocelot", "bigville", "protocol"),
    ]
    assert report["dependencies"] == []
    assert report["cycles"] == []
    assert set(report["order"]) == {"bigville", "ocelot"}
    assert report["levels"] == {"bigville": 0, "ocelot": 0}
    assert report["reverse_dependencies"] == {"bigville": [], "ocelot": []}


def test_cli_relations_output_includes_bridge_report(tmp_path, monkeypatch,
                                                     capsys):
    anchor, member = tmp_path / "bigville", tmp_path / "ocelot"
    _snapshot(anchor, "bigville", [_bridge(1, "repo_bridge:bigville")])
    _snapshot(member, "ocelot")
    _manifest(anchor, [{"path": str(member), "id": "ocelot"}])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    assert main(["relations", "--root", str(anchor), "--no-sync"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["bridges"]["state"] == "ok"
    assert output["bridges"]["edges"][0]["origin"] == "adapter_bridge"
    assert output["edges"][0]["origin"] == "adapter_bridge"
    assert output["counts"]["bridge_edges"] == 1
