// ozagia plugin for OpenCode 2.3: hands the agent's file and shell tool calls
// to the ozagia graph daemon (protocol: src/ozagia/daemon/protocol.py).
//
// Behaviour
// - Without $OZAGIA_DAEMON_SOCK the plugin does nothing: no tool is replaced.
// - Otherwise read, grep, glob, edit, write and shell (those that exist) get a
//   new execute; patch and every other tool are untouched. Each replaced tool
//   keeps its original execute, takes the string output schema of `edit` (the
//   built-ins validate their output against their own schema), and gets one
//   sentence added to its description about `[ozagia]` context lines.
// - New execute: send {op: "tool"} to the daemon.
//     handled, not is_error -> return the daemon's output.
//     handled, is_error     -> throw Error(output): a tool error for the model.
//     handled false         -> run the original, send {op: "after"} with its
//                              output, append the daemon's `append` text.
//     daemon error/timeout  -> run the original and return its output as is;
//                              a best-effort `after` with meta.fallback records it.
//   A daemon failure never fails a tool call.
// - The socket is opened lazily on the first call, so OpenCode never waits for
//   the daemon at startup. `hello` is sent once, on the first connection.
// - Timeouts: 30 s per request; a shell request gets the command's own
//   timeout (input.timeout, ms; 300 s when unset) plus 30 s.
// - setup never throws.
// Plain ES module, node: built-ins only.

import net from "node:net";

export const PLUGIN_ID = "ozagia";
export const PLUGIN_VERSION = "0.1.0";
export const REQUEST_TIMEOUT_MS = 30000;
const SHELL_DEFAULT_TIMEOUT_MS = 300000; // with the 30 s margin: 330000
const HARNESS = "opencode";
const CONTEXT_NOTE = "Lines starting with [ozagia] are graph context, not part of the tool's own output.";
const NOTES = {
  read: "Paths may also be graph node keys (sym:<file>::<name>, test:..., doc:...); "
    + "lines starting with [ozagia] are graph context, not file content.",
  grep: CONTEXT_NOTE,
  glob: CONTEXT_NOTE,
  edit: CONTEXT_NOTE,
  write: CONTEXT_NOTE,
  shell: CONTEXT_NOTE,
};

class DaemonClient {
  constructor(path, timeoutMs) {
    this.path = path;
    this.timeoutMs = timeoutMs;
    this.sock = null;
    this.connecting = null;
    this.pending = new Map();
    this.nextId = 1;
    this.buffer = "";
    this.helloSent = false;
    this.session = undefined;
  }

  connect() {
    if (this.sock) return Promise.resolve(this.sock);
    if (this.connecting) return this.connecting;
    this.connecting = new Promise((resolve, reject) => {
      const s = net.createConnection({ path: this.path });
      s.setEncoding("utf8");
      s.unref(); // an idle connection never keeps OpenCode alive
      s.once("connect", () => {
        this.sock = s;
        this.connecting = null;
        if (!this.helloSent) {
          this.helloSent = true;
          this.send({ op: "hello", harness: HARNESS, plugin: PLUGIN_ID,
                      version: PLUGIN_VERSION, session: this.session }, s);
        }
        resolve(s);
      });
      s.on("data", (chunk) => this.onData(chunk));
      s.on("error", (err) => {
        if (this.connecting) { this.connecting = null; reject(err); }
        this.drop(s, err);
      });
      s.on("close", () => this.drop(s, new Error("daemon connection closed")));
    });
    return this.connecting;
  }

  drop(s, err) {
    if (this.sock !== s && this.sock !== null) return; // an old socket closing late
    this.sock = null;
    for (const [, p] of this.pending) p.reject(err);
    this.pending.clear();
    this.buffer = "";
  }

  onData(chunk) {
    this.buffer += chunk;
    let nl;
    while ((nl = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, nl);
      this.buffer = this.buffer.slice(nl + 1);
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      const p = this.pending.get(msg && msg.id);
      if (p) { this.pending.delete(msg.id); p.resolve(msg); }
    }
  }

