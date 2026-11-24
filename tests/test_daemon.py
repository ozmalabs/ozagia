"""The graph daemon over its Unix socket (`python -m ozagia.daemon serve`)."""
from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ozagia.daemon.client import (
    DaemonClient,
    DaemonError,
    DaemonTimeout,
    DaemonUnavailable,
)
from ozagia.daemon.protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION

SRC = Path(__file__).resolve().parents[1] / "src"
READY_TIMEOUT_S = 60

MOD = '''def helper(a, b=1):
    return a + b


def other():
    return helper(1)
'''

HINT_RETRY = '''import time


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("failed")
'''

HINT_IMPORTED = '''import time
import tenacity


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
'''


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OZAGIA_")}
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _start(root: Path, sock: Path, dump: Path, journal: Path
           ) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia.daemon", "serve", "--root", str(root),
         "--sock", str(sock), "--dump", str(dump), "--journal", str(journal)],
        env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready, _, _ = select.select([proc.stdout], [], [], READY_TIMEOUT_S)
    line = proc.stdout.readline() if ready else ""
    if line.strip() != f"ready {sock}":
        proc.kill()
        raise AssertionError(f"no ready line: {line!r} {proc.stderr.read()}")
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    base = tmp_path_factory.mktemp("dmn")
    root = base / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text(MOD)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return {"base": base, "root": root, "dump": base / "graph.json"}


@pytest.fixture(scope="module")
def daemon(site):
    sock = site["base"] / "main.sock"
    journal = site["base"] / "journal.jsonl"
    proc = _start(site["root"], sock, site["dump"], journal)
    yield {"proc": proc, "sock": sock, "journal": journal, **site}
    _stop(proc)


def _raw(sock: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(sock))
    return s


def _readlines(s: socket.socket, n: int) -> list[dict]:
    buf = b""
    while buf.count(b"\n") < n:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    return [json.loads(x) for x in buf.split(b"\n") if x]


def test_ops_round_trip(daemon):
    with DaemonClient(daemon["sock"], timeout=10) as c:
        ping = c.ping()
        assert ping["ok"] and ping["version"] == PROTOCOL_VERSION
        assert ping["root"] == str(daemon["root"].resolve())
        assert ping["pid"] == daemon["proc"].pid
        assert c.hello(version="0.1.0", session="ses_1") == {"id": 2, "ok": True}
        res = c.tool("read", {"path": "pkg/mod.py"}, session="ses_1")
        assert res["ok"] and res["handled"] and not res["is_error"]
        assert res["output"].split("\n")[0].endswith("pkg/mod.py, lines 1-6")
        assert "[ozagia] sym:pkg/mod.py::helper  tests: 0" in res["output"]
        assert res["meta"]["intent"] == "read_file"
        structural = c.request("structural", input={"kind": "resolve",
                                                     "target": "helper"})
        assert structural["ok"] is True
        assert "freshness" in structural["result"]
        assert "resources" in structural["result"]
        res = c.tool("patch", {"patchText": ""})
        assert res["ok"] and res["handled"] is False
        res = c.after("shell", {"command": "true"}, "", session="ses_1")
        assert res["ok"] and res["append"] == ""
        assert res["meta"]["intent"] == "after_refresh"
        stats = c.stats()["stats"]
        assert stats["hellos"] >= 1 and "ses_1" in stats["hello_sessions"]
        assert stats["by_op"]["tool"] >= 2 and stats["by_tool"]["read"] >= 1
        assert stats["by_intent"]["read_file"] >= 1
        # A design passthrough (patch) is not a fallback (review fix 4).
        assert stats["passthrough"] >= 1 and stats["handled"]["false"] >= 1
        assert stats["fallbacks"] == 0
        assert stats["uptime_s"] >= 0 and stats["max_ms"] >= stats["mean_ms"] > 0
        assert stats["lock_wait_count"] >= 1
        assert stats["lock_wait_ms_max"] >= stats["lock_wait_ms_mean"] >= 0


def test_git_ref_commands_are_passthrough_only_for_interactive_opencode(daemon):
    with DaemonClient(daemon["sock"], timeout=10) as c:
        denied = c.request("tool", tool="shell",
                           input={"command": "git switch -c task"})
        assert denied["handled"] and denied["is_error"]
        assert "changes git refs" in denied["output"]

        preflight = c.request(
            "tool", tool="shell", harness="opencode",
            input={"command": "git switch -c task"},
            git_refs_passthrough=True, preflight=True)
        assert preflight["ok"] and preflight["handled"] is False
        assert preflight["preflight"] is True
        assert preflight["permission"]["action"] == "shell"

        delegated = c.request(
            "tool", tool="shell", harness="opencode",
            input={"command": "git switch -c task"},
            git_refs_passthrough=True)
        assert delegated["ok"] and delegated["handled"] is False
        assert "permission" not in delegated

        # This only routes the call to OpenCode's built-in shell. The daemon
        # still refuses the same operation when no interactive marker exists.
        eval_call = c.request("tool", tool="shell", harness="opencode_eval",
                              input={"command": "git commit -m x"})
        assert eval_call["handled"] and eval_call["is_error"]


def test_hint_round_trip_with_real_index(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "checkout.py").write_text(
        "def calculate_checkout_total(price, tax):\n    return price + tax\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "checkout.py"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm",
                    "fixture"], check=True)
    sock = tmp_path / "hint.sock"
    journal = tmp_path / "hint.jsonl"
    proc = _start(root, sock, tmp_path / "graph.json", journal)
    try:
        with DaemonClient(sock, timeout=30) as client:
            result = client.request(
                "hint", prompt="calculate checkout total",
                history=[{"role": "user", "text": "private pricing task"}],
                session="hint-test")
        assert result["ok"]
        assert "checkout.py" in result["context"]
        assert len(result["context"]) <= 1600
        assert "file:checkout.py" in result["meta"]["keys"]
        assert result["meta"]["token_estimate_method"] == "estimated_chars_div_4"
        assert result["meta"]["estimated_tokens"] <= 400
        assert result["meta"]["allocation"]["budget_chars"] == 1600
        assert "private pricing task" not in repr(result["meta"])
        assert "private pricing task" not in journal.read_text()
    finally:
        _stop(proc)


