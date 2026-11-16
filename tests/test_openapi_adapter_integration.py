"""End-to-end OpenAPI graph indexing through a project adapter."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import write_adapter_config
from ozagia.core import memory as memory_module
from ozagia.core.memory import DevMemory, build_index


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_selected_openapi_spec_indexes_and_reingests(tmp_path, monkeypatch):
    adapter_dir = tmp_path / "adapter"
    _write(adapter_dir / "selected_api_adapter.py", '''
import json
from ozagia.core.openapi import index_openapi_document

def index(ctx):
    rel = "specs/public.json"
    path = ctx.root / rel
    if path.is_file():
        index_openapi_document(ctx.builder, ctx.file_node(rel), rel,
                               path.read_text(encoding="utf-8"))

def register(registry):
    registry.add_indexer(index)
''')
    config = write_adapter_config(
        adapter_dir, ["selected_api_adapter:register"])
    monkeypatch.setenv("OZAGIA_CONFIG", str(config))

    root = tmp_path / "repo"
    spec_path = root / "specs" / "public.json"
    _write(spec_path, json.dumps({
        "openapi": "3.0.3",
        "paths": {"/pets": {"get": {"summary": "List pets"}}},
    }))
    _write(root / ".gitignore", "specs/public.json\n")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-q", "-m", "base"],
                   check=True)
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(memory_module, "TEST_DIRS", ())
    monkeypatch.setattr(memory_module, "DOC_GLOBS", ())

    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    mem = DevMemory(dump, tmp_path / "journal.jsonl", root=root)
    operation = next(n for n in mem.nodes.values()
                     if n["type"] == "ApiOperation")
    assert operation["attrs"]["summary"] == "List pets"
    assert mem.staleness()["stale"] is False

    _write(spec_path, json.dumps({
        "openapi": "3.0.3",
        "paths": {"/pets": {"get": {"summary": "Return all pets"}}},
    }))
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is True
    delta = mem.reingest(["specs/public.json"])

    refreshed = next(n for n in mem.nodes.values()
                     if n["type"] == "ApiOperation")
    assert refreshed["attrs"]["summary"] == "Return all pets"
    assert delta.changed
    assert mem.staleness()["stale"] is False

    spec_path.unlink()
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is True
    mem.refresh()
    assert not any(n["type"] == "ApiOperation" for n in mem.nodes.values())
    assert mem.staleness()["freshness"] == "fresh"

    _write(spec_path, json.dumps({"openapi": "3.0.3", "paths": {
        "/pets": {"get": {"summary": "Restored pets"}}}}))
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is True
    mem.refresh()
    restored = next(n for n in mem.nodes.values() if n["type"] == "ApiOperation")
    assert restored["attrs"]["summary"] == "Restored pets"
    assert mem.staleness()["freshness"] == "fresh"


def test_explicit_api_mapping_build_and_reingest_use_operation_lookup(tmp_path, monkeypatch):
    adapter_dir = tmp_path / "mapping_adapter"
    _write(adapter_dir / "mapping_adapter.py", '''
import json
from ozagia.core.openapi import index_openapi_document
from ozagia.core.api_mappings import index_api_mappings

def index(ctx):
    rel = "specs/public.json"
    path = ctx.root / rel
    if path.is_file():
        index_openapi_document(ctx.builder, ctx.file_node(rel), rel,
                               path.read_text(encoding="utf-8"), repo="fixture")
    registry = {"version": 1, "mappings": [{
        "relation": "produces_api", "repo": "fixture", "path": "app.py",
        "symbol": "list_pets", "api": "fixture:specs/public.json#listPets"
    }]}
    result = index_api_mappings(ctx.builder, json.dumps(registry),
                                "integration/api-links.json", {"fixture": ctx.root})
    if result["errors"]:
        raise RuntimeError(result["errors"])

def register(registry):
    registry.add_indexer(index)
''')
    config = write_adapter_config(adapter_dir, ["mapping_adapter:register"])
    monkeypatch.setenv("OZAGIA_CONFIG", str(config))

    root = tmp_path / "repo"
    spec_path = root / "specs" / "public.json"
    _write(root / "app.py", "def list_pets():\n    return []\n")
    _write(spec_path, json.dumps({
        "openapi": "3.0.3",
        "paths": {"/pets": {"get": {"operationId": "listPets"}}},
    }))
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(memory_module, "TEST_DIRS", ())
    monkeypatch.setattr(memory_module, "DOC_GLOBS", ())

    dump = tmp_path / "mapped-graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    mem = DevMemory(dump, tmp_path / "mapped-journal.jsonl", root=root)

    def assert_linked():
        operations = {n["id"] for n in mem.nodes.values()
                      if n["type"] == "ApiOperation"}
        mappings = [n for n in mem.nodes.values() if n["type"] == "ApiMapping"]
        assert len(mappings) == 1
        edge = next(e for e in mem.edges if e["type"] == "refers_to_api_operation")
        assert edge["src"] == mappings[0]["id"]
        assert edge["tgt"] in operations

    assert_linked()
    _write(spec_path, json.dumps({
        "openapi": "3.0.3",
        "paths": {"/pets": {"get": {"operationId": "listPets", "summary": "Updated"}}},
    }))
    delta = mem.reingest(["specs/public.json"])
    assert delta.changed
    assert_linked()

