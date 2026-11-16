"""The local read-only task executor and its bounded tools."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.core import task_exec
from ozagia.core import tasks as T
from ozagia.core.memory import _tree_digest

FILES = {
    "src/__init__.py": "",
    "src/app.py": (
        "def add(a, b):\n"
        "    return a + b\n"
        "# end\n"
    ),
    "src/other.py": (
        "from src.app import add\n"
        "\n"
        "def run():\n"
        "    return add(1, 2)\n"
    ),
    "src/pkg/__init__.py": "",
    "src/pkg/mod.py": "def h():\n    return 3\n",
    "tests/test_app.py": (
        "from src.app import add\n"
        "\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
    ),
    "top.py": "VALUE = 1\n",
    "README.md": (
        "# Fixture\n"
        "needle one\n"
        "other line\n"
        "Needle two\n"
        "needle three\n"
    ),
    "docs/guide.md": "# Guide\n",
    "ozagia/resources.toml": "[resources]\nenabled = false\n",
    ".gitignore": ".ozagia/\n",
}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Task Tester",
        "GIT_AUTHOR_EMAIL": "task@example.com",
        "GIT_COMMITTER_NAME": "Task Tester",
        "GIT_COMMITTER_EMAIL": "task@example.com",
    })
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        env=env, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _graph_payload(root: Path, head: str) -> dict:
    graph_path = root / ".ozagia" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    digest, error = _tree_digest(root, dump=graph_path)
    assert error is None
    assert digest
    return {
        "version": 2,
        "nodes": [
            {"id": 0, "type": "Repo", "key": "repo:",
             "attrs": {"root": str(root.resolve()), "head": head,
                       "tree_digest": digest}},
            {"id": 1, "type": "File", "key": "file:src/app.py",
             "attrs": {"path": "src/app.py"}},
            {"id": 2, "type": "File", "key": "file:src/other.py",
             "attrs": {"path": "src/other.py"}},
            {"id": 3, "type": "File", "key": "file:tests/test_app.py",
             "attrs": {"path": "tests/test_app.py"}},
            {"id": 4, "type": "Test", "key": "test:tests/test_app.py::test_add",
             "attrs": {"file": "tests/test_app.py", "qualname": "test_add"}},
            {"id": 5, "type": "File", "key": "file:README.md",
             "attrs": {"path": "README.md"}},
            {"id": 6, "type": "File", "key": "file:src/pkg/mod.py",
             "attrs": {"path": "src/pkg/mod.py"}},
        ],
        "edges": [
            {"src": 2, "type": "imports", "tgt": 1},
            {"src": 1, "type": "has_test", "tgt": 4},
            {"src": 4, "type": "tests_module", "tgt": 1},
            {"src": 3, "type": "defines", "tgt": 4},
        ],
        "next_id": 7,
        "build": {},
        "files": {},
    }


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in FILES.items():
        _write(root, rel, text)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Task Tester")
    _git(root, "config", "user.email", "task@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    base = _commit(root, "base")
    graph = root / ".ozagia" / "graph.json"
    graph.write_text(json.dumps(_graph_payload(root, base)), encoding="utf-8")
    return {"root": root, "base": base, "graph": graph}


def _step(tool: str, *, paths: tuple[str, ...] = (".",),
          args: dict | None = None) -> dict:
    return {
        "id": "step",
        "tool": tool,
        "goal": "exercise the local executor",
        "paths": list(paths),
        "write_paths": [],
        "budget": {},
        "args": dict(args or {}),
    }


def _run(root: Path, tool: str, *, paths: tuple[str, ...] = (".",),
         args: dict | None = None, policy: dict | None = None,
         graph_path: str | Path | None = None,
         context: dict | None = None, python: str | None = None) -> dict:
    executor = task_exec.LocalExecutor(
        root, policy=policy, graph_path=graph_path,
        python=python if python is not None else sys.executable)
    return executor(_step(tool, paths=paths, args=args), context or {})


def _test_plan(root: Path, files: list[str], *,
               mode: str = "affected", timeout_s: int = 60,
               **extra) -> dict:
    plan = {
        "schema": "ozagia.testing.plan.v1",
        "root": str(root),
        "mode": mode,
        "requested_mode": mode,
        "available": True,
        "refused": False,
        "selected_files": list(files),
        "timeout_s": timeout_s,
        "executed": False,
    }
    plan.update(extra)
    return plan


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_bounded_range(repo) -> None:
    root = repo["root"]
    result = _run(root, "read", paths=("src",),
                  args={"path": "src/app.py", "start": 2, "end": 3})
    assert result["status"] == "ok", result
    output = result["output"]
    assert output["path"] == "src/app.py"
    assert output["start"] == 2
    assert output["end"] == 3
    assert output["text"] == "    return a + b\n# end\n"
    assert output["truncated"] is False
    assert output["sha256"].startswith("sha256:")
    assert "return a + b" not in json.dumps(result["evidence"])
    normalized = T._normalize_result(result)
    assert normalized["status"] == "ok"
    assert normalized["has_output"] is True
    assert "sha256:" in normalized["state"]


def test_read_rejects_outside_traversal_symlink_and_state(
        repo, tmp_path: Path) -> None:
    root = repo["root"]
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    for value in ("../outside.txt", str(outside)):
        result = _run(root, "read", args={"path": value})
        assert result["status"] == "error", result
        assert result["output"]["error_code"] == "bad_path"
    link = root / "link"
    link.symlink_to(outside)
    result = _run(root, "read", args={"path": "link"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "symlink_refused"
    _write(root, ".ozagia/secret.txt", "secret\n")
    result = _run(root, "read", args={"path": ".ozagia/secret.txt"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "forbidden_path"
    result = _run(root, "read", args={"path": ".git/config"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "forbidden_path"
    result = _run(root, "read", paths=("src",),
                  args={"path": "README.md"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "outside_scope"


def test_read_uses_step_file_and_preserves_text(repo) -> None:
    root = repo["root"]
    _write(root, "src/nonl.txt", "abc")
    result = _run(root, "read", paths=("src/nonl.txt",))
    assert result["status"] == "ok", result
    assert result["output"]["text"] == "abc"
    assert result["output"]["start"] == 1
    assert result["output"]["end"] == 1
    assert result["output"]["total_lines"] == 1
    result = _run(root, "read", paths=("src",), args={"path": "app.py"})
    assert result["status"] == "ok", result
    assert result["output"]["path"] == "src/app.py"


def test_read_caps(repo) -> None:
    root = repo["root"]
    result = _run(root, "read", policy={"max_read_lines": 1},
                  args={"path": "src/app.py"})
    assert result["status"] == "ok"
    assert result["output"]["end"] == 1
    assert result["output"]["truncated"] is True
    result = _run(root, "read", policy={"max_read_bytes": 10},
                  args={"path": "src/app.py"})
    assert result["status"] == "ok"
    assert len(result["output"]["text"].encode("utf-8")) <= 10
    assert result["output"]["truncated"] is True
    result = _run(root, "read", policy={"max_file_bytes": 5},
                  args={"path": "src/app.py"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "file_too_large"


# --------------------------------------------------------------------------- #
# grep and glob
# --------------------------------------------------------------------------- #
def test_grep_literal_regex_scope_and_caps(repo) -> None:
    root = repo["root"]
    result = _run(root, "grep", args={"pattern": "needle"})
    assert result["status"] == "ok"
    assert result["output"]["count"] == 2
    assert {item["path"] for item in result["output"]["matches"]} == {
        "README.md"}
    assert "needle one" not in json.dumps(result["evidence"])
    result = _run(root, "grep", args={"query": "needle"})
    assert result["output"]["count"] == 2
    result = _run(root, "grep",
                  args={"pattern": "needle", "case_sensitive": False})
    assert result["output"]["count"] == 3
    result = _run(root, "grep",
                  args={"pattern": r"need+le", "regex": True})
    assert result["output"]["count"] == 2
    result = _run(root, "grep", paths=("src",),
                  args={"pattern": "needle"})
    assert result["output"]["count"] == 0
    result = _run(root, "grep", policy={"max_grep_matches": 1},
                  args={"pattern": "needle"})
    assert result["output"]["count"] == 1
    assert result["output"]["truncated"] is True
    result = _run(root, "grep", policy={"max_grep_bytes": 5},
                  args={"pattern": "needle"})
    assert result["output"]["bytes_scanned"] <= 5
    assert result["output"]["truncated"] is True
    result = _run(root, "grep", args={"pattern": "[" , "regex": True})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "bad_args"


def test_glob_patterns_scope_and_caps(repo) -> None:
    root = repo["root"]
    result = _run(root, "glob", args={"pattern": "**/*.py"})
    assert result["status"] == "ok"
    paths = set(result["output"]["paths"])
    assert {"src/app.py", "src/other.py", "tests/test_app.py",
            "top.py"} <= paths
    result = _run(root, "glob", args={"pattern": "*.md"})
    assert set(result["output"]["paths"]) == {"README.md"}
    result = _run(root, "glob", args={"pattern": "docs/*.md"})
    assert set(result["output"]["paths"]) == {"docs/guide.md"}
    result = _run(root, "glob", paths=("src",),
                  args={"pattern": "src/**/*.py"})
    assert set(result["output"]["paths"]) == {
        "src/__init__.py", "src/app.py", "src/other.py",
        "src/pkg/__init__.py", "src/pkg/mod.py"}
    result = _run(root, "glob", paths=("src",),
                  args={"pattern": "*.py"})
    assert set(result["output"]["paths"]) == {
        "src/__init__.py", "src/app.py", "src/other.py"}
    result = _run(root, "glob", policy={"max_glob_results": 1},
                  args={"pattern": "*.py"})
    assert result["output"]["count"] == 1
    assert result["output"]["truncated"] is True
    result = _run(root, "glob", args={"pattern": "../*.py"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "bad_args"
    result = _run(root, "glob", args={"pattern": "[z-a].py"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "bad_args"


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #
def test_graph_search_fixture_missing_and_stale(repo) -> None:
    root = repo["root"]
    result = _run(root, "graph_search", args={"query": "app"})
    assert result["status"] == "ok", result
    assert result["output"]["count"] >= 1
    assert result["output"]["graph"]["freshness"] == "fresh"
    assert any(item["type"] == "File" for item in result["output"]["results"])
    assert not (root / ".ozagia" / "tags.json").exists()
    result = _run(root, "graph_search",
                  args={"query": "app", "types": ["File"]})
    assert result["status"] == "ok"
    assert all(item["type"] == "File"
               for item in result["output"]["results"])

    graph = repo["graph"]
    original = graph.read_bytes()
    graph.unlink()
    result = _run(root, "graph_search", args={"query": "app"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "graph_missing"
    assert result["output"]["details"]["freshness"] == "missing"
    assert not graph.exists()

    graph.write_bytes(original)
    _write(root, "README.md", "# Fixture\nchanged\n")
    result = _run(root, "graph_search", args={"query": "app"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "graph_stale"
    assert graph.read_bytes() == original

    # Reusing one executor must not let DevMemory's short staleness memo hide
    # a change made after a fresh call.
    fresh = task_exec.LocalExecutor(root)
    _write(root, "README.md", FILES["README.md"])
    first = fresh(_step("graph_search", args={"query": "app"}), {})
    assert first["status"] == "ok"
    _write(root, "README.md", "# Fixture\nchanged again\n")
    second = fresh(_step("graph_search", args={"query": "app"}), {})
    assert second["status"] == "error"
    assert second["output"]["error_code"] == "graph_stale"

    result = _run(root, "graph_search", policy={"allow_graph": False},
                  args={"query": "app"})
    assert result["status"] == "blocked"


def test_graph_impact_dependents_and_tests(repo) -> None:
    root = repo["root"]
    result = _run(root, "graph_impact", args={"path": "src/app.py"})
    assert result["status"] == "ok", result
    impacted = {item["path"]: item["depth"]
                for item in result["output"]["impacted"]}
    assert impacted == {"src/other.py": 1}
    assert "tests/test_app.py" in result["output"]["tests"]
    assert result["output"]["graph"]["freshness"] == "fresh"
    result = _run(root, "graph_impact", paths=("src",),
                  args={"path": "app.py"})
    assert result["status"] == "ok", result
    assert result["output"]["file"] == "src/app.py"
    result = _run(root, "graph_impact", paths=("src",),
                  args={"path": "README.md"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "outside_scope"
    result = _run(root, "graph_impact", args={"path": "src/missing.py"})
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "file_not_indexed"


# --------------------------------------------------------------------------- #
# test tool
# --------------------------------------------------------------------------- #
def test_test_step_runs_planned_files_only(repo) -> None:
    root = repo["root"]
    plan = _test_plan(root, ["tests/test_app.py"])
    result = _run(root, "test", args={"test_plan": plan})
    assert result["status"] == "ok", result
    output = result["output"]
    assert output["exit_code"] == 0
    assert output["timed_out"] is False
    assert output["files"] == ["tests/test_app.py"]
    assert "pytest" in " ".join(output["command"])
    assert "passed" in result["summary"]
    assert result["evidence"][0]["kind"] == "test"
    assert not (root / ".pytest_cache").exists()
    assert not list(root.rglob("__pycache__"))


def test_test_step_refuses_without_a_plan(repo, monkeypatch) -> None:
    root = repo["root"]

    def boom(*args, **kwargs):
        raise AssertionError("pytest must not run")

    monkeypatch.setattr(task_exec.subprocess, "run", boom)
    cases = [
        {},
        {"test_plan": _test_plan(root, [], mode="none")},
        {"test_plan": _test_plan(root, ["tests/test_app.py"]).copy()
         | {"refused": True}},
        {"test_plan": _test_plan(root, ["tests/test_app.py"]).copy()
         | {"available": False}},
        {"test_plan": _test_plan(root, ["tests/test_app.py"]).copy()
         | {"mode": "none"}},
    ]
    for args in cases:
        result = _run(root, "test", args=args)
        assert result["status"] == "blocked", (args, result)
    result = _run(root, "test", context={
        "test_plan": _test_plan(root, [], mode="none")})
    assert result["status"] == "blocked"
    result = _run(root, "test", args={
        "test_plan": _test_plan(root, ["../outside.py"])})
    assert result["status"] == "error"
    result = _run(root, "test", args={
        "test_plan": _test_plan(root, ["README.md"])})
    assert result["status"] == "error"
    result = _run(root, "test", policy={"allow_tests": False},
                  args={"test_plan": _test_plan(
                      root, ["tests/test_app.py"])})
    assert result["status"] == "blocked"


def test_test_step_timeout(repo) -> None:
    root = repo["root"]
    _write(root, "tests/test_slow.py", (
        "import time\n"
        "\n"
        "def test_slow():\n"
        "    time.sleep(3)\n"
    ))
    plan = _test_plan(root, ["tests/test_slow.py"], timeout_s=30)
    result = _run(root, "test", policy={"test_timeout_s": 1},
                  args={"test_plan": plan})
    assert result["status"] == "error"
    assert result["output"]["timed_out"] is True
    assert result["output"]["exit_code"] is None
    assert result["output"]["timeout_s"] == 1
    assert "timed out" in result["summary"]


def test_test_step_output_cap(repo) -> None:
    root = repo["root"]
    _write(root, "tests/test_noisy.py", (
        "def test_noisy():\n"
        "    print('x' * 20000)\n"
        "    assert False\n"
    ))
    plan = _test_plan(root, ["tests/test_noisy.py"], timeout_s=60)
    result = _run(root, "test", policy={"max_test_output_bytes": 200},
                  args={"test_plan": plan})
    assert result["status"] == "ok", result
    assert result["output"]["exit_code"] == 1
    assert result["output"]["output_truncated"] is True
    assert len(result["output"]["text"].encode("utf-8")) <= 200


def test_task_runner_integration_with_local_executor(repo) -> None:
    root = repo["root"]
    spec_path = _write(root, ".ozagia/specs/integration.json", json.dumps({
        "id": "test_integration",
        "version": 1,
        "objective": "Run the planned tests through the local executor.",
        "inputs": [],
        "tools": ["test"],
        "write_scope": [],
        "requires_graph": False,
        "output": "test_report.v1",
        "worktree": {"base": "main"},
        "resources": {"memory_mb": 512, "disk_mb": 256, "pids": 32,
                      "wall_s": 120},
        "budget": {"tokens": 1000, "model_calls": 0, "cost_usd": 0.0,
                   "wall_s": 60},
        "gates": {"after": ["evidence_present"]},
        "test_policy": "all",
        "approval": {"mutate": "deny", "network": "deny"},
        "steps": [{"id": "tests", "tool": "test",
                   "goal": "Run the planned tests.", "paths": ["."]}],
    }))
    spec = T.load_spec(spec_path)
    runner = T.TaskRunner(
        spec, root, executor=task_exec.LocalExecutor(root),
        state_dir=root / ".ozagia")
    report = runner.run(allow_exec=True)
    assert report["status"] == "ok", report
    assert report["test_plan"]["mode"] == "all"
    assert report["test_plan"]["selected_files"] == ["tests/test_app.py"]
    assert report["steps"][0]["tool"] == "test"
    assert report["steps"][0]["status"] == "ok"
    assert report["evidence_count"] >= 1
    assert report["artifacts"] == []


# --------------------------------------------------------------------------- #
# blocked tools, normalisation, context and policy
# --------------------------------------------------------------------------- #
def test_blocked_mutating_model_and_shell_tools(repo, monkeypatch) -> None:
    root = repo["root"]

    def boom(*args, **kwargs):
        raise AssertionError("no process may start")

    monkeypatch.setattr(task_exec.subprocess, "run", boom)
    for tool in ("write", "edit", "patch", "shell", "model", "lint", "list"):
        result = _run(root, tool)
        assert result["status"] == "blocked", (tool, result)
        assert result["output"]["blocked"] is True
        assert tool in result["summary"]
        normalized = T._normalize_result(result)
        assert normalized["status"] == "blocked"
    result = _run(root, "not_a_tool")
    assert result["status"] == "error"
    assert result["output"]["error_code"] == "unknown_tool"


def test_context_root_mismatch_is_blocked(repo, tmp_path: Path) -> None:
    root = repo["root"]
    result = _run(root, "read", args={"path": "README.md"},
                  context={"root": str(tmp_path / "elsewhere")})
    assert result["status"] == "blocked"
    assert "does not match" in result["summary"]


def test_policy_validation(repo) -> None:
    root = repo["root"]
    with pytest.raises(ValueError):
        task_exec.LocalExecutor(root, policy={"nonsense": 1})
    with pytest.raises(ValueError):
        task_exec.LocalExecutor(root, policy={"max_read_bytes": 0})
    with pytest.raises(ValueError):
        task_exec.LocalExecutor(root, policy="not-a-policy")
    policy = task_exec.LocalPolicy(max_read_bytes=12)
    assert task_exec.LocalPolicy.from_value(policy) is policy
    assert task_exec.LocalPolicy.from_value(
        {"max_read_bytes": 12}).max_read_bytes == 12


def test_no_network_calls(repo, monkeypatch) -> None:
    root = repo["root"]

    def no_socket(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_socket)
    assert _run(root, "read", args={"path": "README.md"})["status"] == "ok"
    assert _run(root, "grep", args={"pattern": "needle"})["status"] == "ok"
    assert _run(root, "glob", args={"pattern": "*.py"})["status"] == "ok"
    assert _run(root, "graph_search",
                args={"query": "app"})["status"] == "ok"
