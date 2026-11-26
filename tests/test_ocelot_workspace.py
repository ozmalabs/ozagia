"""Integration tests for the Ocelot workspace adapter and its synthetic
workspace fixture.

The fixture tree is written by
``tests/fixtures/ocelot_workspace_repo.py`` and indexed with the adapter from
``.ozagia.toml``.  These tests pin the public adapter contract: seed nodes and
edges, seed summaries/census, the Ocelot-aware test plan, and the four MCP
tools.  They never import or execute the synthetic target project.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer


def _load_fixture_builder() -> Any:
    path = Path(__file__).resolve().parent / "fixtures" / "ocelot_workspace_repo.py"
    spec = importlib.util.spec_from_file_location(
        "_ocelot_workspace_repo_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = _load_fixture_builder()

_FULL_MODES = {"full", "full-gate", "full_gate", "gate", "migrated", "all"}


@pytest.fixture(scope="module")
def workspace_api() -> Any:
    """The adapter under test, skipped (not hidden) when it is not present."""
    return pytest.importorskip(
        "ozagia.integrations.ocelot.workspace",
        reason="the Ocelot workspace adapter has not been written yet")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
              workspace_api: Any) -> tuple[Path, DevMemory]:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    return FIXTURE.build_workspace(tmp_path / "ocelot-workspace")


# --------------------------------------------------------------------------- #
# generic payload helpers
# --------------------------------------------------------------------------- #
def _walk(value: Any, path: tuple[str, ...] = ()):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, (*path, str(key)))
    elif isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            yield from _walk(item, (*path, str(index)))


def _strings(value: Any) -> list[str]:
    """Strings in *value*, including mapping keys."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            out.extend(_strings(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            out.extend(_strings(item))
    return out


def _text(value: Any) -> str:
    return "\n".join(_strings(value)).lower()


