"""`ozagia.harness.opencode.run_agent` and `GraphSession`, with a stub `opencode`.

The stub is a Python script put first on PATH. It never calls a model: it
records what it saw, talks to the daemon the way the plugin does (`ping`,
`hello`, one `tool` read) and prints opencode-style JSON events.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ozagia.core import mesh as M
from ozagia.core import resource_grants as RG
from ozagia.core import temp_space as TS
from ozagia.daemon import autostart as daemon_autostart
from ozagia.harness import launch_status
from ozagia.harness import opencode as hop
from ozagia.harness import session as hs
from ozagia.harness.opencode import (AgentError, git_exclude, load_provider_config,
                                     run_agent)
from ozagia.harness.session import GraphSession, SessionError

SRC = Path(__file__).resolve().parents[1] / "src"
PLUGIN_REL = Path(".opencode") / "plugins" / "ozagia.js"
BRAND_REL = Path(".opencode") / "plugins" / "ozagia" / "tui.js"
EXCLUDES = ["/.opencode/plugins/ozagia.js",
            "/.opencode/plugins/ozagia/tui.js", "/.ozagia/"]
PROFILE_EXCLUDE = "/.opencode/plugins/ozagia-profile/tui.js"

MOD = "def add(a, b):\n    return a + b\n"
TEST = "from pkg.mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

_STUB = r'''
import json, os, socket, subprocess, sys, time
rec = {"argv": sys.argv, "cwd": os.getcwd(), "pid": os.getpid(),
       "started_at": time.time(),
       "env": {k: os.environ.get(k) for k in ("OZAGIA_DAEMON_SOCK", "PWD", "OPENCODE_CONFIG",
                                              "OZAGIA_LAUNCH_STATUS",
                                              "OZAGIA_REPO_MAP_TOKENS", "TMPDIR", "TMP",
                                              "TEMP", "OZAGIA_SCRATCH_ROOT",
                                              "OZAGIA_MESH_SUMMARY",
                                              "OZAGIA_MESH_HARNESS",
                                              "OZAGIA_MESH_SESSION",
                                              "OZAGIA_MESH_MODEL")}}
# `opencode run` reads its message from stdin; the TUI path has none.
if sys.argv[1:2] == ["run"]:
    rec["stdin"] = sys.stdin.read()
    rec["stdin_nlink"] = os.fstat(0).st_nlink
else:
    rec["stdin"] = rec["stdin_nlink"] = None
cfg = os.environ.get("OPENCODE_CONFIG")
rec["config"] = json.load(open(cfg)) if cfg and os.path.isfile(cfg) else None
rec["plugin_present"] = os.path.isfile(os.path.join(os.getcwd(), ".opencode", "plugins", "ozagia.js"))
rec["git_status"] = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                   capture_output=True, text=True).stdout
replies = []
sock = os.environ.get("OZAGIA_DAEMON_SOCK")
if sock:
    deadline = time.monotonic() + 60
    while not os.path.exists(sock) and time.monotonic() < deadline:
        time.sleep(0.02)            # an interactive launch starts us before it is up
    c = socket.socket(socket.AF_UNIX)
    c.settimeout(30)
    c.connect(sock)
    f = c.makefile("rwb")
    for i, m in enumerate([{"op": "ping"},
                           {"op": "hello", "harness": "opencode", "plugin": "ozagia"},
                           {"op": "tool", "harness": "opencode", "tool": "read",
                            "input": {"path": "pkg/mod.py"}}]):
        f.write((json.dumps({"id": i, **m}) + "\n").encode())
        f.flush()
        replies.append(json.loads(f.readline()))
    c.close()
rec["replies"] = replies
path = os.environ["STUB_RECORD"]
with open(path + ".tmp", "w") as fh:
    json.dump(rec, fh)
os.replace(path + ".tmp", path)
if os.environ.get("STUB_MODE") == "sleep":
    time.sleep(300)
print(json.dumps({"type": "text", "part": {"text": "stub reply"}}))
print(json.dumps({"type": "step_finish", "part": {"cost": 0, "tokens": {"input": 3, "output": 2}}}))
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text(MOD)
    (root / "tests" / "test_mod.py").write_text(TEST)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch) -> dict:
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    exe = bindir / "opencode"
    exe.write_text(f"#!{sys.executable}\n{_STUB}", encoding="utf-8")
    exe.chmod(0o755)
    record = tmp_path / "stub_record.json"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("STUB_RECORD", str(record))
    for key in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(key)
    return {"exe": exe, "record": record, "bindir": bindir}


def _record(stub: dict) -> dict:
    return json.loads(stub["record"].read_text(encoding="utf-8"))


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def _exclude_lines(repo: Path) -> list[str]:
    return (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()


def _run(repo: Path, tmp_path: Path, **kw) -> dict:
    kw.setdefault("prompt", "fix the addition")
    return run_agent(repo, runs_dir=tmp_path / "runs", quiet=True, **kw)


def test_run_agent_end_to_end(repo, stub, tmp_path):
    res = _run(repo, tmp_path, model="vllm/test-model")
    rec = _record(stub)

    assert "resource_grants" not in res

    assert rec["argv"][1:] == ["run", "--standalone", "--format", "json", "--auto",
                               "--archived", "-m", "vllm/test-model"]
    assert rec["stdin"] == "fix the addition"
    assert "fix the addition" not in res["argv"]
    assert Path(rec["cwd"]) == repo
    assert rec["env"]["PWD"] == str(repo)
    assert rec["env"]["OZAGIA_DAEMON_SOCK"] == res["sock"]
    ping = rec["replies"][0]
    assert ping["ok"] and Path(ping["root"]) == repo
    assert rec["replies"][2]["handled"] is True
    # The plugin is there during the run, and neither it nor the state dir
    # shows in git status.
    assert rec["plugin_present"] is True
    assert rec["git_status"] == ""

    # Snapshot built; daemon stopped: socket and process group gone.
    assert (repo / ".ozagia" / "graph.json").is_file()
    assert res["prep"]["action"] == "built"
    assert not Path(res["sock"]).exists()
    assert _group_gone(ping["pid"])
    # Plugin removed with its empty directories.
    assert not (repo / PLUGIN_REL).exists()
    assert not (repo / ".opencode").exists()

    cfg = rec["config"]
    assert Path(rec["env"]["OPENCODE_CONFIG"]).parent == Path(res["log_dir"])
    assert set(cfg) == {"$schema"}  # no provider config was supplied
    assert "permission" not in cfg  # Preserve the host's own shell policy.
    assert "model" not in cfg  # passed with -m instead

    assert res["exit_code"] == 0 and res["timed_out"] is False
    assert res["plugin_loaded"] is True
    assert res["captured_calls"] >= 1
    assert res["fallbacks"] == 0
    assert res["workdir"] == str(repo)
    assert res["daemon_stats"]["hellos"] == 1
    assert Path(res["stdout_path"]).read_text().count("step_finish") == 1
    assert "stub reply" in Path(res["transcript_path"]).read_text()
    # The plugin's exclude line goes with the plugin (review fix 14); the
    # state dir's stays while the state dir does.
    assert _exclude_lines(repo).count(EXCLUDES[0]) == 0
    assert _exclude_lines(repo).count(EXCLUDES[1]) == 0
    assert _exclude_lines(repo).count(EXCLUDES[2]) == 1
    assert res["exclude_removed"] == [*EXCLUDES[:2], PROFILE_EXCLUDE]
    assert _git(repo, "status", "--porcelain") == ""


def test_prompt_travels_on_stdin_and_never_appears_in_argv_or_logs(repo, stub, tmp_path):
    # OpenCode logs its full argv once, before the plugin hook runs, so a
    # secret-like prompt must reach it only on stdin.
    secret = "OZAGIA-CANARY-7f3d9c: do not put this prompt in argv"
    res = _run(repo, tmp_path, prompt=secret)
    rec = _record(stub)

    assert rec["stdin"] == secret
    assert secret not in " ".join(rec["argv"])
    assert secret not in " ".join(res["argv"])
    assert res["argv"] == rec["argv"]
    if os.name == "posix":
        # TemporaryFile has no remaining link: nothing was left on disk.
        assert rec["stdin_nlink"] == 0
    # The configuration and logs the harness writes hold no prompt.
    assert secret not in json.dumps(rec["config"])
    logs = [p for p in Path(res["log_dir"]).rglob("*") if p.is_file()]
    assert logs
    for path in logs:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert secret not in text, path


def test_prompt_file_content_goes_to_stdin_and_its_path_is_not_in_argv(
        repo, stub, tmp_path, monkeypatch):
    from ozagia import cli
    monkeypatch.setenv("OZAGIA_LANGUAGE_AUTOINSTALL", "0")
    prompt = tmp_path / "task.txt"
    content = "make the failing retry test pass"
    prompt.write_text(content, encoding="utf-8")
    code = cli.main(["agent", "--workdir", str(repo), "--prompt-file", str(prompt),
                     "--runs-dir", str(tmp_path / "runs")])
    rec = _record(stub)
    assert code == 0
    assert rec["stdin"] == content
    assert str(prompt) not in " ".join(rec["argv"])
    assert content not in " ".join(rec["argv"])


def test_empty_prompt_is_rejected_before_opencode_starts(repo, stub, tmp_path):
    with pytest.raises(AgentError, match="needs a prompt"):
        _run(repo, tmp_path, prompt="")
    assert not stub["record"].exists()


def test_host_opencode_config_is_not_read_implicitly(repo, stub, tmp_path, monkeypatch):
    host = tmp_path / "host.json"
    host.write_text(json.dumps({"provider": {"host": {}}, "mcp": {}}),
                    encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(host))
    _run(repo, tmp_path)
    rec = _record(stub)
    assert set(rec["config"]) == {"$schema"}
    assert rec["env"]["OPENCODE_CONFIG"] != str(host)


def test_provider_config_is_layered_with_ozagia_mcp(repo, stub, tmp_path):
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {"vllm": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "vLLM",
            "options": {"baseURL": "http://127.0.0.1:8000/v1",
                        "apiKey": "{env:VLLM_API_KEY}"},
            "models": {"qwen3.8-27b": {"name": "Qwen 3.8 27B"}},
        }},
        "providers": {"local": {"vllm": {"enabled": True}}},
        "model": "vllm/qwen3.8-27b",
        "small_model": "vllm/qwen3.8-27b",
    }), encoding="utf-8")
    _run(repo, tmp_path, opencode_config=provider, mcp_mode="combined")
    cfg = _record(stub)["config"]
    assert cfg["provider"]["vllm"]["options"]["baseURL"] == "http://127.0.0.1:8000/v1"
    # The placeholder is copied verbatim: never resolved or logged.
    assert cfg["provider"]["vllm"]["options"]["apiKey"] == "{env:VLLM_API_KEY}"
    assert cfg["providers"] == {"local": {"vllm": {"enabled": True}}}
    assert cfg["model"] == "vllm/qwen3.8-27b"
    assert cfg["small_model"] == "vllm/qwen3.8-27b"
    # The harness's own MCP entry is still there, and nothing else was copied.
    assert cfg["mcp"]["ozagia"]["type"] == "local"
    assert set(cfg) == {"$schema", "provider", "providers", "model", "small_model", "mcp"}


def test_model_option_wins_over_the_provider_config_model(repo, stub, tmp_path):
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"model": "vllm/from-config",
                                    "provider": {"vllm": {}}}), encoding="utf-8")
    _run(repo, tmp_path, model="vllm/from-cli", opencode_config=provider)
    rec = _record(stub)
    assert rec["argv"][-1] == "vllm/from-cli"
    assert rec["stdin"] == "fix the addition"
    # `opencode run` gets -m, so the config must not keep the file's model.
    assert "model" not in rec["config"]
    assert rec["config"]["provider"] == {"vllm": {}}

    _run(repo, tmp_path, prompt=None, model="vllm/from-cli",
         opencode_config=provider, background=False)
    rec = _record(stub)
    assert rec["argv"] == [str(stub["exe"]), "--standalone", str(repo)]
    assert rec["config"]["model"] == "vllm/from-cli"


@pytest.mark.parametrize("key", ["plugin", "plugins", "mcp", "permission",
                                 "agent", "agents", "tools"])
def test_unsupported_provider_config_keys_fail_before_launch(repo, stub, tmp_path, key):
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"provider": {"vllm": {}}, key: {}}),
                        encoding="utf-8")
    with pytest.raises(AgentError, match="unsupported key"):
        _run(repo, tmp_path, opencode_config=provider)
    assert not stub["record"].exists()


def test_missing_invalid_and_wrong_shaped_provider_configs_fail_before_launch(
        repo, stub, tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(AgentError, match="cannot read --opencode-config"):
        _run(repo, tmp_path, opencode_config=missing)

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"provider": {"vllm": {"apiKey": "super-secret"}},',
                       encoding="utf-8")
    with pytest.raises(AgentError, match="not valid JSON") as exc:
        _run(repo, tmp_path, opencode_config=invalid)
    assert "super-secret" not in str(exc.value)

    not_object = tmp_path / "list.json"
    not_object.write_text('["vllm"]', encoding="utf-8")
    with pytest.raises(AgentError, match="must be a JSON object"):
        _run(repo, tmp_path, opencode_config=not_object)

    bad_provider = tmp_path / "bad-provider.json"
    bad_provider.write_text('{"provider": ["vllm"]}', encoding="utf-8")
    with pytest.raises(AgentError, match="provider must be an object"):
        _run(repo, tmp_path, opencode_config=bad_provider)

    bad_model = tmp_path / "bad-model.json"
    bad_model.write_text('{"model": 7}', encoding="utf-8")
    with pytest.raises(AgentError, match="model must be a string"):
        _run(repo, tmp_path, opencode_config=bad_model)
    assert not stub["record"].exists()


def test_provider_config_error_does_not_echo_secrets(tmp_path):
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({
        "provider": {"vllm": {"options": {"apiKey": "super-secret"}}},
        "plugin": ["some-plugin"],
    }), encoding="utf-8")
    with pytest.raises(AgentError) as exc:
        load_provider_config(provider)
    text = str(exc.value)
    assert "super-secret" not in text
    assert "unsupported key(s): plugin" in text


def test_repo_map_budget_is_passed_to_opencode_and_reported(repo, stub, tmp_path):
    res = _run(repo, tmp_path, repo_map_tokens=500)
    rec = _record(stub)
    assert rec["env"]["OZAGIA_REPO_MAP_TOKENS"] == "500"
    assert res["repo_map_tokens"] == 500
    assert res["daemon_stats"]["repo_map_delivered"] == 0
    assert res["daemon_stats"]["repo_map_skipped"] == 0
    summary = hop._summary(res)
    assert "repo map: 500 tokens; delivered 0, skipped 0" in summary


def test_repo_map_is_off_without_the_option_even_if_inherited(repo, stub, tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("OZAGIA_REPO_MAP_TOKENS", "900")
    res = _run(repo, tmp_path)
    rec = _record(stub)
    assert rec["env"]["OZAGIA_REPO_MAP_TOKENS"] is None
    assert res["repo_map_tokens"] is None
    assert "repo map:" not in hop._summary(res)


@pytest.mark.parametrize("value", [199, 8001, True, "1500", 1500.0])
def test_run_agent_rejects_invalid_repo_map_budgets(repo, value):
    with pytest.raises(AgentError, match="repo_map_tokens must be an integer"):
        run_agent(repo, prompt="x", repo_map_tokens=value)


def test_run_agent_accepts_the_repo_map_bounds(repo, stub, tmp_path):
    low = _run(repo, tmp_path, repo_map_tokens=200)
    high = _run(repo, tmp_path, repo_map_tokens=8000)
    assert low["repo_map_tokens"] == 200
    assert high["repo_map_tokens"] == 8000


def test_second_run_is_idempotent_and_refreshes(repo, stub, tmp_path):
    first = _run(repo, tmp_path)
    assert sorted(first["exclude"]["added"]) == sorted([*EXCLUDES, PROFILE_EXCLUDE])
    second = _run(repo, tmp_path)
    assert second["exclude"]["added"] == [*EXCLUDES[:2], PROFILE_EXCLUDE]
    assert second["prep"]["action"] == "fresh"
    assert _exclude_lines(repo).count(EXCLUDES[0]) == 0
    assert _exclude_lines(repo).count(EXCLUDES[1]) == 0
    assert _exclude_lines(repo).count(EXCLUDES[2]) == 1
    (repo / "pkg" / "mod.py").write_text(MOD + "\n\ndef sub(a, b):\n    return a - b\n")
    third = _run(repo, tmp_path)
    assert third["prep"]["action"] == "refreshed"
    assert third["prep"]["files"] == 1
    assert third["plugin_loaded"] is True


def test_git_exclude_appends_only_missing_lines(repo):
    path = repo / ".git" / "info" / "exclude"
    path.write_text("*.log\n/.ozagia/", encoding="utf-8")  # no trailing newline
    out = git_exclude(repo)
    assert out["added"] == [*EXCLUDES[:2], PROFILE_EXCLUDE]
    assert path.read_text().splitlines() == [
        "*.log", "/.ozagia/", *EXCLUDES[:2], PROFILE_EXCLUDE]
    assert git_exclude(repo)["added"] == []
    sub = repo / "pkg"
    assert git_exclude(sub)["added"] == [
        "/pkg/.opencode/plugins/ozagia.js",
        "/pkg/.opencode/plugins/ozagia/tui.js",
        "/pkg/.opencode/plugins/ozagia-profile/tui.js", "/pkg/.ozagia/"]


def test_git_exclude_outside_git_is_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_exclude(plain) is None


def test_clone_runs_in_clone_and_leaves_source_untouched(repo, stub, tmp_path):
    exclude_before = (repo / ".git" / "info" / "exclude").read_bytes()
    head = _git(repo, "rev-parse", "HEAD")
    res = _run(repo, tmp_path, clone=True)
    rec = _record(stub)
    work = Path(res["workdir"])
    assert work != repo and Path(rec["cwd"]) == work
    assert (tmp_path / "runs") in work.parents
    assert res["base"] == head and _git(work, "rev-parse", "HEAD") == head
    assert _git(work, "remote") == ""
    assert rec["plugin_present"] is True
    assert (work / ".ozagia" / "graph.json").is_file()
    assert not (work / PLUGIN_REL).exists()
    # The source tree: no state dir, no plugin, same exclude file, clean.
    assert not (repo / ".ozagia").exists()
    assert not (repo / ".opencode").exists()
    assert (repo / ".git" / "info" / "exclude").read_bytes() == exclude_before
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert res["plugin_loaded"] is True


def test_clone_at_base(repo, stub, tmp_path):
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "mod.py").write_text(MOD + "# later\n")
    _git(repo, "commit", "-qam", "later")
    res = _run(repo, tmp_path, clone=True, base="HEAD~1")
    assert res["base"] == first
    assert (Path(res["workdir"]) / "pkg" / "mod.py").read_text() == MOD


def test_keep_plugin_and_existing_plugin(repo, stub, tmp_path):
    _run(repo, tmp_path, keep_plugin=True)
    assert (repo / PLUGIN_REL).is_file()
    # A plugin that was there before the run, and is not exactly ours, is
    # not removed afterwards. One with our exact source is (review fix 14).
    (repo / PLUGIN_REL).write_text("// edited by the user\n", encoding="utf-8")
    _run(repo, tmp_path)
    assert (repo / PLUGIN_REL).is_file()
    (repo / PLUGIN_REL).unlink()
    _run(repo, tmp_path, keep_plugin=True)
    _run(repo, tmp_path)
    assert not (repo / PLUGIN_REL).exists()


def test_interactive_uses_standalone_tui(repo, stub, tmp_path):
    phases = _Phases()
    res = _run(repo, tmp_path, prompt=None, model="vllm/test-model", progress=phases,
               background=False)
    rec = _record(stub)
    assert rec["argv"][1:] == ["--standalone", str(repo)]
    assert rec["stdin"] is None
    assert "--prompt" not in rec["argv"]
    assert rec["config"]["model"] == "vllm/test-model"
    assert rec["env"]["OZAGIA_DAEMON_SOCK"] == res["sock"]
    assert res["interactive"] is True and res["exit_code"] == 0
    assert res["plugin_loaded"] is True
    assert res["transcript_path"] is None
    assert not (repo / PLUGIN_REL).exists()
    # The launch says what it is doing, and the TUI starts after the daemon.
    assert phases.of("note")[-1] == "opencode: starting the TUI"


def test_run_agent_reports_the_phases_before_opencode(repo, stub, tmp_path):
    phases = _Phases()
    _run(repo, tmp_path, progress=phases)
    assert [step.split(" for ")[0] for step in phases.of("step")] == [
        "graph: making the snapshot current", "graph daemon: starting"]
    assert phases.of("note")[-1] == "opencode: starting"


def test_interactive_backgrounds_the_snapshot_for_the_tui(repo, stub, tmp_path):
    phases = _Phases()
    res = _run(repo, tmp_path, prompt=None, progress=phases)
    rec = _record(stub)
    assert res["background"] is True and res["interactive"] is True
    # The TUI got the status path; it starts before the daemon is ready and
    # the stub waits for the socket the launch promised.
    assert rec["env"]["OZAGIA_LAUNCH_STATUS"] == res["launch_status"]
    assert rec["env"]["OZAGIA_DAEMON_SOCK"] == res["sock"]
    status = json.loads(Path(res["launch_status"]).read_text(encoding="utf-8"))
    assert status["phase"] == "ready" and status["sock"] == res["sock"]
    assert status["action"] == "built" and status["nodes"] == res["prep"]["nodes"]
    assert res["prep"]["action"] == "built" and res["ready_s"] is not None
    assert res["plugin_loaded"] is True
    assert phases.of("note") == [
        "opencode: starting the TUI (the graph builds in the background)"]


def test_interactive_background_that_never_finishes_is_cancelled(repo, stub, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("STUB_MODE", "sleep")
    # A prep interpreter that never gets to work: the TUI closes long before
    # the snapshot is ready.
    slow = tmp_path / "slow-python"
    slow.write_text(f"#!/bin/sh\nsleep 30\nexec {sys.executable} \"$@\"\n", encoding="utf-8")
    slow.chmod(0o755)
    res = _run(repo, tmp_path, prompt=None, timeout_s=0.3, tool_python=str(slow))
    assert res["background"] is True and res["timed_out"] is True
    # The TUI exited before the daemon was ready: no daemon, no socket left,
    # and the summary says why rather than inventing a graph state.
    assert res["prep"] == {} and res["ready_s"] is None
    assert not Path(res["sock"]).exists()
    assert res["daemon_stats"] is None
    summary = hop._summary(res)
    assert "not ready (the session ended first)" in summary


def test_graph_session_background_fills_in_the_report_and_status(repo, stub, tmp_path):
    status = tmp_path / "launch_status.json"
    with GraphSession(repo, log_dir=tmp_path / "logs", background=True,
                      status_path=status) as s:
        assert isinstance(s.sock, Path)
        assert json.loads(status.read_text(encoding="utf-8"))["phase"] in (
            "preparing", "building", "refreshing", "daemon", "ready")
        deadline = time.monotonic() + 60
        while s.ready_s is None and time.monotonic() < deadline:
            assert s.error is None
            time.sleep(0.02)
        assert s.prep["action"] == "built" and s.ready_s is not None
        assert s.sock.is_socket()
        doc = json.loads(status.read_text(encoding="utf-8"))
        while doc.get("phase") != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)        # the worker reports ready just after the daemon
            doc = json.loads(status.read_text(encoding="utf-8"))
        assert doc["phase"] == "ready" and doc["sock"] == str(s.sock)
        assert doc["action"] == "built" and doc["nodes"] == s.prep["nodes"]
        assert doc["daemon_pid"] == s.pid
        assert doc["started_at"] <= doc["updated_at"]
    assert not s.sock.exists()


def test_prepare_snapshot_writes_its_phase_to_the_launch_status(repo, stub, tmp_path):
    status = tmp_path / "launch_status.json"
    hs.prepare_snapshot(repo, repo / ".ozagia" / "graph.json", log_dir=tmp_path / "logs",
                        status_path=status)
    doc = json.loads(status.read_text(encoding="utf-8"))
    assert doc["phase"] == "building" and doc["detail"] == "no snapshot"


def test_launch_status_merges_writers_bounds_text_and_reads_safely(tmp_path):
    path = tmp_path / "launch_status.json"
    launch_status.write(path, phase="preparing", root="/repo", started_at=1.0)
    doc = launch_status.write(path, phase="building", detail="x" * 900)
    assert doc["phase"] == "building" and doc["root"] == "/repo"
    assert doc["started_at"] == 1.0 and doc["version"] == launch_status.VERSION
    assert len(doc["detail"]) == launch_status.MAX_TEXT + 1
    assert doc["detail"].endswith("…")
    assert launch_status.read(path)["updated_at"] >= doc["started_at"]
    assert launch_status.read(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert launch_status.read(bad) is None
    assert launch_status.write(None, phase="ready") is None


def test_missing_opencode_is_a_clear_error(repo, stub, tmp_path):
    with pytest.raises(AgentError, match="not found on PATH"):
        _run(repo, tmp_path, opencode="no-such-opencode-ozagia-test")
    assert not (tmp_path / "runs").exists()
    assert not (repo / ".ozagia").exists()


def test_bad_arguments(repo, stub, tmp_path):
    with pytest.raises(AgentError, match="needs a prompt"):
        _run(repo, tmp_path, prompt=None, interactive=False)
    with pytest.raises(AgentError, match="needs clone"):
        _run(repo, tmp_path, base="HEAD")
    with pytest.raises(AgentError, match="not a commit"):
        _run(repo, tmp_path, clone=True, base="no-such-rev")


def test_daemon_that_never_gets_ready_raises_and_cleans_up(repo, stub, tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "_daemon_argv", lambda *a: [sys.executable, "-c",
                                                        "import sys; sys.exit(3)"])
    with pytest.raises(SessionError, match="exited before ready"):
        _run(repo, tmp_path)
    assert not stub["record"].exists()
    assert not (repo / PLUGIN_REL).exists()


def test_keyboard_interrupt_during_run_tears_down(repo, stub, tmp_path, monkeypatch):
    seen: dict = {}

    def interrupted(argv, *, cwd, env, timeout_s, stdout, stderr, stdin):
        seen["sock"] = env["OZAGIA_DAEMON_SOCK"]
        seen["plugin"] = (Path(cwd) / PLUGIN_REL).is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(hop, "run_group", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _run(repo, tmp_path)
    assert seen["plugin"] is True
    assert not Path(seen["sock"]).exists()
    assert not (repo / PLUGIN_REL).exists()


def test_sigint_to_the_harness_tears_everything_down(repo, stub, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(SRC), STUB_MODE="sleep")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia.cli", "agent", "--workdir", str(repo),
         "--prompt", "never finishes", "--runs-dir", str(tmp_path / "runs")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    try:
        deadline = time.monotonic() + 60
        while not stub["record"].exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "stub never started"
            time.sleep(0.05)
        rec = _record(stub)
        daemon_pid = rec["replies"][0]["pid"]
        assert rec["plugin_present"]
        os.kill(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert proc.returncode == 130, err
    assert "interrupted" in err
    assert not Path(rec["env"]["OZAGIA_DAEMON_SOCK"]).exists()
    assert _group_gone(daemon_pid)
    assert _group_gone(rec["pid"])  # the stub's own process group
    assert not (repo / PLUGIN_REL).exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_graph_session_lifecycle(repo, stub, tmp_path):
    with GraphSession(repo, log_dir=tmp_path / "logs") as s:
        assert s.prep["action"] == "built" and s.build_s is not None
        assert s.ready_s is not None and s.sock.is_socket()
        assert s.dump == repo / ".ozagia" / "graph.json"
        assert s.stats()["hellos"] == 0
        pid = s.pid
        sock = s.sock
        lock = daemon_autostart.lock_path(repo, sock)
        assert lock.is_file()
    assert not s.sock.exists()
    assert not lock.exists()  # the per-socket lock goes with the daemon
    assert _group_gone(pid)
    assert s.final_stats["by_op"].get("stats") == 1
    assert s.stats() == s.final_stats
    assert (tmp_path / "logs" / "daemon_journal.jsonl").is_file()


class _Phases:
    """Records what a launch reports through a `harness.progress.Progress`."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def step(self, message: str) -> None:
        self.events.append(("step", message))

    def note(self, message: str) -> None:
        self.events.append(("note", message))

    def close(self) -> None:
        self.events.append(("close", ""))

    def of(self, kind: str) -> list[str]:
        return [message for event, message in self.events if event == kind]


