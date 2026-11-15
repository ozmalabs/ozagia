"""tests/test_opencode_plugin.py -- the OpenCode plugin, driven by Node against a stub daemon.

Each test writes a small Node harness into tmp, imports a copy of plugin.js,
builds a stub OpenCode `ctx` whose tool registry holds fake built-ins, runs
`setup`, and calls the replaced `execute`s. The daemon is a Python thread
speaking the JSON-lines protocol of ozagia.daemon.protocol, scripted per test.
No model is called. The one test that runs the real `opencode` only runs
`opencode plugin list`.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from ozagia.integrations.opencode import install as oc

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

HARNESS = r"""
import * as mod from "./plugin.mjs";
const cfg = JSON.parse(process.env.HARNESS_CFG);
const schema = { kind: "string-schema" };
const calls = [];
const fileResult = (text) => ({
  output: { type: "file", uri: "file:///w/x.py", name: "x.py", content: text,
            encoding: "utf-8", mime: "text/x-python" },
  content: [{ type: "text", text }], metadata: { lines: 1 } });
const tools = {
  read: { name: "read", description: "Read a file.", output: { kind: "read" },
          execute: async (input) => { calls.push(["read", input]); return fileResult("ORIG READ"); } },
  grep: { name: "grep", description: "Search.", output: { kind: "grep" },
          execute: async (input) => { calls.push(["grep", input]); return { output: { matches: 2 } }; } },
  edit: { name: "edit", description: "Edit a file.", output: schema,
          options: { codemode: false, permission: "edit" },
          execute: async (input) => { calls.push(["edit", input]); return "ORIG EDIT"; } },
  shell: { name: "shell", description: "Run a command.", output: { kind: "shell" },
           execute: async (input) => { calls.push(["shell", input]);
             return { output: "ORIG SHELL", content: [{ type: "text", text: "ORIG SHELL" }] }; } },
  patch: { name: "patch", description: "Apply a patch.", output: { kind: "patch" },
           execute: async (input) => { calls.push(["patch", input]); return "ORIG PATCH"; } },
};
const original = Object.fromEntries(Object.entries(tools).map(([k, t]) => [k, t.execute]));
const reg = {
  get: (name) => tools[name] && { ...tools[name] },
  update: (name, fn) => { const t = { ...tools[name] }; const r = fn(t); tools[name] = r || t; },
};
const plugin = cfg.timeoutMs ? mod.createPlugin({ requestTimeoutMs: cfg.timeoutMs }) : mod.default;
const asserts = [];
const permission = cfg.permission === undefined ? undefined : {
  assert: async (req) => {
    asserts.push(req);
    if (cfg.permission === "deny") {
      throw Object.assign(new Error("Permission denied: " + req.action),
                          { _tag: "Permission.BlockedError" });
    }
    if (cfg.permission === "declined") {
      throw Object.assign(new Error(""), { _tag: "Permission.DeclinedError" });
    }
    if (cfg.permission === "broken") throw new Error("ParseError: Expected string");
    if (cfg.permission === "transport") throw new Error("connection rejected by peer");
  },
};
await plugin.setup({ tool: { transform: (fn) => fn(reg), hook: () => {} },
                     ...(permission ? { permission } : {}) });
const results = [];
const c = { sessionID: "ses_test", agent: "build", messageID: "msg_1", id: "call_1",
            progress: () => {}, signal: undefined };
for (const call of cfg.calls || []) {
  try { results.push({ ok: true, result: await tools[call.tool].execute(call.input, c) }); }
  catch (err) { results.push({ ok: false, error: String(err && err.message) }); }
}
await new Promise((r) => setTimeout(r, cfg.settleMs || 50));
process.stdout.write(JSON.stringify({
  id: mod.default.id, has_setup: typeof mod.default.setup === "function",
  results, calls, asserts,
  replaced: Object.keys(tools).filter((k) => tools[k].execute !== original[k]),
  schema_swapped: Object.fromEntries(Object.keys(tools).map((k) => [k, tools[k].output === schema])),
  descriptions: Object.fromEntries(Object.keys(tools).map((k) => [k, tools[k].description])),
}) + "\n");
process.exit(0);
"""


# Two tool calls at once, as OpenCode runs the calls of one step.
PARALLEL = r"""
import * as mod from "./plugin.mjs";
const tools = {
  edit: { name: "edit", description: "", output: { kind: "s" },
          execute: async () => "ORIG EDIT" },
  shell: { name: "shell", description: "", output: { kind: "x" },
           execute: async () => "ORIG SHELL" },
};
const reg = { get: (n) => tools[n] && { ...tools[n] },
              update: (n, fn) => { const t = { ...tools[n] }; tools[n] = fn(t) || t; } };
