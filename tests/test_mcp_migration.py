"""The bounded ``dev_migration`` MCP tool: status, tasks and preview."""
from __future__ import annotations

import shutil
from pathlib import Path

from ozagia.config import clear_config_cache
from ozagia.integrations.mcp.server import DevMemoryServer

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "py_rust_migration"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    clear_config_cache()
    return root


def _server(root: Path) -> DevMemoryServer:
    return DevMemoryServer(None, workspace=root)


def test_dev_migration_is_listed_and_bounded(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    server = _server(root)
    tools = {tool["name"]: tool for tool in server._tools}
    assert "dev_migration" in tools
    schema = tools["dev_migration"]["inputSchema"]
    assert schema["additionalProperties"] is False

    result = server._handlers["dev_migration"]({"view": "status", "limit": 2})
    assert result["ok"] is True
    assert result["configured"] is True
    assert result["written"] is False
    assert result["executed"] is False
    assert result["status_counts"]["ported"] == 1
    assert len(result["gaps"]) == 2
    assert result["gaps_count"] == 7
    assert len(result["candidates"]) == 2
    assert result["truncated"] is True
    assert result["freshness"]["state"] == "unknown"
    assert not (root / ".ozagia").exists()


def test_dev_migration_unavailable_state(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    clear_config_cache()
    result = _server(root)._handlers["dev_migration"]({"view": "status"})

    assert result["ok"] is True
    assert result["configured"] is False
    assert "no [migration] section" in result["reason"]
    assert result["status_counts"]["planned"] == 0


def test_dev_migration_tasks_do_not_create_goals(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    result = _server(root)._handlers["dev_migration"](
        {"view": "tasks", "limit": 1})

    assert result["ok"] is True
    assert result["created_goals"] is False
    assert result["executed"] is False
    assert result["counts"]["returned"] == 1
    assert not (root / ".ozagia" / "goals").exists()


def test_dev_migration_scaffold_is_preview_only(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    target = root / "rust" / "src" / "service.rs"
    before = target.read_bytes()
    server = _server(root)
    result = server._handlers["dev_migration"]({
        "view": "scaffold",
        "symbol": "fixturepy.service.normalize_name",
        "target": "rust/src/service.rs",
    })

    assert result["ok"] is True
    assert result["written"] is False
    assert "pub fn normalize_name" in result["candidate"]
    assert target.read_bytes() == before
    assert "preview-only" in result["note"]

    refused = server._handlers["dev_migration"]({
        "view": "scaffold",
        "symbol": "fixturepy.models.ProtocolThing",
        "target": "rust/src/service.rs",
    })
    assert refused["ok"] is False
    assert "protocol" in refused["error"]
    assert target.read_bytes() == before


def test_dev_migration_limit_is_bounded(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    handler = _server(root)._handlers["dev_migration"]

    assert handler({"view": "status", "limit": 101})["ok"] is False
    assert handler({"view": "status", "limit": 0})["ok"] is False
    assert handler({"view": "status", "write": True})["ok"] is False
    assert handler({"view": "unknown"})["ok"] is False
    assert handler({"view": "scaffold"})["ok"] is False
