"""Tests for bounded, redacted local log inspection."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from ozagia import log_inspect_cli as CLI
from ozagia.core import log_inspect as L
from ozagia.core import tasks as T


def _jsonl(path: Path, records: list[object]) -> Path:
    lines = []
    for record in records:
        if isinstance(record, str):
            lines.append(record)
        else:
            lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _journal(path: Path, records: list[object]) -> Path:
    return _jsonl(path, records)


def _opencode(path: Path, blocks: list[tuple[str, str, str, str]]) -> Path:
    """Write OpenCode-style key=value log lines."""
    lines = []
    for timestamp, level, message, cause in blocks:
        cause_json = json.dumps(cause)
        lines.append(
            f'timestamp={timestamp} level={level} run=run123 '
            f'message="{message}" cause={cause_json} role=server')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _eval_run(path: Path) -> Path:
    doc = {
        "version": 1,
        "driver": "opencode",
        "backend": {"model": "provider/model-x"},
        "runs": [
            {
                "task": "fix-bug",
                "arm": "dev_memory",
                "success": False,
                "driver_error": (
                    "Provider blocked the response: api_key=sk-live-"
                    "abcdefghijk contact alice@example.com"),
                "started_at": "2026-11-16T12:00:00Z",
            },
            {
                "task": "ok-task",
                "arm": "dev_memory",
                "success": True,
                "driver_error": None,
            },
            {
                "task": "prep-task",
                "arm": "graph",
                "success": False,
                "prep_ok": False,
                "prep_error": (
                    "prep failed: ModuleNotFoundError: No module named 'x'"),
                "started_at": "2026-11-16T12:10:00Z",
            },
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _all_fixtures(tmp_path: Path) -> list[Path]:
    journal = _journal(tmp_path / "daemon_journal.jsonl", [
        {
            "ts": "2026-11-16T10:00:00", "op": "tool", "tool": "shell",
            "intent": "test", "handled": True, "is_error": True,
            "reason": "Command failed: pytest exited 1",
            "session": "ses_abc", "harness": "opencode",
        },
        {
            "ts": "2026-11-16T11:00:00", "op": "tool", "tool": "shell",
            "intent": "test", "handled": True, "is_error": True,
            "reason": "Command failed: pytest exited 2",
            "session": "ses_def", "harness": "opencode",
        },
        {
            "ts": "2026-11-16T11:30:00", "op": "tool", "tool": "read",
            "intent": "read", "handled": True, "is_error": False,
            "harness": "opencode",
        },
        "{this is not json",
        {
            "ts": "2026-11-16T12:00:00", "op": "tool", "tool": "edit",
            "intent": "edit", "handled": False, "is_error": True,
            "reason": "Error: unmatched quote in patch",
            "harness": "opencode",
        },
        {
            "ts": "2026-11-16T12:30:00", "op": "after", "expired": True,
            "tool": "edit", "intent": "edit", "handled": True,
            "is_error": False,
        },
    ])
    opencode = _opencode(tmp_path / "opencode.log", [
        (
            "2026-11-16T09:00:00.000Z", "ERROR", "Failed to drain Session",
            "Session.StepFailedError: Compaction summary reached the output "
            "token limit\n    at SessionRunner.drain (chunk.js:1:2)",
        ),
        (
            "2026-11-16T09:10:00.000Z", "ERROR", "Failed to drain Session",
            "Session.StepFailedError: Compaction summary reached the output "
            "token limit\n    at SessionRunner.drain (chunk.js:2:3)",
        ),
        (
            "2026-11-16T09:20:00.000Z", "WARN", "failed to load plugin",
            "PluginModule.LoadError: Plugin must export a default definition",
        ),
    ])
    eval_run = _eval_run(tmp_path / "eval-run.json")
    generic = _jsonl(tmp_path / "agent.jsonl", [
        {
            "ts": "2026-11-16T13:00:00Z", "level": "WARNING",
            "message": (
                "prompt=my private prompt api_key=sk-live-zzzzzzzz "
                "contact bob@example.com tool=shell"),
            "tool": "shell", "harness": "opencode", "model": "provider/model-x",
            "task_class": "tests",
        },
        {
            "ts": "2026-11-16T13:05:00Z", "level": "error",
            "type": "error",
            "message": "Authorization: Bearer token-value-1234567890",
            "tool": "read", "harness": "opencode",
        },
        {
            "ts": "2026-11-16T13:06:00Z", "role": "user",
            "type": "message", "content": "SECRET_CONVERSATION_PROMPT",
        },
        {
            "ts": "2026-11-16T13:07:00Z", "level": "error",
            "message": "file failure", "content": "SECRET_FILE_CONTENT",
        },
    ])
    return [journal, opencode, eval_run, generic]


def _finding(report: dict, *, error_type: str | None = None,
             tool: str | None = None) -> dict:
    for item in report["findings"]:
        if error_type is not None and item["error_type"] != error_type:
            continue
        if tool is not None and item["dimensions"].get("tool") != tool:
            continue
        return item
    raise AssertionError(f"no finding for {error_type=} {tool=}")


def test_reads_synthetic_sources_and_clusters(tmp_path: Path) -> None:
    paths = _all_fixtures(tmp_path)
    report = L.inspect_logs(paths, root=tmp_path)

    assert report["schema"] == L.SCHEMA
    assert report["truth_assessed"] is False
    assert report["local_only"] is True
    assert report["uploaded"] is False
    assert report["applied"] is False
    kinds = {Path(item["path"]).name: item["kind"]
             for item in report["inputs"]}
    assert kinds["daemon_journal.jsonl"] == "jsonl"
    assert kinds["opencode.log"] == "opencode"
    assert kinds["eval-run.json"] == "eval"
    assert kinds["agent.jsonl"] == "jsonl"
    assert report["summary"]["malformed_lines"] == 1

    shell = _finding(report, error_type="DaemonToolError", tool="shell")
    assert shell["count"] == 2
    assert shell["dimensions"]["intent"] == "test"
    assert shell["dimensions"]["harness"] == "opencode"
    assert shell["likely_component"] == "daemon tool"
    assert shell["evidence"]["occurrences"] == 2
    assert shell["first_seen"] and shell["last_seen"]
    assert shell["first_seen"] < shell["last_seen"]

    session = _finding(report, error_type="Session.StepFailedError")
    assert session["count"] == 2
    assert session["dimensions"]["harness"] == "opencode"

    eval_failure = _finding(report, error_type="DriverError")
    assert eval_failure["dimensions"]["model"] == "provider/model-x"
    assert eval_failure["dimensions"]["task_class"] == "fix-bug"
    assert eval_failure["dimensions"]["tool"] == "eval"

    malformed = _finding(report, error_type="MalformedInput")
    assert malformed["count"] == 1
    assert malformed["proposed_action"]["action"] == "capture_rule"
    assert malformed["proposed_action"]["kind"] == "proposal"
    assert malformed["proposed_action"]["auto_applied"] is False


def test_findings_are_ranked_by_severity_frequency_and_recency(
        tmp_path: Path) -> None:
    paths = _all_fixtures(tmp_path)
    report = L.inspect_logs(paths, root=tmp_path)
    findings = report["findings"]
    assert findings[0]["severity"] == "high"
    ranks = [item["rank"] for item in findings]
    assert ranks == list(range(1, len(findings) + 1))
    scores = [item["evidence"]["occurrences"] for item in findings
              if item["severity"] == "high"]
    assert scores == sorted(scores, reverse=True)
    warning = _finding(report, error_type="LogWarning")
    assert warning["severity"] == "medium"
    assert findings[0]["rank"] < warning["rank"]


def test_ids_and_reports_are_deterministic(tmp_path: Path) -> None:
    paths = _all_fixtures(tmp_path)
    first = L.inspect_logs(paths, root=tmp_path)
    second = L.inspect_logs(paths, root=tmp_path)
    assert [(item["id"], item["count"], item["signature"])
            for item in first["findings"]] == [
        (item["id"], item["count"], item["signature"])
        for item in second["findings"]]


def test_eval_jsonl_runs_use_eval_dimensions(tmp_path: Path) -> None:
    path = _jsonl(tmp_path / "eval-runs.jsonl", [
        {
            "task": "fix-bug", "arm": "graph", "success": False,
            "driver": "opencode", "model": "provider/model-y",
            "driver_error": "Provider blocked the response",
            "started_at": "2026-11-16T12:00:00Z",
        },
        {
            "task": "ok-task", "arm": "graph", "success": True,
            "driver": "opencode",
        },
    ])
    report = L.inspect_logs([path])
    assert report["inputs"][0]["kind"] == "eval"
    finding = _finding(report, error_type="DriverError")
    assert finding["dimensions"] == {
        "tool": "eval", "intent": "graph", "harness": "opencode",
        "model": "provider/model-y", "task_class": "fix-bug",
    }
    assert finding["count"] == 1


def test_clusters_split_by_tool_harness_and_task_class(tmp_path: Path) -> None:
    path = _journal(tmp_path / "journal.jsonl", [
        {
            "ts": "2026-11-16T10:00:00Z", "op": "tool", "tool": "shell",
            "intent": "test", "is_error": True, "reason": "Command failed",
            "harness": "opencode", "task_class": "one",
        },
        {
            "ts": "2026-11-16T10:01:00Z", "op": "tool", "tool": "read",
            "intent": "test", "is_error": True, "reason": "Command failed",
            "harness": "opencode", "task_class": "one",
        },
        {
            "ts": "2026-11-16T10:02:00Z", "op": "tool", "tool": "shell",
            "intent": "test", "is_error": True, "reason": "Command failed",
            "harness": "other", "task_class": "one",
        },
        {
            "ts": "2026-11-16T10:03:00Z", "op": "tool", "tool": "shell",
            "intent": "test", "is_error": True, "reason": "Command failed",
            "harness": "opencode", "task_class": "two",
        },
    ])
    report = L.inspect_logs([path])
    clusters = [item for item in report["findings"]
                if item["error_type"] == "DaemonToolError"]
    assert len(clusters) == 4
    assert all(item["count"] == 1 for item in clusters)


def test_redaction_counts_values_but_never_emits_them(tmp_path: Path) -> None:
    path = _jsonl(tmp_path / "private.jsonl", [
        {
            "ts": "2026-11-16T13:00:00Z", "level": "ERROR",
            "message": (
                "prompt=do not print this prompt api_key=sk-live-"
                "supersecret email=person@example.com "
                "Authorization: Bearer hidden-token-value"),
            "tool": "shell",
        },
        {
            "ts": "2026-11-16T13:01:00Z", "level": "error",
            "message": "failed to read content=PRIVATE FILE BODY",
        },
        {
            "ts": "2026-11-16T13:02:00Z", "level": "error",
            "message": (
                "failed input={\"a\":{}, \"b\": \"PRIVATE NESTED PAYLOAD\"}"),
        },
        {
            "ts": "2026-11-16T13:03:00Z", "role": "assistant",
            "content": "PRIVATE ASSISTANT MESSAGE",
        },
    ])
    report = L.inspect_logs([path])
    output = L.report_json(report)
    assert "supersecret" not in output
    assert "do not print this prompt" not in output
    assert "person@example.com" not in output
    assert "hidden-token-value" not in output
    assert "PRIVATE FILE BODY" not in output
    assert "PRIVATE NESTED PAYLOAD" not in output
    assert "PRIVATE ASSISTANT MESSAGE" not in output
    counts = report["redactions"]
    assert counts.get("api_key", 0) >= 1
    assert counts.get("email", 0) >= 1
    assert counts.get("prompt", 0) >= 1
    assert counts.get("authorization", 0) >= 1
    redacted_evidence = sum(
        item["evidence"]["redacted_values"] for item in report["findings"])
    assert redacted_evidence >= 1


def test_malformed_lines_are_tolerated_and_proposed_as_capture_rule(
        tmp_path: Path) -> None:
    path = _journal(tmp_path / "daemon_journal.jsonl", [
        "not json at all",
        "{broken",
        "[]",
    ])
    report = L.inspect_logs([path])
    source = report["inputs"][0]
    assert source["kind"] == "jsonl"
    assert source["malformed_lines"] == 3
    finding = _finding(report, error_type="MalformedInput")
    assert finding["count"] == 3
    assert finding["proposed_action"]["action"] == "capture_rule"
    assert "unparseable JSONL line(s)" in finding["signature"]


def test_gzip_inputs_are_read_with_decompressed_bounds(tmp_path: Path) -> None:
    path = tmp_path / "daemon_journal.jsonl.gz"
    lines = [
        json.dumps({
            "ts": "2026-11-16T10:00:00Z", "op": "tool", "tool": "shell",
            "intent": "test", "is_error": True, "reason": "Command failed",
            "harness": "opencode",
        }),
        json.dumps({
            "ts": "2026-11-16T10:01:00Z", "op": "tool", "tool": "shell",
            "intent": "test", "is_error": True, "reason": "Command failed",
            "harness": "opencode",
        }),
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    report = L.inspect_logs([path])
    assert report["inputs"][0]["kind"] == "jsonl"
    assert report["inputs"][0]["bytes_read"] > 0
    assert _finding(report, error_type="DaemonToolError")["count"] == 2

    bounded = L.inspect_logs([path], limits=L.Limits(max_bytes=40))
    assert bounded["inputs"][0]["truncated_bytes"] is True
    assert bounded["inputs"][0]["bytes_read"] == 40


def test_bounded_file_line_and_byte_limits(tmp_path: Path) -> None:
    one = _journal(tmp_path / "one.jsonl", [
        {"ts": f"2026-11-16T10:0{index}:00Z", "op": "tool",
         "tool": "shell", "intent": "test", "is_error": True,
         "reason": f"Command failed {index}", "harness": "opencode"}
        for index in range(5)
    ])
    two = _journal(tmp_path / "two.jsonl", [
        {"ts": "2026-11-16T11:00:00Z", "op": "tool", "tool": "read",
         "intent": "test", "is_error": True, "reason": "Command failed",
         "harness": "opencode"},
    ])

    limited_files = L.inspect_logs([one, two], limits=L.Limits(max_files=1))
    assert limited_files["summary"]["inputs_skipped"] == 1
    assert limited_files["inputs"][1]["skipped"] == "max_files"
    assert len(limited_files["findings"]) == 1

    limited_lines = L.inspect_logs([one], limits=L.Limits(max_lines=2))
    source = limited_lines["inputs"][0]
    assert source["truncated_lines"] is True
    assert _finding(limited_lines, error_type="DaemonToolError")["count"] == 2

    limited_bytes = L.inspect_logs([one], limits=L.Limits(max_bytes=30))
    assert limited_bytes["inputs"][0]["truncated_bytes"] is True
    assert limited_bytes["inputs"][0]["bytes_read"] == 30


def test_since_filters_timestamped_records_only(tmp_path: Path) -> None:
    path = _journal(tmp_path / "journal.jsonl", [
        {"ts": "2020-01-01T00:00:00Z", "op": "tool", "tool": "shell",
         "intent": "test", "is_error": True, "reason": "Old failure",
         "harness": "opencode"},
        {"ts": "2026-11-16T00:00:00Z", "op": "tool", "tool": "shell",
         "intent": "test", "is_error": True, "reason": "New failure",
         "harness": "opencode"},
        {"level": "error", "message": "Untimestamped failure"},
    ])
    report = L.inspect_logs([path], since="2025-01-01T00:00:00Z")
    signatures = " ".join(item["signature"] for item in report["findings"])
    assert "Old failure" not in signatures
    assert "New failure" in signatures
    assert "Untimestamped failure" in signatures
    with pytest.raises(L.LogInspectError):
        L.inspect_logs([path], since="not-a-time")


def test_no_automatic_home_or_root_scan(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    hidden = home / "logs" / "opencode.log"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(
        'timestamp=2026-11-16T00:00:00Z level=ERROR message="hidden" '
        'cause="HiddenError: should not be read"\n',
        encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(L.LogInspectError, match="explicit input"):
        L.inspect_logs([])

    explicit = tmp_path / "explicit.log"
    explicit.write_text("2026-11-16T00:00:00Z ERROR ExplicitError: visible\n",
                        encoding="utf-8")
    report = L.inspect_logs(["explicit.log"], root=tmp_path)
    assert len(report["inputs"]) == 1
    assert report["inputs"][0]["name"] == "source_1"
    output = L.report_json(report)
    assert "HiddenError" not in output
    assert "ExplicitError" in output


def test_proposed_task_is_bounded_and_validates(tmp_path: Path) -> None:
    paths = _all_fixtures(tmp_path)
    report = L.inspect_logs(paths, root=tmp_path)
    spec = L.proposed_task(report)
    assert spec["tools"] == ["read"]
    assert spec["write_scope"] == []
    assert spec["approval"] == {"mutate": "deny", "network": "deny"}
    assert spec["budget"]["model_calls"] == 0
    assert spec["budget"]["cost_usd"] == 0.0
    assert spec["steps"][0]["tool"] == "read"
    assert spec["steps"][0]["write_paths"] == []
    assert spec["steps"][0]["args"]["upload"] is False
    assert spec["steps"][0]["args"]["truth_assessed"] is False

    spec_path = tmp_path / "log_inspect.json"
    L.atomic_write(spec_path, L.task_spec_json(report))
    validation = T.validate_spec(spec_path)
    assert validation["ok"], validation["errors"]
    loaded = T.load_spec(spec_path)
    assert loaded.id == "log_inspect"
    assert loaded.requires_graph is False


def test_cli_json_text_out_and_exit_codes(tmp_path: Path,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    paths = _all_fixtures(tmp_path)
    args = ["--log", str(paths[0]), "--log", str(paths[1]),
            "--log", str(paths[2]), "--log", str(paths[3])]

    assert CLI.main([*args, "--json"]) == 0
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["truth_assessed"] is False
    assert report["summary"]["findings_shown"] <= 20
    assert "supersecret" not in stdout
    assert "SECRET_CONVERSATION_PROMPT" not in stdout
    assert "SECRET_FILE_CONTENT" not in stdout

    out_path = tmp_path / "report.txt"
    spec_path = tmp_path / "task.json"
    assert CLI.main([*args, "--out", str(out_path),
                     "--task-spec", str(spec_path)]) == 0
    text = capsys.readouterr().out
    assert "log inspection (local, redacted, read-only)" in text
    assert "proposed action (not applied)" in text
    assert "truth_assessed: false" in text
    assert out_path.read_text(encoding="utf-8") == text
    assert T.validate_spec(spec_path)["ok"] is True

    missing = str(tmp_path / "missing.log")
    assert CLI.main(["--log", missing, "--json"]) == 2
    captured = capsys.readouterr()
    unreadable = json.loads(captured.out)
    assert unreadable["errors"]
    assert "unreadable" in captured.err
    assert CLI.main(["--log", str(paths[0]), "--since", "nope"]) == 2
    capsys.readouterr()