def test_bad_requests(daemon):
    with DaemonClient(daemon["sock"], timeout=10) as c:
        assert c.request("nope")["ok"] is False
        assert c.request("tool")["ok"] is False
    s = _raw(daemon["sock"])
    s.sendall(b"not json\n[1]\n")
    a, b = _readlines(s, 2)
    assert a["ok"] is False and "bad JSON" in a["error"]
    assert b["ok"] is False and "object" in b["error"]
    s.close()


def test_pipelined_requests_answer_in_order(daemon):
    s = _raw(daemon["sock"])
    reqs = [{"id": 10, "op": "ping"},
            {"id": 11, "op": "tool", "tool": "grep", "input": {"pattern": "helper"}},
            {"id": 12, "op": "tool", "tool": "read", "input": {"path": "pkg"}},
            {"id": 13, "op": "stats"}]
    s.sendall(b"".join(json.dumps(r).encode() + b"\n" for r in reqs))
    replies = _readlines(s, 4)
    assert [r["id"] for r in replies] == [10, 11, 12, 13]
    assert all(r["ok"] for r in replies)
    assert "[ozagia] defined: sym:pkg/mod.py::helper" in replies[1]["output"]
    s.close()


def test_two_concurrent_clients(daemon):
    errors: list[str] = []

    def work(n: int) -> None:
        try:
            with DaemonClient(daemon["sock"], timeout=20) as c:
                for _ in range(15):
                    r = c.tool("read", {"path": "pkg/mod.py", "offset": n,
                                        "limit": 1})
                    if not (r["ok"] and f"\n{n}: " in r["output"]):
                        errors.append(repr(r))
        except DaemonError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=work, args=(n,)) for n in (1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []


def test_oversized_message_is_rejected(daemon):
    s = _raw(daemon["sock"])
    payload = b"x" * (MAX_MESSAGE_BYTES + 100)

    def send() -> None:
        try:
            s.sendall(payload)
        except OSError:
            pass

    t = threading.Thread(target=send)
    t.start()
    (reply,) = _readlines(s, 1)
    t.join(10)
    assert reply["ok"] is False and "exceeds" in reply["error"]
    s.close()
    with DaemonClient(daemon["sock"], timeout=10) as c:
        assert c.ping()["ok"]


def test_journal_records_every_request(daemon):
    with DaemonClient(daemon["sock"], timeout=10) as c:
        c.tool("read", {"path": "pkg/mod.py"}, session="ses_j")
    recs = [json.loads(x) for x in
            daemon["journal"].read_text().splitlines()]
    rec = [r for r in recs if r.get("session") == "ses_j"][-1]
    assert set(rec) == {"ts", "op", "tool", "intent", "handled", "is_error",
                        "expired", "ms", "keys", "reason", "fallback",
                        "session", "harness", "files", "preflight", "ok"}
    assert rec["files"] is None
    assert rec["op"] == "tool" and rec["tool"] == "read"
    assert rec["intent"] == "read_file" and rec["handled"] is True
    assert "sym:pkg/mod.py::helper" in rec["keys"] and len(rec["keys"]) <= 20
    assert {r["op"] for r in recs} >= {"ping", "hello", "tool", "after", "stats"}


def test_cli_client_commands(daemon):
    out = subprocess.run(
        [sys.executable, "-m", "ozagia.daemon", "ping", "--sock",
         str(daemon["sock"])], env=_env(), capture_output=True, text=True,
        timeout=30)
    assert out.returncode == 0 and json.loads(out.stdout)["ok"] is True
    missing = daemon["base"] / "none.sock"
    out = subprocess.run(
        [sys.executable, "-m", "ozagia.daemon", "stats", "--sock", str(missing)],
        env=_env(), capture_output=True, text=True, timeout=30)
    assert out.returncode == 1


def test_second_daemon_on_a_live_socket_is_refused(daemon):
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia.daemon", "serve", "--root",
         str(daemon["root"]), "--sock", str(daemon["sock"]), "--dump",
         str(daemon["dump"])],
        env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 2 and "already answers" in err
    with DaemonClient(daemon["sock"], timeout=10) as c:
        assert c.ping()["ok"]


def test_stale_socket_is_replaced_and_shutdown_removes_it(site):
    sock = site["base"] / "stale.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock))
    dead.close()
    assert sock.exists()
    proc = _start(site["root"], sock, site["dump"], site["base"] / "j2.jsonl")
    try:
        with DaemonClient(sock, timeout=10) as c:
            assert c.ping()["pid"] == proc.pid
            assert c.shutdown() == {"id": 2, "ok": True}
        assert proc.wait(timeout=10) == 0
        assert not sock.exists()
    finally:
        _stop(proc)


def test_sigterm_exits_and_removes_the_socket(site):
    sock = site["base"] / "term.sock"
    proc = _start(site["root"], sock, site["dump"], site["base"] / "j3.jsonl")
    try:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert not sock.exists()
    finally:
        _stop(proc)


def test_client_errors(tmp_path_factory):
    base = tmp_path_factory.mktemp("cl")
    with pytest.raises(DaemonUnavailable):
        DaemonClient(base / "missing.sock", timeout=1).ping()
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = base / "silent.sock"
    silent.bind(str(path))
    silent.listen(1)
    try:
        c = DaemonClient(path, timeout=0.3)
        t0 = time.monotonic()
        with pytest.raises(DaemonTimeout):
            c.ping()
        assert time.monotonic() - t0 < 3
        c.close()
    finally:
        silent.close()


