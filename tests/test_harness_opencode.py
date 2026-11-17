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

from ozagia.eval.agent import OPENCODE_GIT_DENY
from ozagia.harness import opencode as hop
from ozagia.harness import session as hs
from ozagia.harness.opencode import AgentError, git_exclude, run_agent
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
       "env": {k: os.environ.get(k) for k in ("OZAGIA_DAEMON_SOCK", "PWD", "OPENCODE_CONFIG")}}
cfg = os.environ.get("OPENCODE_CONFIG")
rec["config"] = json.load(open(cfg)) if cfg and os.path.isfile(cfg) else None
rec["plugin_present"] = os.path.isfile(os.path.join(os.getcwd(), ".opencode", "plugins", "ozagia.js"))
rec["git_status"] = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                   capture_output=True, text=True).stdout
replies = []
sock = os.environ.get("OZAGIA_DAEMON_SOCK")
if sock:
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

    assert rec["argv"][1:] == ["run", "--standalone", "--format", "json", "--auto",
                               "--archived", "-m", "vllm/test-model", "fix the addition"]
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
    bash = cfg["permission"]["bash"]
    assert bash["*"] == "allow"
    assert all(bash[p] == "deny" for p in OPENCODE_GIT_DENY)
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
    res = _run(repo, tmp_path, prompt=None, model="vllm/test-model")
    rec = _record(stub)
    assert rec["argv"][1:] == ["--standalone", str(repo)]
    assert rec["config"]["model"] == "vllm/test-model"
    assert rec["env"]["OZAGIA_DAEMON_SOCK"] == res["sock"]
    assert res["interactive"] is True and res["exit_code"] == 0
    assert res["plugin_loaded"] is True
    assert res["transcript_path"] is None
    assert not (repo / PLUGIN_REL).exists()


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

    def interrupted(argv, *, cwd, env, timeout_s, stdout, stderr):
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
    assert not s.sock.exists()
    assert _group_gone(pid)
    assert s.final_stats["by_op"].get("stats") == 1
    assert s.stats() == s.final_stats
    assert (tmp_path / "logs" / "daemon_journal.jsonl").is_file()


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
