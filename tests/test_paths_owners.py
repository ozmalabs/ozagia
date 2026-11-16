"""Path resolution and cross-session ownership (W11): candidates for a
missing path (`ozagia.intent.paths`: close names, the same name in the
graph, the same path in another worktree, an absolute path into a sibling
worktree), their use in served reads and edits, `after` and the hooks;
sessions in the edit journal, live sessions and ownership warnings
(`ozagia.core.owners`), `ozagia who` and MCP `dev_who`."""
from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.core import memory as M
from ozagia.core import owners as O
from ozagia.core.memory import DevMemory, build_index
from ozagia.daemon import protocol
from ozagia.intent import paths as PA

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

MOD = "def helper(x):\n    return x + 1\n"
USE = "from pkg.mod import helper\n\n\ndef run():\n    return helper(1)\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def layout(monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(["pkg", "tests"]))
    for name in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR",
                 protocol.ENV_SOCK):
        monkeypatch.delenv(name, raising=False)
    PA.clear_cache()
    yield
    PA.clear_cache()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "main"
    _write(root / "pkg" / "__init__.py", '"""The package."""\n')
    _write(root / "pkg" / "mod.py", MOD)
    _write(root / "pkg" / "use.py", USE)
    _write(root / ".gitignore", ".ozagia/\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _memory(root: Path) -> DevMemory:
    from ozagia.config import state_path
    dump = state_path(root, "graph.json")
    build_index(root, dump, include_docs="features", include_git=False)
    return DevMemory(dump, state_path(root, "journal.jsonl"), root=root)


@pytest.fixture
def mem(repo: Path) -> DevMemory:
    return _memory(repo)


@pytest.fixture
def wt(repo: Path, tmp_path: Path) -> Path:
    """A second worktree of `repo` on branch feat/x."""
    path = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(path))
    return path


# --------------------------------------------------------------------------- #
# candidates
# --------------------------------------------------------------------------- #
def test_close_name_in_the_same_directory(repo, mem):
    got = PA.candidates(repo, "pkg/mdo.py", memory=mem)
    assert got[0].rel == "pkg/mod.py" and got[0].key == "file:pkg/mod.py"
    line = PA.suggestion_line("pkg/mdo.py", got)
    assert line.startswith("[ozagia] no such file pkg/mdo.py; did you mean: ")
    assert "pkg/mod.py (file:pkg/mod.py)" in line


def test_typo_in_a_directory_name(repo, mem):
    got = PA.candidates(repo, "pkgg/use.py", memory=mem)
    assert [c.rel for c in got][:1] == ["pkg/use.py"]


def test_same_basename_elsewhere_in_the_graph(repo, mem):
    got = PA.candidates(repo, "lib/deep/use.py", memory=mem)
    assert any(c.rel == "pkg/use.py" and c.why == "same name" for c in got)
    # Without the graph there is no basename search.
    assert not any(c.why == "same name"
                   for c in PA.candidates(repo, "lib/deep/use.py"))


def test_nothing_close_gives_no_candidates(repo, mem):
    assert PA.candidates(repo, "zzz/qqqqqq.txt", memory=mem) == []
    assert PA.suggest(repo, "pkg/mod.py", memory=mem) == ""   # it exists


def test_at_most_five_candidates(repo, mem):
    for i in range(8):
        _write(repo / "pkg" / f"mod{i}.py", "x = 1\n")
    assert len(PA.candidates(repo, "pkg/mod_.py", memory=mem)) <= 5
    assert len(PA.candidates(repo, "pkg/mod_.py", memory=mem, limit=2)) == 2


def test_worktree_list(repo, wt):
    trees = PA.worktrees(repo)
    assert [(p, b) for p, b in trees] == [(repo.resolve(), "main"),
                                          (wt.resolve(), "feat/x")]


def test_same_relative_path_in_another_worktree(repo, wt):
    # Only in the main checkout (untracked there): found from the worktree.
    _write(repo / "pkg" / "extra.py", "y = 2\n")
    got = PA.candidates(wt, "pkg/extra.py")
    assert [(c.path, c.rel, c.why) for c in got] == [
        (repo.resolve() / "pkg" / "extra.py", None, "worktree main")]
    line = PA.suggestion_line("pkg/extra.py", got)
    assert f"{repo.resolve()}/pkg/extra.py (worktree main)" in line
    # And the reverse: a file only in the worktree, asked for in main.
    _write(wt / "pkg" / "only_wt.py", "z = 3\n")
    got = PA.candidates(repo, "pkg/only_wt.py")
    assert got and got[0].why == "worktree feat/x"


