# Proactive agent context

The Ocelot pilot showed that graph capture alone did not provide the task to
retrieval before the first model call. The graph arm made more calls and emitted
more tool output; a one-task run cannot establish causation. The goal is to
make task and recent conversation useful as retrieval hints while retaining
bounded, explainable context and safe fallback.

1. Build a bounded, read-only hint from the current request and limited recent
   user/assistant history, using existing lexical/graph retrieval. Keep only
   source-attributed candidates; deduplicate and enforce a limit on the whole
   rendered hint, not merely source excerpts. Report stale/unavailable state
   instead of presenting it as current. Never record raw prompt or history in
   the daemon journal.
2. Add a local daemon operation and wire OpenCode's request-time session
   context hook: gather bounded user/assistant text, send a hint request,
   attach the response only to the outgoing model context, and fall back
   unchanged on absence, timeout or malformed input. Do not assume tool output
   is trusted instruction or persist injected context in conversation history.
3. Expand safe shell call recognition only where command semantics and
   permission/error behavior can be preserved; otherwise leave the built-in
   call alone. Avoid doing a full graph refresh for proven read-only calls.
4. Test session isolation, stale snapshots, hard output bounds, absent daemon,
   malformed inputs, command semantics and reingest. Run deterministic
   retrieval checks and the project suite; measure real token and end-to-end
   impact separately. No live model run without the repository's approval
   gates.

Implemented with one fused search per hint and a 1,600-character source
budget. The OpenCode plugin caches hints briefly and invalidates them after
known or possible mutations; changes outside the plugin can take up to the
cache and freshness-check intervals to become visible. The character budget is
only an estimate of model tokens, and the hint is sent again on subsequent
model calls. A controlled live comparison is still needed to determine whether
the extra context reduces tool calls, total tokens, or elapsed time.
