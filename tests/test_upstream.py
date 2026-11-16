"""Tests for consent-based, redacted local field-improvement bundles."""
from __future__ import annotations

import hashlib
import json
import socket
import zipfile
from pathlib import Path

import pytest

from ozagia import upstream_cli as CLI
from ozagia.core import upstream as U

SECRET_KEY = "sk-live-abcdefghijk"
SECRET_TOKEN = "ghp_abcdefghijklmnopqrstuvwx"
EMAIL = "alice@example.com"
PROMPT = "TOP SECRET PROMPT"
FILE_BODY = "TOP SECRET FILE BODY"


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def _log_report(path: Path) -> Path:
    return _write_json(path, {
        "schema": 1,
        "tool": "log_inspect",
        "generated_at": "2026-11-16T12:00:00+00:00",
        "local_only": True,
        "uploaded": False,
        "truth_assessed": False,
        "limits": {"max_files": 2, "max_bytes": 1024, "max_lines": 100,
                   "findings": 5},
        "summary": {
            "inputs": 1, "inputs_read": 1, "inputs_unreadable": 0,
            "records": 10, "failures": 3, "clusters": 1,
            "findings_shown": 1, "malformed_lines": 0,
            "truncated_inputs": 0,
        },
        "redactions": {"api_key": 1, "email": 1},
        "inputs": [{
            "name": "source_1",
            "path": "~/logs/agent.log",
            "kind": "text",
            "bytes_read": 512,
            "lines_read": 10,
            "records": 10,
            "malformed_lines": 0,
            "truncated_bytes": False,
            "truncated_lines": False,
            "redactions": {"api_key": 1},
        }],
        "errors": [],
        "findings": [{
            "id": "LI-ABCDEF123456",
            "rank": 1,
            "severity": "high",
            "count": 3,
            "first_seen": "2026-11-16T11:00:00+00:00",
            "last_seen": "2026-11-16T12:00:00+00:00",
            "error_type": "ProviderError",
            "signature": (
                f"auth failed for {EMAIL} with api_key={SECRET_KEY}"),
            "likely_component": "opencode server",
            "dimensions": {
                "tool": "shell",
                "intent": "test",
                "harness": "opencode",
                "model": "provider/model-a",
                "task_class": "log_inspect",
            },
            "source_kinds": ["text"],
            "evidence": {
                "occurrences": 3,
                "records": 3,
                "sources": {"source_1": 3},
                "severities": {"high": 3},
                "timestamps_known": 3,
                "failure_fields": {"level=ERROR": 3},
                "redacted_values": 2,
                "components": {"opencode server": 3},
            },
            "proposed_action": {
                "kind": "proposal",
                "action": "config_change",
                "summary": "Review provider credentials and timeout settings.",
                "confidence": "medium",
                "auto_applied": False,
                "requires_approval": True,
                "network": False,
            },
            "truth_assessed": False,
        }],
        "not_assessed": [
            f'prompt="{PROMPT}"',
            f'stdout="{FILE_BODY}"',
            f"worktree at {Path.home()}/private/repo",
            "Absence of a finding is not proof that no failure occurred.",
        ],
    })


def _code_review_packet(path: Path) -> Path:
    return _write_json(path, {
        "schema": "ozagia.code_review.packet.v1",
        "schema_version": 1,
        "range": {"spec": "main..HEAD", "base": "main", "head": "HEAD",
                  "base_commit": "a" * 40, "head_commit": "b" * 40},
        "base": "main",
        "head": "HEAD",
        "worktree": {
            "path": str(Path.home() / "repo"),
            "head": "b" * 40,
            "branch": "feat/test",
            "detached": False,
            "expected": {"branch": "feat/test", "source": "worktrees.toml"},
            "dirty": {"staged": 0, "unstaged": 1, "untracked": 0,
                      "conflicted": 0},
            "clean": False,
            "operation": None,
            "base": {"branch": "main", "ahead": 0, "behind": 0},
        },
        "preflight": {
            "ok": True,
            "checks": [{"id": "root", "level": "ok", "message": "ok"}],
        },
        "graph": {"path": ".ozagia/graph.json", "available": True,
                  "freshness": "fresh", "usable": True,
                  "snapshot_head": "b" * 40, "head": "b" * 40,
                  "call_edges": 4, "reason": None},
        "resources": {"ok": True},
        "diff": {"files": 1, "added": 1, "modified": 0, "deleted": 0,
                 "renamed": 0, "binary": 0, "large": 0, "generated": 0,
                 "additions": 5, "deletions": 1},
        "files": [{"path": "src/app.py"}],
        "findings": [{
            "id": "CR-001",
            "type": "missing_test",
            "severity": "major",
            "file": "src/app.py",
            "line": 12,
            "message": (
                f"no covering test; access_token={SECRET_TOKEN} "
                f"contact {EMAIL}"),
            "evidence": ["changed source with no test"],
            "graph_keys": ["sym:app"],
            "confidence": "medium",
            "confidence_note": "test-file matching is deterministic",
        }],
        "test_plan": {"mode": "affected", "selected_files": ["tests/test_app.py"],
                      "executed": False, "truth_assessed": False,
                      "not_run_reason": "tests were not run"},
        "tests_executed": False,
        "summary": {"findings": 1, "blocking": 0, "major": 1, "minor": 0,
                    "note": 0},
        "omissions": [],
        "not_assessed": [f'content="{FILE_BODY}"'],
        "model_review": {"status": "not_run", "truth_assessed": False},
        "truth_assessed": False,
        "root": str(Path.home() / "repo"),
    })