def test_edits_write_the_snapshot_once_on_exit(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text(MOD)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    dump = tmp_path / "graph.json"
    sock = tmp_path / "w.sock"
    proc = _start(root, sock, dump, tmp_path / "j.jsonl")
    try:
        before = dump.stat().st_mtime_ns
        with DaemonClient(sock, timeout=10) as c:
            res = c.tool("edit", {"path": "pkg/mod.py", "oldString": "return a + b",
                                  "newString": "return b + a"})
            assert res["handled"] and not res["is_error"], res
            # The graph is updated in memory, but not yet written.
            assert "~file:pkg/mod.py" in res["output"]
            assert dump.stat().st_mtime_ns == before
            c.shutdown()
        proc.wait(timeout=20)
    finally:
        _stop(proc)
    assert dump.stat().st_mtime_ns != before
    graph = json.loads(dump.read_text())
    helper = [n for n in graph["nodes"] if n.get("key") == "sym:pkg/mod.py::helper"]
    assert helper and "b + a" in (root / "pkg" / "mod.py").read_text()


# --------------------------------------------------------------------------- #
# review fixes: deadlines, counters, reserved fields, parent watch
# --------------------------------------------------------------------------- #
def test_expired_request_is_not_executed(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(MOD)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    sock = tmp_path / "x.sock"
    proc = _start(root, sock, tmp_path / "graph.json", tmp_path / "j.jsonl")
    try:
        s = _raw(sock)
        past = time.time() * 1000 - 1000
        future = time.time() * 1000 + 60_000
        edit = {"path": "pkg/mod.py", "oldString": "return a + b",
                "newString": "return a - b"}
        for i, msg in enumerate([
                {"op": "tool", "tool": "edit", "input": edit, "deadline_ms": past},
                {"op": "after", "tool": "write", "input": {}, "output": "",
                 "deadline_ms": past},
                {"op": "tool", "tool": "read", "input": {"path": "pkg/mod.py"},
                 "deadline_ms": future},
                {"op": "tool", "tool": "shell", "input": {"command": "ls"}},
                {"op": "after", "tool": "shell", "input": {"command": "ls"},
                 "output": "", "meta": {"fallback": "daemon timeout after 1 ms"}},
                {"op": "after", "tool": "edit", "input": edit, "output": "",
                 "meta": {"fallback": "unknown_outcome"}},
                {"op": "stats"}], 1):
            s.sendall((json.dumps({"id": i, **msg}) + "\n").encode())
        got = _readlines(s, 7)
        s.close()
    finally:
        _stop(proc)
    assert got[0]["handled"] is False and got[0]["expired"] is True
    assert got[0]["reason"] == "expired"
    assert got[1] == {"id": 2, "ok": True, "append": "", "expired": True,
                      "meta": got[1]["meta"]}
    assert got[2]["handled"] is True and "expired" not in got[2]
    assert "return a + b" in (root / "pkg" / "mod.py").read_text()
    st = got[6]["stats"]
    assert st["handled"] == {"true": 2, "false": 1}
    assert st["expired"] == 1 and st["after_expired"] == 1
    assert st["passthrough"] == 0
    assert st["plugin_fallbacks"] == 2 and st["unknown_outcomes"] == 1
    assert st["fallbacks"] == 2            # one per plugin fallback; expired is separate


def test_reserved_input_fields_are_replaced_by_the_daemon(tmp_path, monkeypatch):
    from ozagia import intent
    from ozagia.daemon.server import DaemonState, captured_test_env
    seen: list[dict] = []

    def fake(memory, tool, inp, **kw):
        seen.append(inp)
        return {"handled": False, "reason": "x", "meta": {}}

    monkeypatch.setattr(intent, "handle", fake)
    st = DaemonState(tmp_path, None, tmp_path / "j.jsonl", test_env={"A": "1"})
    st.dispatch({"op": "tool", "tool": "read",
                 "input": {"path": "x", "_test_env": {"EVIL": "1"},
                           "_deadline_s_remaining": 0}})
    assert seen == [{"path": "x", "_test_env": {"A": "1"}}]
    env = captured_test_env({"PATH": "/bin", "OZAGIA_ROOT": "/r", "COLUMNS": "250",
                             "PYTHONPATH": "/src", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


_SPAWN_DAEMON = r"""
import os, subprocess, sys, time
root, sock = sys.argv[1], sys.argv[2]
p = subprocess.Popen([sys.executable, "-m", "ozagia.daemon", "serve", "--root", root,
                      "--sock", sock, "--parent-pid", str(os.getpid())],
                     stdout=subprocess.PIPE, text=True, start_new_session=True)
print(p.pid, p.stdout.readline().strip(), flush=True)
time.sleep(600)
"""


def test_daemon_exits_when_its_parent_dies(tmp_path):
    from ozagia.daemon.server import PARENT_POLL_S
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(MOD)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    sock = tmp_path / "p.sock"
    parent = subprocess.Popen([sys.executable, "-c", _SPAWN_DAEMON, str(root), str(sock)],
                              env=_env(), stdout=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([parent.stdout], [], [], READY_TIMEOUT_S)
        pid_s, word, _path = parent.stdout.readline().split(" ", 2)
        assert word == "ready"
        pid = int(pid_s)
        os.kill(pid, 0)
        parent.kill()                       # a hard kill: no teardown runs
        parent.wait(timeout=10)
        deadline = time.monotonic() + PARENT_POLL_S * 5 + 5
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.2)
            except ProcessLookupError:
                alive = False
        assert not alive, "daemon outlived its parent"
        assert not sock.exists()
    finally:
        if parent.poll() is None:
            parent.kill()


def test_shell_capture_counters(tmp_path):
    from ozagia.daemon.server import DaemonState
    state = DaemonState(tmp_path, None, tmp_path / "j.jsonl")

    def shell(resp):
        state.record({"op": "tool", "tool": "shell", "input": {}},
                     {"ok": True, **resp}, 1.0)
    shell({"handled": True, "meta": {"intent": "shell_read"}})
    shell({"handled": True, "meta": {"intent": "shell_compound"}})
    shell({"handled": True, "is_error": True, "meta": {"intent": "shell_refused"}})
    shell({"handled": False, "reason": "nl option -w3 is not mapped",
           "meta": {"intent": "shell_passthrough"}})
    shell({"handled": False, "reason": "nl option -w3 is not mapped",
           "meta": {"intent": "shell_passthrough"}})
    shell({"handled": False, "reason": "expired", "expired": True, "meta": {}})
    shell({"handled": False, "preflight": True, "meta": {}})
    state.record({"op": "tool", "tool": "read"},
                 {"ok": True, "handled": False, "meta": {}}, 1.0)
    got = state.stats()["shell"]
    assert {k: got[k] for k in ("captured", "refused", "passthrough")} == {
        "captured": 2, "refused": 1, "passthrough": 2}
    assert got["capture_rate"] == 0.5
    assert got["by_intent"] == {"shell_read": 1, "shell_compound": 1}
    assert got["passthrough_reasons"] == {"nl option -w3 is not mapped": 2}


def test_daemon_stats_count_structured_reads(tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(tmp_path, None, tmp_path / "j.jsonl")
    for intent in ("shell_jq", "shell_jq", "read_structured",
                   "read_structured_outline", "read_file"):
        st.record({"op": "tool", "tool": "x"},
                  {"ok": True, "handled": True, "meta": {"intent": intent}},
                  1.0)
    stats = st.stats()
    assert stats["jq_captures"] == 2
    assert stats["structured_queries"] == 1
    assert stats["structured_outlines"] == 1


# --------------------------------------------------------------------------- #
# created-file ledger integration (`ozagia.core.files`)
# --------------------------------------------------------------------------- #
def _ledger_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(MOD, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "-q",
                    "-m", "initial"], check=True)
    return root


def test_handled_write_records_created_file(tmp_path):
    from ozagia.core import files as F
    from ozagia.core.memory import DevMemory, build_index
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="features", include_git=False)
    mem = DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)
    st = DaemonState(root, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "write", "harness": "claude",
           "session": "ses-write",
           "input": {"path": "notes.md", "content": "# Notes\n\nsecret\n",
                     "_reason": "one-off note"}}
    resp = st.dispatch(msg)
    assert resp["ok"] and resp["handled"] and not resp["is_error"], resp
    st.record(msg, resp, 1.0)
    record = F.explain(root, "notes.md")["record"]
    assert record is not None
    assert record["reason"] == "one-off note"
    assert record["created_by"]["session"] == "ses-write"
    assert record["created_by"]["harness"] == "claude"
    assert record["created_by"]["tool"] == "write"
    assert "secret" not in F.ledger_path(root).read_text(encoding="utf-8")
    stats = st.stats()
    assert stats["files_recorded"] == 1 and stats["file_ledger_errors"] == 0
    # Replacing an existing file is not recorded as a creation.
    msg2 = {**msg, "input": {"path": "notes.md", "content": "# Notes 2\n"}}
    resp2 = st.dispatch(msg2)
    assert resp2["ok"] and resp2["handled"], resp2
    st.record(msg2, resp2, 1.0)
    assert F.new_file_count(root)["count"] == 1
    assert st.stats()["files_recorded"] == 1
    # An edit with an empty oldString creates the file: recorded too.
    msg3 = {**msg, "tool": "edit",
            "input": {"path": "edit-note.md", "oldString": "",
                      "newString": "# Edit\n", "_reason": "edit note"}}
    resp3 = st.dispatch(msg3)
    assert resp3["ok"] and resp3["handled"], resp3
    st.record(msg3, resp3, 1.0)
    record = F.explain(root, "edit-note.md")["record"]
    assert record is not None and record["reason"] == "edit note"
    assert st.stats()["files_recorded"] == 2


def test_after_records_changed_paths_as_created(tmp_path):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "after-note.md").write_text("# After\n", encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "write", "harness": "claude",
           "session": "ses-after",
           "input": {"path": "after-note.md", "_reason": "after note"},
           "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    st.record(msg, resp, 1.0)
    record = F.explain(root, "after-note.md")["record"]
    assert record is not None
    assert record["reason"] == "after note"
    assert record["created_by"]["session"] == "ses-after"
    stats = st.stats()
    assert stats["file_ledger_scans"] == 1 and stats["files_recorded"] == 1
    # Turned off per request.
    (root / "skipped-note.md").write_text("# Skipped\n", encoding="utf-8")
    msg2 = {**msg, "file_ledger": False,
            "input": {"path": "skipped-note.md"}}
    resp2 = st.dispatch(msg2)
    st.record(msg2, resp2, 1.0)
    assert F.explain(root, "skipped-note.md")["record"] is None
    assert st.stats()["file_ledger_scans"] == 1


def test_after_builtin_shell_derives_ledger_paths(tmp_path):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "shell-note.md").write_text("# Shell\n", encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "shell", "harness": "claude",
           "session": "ses-shell",
           "input": {"command": "touch shell-note.md", "workdir": str(root)},
           "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    record = F.explain(root, "shell-note.md")["record"]
    assert record is not None
    assert record["created_by"]["session"] == "ses-shell"
    assert record["created_by"]["tool"] == "shell"
    stats = st.stats()
    assert stats["file_ledger_scans"] == 1 and stats["files_recorded"] == 1


def test_after_builtin_patch_derives_ledger_paths(tmp_path):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "patch-note.py").write_text("x = 1\n", encoding="utf-8")
    patch = ("*** Begin Patch\n"
             "*** Add File: patch-note.py\n"
             "+x = 1\n"
             "*** End Patch\n")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "patch", "harness": "claude",
           "session": "ses-patch-ledger",
           "input": {"patch": patch}, "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    record = F.explain(root, "patch-note.py")["record"]
    assert record is not None
    assert record["created_by"]["session"] == "ses-patch-ledger"
    assert record["created_by"]["tool"] == "patch"
    stats = st.stats()
    assert stats["file_ledger_scans"] == 1 and stats["files_recorded"] == 1


def test_after_builtin_shell_outside_path_and_cap(tmp_path, monkeypatch):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    msg = {"op": "after", "tool": "shell", "harness": "claude",
           "session": "ses-outside",
           "input": {"command": f"touch {outside}", "workdir": str(root)},
           "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    assert st.stats()["file_ledger_scans"] == 0
    assert F.explain(root, "outside.md")["record"] is None

    captured: list[str] = []

    def fake_scan(_root, **kwargs):
        captured.extend(kwargs.get("paths") or [])
        return []

    monkeypatch.setattr(F, "scan_new_files", fake_scan)
    names = " ".join(f"f{i}.md" for i in range(30))
    resp2 = st.dispatch({**msg, "input": {"command": f"touch {names}",
                                          "workdir": str(root)}})
    assert resp2["ok"], resp2
    assert len(captured) == 20
    assert captured == [f"f{i}.md" for i in range(20)]


def test_files_observe_merges_meta_and_command_paths(tmp_path):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "a.md").write_text("# A\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n", encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    st._files_observe(
        "after", "shell",
        {"command": "touch a.md b.md", "workdir": str(root)},
        {"session": "ses-merge", "harness": "claude"},
        {"meta": {"changed_paths": ["a.md"]}})
    assert F.explain(root, "a.md")["record"] is not None
    assert F.explain(root, "b.md")["record"] is not None
    assert st.stats()["files_recorded"] == 2


def test_after_refresh_notes_derived_shell_and_patch_paths(tmp_path):
    from ozagia.core.memory import DevMemory, build_index
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="features", include_git=False)
    mem = DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)
    st = DaemonState(root, mem, tmp_path / "j.jsonl")
    base = {"harness": "claude", "session": "ses-derived"}
    # `pkg/a.py` is recent, so the intent layer names it in changed_paths;
    # `pkg/b.py` is older and only the command line names it.
    (root / "pkg" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("b = 2\n", encoding="utf-8")
    os.utime(root / "pkg" / "b.py", (1, 1))
    resp = st.dispatch({**base, "op": "after", "tool": "shell",
                        "input": {"command": "touch pkg/a.py pkg/b.py",
                                  "workdir": str(root)},
                        "output": ""})
    assert resp["ok"], resp
    assert resp["meta"]["intent"] == "after_refresh"
    assert resp["meta"]["changed_paths"] == ["pkg/a.py"]
    (state,) = st.sessions.states("claude", "ses-derived")
    assert set(state.work.view().files) == {"pkg/a.py", "pkg/b.py"}
    # A built-in patch: the patch text names both files even without any
    # `meta.edited_files` (a daemon without a graph still notes the derived
    # paths in the session's working set).
    (root / "one.py").write_text("one = 1\n", encoding="utf-8")
    (root / "two.py").write_text("two = 2\n", encoding="utf-8")
    patch = ("*** Begin Patch\n"
             "*** Update File: one.py\n"
             "@@\n"
             "-one = 1\n"
             "+one = 3\n"
             "*** Update File: two.py\n"
             "@@\n"
             "-two = 2\n"
             "+two = 4\n"
             "*** End Patch\n")
    st2 = DaemonState(root, None, tmp_path / "j2.jsonl")
    state2 = st2.session_state({"harness": "claude",
                                "session": "ses-patch-derived"})
    st2._observe("after", "patch", {"patch": patch}, {},
                 {"meta": {"intent": "after_refresh"}}, state2)
    assert set(state2.work.view().files) == {"one.py", "two.py"}


def test_library_after_paths_uses_patch_paths(tmp_path):
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    patch = ("*** Begin Patch\n"
             "*** Update File: one.py\n"
             "@@\n"
             "-one = 1\n"
             "+one = 2\n"
             "*** Update File: two.py\n"
             "@@\n"
             "-two = 1\n"
             "+two = 2\n"
             "*** End Patch\n")
    assert st._library_after_paths(
        "patch", {"patch": patch}, {"edited_files": ["one.py"]}) == [
            "one.py", "two.py"]
    # Shell hints are not a supported tool: no derivation.
    assert st._library_after_paths("shell", {"command": "touch x.py"},
                                   {}) == []


def test_session_end_without_baseline_does_not_attribute(tmp_path):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "untracked.md").write_text("# Untracked\n", encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "session_event", "event": "end", "harness": "claude",
           "session": "ses-end"}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    st.record(msg, resp, 1.0)
    record = F.explain(root, "untracked.md")["record"]
    assert record is not None
    assert record["reason"] == "unknown"
    # No start baseline: the fallback records the file but claims no session.
    assert record["baseline"] == "missing"
    assert record["attribution"] is False
    assert record["created_by"]["session"] is None
    assert record["created_by"]["tool"] == "session_event"
    stats = st.stats()
    assert stats["file_ledger_scans"] == 1 and stats["files_recorded"] == 1
    assert stats["file_manifest_sessions"] == 1
    assert stats["file_manifest_errors"] == 0
    # A start baseline, then one new file: only the new file is attributed.
    msg2 = {**msg, "event": "start"}
    resp2 = st.dispatch(msg2)
    assert resp2["ok"], resp2
    st.record(msg2, resp2, 1.0)
    (root / "new.md").write_text("# New\n", encoding="utf-8")
    resp3 = st.dispatch(msg)
    st.record(msg, resp3, 1.0)
    new_record = F.explain(root, "new.md")["record"]
    assert new_record is not None
    assert new_record["created_by"]["session"] == "ses-end"
    assert new_record["attribution"] is True
    # The pre-existing untracked file keeps its unattributed fallback row.
    old_record = F.explain(root, "untracked.md")["record"]
    assert old_record["created_by"]["session"] is None
    stats = st.stats()
    assert stats["file_ledger_scans"] == 3 and stats["files_recorded"] == 2
    assert stats["file_manifest_sessions"] == 3


