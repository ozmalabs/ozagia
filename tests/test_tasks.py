"""Bounded task specifications, the deterministic runner and evidence."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from ozagia import tasks_cli
from ozagia.core import tasks as T


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env=env)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _write(root / "README.md", "# Test\n")
    _write(root / "ozagia" / "resources.toml",
           "[resources]\nenabled = false\n")
    _write(root / ".gitignore", ".ozagia/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    worktree = tmp_path / "ozagia-wt" / "alpha"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", "feat/alpha", str(worktree))
    return root, worktree


def _merge(base: dict, update: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _doc(**overrides) -> dict:
    doc = {
        "id": "code_review",
        "version": 1,
        "title": "Code review",
        "objective": "Review a diff without writing.",
        "inputs": ["diff"],
        "tools": ["read", "grep"],
        "write_scope": [],
        "requires_graph": False,
        "output": "review_packet.v1",
        "worktree": {"base": "main", "require_clean": True},
        "resources": {"memory_mb": 512, "disk_mb": 256, "pids": 32,
                      "wall_s": 120},
        "budget": {"tokens": 1000, "model_calls": 0, "cost_usd": 0.0,
                   "wall_s": 60},
        "gates": {
            "before": ["worktree_expected", "branch_expected", "drift_clear",
                       "resources_fit"],
            "after": ["schema_valid", "evidence_present"],
        },
        "stop": ["gate_failed", "budget_exhausted", "resource_limit",
                 "no_progress", "repeated_state"],
        "approval": {"mutate": "deny", "network": "deny"},
        "output_schema": {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string", "minLength": 1}},
        },
        "steps": [
            {"id": "read", "tool": "read", "goal": "Read the diff.",
             "paths": ["."]},
            {"id": "report", "tool": "grep", "goal": "Report findings.",
             "paths": ["."]},
        ],
    }
    return _merge(doc, overrides)


def _write_spec(tmp_path: Path, doc: dict | None = None,
                name: str = "task.json") -> Path:
    # Specs live under the ignored state directory so the fixture worktree
    # stays clean for `worktree.require_clean`.
    path = tmp_path / ".ozagia" / "specs" / name
    _write(path, json.dumps(doc if doc is not None else _doc()))
    return path


class FakeExecutor:
    def __init__(self, handler=None) -> None:
        self.calls: list[dict] = []
        self.handler = handler or self.default

    def default(self, step: dict, context: dict) -> dict:
        return {
            "status": "ok",
            "summary": f"step {step['id']}",
            "usage": {"tokens": 10},
            "evidence": [f"checked {step['id']}"],
            "output": {"summary": "ok"},
        }

    def __call__(self, step: dict, context: dict) -> dict:
        self.calls.append({"step": step, "context": context})
        return self.handler(step, context)


def _event_types(path: Path) -> list[str]:
    return [json.loads(line)["type"]
            for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# spec validation
# --------------------------------------------------------------------------- #
def test_examples_validate() -> None:
    examples = Path(__file__).resolve().parent.parent / "examples" / "tasks"
    specs = sorted(examples.glob("*.toml"))
    assert len(specs) >= 3
    for path in specs:
        report = T.validate_spec(path)
        assert report["ok"], (path, report["errors"])
        assert report["spec"]["id"]


def test_load_toml_and_digest_is_stable() -> None:
    path = (Path(__file__).resolve().parent.parent / "examples" / "tasks"
            / "code_review_dry_run.toml")
    first = T.load_spec(path)
    second = T.load_spec(path)
    assert first.id == "code_review"
    assert first.version == 1
    assert first.requires_graph is True
    assert "snapshot_fresh" in first.gates.before
    assert first.output_schema["type"] == "object"
    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")


def test_json_spec_summary(tmp_path: Path) -> None:
    spec = T.load_spec(_write_spec(tmp_path))
    assert spec.inputs == ("diff",)
    assert spec.steps[0].id == "read"
    assert spec.resources.memory_mb == 512
    assert spec.summary()["digest"] == spec.digest


def test_unknown_keys_and_types_rejected(tmp_path: Path) -> None:
    report = T.validate_spec(_write_spec(tmp_path, _doc(nonsense=1)))
    assert report["ok"] is False
    assert any("unknown key" in error for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path, _doc(version="1"), name="bad-version.json"))
    assert report["ok"] is False
    assert any("version" in error and "integer" in error
               for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path, _doc(budget={"tokens": -1}),
                    name="bad-budget.json"))
    assert report["ok"] is False
    assert any("budget.tokens" in error for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path, _doc(steps=[]), name="no-steps.json"))
    assert report["ok"] is False
    assert any("steps" in error for error in report["errors"])


def test_empty_id_title_and_objective_rejected(tmp_path: Path) -> None:
    report = T.validate_spec(
        _write_spec(tmp_path, _doc(id="", title="", objective=""),
                    name="empty.json"))
    assert report["ok"] is False
    joined = "\n".join(report["errors"])
    assert "id:" in joined
    assert "title:" in joined
    assert "objective:" in joined


def test_unknown_tool_and_gate_rejected(tmp_path: Path) -> None:
    report = T.validate_spec(
        _write_spec(tmp_path, _doc(tools=["rm"], steps=[]),
                    name="tool.json"))
    assert any("unknown tool" in error for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path,
                    _doc(gates={"before": ["schema_valid"]}),
                    name="phase.json"))
    assert any("only valid in the after phase" in error
               for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path,
                    _doc(gates={"after": ["not_a_gate"]}),
                    name="gate.json"))
    assert any("unknown gate" in error for error in report["errors"])


def test_path_scope_containment(tmp_path: Path) -> None:
    for index, scope in enumerate(("../escape", "/absolute/path")):
        report = T.validate_spec(
            _write_spec(tmp_path, _doc(write_scope=[scope]),
                        name=f"scope-{index}.json"))
        assert report["ok"] is False
        assert any("write_scope" in error for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path,
                    _doc(write_scope=["out"],
                         steps=[{"id": "write", "tool": "write",
                                 "goal": "Write.", "write_paths": ["other"]}]),
                    name="step-scope.json"))
    assert any("outside write_scope" in error for error in report["errors"])
    report = T.validate_spec(
        _write_spec(tmp_path,
                    _doc(tools=["write"], write_scope=[],
                         steps=[{"id": "write", "tool": "write",
                                 "goal": "Write."}]),
                    name="no-write-scope.json"))
    assert any("requires a write_scope" in error for error in report["errors"])


def test_output_schema_subset_rejected(tmp_path: Path) -> None:
    report = T.validate_spec(
        _write_spec(tmp_path,
                    _doc(output_schema={"type": "object", "oneOf": []}),
                    name="schema.json"))
    assert report["ok"] is False
    assert any("unsupported schema keyword" in error
               for error in report["errors"])


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def test_preflight_in_feature_worktree(repo) -> None:
    _root, worktree = repo
    runner = T.TaskRunner(T.load_spec(_write_spec(worktree)), worktree)
    report = runner.preflight()
    assert report["ok"] is True, report["failures"]
    assert report["worktree"]["branch"] == "feat/alpha"
    assert report["worktree"]["expected"]["branch"] == "feat/alpha"
    assert report["paths"]["ok"] is True
    assert report["resources"]["ok"] is True


def test_preflight_refuses_wrong_branch(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(worktree={"expected_branch": "feat/beta"}),
        name="wrong-branch.json"))
    report = T.TaskRunner(spec, worktree).preflight()
    assert report["ok"] is False
    assert any("expected branch 'feat/beta'" in failure
               for failure in report["failures"])


def test_preflight_refuses_symlink_escape(repo, tmp_path: Path) -> None:
    _root, worktree = repo
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "link").symlink_to(outside)
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(write_scope=["link"],
             worktree={"require_clean": False}),
        name="escape.json"))
    report = T.TaskRunner(spec, worktree).preflight()
    assert report["ok"] is False
    assert any("write_scope 'link'" in failure
               for failure in report["failures"])


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def test_run_refuses_before_executor(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(worktree={"expected_branch": "feat/beta"}),
        name="refuse.json"))
    fake = FakeExecutor()
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "refused"
    assert report["stop"] == "gate_failed"
    assert fake.calls == []
    assert Path(report["events"]).is_file()
    manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "refused"
    assert manifest["stop"] == "gate_failed"


def test_run_without_executor_is_refused(repo) -> None:
    _root, worktree = repo
    runner = T.TaskRunner(T.load_spec(_write_spec(worktree)), worktree)
    report = runner.run({"diff": "x"}, allow_exec=True)
    assert report["status"] == "refused"
    assert report["stop"] == "error"
    assert "no executor" in report["stop_detail"]
    assert report["executed"] is False


def test_run_injected_executor_and_evidence(repo) -> None:
    _root, worktree = repo
    fake = FakeExecutor()
    runner = T.TaskRunner(T.load_spec(_write_spec(worktree)), worktree,
                          executor=fake)
    report = runner.run({"diff": "hello"}, allow_exec=True)
    assert report["status"] == "ok", report
    assert report["ok"] is True
    assert len(fake.calls) == 2
    assert [call["step"]["id"] for call in fake.calls] == ["read", "report"]
    assert fake.calls[0]["context"]["inputs"] == {"diff": "hello"}
    assert report["output"]["schema_valid"] is True
    assert report["output"]["digest"].startswith("sha256:")
    assert report["evidence_count"] == 2
    assert Path(report["artifacts_manifest"]).is_file()
    assert json.loads(Path(report["manifest"]).read_text(
        encoding="utf-8"))["status"] == "ok"
    assert json.loads(Path(report["run_dir"], "output.json").read_text(
        encoding="utf-8")) == {"summary": "ok"}
    event_types = _event_types(Path(report["events"]))
    for expected in ("run_started", "preflight", "step_started",
                     "step_finished", "evidence", "gate", "run_finished"):
        assert expected in event_types
    assert report["what_was_not_assessed"]
    assert any("not enforced" in item
               for item in report["what_was_not_assessed"])


def test_budget_token_stop(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree, _doc(budget={"tokens": 5}), name="tokens.json"))
    fake = FakeExecutor(lambda step, context: {
        "status": "ok", "summary": "too many", "usage": {"tokens": 10},
    })
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "stopped"
    assert report["stop"] == "budget_exhausted"
    assert len(fake.calls) == 1
    assert report["budget"]["used"]["tokens"] == 10


def test_step_budget_is_enforced(repo) -> None:
    _root, worktree = repo
    steps = _doc()["steps"]
    steps[0]["budget"] = {"tokens": 3}
    spec = T.load_spec(_write_spec(
        worktree, _doc(budget={"tokens": 1000}, steps=steps),
        name="step-budget.json"))
    fake = FakeExecutor(lambda step, context: {
        "status": "ok", "summary": "too many", "usage": {"tokens": 10},
    })
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "stopped"
    assert report["stop"] == "budget_exhausted"
    assert "step budget" in report["stop_detail"]
    assert len(fake.calls) == 1


def test_resource_memory_stop(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(resources={"memory_mb": 100}),
        name="memory.json"))
    fake = FakeExecutor(lambda step, context: {
        "status": "ok", "summary": "large", "usage": {"memory_mb": 200},
    })
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "stopped"
    assert report["stop"] == "resource_limit"
    assert report["budget"]["used"]["memory_mb"] == 200


def test_no_progress_and_repeated_state(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(worktree))
    no_progress = FakeExecutor(lambda step, context: {
        "status": "no_progress", "summary": "nothing",
    })
    report = T.TaskRunner(spec, worktree, executor=no_progress).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "stopped"
    assert report["stop"] == "no_progress"
    assert len(no_progress.calls) == 1

    repeated = FakeExecutor(lambda step, context: {
        "status": "ok", "summary": "same", "usage": {"tokens": 0},
    })
    report = T.TaskRunner(spec, worktree, executor=repeated).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "stopped"
    assert report["stop"] == "repeated_state"
    assert len(repeated.calls) == 2


def test_approval_required_and_auto(repo) -> None:
    _root, worktree = repo
    doc = _doc(tools=["write"], write_scope=["out"],
               approval={"mutate": "ask"},
               steps=[{"id": "write", "tool": "write", "goal": "Write.",
                       "write_paths": ["out"]}])
    spec = T.load_spec(_write_spec(worktree, doc, name="approval.json"))

    def handler(step, context):
        return {"status": "ok", "summary": "wrote",
                "usage": {"tokens": 1}, "evidence": ["wrote"],
                "output": {"summary": "ok"}}

    denied = FakeExecutor(handler)
    report = T.TaskRunner(spec, worktree, executor=denied).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "stopped"
    assert report["stop"] == "approval_required"
    assert denied.calls == []

    approved = FakeExecutor(handler)
    report = T.TaskRunner(spec, worktree, executor=approved).run(
        {"diff": "x"}, allow_exec=True, approve=True)
    assert report["status"] == "ok", report
    assert len(approved.calls) == 1

    deny_spec = T.load_spec(_write_spec(
        worktree, _doc(tools=["write"], write_scope=["out"],
                       approval={"mutate": "deny"},
                       steps=[{"id": "write", "tool": "write",
                               "goal": "Write.", "write_paths": ["out"]}]),
        name="deny.json"))
    report = T.TaskRunner(deny_spec, worktree,
                          executor=FakeExecutor(handler)).run(
        {"diff": "x"}, allow_exec=True, approve=True)
    assert report["stop"] == "approval_required"


def test_artifact_manifest_and_no_content(repo, tmp_path: Path) -> None:
    _root, worktree = repo
    secret_body = "TOP SECRET FILE BODY"
    doc = _doc(tools=["write"], write_scope=["artifacts"],
               approval={"mutate": "auto"},
               steps=[{"id": "write", "tool": "write", "goal": "Write.",
                       "write_paths": ["artifacts"]}])
    spec = T.load_spec(_write_spec(worktree, doc, name="artifact.json"))

    def handler(step, context):
        target = Path(context["root"]) / "artifacts" / "report.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(secret_body, encoding="utf-8")
        return {"status": "ok", "summary": "wrote report",
                "usage": {"tokens": 2}, "evidence": ["wrote report"],
                "artifacts": [{"path": "artifacts/report.json",
                               "kind": "report"}],
                "output": {"summary": "ok"}}

    report = T.TaskRunner(spec, worktree,
                          executor=FakeExecutor(handler)).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok", report
    assert len(report["artifacts"]) == 1
    artifact = report["artifacts"][0]
    expected = "sha256:" + hashlib.sha256(secret_body.encode()).hexdigest()
    assert artifact["sha256"] == expected
    assert artifact["size"] == len(secret_body)
    manifest = json.loads(Path(report["artifacts_manifest"]).read_text(
        encoding="utf-8"))
    assert manifest["count"] == 1
    for path in (report["events"], report["manifest"],
                 report["artifacts_manifest"]):
        assert secret_body not in Path(path).read_text(encoding="utf-8")


def test_artifact_outside_root_is_refused(repo, tmp_path: Path) -> None:
    _root, worktree = repo
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(tools=["read"], steps=[{"id": "read", "tool": "read",
                                     "goal": "Read.", "paths": ["."]}]),
        name="outside.json"))

    def handler(step, context):
        return {"status": "ok", "summary": "bad",
                "artifacts": [str(outside)],
                "output": {"summary": "ok"}}

    report = T.TaskRunner(spec, worktree,
                          executor=FakeExecutor(handler)).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "failed"
    assert report["stop"] == "error"
    assert "outside the repository root" in report["stop_detail"]


def test_artifact_outside_write_scope_is_refused(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(write_scope=["out"],
             steps=[{"id": "read", "tool": "read", "goal": "Read.",
                     "paths": ["."]}]),
        name="scope-artifact.json"))

    def handler(step, context):
        return {"status": "ok", "summary": "bad",
                "artifacts": ["README.md"],
                "output": {"summary": "ok"}}

    report = T.TaskRunner(spec, worktree,
                          executor=FakeExecutor(handler)).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "failed"
    assert "outside the declared write_scope" in report["stop_detail"]


def test_schema_gate_failure(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(steps=[{"id": "report", "tool": "read", "goal": "Report.",
                     "paths": ["."]}]),
        name="schema-fail.json"))
    fake = FakeExecutor(lambda step, context: {
        "status": "ok", "summary": "bad output", "usage": {"tokens": 1},
        "evidence": ["checked"], "output": {"summary": 5},
    })
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "failed"
    assert report["stop"] == "gate_failed"
    assert report["output"]["schema_valid"] is False
    assert any("output does not match the schema" in gate["message"]
               for gate in report["gates"]["after"])


def test_policy_commit_gate(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree,
        _doc(gates={"after": ["schema_valid", "evidence_present",
                              "policy_commit"]}),
        name="policy.json"))
    report = T.TaskRunner(spec, worktree,
                          executor=FakeExecutor()).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok", report
    policy = [gate for gate in report["gates"]["after"]
              if gate["id"] == "policy_commit"]
    assert policy and policy[0]["ok"] is True


def test_snapshot_gate_requires_fresh_graph(repo) -> None:
    _root, worktree = repo
    stale = T.load_spec(_write_spec(
        worktree,
        _doc(requires_graph=True, steps=[
            {"id": "graph", "tool": "read", "goal": "Graph.", "paths": ["."]}
        ]),
        name="graph.json"))
    preflight = T.TaskRunner(stale, worktree).preflight()
    assert preflight["ok"] is False
    assert preflight["snapshot"]["state"] == "missing"
    assert any("graph snapshot is missing" in failure
               for failure in preflight["failures"])

    not_required = T.load_spec(_write_spec(
        worktree,
        _doc(gates={"after": ["schema_valid", "evidence_present",
                              "snapshot_fresh"]}),
        name="no-graph.json"))
    report = T.TaskRunner(not_required, worktree,
                          executor=FakeExecutor()).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok"
    snapshot = [gate for gate in report["gates"]["after"]
                if gate["id"] == "snapshot_fresh"]
    assert snapshot and snapshot[0]["level"] == "not_checked"


def test_inputs_required_builtins_and_unknown(repo) -> None:
    _root, worktree = repo
    spec = T.load_spec(_write_spec(
        worktree, _doc(inputs=["diff", "repo_root"]), name="inputs.json"))
    runner = T.TaskRunner(spec, worktree)
    missing = runner.preview({})
    assert missing["ok"] is False
    assert any("missing required input" in error
               for error in missing["errors"])
    preview = runner.preview({"diff": "x"})
    assert preview["ok"] is True
    assert preview["inputs"]["names"] == ["diff", "repo_root"]
    assert preview["inputs"]["digests"]["diff"].startswith("sha256:")
    unknown = runner.preview({"diff": "x", "bogus": "y"})
    assert any("unknown input" in error for error in unknown["errors"])


def test_no_content_leakage(repo, tmp_path: Path) -> None:
    _root, worktree = repo
    secret_prompt = "SECRET PROMPT DO NOT LOG"
    secret_body = "TOP SECRET FILE BODY"
    _write(worktree / ".ozagia" / "notes.txt", secret_body)
    spec = T.load_spec(_write_spec(worktree, name="leak.json"))
    fake = FakeExecutor(lambda step, context: {
        "status": "ok", "summary": f"done {step['id']}",
        "usage": {"tokens": 1}, "evidence": ["checked diff"],
        "output": {"summary": "ok"},
    })
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": secret_prompt}, allow_exec=True)
    assert report["status"] == "ok"
    for path in (report["events"], report["manifest"],
                 report["artifacts_manifest"],
                 str(Path(report["run_dir"], "output.json"))):
        text = Path(path).read_text(encoding="utf-8")
        assert secret_prompt not in text
        assert secret_body not in text
    preview = T.TaskRunner(spec, worktree).preview({"diff": secret_prompt})
    assert secret_prompt not in json.dumps(preview)


def test_list_and_show(repo) -> None:
    _root, worktree = repo
    runner = T.TaskRunner(T.load_spec(_write_spec(worktree)), worktree,
                          executor=FakeExecutor())
    report = runner.run({"diff": "x"}, allow_exec=True)
    runs = T.list_runs(worktree)
    assert len(runs) == 1
    assert runs[0]["run_id"] == report["run_id"]
    assert runs[0]["status"] == "ok"
    shown = T.show_run(worktree, report["run_id"])
    assert shown["state"] == "complete"
    assert shown["manifest"]["run_id"] == report["run_id"]
    assert shown["output"] == {"summary": "ok"}
    assert shown["events"]
    with pytest.raises(T.TaskRunError):
        T.show_run(worktree, "../escape")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_validate_and_preview(repo, tmp_path: Path, capsys) -> None:
    _root, worktree = repo
    spec_path = _write_spec(worktree, name="cli.json")
    assert tasks_cli.main(["validate", str(spec_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True

    assert tasks_cli.main([
        "run", str(spec_path), "--root", str(worktree),
        "--input", "diff=x", "--json",
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "preview"
    assert preview["executed"] is False
    assert preview["would_execute"] is True
    assert not T.tasks_dir(worktree).exists()

    fake = FakeExecutor()
    assert tasks_cli.main([
        "run", str(spec_path), "--root", str(worktree),
        "--input", "diff=x", "--allow-exec", "--dry-run", "--json",
    ], executor=fake) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "preview"
    assert fake.calls == []

    bad = _write_spec(worktree, _doc(id=""), name="bad.json")
    assert tasks_cli.main(["validate", str(bad)]) == 1


def test_cli_run_list_and_show(repo, capsys) -> None:
    _root, worktree = repo
    spec_path = _write_spec(worktree, name="cli-run.json")
    fake = FakeExecutor()
    assert tasks_cli.main([
        "run", str(spec_path), "--root", str(worktree),
        "--input", "diff=x", "--allow-exec", "--json",
    ], executor=fake) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["status"] == "ok"
    run_id = run["run_id"]

    assert tasks_cli.main(["list", "--root", str(worktree), "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["runs"][0]["run_id"] == run_id

    assert tasks_cli.main([
        "show", run_id, "--root", str(worktree), "--json",
    ]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["manifest"]["run_id"] == run_id
    assert shown["output"] == {"summary": "ok"}

    assert tasks_cli.main(["show", "--root", str(worktree), "--json"]) == 0
    latest = json.loads(capsys.readouterr().out)
    assert latest["manifest"]["run_id"] == run_id

    assert tasks_cli.main(["show", "missing", "--root", str(worktree)]) == 2
