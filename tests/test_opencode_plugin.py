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
await plugin.setup({ tool: { transform: (fn) => fn(reg), hook: () => {} } });
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
  results, calls,
  replaced: Object.keys(tools).filter((k) => tools[k].execute !== original[k]),
  schema_swapped: Object.fromEntries(Object.keys(tools).map((k) => [k, tools[k].output === schema])),
  descriptions: Object.fromEntries(Object.keys(tools).map((k) => [k, tools[k].description])),
}) + "\n");
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
        with conn, conn.makefile("rwb") as fh:
            for line in fh:
                req = json.loads(line)
                self.requests.append(req)
                resp = self.script(req)
                if resp is None:
                    continue
                resp = dict(resp)
                time.sleep(resp.pop("_delay", 0))
                fh.write((json.dumps({"id": req["id"], **resp}) + "\n").encode())
                fh.flush()

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
def test_hello_is_sent_once_on_first_connect(tmp_path, daemon):
    d = daemon()
    out = _run(tmp_path, {"calls": [READ, READ, {"tool": "grep", "input": {"pattern": "x"}}]},
               d.path)
    assert all(r["ok"] for r in out["results"])
    assert d.ops() == ["hello", "tool", "tool", "tool"]
    hello = d.requests[0]
    assert hello["harness"] == "opencode" and hello["plugin"] == "ozagia"
    assert hello["version"] and hello["session"] == "ses_test"


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
    assert d.requests == []  # setup does not connect


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