def test_session_manifest_error_is_counted(tmp_path, monkeypatch):
    from ozagia.core import files as F
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    st = DaemonState(root, None, tmp_path / "j.jsonl")

    def boom(*args, **kwargs):
        raise RuntimeError("manifest unavailable")

    monkeypatch.setattr(F, "session_snapshot", boom)
    msg = {"op": "session_event", "event": "start", "harness": "claude",
           "session": "ses-fail"}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    stats = st.stats()
    assert stats["file_manifest_errors"] == 1
    assert stats["file_ledger_errors"] == 1
    assert stats["file_manifest_sessions"] == 0


def test_hook_session_end_scan_records_untracked_file(tmp_path):
    from ozagia.core import files as F
    root = _ledger_repo(tmp_path)
    sock = tmp_path / "hook.sock"
    dump = tmp_path / "graph.json"
    journal = tmp_path / "journal.jsonl"
    proc = _start(root, sock, dump, journal)
    try:
        (root / "hook-note.md").write_text("# Hook note\n", encoding="utf-8")
        env = _env()
        env["OZAGIA_DAEMON_SOCK"] = str(sock)
        payload = {"session_id": "ses-hook", "cwd": str(root),
                   "hook_event_name": "SessionEnd", "reason": "other"}
        done = subprocess.run(
            [sys.executable, "-m", "ozagia.integrations.claude_code", "hook"],
            input=json.dumps(payload), capture_output=True, text=True, env=env,
            cwd=str(root), timeout=30)
        assert done.returncode == 0, done.stderr
        record = F.explain(root, "hook-note.md")["record"]
        assert record is not None
        assert record["reason"] == "unknown"
        # The hook's end arrived without a start manifest: recorded, but not
        # attributed to the session.
        assert record["baseline"] == "missing"
        assert record["created_by"]["session"] is None
        assert record["created_by"]["harness"] is None
        with DaemonClient(sock, timeout=10) as c:
            stats = c.stats()["stats"]
        assert stats["file_ledger_scans"] >= 1
        assert stats["files_recorded"] >= 1
        assert stats["file_manifest_sessions"] >= 1
    finally:
        _stop(proc)


