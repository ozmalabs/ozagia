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
//                              output, append the daemon's `append` text
//                              (also for `expired`: the daemon did nothing).
//     daemon unreachable, error response, or (read/grep/glob) timeout
//                           -> run the original and return its output as is;
//                              a best-effort `after` with meta.fallback records it.
//     edit/write/shell sent, then failed or timed out
//                           -> do not run the original (the daemon may still
//                              run the request); throw an "outcome unknown"
//                              error telling the model to re-read / check,
//                              and send `after` with
//                              meta.fallback = "unknown_outcome".
// - One connection per request (open, send, read one response, close), so a
//   slow call never queues others. Every request carries
//   deadline_ms = Date.now() + its timeout; the daemon drops it unexecuted
//   once that has passed. `hello` is sent once per process, on its own
//   connection, before the first request.
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

// One connection per request: open, send one line, read one response line,
// close. A slow call (a captured pytest) never queues other calls behind it.
class DaemonClient {
  constructor(path, timeoutMs) {
    this.path = path;
    this.timeoutMs = timeoutMs;
    this.nextId = 1;
    this.helloSent = false;
    this.session = undefined;
  }

  // `hello` once per process, on its own short-lived connection; it is sent
  // again only if the daemon could not be reached.
  hello() {
    if (this.helloSent) return;
    this.helloSent = true;
    this.request({ op: "hello", harness: HARNESS, plugin: PLUGIN_ID,
                   version: PLUGIN_VERSION, session: this.session },
                 this.timeoutMs, false, false)
      .catch((err) => { if (!err.sent) this.helloSent = false; });
  }

  // Resolves with the response (ok: true); rejects with an Error whose
  // `sent` is true when the request line had been written, so the daemon
  // may have read it. `deadline_ms` tells the daemon when to stop caring.
  // `keepAlive` holds the event loop open while a tool call waits.
  request(msg, timeoutMs = this.timeoutMs, keepAlive = true, withHello = true) {
    if (withHello) this.hello();
    const id = this.nextId++;
    const line = JSON.stringify({ ...msg, id, deadline_ms: Date.now() + timeoutMs }) + "\n";
    return new Promise((resolve, reject) => {
      let sent = false;
      let done = false;
      let buffer = "";
      const s = net.createConnection({ path: this.path });
      const finish = (err, res) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        s.destroy();
        if (err) { err.sent = sent; reject(err); } else resolve(res);
      };
      const timer = setTimeout(
        () => finish(new Error(`daemon timeout after ${timeoutMs} ms`)), timeoutMs);
      if (!keepAlive) { timer.unref(); s.unref(); }
      s.setEncoding("utf8");
      s.once("connect", () => { sent = true; s.write(line); });
      s.on("data", (chunk) => {
        buffer += chunk;
        const nl = buffer.indexOf("\n");
        if (nl < 0) return;
        let res;
        try { res = JSON.parse(buffer.slice(0, nl)); }
        catch { finish(new Error("daemon sent a bad response")); return; }
        if (!res || res.id !== id) { finish(new Error("daemon response does not match")); return; }
        if (!res.ok) { finish(new Error(`daemon error: ${res.error || "unknown"}`)); return; }
        finish(null, res);
      });
      s.on("error", (err) => finish(err));
      s.on("close", () => finish(new Error("daemon connection closed")));
    });
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

// Tools whose request, once sent, may have changed files or run commands.
const MUTATING = new Set(["edit", "write", "shell"]);

function unknownOutcome(name, why) {
  const what = name === "shell"
    ? "Check its effects (re-read the files it touches) before running it again."
    : "Re-read the file before editing it again.";
  return new Error(`ozagia: the graph daemon did not answer this ${name} call (${why}); `
    + `it may or may not have been applied. ${what}`);
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
      if (err && err.sent && MUTATING.has(name)) {
        client.request({ op: "after", ...base, output: "",
                         meta: { fallback: "unknown_outcome", error: fallback } },
                       client.timeoutMs, false).catch(() => {});
        throw unknownOutcome(name, fallback);
      }
    }
    if (res && res.handled) {
      const out = typeof res.output === "string" ? res.output : asText(res.output);
      if (res.is_error) throw new Error(out);
      return textResult(out);
    }
    // handled false (also `expired`: the daemon did nothing) -> the built-in.
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
