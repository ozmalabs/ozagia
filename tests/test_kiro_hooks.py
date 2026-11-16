"""Kiro hooks: event and tool mapping, exit-2 blocks (git refusal, node-key
reads), plain-text stdout, the session brief, failures, and install/uninstall
of the hook file, the agent profile and mcp.json.

The hook runs as a subprocess, as Kiro runs it, against a stub daemon that
speaks the protocol over a Unix socket. Payload shapes follow what kiro-cli
2.37 sends (V3: PascalCase events, string `tool_response`; V1/V2: lowerCamel
events, CLI 2.x tool names); see docs/kiro.md.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from ozagia.daemon import autostart, protocol
from ozagia.integrations import hookcore
from ozagia.integrations.kiro import hook, install as inst

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PY = "/opt/py/bin/python"
CMD = f"{PY} -m ozagia.integrations.kiro hook"


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
    d = Path(tempfile.mkdtemp(prefix="ozkr", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
    _git(root, "init", "-q")
    return root


def _reply(req):
    if req["op"] == "after":
        return {"ok": True, "append": f"[ozagia] context for {req['tool']}", "meta": {}}
    if req["op"] == "tool":
        return {"ok": True, "handled": True, "output": f"def f(x): # {req['input']['path']}"}
    return {"ok": True}


@pytest.fixture
def daemon(sockdir):
    stubs = []

    def start(reply=_reply):
        stub = StubDaemon(sockdir / "d.sock", reply)
        stubs.append(stub)
        return stub
    yield start
    for s in stubs:
        s.close()


def _env(sock: Path | None, autostart_on: bool = False) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_") and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC)
    if sock is not None:
        env[protocol.ENV_SOCK] = str(sock)
    if not autostart_on:
        env["OZAGIA_DAEMON_AUTOSTART"] = "0"
    return env


def run_hook(payload, *, sock: Path | None, stdin: str | None = None, cwd=None,
             env: dict[str, str] | None = None):
    data = stdin if stdin is not None else json.dumps(payload)
    return subprocess.run([sys.executable, "-m", "ozagia.integrations.kiro", "hook"],
                          input=data, capture_output=True, text=True,
                          env=env or _env(sock), cwd=cwd,
                          timeout=hook.HOOK_TIMEOUT_S + 10)


def event(repo, name, **extra):
    return {"hook_event_name": name, "cwd": str(repo), "session_id": "kiro-sess-1",
            **extra}


def pre(repo, tool, tool_input, name="PreToolUse"):
    return event(repo, name, tool_name=tool, tool_input=tool_input)


def post(repo, tool, tool_input, tool_response, name="PostToolUse"):
    return event(repo, name, tool_name=tool, tool_input=tool_input,
                 tool_response=tool_response)


def _log_lines(repo):
    path = repo / ".ozagia" / hook.LOG_NAME
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# PreToolUse: git refusal
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool, name", [
    ("execute_bash", "PreToolUse"), ("execute_bash", "preToolUse"),
    ("shell", "preToolUse"), ("execute_cmd", "preToolUse"),
])
@pytest.mark.parametrize("command", [
    "git commit -m 'fix'", "cd sub && git push origin main", "git stash",
    "bash -c 'git reset --hard'",
])
def test_pre_shell_ref_moving_git_blocked_with_exit_2(repo, sockdir, tool, name, command):
    proc = run_hook(pre(repo, tool, {"command": command}, name),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "moves refs and is refused" in proc.stderr and "git" in proc.stderr


@pytest.mark.parametrize("command", ["git status", "git log --oneline -5", "npm test",
                                     "git branch", "echo 'git commit'"])
def test_pre_shell_other_commands_pass(repo, sockdir, command):
    proc = run_hook(pre(repo, "execute_bash", {"command": command}),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


# --------------------------------------------------------------------------- #
# PreToolUse: node-key reads
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool, tool_input, key, expect", [
    ("read_file", {"path": "sym:src/a.py::f"}, "sym:src/a.py::f", {}),
    ("read_file", {"path": "sym:src/a.py::f", "offset": 9, "limit": 5},
     "sym:src/a.py::f", {"offset": 10, "limit": 5}),
    ("read_file", {"path": "json:conf.json#/a/0"}, "json:conf.json#/a/0", {}),
    ("readFile", {"path": "toml:pyproject.toml#/project"}, "toml:pyproject.toml#/project", {}),
    ("fs_read", {"operations": [{"mode": "Line", "path": "test:tests/t.py::test_f"}]},
     "test:tests/t.py::test_f", {}),
    ("read_files", {"paths": ["doc:README.md#Install"]}, "doc:README.md#Install", {}),
])
def test_pre_read_node_key_answered_on_stderr(repo, daemon, tool, tool_input, key, expect):
    stub = daemon()
    proc = run_hook(pre(repo, tool, tool_input), sock=stub.sock)
    assert proc.returncode == 2 and proc.stdout == ""
    assert "ozagia answered this" in proc.stderr and "do not retry" in proc.stderr
    assert f"Node {key}:\ndef f(x): # {key}" in proc.stderr
    (req,) = stub.requests
    assert req["op"] == "tool" and req["tool"] == "read" and req["harness"] == "kiro"
    assert req["session"] == "kiro-sess-1"
    assert req["input"] == {"path": key, **expect}


def test_pre_read_key_made_absolute(repo, daemon):
    stub = daemon()
    proc = run_hook(pre(repo, "read_file", {"path": f"{repo}/yaml:k8s/app.yaml#/spec"}),
                    sock=stub.sock)
    assert proc.returncode == 2
    assert stub.requests[0]["input"] == {"path": "yaml:k8s/app.yaml#/spec"}


def test_pre_read_files_with_several_keys(repo, daemon):
    stub = daemon()
    proc = run_hook(pre(repo, "read_files", {"files": [
        "sym:src/a.py::f", {"path": "sym:src/a.py::g", "offset": 0}]}), sock=stub.sock)
    assert proc.returncode == 2
    assert [r["input"] for r in stub.requests] == [
        {"path": "sym:src/a.py::f"}, {"path": "sym:src/a.py::g", "offset": 1}]
    assert "Node sym:src/a.py::f:" in proc.stderr and "Node sym:src/a.py::g:" in proc.stderr


@pytest.mark.parametrize("tool, tool_input", [
    ("read_file", {"path": "src/a.py"}),
    ("read_files", {"paths": ["sym:src/a.py::f", "src/a.py"]}),
    ("fs_read", {"operations": [{"mode": "Image", "image_paths": ["sym:x"]}]}),
    ("grep_search", {"query": "sym:x"}),
])
def test_pre_read_of_real_paths_runs_as_usual(repo, daemon, tool, tool_input):
    stub = daemon()
    proc = run_hook(pre(repo, tool, tool_input), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert stub.requests == []


def test_pre_read_not_handled_runs_as_usual(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "handled": False, "reason": "unknown key"})
    proc = run_hook(pre(repo, "read_file", {"path": "sym:nope"}), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert len(stub.requests) == 1


def test_pre_read_error_is_labelled(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "handled": True, "is_error": True,
                               "output": "no node sym:src/a.py::zz; did you mean f?"})
    proc = run_hook(pre(repo, "read_file", {"path": "sym:src/a.py::zz"}), sock=stub.sock)
    assert proc.returncode == 2
    assert "ozagia read error for sym:src/a.py::zz:\nno node" in proc.stderr
    assert "could not answer this read_file" in proc.stderr
    assert "do not retry" not in proc.stderr


def test_pre_read_partial_error_still_says_answered(repo, daemon):
    def reply(req):
        bad = req["input"]["path"].endswith("zz")
        return {"ok": True, "handled": True, "is_error": bad, "output": "x"}
    stub = daemon(reply)
    proc = run_hook(pre(repo, "read_files", {"paths": ["sym:src/a.py::f", "sym:src/a.py::zz"]}),
                    sock=stub.sock)
    assert proc.returncode == 2 and "do not retry" in proc.stderr


def test_pre_read_real_file_with_key_like_name_uses_event_cwd(repo, daemon, tmp_path):
    # A real relative path with a `text:` segment; the hook process runs in
    # another directory than the event's cwd, so only the event cwd finds it.
    (repo / "notes").mkdir()
    (repo / "notes" / "text:todo.md").write_text("todo\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stub = daemon()
    proc = run_hook(pre(repo, "read_file", {"path": "notes/text:todo.md"}),
                    sock=stub.sock, cwd=elsewhere)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert stub.requests == []
    # The same path names a key when the file is not there.
    proc = run_hook(pre(repo, "read_file", {"path": "notes/text:gone.md"}),
                    sock=stub.sock, cwd=elsewhere)
    assert proc.returncode == 2
    assert stub.requests[0]["input"] == {"path": "text:gone.md"}


def test_pre_read_daemon_failure_fails_open(repo, daemon):
    stub = daemon(lambda req: {"ok": False, "error": "boom"})
    proc = run_hook(pre(repo, "read_file", {"path": "sym:src/a.py::f"}), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] == "PreToolUse" and "boom" in entry["error"]


# --------------------------------------------------------------------------- #
# PostToolUse
# --------------------------------------------------------------------------- #
POST_CASES = [
    # V3 built-ins
    ("execute_bash", {"command": "python -m pytest -q", "run_in_background": False},
     "shell", lambda r: {"command": "python -m pytest -q", "workdir": str(r)}),
    ("execute_bash", {"command": "ls", "cwd": "src", "timeout": 5000, "description": "list"},
     "shell", lambda r: {"command": "ls", "workdir": str(r / "src"), "timeout": 5000,
                         "description": "list"}),
    ("read_file", {"path": "src/a.py", "offset": 0, "limit": 20},
     "read", lambda r: {"path": "src/a.py", "offset": 1, "limit": 20}),
    ("read_files", {"paths": ["src/a.py"]}, "read", lambda r: {"path": "src/a.py"}),
    ("list_directory", {"path": "src", "depth": 2}, "read", lambda r: {"path": "src"}),
    ("str_replace", {"path": "src/a.py", "oldStr": "x", "newStr": "y", "replace_all": True},
     "edit", lambda r: {"path": "src/a.py", "oldString": "x", "newString": "y",
                        "replaceAll": True}),
    ("fs_write", {"path": "src/b.py", "text": "B = 1\n"},
     "write", lambda r: {"path": "src/b.py", "content": "B = 1\n"}),
    ("fs_append", {"path": "src/b.py", "text": "C = 2\n"}, "write", lambda r: {"path": "src/b.py"}),
    ("grep_search", {"query": "def f", "includePattern": "**/*.py", "caseSensitive": True},
     "grep", lambda r: {"pattern": "def f", "include": "**/*.py", "literal": False}),
    ("file_search", {"query": "a.py", "explanation": "find"},
     "glob", lambda r: {"pattern": "**/*a.py*"}),
    # CLI 2.x names
    ("shell", {"command": "ls"}, "shell", lambda r: {"command": "ls", "workdir": str(r)}),
    ("fs_read", {"operations": [{"mode": "Line", "path": "src/a.py", "offset": 4}]},
     "read", lambda r: {"path": "src/a.py", "offset": 5}),
    ("fs_write", {"command": "create", "path": "src/b.py", "content": "B\n"},
     "write", lambda r: {"path": "src/b.py", "content": "B\n"}),
    ("write", {"command": "strReplace", "path": "src/a.py", "oldStr": "x", "newStr": "z"},
     "edit", lambda r: {"path": "src/a.py", "oldString": "x", "newString": "z",
                        "replaceAll": False}),
    ("fs_write", {"command": "str_replace", "path": "src/a.py", "old_str": "x",
                  "new_str": "z"},
     "edit", lambda r: {"path": "src/a.py", "oldString": "x", "newString": "z",
                        "replaceAll": False}),
    ("fs_write", {"command": "insert", "path": "src/a.py", "insertLine": 1, "content": "#"},
     "write", lambda r: {"path": "src/a.py"}),
    ("grep", {"pattern": "def", "path": "src", "include": "*.py"},
     "grep", lambda r: {"pattern": "def", "path": "src", "include": "*.py", "literal": False}),
    ("glob", {"pattern": "**/*.py"}, "glob", lambda r: {"pattern": "**/*.py"}),
    ("web_fetch", {"url": "https://arxiv.org/abs/2401.12345", "mode": "truncated"},
     "webfetch", lambda r: {"url": "https://arxiv.org/abs/2401.12345"}),
]


@pytest.mark.parametrize("name", ["PostToolUse", "postToolUse"])
@pytest.mark.parametrize("tool, tool_input, neutral, expect", POST_CASES,
                         ids=[f"{c[0]}-{i}" for i, c in enumerate(POST_CASES)])
def test_post_maps_tool_and_prints_append(repo, daemon, name, tool, tool_input,
                                          neutral, expect):
    stub = daemon()
    proc = run_hook(post(repo, tool, tool_input, "result text", name), sock=stub.sock)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"[ozagia] context for {neutral}\n"
    (req,) = stub.requests
    assert req["op"] == "after" and req["harness"] == "kiro"
    assert req["session"] == "kiro-sess-1"
    assert req["tool"] == neutral and req["input"] == expect(repo)
    assert req["output"] == "result text"


@pytest.mark.parametrize("response, text", [
    ("plain", "plain"),
    ({"success": True, "result": ["line one", "line two"]}, "line one\nline two"),
    ({"result": "r"}, "r"),
    ({"stdout": "o", "stderr": "e"}, "o\ne"),
    (None, ""),
])
def test_post_flattens_the_response(repo, daemon, response, text):
    stub = daemon()
    run_hook(post(repo, "execute_bash", {"command": "ls"}, response), sock=stub.sock)
    assert stub.requests[0]["output"] == text


def test_post_empty_append_prints_nothing(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "append": "", "meta": {}})
    proc = run_hook(post(repo, "execute_bash", {"command": "true"}, ""), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert len(stub.requests) == 1


@pytest.mark.parametrize("tool, tool_input", [
    ("mcp_ozagia_dev_outline", {"target": "src/a.py"}),
    ("delete_file", {"targetFile": "src/a.py"}),
    ("read_files", {"paths": ["src/a.py", "src/b.py"]}),
    ("execute_bash", {"cmd": "no command field"}),
    ("fs_write", {"command": "rename", "path": "x"}),
    ("web_fetch", {"mode": "truncated"}),
])
def test_post_unmapped_sends_nothing(repo, daemon, tool, tool_input):
    stub = daemon()
    proc = run_hook(post(repo, tool, tool_input, "x"), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert stub.requests == []


def test_post_daemon_down_prints_nothing_and_logs(repo, sockdir):
    proc = run_hook(post(repo, "execute_bash", {"command": "ls"}, "x"),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] == "PostToolUse" and entry["tool"] == "execute_bash"
    assert "autostart is off" in entry["error"]


# --------------------------------------------------------------------------- #
# SessionStart / agentSpawn, compaction, other events
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["SessionStart", "agentSpawn"])
def test_session_start_brief_is_plain_text(repo, sockdir, name):
    (repo / "new.txt").write_text("n\n")
    proc = run_hook(event(repo, name), sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.startswith("[ozagia] session brief\n")
    assert "?? new.txt" in proc.stdout and "dev_outline" in proc.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(proc.stdout)


@pytest.mark.parametrize("name", ["PreCompact", "Stop", "UserPromptSubmit",
                                  "SomethingNew"])
def test_other_events_print_nothing(repo, daemon, name):
    stub = daemon()
    proc = run_hook(event(repo, name, summary="s"), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert stub.requests == []


def test_compaction_triggers_are_v3_only():
    # PostCompact goes in the V3 hook file only (2.37's V3 loader skips an
    # unknown trigger; the CLI 2.x validator rejects one); PreCompact nowhere.
    assert all({v3, cli}.isdisjoint({"PreCompact", "PostCompact", "postCompact"})
               for _, v3, cli, _ in inst.HOOKS)
    assert [t for _, t, _ in inst.V3_ONLY_HOOKS] == ["PostCompact"]
    assert "postCompact" not in json.dumps(inst.legacy_hooks(PY))
    assert all(h["trigger"] != "PostCompact" for h in inst.profile_hooks(PY))


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"])
def test_malformed_stdin_exits_zero(repo, sockdir, stdin):
    proc = run_hook(None, sock=sockdir / "missing.sock", stdin=stdin, cwd=repo)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] is None and entry["error"]


@pytest.fixture
def plain_dir(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(k)
    d = tmp_path / "downloads"
    d.mkdir()
    (d / "notes.py").write_text("x = 1\n")
    assert not hookcore.in_work_tree(d)
    yield d
    reply = autostart.ping(protocol.socket_path(d))
    if reply is not None:  # the guard failed; do not leave a daemon behind
        os.kill(reply["pid"], signal.SIGKILL)


@pytest.mark.parametrize("payload", [
    lambda d: event(d, "SessionStart"),
    lambda d: post(d, "execute_bash", {"command": "ls"}, "notes.py\n"),
    lambda d: post(d, "fs_write", {"path": "notes.py", "text": "x = 2\n"}, "ok"),
    lambda d: pre(d, "read_file", {"path": "sym:notes.py::x"}),
], ids=["SessionStart", "PostToolUse-shell", "PostToolUse-write", "PreToolUse-key"])
def test_non_git_cwd_starts_nothing(plain_dir, payload):
    env = _env(None, autostart_on=True)
    proc = run_hook(payload(plain_dir), sock=None, env=env, cwd=plain_dir)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert not (plain_dir / ".ozagia").exists()
    assert autostart.ping(protocol.socket_path(plain_dir)) is None


def test_alarm_leaves_margin_before_kiro_timeout():
    assert hook.HOOK_ALARM_S + hook.BRIEF_ALARM_S < hook.HOOK_TIMEOUT_S
    assert hookcore.AUTOSTART_BUDGET_S + hook.DAEMON_TIMEOUT_S < hook.HOOK_ALARM_S


def test_kiro_is_a_known_harness():
    assert "kiro" in protocol.HARNESSES and hook.HARNESS == "kiro"


# --------------------------------------------------------------------------- #
# hookcore: block and text answers, key prefixes
# --------------------------------------------------------------------------- #
def _run(handler, payload):
    out, err = io.StringIO(), io.StringIO()
    rc = hookcore.run_hook(handler, log_name="t.log", timeout_s=5,
                           stdin=io.StringIO(json.dumps(payload)), stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def test_run_hook_answers(repo):
    p = event(repo, "X")
    assert _run(lambda p, r: hookcore.HookBlock("no"), p) == (2, "", "no\n")
    assert _run(lambda p, r: hookcore.HookBlock("x\n", 3), p) == (3, "", "x\n")
    assert _run(lambda p, r: "text", p) == (0, "text\n", "")
    assert _run(lambda p, r: "", p) == (0, "", "")
    assert _run(lambda p, r: {"a": 1}, p) == (0, '{"a": 1}\n', "")
    assert _run(lambda p, r: None, p) == (0, "", "")


def test_run_hook_error_fails_open(repo):
    def boom(p, r):
        raise RuntimeError("x")
    assert _run(boom, event(repo, "X")) == (0, "", "")


class _BrokenStream(io.StringIO):
    def write(self, s):
        raise OSError("stderr closed")


def test_run_hook_error_after_block_fails_open(repo):
    # The handler blocks, but writing the block to stderr fails: no block.
    out = io.StringIO()
    rc = hookcore.run_hook(lambda p, r: hookcore.HookBlock("no"), log_name="t.log",
                           timeout_s=5, stdin=io.StringIO(json.dumps(event(repo, "X"))),
                           stdout=out, stderr=_BrokenStream())
    assert rc == 0 and out.getvalue() == ""


def test_node_key_prefixes():
    assert hookcore.node_key("json:a.json#/x") is None
    assert hookcore.node_key("json:a.json#/x", hook.KEY_PREFIXES) == "json:a.json#/x"
    assert hookcore.node_key("/nope/toml:p.toml#/a", hook.KEY_PREFIXES) == "toml:p.toml#/a"
    assert hookcore.node_key("/nope/sym:a.py::f") == "sym:a.py::f"
    assert hookcore.node_key("src/a.py", hook.KEY_PREFIXES) is None


def test_node_key_checks_relative_paths_under_cwd(tmp_path, monkeypatch):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "sym:x.py").write_text("")
    monkeypatch.chdir("/")
    assert hookcore.node_key("d/sym:x.py") == "sym:x.py"  # process cwd: no file
    assert hookcore.node_key("d/sym:x.py", cwd=str(tmp_path)) is None
    assert hookcore.node_key(str(tmp_path / "d" / "sym:x.py"), cwd="/") is None


# --------------------------------------------------------------------------- #
# install: hook file (V3)
# --------------------------------------------------------------------------- #
# The V3 engine's hook document schema in kiro-cli 2.37 (`pEe` in the KAS
# bundle) and its trigger names (with the CLI 2.x aliases).
V3_FIELDS = {"name", "description", "trigger", "matcher", "action", "timeout", "enabled",
             "confirm"}
V3_TRIGGERS = {"SessionStart", "SessionEnd", "Stop", "PreToolUse", "PostToolUse",
               "PreTaskExec", "PostTaskExec", "UserPromptSubmit", "PostFileCreate",
               "PostFileSave", "PostFileDelete", "Manual"}
LEGACY_TRIGGERS = {"agentSpawn", "userPromptSubmit", "preToolUse", "postToolUse", "stop"}


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _hook_file(root: Path) -> Path:
    return root / ".kiro" / "hooks" / "ozagia.json"


def _mcp(root: Path) -> Path:
    return root / ".kiro" / "settings" / "mcp.json"


def _profile(root: Path, name: str = "ozagia") -> Path:
    return root / ".kiro" / "agents" / f"{name}.json"


def test_install_v3_writes_hook_file_and_mcp(tmp_path):
    got = inst.install(tmp_path, python=PY)
    assert got == {"hooks": _hook_file(tmp_path), "mcp": _mcp(tmp_path)}
    assert not _profile(tmp_path).exists()
    data = _json(_hook_file(tmp_path))
    assert data["version"] == "v1"
    hooks = data["hooks"]
    assert [(h["trigger"], h.get("matcher")) for h in hooks] == [
        ("SessionStart", None), ("PreToolUse", "shell"), ("PreToolUse", "read"),
        ("PreToolUse", "write"), ("PostToolUse", "read"), ("PostToolUse", "write"), ("PostToolUse", "shell"),
        ("PostToolUse", "web_fetch"), ("PostCompact", None)]
    for h in hooks:
        assert set(h) <= V3_FIELDS and h["trigger"] in V3_TRIGGERS | {"PostCompact"}
        assert h["action"] == {"type": "command", "command": CMD}
        assert h["timeout"] == hook.HOOK_TIMEOUT_S
        assert h["name"].startswith("ozagia-")
    entry = _json(_mcp(tmp_path))["mcpServers"]["ozagia"]
    root = str(tmp_path.resolve())
    assert entry["command"] == PY
    assert entry["args"] == ["-m", "ozagia.integrations.mcp.server", "--root", root,
                             "--mode", "coding"]
    assert entry["env"] == {"OZAGIA_ROOT": root}


def test_install_routes_pre_write_in_every_form(tmp_path):
    # Ownership warnings come from PreToolUse on writes: the hook file, the
    # profile (array form) and the CLI 2.x object form all register it.
    inst.install(tmp_path, python=PY, fmt="both")
    for p in (_hook_file(tmp_path), _profile(tmp_path)):
        pre = [(h["name"], h.get("matcher")) for h in _json(p)["hooks"]
               if h["trigger"] in ("PreToolUse", "preToolUse")]
        assert ("ozagia-pre-write", "write") in pre
    assert {"matcher": "write", "command": CMD,
            "timeout_ms": hook.HOOK_TIMEOUT_S * 1000} in inst.legacy_hooks(PY)["preToolUse"]


def test_install_is_idempotent_and_updates_python(tmp_path):
    inst.install(tmp_path, python=PY, fmt="both")
    files = [_hook_file(tmp_path), _profile(tmp_path), _mcp(tmp_path)]
    first = [p.read_bytes() for p in files]
    inst.install(tmp_path, python=PY, fmt="both")
    assert [p.read_bytes() for p in files] == first
    inst.install(tmp_path, python="/new dir/python", fmt="both")
    for p, n in zip(files[:2], (len(inst.HOOKS) + len(inst.V3_ONLY_HOOKS),
                                len(inst.HOOKS))):
        hooks = _json(p)["hooks"]
        assert len(hooks) == n
        assert all(h["action"]["command"] ==
                   "'/new dir/python' -m ozagia.integrations.kiro hook" for h in hooks)
    assert _json(_mcp(tmp_path))["mcpServers"]["ozagia"]["command"] == "/new dir/python"


def test_install_keeps_other_hooks_and_servers(tmp_path):
    other_hook = {"name": "lint", "trigger": "PostFileSave", "matcher": "\\.py$",
                  "action": {"type": "command", "command": "ruff check"}}
    _hook_file(tmp_path).parent.mkdir(parents=True)
    _hook_file(tmp_path).write_text(json.dumps({"version": "v1", "hooks": [other_hook]}))
    mcp = {"mcpServers": {"docs": {"command": "npx", "args": ["-y", "docs-mcp"],
                                   "disabled": False}},
           "note": "team servers"}
    _mcp(tmp_path).parent.mkdir(parents=True)
    _mcp(tmp_path).write_text(json.dumps(mcp))
    inst.install(tmp_path, python=PY)
    data = _json(_hook_file(tmp_path))
    assert data["hooks"][0] == other_hook and len(data["hooks"]) == (
        1 + len(inst.HOOKS) + len(inst.V3_ONLY_HOOKS))
    merged = _json(_mcp(tmp_path))
    assert merged["note"] == "team servers"
    assert list(merged["mcpServers"]) == ["docs", "ozagia"]
    assert merged["mcpServers"]["docs"] == mcp["mcpServers"]["docs"]
    assert sorted(inst.uninstall(tmp_path)) == sorted([_hook_file(tmp_path), _mcp(tmp_path)])
    assert _json(_hook_file(tmp_path)) == {"version": "v1", "hooks": [other_hook]}
    assert _json(_mcp(tmp_path)) == mcp
    assert inst.uninstall(tmp_path) == []


def test_fresh_install_then_uninstall_leaves_no_files(tmp_path):
    got = inst.install(tmp_path, python=PY, fmt="both")
    assert set(got) == {"hooks", "agent", "mcp"}
    assert sorted(inst.uninstall(tmp_path)) == sorted(got.values())
    assert not any(p.exists() for p in got.values())


@pytest.mark.parametrize("where, text, msg", [
    ("mcp", "{not json", "not valid JSON"),
    ("mcp", "[1]", "not a JSON object"),
    ("mcp", '{"mcpServers": []}', "`mcpServers` is not an object"),
    ("hooks", '{"version": "v1", "hooks": {}}', "`hooks` is not a list"),
    ("hooks", '{"version": "v2", "hooks": []}', "is not \"v1\""),
    ("agent", '{"name": "ozagia", "hooks": 3}', "neither a list nor an object"),
    ("agent", '{"name": "ozagia", "hooks": {"preToolUse": {}}}', "is not a list"),
])
def test_install_refuses_malformed_files(tmp_path, where, text, msg):
    path = {"mcp": _mcp, "hooks": _hook_file, "agent": _profile}[where](tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(inst.SettingsError, match=msg):
        inst.install(tmp_path, python=PY, fmt="both")
    assert path.read_text(encoding="utf-8") == text
    others = [p for p in (_mcp(tmp_path), _hook_file(tmp_path), _profile(tmp_path))
              if p != path]
    assert not any(p.exists() for p in others)


# --------------------------------------------------------------------------- #
# install: agent profile (CLI 2.x engines)
# --------------------------------------------------------------------------- #
def test_install_agent_creates_universal_profile(tmp_path):
    got = inst.install(tmp_path, python=PY, fmt="agent")
    assert got == {"agent": _profile(tmp_path), "mcp": _mcp(tmp_path)}
    assert not _hook_file(tmp_path).exists()
    data = _json(_profile(tmp_path))
    assert data["name"] == "ozagia" and data["tools"] == ["*"]
    assert data["includeMcpJson"] is True
    assert [(h["trigger"], h.get("matcher")) for h in data["hooks"]] == [
        ("agentSpawn", None), ("preToolUse", "shell"), ("preToolUse", "read"),
        ("preToolUse", "write"), ("postToolUse", "read"), ("postToolUse", "write"), ("postToolUse", "shell"),
        ("postToolUse", "web_fetch")]
    for h in data["hooks"]:
        assert h["trigger"] in LEGACY_TRIGGERS
        assert h["action"] == {"type": "command", "command": CMD}
        assert h["timeout"] == hook.HOOK_TIMEOUT_S


def test_install_agent_into_object_form_profile_keeps_its_form(tmp_path):
    existing = {"name": "dev", "prompt": "be brief", "tools": ["read", "shell"],
                "hooks": {"agentSpawn": [{"command": "git status"}],
                          "stop": [{"command": "make lint"}]}}
    path = _profile(tmp_path, "dev")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(existing))
    inst.install(tmp_path, python=PY, fmt="agent", agent="dev")
    data = _json(path)
    assert data["prompt"] == "be brief" and data["tools"] == ["read", "shell"]
    hooks = data["hooks"]
    assert hooks["stop"] == existing["hooks"]["stop"]
    assert hooks["agentSpawn"][0] == {"command": "git status"}
    assert hooks["agentSpawn"][1] == {"command": CMD, "timeout_ms": 20000}
    assert [e["matcher"] for e in hooks["preToolUse"]] == ["shell", "read", "write"]
    assert [e["matcher"] for e in hooks["postToolUse"]] == ["read", "write", "shell",
                                                           "web_fetch"]
    inst.install(tmp_path, python=PY, fmt="agent", agent="dev")
    assert _json(path) == data
    assert inst.uninstall(tmp_path, agent="dev") == [path, _mcp(tmp_path)]
    assert _json(path) == existing


def test_install_agent_into_array_form_profile(tmp_path):
    existing = {"name": "dev", "hooks": [
        {"name": "status", "trigger": "SessionStart",
         "action": {"type": "command", "command": "git status"}}]}
    path = _profile(tmp_path, "dev")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(existing))
    inst.install(tmp_path, python=PY, fmt="agent", agent="dev")
    hooks = _json(path)["hooks"]
    assert hooks[0] == existing["hooks"][0] and len(hooks) == 1 + len(inst.HOOKS)
    inst.uninstall(tmp_path, agent="dev")
    assert _json(path) == existing


def test_uninstall_keeps_a_profile_kiro_changed(tmp_path):
    inst.install(tmp_path, python=PY, fmt="agent")
    path = _profile(tmp_path)
    data = _json(path)
    data["kiroUpgrade"] = {"revision": 2}
    path.write_text(json.dumps(data))
    inst.uninstall(tmp_path)
    left = _json(path)
    assert "hooks" not in left and left["kiroUpgrade"] == {"revision": 2}


def test_user_scope(tmp_path):
    home = tmp_path / "kiro_home"
    work = tmp_path / "proj"
    work.mkdir()
    got = inst.install(work, scope="user", python=PY, fmt="both", home=home)
    assert got == {"hooks": home / "hooks" / "ozagia.json",
                   "agent": home / "agents" / "ozagia.json",
                   "mcp": home / "settings" / "mcp.json"}
    entry = _json(got["mcp"])["mcpServers"]["ozagia"]
    assert "--root" not in entry["args"] and "OZAGIA_ROOT" not in entry["env"]
    assert not (work / ".kiro").exists()
    inst.uninstall(work, scope="user", home=home)
    assert not any(p.exists() for p in got.values())


@pytest.mark.parametrize("agent", ["", "../x", ".hidden", "a/b"])
def test_bad_agent_names_refused(tmp_path, agent):
    with pytest.raises(ValueError, match="not an agent name"):
        inst.install(tmp_path, python=PY, fmt="agent", agent=agent)


def test_install_writes_through_symlinks(tmp_path):
    dot = tmp_path / "dotfiles" / "mcp.json"
    dot.parent.mkdir()
    dot.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    _mcp(tmp_path).parent.mkdir(parents=True)
    _mcp(tmp_path).symlink_to(dot)
    inst.install(tmp_path, python=PY)
    assert _mcp(tmp_path).is_symlink()
    assert set(_json(dot)["mcpServers"]) == {"other", "ozagia"}
    inst.uninstall(tmp_path)
    assert _mcp(tmp_path).is_symlink() and set(_json(dot)["mcpServers"]) == {"other"}


def test_uninstall_keeps_an_emptied_symlink(tmp_path):
    dot = tmp_path / "dotfiles" / "mcp.json"
    dot.parent.mkdir()
    dot.write_text("{}")
    _mcp(tmp_path).parent.mkdir(parents=True)
    _mcp(tmp_path).symlink_to(dot)
    inst.install(tmp_path, python=PY)
    inst.uninstall(tmp_path)
    assert _mcp(tmp_path).is_symlink() and _json(dot) == {"mcpServers": {}}


def test_reinstall_keeps_user_keys_on_the_mcp_entry(tmp_path):
    inst.install(tmp_path, python=PY)
    data = _json(_mcp(tmp_path))
    data["mcpServers"]["ozagia"].update(autoApprove=["read"], disabled=True)
    _mcp(tmp_path).write_text(json.dumps(data))
    inst.install(tmp_path, python="/other/python")
    entry = _json(_mcp(tmp_path))["mcpServers"]["ozagia"]
    assert entry["autoApprove"] == ["read"] and entry["disabled"] is True
    assert entry["command"] == "/other/python"


def test_install_rolls_back_on_a_failed_write(tmp_path, monkeypatch):
    inst.install(tmp_path, python=PY, fmt="agent")
    before = _profile(tmp_path).read_text()
    real = inst._atomic_write

    def failing(path, text):
        if path.name == "mcp.json":
            raise PermissionError("settings is read-only")
        real(path, text)
    monkeypatch.setattr(inst, "_atomic_write", failing)
    with pytest.raises(PermissionError):
        inst.install(tmp_path, python="/other/python", fmt="both")
    monkeypatch.setattr(inst, "_atomic_write", real)
    assert not _hook_file(tmp_path).exists()  # created, then removed again
    assert _profile(tmp_path).read_text() == before  # changed, then restored
    assert _json(_mcp(tmp_path))["mcpServers"]["ozagia"]["command"] == PY


def test_uninstall_without_agent_scans_every_profile(tmp_path):
    inst.install(tmp_path, python=PY, fmt="agent", agent="dev")
    other = _profile(tmp_path, "mine")
    other.write_text(json.dumps({"name": "mine", "hooks": [{"command": "true"}]}))
    _profile(tmp_path, "broken").write_text("{bad")
    assert sorted(inst.uninstall(tmp_path)) == sorted([_profile(tmp_path, "dev"),
                                                       _mcp(tmp_path)])
    assert not _profile(tmp_path, "dev").exists()
    assert _json(other)["hooks"] == [{"command": "true"}]


def test_uninstall_with_agent_looks_only_there(tmp_path):
    inst.install(tmp_path, python=PY, fmt="agent", agent="dev")
    inst.install(tmp_path, python=PY, fmt="agent", agent="ops")
    inst.uninstall(tmp_path, agent="dev")
    assert not _profile(tmp_path, "dev").exists() and _profile(tmp_path, "ops").exists()


# --------------------------------------------------------------------------- #
# probe hook
# --------------------------------------------------------------------------- #
PROBE = REPO / "scripts" / "kiro_probe_hook.py"


def test_probe_hook_never_exits_non_zero(tmp_path):
    work = tmp_path / "probe"
    work.mkdir()
    r = subprocess.run([sys.executable, str(PROBE), "install", "--workdir", str(work)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    hooks = _json(work / ".kiro" / "hooks" / "ozagia-probe.json")["hooks"]
    profile = _json(work / ".kiro" / "agents" / "ozagia-probe.json")["hooks"]
    commands = {h["action"]["command"] for h in hooks + profile}
    (command,) = commands
    copy = work / ".ozagia" / "kiro_probe_hook.py"
    assert copy.exists() and str(copy) in command and str(PROBE) not in command
    assert command.endswith(" || true")
    records = tmp_path / "records"
    env = {**os.environ, "OZAGIA_KIRO_PROBE_DIR": str(records)}
    ev = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(work),
                     "tool_name": "read_file"})
    r = subprocess.run(["sh", "-c", command], input=ev, capture_output=True,
                       text=True, env=env, timeout=60, cwd=work)
    assert r.returncode == 0 and r.stdout == ""
    assert len(list(records.glob("*.json"))) == 1
    copy.unlink()  # the script is gone: python exits 2, the hook still 0
    r = subprocess.run(["sh", "-c", command], input=ev, capture_output=True,
                       text=True, env=env, timeout=60, cwd=work)
    assert r.returncode == 0
    copy.write_text("")
    r = subprocess.run([sys.executable, str(PROBE), "uninstall", "--workdir", str(work)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and not copy.exists()
    assert not (work / ".kiro" / "hooks" / "ozagia-probe.json").exists()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_install_uninstall(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cmd = [sys.executable, "-m", "ozagia.integrations.kiro"]
    r = subprocess.run(cmd + ["install", "--workdir", str(tmp_path), "--format", "both"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "Trust this workspace" in r.stdout and "--agent ozagia" in r.stdout
    assert _json(_hook_file(tmp_path))["hooks"][0]["action"]["command"].startswith(
        sys.executable)
    r = subprocess.run(cmd + ["uninstall", "--workdir", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not _hook_file(tmp_path).exists() and not _profile(tmp_path).exists()
    _mcp(tmp_path).write_text("{bad")
    r = subprocess.run(cmd + ["install", "--workdir", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 1 and "not valid JSON" in r.stderr


def test_ozagia_install_kiro(tmp_path, capsys):
    from ozagia import cli
    assert cli.main(["install", "kiro", "--workdir", str(tmp_path),
                     "--python", PY, "--format", "agent"]) == 0
    assert "installed the ozagia MCP server" in capsys.readouterr().out
    assert _json(_profile(tmp_path))["hooks"][0]["action"]["command"] == CMD


# --------------------------------------------------------------------------- #
# kiro-cli's own validator (skipped without kiro-cli)
# --------------------------------------------------------------------------- #
def _validate(path: Path) -> str:
    """`kiro-cli agent validate` on `path`, run in the file's tmp directory
    so it does not read a real workspace's `.kiro/`. It still uses the real
    HOME, because it needs the user's kiro-cli login."""
    r = subprocess.run(["kiro-cli", "agent", "validate", "--path", str(path)],
                       capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL, cwd=path.parent)
    text = r.stdout + r.stderr
    if "not logged in" in text:
        pytest.skip("kiro-cli is not logged in")
    # 2.37 exits 0 on an invalid profile and prints `Error: ...` on stderr.
    return text if r.returncode == 0 else f"exit {r.returncode}: {text}"


@pytest.mark.skipif(shutil.which("kiro-cli") is None, reason="kiro-cli not installed")
def test_kiro_cli_validates_generated_profiles(tmp_path):
    # The check works: a profile with an unknown object-form trigger fails.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "bad", "hooks": {"nope": [{"command": "x"}]}}))
    assert "Error" in _validate(bad)
    inst.install(tmp_path, python=PY, fmt="agent")
    assert _validate(_profile(tmp_path)) == ""
    obj = _profile(tmp_path, "dev")
    obj.write_text(json.dumps({"name": "dev", "hooks": {"stop": [{"command": "true"}]}}))
    inst.install(tmp_path, python=PY, fmt="agent", agent="dev")
    assert _validate(obj) == ""
