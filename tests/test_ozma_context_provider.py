from __future__ import annotations

import json
import subprocess

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.ozma.context import task_context


def test_provider_fresh_stale_and_bounded(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    source = tmp_path / "example.py"
    source.write_text("def special_function():\n    return 123\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "example.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    dump = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, dump)
    memory = DevMemory(dump=dump, journal=tmp_path / ".ozagia" / "journal.jsonl", root=tmp_path)
    result = task_context(memory, "special_function", budget_tokens=5)
    assert result["ok"] and result["freshness"] == "current"
    assert sum(len(item["snippet"]["text"]) for item in result["items"]) <= 20
    json.dumps(result)
    source.write_text("def special_function():\n    return 456\n")
    assert task_context(memory, "special_function")["freshness"] == "stale"
    assert not task_context(memory, "special_function")["ok"]
    assert task_context(memory, "special_function", allow_stale=True)["ok"]


def test_provider_validates_before_reading_memory():
    for kwargs in ({"budget_tokens": 0}, {"budget_tokens": True}, {"max_items": 0},
                   {"allow_stale": "yes"}):
        with pytest.raises(ValueError):
            task_context(None, "task", **kwargs)
    with pytest.raises(ValueError):
        task_context(None, " ")


def test_provider_refuses_unknown_freshness():
    class Memory:
        _staleness_memo = None
        raw_repo_root = "."

        def staleness(self):
            return {"freshness": "unknown", "stale": False,
                    "verification_errors": ["cannot compute working-tree digest"]}

        def context(self, *args, **kwargs):
            return {"items": []}

    memory = Memory()
    assert not task_context(memory, "task")["ok"]
    result = task_context(memory, "task", allow_stale=True)
    assert result["ok"] and result["freshness"] == "unknown"


def test_provider_cannot_verify_different_loaded_index_scope(tmp_path, monkeypatch):
    from ozagia.core import memory as memory_module
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", ("configured-elsewhere",))

    class Memory:
        _staleness_memo = None
        raw_repo_root = tmp_path

        def staleness(self):
            return {"freshness": "fresh", "stale": False, "verification_errors": []}

    result = task_context(Memory(), "task")
    assert not result["ok"]
    assert result["freshness"] == "unknown"
    assert "configuration differs" in result["snapshot"]["verification_errors"][0]


def test_provider_rechecks_freshness_after_context_assembly(tmp_path):
    class Memory:
        _staleness_memo = None
        raw_repo_root = tmp_path

        def __init__(self):
            self.checks = 0

        def staleness(self):
            self.checks += 1
            return {"freshness": "fresh" if self.checks == 1 else "stale",
                    "stale": self.checks > 1, "verification_errors": [],
                    "root": str(tmp_path), "snapshot_root": str(tmp_path),
                    "head": "abc", "snapshot_head": "abc",
                    "tree_changed": self.checks > 1}

        def context(self, *args, **kwargs):
            return {"items": [{"snippet": {"text": "possibly changed"}}]}

    result = task_context(Memory(), "task", allow_stale=True)
    assert not result["ok"]
    assert result["freshness"] == "unknown"
    assert result["error"] == "repository changed during context assembly"


def test_provider_detects_edits_while_snapshot_is_already_stale(tmp_path):
    class Memory:
        _staleness_memo = None
        raw_repo_root = tmp_path

        def __init__(self):
            self.checks = 0

        def staleness(self):
            self.checks += 1
            return {"freshness": "stale", "stale": True,
                    "verification_errors": [], "root": str(tmp_path),
                    "snapshot_root": str(tmp_path), "head": "abc",
                    "snapshot_head": "abc", "tree_changed": True,
                    "current_tree_digest": f"digest-{self.checks}"}

        def context(self, *args, **kwargs):
            return {"items": [{"snippet": {"text": "changed during query"}}]}

    result = task_context(Memory(), "task", allow_stale=True)
    assert not result["ok"]
    assert result["freshness"] == "unknown"
    assert result["error"] == "repository changed during context assembly"


def test_provider_does_not_certify_current_without_observed_digest(tmp_path):
    class Memory:
        _staleness_memo = None
        raw_repo_root = tmp_path

        def staleness(self):
            return {"freshness": "fresh", "stale": False,
                    "verification_errors": [], "current_tree_digest": None}

        def context(self, *args, **kwargs):
            return {"items": [{"snippet": {"text": "uncertified"}}]}

    result = task_context(Memory(), "task")
    assert not result["ok"]
    assert result["freshness"] == "unknown"
