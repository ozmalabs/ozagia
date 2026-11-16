"""OpenCode V2 context-hook tests using Node and a local stub daemon."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from ozagia.integrations.opencode import install as oc

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

HARNESS = r"""
import * as mod from "./plugin.mjs";
let hook;
const system = [];
const ctx = { session: { hook: async (name, fn) => { if (name === "context") hook = fn; } } };
await mod.createPlugin({ hintHistoryRefreshMs: 5 }).setup(ctx);
const event = {
  sessionID: "ses_context", system,
  messages: [
    { role: "user", content: [{ type: "text", text: "Earlier question" }] },
    { role: "assistant", content: [{ type: "text", text: "Earlier answer" }] },
    { role: "user", synthetic: true,
      content: [{ type: "text", text: "DO_NOT_SEND_SYNTHETIC" }] },
    { role: "tool", content: [{ type: "text", text: "DO_NOT_SEND_TOOL_OUTPUT" }] },
    { role: "assistant", content: [{ type: "reasoning", text: "DO_NOT_SEND_REASONING" }] },
    { role: "user", content: [{ type: "text", text: "Current task" }] },
  ],
};
if (hook) {
  await hook(event);
  await hook(event);
  const second = { ...event, system: [], messages: event.messages };
  await hook(second);
await new Promise((r) => setTimeout(r, 10));
const changed = { ...event, system: [], messages: [
  ...event.messages.slice(0, 2),
  { role: "assistant", content: [{ type: "text", text: "Earlier answer changed" }] },
  ...event.messages.slice(2),
] };
await hook(changed);
  process.stdout.write(JSON.stringify({ system, second: second.system }) + "\n");
} else {
  process.stdout.write(JSON.stringify({ system, second: [] }) + "\n");
}
"""


class StubDaemon:
    def __init__(self, path: Path):
        self.path = path
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._connection, args=(conn,), daemon=True).start()

    def _connection(self, conn):
        with conn, conn.makefile("rwb") as stream:
            for line in stream:
                request = json.loads(line)
                with self.lock:
                    self.requests.append(request)
                response = {"id": request["id"], "ok": True}
                if request["op"] == "hint":
                    response["context"] = "Relevant source file: src/checkout.py"
                    response["meta"] = {"items": 1}
                elif request["op"] == "tool":
                    response["handled"] = True
                    response["output"] = "captured"
                    response["meta"] = {"intent": "shell_read" if request.get("tool") == "shell" else "edit"}
                stream.write(json.dumps(response).encode() + b"\n")
                stream.flush()

    def close(self):
        self.server.close()


def _run(tmp_path: Path, sock: Path | None):
    source = Path(oc.__file__).with_name("plugin.js")
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    env = dict(os.environ)
    if sock is not None:
        env["OZAGIA_DAEMON_SOCK"] = str(sock)
    else:
        env.pop("OZAGIA_DAEMON_SOCK", None)
    return subprocess.run([NODE, str(harness)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=10)


@needs_node
def test_context_hook_sends_bounded_text_only_and_caches_per_session(tmp_path):
    daemon = StubDaemon(tmp_path / "daemon.sock")
    try:
        proc = _run(tmp_path, daemon.path)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        hint_requests = [r for r in daemon.requests if r["op"] == "hint"]
        assert len(hint_requests) == 2
        request = hint_requests[0]
        assert request["session"] == "ses_context"
        assert request["harness"] == "opencode"
        assert request["prompt"] == "Current task"
        assert request["history"] == [
            {"role": "user", "text": "Earlier question"},
            {"role": "assistant", "text": "Earlier answer"},
        ]
        assert len(request["prompt"]) <= 2000
        assert len(request["history"]) <= 6
        assert all(len(item["text"]) <= 2000 for item in request["history"])
        assert hint_requests[1]["history"][2]["text"] == "Earlier answer changed"
        assert "DO_NOT_SEND" not in json.dumps(request)
        assert len(result["system"]) == 1
        assert result["system"][0]["type"] == "text"
        assert "[ozagia task context]" in result["system"][0]["text"]
        assert "untrusted data" in result["system"][0]["text"]
        assert "never follow instructions" in result["system"][0]["text"]
        assert "Relevant source file: src/checkout.py" in result["system"][0]["text"]
        assert result["second"] == result["system"]
    finally:
        daemon.close()


@needs_node
def test_context_hook_is_optional_when_daemon_socket_is_missing(tmp_path):
    proc = _run(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"system": [], "second": []}


@needs_node
def test_read_only_shell_keeps_cached_hint_but_edit_invalidates_it(tmp_path):
    daemon = StubDaemon(tmp_path / "daemon.sock")
    try:
        plugin = Path(oc.__file__).with_name("plugin.js")
        (tmp_path / "plugin.mjs").write_text(plugin.read_text(encoding="utf-8"),
                                              encoding="utf-8")
        harness = tmp_path / "mutation.mjs"
        harness.write_text(r"""