await mod.default.setup({ tool: { transform: (fn) => fn(reg) } });
const c = { sessionID: "ses_par" };
const timed = async (p) => { const t0 = Date.now(); const result = await p;
                             return { result, ms: Date.now() - t0 }; };
const shell = timed(tools.shell.execute({ command: "pytest -q" }, c));
await new Promise((r) => setTimeout(r, 100));
const edit = await timed(tools.edit.execute({ path: "x.py", oldString: "a", newString: "b" }, c));
process.stdout.write(JSON.stringify({ edit, shell: await shell }) + "\n");
process.exit(0);
"""


class StubDaemon:
    """A Unix-socket server answering each request with `script(request)`.

    `script` returns the response fields (without `id`), or None to send no
    reply. A dict may carry `_delay` (seconds) before the reply."""

    def __init__(self, path: Path, script):
        self.path = path
        self.script = script
        self.requests: list[dict] = []
        self.connections = 0
        self.conns: list[threading.Thread] = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            t = threading.Thread(target=self._conn, args=(conn,), daemon=True)
            self.conns.append(t)
            t.start()

    def _conn(self, conn: socket.socket) -> None:
        self.connections += 1
        try:
            self._serve_conn(conn)
        except OSError:  # the plugin closed its end first
            pass

    def _serve_conn(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as fh:
            for line in fh:
                req = json.loads(line)
                self.requests.append(req)
                resp = self.script(req)
                if resp is None:
                    continue
                resp = dict(resp)
                time.sleep(resp.pop("_delay", 0))
                try:
                    fh.write((json.dumps({"id": req["id"], **resp}) + "\n").encode())
                    fh.flush()
                except OSError:  # the plugin gave up and closed its connection
                    return

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def close(self) -> None:
        # shutdown wakes the blocked accept(); close alone does not on Linux.
        try:
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.srv.close()
        self.thread.join(timeout=5)
        for t in self.conns:
            t.join(timeout=5)


@pytest.fixture()
def sockdir():
    # Unix socket paths are limited to about 100 bytes; tmp_path can be longer.
    path = Path(tempfile.mkdtemp(prefix="ozp-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _run(tmp_path: Path, cfg: dict, sock: Path | None) -> dict:
    (tmp_path / "plugin.mjs").write_text(oc.plugin_source(), encoding="utf-8")
    (tmp_path / "harness.mjs").write_text(HARNESS, encoding="utf-8")
    env = dict(os.environ, HARNESS_CFG=json.dumps(cfg))
    env.pop("OZAGIA_DAEMON_SOCK", None)
    if sock is not None:
        env["OZAGIA_DAEMON_SOCK"] = str(sock)
    proc = subprocess.run([NODE, str(tmp_path / "harness.mjs")], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _standard(req: dict):
    if req["op"] == "hello":
        return {"ok": True}
    if req["op"] == "tool":
        return {"ok": True, "handled": True, "output": f"GRAPH {req['tool']}",
                "meta": {"intent": "understand"}}
    if req["op"] == "after":
        return {"ok": True, "append": ""}
    return {"ok": False, "error": "unknown op"}


@pytest.fixture()
def daemon(sockdir):
    holder: list[StubDaemon] = []

    def start(script=_standard) -> StubDaemon:
        d = StubDaemon(sockdir / "d.sock", script)
        holder.append(d)
        return d

    yield start
    for d in holder:
        d.close()


READ = {"tool": "read", "input": {"path": "pkg/mod.py"}}


# --------------------------------------------------------------------------- #
@needs_node
def test_without_the_socket_env_nothing_is_replaced(tmp_path):
    out = _run(tmp_path, {"calls": [READ]}, None)
    assert out["replaced"] == []
    assert out["descriptions"]["read"] == "Read a file."
    assert out["schema_swapped"]["read"] is False
    assert out["results"][0]["result"]["content"][0]["text"] == "ORIG READ"


@needs_node
def test_default_export_has_the_plugin_shape(tmp_path):
    out = _run(tmp_path, {}, None)
    assert out["id"] == "ozagia" and out["has_setup"] is True


@needs_node
def test_handled_call_returns_the_daemon_output(tmp_path, daemon):
    d = daemon()
    out = _run(tmp_path, {"calls": [READ]}, d.path)
    res = out["results"][0]
    assert res["ok"] is True
    assert res["result"] == {"output": "GRAPH read",
                             "content": [{"type": "text", "text": "GRAPH read"}]}
    assert out["calls"] == []  # the built-in did not run
    tool = [r for r in d.requests if r["op"] == "tool"][0]
    assert tool["input"] == {"path": "pkg/mod.py"}
    assert tool["harness"] == "opencode" and tool["session"] == "ses_test"
    assert tool["tool"] == "read"


@needs_node
def test_is_error_throws_with_the_daemon_message(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return {"ok": True, "handled": True, "is_error": True,
                    "output": "oldString not found in pkg/mod.py"}
        return _standard(req)

    d = daemon(script)
    edit = {"tool": "edit", "input": {"path": "pkg/mod.py", "oldString": "a", "newString": "b"}}
    out = _run(tmp_path, {"calls": [edit]}, d.path)
    assert out["results"][0] == {"ok": False, "error": "oldString not found in pkg/mod.py"}
    assert out["calls"] == []


@needs_node
def test_unhandled_call_runs_the_original_and_appends_after(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return {"ok": True, "handled": False, "reason": "not mapped"}
        if req["op"] == "after":
            return {"ok": True, "append": f"[ozagia] after {req['tool']}"}
        return _standard(req)

    d = daemon(script)
    grep = {"tool": "grep", "input": {"pattern": "add"}}
    out = _run(tmp_path, {"calls": [READ, grep]}, d.path)
    read_res, grep_res = (r["result"] for r in out["results"])
    # The structured read result is converted to its text parts.
    assert read_res["output"] == "ORIG READ\n[ozagia] after read"
    assert read_res["content"] == [{"type": "text", "text": read_res["output"]}]
    # A result without text parts falls back to its JSON-encoded output.
    assert grep_res["output"] == '{"matches":2}\n[ozagia] after grep'
    assert [c[0] for c in out["calls"]] == ["read", "grep"]
    afters = [r for r in d.requests if r["op"] == "after"]
    assert [a["output"] for a in afters] == ["ORIG READ", '{"matches":2}']
    assert afters[0]["input"] == {"path": "pkg/mod.py"} and "meta" not in afters[0]


@needs_node
def test_daemon_down_falls_back_to_the_original(tmp_path, sockdir):
    out = _run(tmp_path, {"calls": [READ, {"tool": "shell", "input": {"command": "ls"}}]},
               sockdir / "missing.sock")
    assert [r["ok"] for r in out["results"]] == [True, True]
    assert out["results"][0]["result"]["output"] == "ORIG READ"
    assert out["results"][1]["result"]["output"] == "ORIG SHELL"
    assert sorted(out["replaced"]) == ["edit", "grep", "read", "shell"]


@needs_node
def test_daemon_timeout_falls_back_and_reports_it(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return None  # never answers
        return _standard(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [READ], "timeoutMs": 300, "settleMs": 300}, d.path)
    assert out["results"][0]["result"]["output"] == "ORIG READ"  # nothing appended
    afters = [r for r in d.requests if r["op"] == "after"]
    assert len(afters) == 1
    assert "timeout" in afters[0]["meta"]["fallback"]
    assert afters[0]["output"] == "ORIG READ"


@needs_node
def test_daemon_error_response_falls_back(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return {"ok": False, "error": "mapping raised"}
        return _standard(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [READ], "settleMs": 200}, d.path)
    assert out["results"][0]["result"]["output"] == "ORIG READ"
    afters = [r for r in d.requests if r["op"] == "after"]
    assert "mapping raised" in afters[0]["meta"]["fallback"]


@needs_node
def test_shell_request_timeout_follows_the_command_timeout(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return {"ok": True, "handled": True, "output": f"GRAPH {req['tool']}",
                    "_delay": 0.6}
        return _standard(req)

    d = daemon(script)
    shell = {"tool": "shell", "input": {"command": "pytest -q", "timeout": 1000}}
    out = _run(tmp_path, {"calls": [READ, shell], "timeoutMs": 300, "settleMs": 700}, d.path)
    assert out["results"][0]["result"]["output"] == "ORIG READ"  # 0.6 s > 0.3 s
    assert out["results"][1]["result"]["output"] == "GRAPH shell"  # within 1.0 + 0.3 s


@needs_node
def test_hello_is_sent_once_on_its_own_connection(tmp_path, daemon):
    d = daemon()
    out = _run(tmp_path, {"calls": [READ, READ, {"tool": "grep", "input": {"pattern": "x"}}]},
               d.path)
    assert all(r["ok"] for r in out["results"])
    assert sorted(d.ops()) == ["hello", "hello", "tool", "tool", "tool"]
    setup, session = [r for r in d.requests if r["op"] == "hello"]
    assert setup["phase"] == "setup" and "session" not in setup
    assert session["phase"] == "session" and session["session"] == "ses_test"
    for hello in (setup, session):
        assert hello["harness"] == "opencode" and hello["plugin"] == "ozagia"
        assert hello["version"] and hello["permission_assert"] is False
    assert session["tools"]["edit"] == {"codemode": False}
    assert session["tools"]["read"] == {"codemode": None}
    # review fix 1b: one connection per request, hello on its own.
    assert d.connections == 5


@needs_node
def test_every_request_carries_a_deadline(tmp_path, daemon):
    d = daemon()
    t0 = time.time() * 1000
    shell = {"tool": "shell", "input": {"command": "pytest", "timeout": 5000}}
    _run(tmp_path, {"calls": [READ, shell], "timeoutMs": 2000}, d.path)
    t1 = time.time() * 1000
    tools = [r for r in d.requests if r["op"] == "tool"]
    read, sh = tools
    assert t0 + 2000 <= read["deadline_ms"] <= t1 + 2000
    assert t0 + 7000 <= sh["deadline_ms"] <= t1 + 7000


@needs_node
def test_a_slow_call_does_not_queue_the_next(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool" and req["tool"] == "shell":
            return {"ok": True, "handled": True, "output": "SLOW", "_delay": 2.0}
        return _standard(req)

    d = daemon(script)
    (tmp_path / "plugin.mjs").write_text(oc.plugin_source(), encoding="utf-8")
    (tmp_path / "par.mjs").write_text(PARALLEL, encoding="utf-8")
    env = dict(os.environ, OZAGIA_DAEMON_SOCK=str(d.path))
    proc = subprocess.run([NODE, str(tmp_path / "par.mjs")], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["edit"]["result"] == {"output": "GRAPH edit",
                                     "content": [{"type": "text", "text": "GRAPH edit"}]}
    assert out["edit"]["ms"] < 1500  # not behind the 2 s shell call
    assert out["shell"]["result"]["output"] == "SLOW"


@needs_node
@pytest.mark.parametrize("tool,inp", [
    ("edit", {"path": "pkg/mod.py", "oldString": "a", "newString": "b"}),
    ("shell", {"command": "pytest -q", "timeout": 100}),
])
def test_sent_mutating_call_that_times_out_is_not_rerun(tmp_path, daemon, tool, inp):
    def script(req):
        if req["op"] == "tool":
            return None  # never answers
        return _standard(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [{"tool": tool, "input": inp}], "timeoutMs": 300,
                          "settleMs": 300}, d.path)
    res = out["results"][0]
    assert res["ok"] is False
    assert "may or may not have been applied" in res["error"]
    assert out["calls"] == []  # the built-in did not run
    afters = [r for r in d.requests if r["op"] == "after"]
    assert len(afters) == 1 and afters[0]["meta"]["fallback"] == "unknown_outcome"


@needs_node
def test_mutating_call_with_daemon_down_runs_the_builtin(tmp_path, sockdir):
    edit = {"tool": "edit", "input": {"path": "pkg/mod.py", "oldString": "a", "newString": "b"}}
    out = _run(tmp_path, {"calls": [edit]}, sockdir / "missing.sock")
    assert out["results"][0] == {"ok": True, "result": {
        "output": "ORIG EDIT", "content": [{"type": "text", "text": "ORIG EDIT"}]}}
    assert [c[0] for c in out["calls"]] == ["edit"]


@needs_node
def test_expired_answer_runs_the_builtin(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return {"ok": True, "handled": False, "reason": "expired", "expired": True}
        return _standard(req)

    d = daemon(script)
    edit = {"tool": "edit", "input": {"path": "pkg/mod.py", "oldString": "a", "newString": "b"}}
    out = _run(tmp_path, {"calls": [edit]}, d.path)
    assert out["results"][0]["result"]["output"] == "ORIG EDIT"
    assert [c[0] for c in out["calls"]] == ["edit"]


@needs_node
def test_registry_patch_untouched_schema_swapped_and_notes_added(tmp_path, daemon):
    d = daemon()
    out = _run(tmp_path, {}, d.path)
    assert sorted(out["replaced"]) == ["edit", "grep", "read", "shell"]
    assert "patch" not in out["replaced"]
    assert out["descriptions"]["patch"] == "Apply a patch."
    assert out["schema_swapped"] == {"read": True, "grep": True, "edit": True,
                                     "shell": True, "patch": False}
    desc = out["descriptions"]
    assert desc["read"].startswith("Read a file.")
    assert "sym:<file>::<name>" in desc["read"] and "[ozagia]" in desc["read"]
    for name in ("grep", "edit", "shell"):
        assert "[ozagia]" in desc[name] and desc[name].count("\n\n") == 1
    # setup connects once, for its hello
    assert [(r["op"], r.get("phase")) for r in d.requests] == [("hello", "setup")]


# --------------------------------------------------------------------------- #
# installer
# --------------------------------------------------------------------------- #
def test_install_and_uninstall(tmp_path):
    path = oc.install(tmp_path)
    assert path == tmp_path / ".opencode" / "plugins" / "ozagia.js"
    assert path.read_text(encoding="utf-8") == oc.plugin_source()
    assert "export default createPlugin()" in oc.plugin_source()
    assert oc.uninstall(tmp_path) is True
    assert not (tmp_path / ".opencode").exists()
    assert oc.uninstall(tmp_path) is False


def test_uninstall_keeps_other_plugins(tmp_path):
    oc.install(tmp_path)
    other = tmp_path / ".opencode" / "plugins" / "other.js"
    other.write_text("export default {}\n", encoding="utf-8")
    oc.uninstall(tmp_path)
    assert other.is_file()


def test_listed_row_matches_the_plugin_file(tmp_path):
    path = tmp_path / ".opencode" / "plugins" / "ozagia.js"
    out = ("ID                         VERSION  SOURCE\n"
           "concise-compaction         local    /home/u/.config/opencode/plugins/c.js\n"
           f"ozagia                     local    {path}\n")
    assert oc._listed_row(out, path) == "ozagia"
    assert oc._listed_row(f"-   local   {path}\n", path) == "-"
    assert oc._listed_row("No plugins found", path) is None


@needs_node
def test_self_test_passes_on_the_packaged_plugin(tmp_path):
    res = oc.self_test(oc.install(tmp_path))
    assert res["ok"] is True, res
    assert sorted(res["replaced"]) == sorted(oc.REPLACED_TOOLS)


def test_cli_install_and_uninstall(tmp_path, capsys):
    from ozagia.integrations.opencode.__main__ import main
    assert main(["install", "--workdir", str(tmp_path)]) == 0
    assert oc.plugin_path(tmp_path).is_file()
    assert main(["uninstall", "--workdir", str(tmp_path)]) == 0
    assert not oc.plugin_path(tmp_path).exists()


@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode not installed")
def test_real_opencode_lists_the_installed_plugin(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    oc.install(work)
    res = oc.check_loads(work, timeout=60)
    assert res["listed"] is True, res
    assert res["ok"] is (res["self_test"].get("ok") is not False)


# --------------------------------------------------------------------------- #
# end to end: the real daemon and the real plugin (review fix 1)
# --------------------------------------------------------------------------- #
E2E = r"""
import * as mod from "./plugin.mjs";
const cfg = JSON.parse(process.env.HARNESS_CFG);
const calls = [];
const tools = {
  edit: { name: "edit", description: "", output: { kind: "s" },
          execute: async (i) => { calls.push(["edit", i]); return "ORIG EDIT"; } },
  shell: { name: "shell", description: "", output: { kind: "x" },
           execute: async (i) => { calls.push(["shell", i]); return "ORIG SHELL"; } },
};
const reg = { get: (n) => tools[n] && { ...tools[n] },
              update: (n, fn) => { const t = { ...tools[n] }; tools[n] = fn(t) || t; } };