def _has_positive(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, dict):
        return any(_has_positive(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_positive(item) for item in value)
    return False


def _emptyish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "", "0", "absent", "false", "missing", "no", "none", "untested"}
    if isinstance(value, dict):
        return all(_emptyish(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return all(_emptyish(item) for item in value)
    return False


def _record_id(value: dict[str, Any]) -> str | None:
    for key in ("id", "seed_id", "name", "seed"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _python_paths(payload: Any) -> set[str]:
    out: set[str] = set()
    for text in _strings(payload):
        out.update(re.findall(r"[\w./-]+\.py", text))
    return out


def _mode(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("mode", "strategy", "plan", "kind"):
        item = value.get(key)
        if isinstance(item, str):
            return item.strip().lower()
    return ""


def _full_gate_required(plan: dict[str, Any]) -> bool:
    # Explicit flags win over the general mode.  A fallback command in the
    # plan is not a requirement; only required/needed/triggered flags are.
    explicit: bool | None = None
    for key in ("full_gate", "full-gate", "full_gate_required",
                "requires_full_gate", "needs_full_gate", "requires_gate"):
        if key not in plan:
            continue
        value = plan[key]
        if isinstance(value, bool):
            explicit = value
        elif isinstance(value, dict):
            for flag in ("required", "needed", "active", "triggered",
                         "enabled", "run"):
                if flag in value:
                    explicit = bool(value[flag])
                    break
        elif value is not None:
            explicit = bool(value)
    if explicit is True:
        return True
    mode = _mode(plan)
    if mode:
        return mode in _FULL_MODES
    if explicit is False:
        return False
    modes = {
        value.strip().lower()
        for path, value in _walk(plan)
        if path and path[-1].lower() in {"mode", "strategy"}
        and isinstance(value, str)
    }
    return bool(modes & _FULL_MODES)


# --------------------------------------------------------------------------- #
# graph helpers
# --------------------------------------------------------------------------- #
def _seed_id(mem: DevMemory, nid: int) -> str | None:
    node = mem.node(nid) or {}
    attrs = node.get("attrs") or {}
    for key in ("id", "seed_id", "name"):
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    key = mem.key_of(nid) or ""
    if key.startswith("ocelot_seed:"):
        return key.split(":", 1)[1]
    return None


def _seed_nodes(mem: DevMemory) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for node in mem.nodes.values():
        if node.get("type") != "OcelotSeed":
            continue
        seed_id = _seed_id(mem, int(node["id"]))
        if seed_id:
            out[seed_id] = node
    return out


def _node_path(mem: DevMemory, nid: int) -> str:
    node = mem.node(nid) or {}
    attrs = node.get("attrs") or {}
    for key in ("path", "rel", "file"):
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value.replace("\\", "/")
    key = mem.key_of(nid) or ""
    prefix, _, rest = key.partition(":")
    if prefix in ("file", "doc"):
        return rest
    if prefix in ("test", "sym"):
        return rest.split("::", 1)[0]
    return mem.label(nid).replace("\\", "/")


def _edges(mem: DevMemory, edge_type: str) -> list[dict[str, Any]]:
    return [edge for edge in mem.edges if edge["type"] == edge_type]


def _mention_paths(mem: DevMemory, edge_type: str, seed_id: str) -> set[str]:
    return {
        _node_path(mem, int(edge["src"]))
        for edge in _edges(mem, edge_type)
        if _seed_id(mem, int(edge["tgt"])) == seed_id
    }


def _closure_has(summary: dict[str, Any], wanted: set[str]) -> bool:
    # A summary's `id` is the seed itself; the closure field normally lists
    # its ancestors.  Both together are the dependency closure (a summary
    # may also include the start id in the closure list).
    own = {
        summary.get(key) for key in ("id", "seed_id", "name")
        if isinstance(summary.get(key), str)
    }
    for path, value in _walk(summary):
        key = "/".join(path).lower()
        if not any(term in key for term in (
                "closure", "depend", "chain", "order", "seeds")):
            continue
        found = set(re.findall(r"[A-Za-z0-9_.-]+", " ".join(_strings(value))))
        if wanted <= (found | own):
            return True
    return False


def _census_has_dependent(census: dict[str, Any], seed: str,
                          dependent: str) -> bool:
    for path, value in _walk(census):
        key = "/".join(path).lower()
        if isinstance(value, dict) and _record_id(value) == seed:
            for child_key, child in value.items():
                if any(term in child_key.lower()
                       for term in ("depend", "fan", "reverse")):
                    if dependent in _strings(child) or _has_positive(child):
                        return True
        if not any(term in key for term in ("depend", "fan", "reverse")):
            continue
        strings = _strings(value)
        seed_named = seed in strings or seed in path
        if seed_named and (dependent in strings or _has_positive(value)):
            return True
    return False


def _census_lacks_tests(census: dict[str, Any], seed: str) -> bool:
    for path, value in _walk(census):
        key = "/".join(path).lower()
        if isinstance(value, dict) and _record_id(value) == seed:
            for child_key, child in value.items():
                child_lower = child_key.lower()
                if "test" in child_lower and _emptyish(child):
                    return True
                if any(term in child_lower for term in (
                        "untested", "no_tests", "missing_tests")):
                    return True
        if not any(term in key for term in (
                "no_test", "untested", "without_test", "missing_test",
                "testless", "lacks_test")):
            continue
        if seed in _strings(value) or seed in path:
            return True
    return False


# --------------------------------------------------------------------------- #
# 1. indexing
# --------------------------------------------------------------------------- #
def test_indexer_builds_seed_graph(workspace: tuple[Path, DevMemory]) -> None:
    _root, mem = workspace
    seeds = _seed_nodes(mem)
    assert set(FIXTURE.SEED_IDS) <= set(seeds), sorted(seeds)
    assert FIXTURE.GENERATED_SEED_ID not in seeds
    assert not any(
        "seeds/generated" in str((node.get("attrs") or {}).get("path", ""))
        for node in mem.nodes.values() if node.get("type") == "OcelotSeed")

    shared = seeds["shared"]
    assert str((shared.get("attrs") or {}).get("version")) == \
        FIXTURE.SEED_VERSIONS["shared"]

    def dependencies(seed_id: str) -> set[str]:
        src = int(seeds[seed_id]["id"])
        return {
            _seed_id(mem, int(edge["tgt"])) or ""
            for edge in _edges(mem, "ocelot_depends_on")
            if int(edge["src"]) == src
        }

    # ``core`` is the built-in substrate boundary: the adapter records it in
    # the manifest attrs rather than creating an edge to a built-in root.
    assert list((shared.get("attrs") or {}).get("depends_on") or []) == ["core"]
    assert dependencies("local") == {"shared"}
    assert dependencies("core") == set()

    boot_edges = [
        edge for edge in _edges(mem, "ocelot_boots")
        if FIXTURE.BOOT_CORE in _node_path(mem, int(edge["src"]))
        and _seed_id(mem, int(edge["tgt"])) == "shared"
    ]
    assert boot_edges, "missing boot_core -> shared edge"

    local_mentions = _mention_paths(mem, "ocelot_mentions_seed", "local")
    assert any(path.endswith("tests/test_local_migrated.py")
               for path in local_mentions), local_mentions
    shared_mentions = _mention_paths(mem, "ocelot_mentions_seed", "shared")
    assert any(path.endswith("tests/test_shared_migrated.py")
               for path in shared_mentions), shared_mentions
    assert any(path.endswith("domains/world.py")
               for path in shared_mentions), shared_mentions

    # The adapter records a document that names a seed as an incoming seed
    # mention.  Accept the dedicated `ocelot_documents` edge too (the adapter
    # declares both edge types), but do not require one spelling of the same
    # graph fact.
    shared_docs = (
        _mention_paths(mem, "ocelot_documents", "shared")
        | _mention_paths(mem, "ocelot_mentions_seed", "shared")
    )
    assert any(path.endswith(FIXTURE.DOC_PATH)
               for path in shared_docs), shared_docs

    engine_uses = [
        edge for edge in _edges(mem, "ocelot_uses_engine")
        if _node_path(mem, int(edge["src"])).endswith(
            "tests/test_shared_migrated.py")
        and (mem.node(int(edge["tgt"])) or {}).get("type") == "OcelotEngine"
    ]
    assert engine_uses, "missing engine-use edge from the shared test"
    engine_sources = [
        edge for edge in _edges(mem, "ocelot_engine_source")
        if _node_path(mem, int(edge["src"])).endswith(FIXTURE.RUST_SOURCE)
        and (mem.node(int(edge["tgt"])) or {}).get("type") == "OcelotEngine"
    ]
    assert engine_sources, "missing Rust source -> engine edge"


# --------------------------------------------------------------------------- #
# 2. seed summary and census
# --------------------------------------------------------------------------- #
def test_seed_summary_and_census(workspace: tuple[Path, DevMemory],
                                 workspace_api: Any) -> None:
    _root, mem = workspace

    summary = workspace_api.seed_summary(mem, "local")
    assert isinstance(summary, dict)
    assert summary.get("ok", True) is not False
    assert _closure_has(summary, {"core", "shared", "local"}), (
        "seed_summary('local') must report the full dependency closure "
        f"core/shared/local; got closure={summary.get('closure')!r}, "
        f"dependencies={summary.get('dependencies')!r}")
    assert any("test_local_migrated.py" in text
               for text in _strings(summary)), summary

    census = workspace_api.seed_census(mem)
    assert isinstance(census, dict)
    assert _census_has_dependent(census, "shared", "local"), census
    assert _census_lacks_tests(census, "no_test"), census


def test_census_counts_all_seeds_when_entries_are_capped(
        workspace: tuple[Path, DevMemory], workspace_api: Any,
        monkeypatch: pytest.MonkeyPatch) -> None:
    _root, mem = workspace
    monkeypatch.setattr(workspace_api, "MAX_CENSUS_ENTRIES", 2)
    census = workspace_api.seed_census(mem)
    assert census["seed_count"] == len(FIXTURE.SEED_IDS)
    assert census["entries_truncated"] is True
    assert len(census["entries"]) == 2


def test_oversized_seed_keeps_head_metadata(
        tmp_path: Path, workspace_api: Any,
        monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "oversized"
    FIXTURE.write_workspace(root)
    body = "x" * 20000
    (root / "seeds" / "local.json").write_text(
        '{"id": "local", "version": "3.1.0", '
        '"depends_on": ["shared"], "description": "large", '
        f'"payload": "{body}"}}\n', encoding="utf-8")
    monkeypatch.setattr(workspace_api, "MAX_SEED_BYTES", 1024)
    monkeypatch.setattr(workspace_api, "MAX_SEED_HEAD_BYTES", 4096)
    out = root / FIXTURE.OUT_REL
    build_index(root, out, include_docs="features", include_git=False)
    mem = DevMemory(out, root / FIXTURE.JOURNAL_REL, root=root)

    summary = workspace_api.seed_summary(mem, "local")
    assert summary["ok"] is True
    assert summary["partial"] is True
    assert summary["manifest_bytes"] > 1024
    assert "shared" in summary["dependencies"]
    assert "local" in workspace_api.seed_census(mem)["partial"]


# --------------------------------------------------------------------------- #
# 3. test plans
# --------------------------------------------------------------------------- #
def test_plan_for_local_seed_is_affected(
        workspace: tuple[Path, DevMemory], workspace_api: Any) -> None:
    _root, mem = workspace
    plan = workspace_api.test_plan(mem, seeds=["local"])
    assert isinstance(plan, dict)
    assert not _full_gate_required(plan), plan
    paths = _python_paths(plan)
    assert any(path.endswith("tests/test_local_migrated.py")
               for path in paths), plan


@pytest.mark.parametrize("seed_id", ["shared", "core"])
def test_plan_for_shared_or_core_seed_forces_full_gate(
        seed_id: str, workspace: tuple[Path, DevMemory],
        workspace_api: Any) -> None:
    _root, mem = workspace
    plan = workspace_api.test_plan(mem, seeds=[seed_id])
    assert isinstance(plan, dict)
    assert _full_gate_required(plan), plan
    assert seed_id in _text(plan), plan


def test_plan_for_rust_source_includes_engine_work(
        workspace: tuple[Path, DevMemory], workspace_api: Any) -> None:
    _root, mem = workspace
    plan = workspace_api.test_plan(mem, changed=[FIXTURE.RUST_SOURCE])
    assert isinstance(plan, dict)
    text = _text(plan)
    assert "cargo" in text or "engine_cargo_test" in text, plan
    paths = _python_paths(plan)
    assert any(path.endswith("tests/test_engine_provenance_migrated.py")
               for path in paths), plan
    assert any(path.endswith("tests/test_shared_migrated.py")
               for path in paths), plan


def test_plan_for_docs_selects_no_tests(
        workspace: tuple[Path, DevMemory], workspace_api: Any) -> None:
    _root, mem = workspace
    plan = workspace_api.test_plan(mem, changed=[FIXTURE.DOC_PATH])
    assert isinstance(plan, dict)
    assert not _python_paths(plan), plan
    assert not _full_gate_required(plan), plan


def test_plan_for_unknown_path_forces_full_gate(
        workspace: tuple[Path, DevMemory], workspace_api: Any) -> None:
    _root, mem = workspace
    plan = workspace_api.test_plan(mem, changed=["mystery/unindexed.bin"])
    assert isinstance(plan, dict)
    assert _full_gate_required(plan), plan
    text = _text(plan)
    assert any(word in text for word in (
        "mystery", "unknown", "unindexed", "not indexed", "full")), plan


# --------------------------------------------------------------------------- #
# 4. MCP registration
# --------------------------------------------------------------------------- #
def _mcp_tools(server: DevMemoryServer) -> list[dict[str, Any]]:
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return response["result"]["tools"]


def _call(server: DevMemoryServer, name: str,
          arguments: dict[str, Any]) -> dict[str, Any]:
    return server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def test_mcp_registers_ocelot_tools_and_dispatches_seed(
        workspace: tuple[Path, DevMemory]) -> None:
    _root, mem = workspace
    server = DevMemoryServer(mem)
    tools = _mcp_tools(server)
    names = {tool["name"] for tool in tools}
    expected = {"ocelot_status", "ocelot_seed", "ocelot_seeds", "ocelot_plan",
                "ocelot_engine", "ocelot_research", "ocelot_seed_validate"}
    assert expected <= names, sorted(names)

    spec = next(tool for tool in tools if tool["name"] == "ocelot_seed")
    properties = (spec.get("inputSchema") or {}).get("properties") or {}
    seed_arg = next((key for key in ("seed_id", "id", "seed", "name")
                     if key in properties), None)
    assert seed_arg, spec
    required = (spec.get("inputSchema") or {}).get("required") or []
    arguments: dict[str, Any] = {seed_arg: "local"}
    for key in required:
        if key in arguments:
            continue
        prop = properties.get(key) or {}
        if isinstance(prop.get("enum"), list) and prop["enum"]:
            arguments[key] = prop["enum"][0]
        elif "default" in prop:
            arguments[key] = prop["default"]
        elif prop.get("type") == "boolean":
            arguments[key] = False
        elif prop.get("type") == "integer":
            arguments[key] = 0
        else:
            arguments[key] = "summary"

    response = _call(server, "ocelot_seed", arguments)
    assert "error" not in response, response
    result = response["result"]
    assert result.get("isError") is False, result
    payload = json.loads(result["content"][0]["text"])
    assert payload.get("ok", True) is not False
    assert "local" in json.dumps(payload).lower(), payload