# --------------------------------------------------------------------------- #
# library hints for built-in edits (`ozagia.core.library_hints`)
# --------------------------------------------------------------------------- #
def test_after_builtin_write_adds_library_hints_and_counters(tmp_path,
                                                             monkeypatch):
    from ozagia.daemon.server import DaemonState
    monkeypatch.setattr(
        "ozagia.core.library_hints._installed_version", lambda dist: None)
    root = _ledger_repo(tmp_path)
    (root / "pkg" / "retry_util.py").write_text(HINT_RETRY, encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "write", "harness": "claude",
           "session": "ses-builtin",
           "input": {"path": "pkg/retry_util.py"}, "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    assert "[ozagia] library hint:" in resp["append"]
    meta = resp["meta"]
    assert meta["library_hints_builtin"] >= 1
    assert meta["library_hint_builtin_files"] == 1
    st.record(msg, resp, 1.0)
    stats = st.stats()
    assert stats["library_hints_builtin"] == meta["library_hints_builtin"]
    assert stats["library_hint_builtin_files"] == 1
    assert stats["library_hints"] == 0

    # The same unchanged content is not hinted again for this session.
    resp2 = st.dispatch(msg)
    assert resp2["ok"], resp2
    assert "library hint" not in resp2["append"]
    assert "library_hints_builtin" not in resp2.get("meta", {})
    st.record(msg, resp2, 1.0)
    assert st.stats()["library_hints_builtin"] == stats["library_hints_builtin"]


def test_after_builtin_patch_adds_library_hints(tmp_path, monkeypatch):
    from ozagia.daemon.server import DaemonState
    monkeypatch.setattr(
        "ozagia.core.library_hints._installed_version", lambda dist: None)
    root = _ledger_repo(tmp_path)
    (root / "pkg" / "retry_util.py").write_text(HINT_RETRY, encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    patch = ("*** Begin Patch\n"
             "*** Update File: pkg/retry_util.py\n"
             "@@\n"
             " def _retry(fn):\n"
             "*** End Patch\n")
    msg = {"op": "after", "tool": "patch", "harness": "claude",
           "session": "ses-patch", "input": {"patch": patch},
           "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    assert "[ozagia] library hint:" in resp["append"]
    assert resp["meta"]["library_hints_builtin"] >= 1


def test_after_builtin_write_respects_env_opt_out(tmp_path, monkeypatch):
    from ozagia.daemon.server import DaemonState
    monkeypatch.setenv("OZAGIA_LIBRARY_HINTS", "0")
    root = _ledger_repo(tmp_path)
    (root / "pkg" / "retry_util.py").write_text(HINT_RETRY, encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "write", "harness": "claude",
           "session": "ses-off",
           "input": {"path": "pkg/retry_util.py"}, "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    assert "library hint" not in resp["append"]
    assert "library_hints_builtin" not in resp.get("meta", {})
    st.record(msg, resp, 1.0)
    assert st.stats()["library_hints_builtin"] == 0


def test_after_builtin_write_no_hint_when_the_package_is_imported(tmp_path):
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "pkg" / "retry_util.py").write_text(HINT_IMPORTED,
                                               encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "write", "harness": "claude",
           "session": "ses-imported",
           "input": {"path": "pkg/retry_util.py"}, "output": "", "meta": {}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    assert "library hint" not in resp["append"]
    meta = resp.get("meta") or {}
    assert "library_hints_builtin" not in meta
    assert meta.get("library_hints_suppressed", 0) >= 1


def test_handled_write_does_not_duplicate_a_library_hint(tmp_path,
                                                         monkeypatch):
    from ozagia.core.memory import DevMemory, build_index
    from ozagia.daemon.server import DaemonState
    monkeypatch.setattr(
        "ozagia.core.library_hints._installed_version", lambda dist: None)
    root = _ledger_repo(tmp_path)
    dump = tmp_path / "graph.json"
    build_index(root, dump, include_docs="features", include_git=False)
    mem = DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)
    st = DaemonState(root, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "write", "harness": "claude",
           "session": "ses-handled",
           "input": {"path": "pkg/retry_util.py", "content": HINT_RETRY}}
    resp = st.dispatch(msg)
    assert resp["ok"] and resp["handled"], resp
    assert "[ozagia] library hint:" in resp["output"]
    assert resp["meta"]["library_hints"] >= 1
    st.record(msg, resp, 1.0)

    after = {"op": "after", "tool": "write", "harness": "claude",
             "session": "ses-handled",
             "input": {"path": "pkg/retry_util.py"}, "output": "",
             "meta": {}}
    resp2 = st.dispatch(after)
    assert resp2["ok"], resp2
    assert "library hint" not in resp2["append"]
    assert "library_hints_builtin" not in resp2.get("meta", {})
    st.record(after, resp2, 1.0)
    stats = st.stats()
    assert stats["library_hints"] >= 1
    assert stats["library_hints_builtin"] == 0


def test_after_builtin_edit_failure_adds_no_hints(tmp_path):
    from ozagia.daemon.server import DaemonState
    root = _ledger_repo(tmp_path)
    (root / "pkg" / "retry_util.py").write_text(HINT_RETRY, encoding="utf-8")
    st = DaemonState(root, None, tmp_path / "j.jsonl")
    msg = {"op": "after", "tool": "edit", "harness": "claude",
           "session": "ses-miss", "input": {"path": "pkg/retry_util.py"},
           "output": "", "meta": {"builtin_error": True}}
    resp = st.dispatch(msg)
    assert resp["ok"], resp
    assert "library hint" not in resp["append"]
    assert "library_hints_builtin" not in resp.get("meta", {})


def test_secrets_op_capture_text_list_forget_providers_and_check(tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(tmp_path, None, tmp_path / "j.jsonl")
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    text = f'key = "{secret}"\nplain text\n'
    got = st.dispatch({"op": "secrets", "action": "capture_text",
                       "text": text, "harness": "opencode", "session": "s1"})
    assert got["ok"] and got["captured"] == 1 and got["withheld"] == 0
    assert secret not in json.dumps(got)
    ref = got["secrets"][0]["ref"]
    assert ref.startswith("secret://session/") and ref in got["text"]
    assert got["secrets"][0]["expires"] > got["secrets"][0]["created"]

    listed = st.dispatch({"op": "secrets", "action": "list",
                          "harness": "opencode", "session": "s1"})
    assert [item["ref"] for item in listed["secrets"]] == [ref]
    assert secret not in json.dumps(listed)

    providers = st.dispatch({"op": "secrets", "action": "providers"})
    assert providers["ok"] and providers["storage"] == "memory-only"
    assert {item["name"] for item in providers["providers"]} >= {
        "env", "file", "session"}

    checks = st.dispatch({"op": "secrets", "action": "check",
                          "refs": [ref, "secret://env/DOES_NOT_EXIST"],
                          "harness": "opencode", "session": "s1"})
    assert [item["status"] for item in checks["checks"]] == [
        "available", "missing"]
    assert secret not in json.dumps(checks)

    forgot = st.dispatch({"op": "secrets", "action": "forget", "ref": ref,
                          "harness": "opencode", "session": "s1"})
    assert forgot["ok"] and forgot["forgotten"] is True
    absent = st.dispatch({"op": "secrets", "action": "forget", "ref": ref,
                          "harness": "opencode", "session": "s1"})
    assert absent["forgotten"] is False
    stats = st.stats()
    assert stats["secret_captures"] == 1 and stats["secret_checks"] == 2
    assert stats["secret_store"]["entries"] == 0
    bad = st.dispatch({"op": "secrets", "action": "resolve",
                       "ref": ref, "harness": "opencode", "session": "s1"})
    assert bad["ok"] is False and bad["code"] == "invalid_action"
    stats = st.stats()
    assert stats["secret_errors"] == 1
    assert stats["secret_store"]["storage"] == "memory-only"


def test_secrets_session_end_clears_and_responses_redact_values(tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(tmp_path, None, tmp_path / "j.jsonl")
    value = "tool-output-secret-1234"
    got = st.dispatch({"op": "secrets", "action": "capture", "value": value,
                       "label": "cli token", "harness": "opencode",
                       "session": "s1"})
    ref = got["ref"]
    assert value not in json.dumps(got)

    redacted = st.redact_response({
        "ok": True, "output": f"out {value}", "append": f"add {value}",
        "error": f"bad {value}", "meta": {"ref": ref},
    })
    assert value not in json.dumps(redacted)
    assert redacted["meta"]["ref"] == ref
    assert "[REDACTED]" in redacted["output"]
    assert "[REDACTED]" in redacted["append"]
    assert "[REDACTED]" in redacted["error"]

    ended = st.dispatch({"op": "session_event", "event": "end",
                         "harness": "opencode", "session": "s1"})
    assert ended["ok"] and ended["secrets_cleared"] == 1
    assert len(st.secret_store) == 0
    stats = st.stats()
    assert stats["secret_captures"] == 1
    assert stats["secret_redactions"] >= 3
    assert stats["secret_store"]["entries"] == 0


def test_secrets_op_over_the_socket_never_returns_a_value(daemon):
    value = "socket-secret-value-1234"
    with DaemonClient(daemon["sock"], timeout=10) as c:
        captured = c.request("secrets", action="capture", value=value,
                             label="socket", harness="opencode",
                             session="socket-s")
        assert captured["ok"] and value not in json.dumps(captured)
        ref = captured["ref"]
        rewritten = c.request(
            "secrets", action="capture_text", harness="opencode",
            session="socket-s", text=f'api_key = "{value}"')
        assert rewritten["ok"] and value not in json.dumps(rewritten)
        assert rewritten["captured"] == 0 or rewritten["secrets"]
        listed = c.request("secrets", action="list", harness="opencode",
                           session="socket-s")
        assert value not in json.dumps(listed)
        checks = c.request("secrets", action="check", refs=[ref],
                           harness="opencode", session="socket-s")
        assert checks["checks"][0]["status"] == "available"
        assert value not in json.dumps(checks)
        c.request("secrets", action="forget", ref=ref, harness="opencode",
                  session="socket-s")
        stats = c.stats()["stats"]
        assert stats["secret_captures"] >= 1
        assert stats["secret_store"]["storage"] == "memory-only"


def test_secrets_request_lifecycle_and_cross_session(tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(tmp_path, None, tmp_path / "j.jsonl")
    value = "daemon-request-secret-1234"
    made = st.dispatch({"op": "secrets", "action": "request",
                        "label": "OpenAI API key",
                        "purpose": "the task calls the API",
                        "harness": "opencode", "session": "s1"})
    assert made["ok"] and made["state"] == "pending"
    assert made["request_id"].startswith("req_")
    assert made["ref"] is None and made["fingerprint"] is None
    assert value not in json.dumps(made)

    listed = st.dispatch({"op": "secrets", "action": "request_list",
                          "harness": "opencode", "session": "s1"})
    assert listed["count"] == 1
    assert listed["requests"][0]["state"] == "pending"

    cross = st.dispatch({"op": "secrets", "action": "request_status",
                         "request_id": made["request_id"],
                         "harness": "opencode", "session": "s2"})
    assert cross["ok"] is False and cross["code"] == "request_mismatch"
    assert "secret://" not in json.dumps(cross)

    fulfilled = st.dispatch({"op": "secrets", "action": "request_fulfill",
                             "request_id": made["request_id"], "value": value,
                             "harness": "opencode", "session": "s1",
                             "ttl_s": 60})
    assert fulfilled["ok"] and fulfilled["state"] == "fulfilled"
    assert fulfilled["ref"].startswith("secret://session/")
    assert fulfilled["fingerprint"].startswith("sha256:")
    assert value not in json.dumps(fulfilled)

    checked = st.dispatch({"op": "secrets", "action": "check",
                           "refs": [fulfilled["ref"]],
                           "harness": "opencode", "session": "s1"})
    assert checked["checks"][0]["status"] == "available"
    assert value not in json.dumps(checked)

    again = st.dispatch({"op": "secrets", "action": "request_fulfill",
                         "request_id": made["request_id"],
                         "value": "another-value-5678",
                         "harness": "opencode", "session": "s1"})
    assert again["ok"] and again["ref"] == fulfilled["ref"]
    entries = st.dispatch({"op": "secrets", "action": "list",
                           "harness": "opencode", "session": "s1"})
    assert entries["count"] == 1

    declined = st.dispatch({"op": "secrets", "action": "request",
                            "harness": "opencode", "session": "s1",
                            "label": "second"})
    ended = st.dispatch({"op": "secrets", "action": "request_decline",
                         "request_id": declined["request_id"],
                         "harness": "opencode", "session": "s1"})
    assert ended["ok"] and ended["state"] == "declined"
    assert ended["ref"] is None

    closed = st.dispatch({"op": "session_event",
                          "event": "end", "harness": "opencode",
                          "session": "s1"})
    assert closed["ok"] and closed["secret_requests_cleared"] == 2
    assert st.dispatch({"op": "secrets", "action": "request_list",
                        "harness": "opencode", "session": "s1"})["count"] == 0
    assert value not in json.dumps(st.stats())


def test_secrets_request_over_the_socket_has_no_value(daemon):
    value = "socket-request-secret-1234"
    with DaemonClient(daemon["sock"], timeout=10) as c:
        made = c.request("secrets", action="request", label="socket token",
                         purpose="socket test", harness="opencode",
                         session="socket-request-s")
        assert made["ok"] and made["state"] == "pending"
        assert value not in json.dumps(made)
        listed = c.request("secrets", action="request_list",
                           harness="opencode", session="socket-request-s")
        assert listed["count"] == 1 and value not in json.dumps(listed)
        cross = c.request("secrets", action="request_fulfill",
                          request_id=made["request_id"], value=value,
                          harness="opencode", session="other-session")
        assert cross["ok"] is False and cross["code"] == "request_mismatch"
        fulfilled = c.request("secrets", action="request_fulfill",
                              request_id=made["request_id"], value=value,
                              harness="opencode", session="socket-request-s")
        assert fulfilled["ok"] and fulfilled["ref"].startswith("secret://")
        assert value not in json.dumps(fulfilled)


def test_daemon_redacts_a_tool_response_that_echoes_a_captured_value(daemon):
    value = "tool-response-secret-1234"
    target = daemon["root"] / "leak.txt"
    target.write_text(f"line one\n{value}\nline three\n")
    with DaemonClient(daemon["sock"], timeout=10) as c:
        captured = c.request("secrets", action="capture", value=value,
                             harness="opencode", session="redact-s")
        assert captured["ok"]
        res = c.tool("read", {"path": "leak.txt"}, session="redact-s")
        assert res["ok"] and res["handled"]
        text = json.dumps(res)
        assert value not in text
        assert "[REDACTED]" in text
        assert "line one" in text and "line three" in text
