# Dev tooling v3: capture the agent's tool calls and carry out their intent on the graph

Status: historical/re-homed plan (2026-11-15). The work moved to a separate
public project, now Ozagia; see [`docs/roadmap.md`](../roadmap.md) for current
work. This document's
W0 probes and intent-mapping table remain the reference for that plan.

## Why

In the v2 pilot (`evaluation/agent_e2e/pilot_opencode.md`), the dev_memory agent
had the Ocelot MCP server and its built-in tools. It called the server in 1 of 7
sessions and solved every task with grep, read and shell. A model given simple
linear tools uses them, and a 43-tool server it has to learn does not change
that.

v3 keeps the simple tools and changes what they do. The agent calls `read`,
`grep`, `edit` and `shell` exactly as it already does. A plugin captures each
call, works out its intent, and carries it out on the repository graph. The
result comes back in the tool's own shape, enriched with graph structure: node
keys, callers, tests, what an edit broke. The LLM does graph cognition through
an interface it already knows, and the graph stays exact underneath it.

Both arms solved 7/7 in v2, so v3 also needs harder tasks. Otherwise any
difference stays hidden at the ceiling.

## What was verified before planning (W0, done)

All probes on OpenCode 2.3.0 with the local `vllm/qwen3.8-27b`, in scratch
repos under `/tmp`:

1. **A v2 plugin can replace a built-in tool.** A plugin at
   `<project>/.opencode/plugins/<name>.js` loads in `opencode run --standalone`.
   (Listing an absolute path under `plugins` in `OPENCODE_CONFIG` did not load it.)
   In `setup(ctx)`, `ctx.tool.transform(reg => reg.update(name, t => {...}))`
   can set `t.execute`, `t.output` and `t.description`. With `read`, `grep`,
   `shell` and `edit` replaced, every call went to the plugin's `execute` with
   the model's input unchanged (`{path}`, `{pattern}`, `{command}`,
   `{path, oldString, newString}`), and the agent quoted the plugin's result as
   the tool's output. The rewritten description reached the model.
2. **Each built-in validates its output schema.** A string result from a replaced
   `read`, `grep` or `shell` failed with "Tool returned an invalid value for its
   output schema". It worked after setting `t.output` to the string schema taken
   from `reg.get("edit").output`. The plugin must swap the output schema of
   every tool it replaces.
3. **Hooks work too.** `ctx.tool.hook("execute.before", e => ...)` receives
   `{tool, input, ...}`, and changing `e.input.path` changed what `read` opened.
   `execute.after` receives `{tool, input, status, result | error}`. v3 uses
   replacement, not hooks, because replacement controls the whole result.
4. **The registry** holds `patch`, `edit`, `glob`, `grep`, `read`, `shell`,
   `write`, `skill`, `subagent`, `question`, `webfetch`, `websearch`,
   `experimental_image_generate` and the `opencode_*` session and worktree tools.
5. **Placement is safe.** The repository ignores `.opencode/` (`.gitignore:13`),
   so a plugin written into a workdir is not an agent edit and is not committed.
6. **Denying the built-ins** (the previous version of this plan) also works:
   permission deny blocked all file access, including from inside Code Mode
   `execute`. It stays available as a control. Capture replaces it as the
   treatment.

Not yet verified: whether a built-in called from inside Code Mode `execute`
(`tools.read(...)`) goes through the replaced `execute`. The W2-B smoke test
checks this.

Resolved in v4 (`docs/plans/2026-11-16_v4_open_issues.md`): in 2.3.0 every
built-in is registered `codemode: false`, so Code Mode's catalog (built from
the registry after transforms) never contains one, and `execute` cannot call
it. The plugin reports each replaced tool's `codemode` option in its
`hello`, and the eval warns if one is not `false`.

## Intent mapping

The plugin is thin. It forwards `{tool, input, session}` over a Unix socket to
a **graph daemon**: a long-lived Python process per session that holds
`DevMemory`, the symbol graph and the search index warm. One call costs one
local round trip, not a Python start (9.6 s cold vs 0.25 s warm in the v2 bench).

