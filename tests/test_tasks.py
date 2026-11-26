"""Bounded task specifications, the deterministic runner and evidence."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from ozagia import tasks_cli
from ozagia.core import grant_lifecycle as GL
from ozagia.core import resource_grants as RG
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


def test_cli_local_executor_preview_and_run(repo, capsys) -> None:
    _root, worktree = repo
    doc = _doc(
        tools=["read"],
        steps=[{"id": "read", "tool": "read", "goal": "Read the readme.",
                "paths": ["."], "args": {"path": "README.md"}}],
        gates={"after": ["evidence_present"]},
    )
    doc.pop("output_schema", None)
    spec_path = _write_spec(worktree, doc, name="local.json")

    assert tasks_cli.main([
        "run", str(spec_path), "--root", str(worktree),
        "--input", "diff=x", "--allow-exec", "--executor", "local",
        "--dry-run", "--json",
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "preview"
    assert preview["executed"] is False
    assert preview["executor_configured"] is True
    assert not T.tasks_dir(worktree).exists()

    assert tasks_cli.main([
        "run", str(spec_path), "--root", str(worktree),
        "--input", "diff=x", "--allow-exec", "--local", "--json",
    ]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["status"] == "ok", run
    assert run["executed"] is True
    assert run["steps"][0]["tool"] == "read"
    assert run["evidence_count"] == 1
    assert run["artifacts"] == []
    run_dir = Path(run["run_dir"])
    for name in ("events.jsonl", "manifest.json", "artifacts.json",
                 "output.json"):
        assert (run_dir / name).is_file()
    artifacts = json.loads((run_dir / "artifacts.json").read_text(
        encoding="utf-8"))
    assert artifacts["count"] == 0
    manifest = json.loads((run_dir / "manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["status"] == "ok"
    for path in (run_dir / "events.jsonl", run_dir / "manifest.json",
                 run_dir / "artifacts.json"):
        text = path.read_text(encoding="utf-8")
        assert "# Test" not in text
    assert not (worktree / ".pytest_cache").exists()


def test_cli_without_executor_is_refused(repo, capsys) -> None:
    _root, worktree = repo
    spec_path = _write_spec(worktree, name="no-executor.json")
    assert tasks_cli.main([
        "run", str(spec_path), "--root", str(worktree),
        "--input", "diff=x", "--allow-exec", "--json",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "refused"
    assert report["stop"] == "error"
    assert "no executor" in report["stop_detail"]


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


# --------------------------------------------------------------------------- #
# resource grants
# --------------------------------------------------------------------------- #
def _grant_file(path: Path, *grants: str) -> None:
    base = "[resources]\nenabled = false\n"
    blocks = []
    for grant_id in grants:
        blocks.append(
            f'[resource_grants.{grant_id}]\n'
            'kind = "service"\nprovider = "other"\ncleanup = "destroy"\n')
    _write(path / "ozagia" / "resources.toml", base + "".join(blocks))


def _ready_grant(monkeypatch) -> None:
    def ready(root, grant_id, **kwargs):
        return {
            "state": "ready", "ready": True, "checks": [],
            "permissions": [], "budget": None, "cleanup": "destroy",
            "approval": {"required": False, "granted": False},
        }
    monkeypatch.setattr(RG, "check_grant", ready)


def _host_ready(monkeypatch) -> None:
    monkeypatch.setattr(RG.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(RG.os.path, "exists", lambda path: path == "/dev/kvm")
    monkeypatch.setattr(RG.os, "access", lambda path, mode: True)


def _task_with_grants(repo, *grant_ids: str, resources=None,
                      **overrides) -> Path:
    root, worktree = repo
    _grant_file(worktree, *grant_ids)
    declared = {"grants": list(grant_ids)}
    if resources:
        declared.update(resources)
    doc = _doc(worktree={"require_clean": False}, resources=declared,
               **overrides)
    return _write_spec(worktree, doc, name="grant-task.json")


def test_task_grant_digest_is_stable_without_grants(tmp_path: Path) -> None:
    example = (Path(__file__).resolve().parent.parent / "examples" / "tasks"
               / "code_review_dry_run.toml")
    spec = T.load_spec(example)
    assert "grants" not in spec.to_dict()["resources"]
    assert spec.digest == (
        "sha256:cd6776ce1c3a552a40f9c89cae8f1cc45d56a3360600939d3b0961a295"
        "e856af")

    _grant_file(tmp_path, "svc")
    path = _write_spec(
        tmp_path, _doc(resources={"grants": ["svc"]}), name="with-grant.json")
    granted = T.load_spec(path, root=tmp_path)
    assert granted.resources.grants == ("svc",)
    assert granted.to_dict()["resources"]["grants"] == ["svc"]
    assert granted.digest != spec.digest


def test_task_grant_ids_are_bounded_and_validated(tmp_path: Path) -> None:
    _grant_file(tmp_path, "svc")
    cases = [
        (["svc", "svc"], "duplicate resource grant id"),
        (["missing"], "unknown resource grant 'missing'"),
        ("svc", "must be a list"),
        ([f"g{index}" for index in range(GL.MAX_GRANTS + 1)],
         f"at most {GL.MAX_GRANTS}"),
        ([1], "entry 0 must be a resource grant id"),
    ]
    for index, (grants, message) in enumerate(cases):
        report = T.validate_spec(
            _write_spec(tmp_path, _doc(resources={"grants": grants}),
                        name=f"bad-grants-{index}.json"),
            root=tmp_path)
        assert report["ok"] is False
        assert any(message in error for error in report["errors"]), (
            message, report["errors"])


def test_missing_grant_refuses_before_work(repo) -> None:
    _root, worktree = repo
    _write(worktree / "ozagia" / "resources.toml",
           "[resources]\nenabled = false\n"
           '[resource_grants.missing-vm]\n'
           'kind = "vm"\nprovider = "qemu"\nimage = "/tmp/not-there.qcow2"\n'
           'cleanup = "destroy"\n')
    spec = T.load_spec(_write_spec(
        worktree, _doc(worktree={"require_clean": False},
                       resources={"grants": ["missing-vm"]}),
        name="missing-grant.json"))
    fake = FakeExecutor()
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "refused"
    assert report["stop"] == "gate_failed"
    assert fake.calls == []
    assert report["grants"]["preflight"]["ok"] is False
    assert any("missing-vm" in failure
               for failure in report["grants"]["preflight"]["failures"])
    assert not (worktree / ".ozagia" / "resources" / "grants").exists()


def test_admin_and_keep_grants_need_host_flags(repo, monkeypatch) -> None:
    _root, worktree = repo
    _host_ready(monkeypatch)
    _write(worktree / "ozagia" / "resources.toml",
           "[resources]\nenabled = false\n"
           '[resource_grants.admin-host]\nkind = "host"\nprovider = "qemu"\n'
           'capabilities = ["kvm"]\npermissions = ["admin"]\n'
           'cleanup = "none"\n'
           '[resource_grants.keep-host]\nkind = "host"\nprovider = "qemu"\n'
           'capabilities = ["kvm"]\ncleanup = "keep"\n'
           'requires_approval = true\nrisk = "a human tears it down"\n')
    doc = _doc(worktree={"require_clean": False},
               resources={"grants": ["admin-host"]})
    spec = T.load_spec(_write_spec(worktree, doc, name="admin-grant.json"))
    fake = FakeExecutor()
    no_approval = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert no_approval["status"] == "refused"
    assert no_approval["grants"]["preflight"]["checks"][0]["state"] == (
        "requires_approval")
    assert fake.calls == []

    approve_only = T.TaskRunner(
        spec, worktree, executor=fake, approve_grants=True).run(
        {"diff": "x"}, allow_exec=True)
    assert approve_only["status"] == "refused"
    row = approve_only["grants"]["preflight"]["checks"][0]
    assert row["state"] == "requires_approval"
    assert "allow-unbounded" in row["reason"]

    allowed = T.TaskRunner(
        spec, worktree, executor=fake, approve_grants=True,
        allow_unbounded_grants=True).run({"diff": "x"}, allow_exec=True)
    assert allowed["status"] == "ok"
    assert len(fake.calls) == 2
    assert allowed["grants"]["claims"][0]["approval"]["granted"] is True

    keep = T.load_spec(_write_spec(
        worktree, _doc(worktree={"require_clean": False},
                       resources={"grants": ["keep-host"]}),
        name="keep-grant.json"))
    fake = FakeExecutor()
    keep_refused = T.TaskRunner(
        keep, worktree, executor=fake, approve_grants=True).run(
        {"diff": "x"}, allow_exec=True)
    assert keep_refused["status"] == "refused"
    row = keep_refused["grants"]["preflight"]["checks"][0]
    assert row["state"] == "requires_approval"
    assert "allow-keep" in row["reason"]
    keep_ok = T.TaskRunner(
        keep, worktree, executor=fake, approve_grants=True,
        allow_keep_grants=True).run({"diff": "x"}, allow_exec=True)
    assert keep_ok["status"] == "ok"


def test_claim_is_taken_before_work_and_released_in_manifest(
        repo, monkeypatch) -> None:
    _root, worktree = repo
    _ready_grant(monkeypatch)
    spec = T.load_spec(_task_with_grants(repo, "svc"))
    seen: list[int] = []

    def handler(step, context):
        seen.append(RG.claims(worktree)["counts"]["active"])
        return {"status": "ok", "summary": step["id"], "usage": {},
                "evidence": [f"checked {step['id']}"],
                "output": {"summary": step["id"]}}

    report = T.TaskRunner(spec, worktree, executor=handler).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok"
    assert seen and all(count == 1 for count in seen)
    manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    grant = manifest["grants"]
    assert grant["declared"] == ["svc"]
    claim = grant["claims"][0]
    assert claim["task"] == "code_review"
    assert claim["claim_id"].startswith("clm-")
    assert claim["budget"]["max_usd"] is None
    assert claim["budget"]["max_minutes"] == 1.0
    assert claim["observed"] == {
        "cost_usd": None, "minutes": None, "provided": False}
    assert claim["cleanup"]["status"] == "unknown"
    assert claim["release"]["applied"] is True
    assert claim["status"] == "released"
    assert grant["unreleased"] == []
    assert RG.claims(worktree)["counts"]["released"] == 1


def test_grants_release_on_exception_and_timeout(repo, monkeypatch) -> None:
    _root, worktree = repo
    _ready_grant(monkeypatch)
    spec = T.load_spec(_task_with_grants(repo, "svc"))

    def explode(step, context):
        raise RuntimeError("boom")

    failed = T.TaskRunner(spec, worktree, executor=explode).run(
        {"diff": "x"}, allow_exec=True)
    assert failed["status"] == "failed"
    assert failed["grants"]["claims"][0]["status"] == "released"
    assert failed["grants"]["claims"][0]["cleanup"]["status"] == "unknown"

    slow = T.load_spec(_task_with_grants(
        repo, "svc", budget={"cost_usd": 0.0, "wall_s": 1},
        resources={"memory_mb": 512, "disk_mb": 256, "pids": 32,
                   "wall_s": 1, "grants": ["svc"]}))

    def sleep(step, context):
        import time
        time.sleep(1.2)
        return {"status": "ok", "summary": "slow", "usage": {},
                "evidence": []}

    timed = T.TaskRunner(slow, worktree, executor=sleep).run(
        {"diff": "x"}, allow_exec=True)
    assert timed["status"] in {"stopped", "failed"}
    assert timed["grants"]["claims"][0]["status"] == "released"
    assert timed["grants"]["claims"][0]["cleanup"]["status"] == "unknown"


def test_partial_claim_failure_releases_in_reverse_order(
        repo, monkeypatch) -> None:
    _root, worktree = repo
    _ready_grant(monkeypatch)
    spec = T.load_spec(_task_with_grants(repo, "a", "b", "c"))
    real_claim = RG.claim
    held: list[str] = []

    def claim(root, grant_id, **kwargs):
        if grant_id == "c":
            raise RG.GrantRefused("already in use", code="grant_in_use")
        record = real_claim(root, grant_id, **kwargs)
        held.append(record["claim_id"])
        return record

    released: list[str] = []
    real_release = RG.release

    def release(root, claim_id, **kwargs):
        released.append(claim_id)
        return real_release(root, claim_id, **kwargs)

    monkeypatch.setattr(RG, "claim", claim)
    monkeypatch.setattr(RG, "release", release)
    fake = FakeExecutor()
    report = T.TaskRunner(spec, worktree, executor=fake).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "refused"
    assert report["stop"] == "gate_failed"
    assert report["grants"]["refused"]["grant"] == "c"
    assert report["grants"]["refused"]["code"] == "grant_in_use"
    assert fake.calls == []
    assert released[:2] == list(reversed(held))
    assert [claim["status"] for claim in report["grants"]["claims"]] == [
        "released", "released"]


def test_unreleased_claim_is_an_explicit_warning(repo, monkeypatch) -> None:
    _root, worktree = repo
    _ready_grant(monkeypatch)

    def cannot_release(root, claim_id, **kwargs):
        raise RG.GrantRefused("teardown failed", code="release_failed")

    monkeypatch.setattr(RG, "release", cannot_release)
    spec = T.load_spec(_task_with_grants(repo, "svc"))
    report = T.TaskRunner(spec, worktree, executor=FakeExecutor()).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "ok"
    grant = report["grants"]
    assert grant["released"] is False
    assert grant["unreleased"]
    assert any("was not released" in warning
               for warning in grant["warnings"])
    manifest = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["grants"]["unreleased"] == grant["unreleased"]
    assert any("was not released" in warning
               for warning in manifest["warnings"])


def test_grant_reason_with_a_secret_is_refused_without_echo(
        repo, monkeypatch, capsys) -> None:
    _root, worktree = repo
    _ready_grant(monkeypatch)
    spec = T.load_spec(_task_with_grants(
        repo, "svc", resources={"grants": ["svc"]}))
    canary = "canary-" + "value-123"
    reason = "tok" + "en=" + canary
    report = T.TaskRunner(
        spec, worktree, executor=FakeExecutor(),
        grant_reason=reason).run(
        {"diff": "x"}, allow_exec=True)
    assert report["status"] == "refused"
    text = json.dumps(report) + Path(report["manifest"]).read_text(
        encoding="utf-8")
    assert canary not in text
    assert "token" not in report["grants"]["refused"]["message"].lower()


def test_cli_grant_flags_reach_the_claim(repo, monkeypatch, capsys) -> None:
    _root, worktree = repo
    _ready_grant(monkeypatch)
    spec = _task_with_grants(repo, "svc")
    fake = FakeExecutor()
    assert tasks_cli.main([
        "run", str(spec), "--root", str(worktree), "--input", "diff=x",
        "--allow-exec", "--approve-grant", "--allow-unbounded-grant",
        "--allow-keep-grant", "--grant-reason", "host asked for this",
        "--json",
    ], executor=fake) == 0
    report = json.loads(capsys.readouterr().out)
    claim = report["grants"]["claims"][0]
    assert claim["approval"]["flags"] == {
        "approve": True, "allow_unbounded": True, "allow_keep": True}
    reason = claim["approval"]["reason"]
    assert reason.startswith("host asked for this; task code_review run ")
    assert "budget 1 minutes" in reason
    assert claim["status"] == "released"
