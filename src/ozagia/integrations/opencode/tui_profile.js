// Independent OpenCode TUI sidebar plugin. This is installed beside (and does
// not replace) ozagia/tui.js, which owns the existing brand row. The summary
// line shows the graph/daemon state and, while an `ozagia agent` launch is
// still preparing the snapshot, the launch phase from OZAGIA_LAUNCH_STATUS.
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { Plugin, usePlugin } from "@opencode/plugin/tui";
import { createResource, createSignal, onCleanup } from "solid-js";
import { jsx } from "@opentui/solid/jsx-runtime";

const EMPTY = "unknown";
const BUNDLED_PYTHON = "__OZAGIA_PYTHON__";
const LAUNCH_ENV = "OZAGIA_LAUNCH_STATUS";
const LAUNCH_MAX_BYTES = 64 * 1024;
const LAUNCH_DONE = new Set(["ready", "failed"]);

function readLaunch(path) {
  if (!path) return undefined;
  try {
    const text = readFileSync(path, "utf8");
    if (!text || text.length > LAUNCH_MAX_BYTES) return undefined;
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || value.version !== 1) return undefined;
    return value;
  } catch {
    return undefined;
  }
}

// One block for the sidebar while the launch is not ready. `ready` falls back
// to the usual status/metrics block; a failure stays visible.
function launchLines(launch, nowMs) {
  const phase = launch && launch.phase;
  if (!phase || phase === "ready") return undefined;
  const started = Number(launch.started_at);
  const elapsed = Number.isFinite(started)
    ? Math.max(0, Math.round(nowMs / 1000 - started)) : 0;
  if (phase === "failed") {
    return ["Ozagia", "Status: offline", "Graph: failed", "Daemon: down"];
  }
  const graph = phase === "building" ? "building"
    : phase === "refreshing" ? "refreshing"
    : phase === "daemon" ? "ready"
    : "checking";
  const daemon = phase === "daemon" ? "starting" : "waiting";
  return ["Ozagia", "Status: starting", `Graph: ${graph} · ${elapsed}s`,
          `Daemon: ${daemon}`];
}

function runPython(python, root, args, timeoutMs = 8000) {
  return new Promise((resolve) => {
    let stdout = "";
    let finished = false;
    let child;
    const timer = setTimeout(() => child?.kill("SIGTERM"), timeoutMs);
    try {
      child = spawn(python, ["-m", "ozagia.session_profile_cli", "--root", root, "--json", ...args], {
        cwd: root, env: process.env, stdio: ["ignore", "pipe", "ignore"],
      });
      child.stdout.on("data", (chunk) => {
        stdout += chunk.toString();
        if (stdout.length > 4 * 1024 * 1024) child.kill("SIGTERM");
      });
      child.on("error", () => done(undefined));
      child.on("close", (code) => done(code === 0 || code === 1 ? parse() : undefined));
    } catch {
      done(undefined);
    }
    function parse() {
      try { return JSON.parse(stdout); } catch { return undefined; }
    }
    function done(value) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve(value);
    }
  });
}