def test_absolute_path_into_a_sibling_worktree(repo, wt):
    mem_wt = _memory(wt)
    raw = str(repo / "pkg" / "mod.py")       # main's file, asked in wt
    got = PA.candidates(wt, raw, memory=mem_wt)
    assert got[0].path == wt.resolve() / "pkg" / "mod.py"
    assert got[0].why == "this checkout" and got[0].key == "file:pkg/mod.py"
    # The served read refuses the path and names the one in this root.
    res = I.handle(mem_wt, "read", {"filePath": raw})
    assert res["handled"] and res["is_error"]
    assert "outside the project root" in res["output"]
    assert (f"[ozagia] no such file {raw}; did you mean: "
            f"{wt.resolve()}/pkg/mod.py (file:pkg/mod.py)") in res["output"]
    assert res["meta"]["path_suggestions"] == 1
    # A missing absolute path in the sibling: this root's file too.
    raw2 = str(repo / "pkg" / "mdo.py")
    assert PA.candidates(wt, raw2)[0].path == wt.resolve() / "pkg" / "mod.py"


def test_missing_paths_in_shell_output():
    out = "\n".join([
        "sed: can't read pkg/a.py: No such file or directory",
        "cat: pkg/b.py: No such file or directory",
        "head: cannot open 'pkg/c.py' for reading: No such file or directory",
        "ls: cannot access 'pkg/d': No such file or directory",
        "FileNotFoundError: [Errno 2] No such file or directory: 'pkg/e.py'",
    ])
    assert PA.missing_paths(out, limit=10) == [
        "pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d", "pkg/e.py"]
    assert PA.missing_paths("rg: x: No such file or directory (os error 2)") == ["x"]
    assert PA.missing_paths("all fine") == []


# --------------------------------------------------------------------------- #
# served reads and edits, after
# --------------------------------------------------------------------------- #
def test_served_read_of_a_missing_path(repo, mem):
    res = I.handle(mem, "read", {"filePath": str(repo / "pkg" / "mdo.py")})
    assert res["handled"] and res["is_error"]
    assert res["output"].splitlines()[0] == f"File not found: {repo.resolve()}/pkg/mdo.py"
    assert "did you mean: " in res["output"] and "(file:pkg/mod.py)" in res["output"]
    assert res["meta"]["intent"] == "read_missing"
    assert res["meta"]["path_suggestions"] == 1
    # No candidate: the built-in reports it.
    res = I.handle(mem, "read", {"filePath": "zzz/qqqqqq.txt"})
    assert res["handled"] is False


def test_served_edit_of_a_missing_path(repo, mem):
    res = I.handle(mem, "edit", {"filePath": "pkg/mdo.py", "oldString": "x",
                                 "newString": "y"})
    assert res["is_error"] and res["meta"]["intent"] == "edit_missing"
    assert "[ozagia] no such file pkg/mdo.py; did you mean: pkg/mod.py" in res["output"]


def test_after_failed_shell_read_names_candidates(repo, mem):
    out = I.after(mem, "shell", {"command": "sed -n '1,5p' pkg/mdo.py",
                                 "workdir": str(repo)},
                  "sed: can't read pkg/mdo.py: No such file or directory\n")
    assert "[ozagia] no such file pkg/mdo.py; did you mean: pkg/mod.py" in out["append"]
    assert out["meta"]["path_suggestions"] == 1
    # A `cd` first: paths are taken from there.
    out = I.after(mem, "shell", {"command": "cd pkg && cat mdo.py",
                                 "workdir": str(repo)},
                  "cat: mdo.py: No such file or directory\n")
    assert "no such file mdo.py; did you mean: " in out["append"]


def test_after_failed_builtin_read_names_candidates(repo, mem):
    out = I.after(mem, "read", {"path": "pkg/mdo.py"}, "File not found")
    assert "did you mean: pkg/mod.py (file:pkg/mod.py)" in out["append"]
    out = I.after(mem, "read", {"path": "pkg/mod.py"}, "1: def helper(x):")
    assert "no such file" not in out["append"]