def _repo_scan_report(path: Path) -> Path:
    return _write_json(path, {
        "output": "repo_scan.v1",
        "opportunities": [{
            "title": f"Add a test for the parser ({EMAIL})",
            "kind": "missing_test",
            "expected_effect": "Catch parser regressions earlier.",
            "effort": "S",
            "risk": "low",
            "evidence": f"src/parser.py has no test; {SECRET_KEY}",
        }],
        "not_assessed": [f'file_text="{FILE_BODY}"'],
    })


def _status_report(path: Path) -> Path:
    home = Path.home()
    return _write_json(path, {
        "schema": 1,
        "checked_at": "2026-11-16T12:00:00+00:00",
        "root": str(home / "repo"),
        "ok": True,
        "errors": [],
        "degraded": [{
            "component": "daemon", "code": "daemon.absent",
            "message": f"no daemon at {home}/repo/.ozagia/daemon.sock",
        }],
        "workspace": {"state": "ok", "report": {
            "worktrees": [{
                "path": str(home / "repo"),
                "head": "a" * 40,
                "branch": "main",
                "detached": False,
                "expected": {"branch": "main", "source": "default"},
                "dirty": {"staged": 0, "unstaged": 2, "untracked": 1,
                          "conflicted": 0},
                "operation": None,
            }],
            "findings": [],
        }},
        "resources": {"state": "ok", "report": {
            "host": {
                "platform": "linux",
                "cpu_count": 8,
                "memory": {"total_mb": 16384, "available_mb": 8192,
                           "used_pct": 50.0},
                "load": {"1m": 0.5, "5m": 0.4, "15m": 0.3},
            },
            "disks": [{"mount": str(home), "fstype": "ext4",
                       "free_gb": 10.0, "free_pct": 20.0,
                       "is_tmpfs": False}],
            "top_groups": [{
                "pgid": 123,
                "command": "shell --token " + SECRET_TOKEN,
                "memory_mb": 100,
                "process_count": 2,
            }],
        }},
        "files": {"state": "ok", "report": {
            "counts": {"records": 3, "orphans": 1, "new": 0},
            "records": [{"path": "history/secret.md"}],
        }},
        "runtime": {"state": "ok", "report": {
            "services": [{"name": "svc", "command": SECRET_TOKEN}],
            "tunnels": [],
            "processes": [{"command": "shell " + SECRET_TOKEN}],
        }},
        "daemon": {"state": "absent", "report": {
            "socket": str(home / "repo/.ozagia/daemon.sock"),
            "pid": 9999,
            "version": "0.1.0",
            "uptime_s": None,
            "stats_error": f"token {SECRET_TOKEN}",
        }},
        "doctor": {"state": "ok", "report": {
            "snapshot": {"state": "fresh",
                         "path": str(home / "repo/.ozagia/graph.json")},
        }},
        "config": {"state": "defaults", "path": None, "present": False},
    })


