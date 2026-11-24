"""Metadata-only session profiles from the Codex, Kiro and Claude Code hooks."""
from __future__ import annotations

import json
import socketserver
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ozagia.core import status as status_module
from ozagia.core.working_set import BRIEF_HEADER
from ozagia.daemon import protocol
from ozagia.daemon.server import DaemonState
from ozagia.integrations import session_profile
from ozagia.integrations.claude_code import hook as claude_hook
from ozagia.integrations.codex import hook as codex_hook
from ozagia.integrations.kiro import hook as kiro_hook
from ozagia.session_profile_cli import report, store_profile_event

CANARY = "CANARY-DO-NOT-PERSIST"
CANARY_PROMPT = f"{CANARY}-PROMPT"
CANARY_PATH = f"/private/{CANARY}-PATH.py"
CANARY_COMMAND = f"echo {CANARY}-COMMAND"
CANARY_OUTPUT = f"{CANARY}-OUTPUT"
CANARY_SECRET = f"{CANARY}-SECRET"


class ProfileDaemon:
    """A protocol peer that stores profile events through the real store."""

    def __init__(self, sock: Path, root: Path):
        self.sock = sock
        self.requests: list[dict] = []
        self.profile_requests: list[dict] = []
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    req = json.loads(line)
                    op = req.get("op")
                    if op == "profile_event":
                        daemon.profile_requests.append(req)
                        body = store_profile_event(root, req)
                    elif op == "working_set":
                        body = {"ok": True, "text": BRIEF_HEADER + "\nstub brief"}
                    elif op == "tool":
                        body = {"ok": True, "handled": False, "reason": "not handled"}
                    else:
                        body = {"ok": True, "append": ""}
                    self.wfile.write((json.dumps(
                        {"id": req.get("id"), **body}) + "\n").encode())
                    self.wfile.flush()
                    if op != "profile_event":
                        daemon.requests.append(req)

        self.server = socketserver.ThreadingUnixStreamServer(str(sock), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.sock.unlink(missing_ok=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture
def daemon(repo, tmp_path, monkeypatch):
    sock = tmp_path / "daemon.sock"
    stub = ProfileDaemon(sock, repo)
    monkeypatch.setenv(protocol.ENV_SOCK, str(sock))
    try:
        yield stub
    finally:
        stub.close()


def codex_base(repo: Path, event: str, **extra) -> dict:
    return {"session_id": "019a-sess", "transcript_path": CANARY_PATH,
            "cwd": str(repo), "hook_event_name": event, "model": "gpt-5.5",
            "permission_mode": "default", "prompt": CANARY_PROMPT, **extra}


def codex_tool(repo: Path, event: str, tool: str, tool_input: dict,
               **extra) -> dict:
    return codex_base(
        repo, event, turn_id="turn-1", tool_name=tool,
        tool_use_id="call_1", tool_input=tool_input, **extra)


def claude_base(repo: Path, event: str, tool: str = "", **extra) -> dict:
    return {"session_id": "abc123", "transcript_path": CANARY_PATH,
            "cwd": str(repo), "permission_mode": "default",
            "hook_event_name": event, "model": "claude-sonnet-4-5",
            "tool_name": tool, "tool_use_id": "toolu_01",
            "prompt": CANARY_PROMPT, **extra}


def kiro_event(repo: Path, event: str, **extra) -> dict:
    return {"hook_event_name": event, "cwd": str(repo),
            "session_id": "kiro-sess-1", "prompt": CANARY_PROMPT,
            "transcript_path": CANARY_PATH, **extra}


def _profile_files_text(repo: Path) -> str:
    directory = repo / ".ozagia" / "session_profiles"
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted(directory.glob("*.json")))


def _assert_content_free(repo: Path) -> None:
    text = _profile_files_text(repo)
    for canary in (CANARY_PROMPT, CANARY_PATH, CANARY_COMMAND,
                   CANARY_OUTPUT, CANARY_SECRET):
        assert canary not in text
    assert "transcript_path" not in text
    assert "tool_input" not in text
    assert "tool_response" not in text


def test_codex_hook_events_persist_metadata_only_and_dedupe(repo, daemon):
    codex_hook.handle(codex_base(repo, "SessionStart", source="startup"), repo)
    codex_hook.handle(codex_tool(
        repo, "PreToolUse", "Bash", {"command": CANARY_COMMAND}), repo)
    post = codex_tool(
        repo, "PostToolUse", "Bash", {"command": CANARY_COMMAND},
        tool_response={"output": CANARY_OUTPUT}, duration_ms=12,
        secret=CANARY_SECRET)
    codex_hook.handle(post, repo)
    codex_hook.handle(post, repo)  # a replayed hook event

    assert len(daemon.profile_requests) == 4
    profile = report(repo)["sessions"][0]["host_profile"]
    assert profile["source"] == "host_hooks"
    assert profile["models"] == ["gpt-5.5"]
    assert profile["model_calls"] == 0
    assert profile["tokens"] == {
        "input": None, "output": None, "reasoning": None,
        "cache_read": None, "cache_write": None,
    }
    assert profile["timing_ms"] == {
        "first_token": None, "streamed": None, "completed": None,
        "model_turn": None,
    }
    coverage = profile["coverage"]
    assert coverage["session_starts"] == 1
    assert coverage["tool_lifecycle_started"] == 1
    assert coverage["tool_lifecycle_terminal"] == 1
    assert coverage["host_tool_calls"] == 1
    assert profile["lifecycle"] == {
        "starts": 1, "ends": 0, "compactions": 0, "last": "start"}
    assert profile["tools"]["bash"]["started"]["count"] == 1
    assert profile["tools"]["bash"]["completed"]["duration_ms_sum"] == 12
    _assert_content_free(repo)


def test_claude_hook_events_cover_compact_tool_failure_and_duration(repo, daemon):
    claude_hook.handle(
        claude_base(repo, "SessionStart", source="compact"), repo)
    post = claude_base(
        repo, "PostToolUse", "Bash",
        tool_input={"command": CANARY_COMMAND},
        tool_response={"stdout": CANARY_OUTPUT}, duration_ms=7)
    claude_hook.handle(post, repo)
    claude_hook.handle(post, repo)  # a replayed hook event
    claude_hook.handle(claude_base(
        repo, "PostToolUseFailure", "WebFetch",
        tool_input={"url": CANARY_PATH},
        error=CANARY_SECRET), repo)

    profile = report(repo)["sessions"][0]["host_profile"]
    assert profile["models"] == ["claude-sonnet-4-5"]
    assert profile["lifecycle"]["compactions"] == 1
    assert profile["lifecycle"]["last"] == "compact"
    assert profile["coverage"]["session_compactions"] == 1
    assert profile["coverage"]["tool_lifecycle_terminal"] == 2
    assert profile["tools"]["bash"]["completed"]["count"] == 1
    assert profile["tools"]["bash"]["completed"]["duration_ms_sum"] == 7
    assert profile["tools"]["webfetch"]["error"]["untimed_count"] == 1
    _assert_content_free(repo)


def test_kiro_hook_events_include_session_end_and_dedupe_without_host_ids(repo, daemon):
    kiro_hook.handle(kiro_event(repo, "SessionStart"), repo)
    post = kiro_event(
        repo, "PostToolUse", tool_name="execute_bash",
        tool_input={"command": CANARY_COMMAND},
        tool_response={"result": [CANARY_OUTPUT]}, secret=CANARY_SECRET)
    kiro_hook.handle(post, repo)
    kiro_hook.handle(post, repo)  # Kiro supplies no tool id: digest dedupe
    kiro_hook.handle(kiro_event(repo, "SessionEnd", reason="other"), repo)

    profile = report(repo)["sessions"][0]["host_profile"]
    assert profile["models"] == []
    assert profile["coverage"]["tool_lifecycle_terminal"] == 1
    assert profile["tools"]["execute_bash"]["completed"]["untimed_count"] == 1
    assert profile["lifecycle"] == {
        "starts": 1, "ends": 1, "compactions": 0, "last": "end"}
    _assert_content_free(repo)


def test_profile_store_accepts_host_events_and_rejects_malformed_ones(repo):
    state = object.__new__(DaemonState)
    state.root = repo
    state.owners = SimpleNamespace(touch=lambda *_: None)
    start = {"op": "profile_event", "harness": "codex", "session": "bad-shape",
             "source": "host_hooks", "kind": "session", "event_id": "start-1",
             "metadata": {"phase": "start", "tokens": {
                 "input": 7, "output": "not numeric"}}}
    assert state.dispatch(start) == {"ok": True, "stored": True}
    assert state.dispatch(start) == {"ok": True, "stored": False,
                                     "duplicate": True}
    profile = report(repo)["sessions"][0]["host_profile"]
    assert profile["tokens"]["input"] == 7
    assert profile["tokens"]["output"] is None

    bad_phase = {**start, "event_id": "start-2",
                 "metadata": {"phase": "invented"}}
    assert state.dispatch(bad_phase)["ok"] is False
    bad_status = {**start, "kind": "tool", "event_id": "tool-1",
                  "metadata": {"tool": "read", "status": "running"}}
    assert state.dispatch(bad_status)["ok"] is False
    bad_kind = {**start, "kind": "invented", "event_id": "event-1"}
    assert state.dispatch(bad_kind)["ok"] is False
    assert "invalid profile event" in state.dispatch(bad_kind)["error"]


def test_profile_record_is_fail_open_without_a_daemon(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(protocol.ENV_SOCK, str(tmp_path / "missing.sock"))
    payload = codex_base(repo, "SessionStart", source="startup")
    assert session_profile.record(
        repo, "codex", payload, kind="session", event="SessionStart",
        phase="start") is False
    assert session_profile.record(
        repo, "codex", {"session_id": "/private/path"}, kind="session",
        event="SessionStart", phase="start") is False
    assert not (repo / ".ozagia" / "session_profiles").exists()


def test_status_summary_still_reads_host_profiles(repo, daemon):
    store_profile_event(repo, {
        "session_id": "status-session", "harness": "codex",
        "source": "host_hooks", "kind": "session", "event_id": "status-start",
        "metadata": {"phase": "start", "model": "gpt-5.5"},
    })
    store_profile_event(repo, {
        "session_id": "status-session", "harness": "codex",
        "source": "host_hooks", "kind": "tool", "event_id": "status-tool",
        "metadata": {"tool": "bash", "status": "completed", "duration_ms": 9},
    })

    section = status_module._sessions_status(repo, [])
    assert section["state"] == "ok"
    summary = section["report"]
    assert summary["profiles"] == 1
    assert summary["models"] == ["gpt-5.5"]
    assert summary["totals"]["tool_calls"] == 1
    assert summary["totals"]["tokens"] == {
        "input": None, "output": None, "reasoning": None,
        "cache_read": None, "cache_write": None,
    }
    (row,) = summary["sessions"]
    assert set(row) == {
        "harness", "label", "observed_at", "age_s", "source", "model_calls",
        "models", "tokens", "timing_ms", "tool_calls", "coverage",
        "capture_diagnostics",
    }
    assert row["source"] == "host_hooks"
    assert CANARY not in json.dumps(summary)
    assert "status-session" not in json.dumps(summary)