def test_graph_session_reports_the_launch_phases(repo, stub, tmp_path):
    phases = _Phases()
    with GraphSession(repo, log_dir=tmp_path / "logs", progress=phases) as s:
        assert s.prep["action"] == "built"
    steps, notes = phases.of("step"), phases.of("note")
    assert steps[0].startswith(f"graph: making the snapshot current for {repo}")
    assert steps[0].endswith("(build=auto)")
    assert notes[0].startswith("graph: built (") and " nodes)" in notes[0]
    assert notes[0].endswith(": no snapshot")
    assert steps[1] == "graph daemon: starting"
    assert notes[1].startswith("graph daemon: ready in ")
    assert not phases.of("close")               # a successful enter needs no cleanup


def test_graph_session_clears_the_line_when_the_snapshot_fails(repo, stub, tmp_path):
    phases = _Phases()
    with pytest.raises(SessionError):
        with GraphSession(repo, build="never", log_dir=tmp_path / "logs",
                          progress=phases):
            pass
    assert phases.of("step") and not phases.of("note")
    assert phases.of("close")


def test_graph_session_build_never_needs_a_snapshot(repo, stub, tmp_path):
    with pytest.raises(SessionError, match="build=never"):
        with GraphSession(repo, build="never", log_dir=tmp_path / "logs"):
            pass
    with pytest.raises(SessionError, match="build must be"):
        GraphSession(repo, build="sometimes")


