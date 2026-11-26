"""Tests for the deterministic, read-only surface inventory."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from ozagia.core import contracts as C
from ozagia.core import inventory as I

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = json.loads(
    (REPO / "tests" / "fixtures" / "selfgraph" / "scenarios.json").read_text(
        encoding="utf-8"))["scenarios"]


def test_inventory_is_deterministic_in_process(tmp_path):
    first = I.collect_inventory(tmp_path)
    second = I.collect_inventory(tmp_path)
    assert first == second
    assert I.inventory_hash(first) == I.inventory_hash(second)
    assert I.inventory_hash(first) == C.content_hash(first)


def test_inventory_is_deterministic_across_hash_seeds():
    script = (
        "from ozagia.core.inventory import collect_inventory, inventory_hash;"
        "print(inventory_hash(collect_inventory()))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env["PYTHONHASHSEED"] = "31337"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess_hash = completed.stdout.strip()
    assert subprocess_hash == I.inventory_hash(I.collect_inventory())


def test_cli_mcp_settings_and_entrypoints_are_present():
    report = I.collect_inventory()
    counts = report["counts"]["by_kind"]
    assert counts["cli"] > 0
    assert counts["mcp"] > 0
    assert counts["settings"] > 0
    assert counts["entrypoint"] > 0
    assert counts["daemon"] > 0
    assert report["coverage"]["exhaustive"] is False
    assert report["schema"]["version"] == I.SCHEMA_VERSION

    identifiers = [surface["id"] for surface in report["surfaces"]]
    assert len(identifiers) == len(set(identifiers))
    for surface in report["surfaces"]:
        assert surface["kind"] in I.SURFACE_KINDS
        assert surface["coverage"] in (I.COVERAGE_OBSERVED,
                                      I.COVERAGE_UNKNOWN)
        assert isinstance(surface["declared"], bool)
        assert isinstance(surface["observed"], bool)
        assert "source" in surface and "version" in surface
        assert C.canonical_json(surface)

    for kind in ("cli", "settings"):
        runtime = [surface for surface in report["surfaces"]
                   if surface["kind"] == kind]
        assert runtime
        assert all(surface["details"]["observation"] == "runtime"
                   for surface in runtime)
        assert all(surface["coverage"] == "observed" for surface in runtime)


def test_graph_mcp_surface_is_present_and_distinct():
    report = I.collect_inventory()
    graph = {surface["id"]: surface for surface in report["surfaces"]
             if surface["id"].startswith("mcp:graph:")}
    assert {surface["name"] for surface in graph.values()} == {
        "find", "node", "expand", "impact", "edit", "create", "check", "note"}
    assert all(surface["details"]["surface"] == "graph"
               for surface in graph.values())
    assert all(surface["details"]["observation"] == "static-ast"
               for surface in graph.values())
    assert all(surface["coverage"] == "unknown"
               for surface in graph.values())
    tools = [surface for surface in report["surfaces"]
             if surface["id"].startswith("mcp:tool:")]
    full = [surface for surface in tools
            if surface["details"]["surface"] == "full"]
    engine = [surface for surface in tools
              if surface["details"]["surface"] == "engine"]
    assert full
    assert {surface["name"] for surface in engine} == {"graph"}
    assert all(surface["details"]["observation"] == "static-ast"
               for surface in full + engine)
    assert all(surface["coverage"] == "unknown" for surface in full + engine)
    assert not ({surface["id"] for surface in full}
                & {surface["id"] for surface in graph.values()})
    assert not ({surface["id"] for surface in full}
                & {surface["id"] for surface in engine})


def test_mcp_cli_names_are_non_empty_and_stable():
    report = I.collect_inventory()
    cli_names = {surface["name"] for surface in report["surfaces"]
                 if surface["kind"] == "cli"}
    mcp_names = {surface["name"] for surface in report["surfaces"]
                 if surface["kind"] == "mcp"}
    assert "ozagia" in cli_names
    assert any(name.startswith("ozagia goal") for name in cli_names)
    assert "dev_context" in mcp_names


def test_synthetic_observed_but_undeclared_mcp_handler_is_a_gap(tmp_path):
    report = I.collect_inventory(
        tmp_path,
        mcp_tools=[{"name": "dev_context", "parameters": ["task"]},
                   {"name": "dev_declared_only"}],
        mcp_handler_keys=["dev_context", "dev_observed_only"],
    )
    missing = {gap["name"] for gap in report["gaps"]["declared_not_observed"]}
    undeclared = {gap["name"]
                  for gap in report["gaps"]["observed_not_declared"]}
    assert "dev_declared_only" in missing
    assert "dev_observed_only" in undeclared
    surfaces = {surface["id"]: surface for surface in report["surfaces"]
                if surface["kind"] == "mcp"}
    assert surfaces["mcp:tool:dev_observed_only"]["declared"] is False
    assert surfaces["mcp:tool:dev_observed_only"]["observed"] is True
    assert surfaces["mcp:tool:dev_observed_only"]["coverage"] == "unknown"
    assert surfaces["mcp:tool:dev_observed_only"]["details"] == {
        "surface": "full", "observation": "static-ast", "parameters": []}
    assert surfaces["mcp:tool:dev_declared_only"]["observed"] is False
    assert surfaces["mcp:tool:dev_declared_only"]["coverage"] == "unknown"


def _write_marker_adapter(root, module_name, marker):
    (root / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "def register(registry):\n"
        "    pass\n",
        encoding="utf-8",
    )


def test_default_inventory_never_imports_target_adapters(tmp_path):
    module_name = "wp0_marker_adapter"
    ref = f"{module_name}:register"
    marker = tmp_path / "wp0-imported.marker"
    _write_marker_adapter(tmp_path, module_name, marker)
    (tmp_path / ".ozagia.toml").write_text(
        "[adapters]\n"
        f'modules = ["{ref}"]\n',
        encoding="utf-8",
    )
    sys.modules.pop(module_name, None)
    report = I.collect_inventory(tmp_path)
    assert module_name not in sys.modules
    assert not marker.exists()
    surface = next(surface for surface in report["surfaces"]
                   if surface["id"] == f"adapter:declared:{ref}")
    assert surface["declared"] is True
    assert surface["observed"] is False
    assert surface["coverage"] == "unknown"
    assert surface["details"]["imported"] is False
    assert surface["details"]["surface"] == "declared-module"
    assert surface["details"]["source"].endswith(".ozagia.toml")
    assert not any(surface["id"] == "adapter:registry"
                   for surface in report["surfaces"])
    gaps = {gap["id"] for gap in report["gaps"]["unverified"]
            if gap["kind"] == "adapter"}
    assert f"adapter:declared:{ref}" in gaps


def test_declared_adapters_are_listed_without_importing(tmp_path):
    refs = ["wp0_declared_a:register", "wp0_declared_b:register"]
    markers = {}
    for ref in refs:
        module_name = ref.split(":", 1)[0]
        marker = tmp_path / f"{module_name}.marker"
        markers[module_name] = marker
        _write_marker_adapter(tmp_path, module_name, marker)
        sys.modules.pop(module_name, None)
    (tmp_path / ".ozagia.toml").write_text(
        "[adapters]\n"
        "modules = [" + ", ".join(f'"{ref}"' for ref in refs) + "]\n",
        encoding="utf-8",
    )
    report = I.collect_inventory(tmp_path)
    declared = {surface["id"]: surface for surface in report["surfaces"]
                if surface["id"].startswith("adapter:declared:")}
    assert set(declared) == {f"adapter:declared:{ref}" for ref in refs}
    for ref in refs:
        surface = declared[f"adapter:declared:{ref}"]
        assert surface["name"] == ref
        assert surface["declared"] is True
        assert surface["observed"] is False
        assert surface["coverage"] == "unknown"
        assert surface["details"]["imported"] is False
        assert surface["details"]["surface"] == "declared-module"
        assert surface["details"]["source"].endswith(".ozagia.toml")
        module_name = ref.split(":", 1)[0]
        assert module_name not in sys.modules
        assert not markers[module_name].exists()
    gaps = {gap["id"] for gap in report["gaps"]["unverified"]
            if gap["kind"] == "adapter"}
    assert {f"adapter:declared:{ref}" for ref in refs} <= gaps


def test_invalid_adapter_config_is_reported_without_raising(tmp_path):
    (tmp_path / ".ozagia.toml").write_text(
        "[adapters\nmodules = [", encoding="utf-8")
    report = I.collect_inventory(tmp_path)
    surface = next(surface for surface in report["surfaces"]
                   if surface["id"] == "adapter:registry")
    assert surface["declared"] is False
    assert surface["observed"] is False
    assert surface["coverage"] == "unknown"
    assert surface["details"]["imported"] is False
    gaps = [gap for gap in report["gaps"]["unverified"]
            if gap["id"] == "adapter:config"]
    assert len(gaps) == 1
    assert gaps[0]["kind"] == "adapter"
    assert "cannot read adapter declarations" in gaps[0]["reason"]


def test_no_root_keeps_the_blanket_adapter_registry():
    report = I.collect_inventory()
    adapters = [surface for surface in report["surfaces"]
                if surface["kind"] == "adapter"]
    assert [surface["id"] for surface in adapters] == ["adapter:registry"]
    assert adapters[0]["declared"] is False
    assert adapters[0]["observed"] is False
    assert adapters[0]["coverage"] == "unknown"
    assert adapters[0]["details"]["imported"] is False


def test_adapter_surfaces_are_unknown_coverage(tmp_path):
    observation = {
        "loaded": ["demo_adapter"],
        "errors": ["bad_adapter: ImportError: missing"],
        "mcp_tools": {"demo_tool": ({}, lambda args: None)},
        "graph_queries": {"demo_query": lambda memory: None},
        "edit_engines": {"demo_engine": (frozenset(), lambda: None)},
        "indexers": [],
    }
    (tmp_path / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["ignored_declaration:register"]\n',
        encoding="utf-8",
    )
    report = I.collect_inventory(
        tmp_path, adapter_registry_result=observation)
    adapters = [surface for surface in report["surfaces"]
                if surface["kind"] == "adapter"]
    assert adapters
    assert not any(surface["id"].startswith("adapter:declared:")
                   for surface in adapters)
    assert all(surface["coverage"] == "unknown" for surface in adapters)
    assert all(surface["declared"] is True for surface in adapters)
    assert all(surface["observed"] is True for surface in adapters)
    unverified = {gap["id"] for gap in report["gaps"]["unverified"]
                  if gap["kind"] == "adapter"}
    assert "adapter:module:demo_adapter" in unverified
    assert "adapter:mcp:demo_tool" in unverified
    assert "adapter:query:demo_query" in unverified
    assert "adapter:engine:demo_engine" in unverified
    assert any(gap["name"] == "adapter-error"
               for gap in report["gaps"]["unverified"])


def test_daemon_surfaces_are_declared_but_not_cross_checked():
    report = I.collect_inventory()
    daemon = [surface for surface in report["surfaces"]
              if surface["kind"] == "daemon"]
    assert daemon
    assert all(surface["coverage"] == "unknown" for surface in daemon)
    assert all(surface["declared"] is True for surface in daemon)
    assert all(surface["observed"] is False for surface in daemon)
    assert {surface["version"] for surface in daemon} == {"1"}
    unverified = {gap["id"] for gap in report["gaps"]["unverified"]
                  if gap["kind"] == "daemon"}
    assert {surface["id"] for surface in daemon} <= unverified


def test_inventory_does_not_write_run_subprocesses_or_open_sockets(
        tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("inventory must not do this")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    report = I.collect_inventory(tmp_path)
    after = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert before == after
    assert report["counts"]["surfaces"] > 0


def test_scenario_manifest_uses_the_frozen_vocabulary():
    identifiers = [scenario["id"] for scenario in SCENARIOS]
    assert len(identifiers) == len(set(identifiers))
    assert len(identifiers) >= 7
    assert "ozagia-inventory" in identifiers
    for scenario in SCENARIOS:
        expected = scenario["expected"]
        if "freshness" in expected:
            C.freshness(expected["freshness"])
        if "result" in expected:
            C.result(expected["result"])
        if "evidence" in expected:
            C.evidence(expected["evidence"])
        if "joined_freshness" in expected:
            C.freshness(expected["joined_freshness"])
        if "readiness" in expected:
            C.readiness_map(expected["readiness"])


def test_inventory_against_a_missing_or_empty_root_is_still_self_only(tmp_path):
    report = I.collect_inventory(tmp_path)
    assert report["tool"]["name"] == "ozagia"
    adapters = [surface for surface in report["surfaces"]
                if surface["kind"] == "adapter"]
    assert [surface["id"] for surface in adapters] == ["adapter:registry"]
    assert adapters[0]["declared"] is False
    assert adapters[0]["observed"] is False
    assert adapters[0]["coverage"] == "unknown"
    assert report["coverage"]["exhaustive"] is False
