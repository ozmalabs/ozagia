"""Tests for the read-only capability self-graph projection."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import capabilities as K
from ozagia.core import contracts as C
from ozagia.core import self_graph as SG
from ozagia.core.memory import DevMemory, build_index
from ozagia.daemon import protocol
from ozagia.integrations.mcp import graph_surface, server
from ozagia.integrations.mcp.server import DevMemoryServer

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _surface(surface_id: str, *, kind: str | None = None,
             source: str = "tests/fixture.py") -> dict:
    return {
        "id": surface_id,
        "kind": kind or surface_id.split(":", 1)[0],
        "name": surface_id.split(":")[-1],
        "source": source,
        "version": "1",
        "declared": True,
        "observed": True,
        "coverage": "unknown",
        "origin": "inventory",
        "details": {},
    }


def _observations(surfaces=(), *, gaps=None, inventory="observed",
                  error=None, daemon_operations=()) -> dict:
    if gaps is None:
        gaps = {"declared_not_observed": [], "observed_not_declared": [],
                "unverified": []}
    emitters = {
        "inventory": {
            "state": inventory, "error": error, "hash": "0" * 64,
            "counts": {}, "coverage": {},
        },
        "toolchain": {
            "state": "observed", "error": None, "hash": "1" * 64,
            "registry_version": "fixture", "capabilities": [], "tools": [],
        },
        "status": {"state": "observed", "error": None, "sections": []},
        "daemon": {
            "state": "observed", "error": None, "hash": "2" * 64,
            "operations": list(daemon_operations),
            "inventory_missing": [], "inventory_extra": [],
        },
    }
    states = [emitters[name]["state"] for name in emitters]
    return {
        "schema": {"namespace": SG.OBSERVATION_NAMESPACE, "version": "1.0"},
        "observation_version": "1.0",
        "state": ("unknown" if all(state == "unknown" for state in states)
                  else "partial" if any(state == "unknown" for state in states)
                  else "current"),
        "sources": {},
        "surfaces": list(surfaces),
        "gaps": gaps,
        "errors": [],
        "emitters": emitters,
    }


def _record(surface: str, *, cap_id: str = "cap:fixture.inspect",
            exposure=None) -> dict:
    prefix = surface.split(":", 1)[0]
    if exposure is None:
        exposure = [prefix] if prefix in ("cli", "mcp", "daemon") else ["internal"]
    return {
        "id": cap_id,
        "domain": "status",
        "purpose": "A fixture capability.",
        "owner": "ozagia.core.self_graph",
        "side_effect": "read",
        "exposure": exposure,
        "impl": ["impl:ozagia.core.self_graph.build_self_graph"],
        "surfaces": [surface],
        "prerequisites": [],
        "limits": [],
        "authorization": "none: read-only",
        "tests": ["test:tests/test_self_graph.py"],
        "docs": ["doc:docs/capabilities.md"],
    }


def test_real_surfaces_are_joined_without_guessing():
    report = SG.build_self_graph()
    caps = K.catalogue()
    declared = {surface for cap in caps for surface in cap.surfaces}
    full = {f"mcp:tool:{tool['name']}" for tool in server._TOOLS}
    graph = {f"mcp:graph:{tool['name']}" for tool in graph_surface.GRAPH_TOOLS}
    parser = cli.build_parser()
    sub = next(action for action in parser._actions
               if hasattr(action, "choices") and action.choices)
    commands = {f"cli:ozagia.{name}" for name in sub.choices}
    daemon = {f"daemon:op:{operation}" for operation in protocol.OPERATIONS}
    assert {surface for surface in declared if surface.startswith("mcp:")} == (
        full | graph)
    assert {surface for surface in declared if surface.startswith("cli:")} == (
        commands)
    assert {surface for surface in declared
            if surface.startswith("daemon:")} == daemon
    assert "toolchain:registry" in declared
    assert report["audit"]["counts"]["surface_states"] == {
        "observed": len(declared)}
    assert report["audit"]["counts"]["undeclared_surfaces"] == 0
    implementations = report["audit"]["counts"]["implementations"]
    references = report["audit"]["counts"]["references"]
    assert implementations.get("missing", 0) == 0
    assert implementations.get("unknown", 0) == 0
    assert references.get("missing", 0) == 0
    assert references.get("unknown", 0) == 0
    assert implementations.get("observed", 0) > 50
    assert references.get("observed", 0) > 50
    kinds = report["audit"]["counts"]["findings_by_kind"]
    assert "declared_surface_not_observed" not in kinds
    assert "observed_surface_undeclared" not in kinds
    assert "implementation_missing" not in kinds
    assert "test_ref_missing" not in kinds
    assert "doc_ref_missing" not in kinds
    assert report["audit"]["counts"]["findings_by_severity"].keys() <= {
        "coverage"}
    assert report["observations"]["state"] == "current"
    assert report["audit"]["state"] == "current"


def test_projection_is_sorted_unique_and_hashed():
    first = SG.build_self_graph()
    second = SG.build_self_graph()
    assert first["graph"] == second["graph"]
    assert first["audit"] == second["audit"]
    nodes = first["graph"]["nodes"]
    edges = first["graph"]["edges"]
    keys = [(node["kind"], node["id"]) for node in nodes]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(nodes)
    assert len({(edge["type"], edge["from"], edge["to"])
                for edge in edges}) == len(edges)
    for node in nodes:
        assert node["kind"] in SG.NODE_KINDS
        assert node["state"] in SG.EDGE_STATES
        assert node["provenance"]
    for edge in edges:
        assert edge["type"] in SG.EDGE_TYPES
        assert edge["state"] in SG.EDGE_STATES
        assert edge["freshness"] in C.FRESHNESS
        assert edge["evidence"] in C.EVIDENCE
        assert edge["provenance"]
    body = {"nodes": nodes, "edges": edges}
    assert first["graph"]["hash"] == C.content_hash(body)
    assert SG.self_graph_hash(first) == first["graph"]["hash"]
    assert first["catalogue"]["hash"] == K.catalogue_hash()
    assert C.canonical_json(first)
    assert first["audit"]["hash"] == C.content_hash({
        "state": first["audit"]["state"],
        "findings": first["audit"]["findings"],
        "counts": first["audit"]["counts"],
    })
    assert first["coverage"]["exhaustive"] is False


def test_declared_surface_missing_is_a_finding_with_provenance():
    capability = K.build_catalogue([_record("mcp:tool:dev_absent")])
    observations = _observations()
    report = SG.build_self_graph(capabilities=capability,
                                 observations=observations)
    state = next(row for row in report["surface_states"]
                 if row["surface"] == "mcp:tool:dev_absent")
    assert state["state"] == "missing"
    finding = next(item for item in report["audit"]["findings"]
                   if item["subject"] == "mcp:tool:dev_absent")
    assert finding["kind"] == "declared_surface_not_observed"
    assert finding["severity"] == "error"
    assert finding["state"] == "missing"
    assert finding["provenance"]["capability"] == "cap:fixture.inspect"
    assert finding["provenance"]["capabilities"] == ["cap:fixture.inspect"]
    assert finding["provenance"]["observation"] == "inventory"
    assert report["audit"]["state"] == "current"


def test_observed_surface_without_declaration_is_a_finding_with_provenance():
    capability = K.build_catalogue([_record("mcp:tool:dev_known")])
    observations = _observations([
        _surface("mcp:tool:dev_known"),
        _surface("mcp:tool:dev_ghost", source="tests/ghost_surface.py"),
    ])
    report = SG.build_self_graph(capabilities=capability,
                                 observations=observations)
    finding = next(item for item in report["audit"]["findings"]
                   if item["subject"] == "mcp:tool:dev_ghost")
    assert finding["kind"] == "observed_surface_undeclared"
    assert finding["severity"] == "coverage"
    assert finding["state"] == "undeclared"
    assert finding["provenance"]["observation"] == "inventory"
    assert finding["provenance"]["source"] == "tests/ghost_surface.py"
    assert finding["provenance"]["surface_kind"] == "mcp"
    assert report["audit"]["counts"]["undeclared_surfaces"] == 1
    assert not any(item["subject"] == "mcp:tool:dev_known"
                   for item in report["audit"]["findings"])


def test_unreadable_handler_map_is_unknown_never_missing():
    capability = K.build_catalogue([_record("mcp:tool:dev_tool")])
    gaps = {
        "declared_not_observed": [{
            "id": "mcp:tool:dev_tool", "kind": "mcp", "name": "dev_tool",
            "reason": "declared in full tools but absent from the static "
                      "handler map",
        }],
        "observed_not_declared": [],
        "unverified": [{
            "id": "mcp:handlers", "kind": "mcp", "name": "handler-map",
            "reason": "handler map could not be read: OSError: unreadable",
        }],
    }
    report = SG.build_self_graph(
        capabilities=capability,
        observations=_observations(gaps=gaps))
    state = next(row for row in report["surface_states"]
                 if row["surface"] == "mcp:tool:dev_tool")
    assert state["state"] == "unknown"
    finding = next(item for item in report["audit"]["findings"]
                   if item["subject"] == "mcp:tool:dev_tool")
    assert finding["kind"] == "declared_surface_unknown"
    assert finding["severity"] == "unknown"
    assert "handler map could not be read" in finding["reason"]
    assert report["audit"]["state"] == "partial"
    assert not any(item["kind"] == "declared_surface_not_observed"
                   for item in report["audit"]["findings"])


def test_unreadable_inventory_survives_and_keeps_partial_state(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("inventory is unreadable")

    monkeypatch.setattr(SG.I, "collect_inventory", boom)
    report = SG.build_self_graph()
    assert report["observations"]["emitters"]["inventory"]["state"] == "unknown"
    assert report["observations"]["state"] == "partial"
    assert report["audit"]["state"] == "partial"
    assert any(item["kind"] == "observation_unavailable"
               for item in report["audit"]["findings"])
    assert any(item["kind"] == "declared_surface_unknown"
               for item in report["audit"]["findings"])
    assert report["graph"]["counts"]["nodes"] > 0
    assert report["graph"]["counts"]["edges"] > 0


def test_real_unreadable_handler_map_is_unknown(monkeypatch):
    def broken(path):
        return [], "handler map could not be read: fixture"

    monkeypatch.setattr(SG.I, "_handler_keys_from_ast", broken)
    report = SG.build_self_graph()
    assert report["observations"]["emitters"]["inventory"]["state"] == "observed"
    states = {row["surface"]: row["state"]
              for row in report["surface_states"]}
    assert states["mcp:tool:dev_context"] == "unknown"
    assert states["mcp:graph:find"] == "observed"
    assert any(item["kind"] == "declared_surface_unknown"
               and item["subject"] == "mcp:tool:dev_context"
               for item in report["audit"]["findings"])
    assert report["audit"]["state"] == "partial"


def test_daemon_operation_omitted_from_inventory_is_synthesized(monkeypatch):
    original = SG._inventory_observation

    def without_hint(root):
        observation = original(root)
        observation["surfaces"] = [
            item for item in observation["surfaces"]
            if item.get("id") != "daemon:op:hint"]
        return observation

    monkeypatch.setattr(SG, "_inventory_observation", without_hint)
    observations = SG.collect_observations()
    surfaces = {surface["id"]: surface
                for surface in observations["surfaces"]}
    hint = surfaces["daemon:op:hint"]
    assert hint["kind"] == "daemon"
    assert hint["observed"] is True
    assert hint["coverage"] == "unknown"
    assert hint["details"]["documented"] is False
    assert hint["details"]["cross_checked"] is True
    assert "hint" in observations["emitters"]["daemon"]["operations"]
    assert "hint" in observations["emitters"]["daemon"]["inventory_missing"]

    report = SG.build_self_graph()
    state = next(row for row in report["surface_states"]
                 if row["surface"] == "daemon:op:hint")
    assert state["state"] == "observed"
    finding = next(item for item in report["audit"]["findings"]
                   if item["subject"] == "daemon:op:hint")
    assert finding["kind"] == "daemon_operation_undocumented"
    assert finding["severity"] == "coverage"


def test_ast_symbol_cache_tracks_source_content(tmp_path):
    source = tmp_path / "symbols.py"
    source.write_text("def before():\n    pass\n", encoding="utf-8")
    assert SG._ast_symbols(source) == ({"before"}, None)
    source.write_text("def after():\n    pass\n", encoding="utf-8")
    assert SG._ast_symbols(source) == ({"after"}, None)


def test_ast_symbol_cache_bounds_concurrent_parse_errors(tmp_path):
    paths = []
    for index in range(SG.MAX_AST_SYMBOL_CACHE + 32):
        path = tmp_path / f"invalid_{index}.py"
        path.write_text("def broken(:\n", encoding="utf-8")
        paths.append(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(SG._ast_symbols, paths))
    assert all(names == set() and error.startswith("SyntaxError:")
               for names, error in results)
    with SG._AST_SYMBOL_CACHE_LOCK:
        assert len(SG._AST_SYMBOL_CACHE) <= SG.MAX_AST_SYMBOL_CACHE


def test_all_real_daemon_operations_are_documented_and_cross_checked():
    observations = SG.collect_observations()
    surfaces = {surface["id"]: surface
                for surface in observations["surfaces"]}
    for operation in protocol.OPERATIONS:
        surface = surfaces[f"daemon:op:{operation}"]
        assert surface["details"]["documented"] is True
        assert surface["details"]["cross_checked"] is True
    assert observations["emitters"]["daemon"]["inventory_missing"] == []
    assert not any(item["kind"] == "daemon_operation_undocumented"
                   for item in SG.build_self_graph()["audit"]["findings"])


def test_inventory_gap_directions_are_reported():
    capability = K.build_catalogue([_record("mcp:tool:dev_known")])
    gaps = {
        "declared_not_observed": [{
            "id": "mcp:tool:dev_forgotten", "kind": "mcp",
            "name": "dev_forgotten",
            "reason": "declared in full tools but absent from the static "
                      "handler map",
        }],
        "observed_not_declared": [{
            "id": "mcp:tool:dev_handler_only", "kind": "mcp",
            "name": "dev_handler_only",
            "reason": "handler present in full but no declared tool entry",
        }],
        "unverified": [],
    }
    report = SG.build_self_graph(
        capabilities=capability,
        observations=_observations([_surface("mcp:tool:dev_known")],
                                   gaps=gaps))
    kinds = {item["kind"]: item for item in report["audit"]["findings"]}
    assert kinds["tool_without_handler"]["subject"] == "mcp:tool:dev_forgotten"
    assert kinds["handler_without_tool_declaration"]["subject"] == (
        "mcp:tool:dev_handler_only")
    assert kinds["tool_without_handler"]["provenance"]["observation"] == (
        "inventory-gap")
    assert kinds["handler_without_tool_declaration"]["severity"] == "error"


def test_tool_without_handler_is_reported_even_when_capability_declares_it():
    surface_id = "mcp:tool:dev_missing_handler"
    capability = K.build_catalogue([_record(surface_id)])
    gaps = {
        "declared_not_observed": [{
            "id": surface_id, "kind": "mcp", "name": "dev_missing_handler",
            "reason": "tool schema has no handler",
        }],
        "observed_not_declared": [],
        "unverified": [],
    }
    report = SG.build_self_graph(
        capabilities=capability,
        observations=_observations([], gaps=gaps))
    findings = report["audit"]["findings"]
    assert any(item["kind"] == "declared_surface_not_observed"
               and item["subject"] == surface_id for item in findings)
    tool_gap = next(item for item in findings
                    if item["kind"] == "tool_without_handler"
                    and item["subject"] == surface_id)
    assert tool_gap["severity"] == "error"
    assert tool_gap["provenance"]["observation"] == "inventory-gap"


def test_duplicate_surface_declarations_are_reported():
    first = _record("mcp:tool:dev_known", cap_id="cap:fixture.one")
    second = _record("mcp:tool:dev_known", cap_id="cap:fixture.two")
    capability = K.build_catalogue([first, second])
    report = SG.build_self_graph(
        capabilities=capability,
        observations=_observations([_surface("mcp:tool:dev_known")]))
    finding = next(item for item in report["audit"]["findings"]
                   if item["kind"] == "duplicate_surface_declaration")
    assert finding["subject"] == "mcp:tool:dev_known"
    assert finding["provenance"]["capabilities"] == [
        "cap:fixture.one", "cap:fixture.two"]
    assert report["duplicate_surfaces"] == {
        "mcp:tool:dev_known": ["cap:fixture.one", "cap:fixture.two"]}


def test_list_show_and_audit_views_are_bounded_and_paginated():
    first = SG.capability_list(limit=2)
    assert first["action"] == "list"
    assert len(first["items"]) == 2
    assert first["truncation"]["truncated"] is True
    cursor = first["truncation"]["next_cursor"]
    assert cursor
    second = SG.capability_list(limit=2, cursor=cursor)
    assert {item["id"] for item in first["items"]} & {
        item["id"] for item in second["items"]} == set()
    assert second["truncation"]["next_cursor"] == str(
        int(cursor) + 2)

    show = SG.capability_show("cap:graph.context", limit=2)
    assert show["capability"]["id"] == "cap:graph.context"
    assert len(show["edges"]) == 2
    assert show["truncation"]["truncated"] is True
    assert all("cap:graph.context" in edge["via"] or
               "cap:graph.context" in (edge["from"], edge["to"])
               for edge in show["edges"])

    audit = SG.audit_self_graph(limit=1)
    assert audit["action"] == "audit"
    assert audit["findings"] == []
    assert audit["truncation"]["truncated"] is False
    assert audit["counts"]["findings"] == 0


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": -1}, {"limit": 201}, {"limit": True},
    {"limit": "3"}, {"cursor": "x"}, {"cursor": ""}, {"cursor": "9" * 65},
])
def test_bounds_and_cursors_reject_invalid_values(kwargs):
    with pytest.raises(SG.SelfGraphError):
        SG.capability_list(**kwargs)


def test_cli_list_show_and_audit(capsys):
    assert cli.main(["capabilities", "list", "--json", "--limit", "3"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["action"] == "list"
    assert len(listed["items"]) == 3
    assert listed["truncation"]["truncated"] is True

    assert cli.main(["capabilities", "show", "cap:graph.context",
                     "--json", "--limit", "2"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["capability"]["id"] == "cap:graph.context"
    assert shown["truncation"]["truncated"] is True

    assert cli.main(["capabilities", "audit", "--json", "--limit", "1"]) == 0
    audited = json.loads(capsys.readouterr().out)
    assert audited["action"] == "audit"
    assert audited["findings"] == []

    assert cli.main(["capabilities", "list"]) == 0
    text = capsys.readouterr().out
    assert "cap:graph.context" in text or "cap:actions.iac" in text

    assert cli.main(["capabilities", "show", "cap:not.real", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["ok"] is False
    assert "unknown capability" in error["error"]

    assert cli.main(["capabilities", "audit", "--limit", "999"]) == 2
    assert "limit" in capsys.readouterr().err


def _fixture_memory(tmp_path: Path) -> DevMemory:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    graph = tmp_path / "graph.json"
    build_index(root, graph, include_docs="none", include_git=False,
                include_scripts=False)
    return DevMemory(graph, tmp_path / "journal.jsonl", root=root)


def test_mcp_dev_capabilities_is_listed_and_bounded(tmp_path):
    mem = _fixture_memory(tmp_path)
    server_instance = DevMemoryServer(mem)
    listed = server_instance.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert "dev_capabilities" in tools
    schema = tools["dev_capabilities"]["inputSchema"]
    assert set(schema["properties"]) == {"action", "id", "limit", "cursor"}
    assert schema["properties"]["action"]["enum"] == ["list", "show", "audit"]
    assert schema["properties"]["limit"]["default"] == 25

    before = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*"))
    listed_call = server_instance.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "dev_capabilities",
                   "arguments": {"action": "list", "limit": 2}}})
    assert listed_call["result"]["isError"] is False
    payload = json.loads(listed_call["result"]["content"][0]["text"])
    assert payload["action"] == "list"
    assert len(payload["items"]) == 2
    assert payload["truncation"]["truncated"] is True

    audited = server_instance.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "dev_capabilities",
                   "arguments": {"action": "audit", "limit": 1}}})
    audit_payload = json.loads(audited["result"]["content"][0]["text"])
    assert audit_payload["action"] == "audit"
    assert audit_payload["findings"] == []

    shown = server_instance.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "dev_capabilities",
                   "arguments": {"action": "show", "id": "cap:graph.context"}}})
    shown_payload = json.loads(shown["result"]["content"][0]["text"])
    assert shown_payload["capability"]["id"] == "cap:graph.context"

    missing = server_instance.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "dev_capabilities",
                   "arguments": {"action": "show", "id": "cap:not.real"}}})
    missing_payload = json.loads(missing["result"]["content"][0]["text"])
    assert missing_payload["ok"] is False
    assert "unknown capability" in missing_payload["error"]

    after = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*"))
    assert before == after


def test_no_probe_no_socket_no_write(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("the self-graph must not probe or write")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    marker = tmp_path / "marker"
    marker.write_text("unchanged", encoding="utf-8")
    report = SG.build_self_graph()
    assert report["catalogue"]["capabilities"] > 0
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["marker"]


def test_graph_hash_is_stable_across_hash_seeds():
    script = (
        "from ozagia.core.self_graph import build_self_graph;"
        "r=build_self_graph();"
        "print(r['catalogue']['hash'], r['graph']['hash'], "
        "r['audit']['hash'], r['observations']['hash'])"
    )
    outputs = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO, env=env, check=True,
            capture_output=True, text=True)
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def test_cli_audit_bytes_are_stable_across_hash_seeds():
    outputs = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "ozagia.cli", "capabilities", "audit",
             "--json"],
            cwd=REPO, env=env, check=True, capture_output=True, text=True)
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]
    payload = json.loads(outputs[0])
    assert payload["action"] == "audit"
    assert payload["schema"]["namespace"] == SG.SCHEMA_NAMESPACE


def test_self_graph_does_not_import_declared_target_adapters(tmp_path):
    module_name = "wp1_marker_adapter"
    marker = tmp_path / "wp1-imported.marker"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "def register(registry):\n"
        "    pass\n",
        encoding="utf-8",
    )
    ref = f"{module_name}:register"
    (tmp_path / ".ozagia.toml").write_text(
        "[adapters]\n"
        f'modules = ["{ref}"]\n',
        encoding="utf-8",
    )
    sys.modules.pop(module_name, None)
    report = SG.build_self_graph(root=tmp_path)
    assert module_name not in sys.modules
    assert not marker.exists()
    surface = next(item for item in report["observations"]["surfaces"]
                   if item["id"] == f"adapter:declared:{ref}")
    assert surface["details"]["imported"] is False
    assert surface["coverage"] == "unknown"
    assert not any(item["subject"] == f"adapter:declared:{ref}"
                   for item in report["audit"]["findings"])