def _task_run(root: Path, run_id: str = "run-2026-11-16") -> Path:
    run_dir = root / ".ozagia" / "tasks" / run_id
    manifest = {
        "schema": 1,
        "mode": "exec",
        "executed": True,
        "ok": False,
        "run_id": run_id,
        "root": str(root),
        "spec": {
            "id": "log_inspect",
            "version": 1,
            "title": "Log inspection",
            "digest": "sha256:specdigest",
            "path": str(root / "spec.toml"),
            "objective": "TOP SECRET OBJECTIVE",
            "output": "failure_clusters.v1",
            "tools": ["read", "grep"],
            "write_scope": [],
            "requires_graph": False,
            "gates": {"before": ["resources_fit"], "after": []},
            "budget": {"tokens": 1000, "model_calls": 0, "cost_usd": 0.0,
                       "wall_s": 60},
            "resources": {"memory_mb": 512, "disk_mb": 256, "pids": 32,
                          "wall_s": 60},
            "approval": {"mutate": "deny", "network": "deny"},
            "test_policy": None,
        },
        "inputs": {
            "names": ["log_path"],
            "digests": {"log_path": "sha256:inputdigest"},
            "digest": "sha256:inputsdigest",
        },
        "started_at": "2026-11-16T12:00:00+00:00",
        "finished_at": "2026-11-16T12:00:10+00:00",
        "duration_s": 10.0,
        "status": "failed",
        "stop": "gate_failed",
        "stop_detail": f"worktree at {Path.home()}/repo had a dirty path",
        "preflight": {
            "ok": False,
            "failures": [f"expected branch feat/x; token {SECRET_TOKEN}"],
            "warnings": [],
            "worktree": {
                "path": str(Path.home() / "repo"),
                "branch": "feat/x",
                "head": "a" * 40,
                "detached": False,
                "operation": None,
                "dirty": {"staged": 0, "unstaged": 1, "untracked": 0,
                          "conflicted": 0},
                "expected": {"branch": "feat/x", "source": "config"},
                "error": None,
            },
            "paths": {"ok": True, "write_scope": [], "violations": []},
            "resources": {"ok": True, "reason": None, "requested": {},
                          "available": {}, "shortfall": {},
                          "suggestions": []},
            "snapshot": {"required": False, "state": "not_checked",
                         "error": None},
            "test_plan": None,
        },
        "test_plan": None,
        "gates": {
            "before": [{
                "id": "worktree_expected",
                "phase": "before",
                "level": "error",
                "ok": False,
                "message": f"prompt=\"{PROMPT}\"",
                "details": {},
                "command": None,
            }],
            "after": [],
            "after_skipped": "a step stop occurred",
        },
        "steps": [{
            "id": "collect",
            "index": 1,
            "tool": "read",
            "status": "error",
            "summary": f"failed reading {SECRET_TOKEN}",
            "elapsed_s": 1.0,
            "usage": {"tokens": 10, "model_calls": 0, "cost_usd": 0.0,
                      "wall_s": 1.0, "memory_mb": 10, "disk_mb": 1,
                      "pids": 1},
            "artifacts": 1,
            "evidence": 1,
        }],
        "artifacts": [{
            "path": "history/failure.md",
            "sha256": "sha256:artifact",
            "size": 100,
            "kind": "report",
            "step": "collect",
        }],
        "evidence_count": 1,
        "budget": {
            "declared": {"tokens": 1000, "model_calls": 0, "cost_usd": 0.0,
                         "wall_s": 60},
            "resources": {"memory_mb": 512, "disk_mb": 256, "pids": 32,
                          "wall_s": 60},
            "used": {"tokens": 10, "model_calls": 0, "cost_usd": 0.0,
                     "wall_s": 10.0, "memory_mb": 10, "disk_mb": 1,
                     "pids": 1},
            "remaining": {},
        },
        "output": {
            "name": "failure_clusters.v1",
            "present": True,
            "digest": "sha256:output",
            "schema_declared": True,
            "serializable": True,
            "errors": [],
            "checked": True,
            "schema_valid": True,
            "value": {"body": FILE_BODY},
        },
        "what_was_not_assessed": [
            f'prompt="{PROMPT}"',
            f'file_text="{FILE_BODY}"',
        ],
        "warnings": [],
    }
    _write_json(run_dir / "manifest.json", manifest)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "input", "prompt": PROMPT}) + "\n",
        encoding="utf-8")
    return run_dir


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _snapshot(path: Path) -> list[tuple[str, int]]:
    return sorted(
        (item.relative_to(path).as_posix(),
         item.stat().st_size if item.is_file() else -1)
        for item in path.rglob("*"))


