from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ozagia import core
from ozagia.federation_cli import main
from ozagia.integrations.mcp.server import DevMemoryServer


def test_federation_cli_forwards_explicit_roots(monkeypatch, tmp_path: Path,
                                                capsys) -> None:
    calls = []
    fake = SimpleNamespace(
        status=lambda root, roots=None: calls.append(("status", root, roots)) or
        {"ok": True, "repositories": []},
        search=lambda root, query, roots=None, limit=20, family_boost=False:
        calls.append(("search", root, query, roots, limit, family_boost)) or
        {"ok": True, "results": []},
        code_search=lambda root, query, roots=None, limit=20,
        include_snippet=False, cache_dir=None:
        calls.append(("code_search", root, query, roots, limit,
                      include_snippet, cache_dir)) or
        {"ok": True, "results": []},
        node=lambda root, key, roots=None: calls.append(
            ("node", root, key, roots)) or {"ok": True, "node": None},
        check=lambda root, roots=None: calls.append(
            ("check", root, roots)) or {"ok": True, "checks": []},
    )
    monkeypatch.setattr(core, "federation", fake, raising=False)
    monkeypatch.setattr(core, "cross_repo", SimpleNamespace(check=lambda root, roots=None:
                         calls.append(("check", root, roots)) or
                         {"ok": True, "checks": []}), raising=False)
    root, member = tmp_path / "root", tmp_path / "member"
    assert main(["search", "cache", "--root", str(root), "--repo", str(member),
                 "--limit", "7"]) == 0
    assert calls == [("search", root.resolve(), "cache", [member.resolve()], 7, False)]
    assert '"results": []' in capsys.readouterr().out

    assert main(["search", "cache", "--root", str(root), "--repo", str(member),
                 "--limit", "7", "--family-boost"]) == 0
    assert calls[-1] == ("search", root.resolve(), "cache", [member.resolve()],
                         7, True)
    capsys.readouterr()

    assert main(["check", "--root", str(root), "--repo", str(member)]) == 0
    assert calls[-1] == ("check", root.resolve(), [member.resolve()])
    capsys.readouterr()

    assert main(["code-search", "cache", "--root", str(root),
                 "--repo", str(member), "--limit", "7"]) == 0
    assert calls[-1] == ("code_search", root.resolve(), "cache",
                         [member.resolve()], 7, False, None)
    assert '"results": []' in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        main(["code-search", "cache", "--root", str(root),
              "--family-boost"])
    assert exc.value.code == 2
    assert calls[-1][0] == "code_search"
    capsys.readouterr()


def test_federation_mcp_tools_are_read_only_surface(monkeypatch,
                                                    tmp_path: Path) -> None:
    calls = []
    fake = SimpleNamespace(
        status=lambda root, roots=None: calls.append(("status", root, roots)) or
        {"ok": True, "repositories": []},
        search=lambda root, query, roots=None, limit=20, family_boost=False:
        calls.append(("search", root, query, roots, limit, family_boost)) or
        {"ok": True, "results": []},
        code_search=lambda root, query, roots=None, limit=20,
        include_snippet=False, cache_dir=None:
        calls.append(("code_search", root, query, roots, limit,
                      include_snippet, cache_dir)) or
        {"ok": True, "results": []},
        node=lambda root, key, roots=None: calls.append(
            ("node", root, key, roots)) or {"ok": True, "node": None},
    )
    monkeypatch.setattr(core, "federation", fake, raising=False)
    cross_repo = SimpleNamespace(check=lambda root, roots=None: calls.append(
        ("check", root, roots)) or {"ok": True, "checks": []})
    monkeypatch.setattr(core, "cross_repo", cross_repo, raising=False)
    memory = SimpleNamespace(raw_repo_root=tmp_path, nodes={})
    server = DevMemoryServer(memory, workspace=tmp_path)
    tools = {tool["name"]: tool for tool in server._tools}
    assert {"federation_status", "federation_search", "federation_node",
            "federation_check"} <= set(tools)
    family_boost_schema = tools["federation_search"]["inputSchema"]["properties"][
        "family_boost"]
    assert family_boost_schema["type"] == "boolean"
    assert family_boost_schema["default"] is False
    assert "opt-in" in family_boost_schema["description"]
    mode_schema = tools["federation_search"]["inputSchema"]["properties"]["mode"]
    assert mode_schema["enum"] == ["graph", "code"]
    assert mode_schema["default"] == "graph"
    assert "source" in mode_schema["description"]
    assert "Graph mode only" in family_boost_schema["description"]
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "federation_search", "arguments": {
            "query": "cache", "roots": [str(tmp_path / "member")], "limit": 4}},
    })
    assert response["result"]["isError"] is False
    assert calls == [("search", str(tmp_path), "cache",
                      [str((tmp_path / "member").resolve())], 4, False)]
    response = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "federation_search", "arguments": {
            "query": "cache", "limit": 2, "family_boost": True}},
    })
    assert response["result"]["isError"] is False
    assert calls[-1] == ("search", str(tmp_path), "cache", None, 2, True)
    response = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "federation_search", "arguments": {
            "query": "cache", "roots": [str(tmp_path / "member")],
            "limit": 3, "mode": "code"}},
    })
    assert response["result"]["isError"] is False
    assert calls[-1] == ("code_search", str(tmp_path), "cache",
                         [str((tmp_path / "member").resolve())], 3, False,
                         None)
    response = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "federation_search", "arguments": {
            "query": "cache", "mode": "code", "family_boost": True}},
    })
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["mode"] == "code"
    assert payload["error"] == "family_boost is only supported in graph mode"
    assert calls[-1] == ("code_search", str(tmp_path), "cache",
                         [str((tmp_path / "member").resolve())], 3, False,
                         None)
    server._federation_status({"roots": []})
    assert calls[-1] == ("status", str(tmp_path), [])
    response = server.handle({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "federation_check", "arguments": {"roots": []}},
    })
    assert response["result"]["isError"] is False
    assert calls[-1] == ("check", str(tmp_path), [])
