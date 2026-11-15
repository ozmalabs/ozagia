"""OpenCode integration: a plugin that hands built-in tool calls to the ozagia daemon.

`plugin.js` is the plugin (plain ES module, `node:` built-ins only).
`install.py` copies it into `<workdir>/.opencode/plugins/ozagia.js`, where
OpenCode 2.3 loads project plugins, and checks that it loads. See
`docs/eval_agent.md` (graph_intent arm).
"""