# --------------------------------------------------------------------------- #
# ownership
# --------------------------------------------------------------------------- #
def _state(repo, mem):
    from ozagia.config import state_path
    from ozagia.daemon.server import JOURNAL_NAME, DaemonState
    return DaemonState(repo, mem, state_path(repo, JOURNAL_NAME))


def _edit(st, session, old, new, harness="opencode"):
    msg = {"op": "tool", "tool": "edit", "harness": harness, "session": session,
           "input": {"filePath": "pkg/mod.py", "oldString": old, "newString": new}}
    resp = st.dispatch(msg)
    st.record(msg, resp, 1.0)
    assert resp["handled"] and not resp["is_error"], resp
    return resp


def test_edits_carry_the_session_in_both_journals(repo, mem):
    st = _state(repo, mem)
    _edit(st, "ses_alpha_1234", "x + 1", "x + 2")
    entry = mem.journal_recent(1)[0]
    assert entry["kind"] == "span_edit"
    assert entry["session"] == {"harness": "opencode", "id": "ses_alpha_1234"}
    assert isinstance(entry["epoch"], float)
    rec = json.loads(st.journal.read_text().splitlines()[-1])
    assert rec["files"] == ["pkg/mod.py"] and rec["session"] == "ses_alpha_1234"
    assert mem.edit_session is None          # only while the edit runs


def test_ownership_warning_between_two_live_sessions(repo, mem):
    st = _state(repo, mem)
    _edit(st, "ses_alpha_1234", "x + 1", "x + 2")
    resp = _edit(st, "ses_beta", "x + 2", "x + 3", harness="claude")
    warn = [x for x in resp["output"].splitlines() if "warning:" in x]
    assert len(warn) == 1
    assert warn[0].startswith("[ozagia] warning: pkg/mod.py has uncommitted edits "
                              "by another live session (opencode ses_alph, ")
    assert warn[0].endswith("s ago)")
    assert resp["meta"]["ownership_warnings"] == 1
    # The first session's own next edit gets a warning about the second.
    resp = _edit(st, "ses_alpha_1234", "x + 3", "x + 4")
    assert "(claude ses_beta, " in resp["output"]
    assert st.stats()["ownership_warnings"] == 2
    assert st.stats()["live_sessions"] == 2


def test_no_warning_once_the_edits_are_committed(repo, mem):
    st = _state(repo, mem)
    _edit(st, "ses_alpha", "x + 1", "x + 2")
    _git(repo, "commit", "-q", "-am", "alpha's edit")
    resp = _edit(st, "ses_beta", "x + 2", "x + 3")
    assert "warning:" not in resp["output"]
    # Clean after a commit of beta's edit too: alpha's next edit is quiet.
    _git(repo, "commit", "-q", "-am", "beta's edit")
    assert "warning:" not in _edit(st, "ses_alpha", "x + 3", "x + 4")["output"]


def test_no_warning_when_the_other_session_is_stale(repo, mem):
    st = _state(repo, mem)
    _edit(st, "ses_alpha", "x + 1", "x + 2")
    st.owners.seen[("opencode", "ses_alpha")] = time.time() - O.LIVE_WINDOW_S - 60
    resp = _edit(st, "ses_beta", "x + 2", "x + 3")
    assert "warning:" not in resp["output"]


def test_liveness_survives_a_daemon_restart(repo, mem):
    st = _state(repo, mem)
    _edit(st, "ses_alpha", "x + 1", "x + 2")
    st2 = _state(repo, mem)                  # reads the daemon journal back
    assert st2.owners.live("opencode", "ses_alpha")
    assert "(opencode ses_alph" in _edit(st2, "ses_beta", "x + 2", "x + 3")["output"]


