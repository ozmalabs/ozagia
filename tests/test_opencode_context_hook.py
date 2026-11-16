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
await mod.createPlugin({}).setup(ctx);
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
        self.fail_events = 0            # session_event requests to refuse
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
                if request["op"] == "session_event" and self.fail_events > 0:
                    self.fail_events -= 1
                    response = {"id": request["id"], "ok": False, "error": "busy"}
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
        # One per user prompt: new history (or time) does not refresh it,
        # so the system prompt stays the same and the prompt cache holds.
        assert len(hint_requests) == 1
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
def test_hint_is_stable_across_tool_calls_and_new_prompts_refresh_it(tmp_path):
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
const texts = [];
const run = async (e) => { await hook(e); texts.push(JSON.stringify(e.system)); };
await run(event());
await tools.shell.execute({ command: "cat src/checkout.py" }, { sessionID: "ses_mutation" });
await run(event());
await tools.edit.execute({ path: "src/checkout.py" }, { sessionID: "ses_mutation" });
await run(event());
// A new user prompt asks again.
const next = event();
next.messages.push({ role: "assistant", content: [{ type: "text", text: "Done" }] },
                   { role: "user", content: [{ type: "text", text: "Now fix refunds" }] });
await run(next);
process.stdout.write(JSON.stringify(texts) + "\n");
""", encoding="utf-8")
        env = dict(os.environ, OZAGIA_DAEMON_SOCK=str(daemon.path))
        proc = subprocess.run([NODE, str(harness)], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
        hints = [r for r in daemon.requests if r["op"] == "hint"]
        assert [r["prompt"] for r in hints] == ["Fix checkout", "Now fix refunds"]
        texts = json.loads(proc.stdout)
        # Identical system prompt text before and after a read and an edit.
        assert texts[0] == texts[1] == texts[2]
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
// OpenCode 2.3 triggers the session hook "compaction" with the event of the
// compaction model request, the same shape as "context":
// L.trigger("session", "compaction", {sessionID, model, system, messages,
// options, agent, tools}) (from the 2.3 binary).
const event = (sessionID) => ({
  sessionID, model: { providerID: "openai", modelID: "gpt-5" }, system: [],
  messages: [], options: { maxTokens: 4096 }, agent: "build", tools: {} });
if (hooks.compaction) {
  for (let i = 0; i < Number(process.env.COMPACTIONS || 1); i++) {
    await hooks.compaction(event("ses_compact"));
  }
  const { sessionID, ...noSession } = event("x");
  await hooks.compaction(noSession);   // no session: nothing sent
}
await new Promise((r) => setTimeout(r, 30));
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


@needs_node
def test_hello_reports_whether_the_compaction_hook_registered(tmp_path):
    daemon = StubDaemon(tmp_path / "daemon.sock")
    try:
        proc = _run_compaction(tmp_path, daemon.path)
        assert proc.returncode == 0, proc.stderr
        hellos = [r for r in daemon.requests if r["op"] == "hello"]
        assert hellos and all(h["compaction_hook"] is True for h in hellos)
        (event,) = [r for r in daemon.requests if r["op"] == "session_event"]
        assert event["compaction_hook"] is True and "reset_pending" not in event
        daemon.requests.clear()
        proc = _run_compaction(tmp_path, daemon.path, NO_COMPACTION_HOOK="1")
        assert proc.returncode == 0, proc.stderr
        hellos = [r for r in daemon.requests if r["op"] == "hello"]
        assert hellos and all(h["compaction_hook"] is False for h in hellos)
    finally:
        daemon.close()


@needs_node
def test_a_failed_compaction_event_is_sent_again_as_reset_pending(tmp_path):
    daemon = StubDaemon(tmp_path / "daemon.sock")
    daemon.fail_events = 1
    try:
        proc = _run_compaction(tmp_path, daemon.path, COMPACTIONS="3")
        assert proc.returncode == 0, proc.stderr
        events = [r for r in daemon.requests if r["op"] == "session_event"]
        assert [e.get("reset_pending") for e in events] == [None, True, None]
    finally:
        daemon.close()