| captured call | intent | what the daemon does | what the agent gets back |
|---|---|---|---|
| `read(path)` of a source file | understand this file | Resolve the file node and its symbol nodes. | The file's text with line numbers, **verbatim**, split at symbol boundaries. Each symbol has a header line with its key, callers, callees and tests. Large files: an outline with keys, plus the bodies of symbols the session has touched (search hits, failure frames, edits). |
| `read(path, offset, limit)` | look at this part | Map the range to the nodes covering it. | Those nodes, verbatim, with headers. |
| `read("sym:...")`, `read("test:...")` | look at this node | The node, keyed. | The node's source and its neighbours. Node keys are accepted wherever a path is (the description says so). |
| `read` of an unparsed file (YAML, TOML, shell, text) | read it | Text-chunk nodes. | Verbatim text with chunk keys. |
| `grep(pattern, path?, include?)` | where is X | Run the real regex (ripgrep), then group matches by enclosing node. An identifier-like pattern also adds its definition and references from the symbol graph, and the code-search ranking. | Matches grouped under node keys, definitions first. Every real match is kept; graph additions are labelled. |
| `glob`, `list` | what is here | File and directory nodes. | Paths with a one-line role: symbol count, covering tests, importers. |
| `edit(path, oldString, newString)` | change this code | Find the span. Map it to the enclosing node. Apply through the edit transaction (atomic, base hash). Re-parse; if the edit breaks the parse, refuse and return the node with the error. Reingest the file. | `Edited <key>` plus the **graph delta**: changed, added and removed keys, references now broken (callers of a changed signature, imports of a removed name), and the tests that cover the changed nodes. |
| `write(path, content)` | create or replace a file | Write through the transaction, reingest. | New keys and the delta. |
| `patch(...)` | several edits | Each hunk as an `edit`. | One delta for all hunks. |
| `shell` running tests (`pytest`, `python -m pytest`) | does it work | Run with `--junitxml`, map failure frames to symbol keys. | Per-test pass and fail, the assertion, the failing frames as keys, and the raw output tail. |
| `shell` reading or searching (`cat`, `head`, `tail`, `sed -n`, `wc`, `grep`, `rg`, `find`, `ls`, `git grep`) | as `read`, `grep`, `glob` | Parsed with `shlex`, mapped to that intent. | As the mapped tool. |
| `shell` editing in place (`sed -i`, `perl -pi`, a redirect `>` into a tracked file) | change files | Run it, find the files that changed, reingest them. | Raw output plus the delta. |
| `shell` read-only git (`status`, `diff`, `log`, `show`, `blame`) | what changed | Run it. Map diff hunks to node keys. | Raw output plus the keys touched. |
| `shell` ref-moving git (`commit`, `stash`, `reset`, ...) | (not allowed) | Refuse, as today. | The refusal. |
| any other `shell` (`python -c`, scripts) | run this | Run it in the workdir with the run's Python, as today. Then reingest any files that changed. | Raw output, plus the delta if files changed. |
| `webfetch`, `websearch`, session tools | not about the repo | Not captured. | Unchanged. |

Rules for every mapping:

- **Verbatim source.** Any text the agent may copy into `oldString` is the
  file's exact text. The graph adds header lines and never rewrites source
  lines, so the model's edit habits keep working.
- **No lost information.** A captured call never returns less than the
  built-in would have (all regex matches, all output, raw shell output),
  except that oversized results are cut with the same limits the built-ins use.
- **Graph additions are labelled** so the agent can tell them from file content.
- **Fallback.** If the daemon is down or a mapping raises, the plugin calls
  the original built-in `execute` (kept from `reg.get(name)` before the update)
  and adds a one-line notice. The run records each fallback, so a broken
  daemon shows up as a result, not as a silent baseline.

## Gaps in the current tooling

| ID | Gap | Where now |
|---|---|---|
| G1 | The snapshot is whole-repo and goes stale on the first edit (`staleness()` compares a tree digest). Every captured edit needs the graph updated in under a second. | `ocelot_dev_memory.py` `build_index`, `staleness` |
| G2 | Node ids are integers from one build and change on rebuild. Keys shown to the agent must survive edits. | `dev_node`, `dev_search` results |
| G3 | Edits are addressed by file plus a named op. Nothing maps a text span to its enclosing node, and nothing returns a graph delta or broken references. | `ocelot_code_edit.py`, `ocelot_edit_txn.py` |
| G4 | Test output is a text log. Nothing maps failures to symbols. | none |
| G5 | Files with no structural parser (YAML, TOML, shell, text) have no nodes. | tag languages, edit modules |
| G6 | No persistent process outside MCP. The MCP server is stdio and per client; the plugin needs a socket. | `ocelot_dev_memory_mcp.py` |

Keys:

| kind | key |
|---|---|
| file | `file:scripts/ocelot_architecture.py` |
| symbol | `sym:scripts/ocelot_architecture.py::_summary` (methods `Class.method`) |
| test | `test:tests/test_architecture_migrated.py::test_hub_ordering_uses_pagerank_then_in_degree` |
| doc section | `doc:docs/features/X.md#heading/sub-heading` |
| JSON value | `json:path/file.json#/json/pointer` |
| text chunk | `text:path/file.yaml#L10-L42` |
| seed, rule | `seed:<id>`, `rule:<seed>/<name>` |

## Waves, tasks and write scopes

Same process as v2: each task runs as an `ocelot-substrate-executor` in its own
worktree and branch cut from `dt/v3`, with disjoint write scopes inside a wave.
The supervisor merges, runs the combined gate and cuts the next wave. The v2
shared rules apply (dev tooling, not the agent runtime; no new third-party
dependencies; executors never commit). No changes to `runners/dsl` or `seeds/`.
The plugin is plain JavaScript with no npm dependencies, using only `node:`
built-ins that OpenCode's runtime provides.

### Wave 1: a graph that stays live (parallel)

**W1-A Stable keys and incremental reingest** (G1, G2).
Keys on every node with a key→id index. `reingest(paths)` re-parses only the
given files and updates their nodes, edges, symbol-graph rows and code-search
side rows (already cached by per-file sha256), and returns the delta.
`staleness()` reports fresh after every changed file is reingested.
Scope: index and staleness parts of `scripts/ocelot_dev_memory.py`,
`scripts/ocelot_symbol_graph.py`, the cache entry points of
`scripts/ocelot_code_search.py`; tests in `test_dev_memory_migrated.py`,
`test_symbol_graph_migrated.py`, new `tests/test_graph_reingest_migrated.py`.
Targets: reingest of one file of up to 2,000 lines in ≤ 0.5 s warm. An
equivalence test: after a set of edits plus reingest, the graph equals a full
rebuild, apart from build timestamps.

**W1-B Span→node edits and graph deltas** (G3).
`locate(path, old_string)` returns the unique span and its innermost enclosing
node (same uniqueness rule as OpenCode's edit: zero or several matches is an
error that lists the candidate nodes). `apply_span_edit` writes through
`ocelot_edit_txn`, refuses an edit that breaks the parse of a Python, JSON or
Markdown file, and returns the delta from W1-A's `reingest`, plus broken
references: callers whose call arity no longer fits a changed signature, and
imports of a removed or renamed name.
Scope: new `scripts/ocelot_span_edit.py`, new `tests/test_span_edit_migrated.py`.
Depends on W1-A's `reingest` interface, which W1-A publishes first as a stub
signature in its brief.

**W1-C Test runs and text nodes** (G4, G5).
`scripts/ocelot_graph_check.py`: run pytest selectors with `--junitxml` (stdlib
`xml.etree`), map traceback frames to the innermost enclosing symbol key, and
return per-test results. It uses a time limit and a process-group kill.
`scripts/ocelot_text_nodes.py`: chunk files with no structural parser into
blocks of at most 60 lines split at blank lines, keyed by line range.
Scope: the two new modules, new `tests/test_graph_check_migrated.py`, new
`tests/test_text_nodes_migrated.py`.

**W1-D Harder tasks.**
About 8 new break/fix tasks in `evaluation/agent_e2e/tasks_v2.json`, on
`scripts/` and `domains/`:
- prompts that give a symptom only and name neither the test nor the file;
- at least 3 breaks spanning 2 or more files (a renamed helper with stale
  callers, a signature change, a constant moved between modules);
- at least 1 break whose right fix is in a caller, not where the error shows;
- at least 1 task touching JSON or Markdown alongside Python.
Each must pass `validate`.
Scope: `evaluation/agent_e2e/tasks_v2.json`, `evaluation/agent_e2e/patches_v2/`.

### Wave 2: daemon, plugin and arm (parallel, after W1)

**W2-A Graph daemon and intent mapping** (G6).
New `scripts/ocelot_graph_daemon.py`: one process per workdir, JSON lines over
a Unix socket under the run's log dir, holding `DevMemory` warm. A method per
captured tool implements the mapping table, using W1-A to W1-C. The shell
classifier uses `shlex` and maps only the command forms in the table; anything
else runs as-is. It writes a per-call journal (tool, intent, latency, fallback)
to the log dir.
Scope: new module, new `tests/test_graph_daemon_migrated.py` (mapping tests use
recorded OpenCode inputs from the W0 probes), new `docs/features/GRAPH_INTENT.md`.
Targets, warm: captured `read` ≤ 0.2 s, `grep` ≤ 0.5 s, `edit` with reingest
≤ 1 s. Every path and key is checked to be inside the workdir.