await mod.createPlugin({ requestTimeoutMs: cfg.timeoutMs }).setup(
  { tool: { transform: (fn) => fn(reg) } });
const c = { sessionID: "ses_e2e" };
const timed = async (p) => {
  const t0 = Date.now();
  try { return { ok: true, result: await p, ms: Date.now() - t0 }; }
  catch (e) { return { ok: false, error: String(e.message), ms: Date.now() - t0 }; }
};
const shell = timed(tools.shell.execute({ command: cfg.pytest, timeout: 60000 }, c));
await new Promise((r) => setTimeout(r, 500));
const edit = await timed(tools.edit.execute(cfg.edit, c));
process.stdout.write(JSON.stringify({ edit, shell: await shell, calls }) + "\n");
process.exit(0);
"""

SLOW_TEST = '''import time

from pkg.mod import helper


def test_slow():
    time.sleep(8)
    assert helper(1) == 2
'''


@needs_node
def test_real_daemon_edit_runs_once_while_pytest_is_in_flight(tmp_path, sockdir):
    import select
    import sys
    src = Path(__file__).resolve().parents[1] / "src"
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    mod = root / "pkg" / "mod.py"
    mod.write_text("def helper(a, b=1):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_slow.py").write_text(SLOW_TEST)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    sock = sockdir / "real.sock"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OZAGIA_")}
    env.update(PYTHONPATH=str(src), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia.daemon", "serve", "--root", str(root),
         "--sock", str(sock), "--dump", str(tmp_path / "graph.json"),
         "--journal", str(tmp_path / "j.jsonl"), "--parent-pid", str(os.getpid())],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 60)
        assert ready and proc.stdout.readline().startswith("ready "), proc.stderr.read()
        # Warm up: the first edit request imports the edit modules.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(60)
            s.connect(str(sock))
            warm = {"path": "pkg/mod.py", "oldString": "no such text", "newString": "x"}
            s.sendall((json.dumps({"id": 1, "op": "tool", "tool": "edit",
                                   "input": warm}) + "\n").encode())
            assert json.loads(s.makefile("rb").readline())["ok"] is True
        # newString contains oldString: a second application would show.
        edit = {"path": "pkg/mod.py", "oldString": "return a + b",
                "newString": "return a + b  # once"}
        cfg = {"timeoutMs": 5000, "edit": edit,
               "pytest": f"{sys.executable} -m pytest tests/test_slow.py -q"}
        (tmp_path / "plugin.mjs").write_text(oc.plugin_source(), encoding="utf-8")
        (tmp_path / "e2e.mjs").write_text(E2E, encoding="utf-8")
        run_env = dict(os.environ, HARNESS_CFG=json.dumps(cfg),
                       OZAGIA_DAEMON_SOCK=str(sock))
        out = subprocess.run([NODE, str(tmp_path / "e2e.mjs")], cwd=tmp_path,
                             env=run_env, capture_output=True, text=True, timeout=120,
                             stdin=subprocess.DEVNULL)
        assert out.returncode == 0, out.stderr
        got = json.loads(out.stdout.strip().splitlines()[-1])
        # The edit was answered by the daemon, promptly, while pytest ran.
        journal = (tmp_path / "j.jsonl").read_text()
        assert got["edit"]["ok"], (got, journal)
        assert got["edit"]["result"]["output"].startswith("Edited "), got
        assert got["edit"]["ms"] < 5000, (got, journal)
        assert got["shell"]["ok"] and "pytest passed" in got["shell"]["result"]["output"]
        assert got["shell"]["ms"] >= 8000
        assert got["edit"]["ms"] + 500 < got["shell"]["ms"]  # answered during pytest
        assert got["calls"] == []  # no built-in ran
        time.sleep(0.5)
        assert mod.read_text().count("# once") == 1

        # An expired request is answered without being executed.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(sock))
            late = {"path": "pkg/mod.py", "oldString": "# once", "newString": "# twice"}
            s.sendall((json.dumps({"id": 7, "op": "tool", "tool": "edit", "input": late,
                                   "deadline_ms": time.time() * 1000 - 1}) + "\n").encode())
            resp = json.loads(s.makefile("rb").readline())
        assert resp["expired"] is True and resp["handled"] is False
        assert "# twice" not in mod.read_text()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(sock))
            s.sendall(b'{"id": 8, "op": "stats"}\n')
            stats = json.loads(s.makefile("rb").readline())["stats"]
        assert stats["handled"]["true"] == 2 and stats["expired"] == 1
        assert stats["passthrough"] == 1  # the warm-up miss
        assert stats["hellos"] == 2   # setup and the session
        assert stats["hello_sessions"] == ["ses_e2e"]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# --------------------------------------------------------------------------- #
# permissions (ctx.permission.assert)
# --------------------------------------------------------------------------- #
EDIT = {"tool": "edit", "input": {"path": "pkg/mod.py", "oldString": "a", "newString": "b"}}
EDIT_PERM = {"action": "edit", "resources": ["pkg/mod.py"], "save": ["*"]}


def _preflighting(req):
    """A daemon that answers a preflight with the edit permission and the
    real call as handled."""
    if req["op"] == "tool" and req.get("preflight"):
        return {"ok": True, "handled": False, "preflight": True,
                "reason": "permission required", "permission": EDIT_PERM}
    if req["op"] == "tool":
        return {"ok": True, "handled": True, "output": "Edited",
                "permission": EDIT_PERM}
    return _standard(req)


@needs_node
def test_mutating_call_asks_permission_before_the_daemon_runs_it(tmp_path, daemon):
    d = daemon(_preflighting)
    out = _run(tmp_path, {"calls": [EDIT], "permission": "allow"}, d.path)
    assert out["results"][0]["ok"] is True
    assert out["results"][0]["result"]["output"] == "Edited"
    tools = [r for r in d.requests if r["op"] == "tool"]
    assert [t.get("preflight") for t in tools] == [True, None]
    assert out["asserts"] == [{
        "sessionID": "ses_test", "agent": "build", "action": "edit",
        "resources": ["pkg/mod.py"], "save": ["*"],
        "source": {"type": "tool", "messageID": "msg_1", "id": "call_1"}}]
    assert out["calls"] == []
    hello = [r for r in d.requests if r["op"] == "hello"][0]
    assert hello["permission_assert"] is True


@needs_node
def test_refused_permission_is_the_tool_error_and_nothing_runs(tmp_path, daemon):
    d = daemon(_preflighting)
    out = _run(tmp_path, {"calls": [EDIT], "permission": "deny"}, d.path)
    assert out["results"][0] == {"ok": False, "error": "Permission denied: edit"}
    tools = [r for r in d.requests if r["op"] == "tool"]
    assert [t.get("preflight") for t in tools] == [True]  # never sent for real
    assert out["calls"] == []                             # nor run by the built-in


@needs_node
def test_broken_permission_api_hands_the_call_to_the_builtin(tmp_path, daemon):
    d = daemon(_preflighting)
    out = _run(tmp_path, {"calls": [EDIT], "permission": "broken", "settleMs": 200},
               d.path)
    assert out["results"][0]["ok"] is True
    assert out["calls"] == [["edit", EDIT["input"]]]
    tools = [r for r in d.requests if r["op"] == "tool"]
    assert [t.get("preflight") for t in tools] == [True]
    after = [r for r in d.requests if r["op"] == "after"][0]
    assert after["meta"]["fallback"].startswith("permission: ParseError")


@needs_node
def test_passthrough_preflight_does_not_ask(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            return {"ok": True, "handled": False, "reason": "not mapped"}
        return _standard(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [EDIT], "permission": "deny"}, d.path)
    assert out["results"][0]["ok"] is True   # the built-in asks for itself
    assert out["asserts"] == []
    assert out["calls"] == [["edit", EDIT["input"]]]


@needs_node
def test_read_output_is_shown_only_after_the_permission(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool":
            assert not req.get("preflight")
            return {"ok": True, "handled": True, "output": "SECRET",
                    "permission": {"action": "read", "resources": [".env"],
                                   "save": ["*"]}}
        return _standard(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [READ], "permission": "deny"}, d.path)
    assert out["results"][0] == {"ok": False, "error": "Permission denied: read"}
    out = _run(tmp_path, {"calls": [READ], "permission": "allow"}, d.path)
    assert out["results"][0]["result"]["output"] == "SECRET"
    assert out["asserts"][0]["action"] == "read"
    assert out["asserts"][0]["resources"] == [".env"]


@needs_node
def test_without_the_permission_api_there_is_no_preflight(tmp_path, daemon):
    d = daemon(_preflighting)
    out = _run(tmp_path, {"calls": [EDIT]}, d.path)
    assert out["results"][0]["result"]["output"] == "Edited"
    tools = [r for r in d.requests if r["op"] == "tool"]
    assert [t.get("preflight") for t in tools] == [None]


@needs_node
@pytest.mark.parametrize("kind,refused", [("declined", True), ("transport", False)])
def test_only_permission_errors_count_as_refusals(tmp_path, daemon, kind, refused):
    d = daemon(_preflighting)
    out = _run(tmp_path, {"calls": [EDIT], "permission": kind, "settleMs": 200}, d.path)
    if refused:
        assert out["results"][0]["ok"] is False and out["calls"] == []
    else:   # the permission API failed: the built-in runs and asks itself
        assert out["results"][0]["ok"] is True
        assert out["calls"] == [["edit", EDIT["input"]]]


@needs_node
def test_preflight_without_a_permission_request_goes_to_the_builtin(tmp_path, daemon):
    def script(req):
        if req["op"] == "tool" and req.get("preflight"):
            return {"ok": True, "handled": False, "preflight": True,
                    "reason": "permission required", "permission": None}
        return _preflighting(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [EDIT], "permission": "allow", "settleMs": 200},
               d.path)
    assert out["calls"] == [["edit", EDIT["input"]]]
    assert [t.get("preflight") for t in d.requests if t["op"] == "tool"] == [True]


@needs_node
def test_permission_metadata_is_passed_to_assert(tmp_path, daemon):
    meta = {"files": [{"file": "pkg/mod.py", "patch": "--- a\n+++ b\n"}]}

    def script(req):
        if req["op"] == "tool" and req.get("preflight"):
            return {"ok": True, "handled": False, "preflight": True,
                    "permission": {**EDIT_PERM, "metadata": meta}}
        return _preflighting(req)

    d = daemon(script)
    out = _run(tmp_path, {"calls": [EDIT], "permission": "allow"}, d.path)
    assert out["asserts"][0]["metadata"] == meta
