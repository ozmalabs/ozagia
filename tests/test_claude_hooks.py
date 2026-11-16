"""Claude Code hooks: payload mapping, local git refusal, install/uninstall.

The hook runs as a subprocess, as Claude Code runs it, against a stub daemon
that speaks the protocol over a Unix socket. Payload shapes follow the
examples in the Claude Code hooks reference (see docs/claude_code.md).
"""
from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from ozagia.daemon import protocol
from ozagia.integrations.claude_code import hook, install as inst

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


# --------------------------------------------------------------------------- #
# stub daemon
# --------------------------------------------------------------------------- #
class StubDaemon:
    """A protocol peer that records requests and answers with `reply(req)`."""

    def __init__(self, sock: Path, reply):
        self.sock = sock
        self.reply = reply
        self.requests: list[dict] = []
        stub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    req = json.loads(line)
                    stub.requests.append(req)
                    resp = {"id": req.get("id"), **stub.reply(req)}
                    self.wfile.write((json.dumps(resp) + "\n").encode())
                    self.wfile.flush()

        self.server = socketserver.ThreadingUnixStreamServer(str(sock), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.sock.unlink(missing_ok=True)


@pytest.fixture
def sockdir():
    d = Path(tempfile.mkdtemp(prefix="ozcc", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    return root


def _after_reply(req):
    if req["op"] == "after":
        return {"ok": True, "append": f"[ozagia] context for {req['tool']}", "meta": {}}
    if req["op"] == "tool":
        return {"ok": True, "handled": True, "output": "sym:src/a.py::f\n1\tdef f(x):",
                "meta": {"intent": "node"}}
    return {"ok": True}


@pytest.fixture
def daemon(sockdir):
    stubs = []

    def start(reply=_after_reply, sock=None):
        stub = StubDaemon(sock or sockdir / "d.sock", reply)
        stubs.append(stub)
        return stub
    yield start
    for s in stubs:
        s.close()


def run_hook(payload, *, sock: Path | None, stdin: str | None = None, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_") and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC)
    if sock is not None:
        env[protocol.ENV_SOCK] = str(sock)
    data = stdin if stdin is not None else json.dumps(payload)
    proc = subprocess.run([sys.executable, "-m", "ozagia.integrations.claude_code", "hook"],
                          input=data, capture_output=True, text=True, env=env, cwd=cwd,
                          timeout=hook.HOOK_TIMEOUT_S + 10)
    return proc


def base(repo, event, tool, tool_input, **extra):
    return {"session_id": "abc123",
            "transcript_path": "/home/user/.claude/projects/x/transcript.jsonl",
            "cwd": str(repo), "permission_mode": "default",
            "hook_event_name": event, "tool_name": tool, "tool_input": tool_input,
            "tool_use_id": "toolu_01ABC123", **extra}


def post(repo, tool, tool_input, tool_response):
    return base(repo, "PostToolUse", tool, tool_input, tool_response=tool_response,
                duration_ms=12)


def context_of(proc):
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PostToolUse"
    return spec["additionalContext"]


# --------------------------------------------------------------------------- #
# PostToolUse: mapping onto `after`
# --------------------------------------------------------------------------- #
def test_post_read_maps_to_read(repo, daemon):
    stub = daemon()
    fp = str(repo / "src" / "a.py")
    resp = {"type": "text", "file": {"filePath": fp, "content": "def f(x):\n    return x\n",
                                     "numLines": 2, "startLine": 1, "totalLines": 2}}
    proc = run_hook(post(repo, "Read", {"file_path": fp, "offset": 1, "limit": 50}, resp),
                    sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for read"
    (req,) = stub.requests
    assert req["op"] == "after" and req["harness"] == "claude"
    assert req["session"] == "abc123" and req["tool"] == "read"
    assert req["input"] == {"path": fp, "offset": 1, "limit": 50}
    assert req["output"] == "def f(x):\n    return x\n"


def test_post_grep_maps_glob_to_include(repo, daemon):
    stub = daemon()
    ti = {"pattern": "def f", "path": str(repo), "glob": "*.py",
          "output_mode": "content", "-i": False}
    resp = {"mode": "content", "numFiles": 1, "filenames": [],
            "content": "src/a.py:1:def f(x):", "numLines": 1}
    proc = run_hook(post(repo, "Grep", ti, resp), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for grep"
    (req,) = stub.requests
    assert req["tool"] == "grep"
    assert req["input"] == {"pattern": "def f", "path": str(repo), "include": "*.py",
                            "literal": False}
    assert req["output"] == "src/a.py:1:def f(x):"


def test_post_glob(repo, daemon):
    stub = daemon()
    resp = {"filenames": [str(repo / "src" / "a.py")], "numFiles": 1, "truncated": False}
    proc = run_hook(post(repo, "Glob", {"pattern": "**/*.py"}, resp), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for glob"
    (req,) = stub.requests
    assert req["tool"] == "glob" and req["input"] == {"pattern": "**/*.py"}
    assert req["output"] == str(repo / "src" / "a.py")


def test_post_edit_maps_fields(repo, daemon):
    stub = daemon()
    fp = str(repo / "src" / "a.py")
    ti = {"file_path": fp, "old_string": "return x", "new_string": "return x + 1",
          "replace_all": False}
    resp = {"filePath": fp, "oldString": "return x", "newString": "return x + 1",
            "originalFile": "def f(x):\n    return x\n", "structuredPatch": [],
            "userModified": False, "replaceAll": False}
    proc = run_hook(post(repo, "Edit", ti, resp), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for edit"
    (req,) = stub.requests
    assert req["tool"] == "edit"
    assert req["input"] == {"path": fp, "oldString": "return x",
                            "newString": "return x + 1", "replaceAll": False}
    assert "originalFile" not in req["output"] and fp in req["output"]


def test_post_multiedit_is_one_edit_after(repo, daemon):
    stub = daemon()
    fp = str(repo / "src" / "a.py")
    ti = {"file_path": fp, "edits": [
        {"old_string": "def f", "new_string": "def g"},
        {"old_string": "return x", "new_string": "return 2 * x", "replace_all": True}]}
    proc = run_hook(post(repo, "MultiEdit", ti, {"filePath": fp}), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for edit"
    (req,) = stub.requests
    assert req["tool"] == "edit" and req["input"]["path"] == fp
    assert req["input"]["edits"] == [
        {"oldString": "def f", "newString": "def g", "replaceAll": False},
        {"oldString": "return x", "newString": "return 2 * x", "replaceAll": True}]


def test_post_write_doc_example(repo, daemon):
    # The PostToolUse example from the hooks reference.
    stub = daemon()
    ti = {"file_path": "/path/to/file.txt", "content": "file content"}
    proc = run_hook(post(repo, "Write", ti, {"filePath": "/path/to/file.txt",
                                             "type": "create"}), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for write"
    (req,) = stub.requests
    assert req["tool"] == "write"
    assert req["input"] == {"path": "/path/to/file.txt", "content": "file content"}
    assert json.loads(req["output"]) == {"filePath": "/path/to/file.txt", "type": "create"}


def test_post_bash_maps_to_shell(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "append": "FAILED test:tests/t.py::test_f",
                               "meta": {}})
    ti = {"command": "python -m pytest -q", "description": "Run tests",
          "timeout": 120000, "run_in_background": False}
    resp = {"stdout": "1 failed", "stderr": "warn", "interrupted": False, "isImage": False}
    proc = run_hook(post(repo, "Bash", ti, resp), sock=stub.sock)
    assert context_of(proc) == "FAILED test:tests/t.py::test_f"
    (req,) = stub.requests
    assert req["tool"] == "shell"
    assert req["input"] == {"command": "python -m pytest -q", "workdir": str(repo),
                            "timeout": 120000, "description": "Run tests"}
    assert req["output"] == "1 failed\nwarn"


def test_post_empty_append_prints_nothing(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "append": "", "meta": {}})
    proc = run_hook(post(repo, "Glob", {"pattern": "*"}, {"filenames": []}), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert len(stub.requests) == 1


def test_post_unmapped_tool_sends_nothing(repo, daemon):
    stub = daemon()
    proc = run_hook(post(repo, "WebSearch", {"query": "x"}, "page"), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert stub.requests == []


def test_post_webfetch_maps_to_webfetch(repo, daemon):
    stub = daemon()
    url = "https://arxiv.org/abs/2401.12345"
    proc = run_hook(post(repo, "WebFetch", {"url": url, "prompt": "summarise"},
                         "Request failed with status code 404"), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for webfetch"
    (req,) = stub.requests
    assert req["op"] == "after" and req["tool"] == "webfetch"
    assert req["input"] == {"url": url}
    assert req["output"] == "Request failed with status code 404"


def test_post_webfetch_real_response_object_is_flattened(repo, daemon):
    """Claude's WebFetch `tool_response` is an object; a non-2xx `code`
    leads, then `result`, so the failure checks see them."""
    stub = daemon()
    url = "https://www.nature.com/articles/s41586-020-0000-0"
    response = {"bytes": 1234, "code": 404, "codeText": "Not Found",
                "result": "The page could not be found.", "durationMs": 210,
                "url": url}
    proc = run_hook(post(repo, "WebFetch", {"url": url, "prompt": "summarise"}, response),
                    sock=stub.sock)
    assert proc.returncode == 0, proc.stderr
    (req,) = stub.requests
    assert req["output"] == "HTTP 404 Not Found\nThe page could not be found."
    from ozagia.research.fallback import after_lines
    lines, reason = after_lines(req["input"], req["output"])
    assert reason == "http 404" and lines and "research_fetch" in lines[0]
    # A 200 sends only the result text.
    stub.requests.clear()
    ok = {**response, "code": 200, "codeText": "OK", "result": "A summary."}
    run_hook(post(repo, "WebFetch", {"url": url, "prompt": "p"}, ok), sock=stub.sock)
    assert stub.requests[0]["output"] == "A summary."


def test_webfetch_text_shapes():
    from ozagia.integrations.claude_code.hook import webfetch_text
    assert webfetch_text("plain") == "plain"
    assert webfetch_text({"code": 403, "result": ""}) == "HTTP 403"
    assert webfetch_text({"code": 200, "result": "%PDF-1.7 x"}) == "%PDF-1.7 x"
    assert webfetch_text({"stdout": "x"}) == "x"     # not the WebFetch shape


def test_post_failure_webfetch_sends_the_error(repo, daemon):
    stub = daemon()
    url = "https://doi.org/10.1234/abc"
    payload = base(repo, "PostToolUseFailure", "WebFetch", {"url": url, "prompt": "p"},
                   error="Request failed with status code 403", is_interrupt=False)
    proc = run_hook(payload, sock=stub.sock)
    assert proc.returncode == 0, proc.stderr
    spec = json.loads(proc.stdout)["hookSpecificOutput"]
    assert spec == {"hookEventName": "PostToolUseFailure",
                    "additionalContext": "[ozagia] context for webfetch"}
    (req,) = stub.requests
    assert req["tool"] == "webfetch" and req["input"] == {"url": url}
    assert req["output"] == "Request failed with status code 403"
    # Other tools' failures are not sent.
    stub.requests.clear()
    other = base(repo, "PostToolUseFailure", "Bash", {"command": "false"}, error="exit 1")
    proc = run_hook(other, sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == "" and stub.requests == []


def test_default_socket_path_from_cwd(repo, daemon, monkeypatch):
    monkeypatch.delenv(protocol.ENV_SOCK, raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    sock = protocol.socket_path(repo)
    sock.parent.mkdir(parents=True, exist_ok=True)
    stub = daemon(sock=sock)
    proc = run_hook(post(repo, "Glob", {"pattern": "*"}, {"filenames": []}), sock=None)
    assert context_of(proc) == "[ozagia] context for glob"
    assert len(stub.requests) == 1


# --------------------------------------------------------------------------- #
# failures never block
# --------------------------------------------------------------------------- #
def _log_lines(repo):
    path = repo / ".ozagia" / hook.LOG_NAME
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_daemon_down_prints_nothing_and_logs(repo, sockdir):
    proc = run_hook(post(repo, "Read", {"file_path": str(repo / "src" / "a.py")}, "x"),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] == "PostToolUse" and entry["tool"] == "Read"
    assert entry["session"] == "abc123" and entry["error"]


def test_daemon_error_prints_nothing_and_logs(repo, daemon):
    stub = daemon(lambda req: {"ok": False, "error": "boom"})
    proc = run_hook(post(repo, "Glob", {"pattern": "*"}, {"filenames": []}), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    (entry,) = _log_lines(repo)
    assert "boom" in entry["error"]


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '{"hook_event_name": '])
def test_malformed_stdin_exits_zero(repo, stdin, sockdir):
    proc = run_hook(None, sock=sockdir / "missing.sock", stdin=stdin, cwd=repo)
    assert proc.returncode == 0 and proc.stdout == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] is None and entry["error"]


def test_slow_daemon_times_out(sockdir):
    import time

    def slow(req):
        time.sleep(1.0)
        return {"ok": True}
    stub = StubDaemon(sockdir / "slow.sock", slow)
    try:
        start = time.monotonic()
        with pytest.raises((hook.HookError, TimeoutError, OSError)):
            hook.daemon_request(stub.sock, {"id": 1, "op": "ping"}, timeout=0.2)
        assert time.monotonic() - start < 0.9
    finally:
        stub.close()


# --------------------------------------------------------------------------- #
# PreToolUse
# --------------------------------------------------------------------------- #
def pre(repo, tool, tool_input):
    return base(repo, "PreToolUse", tool, tool_input)


def decision_of(proc):
    assert proc.returncode == 0, proc.stderr
    spec = json.loads(proc.stdout)["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    return spec["permissionDecision"], spec["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "git commit -m 'fix'",
    "git -C x commit -m y",
    "cd sub && git push origin main",
    "git status; git stash",
    "FOO=1 git -c user.name=a --no-pager reset --hard",
    "/usr/bin/git checkout -b new",
    "echo ok\ngit rebase main",
    "(git tag v1)",
    "git worktree add ../w",
    # review fix 10: forms the intent layer used to miss; one shared list
    "git cherry-pick abc",
    "git bisect start HEAD~3",
    "echo `git stash`",
    "bash -c 'git reset --hard'",
    "git -c alias.c=commit c -m x",
])
def test_pre_bash_ref_moving_git_denied(repo, command, sockdir):
    proc = run_hook(pre(repo, "Bash", {"command": command}), sock=sockdir / "missing.sock")
    decision, reason = decision_of(proc)
    assert decision == "deny" and "git" in reason


@pytest.mark.parametrize("command", [
    "git status", "git -C x diff HEAD", "git log --oneline -5", "npm test",
    "echo 'git commit'", "grep -rn commit src", "git show HEAD:src/a.py",
    # listing forms, as in the intent layer
    "git branch", "git tag -l", "git stash list", "git worktree list",
])
def test_pre_bash_other_commands_allowed(repo, command, sockdir):
    proc = run_hook(pre(repo, "Bash", {"command": command}), sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == ""


def test_pre_bash_doc_example_allowed(repo, sockdir):
    ti = {"command": "npm test", "description": "Run test suite", "timeout": 120000,
          "run_in_background": False}
    proc = run_hook(pre(repo, "Bash", ti), sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == ""


@pytest.mark.parametrize("file_path", ["sym:src/a.py::f", "{repo}/sym:src/a.py::f"])
def test_pre_read_node_key_denied_with_daemon_output(repo, daemon, file_path):
    stub = daemon()
    proc = run_hook(pre(repo, "Read", {"file_path": file_path.format(repo=repo)}),
                    sock=stub.sock)
    decision, reason = decision_of(proc)
    assert decision == "deny"
    assert "sym:src/a.py::f\n1\tdef f(x):" in reason
    (req,) = stub.requests
    assert req["op"] == "tool" and req["tool"] == "read" and req["harness"] == "claude"
    assert req["input"] == {"path": "sym:src/a.py::f"}


def test_pre_read_node_key_unhandled_lets_builtin_run(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "handled": False, "reason": "unknown key"})
    proc = run_hook(pre(repo, "Read", {"file_path": "test:tests/t.py::test_x"}),
                    sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""


def test_pre_read_plain_file_only_asks_for_a_repeat(repo, daemon):
    stub = daemon()
    proc = run_hook(pre(repo, "Read", {"file_path": str(repo / "src" / "a.py")}),
                    sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    (req,) = stub.requests
    assert req["op"] == "read_check" and req["tool"] == "read"
    assert req["input"] == {"path": str(repo / "src" / "a.py")}
    assert "sub" not in req


# --------------------------------------------------------------------------- #
# read ledger: PreToolUse repeat checks, session events
# --------------------------------------------------------------------------- #
STUB = "[ozagia] src/a.py unchanged since read 1 (lines 1-2, sha 0123456789ab)"


def _stub_reply(req):
    if req["op"] == "read_check":
        return {"ok": True, "repeat": True, "policy": "stub", "stub": STUB, "meta": {}}
    return {"ok": True}


def test_pre_read_repeat_is_denied_with_the_stub(repo, daemon):
    stub = daemon(_stub_reply)
    proc = run_hook(pre(repo, "Read", {"file_path": str(repo / "src" / "a.py"),
                                       "offset": 1, "limit": 2}), sock=stub.sock)
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == STUB
    (req,) = stub.requests
    assert req["input"] == {"path": str(repo / "src" / "a.py"), "offset": 1, "limit": 2}


def test_pre_read_note_policy_allows_silently(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "repeat": True, "policy": "note",
                               "stub": None, "meta": {}})
    proc = run_hook(pre(repo, "Read", {"file_path": str(repo / "src" / "a.py")}),
                    sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""


def test_pre_bash_shell_read_repeat_is_denied(repo, daemon):
    stub = daemon(_stub_reply)
    proc = run_hook(pre(repo, "Bash", {"command": "sed -n '1,2p' src/a.py"}),
                    sock=stub.sock)
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == STUB
    (req,) = stub.requests
    assert req["op"] == "read_check" and req["tool"] == "shell"


def test_pre_bash_non_read_does_not_ask(repo, daemon):
    stub = daemon(_stub_reply)
    proc = run_hook(pre(repo, "Bash", {"command": "make build"}), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert stub.requests == []


def test_pre_read_without_daemon_allows(repo, sockdir):
    proc = run_hook(pre(repo, "Read", {"file_path": str(repo / "src" / "a.py")}),
                    sock=sockdir / "none.sock")
    assert proc.returncode == 0 and proc.stdout == ""


def test_subagent_calls_carry_agent_id_as_sub(repo, daemon):
    stub = daemon()
    payload = pre(repo, "Read", {"file_path": str(repo / "src" / "a.py")})
    payload.update(agent_id="agent-7", agent_type="Explore")
    run_hook(payload, sock=stub.sock)
    fp = str(repo / "src" / "a.py")
    p2 = post(repo, "Read", {"file_path": fp}, {"file": {"content": "x"}})
    p2["agent_id"] = "agent-7"
    run_hook(p2, sock=stub.sock)
    assert [r.get("sub") for r in stub.requests] == ["agent-7", "agent-7"]


@pytest.mark.parametrize("source,event", [("compact", "compact"), ("clear", "start"),
                                          ("startup", "start"), ("resume", "start")])
def test_session_start_sends_session_event(repo, daemon, source, event):
    stub = daemon()
    payload = {"session_id": "abc123", "cwd": str(repo),
               "hook_event_name": "SessionStart", "source": source}
    proc = run_hook(payload, sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    # After a compaction the hook asks for the working set first (the stub
    # has none, so nothing is printed).
    ops = [r["op"] for r in stub.requests]
    assert ops == (["working_set", "session_event"] if source == "compact"
                   else ["session_event"])
    req = stub.requests[-1]
    assert req == {"id": 1, "op": "session_event", "harness": "claude",
                   "session": "abc123", "event": event, "compaction_hook": True}


def test_post_read_sends_the_lines_claude_showed(repo, daemon):
    stub = daemon()
    fp = str(repo / "src" / "a.py")
    run_hook(post(repo, "Read", {"file_path": fp},
                  {"type": "text", "file": {"filePath": fp, "content": "x\ny",
                                            "startLine": 3, "numLines": 2,
                                            "totalLines": 9}}), sock=stub.sock)
    (req,) = stub.requests
    assert req["delivered"] == {"start": 3, "lines": 2} and req["output"] == "x\ny"
    assert hook.delivered_lines("Bash", {"file": {"startLine": 1, "numLines": 1}}) is None
    assert hook.delivered_lines("Read", {"file": {"startLine": True, "numLines": 1}}) is None


def test_failed_session_start_is_carried_as_reset_pending(repo, daemon, sockdir):
    payload = {"session_id": "abc123", "cwd": str(repo),
               "hook_event_name": "SessionStart", "source": "compact"}
    assert run_hook(payload, sock=sockdir / "none.sock").returncode == 0
    stub = daemon()
    fp = str(repo / "src" / "a.py")
    run_hook(post(repo, "Read", {"file_path": fp}, {"file": {"content": "x"}}),
             sock=stub.sock)
    run_hook(post(repo, "Read", {"file_path": fp}, {"file": {"content": "x"}}),
             sock=stub.sock)
    first, second = stub.requests
    assert first["reset_pending"] is True and first["compaction_hook"] is True
    assert "reset_pending" not in second


def test_session_end_sends_end(repo, daemon):
    stub = daemon()
    run_hook({"session_id": "abc123", "cwd": str(repo),
              "hook_event_name": "SessionEnd", "reason": "other"}, sock=stub.sock)
    assert [r["event"] for r in stub.requests] == ["end"]


# --------------------------------------------------------------------------- #
# unit checks
# --------------------------------------------------------------------------- #
def test_node_key_ignores_existing_files(tmp_path):
    real = tmp_path / "doc:notes.md"
    real.write_text("x", encoding="utf-8")
    assert hook.node_key(str(real)) is None
    assert hook.node_key(str(tmp_path / "text:a.yaml#L1-L5")) == "text:a.yaml#L1-L5"
    assert hook.node_key("/abs/src/a.py") is None


def test_response_text_shapes():
    assert hook.response_text(None) == ""
    assert hook.response_text("plain") == "plain"
    assert hook.response_text([{"type": "text", "text": "a"},
                               {"type": "text", "text": "b"}]) == "a\nb"
    assert hook.response_text({"stdout": "o", "stderr": ""}) == "o"


# --------------------------------------------------------------------------- #
# install / uninstall
# --------------------------------------------------------------------------- #
EXISTING = {
    "permissions": {"allow": ["Bash(npm test)"]},
    "env": {"FOO": "1"},
    "hooks": {
        "PostToolUse": [{"matcher": "Edit|Write",
                         "hooks": [{"type": "command", "command": "npx prettier --write"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
    },
}


def _ours(data, event):
    return [(g["matcher"], h) for g in data["hooks"].get(event, [])
            for h in g["hooks"] if inst.is_ozagia_handler(h)]


def test_install_merges_and_is_idempotent(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(json.dumps(EXISTING), encoding="utf-8")
    assert inst.install(tmp_path, python="/opt/py/bin/python") == path
    inst.install(tmp_path, python="/opt/py/bin/python")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"] == EXISTING["permissions"] and data["env"] == {"FOO": "1"}
    assert data["hooks"]["Stop"] == EXISTING["hooks"]["Stop"]
    assert data["hooks"]["PostToolUse"][0] == EXISTING["hooks"]["PostToolUse"][0]
    cmd = "/opt/py/bin/python -m ozagia.integrations.claude_code hook"
    expected = {"type": "command", "command": cmd, "timeout": hook.HOOK_TIMEOUT_S}
    assert _ours(data, "PostToolUse") == [
        ("Read|Grep|Glob|Edit|MultiEdit|Write|Bash|WebFetch", expected)]
    assert _ours(data, "PostToolUseFailure") == [("WebFetch", expected)]
    assert _ours(data, "PreToolUse") == [("Bash|Read|Edit|MultiEdit|Write", expected)]
    assert _ours(data, "SessionStart") == [("startup|resume|clear|compact", expected)]
    assert _ours(data, "SessionEnd") == [("", expected)]


def test_install_updates_python_without_duplicates(tmp_path):
    inst.install(tmp_path, python="/old/python")
    path = inst.install(tmp_path, python="/new dir/python")
    data = json.loads(path.read_text(encoding="utf-8"))
    (m, h), = _ours(data, "PostToolUse")
    assert h["command"] == "'/new dir/python' -m ozagia.integrations.claude_code hook"


def test_install_project_settings(tmp_path):
    path = inst.install(tmp_path, python="py", settings="project")
    assert path == tmp_path / ".claude" / "settings.json"
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_install_keeps_handlers_sharing_a_group(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    shared = {"hooks": {"PreToolUse": [{"matcher": "Bash|Read", "hooks": [
        {"type": "command", "command": "py -m ozagia.integrations.claude_code hook"},
        {"type": "command", "command": "./guard.sh"}]}]}}
    path.write_text(json.dumps(shared), encoding="utf-8")
    inst.install(tmp_path, python="py")
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["PreToolUse"]
    assert groups[0] == {"matcher": "Bash|Read",
                         "hooks": [{"type": "command", "command": "./guard.sh"}]}
    assert len(_ours(data, "PreToolUse")) == 1


def test_uninstall_removes_only_ours(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(json.dumps(EXISTING), encoding="utf-8")
    inst.install(tmp_path, python="py")
    assert inst.uninstall(tmp_path) == [path]
    assert json.loads(path.read_text(encoding="utf-8")) == EXISTING
    assert inst.uninstall(tmp_path) == []


def test_uninstall_drops_empty_hooks_key(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"model": "x"}), encoding="utf-8")
    inst.install(tmp_path, python="py")
    inst.uninstall(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {"model": "x"}


def test_install_refuses_malformed_settings(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(inst.SettingsError):
        inst.install(tmp_path, python="py")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_cli_install_uninstall(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cmd = [sys.executable, "-m", "ozagia.integrations.claude_code"]
    r = subprocess.run(cmd + ["install", "--workdir", str(tmp_path), "--settings", "project"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    path = tmp_path / ".claude" / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert _ours(data, "PreToolUse")[0][1]["command"].startswith(sys.executable)
    r = subprocess.run(cmd + ["uninstall", "--workdir", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_hook_shares_the_intent_ref_moving_list():
    from ozagia.intent.shell import GIT_REF_MOVING
    assert hook.REF_MOVING_GIT is GIT_REF_MOVING