def test_graph_session_state_dir_and_run_python(repo, stub, tmp_path):
    state = tmp_path / "state"
    with GraphSession(repo, state_dir=state, run_python=sys.executable,
                      log_dir=tmp_path / "logs") as s:
        assert s.dump == state / "graph.json"
        assert s.env["OZAGIA_STATE_DIR"] == str(state)
        assert s.env["PATH"].split(os.pathsep)[0] == str(tmp_path / "logs" / "bin")
    assert (state / "graph.json").is_file()
    assert not (repo / ".ozagia").exists()


def test_plugin_left_by_a_killed_run_is_removed(repo, stub, tmp_path):
    # review fix 14: a hard-killed run leaves the plugin (our exact source)
    # and its exclude line; the next run treats the file as its own.
    from ozagia.integrations.opencode import install as oc
    oc.install(repo)
    git_exclude(repo)
    res = _run(repo, tmp_path)
    assert _record(stub)["plugin_present"] is True
    assert not (repo / PLUGIN_REL).exists()
    assert res["exclude"]["added"] == []          # the leftover line was there
    assert _exclude_lines(repo).count(EXCLUDES[0]) == 1  # not ours to remove


def test_foreign_plugin_file_is_kept(repo, stub, tmp_path):
    other = repo / PLUGIN_REL
    other.parent.mkdir(parents=True)
    other.write_text("// someone else's plugin\n", encoding="utf-8")
    _run(repo, tmp_path)
    assert other.is_file()