def _all_text(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def test_preview_redacts_secrets_prompts_emails_and_home_paths(
        root: Path, tmp_path: Path) -> None:
    log = _log_report(tmp_path / "log.json")
    review = _code_review_packet(tmp_path / "review.json")
    report = U.preview(
        root,
        title=f"Crash with {SECRET_KEY} for {EMAIL}",
        summary=f"contact {EMAIL} about {SECRET_KEY}",
        evidence=[log, review],
    )
    text = _all_text(report)
    for secret in (SECRET_KEY, SECRET_TOKEN, EMAIL, PROMPT, FILE_BODY,
                   str(Path.home())):
        assert secret not in text
    assert "<redacted:api_key>" in text
    assert "<redacted:email>" in text
    assert report["redactions"]["api_key"] >= 1
    assert report["redactions"]["email"] >= 1
    assert report["redactions"]["prompt"] >= 1
    assert report["redactions"]["file_content"] >= 1
    assert report["consent_required"] is True
    assert report["network"] is False
    assert report["uploaded"] is False


def test_home_prefix_in_source_path_is_redacted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = home / "repo"
    repo.mkdir()
    logs = home / "logs"
    logs.mkdir()
    report = _log_report(logs / "report.json")
    preview_report = U.preview(repo, title="Home path", evidence=[report])
    text = _all_text(preview_report)
    assert str(home) not in text
    assert preview_report["included"][0]["source"] == "~/logs/report.json"
    assert preview_report["root"] == "~/repo"


def test_preview_writes_nothing(root: Path) -> None:
    log = _log_report(root / "log.json")
    before = _snapshot(root)
    report = U.preview(root, title="No writes", evidence=[log])
    after = _snapshot(root)
    assert before == after
    assert report["included"]
    assert not (root / "bundle").exists()


def test_evidence_allowlist_and_exclusions(root: Path) -> None:
    log = _log_report(root / "log.json")
    unknown = _write_json(root / "unknown.json", {"hello": "world"})
    raw = root / "raw.log"
    raw.write_text(f"ERROR {SECRET_KEY} {EMAIL}\n", encoding="utf-8")
    empty_dir = root / "not-a-run"
    empty_dir.mkdir()
    report = U.preview(
        root, title="Allowlist",
        evidence=[log, unknown, raw, empty_dir, log])
    assert [item["kind"] for item in report["included"]] == ["log_report"]
    reasons = {item["source"]: item["reason"] for item in report["excluded"]}
    assert reasons["unknown.json"].startswith("unsupported evidence type")
    assert "not a JSON document" in reasons["raw.log"]
    assert "not a task run" in reasons["not-a-run"]
    assert reasons["log.json"] == "duplicate evidence path"
    text = _all_text(report)
    assert SECRET_KEY not in text
    assert EMAIL not in text


def test_evidence_bounds(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(U, "MAX_EVIDENCE", 2)
    with pytest.raises(U.UpstreamError):
        U.preview(root, title="Too many",
                  evidence=[root / "a.json", root / "b.json",
                            root / "c.json"])

    monkeypatch.setattr(U, "MAX_EVIDENCE_FILE_BYTES", 64)
    big = root / "big.json"
    big.write_text('{"padding": "' + "x" * 100 + '"}', encoding="utf-8")
    report = U.preview(root, title="Too big", evidence=[big])
    assert report["included"] == []
    assert "too large" in report["excluded"][0]["reason"]


def test_task_run_evidence_omits_prompt_file_body_and_output_value(
        root: Path) -> None:
    run_dir = _task_run(root)
    report = U.preview(root, title="Task run", evidence=[run_dir])
    assert len(report["included"]) == 1
    content = report["included"][0]["content"]
    assert content["kind"] == "task_run"
    assert content["run_id"] == "run-2026-11-16"
    assert content["spec"]["id"] == "log_inspect"
    assert content["status"] == "failed"
    assert content["stop"] == "gate_failed"
    assert content["artifacts"][0]["path"] == "history/failure.md"
    text = _all_text(report)
    for secret in (PROMPT, FILE_BODY, "TOP SECRET OBJECTIVE", SECRET_TOKEN,
                   str(Path.home())):
        assert secret not in text
    assert "value" not in content["output"]
    assert "objective" not in content["spec"]
    assert "events" not in content
    assert "worktree_expected" in text
    assert "log_inspect" in report["task_classes"]


def test_task_run_manifest_file_and_incomplete_run(root: Path) -> None:
    run_dir = _task_run(root, "run-file")
    manifest = run_dir / "manifest.json"
    report = U.preview(root, title="Manifest", evidence=[manifest])
    assert report["included"][0]["content"]["run_id"] == "run-file"

    incomplete = root / ".ozagia" / "tasks" / "run-partial"
    incomplete.mkdir(parents=True)
    (incomplete / "events.jsonl").write_text("{}\n", encoding="utf-8")
    partial = U.preview(root, title="Partial", evidence=[incomplete])
    content = partial["included"][0]["content"]
    assert content["kind"] == "task_run"
    assert content["state"] == "incomplete"


def test_code_review_repo_scan_and_status_summaries(root: Path,
                                                    tmp_path: Path) -> None:
    review = _code_review_packet(tmp_path / "review.json")
    scan = _repo_scan_report(tmp_path / "scan.json")
    status = _status_report(tmp_path / "status.json")
    report = U.preview(
        root, title="Mixed evidence", evidence=[review, scan, status])
    kinds = [item["kind"] for item in report["included"]]
    assert kinds == ["code_review", "repo_scan", "status"]
    text = _all_text(report)
    for secret in (SECRET_TOKEN, EMAIL, FILE_BODY, PROMPT, str(Path.home())):
        assert secret not in text
    review_content = report["included"][0]["content"]
    assert review_content["range"]["spec"] == "main..HEAD"
    assert review_content["findings"][0]["type"] == "missing_test"
    scan_content = report["included"][1]["content"]
    assert scan_content["opportunity_count"] == 1
    status_content = report["included"][2]["content"]
    assert status_content["workspace"]["worktrees"][0]["branch"] == "main"
    assert status_content["resources"]["disks"][0]["fstype"] == "ext4"
    # The status summariser never includes top process groups or raw argv.
    assert "top_groups" not in status_content["resources"]
    assert "shell --token" not in text
    assert any(item["kind"] == "code_review"
               for item in report["findings"])


def test_config_diff_redacted_and_optional(root: Path) -> None:
    config_dir = root / "ozagia"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "# SECRET CONFIG COMMENT\n"
        "[testing]\n"
        'mode = "none"\n'
        "[ui]\n"
        'theme = "light"\n',
        encoding="utf-8")
    report = U.preview(root, title="Config")
    config = report["config"]
    assert config["state"] == "changed"
    keys = {item["key"] for item in config["changed"]}
    assert "testing.mode" in keys
    assert "ui.theme" in keys
    assert config["repo_config"]["present"] is True
    assert config["repo_config"]["path"] == "ozagia/config.toml"
    assert "SECRET CONFIG COMMENT" not in _all_text(report)

    excluded = U.preview(root, title="No sections",
                         include_environment=False, include_config=False)
    assert excluded["environment"] is None
    assert excluded["config"] is None
    sources = {item["source"] for item in excluded["excluded"]}
    assert {"environment", "config"} <= sources


def test_environment_summary_is_bounded(root: Path) -> None:
    report = U.preview(root, title="Environment")
    environment = report["environment"]
    assert environment["python_version"]
    assert environment["system"]
    assert "hostname" in environment["excluded"]


def test_preview_is_deterministic_and_json_serializable(root: Path) -> None:
    log = _log_report(root / "log.json")
    first = U.preview(root, title="Deterministic", evidence=[log])
    second = U.preview(root, title="Deterministic", evidence=[log])
    assert first == second
    assert json.loads(_all_text(first)) == first


def test_write_requires_explicit_consent(root: Path, tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    with pytest.raises(U.ConsentRequired):
        U.write_bundle(root, destination, consent=False, title="Consent")
    assert not destination.exists()
    with pytest.raises(U.ConsentRequired):
        U.write_bundle(root, destination, consent="yes", title="Consent")  # type: ignore[arg-type]
    assert not destination.exists()
    assert CLI.main([
        "write", "--root", str(root), "--title", "Consent",
        "--out", str(destination),
    ]) == 1
    assert not destination.exists()


def test_deterministic_bundle_manifest_and_zip(root: Path,
                                               tmp_path: Path) -> None:
    log = _log_report(root / "log.json")
    review = _code_review_packet(root / "review.json")
    preview = U.preview(root, title="Deterministic bundle",
                        summary="A stable bundle.", evidence=[log, review])
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_result = U.write_bundle(root, first, consent=True, preview=preview)
    U.write_bundle(root, second, consent=True, preview=preview)

    assert first_result["format"] == "directory"
    assert (first / "manifest.json").read_bytes() == (
        second / "manifest.json").read_bytes()
    assert (first / "README.md").read_bytes() == (
        second / "README.md").read_bytes()
    assert (first / "issue.md").read_bytes() == (
        second / "issue.md").read_bytes()
    for item in json.loads((first / "manifest.json").read_text(
            encoding="utf-8"))["files"]:
        data = (first / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"].split(
            ":", 1)[1]
    expected_manifest = "sha256:" + hashlib.sha256(
        (first / "manifest.json").read_bytes()).hexdigest()
    assert first_result["manifest_sha256"] == expected_manifest

    first_zip = tmp_path / "first.zip"
    second_zip = tmp_path / "second.zip"
    U.write_bundle(root, first_zip, consent=True, preview=preview)
    U.write_bundle(root, second_zip, consent=True, preview=preview)
    assert first_zip.read_bytes() == second_zip.read_bytes()


def test_bundle_directory_and_zip_layout(root: Path, tmp_path: Path) -> None:
    log = _log_report(root / "log.json")
    preview = U.preview(root, title="Layout", evidence=[log])
    directory = tmp_path / "bundle"
    result = U.write_bundle(root, directory, consent=True, preview=preview)
    names = {item["path"] for item in result["files"]}
    assert names == {"manifest.json", "README.md", "issue.md",
                     "evidence/01-log_report.json"}
    manifest = json.loads((directory / "manifest.json").read_text(
        encoding="utf-8"))
    assert manifest["schema"] == U.BUNDLE_SCHEMA
    assert manifest["consent"]["granted"] is True
    assert manifest["consent"]["network"] is False
    assert manifest["local_only"] is True
    assert manifest["uploaded"] is False
    assert manifest["issue_created"] is False
    assert manifest["evidence_count"] == 1
    evidence = json.loads((directory / "evidence/01-log_report.json").read_text(
        encoding="utf-8"))
    assert evidence["schema"] == U.EVIDENCE_SCHEMA
    assert evidence["content"]["kind"] == "log_report"
    assert preview["included"][0]["bytes"] == len(
        (directory / "evidence/01-log_report.json").read_bytes())

    archive = tmp_path / "bundle.zip"
    zip_result = U.write_bundle(root, archive, consent=True, preview=preview)
    assert zip_result["format"] == "zip"
    with zipfile.ZipFile(archive) as handle:
        entries = handle.namelist()
        assert set(entries) == names
        for name in entries:
            assert not name.startswith("/")
            assert ".." not in Path(name).parts
        assert handle.read("issue.md") == (directory / "issue.md").read_bytes()


def test_crafted_preview_paths_stay_inside_bundle(root: Path,
                                                  tmp_path: Path) -> None:
    preview = U.preview(root, title="Crafted")
    preview["included"] = [{
        "index": 1,
        "kind": "../../evil",
        "source": "crafted",
        "source_bytes": 0,
        "source_sha256": None,
        "bytes": 2,
        "content": {"kind": "log_report"},
        "excluded_fields": [],
    }]
    destination = tmp_path / "crafted"
    result = U.write_bundle(root, destination, consent=True, preview=preview)
    names = [item["path"] for item in result["files"]]
    assert "evidence/01-evil.json" in names
    for name in names:
        assert ".." not in Path(name).parts
    archive = tmp_path / "crafted.zip"
    U.write_bundle(root, archive, consent=True, preview=preview)
    with zipfile.ZipFile(archive) as handle:
        for name in handle.namelist():
            assert ".." not in Path(name).parts


def test_unsafe_destination_refused(root: Path, tmp_path: Path) -> None:
    for destination in (root, root / ".git" / "bundle", root.parent,
                        Path(Path.cwd().anchor)):
        with pytest.raises(U.BundleError):
            U.write_bundle(root, destination, consent=True, title="Unsafe")

    non_empty = tmp_path / "non-empty"
    non_empty.mkdir()
    (non_empty / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(U.BundleError):
        U.write_bundle(root, non_empty, consent=True, title="Unsafe")

    existing_file = tmp_path / "file"
    existing_file.write_text("x", encoding="utf-8")
    with pytest.raises(U.BundleError):
        U.write_bundle(root, existing_file, consent=True, title="Unsafe")

    missing_parent = tmp_path / "missing" / "bundle"
    with pytest.raises(U.UpstreamError):
        U.write_bundle(root, missing_parent, consent=True, title="Unsafe")

    assert CLI.main([
        "write", "--root", str(root), "--title", "Unsafe",
        "--out", str(root), "--consent",
    ]) == 1
    assert CLI.main([
        "write", "--root", str(root), "--title", "Missing parent",
        "--out", str(tmp_path / "missing" / "bundle"), "--consent",
    ]) == 2


def test_bundle_writes_only_inside_destination(root: Path,
                                               tmp_path: Path) -> None:
    parent = tmp_path / "out"
    parent.mkdir()
    log = _log_report(root / "log.json")
    destination = parent / "bundle"
    U.write_bundle(root, destination, consent=True, title="Contained",
                   evidence=[log])
    children = {item.name for item in parent.iterdir()}
    assert children == {"bundle"}
    for item in parent.rglob("*"):
        assert destination == item or destination in item.parents


def test_no_network(root: Path, tmp_path: Path,
                    monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is not allowed")

    monkeypatch.setattr(socket, "socket", _fail)
    log = _log_report(root / "log.json")
    preview = U.preview(root, title="Offline", evidence=[log])
    result = U.write_bundle(root, tmp_path / "offline", consent=True,
                            preview=preview)
    assert result["network"] is False
    assert result["uploaded"] is False
    assert result["issue_created"] is False
    assert (tmp_path / "offline" / "manifest.json").is_file()


def test_issue_draft_only(root: Path, tmp_path: Path) -> None:
    log = _log_report(root / "log.json")
    preview = U.preview(root, title="Issue draft", summary="Something failed.",
                        evidence=[log])
    text = U.issue_markdown(preview)
    assert text.startswith("# Issue draft")
    assert "Nothing was uploaded" in text
    assert "## Reproduction" in text
    assert "## Selected findings" in text
    assert SECRET_KEY not in text
    assert EMAIL not in text
    assert U.issue_text(preview) == text

    output = tmp_path / "issue.md"
    result = U.write_issue(root, output, title="Issue draft",
                           summary="Something failed.", evidence=[log])
    assert output.read_text(encoding="utf-8") == text
    assert result["local_only"] is True
    assert result["uploaded"] is False
    assert result["issue_created"] is False

    cli_output = tmp_path / "cli-issue.md"
    assert CLI.main([
        "issue", "--root", str(root), "--title", "Issue draft",
        "--evidence", str(log), "--out", str(cli_output),
    ]) == 0
    assert cli_output.is_file()
    assert "Nothing was uploaded" in cli_output.read_text(encoding="utf-8")


def test_cli_preview_write_and_exit_codes(root: Path, tmp_path: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    log = _log_report(root / "log.json")
    assert CLI.main([
        "preview", "--root", str(root), "--title", "CLI preview",
        "--evidence", str(log), "--json",
    ]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["schema"] == U.PREVIEW_SCHEMA
    assert report["included"][0]["kind"] == "log_report"

    destination = tmp_path / "cli-bundle"
    assert CLI.main([
        "write", "--root", str(root), "--title", "CLI write",
        "--evidence", str(log), "--out", str(destination), "--consent",
        "--json",
    ]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert result["format"] == "directory"
    assert (destination / "manifest.json").is_file()

    assert CLI.main([
        "preview", "--root", str(tmp_path / "missing"), "--title", "Missing",
    ]) == 2
    assert CLI.main([
        "preview", "--root", str(root), "--title", "Missing evidence",
        "--evidence", str(root / "missing.json"),
    ]) == 2


def test_format_helpers(root: Path, tmp_path: Path) -> None:
    log = _log_report(root / "log.json")
    preview = U.preview(root, title="Format", evidence=[log])
    text = U.format_preview(preview)
    assert "Upstream field-improvement preview" in text
    assert "Consent: required" in text
    assert "Included (1)" in text
    assert "Excluded (0)" in text
    assert "Local only: true" in text
    assert text.endswith("\n")

    result = U.write_bundle(root, tmp_path / "format-bundle", consent=True,
                            preview=preview)
    bundle_text = U.format_bundle(result)
    assert "Bundle written:" in bundle_text
    assert "manifest.json" in bundle_text
    assert "Consent: granted locally" in bundle_text
    assert bundle_text.endswith("\n")
