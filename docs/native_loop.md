# Native agent loop

`src/ozagia/harness/native.py` is a small agent loop over an OpenAI-compatible
chat API. The model gets the six tools OpenCode 2.3 has, with OpenCode's input
field names, so the same model can be run with and without the graph behind the
same tool shapes. The built-in tools are in `src/ozagia/harness/tools.py`.
Tests: `tests/test_harness_native.py` (a local stub server; no model is called).

```
python -m ozagia.harness.native run --workdir W --prompt-file P --mode plain|graph \
    --base-url U --model M [--api-key-env NAME | --api-key-file F] [--dump D] \
    [--log-dir L] [--max-steps N] [--timeout S] [--temperature T] [--system-prompt-file F]
```

It prints `summary.json` on stdout and exits with the summary's `exit_code`
(2 for a bad key or prompt argument). The eval harness runs it as the `native`
driver (`docs/eval_agent.md`).

## Modes

- `plain`: the built-in tools only.
- `graph`: each call goes first to `ozagia.intent.handle(memory, tool, input,
  harness="native")` in process. A handled call returns the intent layer's
  output; a handled call with `is_error` is a tool error. A call it does not
  handle (`handled: false`) runs the built-in, then `ozagia.intent.after(...)`,
  and its `append` text is added after the built-in's output on a new line.
  The graph is `DevMemory` from `--dump`, built there if missing
  (`ozagia.daemon.server.load_memory`). The loop sets `OZAGIA_ROOT` to the
  workdir before `ozagia.core.memory` is imported.

In graph mode every tool description gets one sentence saying that `[ozagia]`
lines are graph context, and `read`'s says the path may be a node key.

## Tools

| tool | input | output |
|---|---|---|
| read | `path, offset?, limit?` | `Read file <abs>, lines <a>-<b>[ of <N>]` then `<n>: <line>`; a directory: `Read directory <abs>, entries <a>-<b>[ of <N>]`, one name per line, `/` after directories. Default 2000 lines. |
| grep | `pattern, path?, include?, caseSensitive?` | `Found <N> matches[ (showing first 100)]`, then per file (newest first) `<abs>:` and `  Line <n>: <text>`, a blank line between files; `No files found`. ripgrep, or a Python `re` walk without `rg` (the search of `ozagia.intent.search`). `caseSensitive: false` is `-i`. |
| glob | `pattern, path?` | absolute paths, newest first, at most 100; `No files found` |
| edit | `path, oldString, newString, replaceAll?` | `Edit applied successfully.` Errors: `oldString not found in content`, `Found multiple matches for oldString. Provide more surrounding lines in oldString to identify the correct match.`, `oldString and newString must be different`. An empty `oldString` writes `newString` as the file. |
| write | `path, content` | `Wrote file successfully.` (parent directories made) |
| shell | `command, workdir?, timeout? (ms), description?` | combined stdout and stderr; `(exit code N)` after a non-zero exit; `(no output)` when empty |

Paths are absolute or relative to the workdir and must resolve inside it.

`shell` runs `bash -c <command>` in the workdir (or `workdir`) in its own
process group. The timeout defaults to 120 000 ms and is capped by the time the
run has left. On timeout the group gets SIGTERM, then SIGKILL after 2 s, and the
output ends with `(Command timed out after <ms> ms; its process group was
killed)`. Output longer than `MAX_SHELL_OUTPUT_CHARS` (30 000) keeps its last
30 000 characters after the line `(output truncated: showing the last 30000 of
<N> characters)`. The shell's environment is the loop's without `PYTHONPATH`,
without `OZAGIA_NATIVE_*` (one of them names the key file) and without any
variable whose value contains the API key.

Both modes refuse ref-moving git (`intent.shell.classify(...)["class"] ==
"git_refs"`) with the tool error `Refused by ozagia: <command> changes git
refs`. `edit` and `write` refuse paths inside `.git`. In graph mode a captured
pytest gets the same default timeout (120 000 ms), the same cap by the run's
remaining time and the same environment as the plain shell (the intent
layer's reserved `_deadline_s_remaining` and `_test_env` input fields).
Reingests during the run do not rewrite the snapshot; it is written once
after the loop.

## The loop

1. Messages start with a system prompt (the workdir; fix the problem, verify with
   tests, no git commits; `--system-prompt-file` replaces it) and the user prompt.
2. `POST {base_url}/chat/completions` with `model`, `messages`, `tools` (JSON
   schemas of the six tools), `tool_choice: "auto"` and `temperature` if given.
3. A reply without tool calls ends the run (`stopped: "done"`, its `content` is
   `final_text`). Otherwise the assistant message is appended, every tool call
   runs in order, and each result is appended as `{"role": "tool",
   "tool_call_id", "content"}`. A tool error is sent as `Error: <text>`.
   Arguments that are not a JSON object, and unknown tool names, are tool errors.
4. Repeat until `max_steps` model turns (default 100, `stopped: "max_steps"`) or
   `timeout_s` seconds (default 900, `stopped: "timeout"`) are used up.

HTTP: 429 and 5xx are retried, 3 attempts in all, waiting 1 s then 2 s. 401 and
403 are reported as `authentication failed (...)`. Another 4xx, an unreachable
server, a reply that is not JSON or has no `choices[0].message`, or a graph that
cannot be loaded stops the run with `stopped: "error"` and `exit_code: 1`. A
request waits at most for the time the run has left.

## Output

`log_dir` (default: a new `ozagia-native-*` temp dir) gets:

- `transcript.jsonl`: one object per step (model turn): `step`, `ms`, `usage`,
  `finish_reason`, `content`, and `tools`, one entry per call with `id`, `name`,
  `input`, `output` (as sent to the model), `output_chars`, `ms`, `is_error`, and
  in graph mode `handled`, `fallback` (the intent layer raised), `intent`, and
  for unhandled calls `reason` and `after_intent`.
- `summary.json`: `exit_code` (0, or 1 for an error), `steps`, `tokens_in`,
  `tokens_out` (sums of `usage.prompt_tokens` and `usage.completion_tokens`),
  `cost_usd` (sum of `usage.cost` or a top-level `cost`; `null` when the server
  sends none), `tool_calls`, `tools_by_name`, `captured_calls`,
  `passthrough_calls` and `fallbacks` (graph mode: calls the intent layer
  handled, calls it left to the built-in by design, and calls where it raised
  so the built-in ran; `null` in plain mode), `stopped`, `error`,
  `final_text`, `mode`, `model`, `wall_s`, `log_dir`, and in graph mode
  `snapshot_write_s` (the one snapshot write at the end) or `snapshot_error`.

The API key is sent as `Authorization: Bearer <key>`. It is read from the named
environment variable or the file, never taken on the command line, and any
occurrence of it is replaced by `***` in both files. The agent's shell can read
any file the loop can, so a key file must not be readable by the agent (keep it
outside the workdir under another user, or pass the key in an environment
variable, which the shell does not get). A `http://` base URL to a host other
than loopback prints a warning on stderr: the key is sent unencrypted.