def test_daemon_argv_passes_the_parent_pid():
    import ozagia.harness.session as hs
    argv = hs._daemon_argv("py", Path("/r"), Path("/d"), Path("/j"), Path("/s"))
    assert argv[-2:] == ["--parent-pid", str(os.getpid())]


def _managed_run(repo, monkeypatch, tmp_path, **kw):
    """A default run (no --runs-dir), with /tmp redirected under tmp_path."""
    monkeypatch.setattr(hop.tempfile, "gettempdir",
                        lambda: str(tmp_path / "temp-base"))
    kw.setdefault("prompt", "fix the addition")
    return run_agent(repo, quiet=True, **kw)


def test_default_run_root_is_registered_with_managed_tmp(repo, stub, monkeypatch,
                                                         tmp_path):
    res = _managed_run(repo, monkeypatch, tmp_path)
    rec = _record(stub)

    assert res["tmp_registered"] is True
    assert res["tmp_id"] and res["tmp_path"] == res["run_root"]
    entry = TS.get(repo, res["tmp_id"])
    assert entry["kind"] == "agent" and entry["owned"] is True
    assert entry["path"] == res["run_root"]
    assert entry["ttl_s"] == hop.DEFAULT_TMP_TTL_S
    assert res["tmp_ttl_s"] == hop.DEFAULT_TMP_TTL_S
    assert res["tmp_expires_at"] == entry["expires_at"]
    marker = Path(res["run_root"]) / TS.MARKER_NAME
    assert marker.is_file()

    # TMPDIR/TMP/TEMP point at <run root>/tmp, not the inherited /tmp.
    tmpdir = Path(rec["env"]["TMPDIR"])
    assert tmpdir == Path(res["run_root"]) / "tmp"
    assert rec["env"]["TMP"] == rec["env"]["TEMP"] == rec["env"]["TMPDIR"]
    assert tmpdir.stat().st_mode & 0o777 == 0o700
    probe = tmpdir / "shell-probe"
    subprocess.run(["/bin/sh", "-c", 'printf ok > "$TMPDIR/shell-probe"'],
                   env={**os.environ, "TMPDIR": rec["env"]["TMPDIR"]}, check=True)
    assert probe.read_text() == "ok"

    # The disk-backed scratch workspace is under the source state dir and is
    # registered separately from the run root.
    scratch = Path(rec["env"]["OZAGIA_SCRATCH_ROOT"])
    assert scratch == TS.workspaces_root(repo) / "agent" / Path(res["run_root"]).name
    assert scratch.is_dir()
    entries = {entry["path"]: entry for entry in TS.list_entries(repo, kind="agent")}
    assert str(scratch) in entries and entries[str(scratch)]["owned"] is True
    assert res["scratch_root"] == str(scratch)

    summary = hop._summary(res)
    assert res["tmp_id"] in summary
    assert str(res["tmp_path"]) in summary
    assert "ozagia tmp release" in summary and "--delete" in summary