**W2-B OpenCode plugin and the `graph_intent` arm.**
New `tools/opencode/ocelot-graph-intent.js`: in `setup`, connect to the
daemon. Through `ctx.tool.transform`, replace `read`, `grep`, `glob`, `edit`,
`write`, `patch` and `shell`: keep each original `execute` for the fallback,
set `output` to the string schema, and add one line to the description
(node keys are accepted as paths, results carry graph headers). The harness
gets a new arm, `graph_intent`. It runs dev_memory prep, starts the daemon,
copies the plugin into `<workdir>/.opencode/plugins/`, and does **not**
register the MCP server, so the agent's tool list matches baseline. The
preflight checks that the OpenCode version is 2.3.x and that the plugin loads,
by running a no-model plugin self-test. The run record gains `captured_calls`
by tool and intent, `fallbacks`, and `daemon_ms`.
Scope: the plugin, `scripts/ocelot_agent_eval.py`,
`tests/test_agent_eval_migrated.py`, `docs/features/AGENT_E2E_EVAL.md`.

Claude Code is out of scope for v3. Its PreToolUse hook can rewrite or deny a
built-in call, but replacing a built-in's result is not verified. It is also
untestable here until Claude is logged in.

### Wave 3: supervisor

1. Merge into `main`, run the combined gate, semantic review of the v3 diff,
   fix blockers.
2. Smoke: `graph_intent` on 1 v1 task and 1 v2 task. From the transcript and
   the daemon journal, check that every repo tool call was captured, fallbacks
   are 0, `pytest` became a check result, and a call from inside Code Mode
   `execute` was captured (the open W0 item).
3. Pilot: 3 arms (baseline, dev_memory, graph_intent) × 15 tasks (7 v1 + 8 v2)
   × 3 repeats = 135 sessions on the local model, $0. At the v2 pilot's
   ~40 s per session plus ~64 s prep, about 3 hours.
4. Record results here and in `evaluation/agent_e2e/`.

## What the pilot measures

Per arm and task set: success rate, localization, wall time, input and output
tokens, tool calls. For graph_intent also: captured calls by intent, fallbacks,
daemon latency, and how often the agent used a node key it was shown (keys in
inputs, read from the daemon journal). That last number shows whether the
agent is working on the graph or only receiving it.

Questions the numbers answer:
- Does graph_intent solve the v2 tasks more often than baseline?
- Does it get there in fewer calls and fewer input tokens?
- Does it reach multi-file fixes faster, because an edit's delta names the
  broken callers?
- Where it fails, is the cause a wrong mapping, a coverage gap (a text-only
  file), a wrong graph (an equivalence bug), or the model? Every failed
  graph_intent transcript gets one of these labels.

## Risks

- **Plugin API churn.** OpenCode's v1-to-v2 plugin change broke existing
  plugins, and v2 behaviour has shifted between minor versions. The preflight
  pins 2.3.x and runs the plugin self-test, and the fallback keeps a session
  usable if a hook breaks.
- **Edit fidelity.** If any captured `read` changes source text, the model's
  `oldString` stops matching. Verbatim source is a tested invariant, with a
  test that round-trips every W0 read through an edit.
- **Graph errors become agent errors.** A wrong edge or delta misleads the
  agent. The W1-A equivalence test and labelled graph additions are the guard;
  the raw matches and output are always present.
- **Shell classification.** A misclassified command can do the wrong thing.
  Only the listed command forms are mapped. Everything else runs unchanged,
  and changed files are found by snapshot, not by guessing from the command.
- **Model capacity.** A 27B local model may ignore graph headers. That shows up
  as a low key-use rate, not as a failure. A hosted model would test it, at a
  cost that needs approval.

## Open questions

1. Models: local `vllm/qwen3.8-27b` only, or also one hosted model (OpenAI is
   configured in OpenCode) for baseline and graph_intent?
2. Large-file `read`: this plan returns an outline plus the bodies the session
   has touched. The alternative is the full verbatim file with headers, which
   costs tokens but never hides text. Which do you want as the default?
3. Interactive use: the plugin could also run in your normal OpenCode
   sessions, with a daemon per project. This plan builds and evaluates it
   only in the harness, and does not change your global config.