  // Write one request on an open socket; the response is matched by id.
  send(msg, s, timeoutMs = this.timeoutMs, keepAlive = false) {
    const id = this.nextId++;
    const done = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`daemon timeout after ${timeoutMs} ms`));
      }, timeoutMs);
      if (!keepAlive) timer.unref();
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
    });
    s.write(JSON.stringify({ ...msg, id }) + "\n");
    return done.then((res) => {
      if (!res.ok) throw new Error(`daemon error: ${res.error || "unknown"}`);
      return res;
    });
  }

  // Connect if needed, then send; the timeout covers both. `keepAlive` holds
  // the event loop open while a tool call waits for its answer.
  async request(msg, timeoutMs = this.timeoutMs, keepAlive = true) {
    let timer;
    const limit = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`daemon timeout after ${timeoutMs} ms`)), timeoutMs);
      if (!keepAlive) timer.unref();
    });
    try {
      return await Promise.race([
        this.connect().then((s) => this.send(msg, s, timeoutMs, keepAlive)), limit]);
    } finally {
      clearTimeout(timer);
    }
  }
}

// The string form of a built-in's result, for a tool whose output schema is a string.
export function asText(result) {
  if (typeof result === "string") return result;
  if (result && Array.isArray(result.content)) {
    const parts = result.content
      .filter((p) => p && p.type === "text" && typeof p.text === "string")
      .map((p) => p.text);
    if (parts.length) return parts.join("\n");
  }
  if (result && "output" in result) {
    return typeof result.output === "string" ? result.output : JSON.stringify(result.output);
  }
  return result === undefined ? "" : JSON.stringify(result);
}

const textResult = (text) => ({ output: text, content: [{ type: "text", text }] });

function timeoutFor(name, input, base) {
  if (name !== "shell") return base;
  const t = Number(input && input.timeout);
  return (Number.isFinite(t) && t > 0 ? t : SHELL_DEFAULT_TIMEOUT_MS) + base;
}

function wrap(client, name, tool, original) {
  return async function execute(input, c) {
    const session = c && c.sessionID;
    if (client.session === undefined && session) client.session = session;
    const base = { harness: HARNESS, session, tool: name, input };
    let res;
    let fallback = null;
    try {
      res = await client.request({ op: "tool", ...base }, timeoutFor(name, input, client.timeoutMs));
    } catch (err) {
      fallback = String((err && err.message) || err);
    }
    if (res && res.handled) {
      const out = typeof res.output === "string" ? res.output : asText(res.output);
      if (res.is_error) throw new Error(out);
      return textResult(out);
    }
    const output = asText(await original.call(tool, input, c));
    if (fallback !== null) {
      client.request({ op: "after", ...base, output, meta: { fallback } },
                     client.timeoutMs, false).catch(() => {});
      return textResult(output);
    }
    try {
      const after = await client.request({ op: "after", ...base, output });
      if (typeof after.append === "string" && after.append) {
        return textResult(output + "\n" + after.append);
      }
    } catch { /* the built-in's output stands on its own */ }
    return textResult(output);
  };
}

export function createPlugin(options = {}) {
  const timeoutMs = options.requestTimeoutMs || REQUEST_TIMEOUT_MS;
  return {
    id: PLUGIN_ID,
    async setup(ctx) {
      try {
        const sockPath = process.env.OZAGIA_DAEMON_SOCK;
        if (!sockPath || !ctx || !ctx.tool || typeof ctx.tool.transform !== "function") return;
        const client = new DaemonClient(sockPath, timeoutMs);
        ctx.tool.transform((reg) => {
          const edit = reg.get("edit");
          const stringSchema = edit && edit.output;
          if (!stringSchema) return; // without a string schema a replaced tool would fail
          for (const name of Object.keys(NOTES)) {
            try {
              const tool = reg.get(name);
              if (!tool || typeof tool.execute !== "function") continue;
              const original = tool.execute;
              const description = tool.description || "";
              reg.update(name, (t) => {
                t.execute = wrap(client, name, tool, original);
                t.output = stringSchema;
                t.description = (description ? description + "\n\n" : "") + NOTES[name];
                return t;
              });
            } catch { /* leave this tool as it was */ }
          }
        });
      } catch { /* never break OpenCode startup */ }
    },
  };
}

export default createPlugin();
