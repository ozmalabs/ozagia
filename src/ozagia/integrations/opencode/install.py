"""Install the ozagia OpenCode plugin into a workdir and check that it loads.

OpenCode 2.3 loads project plugins from `<project>/.opencode/plugins/*.js`
(a path under `plugins` in OPENCODE_CONFIG did not load, verified). `install`
writes two of them:

- `ozagia.js` -- the server plugin (a top-level file). It does nothing unless
  `OZAGIA_DAEMON_SOCK` is set in OpenCode's environment.
- `ozagia/tui.js` -- the TUI brand plugin (in a subdirectory, the only place
  the TUI process loads a plugin from). It renders `[ + ozAGIa ]` (green) centred
  under
  the home-screen logo. It fails silently: if the render tree it pokes at has
  changed, it leaves the base TUI untouched.

The server plugin is the one `check_loads` and `self_test` verify; the brand
plugin is a companion that has no daemon to talk to.

`check_loads` uses two checks, neither of which calls a model:

- `opencode plugin list`, run in the workdir. It asks OpenCode's background
  service, which imports the project's plugins asynchronously after the first
  request for a directory: the first calls print "No plugins found", later
  ones a table `ID VERSION SOURCE` whose ID is the module's `id` (or `-` when
  the file failed to import or has no plugin shape). With `--standalone` it
  printed "No plugins found" every time (verified on 2.3.0), so the check
  polls the background service until the plugin's row appears or `timeout`
  runs out. A listed row proves the module imported with the right shape; it
  does not run `setup`.
- a self-test in Node: import the file, call `setup` with a stub `ctx` and a
  V2 tool editor, and check the six wrapped tools plus the permission-gated
  read-only structural tool registration. OpenCode itself runs on Bun, so
  this is a proxy for the host registry.

CLI: `python -m ozagia.integrations.opencode install|uninstall|check --workdir W`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path
from typing import Any

PLUGIN_ID = "ozagia"
PLUGIN_REL = Path(".opencode") / "plugins" / "ozagia.js"
TUI_BRAND_ID = "ozagia-brand"
# The TUI process loads a plugin only from `plugins/<name>/tui.js` (a
# top-level `plugins/*.js` or a subdir `index.js` is not picked up by the TUI;
# verified against the 2.3.0 binary). The server plugin stays a top-level file.
TUI_BRAND_REL = Path(".opencode") / "plugins" / "ozagia" / "tui.js"
TUI_PROFILE_ID = "ozagia-profile"
TUI_PROFILE_REL = Path(".opencode") / "plugins" / TUI_PROFILE_ID / "tui.js"
REPLACED_TOOLS = ("read", "grep", "glob", "edit", "write", "shell")
# Pause between `opencode plugin list` polls while the service loads plugins.
LIST_POLL_S = 1.0

_SELF_TEST = r"""
const [path] = process.argv.slice(1);
const out = { imported: false };
try {
  const mod = await import(new URL("file://" + path).href);
  const plugin = mod.default;
  out.imported = true;
  out.id = plugin && plugin.id;
  out.has_setup = !!plugin && typeof plugin.setup === "function";
  const schema = { kind: "string-schema" };
  const tools = {};
  const nativeTools = {};
  for (const name of ["read", "grep", "glob", "edit", "write", "shell", "patch"]) {
    tools[name] = { name, description: name, output: name === "edit" ? schema : { kind: name },
                    execute: async () => "" };
  }
  const original = Object.fromEntries(Object.entries(tools).map(([k, t]) => [k, t.execute]));
  const reg = {
    get: (name) => tools[name],
    update: (name, fn) => { const r = fn(tools[name]); if (r) tools[name] = r; },
    namespace: () => {},
    add: (definition) => { nativeTools[`ozagia_${definition.name}`] = definition; },
  };
  let transformed = false;
  await plugin.setup({ tool: { transform: (fn) => { transformed = true; fn(reg); },
                               hook: () => {} },
                       permission: { assert: async () => {} } });
  out.transformed = transformed;
  out.replaced = Object.keys(tools).filter((k) => tools[k].execute !== original[k]);
  out.schema_swapped = ["read", "grep", "glob", "write", "shell"]
    .every((k) => tools[k].output === schema);
  out.patch_untouched = tools.patch.execute === original.patch;
  out.native_tools = Object.keys(nativeTools);
  out.structure_readonly = nativeTools.ozagia_structure
    && nativeTools.ozagia_structure.options.codemode === false;
} catch (err) {
  out.error = String((err && err.stack) || err);
}
process.stdout.write(JSON.stringify(out) + "\n");
process.exit(0);
"""


def plugin_source() -> str:
    """The packaged plugin.js (server plugin)."""
    return (resources.files("ozagia.integrations.opencode")
            .joinpath("plugin.js").read_text(encoding="utf-8"))


def tui_brand_source() -> str:
    """The packaged tui_brand.js (TUI brand plugin)."""
    return (resources.files("ozagia.integrations.opencode")
            .joinpath("tui_brand.js").read_text(encoding="utf-8"))


def tui_profile_source() -> str:
    """The metadata-only OpenCode session profile sidebar plugin."""
    source = (resources.files("ozagia.integrations.opencode")
              .joinpath("tui_profile.js").read_text(encoding="utf-8"))
    return source.replace('"__OZAGIA_PYTHON__"', json.dumps(sys.executable))


def plugin_path(workdir: str | Path) -> Path:
    return Path(workdir) / PLUGIN_REL


def tui_brand_path(workdir: str | Path) -> Path:
    return Path(workdir) / TUI_BRAND_REL


def install(workdir: str | Path) -> Path:
    """Write the server plugin and separate TUI companions into the workdir.

    Returns the server plugin's path (the one `check_loads` and `self_test`
    operate on). The TUI brand plugin is written alongside it, in the
    `ozagia/` subdirectory the TUI process loads from."""
    workdir = Path(workdir)
    server = plugin_path(workdir)
    server.parent.mkdir(parents=True, exist_ok=True)
    server.write_text(plugin_source(), encoding="utf-8")
    brand = tui_brand_path(workdir)
    brand.parent.mkdir(parents=True, exist_ok=True)
    brand.write_text(tui_brand_source(), encoding="utf-8")
    profile = workdir / TUI_PROFILE_REL
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(tui_profile_source(), encoding="utf-8")
    return server


def uninstall(workdir: str | Path) -> bool:
    """Remove both plugin files, and their directories when left empty.
    Returns whether a file was removed."""
    workdir = Path(workdir)
    removed = False
    for rel in (PLUGIN_REL, TUI_BRAND_REL, TUI_PROFILE_REL):
        path = workdir / rel
        if path.is_file():
            path.unlink()
            removed = True
    # deepest-first; a non-empty directory (another plugin) stops the cleanup
    for parent in (workdir / TUI_BRAND_REL.parent,
                   workdir / TUI_PROFILE_REL.parent,
                   workdir / PLUGIN_REL.parent,
                   workdir / ".opencode"):
        try:
            parent.rmdir()
        except OSError:
            break
    return removed


def self_test(path: str | Path, *, timeout: float = 60,
              node: str | None = None) -> dict[str, Any]:
    """Import the plugin in Node and run `setup` against a stub registry."""
    node = node or shutil.which("node")
    if not node:
        return {"ok": None, "error": "node not found"}
    env = dict(os.environ, OZAGIA_DAEMON_SOCK="/nonexistent/ozagia-self-test.sock")
    try:
        # Node 20 reads a .js file outside an ESM package as CommonJS (Bun,
        # which runs OpenCode, does not), so the test imports an .mjs copy.
        with tempfile.TemporaryDirectory(prefix="ozagia-plugin-") as tmp:
            copy = Path(tmp) / "plugin.mjs"
            shutil.copyfile(path, copy)
            proc = subprocess.run([node, "--input-type=module", "-e", _SELF_TEST, str(copy)],
                                  capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=env)
        doc = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return {"ok": False, "error": f"self-test did not run: {exc}"}
    doc["ok"] = bool(doc.get("imported") and doc.get("id") == PLUGIN_ID
                     and doc.get("transformed") and doc.get("schema_swapped")
                     and doc.get("patch_untouched")
                     and doc.get("native_tools") == ["ozagia_structure"]
                     and doc.get("structure_readonly")
                     and sorted(doc.get("replaced") or []) == sorted(REPLACED_TOOLS))
    return doc


def _listed_row(output: str, path: Path) -> str | None:
    """The ID column of the `opencode plugin list` row for `path`, if any."""
    want = path.resolve()
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].startswith("/"):
            try:
                if Path(parts[2].strip()).resolve() == want:
                    return parts[0]
            except OSError:
                continue
    return None


def check_loads(workdir: str | Path, *, timeout: float = 60) -> dict[str, Any]:
    """Does OpenCode see the installed plugin, and does its setup work?

    Returns {ok, listed, output, attempts, row_id, self_test}. `listed` is true
    when `opencode plugin list` shows the plugin file with id `ozagia`. `ok`
    needs `listed` and a passing Node self-test (a missing Node is not held
    against it)."""
    workdir = Path(workdir).resolve()
    path = plugin_path(workdir)
    test = self_test(path, timeout=timeout) if path.is_file() else {
        "ok": False, "error": f"plugin not installed: {path}"}
    exe = shutil.which("opencode")
    output, row_id, attempts = "", None, 0
    if not path.is_file():
        output = f"plugin not installed: {path}"
    elif not exe:
        output = "opencode not found on PATH"
    else:
        env = dict(os.environ, PWD=str(workdir))
        deadline = time.monotonic() + timeout
        while True:
            attempts += 1
            try:
                proc = subprocess.run([exe, "plugin", "list"], cwd=str(workdir), env=env,
                                      capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                      timeout=max(1.0, deadline - time.monotonic()))
                output = (proc.stdout + proc.stderr).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                output = f"opencode plugin list failed: {exc}"
                break
            row_id = _listed_row(output, path)
            if row_id is not None or time.monotonic() + LIST_POLL_S >= deadline:
                break
            time.sleep(LIST_POLL_S)
    listed = row_id == PLUGIN_ID
    return {"ok": bool(listed and test.get("ok") is not False), "listed": listed,
            "row_id": row_id, "attempts": attempts, "output": output,
            "self_test": test}