def test_builtin_edit_is_journaled_and_warned_in_pretooluse(repo, mem):
    st = _state(repo, mem)
    (repo / "pkg" / "mod.py").write_text(MOD.replace("x + 1", "x + 9"))
    msg = {"op": "after", "tool": "edit", "harness": "claude", "session": "c-sess-1",
           "input": {"path": str(repo / "pkg" / "mod.py"), "oldString": "x + 1",
                     "newString": "x + 9"}, "output": "ok"}
    resp = st.dispatch(msg)
    st.record(msg, resp, 1.0)
    assert resp["meta"]["edited_files"] == ["pkg/mod.py"]
    entry = mem.journal_recent(1)[0]
    assert entry["kind"] == "builtin_edit"
    assert entry["session"] == {"harness": "claude", "id": "c-sess-1"}
    # Another session's PreToolUse (Kiro, Codex) asks before its own edit.
    resp = st.dispatch({"op": "owner_check", "harness": "kiro", "session": "k1",
                        "paths": ["pkg/mod.py"], "cwd": str(repo)})
    assert resp["ok"] and len(resp["warnings"]) == 1
    assert "(claude c-sess-1, " in resp["warnings"][0]
    st.record({"op": "owner_check"}, resp, 1.0)
    assert st.stats()["ownership_warnings"] == 1
    # The session itself gets none.
    resp = st.dispatch({"op": "owner_check", "harness": "claude",
                        "session": "c-sess-1", "paths": ["pkg/mod.py"]})
    assert resp["warnings"] == []


def test_failed_builtin_edit_is_not_journaled(repo, mem):
    st = _state(repo, mem)
    msg = {"op": "after", "tool": "edit", "harness": "claude", "session": "c1",
           "input": {"path": "pkg/mod.py", "oldString": "nope", "newString": "x"},
           "output": "String to replace not found"}
    resp = st.dispatch(msg)
    assert "edited_files" not in resp["meta"]
    assert all(e.get("kind") != "builtin_edit" for e in mem.journal_recent(5))


def test_path_check_op_and_counter(repo, mem):
    st = _state(repo, mem)
    resp = st.dispatch({"op": "path_check", "path": "pkg/mdo.py", "cwd": str(repo)})
    assert resp["exists"] is False and "pkg/mod.py (file:pkg/mod.py)" in resp["line"]
    assert resp["candidates"][0]["rel"] == "pkg/mod.py"
    st.record({"op": "path_check"}, resp, 1.0)
    resp = st.dispatch({"op": "path_check", "path": "pkg/mod.py", "cwd": str(repo)})
    assert resp["exists"] is True and resp["line"] == ""
    msg = {"op": "tool", "tool": "read", "input": {"filePath": "pkg/mdo.py"}}
    st.record(msg, st.dispatch(msg), 1.0)
    assert st.stats()["path_suggestions"] == 2


# --------------------------------------------------------------------------- #
# who
# --------------------------------------------------------------------------- #
def test_who_report(repo, mem):
    st = _state(repo, mem)
    _edit(st, "ses_alpha", "x + 1", "x + 2")
    _edit(st, "ses_alpha", "x + 2", "x + 3")
    _edit(st, "ses_beta", "x + 3", "x + 4", harness="codex")
    resp = st.dispatch({"op": "who", "path": "pkg/mod.py"})
    rep = resp["who"]
    assert rep["path"] == "pkg/mod.py" and rep["dirty"] is True
    assert [(s["harness"], s["session"], s["edits"], s["live"], s["committed"])
            for s in rep["sessions"]] == [("codex", "ses_beta", 1, True, False),
                                          ("opencode", "ses_alpha", 2, True, False)]
    _git(repo, "commit", "-q", "-am", "both")
    rep = st.dispatch({"op": "who", "path": "pkg/mod.py"})["who"]
    assert rep["dirty"] is False and rep["last_commit"]
    assert all(s["committed"] for s in rep["sessions"])