def test_clean_logs_deletes_only_after_a_successful_run(repo, stub, monkeypatch,
                                                        tmp_path):
    res = _managed_run(repo, monkeypatch, tmp_path, clean_logs=True)
    assert res["exit_code"] == 0 and res["timed_out"] is False
    assert res["tmp_cleaned"] is True
    assert not Path(res["run_root"]).exists()
    assert not Path(res["scratch_root"]).exists()
    assert TS.get(repo, res["tmp_id"])["state"] == "released"


def test_clean_logs_keeps_a_failed_or_timed_out_run(repo, stub, monkeypatch,
                                                    tmp_path):
    monkeypatch.setenv("STUB_MODE", "sleep")
    res = _managed_run(repo, monkeypatch, tmp_path, timeout_s=0.3, clean_logs=True)
    assert res["timed_out"] is True
    assert res["tmp_cleaned"] is False
    assert Path(res["run_root"]).is_dir()
    assert Path(res["scratch_root"]).is_dir()


def test_clean_logs_keeps_a_clone(repo, stub, monkeypatch, tmp_path):
    res = _managed_run(repo, monkeypatch, tmp_path, clone=True, clean_logs=True)
    assert res["exit_code"] == 0 and res["clone"] is True
    assert res["tmp_cleaned"] is False
    assert Path(res["run_root"]).is_dir()
    assert "clone kept" in hop._summary(res)


