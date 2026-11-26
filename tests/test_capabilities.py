"""Tests for the explicit capability catalogue."""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import capabilities as K
from ozagia.core import contracts as C
from ozagia.core import graph_engine
from ozagia.daemon import jobs, protocol
from ozagia.integrations.mcp import graph_surface, server

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _base_record() -> dict:
    return {
        "id": "cap:fixture.check",
        "domain": "status",
        "purpose": "A fixture capability.",
        "owner": "ozagia.core.self_graph",
        "side_effect": "read",
        "exposure": ["mcp"],
        "impl": ["impl:ozagia.core.self_graph.build_self_graph"],
        "surfaces": ["mcp:tool:dev_capabilities"],
        "prerequisites": [],
        "limits": ["bounded"],
        "authorization": "none: read-only",
        "tests": ["test:tests/test_capabilities.py"],
        "docs": ["doc:docs/capabilities.md"],
    }


def test_catalogue_is_sorted_unique_and_covers_every_domain():
    caps = K.catalogue()
    ids = [cap.id for cap in caps]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    assert K.catalogue() == caps
    assert K.catalogue_hash() == K.catalogue_hash(caps)
    assert K.domain_counts() == {
        domain: sum(1 for cap in caps if cap.domain == domain)
        for domain in K.DOMAINS
    }
    for domain in K.DOMAINS:
        assert any(cap.domain == domain for cap in caps), domain


def test_every_record_validates_against_the_contract_layer():
    for cap in K.catalogue():
        assert C.parse_id(cap.id).kind == "cap"
        assert C.canonical_id(cap.id) == cap.id
        assert cap.domain in K.DOMAINS
        assert cap.side_effect in K.SIDE_EFFECTS
        assert cap.exposure
        assert set(cap.exposure) <= set(K.EXPOSURES)
        assert ("internal" in cap.exposure) == (cap.exposure == ("internal",))
        assert cap.owner
        assert cap.impl
        for impl_id in cap.impl:
            assert C.parse_id(impl_id).kind == "impl"
        assert cap.tests and cap.docs
        for ref in (*cap.tests, *cap.docs):
            assert ref.startswith(("test:", "doc:"))
            body = ref.split(":", 1)[1]
            assert not body.startswith("/")
            assert ".." not in body.split("/")
        assert C.canonical_json(cap.to_dict())
    assert K.duplicate_surfaces() == {}


def test_builtin_mcp_and_graph_tools_are_declared():
    declared = {surface for cap in K.catalogue() for surface in cap.surfaces}
    full = {f"mcp:tool:{tool['name']}" for tool in server._TOOLS}
    graph = {f"mcp:graph:{tool['name']}" for tool in graph_surface.GRAPH_TOOLS}
    engine = {f"mcp:tool:{tool['name']}"
              for tool in graph_engine.ENGINE_TOOLS}
    assert full <= declared
    assert graph <= declared
    assert engine <= declared
    # Every declared MCP surface is a real built-in surface, not a guess.
    assert {surface for surface in declared
            if surface.startswith("mcp:")} == full | graph | engine
    assert "mcp:tool:dev_capabilities" in declared
    for name in ("dev_context", "dev_tools", "dev_capabilities",
                 "goal_list", "job_start", "federation_status",
                 "dev_tmp_list", "dev_tmp_status", "dev_assets_list",
                 "dev_assets_status", "dev_mesh_status", "dev_mesh_inbox",
                 "dev_ozma_flight"):
        assert f"mcp:tool:{name}" in declared
    assert "status:mesh" in declared
    assert "cap:mesh.status" in K.surface_index()["mcp:tool:dev_mesh_status"]


def test_cli_top_level_commands_are_declared():
    parser = cli.build_parser()
    sub = next(action for action in parser._actions
               if hasattr(action, "choices") and action.choices)
    expected = {f"cli:ozagia.{name}" for name in sub.choices}
    declared = {surface for cap in K.catalogue() for surface in cap.surfaces
                if surface.startswith("cli:")}
    assert expected == declared
    assert "cli:ozagia.capabilities" in declared
    assert "cli:ozagia.replace" in declared
    assert "cli:ozagia.goal" in declared


def _dispatch_operations() -> set[str]:
    """The op branches of ``daemon/server.py`` plus ``jobs.OPS``, by AST."""
    path = SRC / "ozagia" / "daemon" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    operations: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "op"):
            continue
        for operator, comparator in zip(node.ops, node.comparators):
            if (isinstance(operator, ast.Eq)
                    and isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)):
                operations.add(comparator.value)
            elif isinstance(operator, ast.In):
                values = getattr(comparator, "elts", None)
                if values is None:
                    continue
                for value in values:
                    if (isinstance(value, ast.Constant)
                            and isinstance(value.value, str)):
                        operations.add(value.value)
    operations.update(jobs.OPS)
    return operations


def test_daemon_operations_match_the_dispatch_vocabulary():
    expected = _dispatch_operations()
    assert set(protocol.OPERATIONS) == expected
    assert tuple(protocol.OPERATIONS) == tuple(sorted(protocol.OPERATIONS))
    declared = {surface.split(":", 2)[2] for cap in K.catalogue()
                for surface in cap.surfaces
                if surface.startswith("daemon:op:")}
    assert declared == set(protocol.OPERATIONS)
    assert "tmp" in protocol.OPERATIONS


def test_invalid_records_fail_explicitly():
    record = _base_record()
    K.build_capability(record)

    cases = [
        ({**record, "id": "graph.context"}, C.ContractError),
        ({**record, "domain": "unknown"}, K.CapabilityError),
        ({**record, "side_effect": "unknown"}, K.CapabilityError),
        ({**record, "exposure": ["internal", "mcp"]}, K.CapabilityError),
        ({**record, "exposure": ["cli"]}, K.CapabilityError),
        ({**record, "tests": []}, K.CapabilityError),
        ({**record, "docs": []}, K.CapabilityError),
        ({**record, "tests": ["test:tests/../escape.py"]}, K.CapabilityError),
        ({**record, "docs": ["doc:docs/capabilities.md", "doc:../x"]},
         K.CapabilityError),
        ({**record, "surfaces": ["mcp:tool:Upper"]}, K.CapabilityError),
        ({**record, "owner": "OZAGIA"}, K.CapabilityError),
        ({**record, "extra": 1}, K.CapabilityError),
    ]
    for bad, error in cases:
        with pytest.raises(error):
            K.build_capability(bad)

    with pytest.raises(C.IDCollisionError):
        K.build_catalogue([record, record])


def test_catalogue_is_pure_and_does_not_probe(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("catalogue building must not probe or run")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    caps = K.catalogue()
    assert K.catalogue_hash(caps)
    after = sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert before == after


def test_catalogue_hash_is_stable_across_hash_seeds():
    script = (
        "from ozagia.core.capabilities import catalogue_hash;"
        "print(catalogue_hash())"
    )
    hashes = []
    for seed in ("0", "1"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO, env=env, check=True,
            capture_output=True, text=True)
        hashes.append(completed.stdout.strip())
    assert hashes[0] == hashes[1] == K.catalogue_hash()
