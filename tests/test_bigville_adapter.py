"""Bigville's read-only scenario graph extension."""
from __future__ import annotations

import json
from pathlib import Path
from ozagia.core import memory as memory_module
from ozagia.core.memory import DevMemory, build_index


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_bigville_scenarios_and_seed_links_are_indexed_read_only(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "bigville"
    scenario = {
        "id": "market_day", "name": "Market Day", "description": "Trade loop",
        "actors": [{"name": "Ada"}, {"name": "Bryn"}],
        "seeds": ["trade_core", "conversation"],
    }
    _write(root / "scenarios" / "market_day.json", json.dumps(scenario))
    _write(root / "seeds" / "trade_core.json",
           '{"id":"trade_core","depends_on":["conversation"],'
           '"rules":[{"name":"trade_rule","head":"trade",'
           '"body":"wheat exchanged for bread"}],"nodes":[]}')
    _write(root / "seeds" / "conversation.json",
           '{"id":"conversation","depends_on":[],"rules":[]}')
    _write(root / "bigville" / "seeds" / "trade_core.json",
           '{"id":"trade_core","depends_on":["conversation"],'
           '"rules":[{"name":"packaged_trade_rule"}],"nodes":[]}')
    _write(root / "bigville" / "seeds" / "gen_demo.json",
           '{"id":"gen_demo","rules":[],"generated_from":{'
           '"pack":"demo","tables":["ITEMS"],"sha256":"declared-hash"}}')
    _write(root / "bigville" / "seeds" / "gen_missing_table.json",
           '{"id":"gen_missing_table","rules":[],"generated_from":{'
           '"pack":"demo","tables":["ABSENT"],"sha256":"recorded-hash"}}')
    _write(root / "seeds" / "broken.json", "{bad json")
    _write(
        root / "tests" / "test_market.py",
        "def test_market():\n"
        "    # scenarios/market_day.json\n"
        "    # seeds/trade_core.json\n"
        "    # packs/demo/world/items.json\n"
        "    assert True\n")
    _write(root / "bigville" / "server.py", """
class Handler:
    def do_GET(self):
        if parsed.path == "/api/state":
            pass
    def do_POST(self):
        if parsed.path == "/api/turn":
            pass
""")
    _write(root / "bigville" / "game" / "game.js",
           "fetch('/api/state');\nfetch('/api/turn', {method: 'POST'});\n"
           "fetch('/api/not-implemented');\n")
    _write(root / "bigville" / "management" / "http.py", """
API_PREFIX = "/api/management/v1"
class Handler:
    def do_GET(self):
        if path == f"{API_PREFIX}/runs":
            pass
        if path.startswith(f"{API_PREFIX}/runs/") and path.endswith("/report"):
            pass
        if path.startswith(f"{API_PREFIX}/runs/") and "/views/" in path:
            pass
    def do_POST(self):
        if path == f"{API_PREFIX}/runs":
            pass
""")
    _write(root / "bigville" / "management" / "static" / "management.js", """
const API = "/api/management/v1";
async function requestJson(path) { return fetch(`${API}${path}`); }
requestJson(`/runs/${encodeURIComponent(runId)}/report`);
requestJson(`/runs/${encodeURIComponent(runId)}/views/referee`);
requestJson("/runs");
requestJson("/runs", {method: "POST"});
""")
    _write(root / "packs" / "demo" / "pack.json",
           '{"name":"demo","version":1,"tables":[{"name":"ITEMS",'
           '"file":"world/items.json"}]}')
    _write(root / "packs" / "demo" / "world" / "items.json", '{"items":[]}')
    outside_seed = tmp_path / "outside-seed.json"
    outside_seed.write_text('{"id":"outside","rules":[]}', encoding="utf-8")
    (root / "seeds" / "escape.json").symlink_to(outside_seed)

    adapter_dir = Path(__file__).resolve().parents[1] / "addins"
    monkeypatch.syspath_prepend(str(adapter_dir))
    monkeypatch.setenv("OZAGIA_ADAPTERS", "bigville_adapter:register")
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(memory_module, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(memory_module, "DOC_GLOBS", ())
    dump = tmp_path / "snapshot.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)

    scenario_node = memory.node_by_key("bigville_scenario:market_day")
    assert scenario_node is not None
    attrs = memory.nodes[scenario_node]["attrs"]
    assert attrs["actor_count"] == 2
    assert attrs["actor_names"] == ["Ada", "Bryn"]
    assert memory.stats()["repo"]["scenarios"] == 1
    assert memory.stats()["repo"]["seed_manifests"] == 5
    assert memory.adapter_query("bigville_scenarios", prefix="market") == [{
        "id": "market_day", "name": "Market Day", "description": "Trade loop",
        "path": "scenarios/market_day.json", "actor_count": 2,
        "seed_ids": ["conversation", "trade_core"],
    }]

    # Duplicate IDs remain separate path-keyed manifest nodes; ID-only
    # scenario/dependency references are visibly ambiguous.
    edges = {(e["src"], e["type"], e["tgt"]) for e in memory.edges}
    sid = scenario_node
    trade_nodes = [n for n in memory.nodes.values()
                   if n["type"] == "BigvilleSeedManifest" and n["attrs"]["id"] == "trade_core"]
    assert {n["attrs"]["path"] for n in trade_nodes} == {
        "seeds/trade_core.json", "bigville/seeds/trade_core.json"}
    assert all(n["attrs"]["id_resolution"] == "ambiguous_duplicate_id"
               for n in trade_nodes)
    assert attrs["ambiguous_seed_ids"] == ["trade_core"]
    assert set(attrs["seed_manifest_candidates"]["trade_core"]) == {
        "seeds/trade_core.json", "bigville/seeds/trade_core.json"}
    assert all(any(e["src"] == sid and e["type"] == "references_seed_manifest"
                   and e["tgt"] == int(n["id"]) for e in memory.edges) for n in trade_nodes)
    trade_root = next(n for n in trade_nodes if n["attrs"]["path"] == "seeds/trade_core.json")
    assert trade_root["attrs"]["rule_count"] == 1
    assert any(e["src"] == int(trade_root["id"])
               and e["type"] == "declares_dependency_candidate"
               for e in memory.edges)
    assert any(n["type"] == "BigvilleSeedItem"
               and n["attrs"]["name"] == "trade_rule"
               and n["attrs"]["seed_path"] == "seeds/trade_core.json"
               and n["attrs"]["body"] == "wheat exchanged for bread"
               for n in memory.nodes.values())
    assert not [n for n in memory.nodes.values() if n["attrs"].get("id") == "broken"]
    assert any(edge[1] == "mentions_scenario" and edge[2] == sid for edge in edges)
    assert any(edge[1] == "mentions_seed_manifest" and edge[2] == int(trade_root["id"])
               for edge in edges)
    generated = memory.adapter_query("bigville_seed_manifests", seed_id="gen_demo")[0]
    assert generated["path"] == "bigville/seeds/gen_demo.json"
    assert generated["source_kind"] == "packaged_runtime_seed"
    assert generated["freshness"] == "unverified_declared_generation"
    assert generated["generated_from"]["sha256"] == "declared-hash"
    assert generated["unresolved_generated_tables"] == []
    generated_node = next(n for n in memory.nodes.values()
                          if n["type"] == "BigvilleSeedManifest"
                          and n["attrs"]["id"] == "gen_demo")
    assert any(e["src"] == int(generated_node["id"])
               and e["type"] == "generated_from_table" for e in memory.edges)
    missing_generated = memory.adapter_query(
        "bigville_seed_manifests", seed_id="gen_missing_table")[0]
    assert missing_generated["unresolved_generated_tables"] == ["ABSENT"]
    missing_node = next(n for n in memory.nodes.values()
                        if n["type"] == "BigvilleSeedManifest"
                        and n["attrs"]["id"] == "gen_missing_table")
    assert not any(e["src"] == int(missing_node["id"])
                   and e["type"] == "generated_from_table" for e in memory.edges)
    routes = [n for n in memory.nodes.values() if n["type"] == "BigvilleRoute"]
    route_by_path = {n["attrs"]["path"]: n["attrs"] for n in routes}
    assert route_by_path["/api/state"]["evidence"] == "explicit_handler_and_client"
    assert route_by_path["/api/turn"]["method"] == "POST"
    assert route_by_path["/api/not-implemented"]["evidence"] == "client_only_unverified"
    assert route_by_path["/api/not-implemented"]["uncertainty"]
    management_route = route_by_path["/api/management/v1/runs/{param}/report"]
    assert management_route["evidence"] == "explicit_handler_and_client"
    run_routes = {(n["attrs"]["method"], n["attrs"]["evidence"])
                  for n in routes if n["attrs"]["path"] == "/api/management/v1/runs"}
    assert run_routes == {("GET", "explicit_handler_and_client"),
                          ("POST", "explicit_handler_and_client")}
    assert any(n["attrs"]["path"] == "/api/management/v1/runs/{param}/views/{param}"
               and n["attrs"]["evidence"] == "explicit_handler_and_client"
               for n in routes)
    assert memory.stats()["repo"]["packs"] == 1
    assert any(n["type"] == "PackTable" and n["attrs"]["name"] == "ITEMS"
               for n in memory.nodes.values())
    assert memory.adapter_query("bigville_routes", prefix="/api/state")[0]["client_files"] == [
        "bigville/game/game.js"]
    data_evidence = memory.adapter_query(
        "bigville_data_test_evidence", path_prefix="scenarios/")
    assert data_evidence == [{
        "subject_type": "scenario",
        "path": "scenarios/market_day.json",
        "test_files": ["tests/test_market.py"],
        "evidence": ["literal_scenario_mention"],
        "ambiguity": "",
        "completeness": "advisory_only",
    }]
    table_evidence = memory.adapter_query(
        "bigville_data_test_evidence", path_prefix="packs/")
    assert any(
        row["path"] == "packs/demo/world/items.json"
        and row["test_files"] == ["tests/test_market.py"]
        and row["evidence"] == ["literal_pack_table_mention"]
        and row["completeness"] == "advisory_only"
        for row in table_evidence)
    assert (root / "scenarios" / "market_day.json").read_text(encoding="utf-8") == json.dumps(scenario)
    assert {p.relative_to(root).as_posix() for p in root.rglob("*")} == {
        "scenarios", "scenarios/market_day.json", "seeds", "seeds/trade_core.json",
        "seeds/conversation.json", "seeds/broken.json", "seeds/escape.json",
        "bigville/seeds", "bigville/seeds/trade_core.json", "bigville/seeds/gen_demo.json",
        "bigville/seeds/gen_missing_table.json",
        "tests", "tests/test_market.py",
        "bigville", "bigville/server.py", "bigville/game", "bigville/game/game.js",
        "bigville/management", "bigville/management/http.py",
        "bigville/management/static", "bigville/management/static/management.js",
        "packs", "packs/demo", "packs/demo/pack.json", "packs/demo/world",
        "packs/demo/world/items.json",
    }

    # Pack manifests and table files are adapter watch inputs. Adding a table
    # resolves the previously visible generated-source gap; deleting it makes
    # the unresolved declaration visible again.
    pack_manifest = root / "packs" / "demo" / "pack.json"
    _write(pack_manifest,
           '{"name":"demo","version":1,"tables":['
           '{"name":"ITEMS","file":"world/items.json"},'
           '{"name":"ABSENT","file":"world/absent.json"}]}')
    absent_table = root / "packs" / "demo" / "world" / "absent.json"
    _write(absent_table, '{"rows":[]}')
    delta = memory.refresh(write=False)
    assert "packs/demo/pack.json" in delta.files
    assert "packs/demo/world/absent.json" in delta.files
    missing_generated = memory.adapter_query(
        "bigville_seed_manifests", seed_id="gen_missing_table")[0]
    assert missing_generated["unresolved_generated_tables"] == []
    generated_node_id = memory.node_by_key(
        "bigville_seed_manifest:bigville/seeds/gen_missing_table.json")
    absent_table_node = next(n for n in memory.nodes.values()
                             if n["type"] == "PackTable"
                             and n["attrs"].get("name") == "ABSENT")
    assert any(edge["src"] == generated_node_id
               and edge["type"] == "generated_from_table"
               and edge["tgt"] == absent_table_node["id"]
               for edge in memory.edges)

    pack_manifest.write_text(
        '{"name":"demo","version":1,"tables":['
        '{"name":"ITEMS","file":"world/items.json"}]}', encoding="utf-8")
    absent_table.unlink()
    delta = memory.refresh(write=False)
    assert "packs/demo/pack.json" in delta.files
    assert "packs/demo/world/absent.json" in delta.files
    missing_generated = memory.adapter_query(
        "bigville_seed_manifests", seed_id="gen_missing_table")[0]
    assert missing_generated["unresolved_generated_tables"] == ["ABSENT"]
    assert not any(edge["src"] == generated_node_id
                   and edge["type"] == "generated_from_table"
                   and edge["tgt"] == absent_table_node["id"]
                   for edge in memory.edges)


def test_scenario_mentions_resolve_by_path_and_refresh_created_deleted_manifests(
        tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "bigville"
    scenario = {"id": "shared_runtime_id", "name": "Market", "seeds": []}
    first = root / "scenarios" / "nested" / "market-file.json"
    second = root / "scenarios" / "nested" / "market-alias.json"
    _write(first, json.dumps(scenario))
    source = root / "src" / "engine.py"
    _write(source, "SCENARIOS = []\n")

    adapter_dir = Path(__file__).resolve().parents[1] / "addins"
    monkeypatch.syspath_prepend(str(adapter_dir))
    monkeypatch.setenv("OZAGIA_ADAPTERS", "bigville_adapter:register")
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", ("src",))
    monkeypatch.setattr(memory_module, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(memory_module, "DOC_GLOBS", ())
    dump = tmp_path / "snapshot.json"
    build_index(root, dump, include_docs="none", include_git=False)
    memory = DevMemory(dump, tmp_path / "journal.jsonl", root=root)

    # The manifest ID differs from its filename, so a literal path mention
    # must still resolve to the one exact scenario file.
    first_node = next(node for node in memory.nodes.values()
                      if node["type"] == "Scenario"
                      and node["attrs"]["path"] == "scenarios/nested/market-file.json")
    assert memory.node_by_key("bigville_scenario:shared_runtime_id") == first_node["id"]
    source_node = memory.node_by_key("file:src/engine.py")
    assert not any(edge["src"] == source_node
                   and edge["type"] == "mentions_scenario"
                   for edge in memory.edges)

    # Changing a source file to mention an existing scenario reruns the
    # regex-dependent indexer as well as rebuilding the core file node.
    _write(source, "SCENARIOS = ['scenarios/nested/market-file.json']\n")
    delta = memory.refresh(write=False)
    assert "src/engine.py" in delta.files
    assert any(edge["src"] == source_node
               and edge["type"] == "mentions_scenario"
               and edge["tgt"] == first_node["id"]
               for edge in memory.edges)

    # The scenario watch glob must make a newly created path visible on
    # refresh. A duplicate runtime ID remains two path-distinct records.
    _write(second, json.dumps(scenario))
    _write(source, "SCENARIOS = ['scenarios/nested/market-file.json', "
                   "'scenarios/nested/market-alias.json']\n")
    delta = memory.refresh(write=False)
    assert "scenarios/nested/market-alias.json" in delta.files
    assert "src/engine.py" in delta.files
    scenarios = memory.adapter_query("bigville_scenarios", prefix="shared_runtime_id")
    assert {item["path"] for item in scenarios} == {
        "scenarios/nested/market-file.json", "scenarios/nested/market-alias.json"}
    assert memory.node_by_key(
        "bigville_scenario:shared_runtime_id::scenarios/nested/market-file.json") is not None
    assert memory.node_by_key(
        "bigville_scenario:shared_runtime_id::scenarios/nested/market-alias.json") is not None
    second_node = next(node for node in memory.nodes.values()
                       if node["type"] == "Scenario"
                       and node["attrs"]["path"] == "scenarios/nested/market-alias.json")
    assert any(edge["src"] == source_node
               and edge["type"] == "mentions_scenario"
               and edge["tgt"] == second_node["id"]
               for edge in memory.edges)

    # Deleting a watched path also reruns the adapter and removes its node and
    # mention edge while preserving the other file with the same manifest ID.
    first.unlink()
    delta = memory.refresh(write=False)
    assert "scenarios/nested/market-file.json" in delta.files
    scenarios = memory.adapter_query("bigville_scenarios", prefix="shared_runtime_id")
    assert [item["path"] for item in scenarios] == ["scenarios/nested/market-alias.json"]
    assert memory.node_by_key("bigville_scenario:shared_runtime_id") is not None
    assert not any(node["type"] == "Scenario"
                   and node["attrs"].get("path") == "scenarios/nested/market-file.json"
                   for node in memory.nodes.values())
    assert not any(edge["src"] == source_node
                   and edge["type"] == "mentions_scenario"
                   and edge["tgt"] == first_node["id"]
                   for edge in memory.edges)