def test_explicit_runs_dir_is_not_registered_and_leaves_the_source_untouched(
        repo, stub, tmp_path):
    res = _run(repo, tmp_path, clone=True)

    assert res["tmp_registered"] is False
    assert res["tmp_id"] is None and res["tmp_path"] is None
    assert res["tmp_leased"] is False
    assert res["scratch_root"] == str(Path(res["run_root"]) / "scratch")
    assert Path(res["scratch_root"]).is_dir()
    assert not (Path(res["run_root"]) / TS.LEASE_NAME).exists()
    # No managed entry and no source state dir created by the harness.
    assert TS.list_entries(repo, kind="agent") == []
    assert not (repo / ".ozagia").exists()
    assert not (Path(res["run_root"]).parent / ".ozagia").exists()
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_managed_run_acquires_and_releases_the_run_root_lease(
        repo, stub, monkeypatch, tmp_path):
    leases = []
    original = TS.acquire_lease

    def spy(root, entry_id):
        lease = original(root, entry_id)
        leases.append((entry_id, lease))
        return lease

    monkeypatch.setattr(TS, "acquire_lease", spy)
    res = _managed_run(repo, monkeypatch, tmp_path)

    assert res["tmp_leased"] is True
    assert leases and leases[0][0] == res["tmp_id"]
    assert leases[0][1].closed is True
    assert TS.lease_held(repo, res["tmp_id"]) is False
    assert (Path(res["run_root"]) / TS.LEASE_NAME).is_file()


