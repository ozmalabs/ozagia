"""Test policy, affected-test planning, CLI and task/code-review integration."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia import testing_cli
from ozagia.core import testing as T
from ozagia.core import settings as S
from ozagia.core.memory import _tree_digest

FILES = {
    "src/__init__.py": "",
    "src/app.py": "def f():\n    return 1\n",
    "src/other.py": "def g():\n    return 2\n",
    "src/pkg/__init__.py": "",
    "src/pkg/mod.py": "def h():\n    return 3\n",
    "tests/test_app.py": "def test_f():\n    assert True\n",
    "tests/test_other.py": "def test_g():\n    assert True\n",
    "tests/pkg/test_mod.py": "def test_h():\n    assert True\n",
    "README.md": "# Fixture\n",
    "docs/guide.md": "# Guide\n",
    "pyproject.toml": "[project]\nname = \"fixture\"\n",
    "ozagia/resources.toml": "enabled = false\n",
    ".gitignore": ".ozagia/\n",
}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Testing Tester",
        "GIT_AUTHOR_EMAIL": "testing@example.com",
        "GIT_COMMITTER_NAME": "Testing Tester",
        "GIT_COMMITTER_EMAIL": "testing@example.com",
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


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OZAGIA_USER_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.delenv("OZAGIA_ROOT", raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "repo"
    root.mkdir()
    for rel, text in FILES.items():
        _write(root, rel, text)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Testing Tester")
    _git(root, "config", "user.email", "testing@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    base = _commit(root, "base")
    graph = _write_graph(root, base)
    return {"root": root, "base": base, "graph": graph}


def _graph_payload(root: Path, head: str, *,
                   snapshot_head: str | None = None) -> dict[str, Any]:
    graph_path = root / ".ozagia" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    digest, error = _tree_digest(root, dump=graph_path)
    assert error is None
    assert digest
    nodes = [
        {"id": 0, "type": "Repo", "key": "repo:",
         "attrs": {"root": str(root.resolve()),
                   "head": snapshot_head or head,
                   "tree_digest": digest}},
        {"id": 1, "type": "File", "key": "file:src/app.py",
         "attrs": {"path": "src/app.py"}},
        {"id": 2, "type": "File", "key": "file:src/other.py",
         "attrs": {"path": "src/other.py"}},
        {"id": 3, "type": "File", "key": "file:src/pkg/mod.py",
         "attrs": {"path": "src/pkg/mod.py"}},
        {"id": 4, "type": "File", "key": "file:tests/test_app.py",
         "attrs": {"path": "tests/test_app.py"}},
        {"id": 5, "type": "Test", "key": "test:tests/test_app.py::test_f",
         "attrs": {"file": "tests/test_app.py", "qualname": "test_f"}},
        {"id": 6, "type": "File", "key": "file:tests/test_other.py",
         "attrs": {"path": "tests/test_other.py"}},
        {"id": 7, "type": "Test", "key": "test:tests/test_other.py::test_g",
         "attrs": {"file": "tests/test_other.py", "qualname": "test_g"}},
        {"id": 8, "type": "File", "key": "file:tests/pkg/test_mod.py",
         "attrs": {"path": "tests/pkg/test_mod.py"}},
        {"id": 9, "type": "Test", "key": "test:tests/pkg/test_mod.py::test_h",
         "attrs": {"file": "tests/pkg/test_mod.py", "qualname": "test_h"}},
    ]
    edges = [
        {"src": 1, "type": "has_test", "tgt": 5},
        {"src": 5, "type": "tests_module", "tgt": 1},
        {"src": 4, "type": "defines", "tgt": 5},
        {"src": 6, "type": "defines", "tgt": 7},
        {"src": 8, "type": "defines", "tgt": 9},
        {"src": 9, "type": "tests_module", "tgt": 3},
        {"src": 3, "type": "has_test", "tgt": 9},
    ]
    return {
        "version": 2,
        "nodes": nodes,
        "edges": edges,
        "next_id": 10,
        "build": {},
        "files": {},
    }


def _write_graph(root: Path, head: str, *,
                 snapshot_head: str | None = None) -> Path:
    path = root / ".ozagia" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_graph_payload(root, head, snapshot_head=snapshot_head)),
        encoding="utf-8")
    return path


def _repo_config(root: Path, text: str) -> Path:
    return _write(root, "ozagia/config.toml", text)


def _state(root: Path, failing: list[str],
           flaky: list[str]) -> None:
    from ozagia.intent import affected as A
    files = {path: 1.0 for path in failing}
    data = {
        "version": A.VERSION,
        "suites": {},
        "files": {},
        "nodes": {},
        "flaky": {nodeid: {"k": "key", "t": 1.0} for nodeid in flaky},
        "slow": {},
        "failing": ({"suite": {"files": files, "t": 1.0}}
                    if files else {}),
    }
    path = root / ".ozagia" / "tests" / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def test_policy_defaults(repo) -> None:
    root = repo["root"]
    policy = T.load_policy(root)
    assert policy.mode == "affected"
    assert policy.fallback == "all"
    assert policy.max_tests == 50
    assert policy.timeout_s == 300
    assert policy.captured == "all"
    assert policy.sources["mode"] == "default"
    assert T.load_policy(root, captured=True).mode == "all"


def test_policy_precedence_task_class_path_repo(repo, isolated_home) -> None:
    root = repo["root"]
    _repo_config(root, (
        "[testing]\n"
        'mode = "smoke"\n'
        'fallback = "refuse"\n'
        "max_tests = 7\n"
        "timeout_s = 42\n"
        'captured = "all"\n'
        "\n[testing.tasks]\n"
        'code_review = "none"\n'
        'diff_review = "affected"\n'
        "\n[testing.paths]\n"
        '"docs/**" = "none"\n'
        '"src/**" = "all"\n'
    ))
    user = isolated_home / ".config" / "ozagia" / "config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('[testing]\nmode = "package"\n', encoding="utf-8")
    repo_policy = T.load_policy(root)
    assert (repo_policy.mode, repo_policy.sources["mode"]) == (
        "smoke", "repo")
    assert repo_policy.fallback == "refuse"
    assert (repo_policy.max_tests, repo_policy.timeout_s) == (7, 42)

    task = T.load_policy(root, task_class="code_review")
    assert (task.mode, task.sources["mode"]) == ("none", "task_class")
    path = T.load_policy(root, paths=["docs/guide.md"])
    assert (path.mode, path.sources["mode"]) == ("none", "path")
    assert T.load_policy(root, paths=["docs"]).mode == "none"
    wider = T.load_policy(root, paths=["src/app.py"])
    assert (wider.mode, wider.sources["mode"]) == ("all", "path")
    # A task class wins over a path override.
    assert T.load_policy(
        root, task_class="code_review", paths=["src/app.py"]).mode == "none"
    # A task specification wins over the task class.
    spec = T.load_policy(root, task_class="code_review",
                         task_mode="affected")
    assert (spec.mode, spec.sources["mode"]) == ("affected", "task")
    assert T.load_policy(root, captured=True).mode == "all"

    # The user value is visible when the repository does not set one.
    _repo_config(root, "[testing]\nmax_tests = 9\n")
    assert T.load_policy(root).mode == "package"


def test_policy_environment_override(repo, monkeypatch) -> None:
    root = repo["root"]
    _repo_config(root, '[testing]\nmode = "none"\nmax_tests = 3\n')
    monkeypatch.setenv("OZAGIA_TESTING_MODE", "affected")
    monkeypatch.setenv("OZAGIA_TESTING_MAX_TESTS", "9")
    policy = T.load_policy(root)
    assert (policy.mode, policy.sources["mode"]) == ("affected", "env")
    assert policy.max_tests == 9


def test_policy_invalid_values(repo) -> None:
    root = repo["root"]
    with pytest.raises(T.TestPolicyError, match="mode"):
        T.load_policy(root, task_mode="sometimes")
    policy = T.load_policy(root)
    with pytest.raises(T.TestPolicyError, match="max_tests"):
        T.with_overrides(policy, max_tests=0)
    with pytest.raises(T.TestPolicyError, match="fallback"):
        T.with_overrides(policy, fallback="sometimes")

    _repo_config(root, '[testing]\nfallback = "sometimes"\n')
    with pytest.raises(S.SettingsError, match="testing.fallback"):
        T.load_policy(root)
    _repo_config(root, '[testing.tasks]\ncode_review = "sometimes"\n')
    with pytest.raises(S.SettingsError, match="testing.tasks"):
        T.load_policy(root)
    _repo_config(root, '[testing.paths]\n"../escape" = "none"\n')
    with pytest.raises(S.SettingsError, match="testing.paths"):
        T.load_policy(root)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def test_plan_links_changed_source_to_graph_test(repo) -> None:
    plan = T.plan(repo["root"], changed=["src/app.py"])
    assert plan["mode"] == "affected"
    assert plan["selected_files"] == ["tests/test_app.py"]
    assert plan["selected_tests"] == [{
        "file": "tests/test_app.py",
        "node_ids": ["test:tests/test_app.py::test_f"],
        "sources": ["graph", "package"],
    }]
    assert plan["graph"]["freshness"] == "fresh"
    assert plan["graph"]["usable"] is True
    assert plan["executed"] is False
    assert plan["truth_assessed"] is False
    assert "no test was executed" in plan["claim"]
    assert plan["counts"]["selected_files"] == 1


def test_plan_package_mode_selects_package_tests(repo) -> None:
    policy = T.with_overrides(T.load_policy(repo["root"]), mode="package")
    plan = T.plan(repo["root"], changed=["src/pkg/mod.py"], policy=policy)
    assert plan["mode"] == "package"
    assert plan["selected_files"] == ["tests/pkg/test_mod.py"]
    assert "package" in plan["selected_tests"][0]["sources"]


def test_plan_docs_or_config_only_selects_none(repo) -> None:
    for changed in (["README.md", "docs/guide.md"], ["pyproject.toml"]):
        plan = T.plan(repo["root"], changed=changed)
        assert plan["mode"] == "none"
        assert plan["selected_files"] == []
        assert plan["counts"]["selected_files"] == 0
        assert "documentation and configuration" in plan["mode_reason"]
        assert plan["executed"] is False

    # An explicit `all` is the command as typed, even for a docs-only change.
    policy = T.with_overrides(T.load_policy(repo["root"]), mode="all")
    plan = T.plan(repo["root"], changed=["README.md"], policy=policy)
    assert plan["mode"] == "all"
    assert "tests/test_app.py" in plan["selected_files"]


def test_plan_unlinked_change_uses_fallback(repo) -> None:
    root = repo["root"]
    plan = T.plan(root, changed=["src/other.py"])
    assert plan["mode"] == "all"
    assert plan["fallback_used"] is True
    assert "no test is linked to src/other.py" in plan["fallback_reason"]
    assert "tests/test_app.py" in plan["selected_files"]
    assert "tests/test_other.py" in plan["selected_files"]
    assert any("no test was executed" in line for line in plan["not_assessed"])

    _repo_config(root, '[testing]\nfallback = "refuse"\n')
    refused = T.plan(root, changed=["src/other.py"])
    assert refused["refused"] is True
    assert refused["mode"] == "affected"
    assert refused["selected_files"] == []
    assert refused["fallback_used"] is True
    assert any("refused" in line for line in refused["not_assessed"])

    _repo_config(root, '[testing]\nfallback = "affected_with_warning"\n')
    warning = T.plan(root, changed=["src/other.py"])
    assert warning["mode"] == "affected"
    assert warning["fallback_used"] is True
    assert warning["warnings"]
    assert warning["selected_files"]
    assert any("best-effort" in line
               for line in warning["not_assessed"])


def test_plan_max_tests_cap_and_mandatory_tests(repo) -> None:
    root = repo["root"]
    all_policy = T.with_overrides(T.load_policy(root), mode="all")
    plan = T.plan(root, changed=["src/app.py"], policy=all_policy, max_tests=1)
    assert plan["counts"]["max_tests"] == 1
    assert len(plan["selected_files"]) == 1
    assert plan["counts"]["omitted_files"] >= 1
    assert any(item["reason"] == "max_tests=1 cap"
               for item in plan["omitted"])
    assert plan["counts"]["cap_exceeded"] is True

    _state(root, ["tests/test_other.py"], ["tests/pkg/test_mod.py::test_h"])
    mandatory = T.plan(root, changed=["src/app.py"], max_tests=1)
    assert {"tests/test_other.py", "tests/pkg/test_mod.py"} <= set(
        mandatory["selected_files"])
    assert mandatory["counts"]["cap_exceeded"] is True
    assert not any(item["file"] in {"tests/test_other.py",
                                    "tests/pkg/test_mod.py"}
                   for item in mandatory["omitted"])
    assert "tests/test_app.py" in [item["file"]
                                   for item in mandatory["omitted"]]


def test_plan_failed_and_flaky_tests_are_never_skipped(repo) -> None:
    root = repo["root"]
    _state(root, ["tests/test_other.py"], ["tests/pkg/test_mod.py::test_h"])
    plan = T.plan(root, changed=["src/app.py"])
    assert plan["test_results"]["available"] is True
    assert plan["test_results"]["failing"] == ["tests/test_other.py"]
    assert plan["test_results"]["flaky"] == ["tests/pkg/test_mod.py"]
    assert {"tests/test_app.py", "tests/test_other.py",
            "tests/pkg/test_mod.py"} <= set(plan["selected_files"])
    sources = {item["file"]: item["sources"]
               for item in plan["selected_tests"]}
    assert "failed_last_run" in sources["tests/test_other.py"]
    assert "flaky" in sources["tests/pkg/test_mod.py"]


def test_plan_missing_or_stale_graph_falls_back(repo) -> None:
    root = repo["root"]
    graph = repo["graph"]
    graph.unlink()
    missing = T.plan(root, changed=["src/app.py"])
    assert missing["mode"] == "all"
    assert missing["graph"]["freshness"] == "missing"
    assert missing["fallback_used"] is True
    assert "graph snapshot is missing" in missing["fallback_reason"]
    assert any("affected coverage" in line
               for line in missing["not_assessed"])

    _write_graph(root, repo["base"])
    _write(root, "src/app.py", "def f():\n    return 99\n")
    stale = T.plan(root, changed=["src/app.py"])
    assert stale["graph"]["freshness"] == "stale"
    assert stale["graph"]["usable"] is False
    assert stale["mode"] == "all"
    assert stale["fallback_used"] is True
    assert "stale" in stale["fallback_reason"]


def test_plan_is_deterministic(repo) -> None:
    first = T.plan(repo["root"], changed=["src/app.py"])
    second = T.plan(repo["root"], changed=["src/app.py"])
    assert first == second


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def test_settings_testing_keys_and_dynamic_tables(repo) -> None:
    root = repo["root"]
    assert S.get(root, "testing.mode")["value"] == "affected"
    assert S.get(root, "testing.fallback")["value"] == "all"
    assert S.get(root, "testing.max_tests")["value"] == 50
    assert S.get(root, "testing.timeout_s")["value"] == 300
    assert S.get(root, "testing.captured")["value"] == "all"

    S.set_value(root, "testing.tasks.code_review", "none", dry_run=False)
    S.set_value(root, "testing.paths.docs/**", "none", dry_run=False)
    text = (root / "ozagia" / "config.toml").read_text(encoding="utf-8")
    assert "[testing.tasks]" in text
    assert "[testing.paths]" in text
    assert '"docs/**" = "none"' in text
    assert S.get(root, "testing.tasks.code_review")["value"] == "none"
    assert S.get(root, "testing.paths.docs/**")["source"] == "repo"
    assert S.testing_overrides(root) == {
        "tasks": {"code_review": "none"},
        "paths": {"docs/**": "none"},
    }
    assert S.values(root)["testing.tasks.code_review"]["value"] == "none"

    with pytest.raises(S.SettingsError, match="testing.tasks"):
        S.set_value(root, "testing.tasks.code_review", "sometimes",
                    dry_run=False)


# --------------------------------------------------------------------------- #
# task integration
# --------------------------------------------------------------------------- #
def _task_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "task-root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Testing Tester")
    _git(root, "config", "user.email", "testing@example.com")
    _write(root, "README.md", "# Task\n")
    _write(root, "ozagia/resources.toml", "[resources]\nenabled = false\n")
    _write(root, ".gitignore", ".ozagia/\n")
    _write(root, "tests/test_x.py", "def test_x():\n    pass\n")
    _commit(root, "initial")
    worktree = tmp_path / "ozagia-wt" / "alpha"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", "feat/alpha", str(worktree))
    return root, worktree


def _task_doc(tmp_path: Path, *, test_policy: Any = None,
              steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": "code_review",
        "version": 1,
        "title": "Code review",
        "objective": "Review a diff without writing.",
        "inputs": ["diff"],
        "tools": ["read", "test"],
        "write_scope": [],
        "requires_graph": False,
        "output": "review_packet.v1",
        "worktree": {"base": "main", "require_clean": True},
        "resources": {"memory_mb": 512, "disk_mb": 256, "pids": 32,
                      "wall_s": 120},
        "budget": {"tokens": 1000, "model_calls": 0, "cost_usd": 0.0,
                   "wall_s": 60},
        "gates": {"after": ["schema_valid", "evidence_present"]},
        "approval": {"mutate": "deny", "network": "deny"},
        "output_schema": {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string", "minLength": 1}},
        },
        "steps": steps or [
            {"id": "read", "tool": "read", "goal": "Read the diff.",
             "paths": ["."]},
            {"id": "run_tests", "tool": "test", "goal": "Run the planned tests.",
             "paths": ["tests"]},
        ],
    }
    if test_policy is not None:
        doc["test_policy"] = test_policy
    return doc


def _write_spec(tmp_path: Path, doc: dict[str, Any], name: str = "task.json") -> Path:
    path = tmp_path / ".ozagia" / "specs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class FakeExecutor:
    def __init__(self, handler=None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.handler = handler or self.default

    def default(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "summary": f"step {step['id']}",
            "usage": {"tokens": 1},
            "evidence": [f"checked {step['id']}"],
            "output": {"summary": "ok"},
        }

    def __call__(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"step": step, "context": context})
        return self.handler(step, context)


def test_tasks_preflight_records_none_plan_and_skips_test_step(
        tmp_path: Path) -> None:
    from ozagia.core import tasks as Task

    _root, worktree = _task_repo(tmp_path)
    spec = Task.load_spec(_write_spec(
        tmp_path, _task_doc(tmp_path, test_policy="none")))
    fake = FakeExecutor()
    runner = Task.TaskRunner(spec, worktree, executor=fake)
    preflight = runner.preflight()
    assert preflight["ok"] is True, preflight["failures"]
    assert preflight["test_plan"]["mode"] == "none"
    assert preflight["test_plan"]["executed"] is False

    report = runner.run({"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok", report
    assert [call["step"]["id"] for call in fake.calls] == ["read"]
    assert report["steps"][1]["status"] == "skipped"
    assert report["steps"][1]["skipped"] is True
    assert "test policy is none" in report["steps"][1]["reason"]
    manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["test_plan"]["mode"] == "none"
    event_types = [json.loads(line)["type"]
                   for line in Path(report["events"]).read_text(
                       encoding="utf-8").splitlines()]
    assert "step_skipped" in event_types
    assert any("test steps were skipped" in item
               for item in report["what_was_not_assessed"])


def test_tasks_test_step_receives_the_plan(tmp_path: Path) -> None:
    from ozagia.core import tasks as Task

    _root, worktree = _task_repo(tmp_path)
    spec = Task.load_spec(_write_spec(
        tmp_path, _task_doc(tmp_path, test_policy={"mode": "all"}),
        name="all-tests.json"))
    fake = FakeExecutor()
    report = Task.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok", report
    test_call = next(call for call in fake.calls
                     if call["step"]["id"] == "run_tests")
    plan = test_call["step"]["args"]["test_plan"]
    assert plan["mode"] == "all"
    assert "tests/test_x.py" in plan["selected_files"]
    assert test_call["context"]["test_plan"] == plan
    assert report["test_plan"]["mode"] == "all"


def test_tasks_spec_test_policy_validation(tmp_path: Path) -> None:
    from ozagia.core import tasks as Task

    report = Task.validate_spec(_write_spec(
        tmp_path, _task_doc(tmp_path, test_policy="sometimes"),
        name="bad-mode.json"))
    assert report["ok"] is False
    assert any("test_policy.mode" in error for error in report["errors"])
    report = Task.validate_spec(_write_spec(
        tmp_path, _task_doc(tmp_path, test_policy={"unknown": 1}),
        name="bad-key.json"))
    assert report["ok"] is False
    assert any("unknown key" in error for error in report["errors"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_policy_and_plan_json(repo, capsys) -> None:
    root = repo["root"]
    assert testing_cli.main(["policy", "--root", str(root), "--json"]) == 0
    policy = json.loads(capsys.readouterr().out)
    assert policy["policy"]["mode"] == "affected"

    assert testing_cli.main([
        "plan", "--root", str(root), "--changed", "src/app.py", "--json",
    ]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "affected"
    assert plan["selected_files"] == ["tests/test_app.py"]
    assert plan["truth_assessed"] is False

    _repo_config(root, '[testing]\nfallback = "refuse"\n')
    assert testing_cli.main([
        "plan", "--root", str(root), "--changed", "src/other.py", "--json",
    ]) == 0
    refused = json.loads(capsys.readouterr().out)
    assert refused["refused"] is True


def test_cli_text_and_errors(repo, tmp_path, capsys) -> None:
    root = repo["root"]
    assert testing_cli.main([
        "plan", "--root", str(root), "--changed", "src/app.py",
    ]) == 0
    out = capsys.readouterr().out
    assert "test plan: mode=affected" in out
    assert "truth_assessed: false" in out

    assert testing_cli.main([
        "plan", "--root", str(tmp_path / "missing"), "--changed", "src/app.py",
    ]) == 2
    assert "error:" in capsys.readouterr().err

    assert testing_cli.main([
        "plan", "--root", str(root), "--base", "not-a-revision",
    ]) == 2
    assert "error:" in capsys.readouterr().err

    assert testing_cli.main([
        "plan", "--root", str(root), "--changed", "src/app.py",
        "--max-tests", "0",
    ]) == 2
