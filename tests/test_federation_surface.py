from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ozagia import core
from ozagia.federation_cli import main
from ozagia.integrations.mcp.server import DevMemoryServer


def test_federation_cli_forwards_explicit_roots(monkeypatch, tmp_path: Path,
                                                capsys) -> None:
    calls = []
    fake = SimpleNamespace(
        status=lambda root, roots=None: calls.append(("status", root, roots)) or
        {"ok": True, "repositories": []},
        search=lambda root, query, roots=None, limit=20: calls.append(
            ("search", root, query, roots, limit)) or {"ok": True, "results": []},
        node=lambda root, key, roots=None: calls.append(
            ("node", root, key, roots)) or {"ok": True, "node": None},
    )
    monkeypatch.setattr(core, "federation", fake, raising=False)
    root, member = tmp_path / "root", tmp_path / "member"
    assert main(["search", "cache", "--root", str(root), "--repo", str(member),
                 "--limit", "7"]) == 0
    assert calls == [("search", root.resolve(), "cache", [member.resolve()], 7)]
    assert '"results": []' in capsys.readouterr().out


def test_federation_mcp_tools_are_read_only_surface(monkeypatch,
                                                    tmp_path: Path) -> None:
    calls = []
    fake = SimpleNamespace(
        status=lambda root, roots=None: calls.append(("status", root, roots)) or
        {"ok": True, "repositories": []},
        search=lambda root, query, roots=None, limit=20: calls.append(
            ("search", root, query, roots, limit)) or {"ok": True, "results": []},
        node=lambda root, key, roots=None: calls.append(
            ("node", root, key, roots)) or {"ok": True, "node": None},
    )
    monkeypatch.setattr(core, "federation", fake, raising=False)
    memory = SimpleNamespace(raw_repo_root=tmp_path, nodes={})
    server = DevMemoryServer(memory, workspace=tmp_path)
    names = {tool["name"] for tool in server._tools}
    assert {"federation_status", "federation_search", "federation_node"} <= names
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "federation_search", "arguments": {
            "query": "cache", "roots": [str(tmp_path / "member")], "limit": 4}},
    })
    assert response["result"]["isError"] is False
    assert calls == [("search", str(tmp_path), "cache",
                      [str((tmp_path / "member").resolve())], 4)]