def test_ozagia_who_cli(repo, mem, capsys, monkeypatch):
    from ozagia import cli
    st = _state(repo, mem)
    _edit(st, "ses_alpha_long_id", "x + 1", "x + 2")
    monkeypatch.chdir(repo / "pkg")
    assert cli.main(["who", "mod.py", "--root", str(repo), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["path"] == "pkg/mod.py"
    (s,) = rep["sessions"]
    assert (s["harness"], s["session"], s["live"], s["committed"]) == (
        "opencode", "ses_alpha_long_id", True, False)
    assert cli.main(["who", "mod.py", "--root", str(repo)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("pkg/mod.py: dirty, last commit ")
    assert "opencode ses_alpha_long_id: 1 edit(s)" in text and "live, uncommitted" in text
    assert cli.main(["who", "/etc/hostname", "--root", str(repo)]) == 2


def test_dev_who_mcp(repo, mem):
    from ozagia.integrations.mcp.server import DevMemoryServer
    st = _state(repo, mem)
    _edit(st, "ses_alpha", "x + 1", "x + 2")
    server = DevMemoryServer(mem)
    assert "dev_who" in {t["name"] for t in server._tools}
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "dev_who",
                                     "arguments": {"path": "pkg/mod.py"}}})
    rep = json.loads(resp["result"]["content"][0]["text"])
    assert rep["ok"] and rep["source"] == "journal" and rep["dirty"] is True
    assert rep["sessions"][0]["session"] == "ses_alpha"
    assert rep["sessions"][0]["live"] is True
    # Edits through the MCP server are journaled as its own session.
    resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "dev_span_edit", "arguments": {
                              "file": "pkg/mod.py", "old_string": "x + 2",
                              "new_string": "x + 5", "apply": True}}})
    assert mem.journal_recent(1)[0]["session"] == {"harness": "mcp",
                                                   "id": f"pid{os.getpid()}"}
    # The other session sees the MCP edit as live (this process runs).
    assert "(mcp pid" in _edit(st, "ses_beta", "x + 5", "x + 6")["output"]


# --------------------------------------------------------------------------- #
# hooks: a subprocess against a stub daemon
# --------------------------------------------------------------------------- #
class StubDaemon:
    def __init__(self, sock: Path, reply):
        self.sock, self.reply, self.requests = sock, reply, []
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
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.sock.unlink(missing_ok=True)


@pytest.fixture
def sock():
    d = Path(tempfile.mkdtemp(prefix="ozw11", dir="/tmp"))
    yield d / "d.sock"
    shutil.rmtree(d, ignore_errors=True)


WARNING = ("[ozagia] warning: pkg/mod.py has uncommitted edits by another live "
           "session (opencode ses_alph, 1m ago)")


def _stub_reply(req):
    if req["op"] == "path_check":
        return {"ok": True, "exists": False,
                "line": f"[ozagia] no such file {req['path']}; did you mean: GRAPH"}
    if req["op"] == "owner_check":
        return {"ok": True, "warnings": [WARNING]}
    return {"ok": True, "append": "", "meta": {}}