function ProfileSidebar(props) {
  const context = usePlugin();
  const root = () => context.location?.directory || context.data.location.default()?.directory;
  const options = context.options && typeof context.options === "object" ? context.options : {};
  const python = typeof options.python === "string" ? options.python : BUNDLED_PYTHON;
  const [view, { refetch }] = createResource(() => props.sessionID, async (sessionID) => {
    try {
      if (!root()) return undefined;
      const [profile, status] = await Promise.all([
        runPython(python, root(), []),
        new Promise((resolve) => {
          let stdout = "";
          let child;
          const timer = setTimeout(() => child?.kill("SIGTERM"), 10000);
          try {
            child = spawn(python, ["-m", "ozagia.status_cli", "--root", root(), "--json"], {
              cwd: root(), env: process.env, stdio: ["ignore", "pipe", "ignore"],
            });
            child.stdout.on("data", (chunk) => { stdout += chunk.toString(); if (stdout.length > 2 * 1024 * 1024) child.kill("SIGTERM"); });
            child.on("error", () => finish(undefined));
            child.on("close", (code) => { let value; try { if (code === 0 || code === 1) value = JSON.parse(stdout); } catch {} finish(value); });
          } catch { finish(undefined); }
          function finish(value) { clearTimeout(timer); resolve(value); }
        }),
      ]);
      return { profile, status };
    } catch {
      return undefined;
    }
  });
  const refreshTimer = setInterval(() => void refetch(), 30000);
  onCleanup(() => clearInterval(refreshTimer));
  // The launch status is a small file the session replaces while the graph
  // builds; poll it until the snapshot is ready, then stop and refresh the
  // profile/status once so the usual G/D line is current.
  const launchPath = process.env[LAUNCH_ENV];
  const [launch, setLaunch] = createSignal(readLaunch(launchPath));
  if (launchPath) {
    const poll = setInterval(() => {
      const next = readLaunch(launchPath);
      setLaunch(next);
      if (next && LAUNCH_DONE.has(next.phase)) {
        clearInterval(poll);
        if (next.phase === "ready") void refetch();
      }
    }, 500);
    onCleanup(() => clearInterval(poll));
  }
  const value = () => view();
  const session = () => value()?.profile?.sessions?.find((entry) => entry.session_id === props.sessionID);
  const count = (number) => Number.isFinite(number)
    ? Math.max(0, Math.round(number)).toLocaleString("en-US") : "?";
  const statusText = (state) => {
    if (state === "running") return "online";
    if (state === "absent") return "offline";
    if (state === "unknown") return "unavailable";
    return state || "unknown";
  };
  const graphText = (snapshot, details) => {
    const state = snapshot?.state || EMPTY;
    if (state !== "stale") return state;
    let reason;
    if (details?.root_mismatch) reason = "different root";
    else if (details?.tree_changed) reason = "working tree changed";
    else if (details?.head && details?.snapshot_head && details.head !== details.snapshot_head) {
      reason = "HEAD moved";
    }
    return reason ? `stale · ${reason}` : "stale";
  };
  const sessionText = (record) => {
    const profile = value()?.profile;
    const daemon = record?.daemon_profile || {};
    const tools = daemon.tool_calls_observed ?? record?.tool_calls_observed
      ?? record?.tools;
    const calls = record?.host_profile?.model_calls;
    const errors = daemon.errors ?? record?.errors;
    if (tools == null && calls == null) {
      return profile?.journal === "absent" ? "Session: no journal" : "Session: no activity yet";
    }
    const parts = [];
    if (tools != null) {
      parts.push(`${count(tools)} ${tools === 1 ? "tool" : "tools"}`);
    }
    if (calls != null) {
      parts.push(`${count(calls)} model ${calls === 1 ? "call" : "calls"}`);
    }
    if (Number.isFinite(errors) && errors > 0) {
      parts.push(`${count(errors)} error${errors === 1 ? "" : "s"}`);
    }
    return `Session: ${parts.join(" · ")}`;
  };
  const color = context.theme.text.base;
  const lines = () => {
    const during = launchLines(launch(), Date.now());
    if (during) return during;
    const status = value()?.status;
    const snapshot = status?.doctor?.report?.snapshot;
    const record = session();
    const result = [
      "Ozagia",
      `Status: ${statusText(status?.daemon?.state)}`,
      `Graph: ${graphText(snapshot, snapshot?.details)}`,
    ];
    const model = record?.host_profile?.models?.[0];
    if (model) result.push(`Model: ${model}`);
    result.push(sessionText(record));
    return result;
  };
  return jsx("text", { fg: color, get children() { return lines().join("\n"); } });
}

export default Plugin.define({
  id: "ozagia-profile",
  setup(context) {
    return context.ui.slot({
      append: "sidebar.content",
      render: (input) => jsx(ProfileSidebar, { sessionID: input.sessionID }),
    });
  },
});