# --------------------------------------------------------------------------- #
# session mesh (layer 0-1)
# --------------------------------------------------------------------------- #
def test_mesh_enabled_registers_heartbeats_and_unregisters(repo, stub,
                                                           monkeypatch, tmp_path):
    beats = []
    original = M.heartbeat

    def spy(*args, **kwargs):
        beats.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(M, "heartbeat", spy)
    monkeypatch.setattr(hop, "MESH_HEARTBEAT_S", 0.02)
    monkeypatch.setenv("STUB_MODE", "sleep")
    res = _run(repo, tmp_path, mesh=True, timeout_s=0.3)
    rec = _record(stub)

    assert res["mesh_enabled"] is True
    assert res["mesh_registered"] is True
    assert res["mesh_id"] == "default"
    assert res["mesh_session"] == f"run:{Path(res['run_root']).name}"
    assert not res["mesh_session"].startswith("ses_")
    assert res["mesh_key"].startswith("sk_")
    assert res["mesh_exposure"] == "read"
    assert res["mesh_peer_count"] == 0
    assert res["mesh_lease_ids"] == []
    assert res["mesh_cleanup_errors"] == []
    assert beats, "the heartbeat thread must run while the session is alive"

    summary = rec["env"]["OZAGIA_MESH_SUMMARY"]
    assert summary and len(summary) <= hop.MESH_SUMMARY_MAX_CHARS
    assert res["mesh_key"] in summary
    assert "role=peer" in summary and "exposure=read" in summary
    assert rec["env"]["OZAGIA_MESH_HARNESS"] == "opencode-runner"
    assert rec["env"]["OZAGIA_MESH_SESSION"] == res["mesh_session"]

    status = M.status(repo, harness="opencode-runner",
                      session=res["mesh_session"])
    self_row = status["self"]
    assert self_row["effective_state"] == "ended"
    assert self_row["exposure"] == "read"
    assert self_row["worktree"] == str(repo)
    assert "mesh:" in hop._summary(res)


def test_mesh_disabled_wins_over_env_and_creates_no_state(repo, stub,
                                                          monkeypatch, tmp_path):
    monkeypatch.setenv("OZAGIA_MESH", "1")
    res = _run(repo, tmp_path, mesh=False)
    rec = _record(stub)

    assert res["mesh_enabled"] is False
    assert res["mesh_registered"] is False
    assert not (repo / ".ozagia" / "mesh").exists()
    assert rec["env"]["OZAGIA_MESH_SUMMARY"] is None
    assert "mesh: off" in hop._summary(res)


def test_mesh_config_enabled_is_used_when_no_flag_is_given(repo, stub, tmp_path):
    (repo / ".ozagia.toml").write_text("[mesh]\nenabled = true\n",
                                       encoding="utf-8")
    res = _run(repo, tmp_path)

    assert res["mesh_enabled"] is True and res["mesh_registered"] is True


def test_mesh_model_defaults_to_read_and_adds_no_model_env(repo, stub, tmp_path):
    res = _run(repo, tmp_path, mesh=True)
    rec = _record(stub)

    assert res["mesh_model"] == "read"
    assert res["mesh_exposure"] == "read"
    assert rec["env"]["OZAGIA_MESH_MODEL"] is None
    summary = rec["env"]["OZAGIA_MESH_SUMMARY"]
    assert "exposure=read" in summary


def test_mesh_model_coordinate_is_passed_to_the_plugin(repo, stub, tmp_path):
    res = _run(repo, tmp_path, mesh=True, mesh_model="coordinate")
    rec = _record(stub)

    assert res["mesh_model"] == "coordinate"
    assert res["mesh_exposure"] == "coordinate"
    assert rec["env"]["OZAGIA_MESH_MODEL"] == "coordinate"
    summary = rec["env"]["OZAGIA_MESH_SUMMARY"]
    assert "exposure=coordinate" in summary


def test_mesh_model_supervise_is_passed_to_the_plugin(repo, stub, tmp_path):
    res = _run(repo, tmp_path, mesh=True, mesh_model="supervise")
    rec = _record(stub)

    assert res["mesh_model"] == "supervise"
    assert res["mesh_exposure"] == "supervise"
    assert rec["env"]["OZAGIA_MESH_MODEL"] == "supervise"
    summary = rec["env"]["OZAGIA_MESH_SUMMARY"]
    assert "exposure=supervise" in summary
    assert "model=supervise" in hop._summary(res)