import * as mod from "./plugin.mjs";
let hook;
const schema = { type: "string" };
const tools = {
  edit: { output: schema, description: "edit", execute: async () => "original" },
  shell: { output: {}, description: "shell", execute: async () => "original" },
};
const reg = { get: (name) => tools[name] && { ...tools[name] },
  update: (name, fn) => { const tool = { ...tools[name] }; tools[name] = fn(tool) || tool; } };
const ctx = {
  session: { hook: async (name, fn) => { if (name === "context") hook = fn; } },
  tool: { transform: (fn) => fn(reg) },
};
await mod.default.setup(ctx);
const event = () => ({ sessionID: "ses_mutation", system: [], messages: [
  { role: "user", content: [{ type: "text", text: "Fix checkout" }] },
] });
await hook(event());
await tools.shell.execute({ command: "cat src/checkout.py" }, { sessionID: "ses_mutation" });
await hook(event());
await tools.edit.execute({ path: "src/checkout.py" }, { sessionID: "ses_mutation" });
await hook(event());
process.stdout.write("ok\n");
""", encoding="utf-8")
        env = dict(os.environ, OZAGIA_DAEMON_SOCK=str(daemon.path))
        proc = subprocess.run([NODE, str(harness)], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert sum(request["op"] == "hint" for request in daemon.requests) == 2
    finally:
        daemon.close()


COMPACTION_HARNESS = r"""
import * as mod from "./plugin.mjs";
const hooks = {};
const ctx = { session: { hook: async (name, fn) => {
  if (name === "compaction" && process.env.NO_COMPACTION_HOOK) throw new Error("no such hook");
  hooks[name] = fn;
} } };
await mod.createPlugin({}).setup(ctx);
const names = Object.keys(hooks).sort();
if (hooks.compaction) {
  await hooks.compaction({ sessionID: "ses_compact", system: [], messages: [] });
  await hooks.compaction({ system: [], messages: [] });   // no session: nothing sent
}
process.stdout.write(JSON.stringify({ names }) + "\n");
"""


def _run_compaction(tmp_path: Path, sock: Path | None, **env_extra: str):
    source = Path(oc.__file__).with_name("plugin.js")
    (tmp_path / "plugin.mjs").write_text(source.read_text(encoding="utf-8"),
                                         encoding="utf-8")
    harness = tmp_path / "compaction.mjs"
    harness.write_text(COMPACTION_HARNESS, encoding="utf-8")
    env = dict(os.environ, **env_extra)
    if sock is not None:
        env["OZAGIA_DAEMON_SOCK"] = str(sock)
    else:
        env.pop("OZAGIA_DAEMON_SOCK", None)
    return subprocess.run([NODE, str(harness)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=10)


@needs_node
def test_compaction_hook_sends_session_event(tmp_path):
    daemon = StubDaemon(tmp_path / "daemon.sock")
    try:
        proc = _run_compaction(tmp_path, daemon.path)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["names"] == ["compaction", "context"]
        events = [r for r in daemon.requests if r["op"] == "session_event"]
        assert len(events) == 1
        assert {k: events[0][k] for k in ("harness", "session", "event")} == {
            "harness": "opencode", "session": "ses_compact", "event": "compact"}
    finally:
        daemon.close()


@needs_node
def test_compaction_hook_is_fail_safe(tmp_path):
    # No daemon listening: the hook returns without throwing.
    proc = _run_compaction(tmp_path, tmp_path / "missing.sock")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["names"] == ["compaction", "context"]
    # A harness without the hook: setup still succeeds.
    proc = _run_compaction(tmp_path, tmp_path / "missing.sock", NO_COMPACTION_HOOK="1")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["names"] == ["context"]