def _run(module: str, payload: dict, sock: Path, cwd: Path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_") and k != "PYTHONPATH"}
    env.update({"PYTHONPATH": str(SRC), protocol.ENV_SOCK: str(sock),
                "OZAGIA_DAEMON_AUTOSTART": "0"})
    return subprocess.run([sys.executable, "-m", module, "hook"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=40)


def _pre(repo, tool, ti, session="s1"):
    return {"hook_event_name": "PreToolUse", "cwd": str(repo), "session_id": session,
            "tool_name": tool, "tool_input": ti}


def test_claude_read_of_a_missing_path_is_denied_with_candidates(repo, sock):
    stub = StubDaemon(sock, _stub_reply)
    try:
        proc = _run("ozagia.integrations.claude_code",
                    _pre(repo, "Read", {"file_path": str(repo / "pkg" / "mdo.py")}),
                    sock, repo)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "did you mean: GRAPH" in out["permissionDecisionReason"]
        assert stub.requests[0]["op"] == "path_check"
        # An existing file: no path check, the Read runs.
        n = len(stub.requests)
        proc = _run("ozagia.integrations.claude_code",
                    _pre(repo, "Read", {"file_path": str(repo / "pkg" / "mod.py")}),
                    sock, repo)
        assert "deny" not in proc.stdout
        assert all(r["op"] != "path_check" for r in stub.requests[n:])
    finally:
        stub.close()


def test_claude_read_without_daemon_answers_locally(repo, sock):
    proc = _run("ozagia.integrations.claude_code",
                _pre(repo, "Read", {"file_path": str(repo / "pkg" / "mdo.py")}),
                sock, repo)
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert f"did you mean: {repo.resolve()}/pkg/mod.py" in reason
    # Nothing close: the Read runs (and reports its own error).
    proc = _run("ozagia.integrations.claude_code",
                _pre(repo, "Read", {"file_path": str(repo / "zzz" / "qqqq.txt")}),
                sock, repo)
    assert proc.stdout == "" and proc.returncode == 0


def test_claude_edit_gets_ownership_warning_as_context(repo, sock):
    stub = StubDaemon(sock, _stub_reply)
    try:
        proc = _run("ozagia.integrations.claude_code",
                    _pre(repo, "Edit", {"file_path": str(repo / "pkg" / "mod.py"),
                                        "old_string": "a", "new_string": "b"}),
                    sock, repo)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out == {"hookEventName": "PreToolUse", "additionalContext": WARNING}
        req = stub.requests[-1]
        assert req["op"] == "owner_check" and req["harness"] == "claude"
        assert req["paths"] == [str(repo / "pkg" / "mod.py")]
    finally:
        stub.close()


def test_kiro_read_of_a_missing_path_blocks_with_candidates(repo, sock):
    stub = StubDaemon(sock, _stub_reply)
    try:
        proc = _run("ozagia.integrations.kiro",
                    _pre(repo, "read_file", {"path": "pkg/mdo.py"}), sock, repo)
        assert proc.returncode == 2
        assert "[ozagia] no such file pkg/mdo.py; did you mean: GRAPH" in proc.stderr
        # Relative to the event's cwd: `mod.py` exists under pkg/.
        n = len(stub.requests)
        proc = _run("ozagia.integrations.kiro",
                    _pre(repo / "pkg", "read_file", {"path": "mod.py"}), sock, repo)
        assert proc.returncode == 0 and proc.stderr == ""
        assert all(r["op"] != "path_check" for r in stub.requests[n:])
    finally:
        stub.close()


def test_kiro_read_fails_open_without_candidates(repo, sock):
    def reply(req):
        if req["op"] == "path_check":
            return {"ok": True, "exists": False, "line": ""}
        return {"ok": True}
    stub = StubDaemon(sock, reply)
    try:
        proc = _run("ozagia.integrations.kiro",
                    _pre(repo, "read_file", {"path": "zzz/qqqq.txt"}), sock, repo)
        assert proc.returncode == 0 and proc.stderr == ""
    finally:
        stub.close()
    # Daemon down: the local check still finds the close name.
    proc = _run("ozagia.integrations.kiro",
                _pre(repo, "read_file", {"path": "pkg/mdo.py"}), sock, repo)
    assert proc.returncode == 2 and "did you mean: pkg/mod.py" in proc.stderr


def test_kiro_write_prints_ownership_warning(repo, sock):
    stub = StubDaemon(sock, _stub_reply)
    try:
        proc = _run("ozagia.integrations.kiro",
                    _pre(repo, "str_replace", {"path": "pkg/mod.py", "oldStr": "a",
                                               "newStr": "b"}), sock, repo)
        assert proc.returncode == 0 and proc.stdout.strip() == WARNING
        assert stub.requests[-1]["op"] == "owner_check"
        assert stub.requests[-1]["harness"] == "kiro"
    finally:
        stub.close()


def test_codex_apply_patch_gets_ownership_warning(repo, sock):
    stub = StubDaemon(sock, _stub_reply)
    patch = ("*** Begin Patch\n*** Update File: pkg/mod.py\n@@\n"
             "-    return x + 1\n+    return x + 2\n*** End Patch\n")
    try:
        proc = _run("ozagia.integrations.codex",
                    _pre(repo, "apply_patch", {"command": patch}), sock, repo)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        assert out == {"hookEventName": "PreToolUse", "additionalContext": WARNING}
        req = stub.requests[-1]
        assert req["op"] == "owner_check" and req["paths"] == ["pkg/mod.py"]
    finally:
        stub.close()


def test_codex_failed_cat_gets_candidates_from_a_real_daemon_state(repo, mem):
    # The Codex PostToolUse sends `after` for Bash; the daemon's answer.
    st = _state(repo, mem)
    resp = st.dispatch({"op": "after", "tool": "shell", "harness": "codex",
                        "session": "cx1",
                        "input": {"command": "cat pkg/mdo.py", "workdir": str(repo)},
                        "output": "cat: pkg/mdo.py: No such file or directory"})
    assert "did you mean: pkg/mod.py (file:pkg/mod.py)" in resp["append"]
