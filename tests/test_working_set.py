"""The compaction working set and the session-start brief
(`ozagia.core.working_set`): tracking in the daemon, bounds, survival across
compaction, text caps, the `working_set` op and the MCP tool."""
from __future__ import annotations

import json
import os
import subprocess
from collections import OrderedDict
from pathlib import Path

import pytest

from ozagia.config import clear_config_cache
from ozagia.core import memory as M
from ozagia.core import working_set as W
from ozagia.core.memory import DevMemory, build_index

SMALL = "def f(x):\n    return x\n\n\ndef g(y):\n    return y * 2\n"
TESTS = ("from pkg.small import f, g\n\n\ndef test_one():\n    assert f(1) == 1\n\n\n"
         "def test_two():\n    assert g(1) == 2\n")


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    for var in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    clear_config_cache()
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "small.py", SMALL)
    _write(root / "tests" / "test_a.py", TESTS)
    return root


@pytest.fixture
def mem(repo, tmp_path):
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


@pytest.fixture
def daemon(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")

    def req(msg):
        resp = st.dispatch(msg)
        st.record(msg, resp, 1.0)
        return resp
    req.state = st
    return req


FAIL_OUT = ("F.\n=== short test summary info ===\n"
            "FAILED tests/test_a.py::test_one - assert 2 == 1\n"
            "1 failed, 1 passed in 0.05s\n")
PASS_OUT = "..\n2 passed in 0.04s\n"
PYTEST = ".venv/bin/python -m pytest -q tests/test_a.py"


# --------------------------------------------------------------------------- #
# the data structure and its bounds
# --------------------------------------------------------------------------- #
def test_working_set_bounds_and_order():
    ws = W.WorkingSet()
    ws.note_edit([f"f{i}.py" for i in range(40)],
                 [f"sym:k{i}.py::f" for i in range(50)])
    ws.note_read("a.py", 1, 10)
    for i in range(30):
        ws.note_read(f"r{i}.py", 1, 5)
    v = ws.view()
    assert len(v.files) == W.MAX_FILES and len(v.keys) == W.MAX_KEYS
    assert v.keys[0] == "sym:k49.py::f"          # newest first
    assert len(v.reads) == W.MAX_READ_FILES and v.reads[0][0] == "r29.py"
    tests: OrderedDict = OrderedDict()
    run = W.TestRun("pytest", ["pytest"], [], {
        f"test:t.py::test_{i}": {"outcome": "failed", "params": [], "message": "x"}
        for i in range(25)}, complete=True)
    W.apply_test_run(tests, run)
    assert len(tests) == W.MAX_TESTS


def test_read_ranges_merge_and_stay_bounded():
    ws = W.WorkingSet()
    ws.note_read("a.py", 1, 10)
    ws.note_read("a.py", 11, 20)
    ws.note_read("a.py", 5, 8)
    assert ws.view().reads == [("a.py", [(1, 20)])]
    for a in range(100, 1000, 100):
        ws.note_read("a.py", a, a + 5)
    spans = ws.view().reads[0][1]
    assert len(spans) == W.MAX_READ_RANGES and (900, 905) in spans


def test_key_file():
    assert W.key_file("sym:pkg/a.py::f") == "pkg/a.py"
    assert W.key_file("doc:docs/x.md#intro") == "docs/x.md"
    assert W.key_file("test:tests/t.py::T.test") == "tests/t.py"
    assert W.key_file("plain") is None


# --------------------------------------------------------------------------- #
# pytest runs
# --------------------------------------------------------------------------- #
def _run(command, text, root, heads=None, workdir=None):
    heads = heads if heads is not None else [command.split()]
    return W.pytest_run(command, heads, text, workdir=workdir or root, root=root)


def test_pytest_run_parses_failures_and_selection(tmp_path):
    text = ("[ozagia] pytest failed: exit 1, 0.1 s, 1 passed, 2 failed, 0 error, 0 skipped\n"
            "[ozagia] failed test:tests/t.py::test_a[p1]: assert 1\n"
            "[ozagia] failed test:tests/t.py::test_a[p2]: assert 2\n"
            "[ozagia] error test:tests/t.py::T.test_b: boom\n")
    run = _run("python -m pytest -q tests/t.py::test_a tests/u.py", text, tmp_path)
    assert run.complete and not run.filtered and not run.stops
    assert run.selectors == ["tests/t.py::test_a", "tests/u.py"]
    assert run.failures["test:tests/t.py::test_a"]["params"] == ["[p1]", "[p2]"]
    assert run.failures["test:tests/t.py::T.test_b"]["outcome"] == "error"
    assert W.pytest_run("ls -la", [["ls", "-la"]], text, workdir=None,
                        root=tmp_path) is None
    tests: OrderedDict = OrderedDict()
    W.apply_test_run(tests, run)
    assert W.rerun_command(list(tests.items())) == (
        "python -m pytest -q tests/t.py::test_a tests/t.py::T::test_b")


def test_a_passing_run_clears_only_what_it_selected(tmp_path):
    tests: OrderedDict = OrderedDict()
    fail = ("[ozagia] failed test:tests/a.py::test_x: no\n"
            "[ozagia] failed test:tests/b.py::test_y: no\n2 failed in 0.1s\n")
    W.apply_test_run(tests, _run("pytest tests", fail, tmp_path))
    assert list(tests) == ["test:tests/a.py::test_x", "test:tests/b.py::test_y"]
    # Selects tests/a.py only, passes: test_x cleared, test_y kept.
    W.apply_test_run(tests, _run("pytest tests/a.py", "1 passed in 0.1s", tmp_path))
    assert list(tests) == ["test:tests/b.py::test_y"]
    # -k filters: a pass says nothing about the rest.
    W.apply_test_run(tests, _run("pytest -k foo", "1 passed in 0.1s", tmp_path))
    assert list(tests) == ["test:tests/b.py::test_y"]
    # No summary (killed, crashed): nothing cleared.
    W.apply_test_run(tests, _run("pytest", "Traceback...", tmp_path))
    assert list(tests) == ["test:tests/b.py::test_y"]
    # Failures pytest reports but the graph does not know: unknown.
    W.apply_test_run(tests, _run("pytest", "3 failed in 0.1s", tmp_path))
    assert list(tests) == ["test:tests/b.py::test_y"]
    # -x with a failure: the rest did not run.
    W.apply_test_run(tests, _run(
        "pytest -x", "[ozagia] failed test:tests/c.py::test_z: no\n1 failed in 0.1s",
        tmp_path))
    assert list(tests) == ["test:tests/b.py::test_y", "test:tests/c.py::test_z"]
    # A full passing run clears everything.
    W.apply_test_run(tests, _run("pytest", "9 passed in 0.3s", tmp_path))
    assert not tests


def test_selectors_and_prefix_follow_the_workdir(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    run = _run("../.venv/bin/python -m pytest t.py", "1 passed in 0.1s", tmp_path,
               heads=[["../.venv/bin/python", "-m", "pytest", "t.py"]], workdir=sub)
    assert run.selectors == ["pkg/t.py"] and run.prefix[0] == ".venv/bin/python"
    run = _run("cd pkg && pytest t.py", "1 passed in 0.1s", tmp_path,
               heads=[["cd", "pkg"], ["pytest", "t.py"]])
    assert run.selectors == ["pkg/t.py"]


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #
def _big_view() -> W.View:
    ws = W.WorkingSet()
    ws.note_edit([f"src/module_{i:02d}_{'x' * 40}.py" for i in range(30)],
                 [f"sym:src/module_{i % 20:02d}_{'x' * 40}.py::function_{i}"
                  for i in range(40)])
    for i in range(20):
        ws.note_read(f"docs/very/long/path/number_{i}_{'y' * 50}.md", 1, 400)
    ws.note_tests(W.TestRun(
        "python -m pytest -q", ["python", "-m", "pytest"], [],
        {f"test:tests/test_{i}_{'z' * 40}.py::test_case": {
            "outcome": "failed", "params": [], "message": "m" * 300}
         for i in range(15)}, complete=True))
    return ws.view()


@pytest.mark.parametrize("cap", [W.TEXT_MAX_CHARS, 3000, 800])
def test_text_is_capped_and_deterministic(cap):
    view = _big_view()
    jobs = [{"id": f"j{i}", "command": "sleep 100 " + "a" * 100, "status": "running",
             "elapsed_s": 12.3, "session": "s"} for i in range(12)]
    text = W.working_set_text(view, harness="codex", session="s", jobs=jobs,
                              plan="DevSession d1 \"task\": step 1/2", max_chars=cap)
    assert len(text) <= cap
    assert text.startswith(W.HEADER)
    assert text == W.working_set_text(view, harness="codex", session="s", jobs=jobs,
                                      plan="DevSession d1 \"task\": step 1/2",
                                      max_chars=cap)
    assert "dev_node" in text.splitlines()[1]
    if cap == W.TEXT_MAX_CHARS:
        assert "rerun (from the repo root): python -m pytest -q tests/test_" in text
        assert "job_wait job=j0" in text and "more" in text
        assert len(text) > 5000          # uses the room it has


def test_text_lines_are_actionable(tmp_path):
    ws = W.WorkingSet()
    ws.note_edit(["pkg/a.py"], ["sym:pkg/a.py::f", "sym:pkg/a.py::gone"])
    ws.note_read("pkg/b.py", 3, 9)
    text = W.working_set_text(ws.view(), harness="claude", session="s1",
                              compactions=2,
                              key_exists=lambda k: not k.endswith("gone"))
    assert "  pkg/a.py: sym:pkg/a.py::f\n" in text + "\n"
    assert "gone" not in text
    assert "  pkg/b.py lines 3-9" in text
    assert "Read takes a key" in text and "2 compaction(s)" in text
    assert W.working_set_text(W.View(), harness="claude") == ""


def test_session_brief_since_last_session(tmp_path):
    tests = [("test:tests/a.py::test_x", {"outcome": "failed", "params": [],
                                          "message": "boom", "prefix": ["pytest"],
                                          "ts": 0})]
    jobs = [{"id": "j2", "command": "make serve", "status": "running", "elapsed_s": 3}]
    text = W.session_brief(tmp_path, tests=tests, last_test={"ts": 0}, jobs=jobs,
                           jobs_enabled=False)
    lines = text.splitlines()
    assert lines[0] == W.BRIEF_HEADER and lines[-1] == W.MCP_TOOLS_LINE
    assert "  failed test:tests/a.py::test_x: boom" in lines
    assert "  rerun (from the repo root): pytest -q tests/a.py::test_x" in lines
    assert any(x.startswith("  j2 `make serve` running") for x in lines)
    short = W.session_brief(tmp_path, 300, tests=tests * 1, jobs=jobs * 20,
                            jobs_enabled=False)
    assert len(short) <= 300 and short.endswith(W.MCP_TOOLS_LINE)


def test_test_record_persists(tmp_path):
    rec = W.TestRecord(tmp_path)
    rec.apply(_run("pytest", "[ozagia] failed test:tests/a.py::test_x: no\n"
                   "1 failed in 0.1s", tmp_path))
    again = W.TestRecord(tmp_path)
    tests, last = again.view()
    assert [k for k, _ in tests] == ["test:tests/a.py::test_x"] and last["failed"] == 1
    assert (tmp_path / ".ozagia" / "working_set" / "tests.json").is_file()
    text = W.session_brief(tmp_path, jobs_enabled=False)
    assert "failed test:tests/a.py::test_x" in text


def test_devsession_line(tmp_path):
    from ozagia.core.session import DevSession
    assert W.devsession_line(tmp_path) is None
    s = DevSession.start("fix the thing", root=tmp_path)
    s.record("plan", steps=["read it", "fix it"])
    s.record("step", title="read it")
    line = W.devsession_line(tmp_path)
    assert line.startswith(f"DevSession {s.session_id} \"fix the thing\": step 1/2")
    assert "dev_session_next" in line
    s.finish("completed")
    assert W.devsession_line(tmp_path) is None


# --------------------------------------------------------------------------- #
# the daemon
# --------------------------------------------------------------------------- #
def test_daemon_tracks_edits_tests_and_reads_across_compaction(daemon, repo):
    base = {"harness": "claude", "session": "ses_w"}
    # A built-in Read the hook reports (recorded by the ledger).
    daemon({"op": "after", **base, "tool": "read",
            "input": {"path": "pkg/small.py"}, "output": SMALL})
    # A built-in edit, then its after: the refresh names the file and keys.
    (repo / "pkg" / "small.py").write_text(SMALL.replace("x\n", "x + 0\n", 1))
    resp = daemon({"op": "after", **base, "tool": "edit",
                   "input": {"path": str(repo / "pkg" / "small.py"),
                             "oldString": "x", "newString": "x + 0"},
                   "output": "ok"})
    assert resp["meta"]["intent"] == "after_refresh"
    assert resp["meta"]["changed_paths"] == ["pkg/small.py"]
    # A failing pytest run seen by after.
    daemon({"op": "after", **base, "tool": "shell", "input": {
        "command": PYTEST, "workdir": str(repo)}, "output": FAIL_OUT})
    st = daemon.state
    assert st.sessions.event("claude", "ses_w", "compact") == 1
    (state,) = st.sessions.states("claude", "ses_w")
    assert not state.files            # the ledger is cleared...
    ws = daemon({"op": "working_set", **base})
    text = ws["text"]                 # ...the working set is not
    assert text.startswith(W.HEADER) and "1 compaction(s)" in text
    # The refresh's delta keys (here the file's own) and the file.
    assert "  pkg/small.py" in text
    assert "failed test:tests/test_a.py::test_one: assert 2 == 1" in text
    assert ("rerun (from the repo root): .venv/bin/python -m pytest -q "
            "tests/test_a.py::test_one") in text
    assert "  pkg/small.py lines 1-6" in text
    assert ws["meta"]["intent"] == "working_set" and ws["meta"]["chars"] == len(text)
    # A passing run of the same file clears the failure.
    daemon({"op": "after", **base, "tool": "shell", "input": {
        "command": PYTEST, "workdir": str(repo)}, "output": PASS_OUT})
    assert "test_one" not in daemon({"op": "working_set", **base})["text"]
    stats = st.stats()
    assert stats["working_sets_served"] == 2
    assert stats["working_set_chars"] >= len(text)


def test_daemon_handled_edit_and_latest_session(daemon, repo):
    resp = daemon({"op": "tool", "harness": "opencode", "session": "ses_o",
                   "tool": "edit", "input": {"path": "pkg/small.py",
                                             "oldString": "y * 2", "newString": "y * 3"}})
    assert resp["handled"] and not resp["is_error"]
    # No session: the most recently active one (documented for MCP).
    got = daemon({"op": "working_set"})
    assert got["meta"]["resolved"] == "latest" and got["meta"]["session"] == "ses_o"
    assert "pkg/small.py" in got["text"] and "read takes a key" in got["text"]
    assert daemon({"op": "working_set", "harness": "codex"})["text"] == ""
    assert daemon({"op": "working_set", "harness": "opencode",
                   "session": "nope"})["meta"]["intent"] == "working_set_empty"
    # `end` drops it with the session.
    daemon({"op": "session_event", "harness": "opencode", "session": "ses_o",
            "event": "end"})
    assert daemon({"op": "working_set", "harness": "opencode",
                   "session": "ses_o"})["text"] == ""


def test_daemon_working_set_lists_the_sessions_jobs(daemon, repo):
    (repo / ".ozagia.toml").write_text("[jobs]\nenabled = true\n")
    clear_config_cache()
    st = daemon.state
    st.jobs.start(command="sleep 30", session="ses_j", harness="kiro")
    try:
        daemon({"op": "after", "harness": "kiro", "session": "ses_j", "tool": "read",
                "input": {"path": "pkg/small.py"}, "output": SMALL})
        text = daemon({"op": "working_set", "harness": "kiro", "session": "ses_j",
                       "max_chars": 3000})["text"]
        assert "running jobs:" in text and "`sleep 30` running" in text
        assert len(text) <= 3000
        brief = daemon({"op": "working_set", "since": True})
        assert brief["text"].startswith(W.BRIEF_HEADER) and "`sleep 30`" in brief["text"]
        assert st.stats()["session_briefs_served"] == 1
    finally:
        st.jobs.shutdown(0.5)


def test_daemon_brief_carries_the_roots_failing_tests(daemon, repo):
    daemon({"op": "after", "harness": "codex", "session": "a", "tool": "shell",
            "input": {"command": PYTEST, "workdir": str(repo)}, "output": FAIL_OUT})
    brief = daemon({"op": "working_set", "since": True})["text"]
    assert "failed test:tests/test_a.py::test_one" in brief
    assert len(brief) <= W.BRIEF_MAX_CHARS


def test_daemon_observe_never_breaks_a_request(daemon, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(W, "pytest_run", boom)
    resp = daemon({"op": "after", "harness": "claude", "session": "s", "tool": "shell",
                   "input": {"command": "pytest"}, "output": PASS_OUT})
    assert resp["ok"]


# --------------------------------------------------------------------------- #
# the MCP tool
# --------------------------------------------------------------------------- #
def test_mcp_dev_working_set(monkeypatch, tmp_path):
    from ozagia.daemon import jobcli
    from ozagia.integrations.mcp import server as S
    sent = []

    def fake(root, op, fields, **k):
        sent.append((op, fields))
        return {"id": 1, "ok": True, "text": "" if fields.get("session") == "x"
                else "[ozagia] working set ...", "meta": {}}
    monkeypatch.setattr(jobcli, "request", fake)
    srv = S.DevMemoryServer.__new__(S.DevMemoryServer)
    srv.workspace = tmp_path
    assert srv._working_set({})["text"].startswith("[ozagia] working set")
    assert srv._working_set({"session": "x", "harness": "codex"})["text"].startswith(
        "[ozagia] no working set for session x")
    srv._working_set({"since": True})
    assert sent == [("working_set", {}),
                    ("working_set", {"session": "x", "harness": "codex"}),
                    ("working_set", {"since": True})]
    assert any(t["name"] == "dev_working_set" for t in S._TOOLS)


# --------------------------------------------------------------------------- #
# a real daemon and the hooks as subprocesses
# --------------------------------------------------------------------------- #
@pytest.fixture
def live(repo, mem, tmp_path, monkeypatch):
    """A daemon serving `repo` on a short socket path, in a thread."""
    import tempfile
    import threading

    from ozagia.daemon import server as srv
    sockdir = Path(tempfile.mkdtemp(prefix="ozws", dir="/tmp"))
    sock = sockdir / "d.sock"
    monkeypatch.setenv("OZAGIA_DAEMON_SOCK", str(sock))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    state = srv.DaemonState(repo.resolve(), mem, tmp_path / "dj.jsonl")
    server = srv._Server(str(sock), state)
    state.server = server
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield sock, state
    server.shutdown()
    server.server_close()
    sock.unlink(missing_ok=True)
    sockdir.rmdir()


def _hook(module: str, payload: dict, sock: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OZAGIA_")}
    env.update(OZAGIA_DAEMON_SOCK=str(sock), OZAGIA_DAEMON_AUTOSTART="0")
    return subprocess.run([os.sys.executable, "-m", module, "hook"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=60)


def _seed(state, harness, session, sub=None):
    st = state.sessions.get(harness, session, sub)
    st.work.note_edit(["pkg/small.py"], ["sym:pkg/small.py::f"])
    return st


def test_codex_session_start_compact_returns_the_working_set(live, repo):
    sock, state = live
    _seed(state, "codex", "thr", "/x/rollout.jsonl")
    payload = {"session_id": "thr", "cwd": str(repo), "hook_event_name": "SessionStart",
               "source": "compact", "transcript_path": "/x/rollout.jsonl"}
    proc = _hook("ozagia.integrations.codex", payload, sock)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    text = out["additionalContext"]
    assert text.startswith(W.HEADER) and "sym:pkg/small.py::f" in text
    assert "dev_node" in text and len(text) <= W.TEXT_MAX_CHARS
    assert state.stats()["working_sets_served"] == 1
    # A startup gets the brief.
    proc = _hook("ozagia.integrations.codex", {**payload, "source": "startup"}, sock)
    assert json.loads(proc.stdout)["hookSpecificOutput"][
        "additionalContext"].startswith(W.BRIEF_HEADER)


def test_claude_session_start_compact_returns_the_working_set(live, repo):
    sock, state = live
    _seed(state, "claude", "abc")
    payload = {"session_id": "abc", "cwd": str(repo), "hook_event_name": "SessionStart",
               "source": "compact"}
    proc = _hook("ozagia.integrations.claude_code", payload, sock)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "pkg/small.py" in out["additionalContext"]
    # The ledger was reset after, and the working set kept.
    assert state.sessions.states("claude", "abc")[0].compactions == 1
    proc = _hook("ozagia.integrations.claude_code", {**payload, "source": "startup"}, sock)
    assert proc.stdout == ""


def test_kiro_post_compact_prints_the_working_set(live, repo):
    sock, state = live
    st = _seed(state, "kiro", "k1")
    for i in range(40):
        st.work.note_read(f"pkg/file_{i}_{'q' * 60}.py", 1, 50)
    payload = {"session_id": "k1", "cwd": str(repo), "hook_event_name": "PostCompact",
               "summary": "s"}
    proc = _hook("ozagia.integrations.kiro", payload, sock)
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.startswith(W.HEADER) and "read_file takes a key" in proc.stdout
    assert len(proc.stdout.rstrip("\n")) <= 3000
    assert st.compactions == 1
    # PreCompact: nothing, exit 0 (never cancels the compaction).
    proc = _hook("ozagia.integrations.kiro", {**payload, "hook_event_name": "PreCompact"},
                 sock)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""


def test_hooks_without_a_daemon_print_no_working_set(repo, tmp_path):
    sock = tmp_path / "missing.sock"
    payload = {"session_id": "k1", "cwd": str(repo), "hook_event_name": "PostCompact"}
    proc = _hook("ozagia.integrations.kiro", payload, sock)
    assert proc.returncode == 0 and proc.stdout == ""
    proc = _hook("ozagia.integrations.claude_code",
                 {**payload, "hook_event_name": "SessionStart", "source": "compact"}, sock)
    assert proc.returncode == 0 and proc.stdout == ""


def test_raw_pytest_summary_lines_name_the_test(tmp_path):
    got = W.parse_failures("FAILED tests/t.py::T::test_b[x] - boom\n"
                           "ERROR ../out.py::test_c\n", tmp_path, tmp_path)
    assert got == {"test:tests/t.py::T.test_b": {"outcome": "failed",
                                                 "params": ["[x]"], "message": "boom"}}
    assert W.parse_failures("FAILED tests/t.py::test_b") == {}


# --------------------------------------------------------------------------- #
# OpenCode: the compaction hook and the next context hook
# --------------------------------------------------------------------------- #
OPENCODE = r"""
import * as mod from "./plugin.mjs";
const hooks = {};
const ctx = { session: { hook: async (name, fn) => { hooks[name] = fn; } } };
await mod.createPlugin({}).setup(ctx);
const ev = (system) => ({ sessionID: "ses_c", system, messages: [], options: {} });
const summary = ev([]);
await hooks.compaction(summary);
const ctx1 = { ...ev([]), messages: [{ role: "user", content: [{ type: "text", text: "go on" }] }] };
await hooks.context(ctx1);
const ctx2 = { ...ev([]), messages: ctx1.messages };
await hooks.context(ctx2);
process.stdout.write(JSON.stringify({ summary: summary.system, ctx1: ctx1.system,
                                      ctx2: ctx2.system }) + "\n");
"""


class _JsStub:
    def __init__(self, path: Path, text: str):
        import socket
        import threading
        self.requests: list[dict] = []
        self.text = text
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as f:
                for line in f:
                    req = json.loads(line)
                    self.requests.append(req)
                    resp = {"id": req["id"], "ok": True}
                    if req["op"] == "working_set":
                        resp["text"] = self.text
                    if req["op"] == "hint":
                        resp["context"] = ""
                    f.write(json.dumps(resp).encode() + b"\n")
                    f.flush()


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node not installed")
def test_opencode_compaction_keeps_the_working_set(tmp_path):
    from ozagia.integrations.opencode import install as oc
    plugin = Path(oc.__file__).with_name("plugin.js")
    (tmp_path / "plugin.mjs").write_text(plugin.read_text(encoding="utf-8"))
    (tmp_path / "h.mjs").write_text(OPENCODE)
    stub = _JsStub(tmp_path / "d.sock", "[ozagia] working set: X")
    try:
        env = dict(os.environ, OZAGIA_DAEMON_SOCK=str(tmp_path / "d.sock"))
        proc = subprocess.run(["node", str(tmp_path / "h.mjs")], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)
        # The summary request keeps it, and the next request gets it once.
        assert len(got["summary"]) == 1 and "Keep the following in the summary" in got[
            "summary"][0]["text"] and "[ozagia] working set: X" in got["summary"][0]["text"]
        assert any("[ozagia working set]" in p["text"] for p in got["ctx1"])
        assert not any("[ozagia working set]" in p["text"] for p in got["ctx2"])
        ops = [r["op"] for r in stub.requests if r["op"] != "hello"]
        assert ops[:2] == ["working_set", "session_event"]
        ws = [r for r in stub.requests if r["op"] == "working_set"]
        assert all(r["session"] == "ses_c" and r["harness"] == "opencode" for r in ws)
    finally:
        stub.server.close()


def test_an_affected_subset_run_clears_nothing_else(tmp_path):
    tests: OrderedDict = OrderedDict()
    W.apply_test_run(tests, _run("pytest", "[ozagia] failed test:tests/a.py::test_x: no\n"
                                 "1 failed in 0.1s", tmp_path))
    text = ("[ozagia] pytest passed: exit 0, 0.1 s, 2 passed, 0 failed, 0 error, 0 skipped\n"
            "[ozagia] ran 2 affected tests of 40; not run: ...; run all: pytest\n")
    W.apply_test_run(tests, _run("pytest", text, tmp_path))
    assert list(tests) == ["test:tests/a.py::test_x"]
    W.apply_test_run(tests, _run("pytest", "[ozagia] pytest not run: every selected "
                                 "test has a cached pass (exit 0)", tmp_path))
    assert list(tests) == ["test:tests/a.py::test_x"]