def test_mesh_model_env_and_explicit_argument_precedence(repo, stub, tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("OZAGIA_MESH_MODEL", "coordinate")
    from_env = _run(repo, tmp_path, mesh=True)
    assert from_env["mesh_model"] == "coordinate"

    explicit = _run(repo, tmp_path, mesh=True, mesh_model="read")
    rec = _record(stub)
    assert explicit["mesh_model"] == "read"
    assert explicit["mesh_exposure"] == "read"
    assert rec["env"]["OZAGIA_MESH_MODEL"] is None


def test_mesh_model_invalid_argument_is_refused(repo, stub, tmp_path):
    with pytest.raises(hop.AgentError, match="mesh model"):
        _run(repo, tmp_path, mesh=True, mesh_model="admin")


def test_mesh_registration_failure_does_not_abort_the_run(repo, stub,
                                                          monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError("mesh registry unavailable")

    monkeypatch.setattr(M, "register", fail)
    res = _run(repo, tmp_path, mesh=True)
    rec = _record(stub)

    assert res["exit_code"] == 0 and res["timed_out"] is False
    assert res["mesh_enabled"] is True
    assert res["mesh_registered"] is False
    assert "mesh registry unavailable" in res["mesh_warning"]
    assert rec["env"]["OZAGIA_MESH_SUMMARY"] is None
    assert "off/unavailable" in hop._summary(res)


def test_mesh_cleanup_releases_leases_on_an_exception(repo, stub,
                                                      monkeypatch, tmp_path):
    def boom(exe, work, session, **kwargs):
        run_root = Path(kwargs["log_dir"]).parent
        name = f"run:{run_root.name}"
        M.register(repo, "opencode-runner", name, exposure="coordinate")
        M.lease_acquire(repo, "opencode-runner", name, kind="branch",
                        resource_id="main", mode="read")
        raise RuntimeError("stop after registration")

    monkeypatch.setattr(hop, "_with_plugin", boom)
    with pytest.raises(RuntimeError, match="stop after registration"):
        _run(repo, tmp_path, mesh=True)

    records = list((repo / ".ozagia" / "mesh" / "default" /
                    "sessions").glob("*.json"))
    assert len(records) == 1
    session = json.loads(records[0].read_text(encoding="utf-8"))
    assert session["state"] == "ended"
    assert session["harness"] == "opencode-runner"

    leases = M.lease_list(repo, include_all=True)["leases"]
    mine = [item for item in leases
            if item["holder"]["session"] == session["session"]]
    assert mine and all(item["effective_state"] == "released" for item in mine)


def test_mesh_clone_registers_under_the_source_state_dir(repo, stub, tmp_path):
    res = _run(repo, tmp_path, mesh=True, clone=True)

    assert res["mesh_registered"] is True
    assert Path(res["workdir"]) != repo
    source_sessions = repo / ".ozagia" / "mesh" / "default" / "sessions"
    assert source_sessions.is_dir()
    record = json.loads(next(source_sessions.glob("*.json")).read_text(
        encoding="utf-8"))
    assert record["worktree"] == res["workdir"]
    assert not (Path(res["workdir"]) / ".ozagia" / "mesh").exists()


def test_mesh_summary_counts_peers_and_carries_no_message_body(repo, stub,
                                                               tmp_path):
    M.register(repo, "peer", "a", pid=os.getpid(), exposure="coordinate")
    M.register(repo, "peer", "b", pid=os.getpid(), exposure="read")
    M.send(repo, "peer", "a", to="peer:b", kind="note", subject="SUBJECT CANARY",
           body="BODY CANARY")
    res = _run(repo, tmp_path, mesh=True)
    summary = _record(stub)["env"]["OZAGIA_MESH_SUMMARY"]

    assert res["mesh_peer_count"] == 2
    assert "peers live=2 stale=0" in summary
    assert "SUBJECT CANARY" not in summary
    assert "BODY CANARY" not in summary


def test_mesh_summary_text_is_bounded_and_keeps_the_unread_count():
    leases = [{
        "effective_state": "active",
        "resource": {"kind": "worktree",
                     "id": "/tmp/" + "x" * 300, "mode": "write"},
        "holder": {"key": "sk_other"},
        "expires_at": "2026-01-01T00:00:00+00:00",
        "queue": [{"key": "sk_a"}, {"key": "sk_b"}],
    } for _ in range(4)]
    links = [{
        "peer": {"harness": "opencode", "session": "s" * 100},
        "relation": "peer", "state": "active",
    } for _ in range(4)]
    payload = {
        "mesh_id": "default",
        "self": {"key": "sk_self", "harness": "opencode-runner",
                 "session": "run:abcdef", "role": "peer",
                 "exposure": "read", "unread": 9, "links": links},
        "sessions": [], "leases": leases,
    }
    text = hop._mesh_summary_text(payload)

    assert len(text) <= hop.MESH_SUMMARY_MAX_CHARS
    assert text.endswith("unread=9")
    assert "mesh=default" in text


def test_mesh_heartbeat_failure_is_advisory_not_fatal(repo, stub, monkeypatch,
                                                      tmp_path):
    monkeypatch.setattr(
        M, "heartbeat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("heartbeat unavailable")))
    monkeypatch.setattr(hop, "MESH_HEARTBEAT_S", 0.02)
    monkeypatch.setenv("STUB_MODE", "sleep")
    res = _run(repo, tmp_path, mesh=True, timeout_s=0.3)

    assert res["mesh_registered"] is True
    assert "heartbeat unavailable" in res["mesh_warning"]
    status = M.status(repo, harness="opencode-runner",
                      session=res["mesh_session"])
    assert status["self"]["effective_state"] == "ended"


# --------------------------------------------------------------------------- #
# resource grants
# --------------------------------------------------------------------------- #
def _harness_grant(repo: Path, *grant_ids: str) -> None:
    (repo / "ozagia").mkdir(exist_ok=True)
    blocks = ["[resources]\nenabled = false\n"]
    for grant_id in grant_ids:
        blocks.append(
            f'[resource_grants.{grant_id}]\n'
            'kind = "service"\nprovider = "other"\ncleanup = "destroy"\n')
    (repo / "ozagia" / "resources.toml").write_text(
        "".join(blocks), encoding="utf-8")


def _harness_grants_ready(monkeypatch) -> None:
    def ready(root, grant_id, **kwargs):
        return {"state": "ready", "ready": True, "checks": [],
                "permissions": [], "budget": None, "cleanup": "destroy",
                "approval": {"required": False, "granted": False}}
    monkeypatch.setattr(RG, "check_grant", ready)


def test_run_agent_claims_and_releases_a_resource_grant(
        repo, stub, tmp_path, monkeypatch):
    _harness_grant(repo, "svc")
    _harness_grants_ready(monkeypatch)
    res = _run(repo, tmp_path, resource_grants=["svc"])
    grants = res["resource_grants"]
    assert grants["declared"] == ["svc"]
    claim = grants["claims"][0]
    assert claim["status"] == "released"
    assert claim["release"]["applied"] is True
    assert claim["cleanup"]["status"] == "unknown"
    assert grants["unreleased"] == []
    assert RG.claims(repo)["counts"]["released"] == 1
    assert res["exit_code"] == 0


def test_run_agent_config_error_after_preflight_does_not_leak_a_claim(
        repo, stub, tmp_path, monkeypatch):
    _harness_grant(repo, "svc")
    _harness_grants_ready(monkeypatch)
    with pytest.raises(AgentError, match="mesh model"):
        _run(repo, tmp_path, resource_grants=["svc"], mesh=True,
             mesh_model="not-a-mode")
    assert not stub["record"].exists()
    assert RG.claims(repo)["counts"]["total"] == 0


def test_run_agent_grant_failure_fails_closed_before_launch(
        repo, stub, tmp_path):
    _harness_grant(repo, "missing-vm")
    (repo / "ozagia" / "resources.toml").write_text(
        "[resources]\nenabled = false\n"
        '[resource_grants.missing-vm]\n'
        'kind = "vm"\nprovider = "qemu"\nimage = "/tmp/not-there.qcow2"\n'
        'cleanup = "destroy"\n', encoding="utf-8")
    with pytest.raises(AgentError, match="preflight refused"):
        _run(repo, tmp_path, resource_grants=["missing-vm"])
    assert not stub["record"].exists()
    assert RG.claims(repo)["counts"]["total"] == 0


def test_agent_cli_accepts_resource_grant_flags():
    from ozagia import cli
    args = cli.build_parser().parse_args([
        "agent", "--resource-grant", "one", "--resource-grant", "two",
        "--approve-grant", "--allow-unbounded-grant",
        "--allow-keep-grant", "--grant-reason", "host test",
    ])
    assert args.resource_grants == ["one", "two"]
    assert args.approve_grant is True
    assert args.allow_unbounded_grant is True
    assert args.allow_keep_grant is True
    assert args.grant_reason == "host test"
