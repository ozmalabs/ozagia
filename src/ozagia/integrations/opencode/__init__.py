"""OpenCode integration: plugins for the ozagia daemon and the home-screen brand.

`plugin.js` is the server plugin (plain ES module, `node:` built-ins only);
it hands built-in tool calls to the ozagia daemon. `tui_brand.js` is the TUI
brand plugin; it renders `[ + ozAGIa ]` (green) under the home-screen logo and
fails silently if the render tree it pokes at has changed.

`install.py` copies `plugin.js` to `<workdir>/.opencode/plugins/ozagia.js` and
`tui_brand.js` to `<workdir>/.opencode/plugins/ozagia/tui.js` (the TUI process
loads a plugin only from a `plugins/<name>/tui.js` subdirectory), and checks
that the server plugin loads. See `docs/eval_agent.md` (graph_intent arm).
"""
