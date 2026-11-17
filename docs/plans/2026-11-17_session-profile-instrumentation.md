# Session profile instrumentation plan — 2026-11-17

1. Reuse the daemon's existing metadata-only journal for per-session Ozagia outcomes/durations, and persist sanitized handled/passthrough/error counters as each daemon operation is recorded so reports survive daemon teardown.
2. Subscribe on the OpenCode server plugin to `message.updated` and `message.part.updated`; persist a strict allowlist of model names, token counts, timing milestones, and tool outcome counts under `.ozagia/session_profiles/`, bounded to 200 sessions. Never persist prompt, source, tool input/output, or path content.
3. Add a bounded, read-only JSON profile command and OpenCode Desktop Profile tab plus TUI right-sidebar slot. Show graph/daemon status, host transcript metadata, Ozagia-observed counters, refresh/stale/unknown states, and no inferred critical path.
4. Add focused aggregation, SDK-shaped event extraction, and isolated TUI PTY checks; document interpretation and limits, build/typecheck the add-in, then commit this worktree.

Follow-up integration fixes: accept the daemon wire field `session`, serialize per-session updates across processes, keep profile replacement outside the daemon stats lock, and verify a live extracted event through daemon dispatch. The installed sidebar embeds the interpreter used by `ozagia integrations opencode install` so runtime import resolution does not depend on `python3` or inherited `PYTHONPATH`.

Scope: instrumentation and reporting only; no model execution, scheduling, or inferred critical path.

Verification limit: the installed OpenCode SDK schema and event API were inspected and represented in tests, but a host-generated message/part event was not injected because available session prompt APIs would invoke a model. The PTY smoke verifies ordinary installation and sidebar rendering only.
