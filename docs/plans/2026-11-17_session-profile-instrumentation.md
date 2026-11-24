# Session profile instrumentation plan — 2026-11-17

Status: implemented in part, verified 2026-11-25. Daemon counters, the bounded
`.ozagia/session_profiles/` store, `ozagia-session-profile`, daemon
`profile_event` dispatch, the OpenCode subscriber/TUI sidebar and
metadata-only host-hook capture for Codex, Kiro and Claude Code exist in
`src/ozagia/session_profile_cli.py`, `src/ozagia/daemon/server.py`,
`src/ozagia/integrations/session_profile.py`,
`src/ozagia/integrations/opencode/plugin.js`,
`src/ozagia/integrations/opencode/tui_profile.js`,
`src/ozagia/integrations/{codex,kiro,claude_code}/hook.py` and
`tests/test_session_profile.py`/`tests/test_host_session_profiles.py`. Not
implemented: a host-generated OpenCode message/part event test that does not
invoke a model, live Codex/Kiro/Claude Code sessions, and new status/console
rows for host lifecycle counters (the existing status summary shape is
unchanged). The verification limit below still applies.

1. Reuse the daemon's existing metadata-only journal for per-session Ozagia outcomes/durations, and persist sanitized handled/passthrough/error counters as each daemon operation is recorded so reports survive daemon teardown.
2. Subscribe on the OpenCode server plugin to `message.updated` and `message.part.updated`; persist a strict allowlist of model names, token counts, timing milestones, and tool outcome counts under `.ozagia/session_profiles/`, bounded to 200 sessions. Never persist prompt, source, tool input/output, or path content.
3. Add a bounded, read-only JSON profile command and OpenCode Desktop Profile tab plus TUI right-sidebar slot. Show graph/daemon status, host transcript metadata, Ozagia-observed counters, refresh/stale/unknown states, and no inferred critical path.
4. Add focused aggregation, SDK-shaped event extraction, and isolated TUI PTY checks; document interpretation and limits, build/typecheck the add-in, then commit this worktree.

Host hook capture (2026-11-25): `integrations/session_profile.py` sends at
most one `profile_event` per hook invocation, through the existing daemon
operation and store. Codex, Kiro and Claude Code record session
start/end/compact, tool started/terminal outcomes, host-supplied numeric
durations and model names. No prompt, source, path, command, tool input/output
or secret value is sent; hooks without a host tool id use an opaque digest for
replay dedupe (two identical Kiro calls in the same session share a digest and
count once). A `started` count includes host attempts that a hook then
refuses (for example a ref-moving git command), so started and terminal counts
need not match. Missing daemons and malformed payloads are ignored, so a profile
cannot block a tool call. None of these hook payloads currently carries token
counts or turn timestamps, so token and turn timing fields stay `null` rather
than being estimated. Kiro's session-end hook is registered for the V3 engine
only; the CLI 2.x engines have no session-end trigger. `ozagia-status`'s
existing session summary reads these profiles without a shape change.

Follow-up integration fixes: accept the daemon wire field `session`, serialize per-session updates across processes, keep profile replacement outside the daemon stats lock, and verify a live extracted event through daemon dispatch. The installed sidebar embeds the interpreter used by `ozagia integrations opencode install` so runtime import resolution does not depend on `python3` or inherited `PYTHONPATH`.

Scope: instrumentation and reporting only; no model execution, scheduling, or inferred critical path.

Verification limit: the installed OpenCode SDK schema and event API were inspected and represented in tests, but a host-generated message/part event was not injected because available session prompt APIs would invoke a model. The PTY smoke verifies ordinary installation and sidebar rendering only.
