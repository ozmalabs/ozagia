# Ocelot agent pilot, v4 (compound capture, permissions)

A rerun of the graph_intent arm on the 8 v2 tasks
(`evaluation/agent_e2e/tasks_v2.json`), 2 repeats, on branch
`v4/open-issues` at `969d0ba`. It ran on 16 Nov 2026 with OpenCode 2.3.0
and the local `vllm/qwen3.8-27b` ($0), with a 900 s timeout per session.
Raw output: `ocelot_agent_pilot_v4_run.{json,md}`. The v2 numbers are from
`ocelot_agent_pilot_v3_v2.json` (graph_intent arm, 3 repeats, `7f294ae`).

| | v2 (`7f294ae`) | v4 (`969d0ba`) |
|---|---|---|
| sessions | 24 | 16 |
| success | 24/24 | 16/16 |
| timeouts | 1 | 0 |
| mean wall s | 236.1 | 132.4 |
| median wall s | 126.7 | 97.1 |
| median tokens in | 63.4k | 78.1k |
| mean tool calls | 27.2 | 26.6 |
| share of calls the daemon handled | 43% | 73% |
| fallbacks | 0 | 0 |
| permission preflights | (none) | 140 |
| slowest daemon call | 589 s | 120 s |

- **Capture.** The daemon handled 73% of calls, up from 43%. It ran 85
  compound shell commands step by step (`shell_compound`); before v4 all of
  them went to the built-in. Every session's plugin found
  `ctx.permission.assert`. The daemon answered 140 preflights (103 shell,
  34 edit, 3 write) and the plugin asked OpenCode before each one.
- **Code Mode.** No replaced tool was reachable through Code Mode
  (`codemode_tools` was empty in every session).
- **What still falls back.** The remaining shell passthroughs are mostly
  code the model writes inline:
  - heredocs (21);
  - multi-line `python -c` (24);
  - one-line `python` commands (8);
  - plain `git` reads (13), which get `touched:` keys afterwards;
  - `ls` (8);
  - commands with no graph step (9).
- **Wall time.** Wall time fell, but these runs cannot say why. Most of the
  v2 mean came from captured pytest runs that hit the old 300 s default, or
  were given 600 s. That default has been 120 s since `74d2eb0`, the same
  as the built-in. Per task the two runs overlap; with 2 or 3 repeats, the
  numbers do not separate the effect of compound capture from noise.
  Tokens were higher.

A first attempt at this rerun was discarded. Its daemons ran the old `main`
code next to the new plugin, because the eval dropped `PYTHONPATH` for its
subprocesses. Every handled read answer then lacked a permission request,
so the plugin fell back 118 times. Commit `969d0ba` fixes the eval, and its
test covers it.
