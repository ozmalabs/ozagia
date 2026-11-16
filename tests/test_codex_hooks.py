"""Codex hooks: payload mapping, local git refusal, SessionStart brief,
daemon autostart, and install/uninstall of hooks.json, config.toml and
AGENTS.md.

The hook runs as a subprocess, as Codex runs it, against a stub daemon that
speaks the protocol over a Unix socket (and, for autostart, against a real
daemon on a temp repository). Payload shapes follow the Codex hooks
reference (see docs/codex.md).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

import pytest

from ozagia.daemon import autostart, protocol
from ozagia.daemon.client import DaemonClient
from ozagia.integrations.codex import hook, install as inst

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
    d = Path(tempfile.mkdtemp(prefix="ozcx", dir="/tmp"))
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


def _after_reply(req):
    if req["op"] == "after":
        return {"ok": True, "append": f"[ozagia] context for {req['tool']}", "meta": {}}
    return {"ok": True}


@pytest.fixture
def daemon(sockdir):
    stubs = []

    def start(reply=_after_reply):
        stub = StubDaemon(sockdir / "d.sock", reply)
        stubs.append(stub)
        return stub
    yield start
    for s in stubs:
        s.close()


def _env(sock: Path | None, autostart_on: bool = False, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_") and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC)
    if sock is not None:
        env[protocol.ENV_SOCK] = str(sock)
    if not autostart_on:
        env["OZAGIA_DAEMON_AUTOSTART"] = "0"
    env.update(extra)
    return env


def run_hook(payload, *, sock: Path | None, stdin: str | None = None, cwd=None,
             env: dict[str, str] | None = None):
    data = stdin if stdin is not None else json.dumps(payload)
    return subprocess.run([sys.executable, "-m", "ozagia.integrations.codex", "hook"],
                          input=data, capture_output=True, text=True,
                          env=env or _env(sock), cwd=cwd,
                          timeout=hook.HOOK_TIMEOUT_S + 10)


def base(repo, event, **extra):
    return {"session_id": "019a-sess", "transcript_path": "/home/u/.codex/sessions/x.jsonl",
            "cwd": str(repo), "hook_event_name": event, "model": "gpt-5.5",
            "permission_mode": "default", **extra}


def tool_event(repo, event, tool, tool_input, **extra):
    return base(repo, event, turn_id="turn-1", tool_name=tool, tool_use_id="call_1",
                tool_input=tool_input, **extra)


def post(repo, tool, tool_input, tool_response):
    return tool_event(repo, "PostToolUse", tool, tool_input, tool_response=tool_response)


def context_of(proc, event="PostToolUse"):
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert set(out) == {"hookSpecificOutput"}
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == event
    assert set(spec) == {"hookEventName", "additionalContext"}
    return spec["additionalContext"]


PATCH = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n-    return x\n"
         "+    return x + 1\n*** End Patch\n")


# --------------------------------------------------------------------------- #
# PostToolUse
# --------------------------------------------------------------------------- #
def test_post_bash_maps_to_shell(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "append": "FAILED test:tests/t.py::test_f",
                               "meta": {}})
    proc = run_hook(post(repo, "Bash", {"command": "python -m pytest -q"},
                         "1 failed in 0.1s"), sock=stub.sock)
    assert context_of(proc) == "FAILED test:tests/t.py::test_f"
    (req,) = stub.requests
    assert req["op"] == "after" and req["harness"] == "codex"
    assert req["session"] == "019a-sess" and req["tool"] == "shell"
    assert req["input"] == {"command": "python -m pytest -q", "workdir": str(repo)}
    assert req["output"] == "1 failed in 0.1s"


@pytest.mark.parametrize("response, text", [
    ({"output": "out text", "exit_code": 1}, "out text"),
    ({"stdout": "o", "stderr": "e"}, "o\ne"),
    ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
    (None, ""),
])
def test_post_bash_flattens_the_response(repo, daemon, response, text):
    stub = daemon()
    proc = run_hook(post(repo, "Bash", {"command": "ls"}, response), sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for shell"
    assert stub.requests[0]["output"] == text


def test_post_apply_patch_maps_to_patch(repo, daemon):
    stub = daemon()
    proc = run_hook(post(repo, "apply_patch", {"command": PATCH},
                         "Success. Updated the following files:\nM src/a.py\n"),
                    sock=stub.sock)
    assert context_of(proc) == "[ozagia] context for patch"
    (req,) = stub.requests
    assert req["tool"] == "patch" and req["input"] == {"patch": PATCH}
    assert req["output"].startswith("Success.")


def test_post_empty_append_prints_nothing(repo, daemon):
    stub = daemon(lambda req: {"ok": True, "append": "", "meta": {}})
    proc = run_hook(post(repo, "Bash", {"command": "true"}, ""), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert len(stub.requests) == 1


@pytest.mark.parametrize("tool, tool_input", [
    ("mcp__ozagia__dev_outline", {"target": "src/a.py"}),
    ("Bash", {"cmd": "no command field"}),
    ("apply_patch", {}),
])
def test_post_unmapped_sends_nothing(repo, daemon, tool, tool_input):
    stub = daemon()
    proc = run_hook(post(repo, tool, tool_input, "x"), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    assert stub.requests == []


# --------------------------------------------------------------------------- #
# PreToolUse
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "git commit -m 'fix'", "cd sub && git push origin main", "git stash",
    "bash -c 'git reset --hard'", "git checkout -b new",
])
def test_pre_bash_ref_moving_git_denied(repo, command, sockdir):
    proc = run_hook(tool_event(repo, "PreToolUse", "Bash", {"command": command}),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # Only fields Codex supports on PreToolUse: no ask, continue, stopReason.
    assert out == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": out["hookSpecificOutput"]["permissionDecisionReason"]}}
    assert "git" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", ["git status", "git log --oneline -5", "npm test",
                                     "git branch", "echo 'git commit'"])
def test_pre_bash_other_commands_allowed(repo, command, sockdir):
    proc = run_hook(tool_event(repo, "PreToolUse", "Bash", {"command": command}),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == ""


def test_pre_apply_patch_passes(repo, sockdir):
    proc = run_hook(tool_event(repo, "PreToolUse", "apply_patch", {"command": PATCH}),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == ""


# --------------------------------------------------------------------------- #
# SessionStart
# --------------------------------------------------------------------------- #
def test_session_start_brief(repo, sockdir):
    _git(repo, "add", "-A")
    for i in range(4):
        (repo / "src" / "a.py").write_text(f"X = {i}\n")
        _git(repo, "commit", "-qam", f"commit number {i}")
    (repo / "src" / "a.py").write_text("X = 9\n")
    (repo / "new.txt").write_text("n\n")
    proc = run_hook(base(repo, "SessionStart", source="startup"),
                    sock=sockdir / "missing.sock")
    text = context_of(proc, "SessionStart")
    assert len(text) <= hook.BRIEF_MAX_CHARS
    assert "2 changed file(s)" in text
    assert " M src/a.py" in text and "?? new.txt" in text
    assert "commit number 3" in text and "commit number 1" in text
    assert "commit number 0" not in text
    assert "dev_outline" in text and "dev_impact" in text


def test_session_brief_caps_files_and_length(repo):
    for i in range(40):
        (repo / f"file_{i:02d}_{'x' * 60}.txt").write_text("x")
    text = hook.session_brief(repo)
    assert len(text) <= hook.BRIEF_MAX_CHARS
    assert text.endswith(hook.MCP_TOOLS_LINE)
    short = hook.session_brief(repo, max_chars=400)
    assert len(short) <= 400 and short.endswith(hook.MCP_TOOLS_LINE)


def test_session_brief_outside_git(tmp_path):
    text = hook.session_brief(tmp_path)
    assert text.splitlines() == ["[ozagia] session brief", hook.MCP_TOOLS_LINE]


# --------------------------------------------------------------------------- #
# failures never block
# --------------------------------------------------------------------------- #
def _log_lines(repo):
    path = repo / ".ozagia" / hook.LOG_NAME
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_daemon_down_autostart_off_prints_nothing_and_logs(repo, sockdir):
    proc = run_hook(post(repo, "Bash", {"command": "ls"}, "x"),
                    sock=sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] == "PostToolUse" and entry["tool"] == "Bash"
    assert "autostart is off" in entry["error"]


def test_daemon_error_prints_nothing_and_logs(repo, daemon):
    stub = daemon(lambda req: {"ok": False, "error": "boom"})
    proc = run_hook(post(repo, "Bash", {"command": "ls"}, "x"), sock=stub.sock)
    assert proc.returncode == 0 and proc.stdout == ""
    (entry,) = _log_lines(repo)
    assert "boom" in entry["error"]


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]"])
def test_malformed_stdin_exits_zero(repo, stdin, sockdir):
    proc = run_hook(None, sock=sockdir / "missing.sock", stdin=stdin, cwd=repo)
    assert proc.returncode == 0 and proc.stdout == ""
    (entry,) = _log_lines(repo)
    assert entry["event"] is None and entry["error"]


# --------------------------------------------------------------------------- #
# autostart against a real daemon
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_repo(repo, monkeypatch):
    for k in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(k)
    yield repo
    sock = protocol.socket_path(repo)
    reply = autostart.ping(sock)
    if reply is None:
        return
    try:
        with DaemonClient(sock, timeout=10) as c:
            c.shutdown()
    except Exception:
        os.kill(reply["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(reply["pid"], 0)
        except OSError:
            return
        time.sleep(0.05)


def _wait_for_daemon(root: Path, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = autostart.ping(protocol.socket_path(root))
        if reply is not None:
            return reply
        time.sleep(0.1)
    raise AssertionError("no daemon came up")


def test_hooks_start_one_daemon_and_use_it(live_repo):
    env = _env(None, autostart_on=True, OZAGIA_DAEMON_IDLE_TIMEOUT="600")
    payload = post(live_repo, "Bash", {"command": "ls src"}, "a.py\n")
    procs = [subprocess.Popen([sys.executable, "-m", "ozagia.integrations.codex", "hook"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env)
             for _ in range(3)]
    for p in procs:
        p.communicate(json.dumps(payload), timeout=hook.HOOK_TIMEOUT_S + 10)
        assert p.returncode == 0
    _wait_for_daemon(live_repo)
    log = (live_repo / ".ozagia" / autostart.LOG_NAME).read_text()
    assert log.count("ready ") == 1, log
    # The next call goes straight to the running daemon.
    proc = run_hook(post(live_repo, "apply_patch", {"command": PATCH}, "Success."),
                    sock=None, env=env)
    assert proc.returncode == 0 and proc.stderr == ""
    with DaemonClient(protocol.socket_path(live_repo), timeout=10) as c:
        stats = c.stats()["stats"]
    assert stats["idle_timeout_s"] == 600
    assert stats["by_harness"].get("codex", 0) >= 1
    assert stats["by_tool"].get("patch") == 1


def test_session_start_warms_the_daemon(live_repo):
    env = _env(None, autostart_on=True)
    proc = run_hook(base(live_repo, "SessionStart", source="startup"), sock=None, env=env)
    assert "[ozagia] session brief" in context_of(proc, "SessionStart")
    assert _wait_for_daemon(live_repo)["root"] == str(live_repo.resolve())


# --------------------------------------------------------------------------- #
# install / uninstall: hooks.json
# --------------------------------------------------------------------------- #
EXISTING_HOOKS = {
    "description": "team hooks",
    "hooks": {
        "PreToolUse": [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": "./guard.sh", "timeout": 5}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
    },
}


def _ours(data, event):
    return [(g.get("matcher"), h) for g in data["hooks"].get(event, [])
            for h in g["hooks"] if inst.is_ozagia_handler(h)]


def _hooks(tmp_path):
    return json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))


def test_install_merges_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps(EXISTING_HOOKS), encoding="utf-8")
    got = inst.install(tmp_path, python="/opt/py/bin/python")
    assert got["hooks"] == path
    first = path.read_text(encoding="utf-8")
    inst.install(tmp_path, python="/opt/py/bin/python")
    assert path.read_text(encoding="utf-8") == first
    data = _hooks(tmp_path)
    assert data["description"] == "team hooks"
    assert data["hooks"]["Stop"] == EXISTING_HOOKS["hooks"]["Stop"]
    assert data["hooks"]["PreToolUse"][0] == EXISTING_HOOKS["hooks"]["PreToolUse"][0]
    cmd = "/opt/py/bin/python -m ozagia.integrations.codex hook"
    expected = {"type": "command", "command": cmd, "timeout": hook.HOOK_TIMEOUT_S}
    assert _ours(data, "PreToolUse") == [("^(Bash|apply_patch)$", expected)]
    assert _ours(data, "PostToolUse") == [("^(Bash|apply_patch)$", expected)]
    assert _ours(data, "SessionStart") == [(None, expected)]


def test_install_updates_python_without_duplicates(tmp_path):
    inst.install(tmp_path, python="/old/python")
    inst.install(tmp_path, python="/new dir/python")
    data = _hooks(tmp_path)
    (_, h), = _ours(data, "PostToolUse")
    assert h["command"] == "'/new dir/python' -m ozagia.integrations.codex hook"
    config = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text())
    assert config["mcp_servers"]["ozagia"]["command"] == "/new dir/python"


def test_uninstall_restores_hooks(tmp_path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps(EXISTING_HOOKS), encoding="utf-8")
    inst.install(tmp_path, python="py")
    assert path in inst.uninstall(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == EXISTING_HOOKS
    assert inst.uninstall(tmp_path) == []


@pytest.mark.parametrize("text, msg", [
    ("{not json", "not valid JSON"), ("[1]", "not a JSON object"),
    ('{"hooks": []}', "`hooks` is not an object"),
    ('{"hooks": {"PreToolUse": {}}}', "is not a list"),
])
def test_install_refuses_malformed_hooks(tmp_path, text, msg):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    with pytest.raises(inst.SettingsError, match=msg):
        inst.install(tmp_path, python="py")
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / "AGENTS.md").exists()


# --------------------------------------------------------------------------- #
# install / uninstall: config.toml
# --------------------------------------------------------------------------- #
CONFIG_HEAD = '''# my codex settings
model = "gpt-5.5"   # pinned
approval_policy = "on-request"

[mcp_servers.docs]
command = "npx"
args = ["-y", "docs-mcp"]
'''
CONFIG_TAIL = '''# the profile I use at work
[profiles.work]
model = "o3"
'''


def _config(tmp_path):
    return tmp_path / ".codex" / "config.toml"


def test_install_appends_mcp_table_keeping_every_byte(tmp_path):
    path = _config(tmp_path)
    path.parent.mkdir()
    original = CONFIG_HEAD + "\n" + CONFIG_TAIL
    path.write_text(original, encoding="utf-8")
    inst.install(tmp_path, python="/opt/py/bin/python")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original)
    data = tomllib.loads(text)
    entry = data["mcp_servers"]["ozagia"]
    root = str(tmp_path.resolve())
    assert entry["command"] == "/opt/py/bin/python"
    assert entry["args"] == ["-m", "ozagia.integrations.mcp.server", "--root", root,
                             "--mode", "coding"]
    assert entry["env"] == {"OZAGIA_ROOT": root}
    assert data["mcp_servers"]["docs"]["command"] == "npx"
    assert data["profiles"]["work"]["model"] == "o3"
    inst.install(tmp_path, python="/opt/py/bin/python")
    assert path.read_text(encoding="utf-8") == text
    inst.uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == original


def test_install_replaces_an_old_table_in_place(tmp_path):
    path = _config(tmp_path)
    path.parent.mkdir()
    old = ('[mcp_servers.ozagia]\ncommand = "/old/python"\nargs = ["-m", "x"]\n'
           'startup_timeout_sec = 30\n\n[mcp_servers.ozagia.env]\nFOO = "1"\n\n')
    path.write_text(CONFIG_HEAD + "\n" + old + CONFIG_TAIL, encoding="utf-8")
    inst.install(tmp_path, python="/opt/py/bin/python")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(CONFIG_HEAD + "\n[mcp_servers.ozagia]\n")
    assert text.endswith("\n\n" + CONFIG_TAIL)
    assert "/old/python" not in text and "FOO" not in text
    assert text.count("[mcp_servers.ozagia]") == 1
    entry = tomllib.loads(text)["mcp_servers"]["ozagia"]
    assert entry["command"] == "/opt/py/bin/python" and "startup_timeout_sec" not in entry
    inst.uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == CONFIG_HEAD + "\n\n" + CONFIG_TAIL


@pytest.mark.parametrize("text", [
    '[mcp_servers]\nozagia = { command = "x" }\n',
    'mcp_servers.ozagia.command = "x"\n',
])
def test_install_refuses_ozagia_in_other_forms(tmp_path, text):
    path = _config(tmp_path)
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    with pytest.raises(inst.SettingsError, match="does not edit"):
        inst.install(tmp_path, python="py")
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_install_refuses_malformed_toml(tmp_path):
    path = _config(tmp_path)
    path.parent.mkdir()
    path.write_text("model = \n", encoding="utf-8")
    with pytest.raises(inst.SettingsError, match="not valid TOML"):
        inst.install(tmp_path, python="py")
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_toml_strings_are_escaped(tmp_path):
    entry = {"command": 'C:\\py "x"\n', "args": ["-m", "é"], "env": {"A": "b\\c"}}
    assert tomllib.loads(inst.mcp_table(entry))["mcp_servers"]["ozagia"] == entry


def test_header_parsing():
    assert inst._header_parts('[mcp_servers.ozagia]') == ["mcp_servers", "ozagia"]
    assert inst._header_parts(' [ mcp_servers . "ozagia" . env ] # c') == [
        "mcp_servers", "ozagia", "env"]
    assert inst._header_parts('["a", "b"],') is None
    assert inst._header_parts('x = 1') is None


# --------------------------------------------------------------------------- #
# install / uninstall: AGENTS.md
# --------------------------------------------------------------------------- #
def test_agents_section_added_replaced_and_removed(tmp_path):
    path = tmp_path / "AGENTS.md"
    original = "# Project\n\nRun `make test`.\n"
    path.write_text(original, encoding="utf-8")
    inst.install(tmp_path, python="py")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original + "\n" + inst.BEGIN)
    assert text.endswith(inst.END + "\n")
    assert "dev_outline" in text and "dev_impact" in text
    # An older section is replaced, not duplicated.
    path.write_text(text.replace("Start a task", "OLD"), encoding="utf-8")
    inst.install(tmp_path, python="py")
    assert path.read_text(encoding="utf-8") == text
    inst.uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == original


def test_agents_section_in_the_middle_is_replaced_in_place(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text(f"top\n{inst.BEGIN}\nold\n{inst.END}\nbottom\n", encoding="utf-8")
    inst.install(tmp_path, python="py")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("top\n" + inst.BEGIN) and text.endswith(inst.END + "\nbottom\n")
    inst.uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == "top\nbottom\n"


def test_agents_without_end_marker_is_refused(tmp_path):
    (tmp_path / "AGENTS.md").write_text(f"x\n{inst.BEGIN}\n", encoding="utf-8")
    with pytest.raises(inst.SettingsError, match="without"):
        inst.install(tmp_path, python="py")


def test_fresh_install_then_uninstall_leaves_nothing(tmp_path):
    got = inst.install(tmp_path, python="py")
    assert all(p.exists() for p in got.values())
    assert sorted(inst.uninstall(tmp_path)) == sorted(got.values())
    assert not any(p.exists() for p in got.values())
    assert inst.uninstall(tmp_path) == []


def test_user_scope(tmp_path):
    home = tmp_path / "codex_home"
    work = tmp_path / "proj"
    work.mkdir()
    got = inst.install(work, scope="user", python="py", home=home)
    assert got == {"hooks": home / "hooks.json", "config": home / "config.toml",
                   "agents": home / "AGENTS.md"}
    entry = tomllib.loads(got["config"].read_text())["mcp_servers"]["ozagia"]
    assert "--root" not in entry["args"] and "OZAGIA_ROOT" not in entry["env"]
    assert not (work / ".codex").exists() and not (work / "AGENTS.md").exists()
    inst.uninstall(work, scope="user", home=home)
    assert not got["hooks"].exists()


def test_cli_install_uninstall(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    cmd = [sys.executable, "-m", "ozagia.integrations.codex"]
    r = subprocess.run(cmd + ["install", "--workdir", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "/hooks" in r.stdout and "ozagia daemon ensure" in r.stdout
    data = _hooks(tmp_path)
    assert _ours(data, "PreToolUse")[0][1]["command"].startswith(sys.executable)
    r = subprocess.run(cmd + ["uninstall", "--workdir", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".codex" / "hooks.json").exists()
    (tmp_path / ".codex").mkdir(exist_ok=True)
    (tmp_path / ".codex" / "hooks.json").write_text("{bad")
    r = subprocess.run(cmd + ["install", "--workdir", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 1 and "not valid JSON" in r.stderr


def test_hook_shares_the_intent_ref_moving_list():
    from ozagia.integrations import hookcore
    from ozagia.intent.shell import GIT_REF_MOVING
    assert hookcore.REF_MOVING_GIT is GIT_REF_MOVING


# --------------------------------------------------------------------------- #
# outside a repository: no autostart, no brief, nothing written
# --------------------------------------------------------------------------- #
@pytest.fixture
def plain_dir(tmp_path, monkeypatch):
    from ozagia.integrations import hookcore
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
    lambda d: base(d, "SessionStart", source="startup"),
    lambda d: post(d, "Bash", {"command": "ls"}, "notes.py\n"),
    lambda d: post(d, "apply_patch", {"command": PATCH}, "Success."),
], ids=["SessionStart", "PostToolUse-Bash", "PostToolUse-apply_patch"])
def test_non_git_cwd_starts_nothing(plain_dir, payload):
    env = _env(None, autostart_on=True)
    proc = run_hook(payload(plain_dir), sock=None, env=env, cwd=plain_dir)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert not (plain_dir / ".ozagia").exists()
    assert autostart.ping(protocol.socket_path(plain_dir)) is None


def test_autostart_allowed(plain_dir, repo, monkeypatch):
    from ozagia.integrations import hookcore
    assert not hookcore.autostart_allowed(plain_dir)
    assert hookcore.ensure_daemon(plain_dir)["refused"] is True
    assert hookcore.autostart_allowed(repo)
    snap = plain_dir / ".ozagia" / hookcore.SNAPSHOT_NAME
    snap.parent.mkdir()
    snap.write_text("{}")
    assert hookcore.autostart_allowed(plain_dir)
    snap.unlink()
    monkeypatch.setenv("OZAGIA_ROOT", str(plain_dir))
    assert hookcore.autostart_allowed(plain_dir)


# --------------------------------------------------------------------------- #
# the hook's own timeout
# --------------------------------------------------------------------------- #
def test_alarm_leaves_margin_before_codex_timeout():
    from ozagia.integrations import hookcore
    assert hook.HOOK_ALARM_S + hook.BRIEF_ALARM_S < hook.HOOK_TIMEOUT_S
    assert hookcore.AUTOSTART_BUDGET_S + hook.DAEMON_TIMEOUT_S < hook.HOOK_ALARM_S


@pytest.fixture
def clean_env(monkeypatch):
    for k in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(k)


def _run_main(payload):
    import io
    out = io.StringIO()
    t0 = time.monotonic()
    assert hook.main(io.StringIO(json.dumps(payload)), out) == 0
    return out.getvalue(), time.monotonic() - t0


def test_run_hook_timeout_prints_nothing_and_logs(repo, clean_env):
    import io
    from ozagia.integrations import hookcore
    before = signal.getsignal(signal.SIGALRM)
    out = io.StringIO()
    t0 = time.monotonic()
    rc = hookcore.run_hook(lambda p, r: time.sleep(5) or {"x": 1}, log_name=hook.LOG_NAME,
                           timeout_s=1, stdin=io.StringIO(json.dumps(base(repo, "X"))),
                           stdout=out)
    assert rc == 0 and out.getvalue() == "" and time.monotonic() - t0 < 4
    (entry,) = _log_lines(repo)
    assert entry["error"].startswith("HookTimeout")
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.alarm(0) == 0


def test_post_tool_use_timeout_during_autostart(repo, clean_env, monkeypatch):
    from ozagia.integrations import hookcore
    monkeypatch.setattr(hook, "HOOK_ALARM_S", 1)

    def refused(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(hookcore, "daemon_request", refused)
    monkeypatch.setattr(hookcore, "ensure_daemon", lambda *a, **k: time.sleep(5))
    out, took = _run_main(post(repo, "Bash", {"command": "ls"}, "x"))
    assert out == "" and took < 4
    (entry,) = _log_lines(repo)
    assert entry["event"] == "PostToolUse" and entry["error"].startswith("HookTimeout")


def test_session_start_keeps_an_alarm_after_a_swallowed_timeout(repo, clean_env,
                                                                monkeypatch):
    from ozagia.integrations import hookcore
    monkeypatch.setattr(hook, "HOOK_ALARM_S", 1)
    monkeypatch.setattr(hook, "BRIEF_ALARM_S", 1)
    monkeypatch.setattr(hookcore, "ensure_daemon", lambda *a, **k: time.sleep(5))
    monkeypatch.setattr(hook, "session_brief", lambda root: time.sleep(5) or "late")
    out, took = _run_main(base(repo, "SessionStart", source="startup"))
    assert out == "" and took < 4
    entries = _log_lines(repo)
    assert [e["error"].split(":")[0] for e in entries] == ["HookTimeout", "HookTimeout"]


def test_session_start_brief_after_a_swallowed_timeout(repo, clean_env, monkeypatch):
    from ozagia.integrations import hookcore
    monkeypatch.setattr(hook, "HOOK_ALARM_S", 1)
    monkeypatch.setattr(hookcore, "ensure_daemon", lambda *a, **k: time.sleep(5))
    out, took = _run_main(base(repo, "SessionStart", source="startup"))
    assert "[ozagia] session brief" in json.loads(out)["hookSpecificOutput"][
        "additionalContext"]
    assert took < 4


def test_rearm_alarm_outside_run_hook_does_nothing():
    from ozagia.integrations import hookcore
    assert hookcore.rearm_alarm(5) is False
    assert signal.alarm(0) == 0


# --------------------------------------------------------------------------- #
# config.toml: explicit [mcp_servers], refusals that name the line, no EOL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "[mcp_servers]\n",
    '[mcp_servers]\n\n[mcp_servers.ozagia]\ncommand = "/old/python"\n',
    'model = "o3"\n[mcp_servers]\n\n[mcp_servers.ozagia]\ncommand = "/old"\n\n'
    '[profiles.work]\nmodel = "o3"\n',
])
def test_install_with_explicit_mcp_servers_header(tmp_path, text):
    path = _config(tmp_path)
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    inst.install(tmp_path, python="/opt/py/bin/python")
    merged = path.read_text(encoding="utf-8")
    data = tomllib.loads(merged)
    assert data["mcp_servers"]["ozagia"]["command"] == "/opt/py/bin/python"
    assert merged.startswith(text.split("[mcp_servers.ozagia]")[0])
    inst.install(tmp_path, python="/opt/py/bin/python")
    assert path.read_text(encoding="utf-8") == merged
    inst.uninstall(tmp_path)
    rest = tomllib.loads(path.read_text(encoding="utf-8"))
    assert rest.get("mcp_servers", {}) == {}
    assert rest.get("profiles", {}) == tomllib.loads(text).get("profiles", {})


@pytest.mark.parametrize("text, line, why", [
    ('[mcp_servers.ozagia]\ncommand = "x"\n\n[mcp_servers."a/b"]\ncommand = "y"\n',
     4, 'line 4 (`[mcp_servers."a/b"]`) is a table header ozagia cannot read'),
    ('[a]\ns = """\n[mcp_servers.ozagia]\n"""\n',
     3, "multi-line string"),
])
def test_install_refusal_names_the_line(tmp_path, text, line, why):
    path = _config(tmp_path)
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    tomllib.loads(text)  # the file is valid
    with pytest.raises(inst.SettingsError) as err:
        inst.install(tmp_path, python="py")
    msg = str(err.value)
    assert "could not isolate its [mcp_servers.ozagia] table" in msg
    assert why in msg
    assert f"line {line}" in msg
    assert path.read_text(encoding="utf-8") == text
    assert not (tmp_path / ".codex" / "hooks.json").exists()
    with pytest.raises(inst.SettingsError, match="could not isolate"):
        inst.uninstall(tmp_path)
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("text", [
    'model = "o3"', "# only a comment", '[profiles.work]\nmodel = "o3"',
    'model = "o3"\r\n[profiles.work]\r\nmodel = "o3"',
])
def test_config_without_final_newline_round_trips(tmp_path, text):
    path = _config(tmp_path)
    path.parent.mkdir()
    path.write_bytes(text.encode())
    inst.install(tmp_path, python="py")
    merged = path.read_bytes()
    assert merged.startswith(text.encode() + b"\n")
    assert inst.NO_EOL_TOML.encode() in merged
    inst.install(tmp_path, python="py2")
    assert tomllib.loads(path.read_text())["mcp_servers"]["ozagia"]["command"] == "py2"
    assert inst.NO_EOL_TOML.encode() in path.read_bytes()
    inst.uninstall(tmp_path)
    assert path.read_bytes() == text.encode()


@pytest.mark.parametrize("text", ['model = "o3"\n', 'model = "o3"\r\n[p]\r\nx = 1\r\n'])
def test_config_with_final_newline_has_no_marker(tmp_path, text):
    path = _config(tmp_path)
    path.parent.mkdir()
    path.write_bytes(text.encode())
    inst.install(tmp_path, python="py")
    assert path.read_bytes().startswith(text.encode())
    assert inst.NO_EOL_TOML not in path.read_text(encoding="utf-8")
    inst.uninstall(tmp_path)
    assert path.read_bytes() == text.encode()


@pytest.mark.parametrize("text", ["# Project\n\nRun `make test`.", "x", "a\n\nb"])
def test_agents_without_final_newline_round_trips(tmp_path, text):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(text.encode())
    inst.install(tmp_path, python="py")
    merged = path.read_bytes()
    assert merged.startswith(text.encode() + b"\n\n" + inst.BEGIN.encode())
    assert inst.NO_EOL_MD.encode() in merged
    inst.install(tmp_path, python="py")
    assert path.read_bytes() == merged
    inst.uninstall(tmp_path)
    assert path.read_bytes() == text.encode()
