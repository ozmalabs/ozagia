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
                        "session", "harness"}
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
