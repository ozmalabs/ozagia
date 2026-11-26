# What local agent sessions say ozagia should do next

Status: historical input (2026-11-16). This is the source analysis for
[`2026-11-16_usage_features_plan.md`](2026-11-16_usage_features_plan.md); its
feature waves are implemented, but the claimed savings remain unmeasured.

Date: 16 Nov 2026. Source: this machine's OpenCode, Kiro and Codex session
history, mined with throwaway scripts (`/tmp/mine/`, not in the repository).
Only aggregate counts are recorded here; no prompt text, file contents or
paths from those sessions.

## Corpus

| | OpenCode | Kiro | Codex |
|---|---|---|---|
| sessions (eval and probe sessions excluded) | 131 (68 of them subagents) | 26 | 499 in 356 rollout files (303 of them subagents) |
| user prompts | 405 | 1,362 | 1,514 |
| tool calls | 13,788 | 22,117 | 237,652 |
| tool calls per user prompt | 34.0 | 15.6 | 157.0 |
| failed tool calls | 106 (0.8%) | 1,148 (5.2%) | 4,466 (1.9%) |

The 175 OpenCode eval sessions (`/tmp/ozagia_pilot/...`, probes) are
excluded; they are measured in `docs/results/`. The Codex history had no eval
sessions.

Codex runs 87% of the 273,557 tool calls here, Kiro 8% and OpenCode 5%.
ozagia integrates with neither Codex nor Kiro today.

Codex tool calls are JavaScript cells that call `exec_command` (133,304),
`write_stdin` (51,005) and `apply_patch` (38,518), plus the functions `wait`,
`wait_agent` and `sleep`. Codex reads files through the shell (`sed -n`,
`nl -ba`, `cat`, `head`, `tail`), so its reads are counted from commands.

## Findings

Ranked by how much agent time or context they cost.

1. **Re-reading what is already in context.**
   - Codex read files through commands 82,524 times. 43,119 of those were
     re-reads of a file with no edit and no compaction since the previous
     read, and 2,256 asked for the same range. Another 12,806 re-reads came
     right after a compaction.
   - The median gap between the two reads is 1 tool call (p75: 4, p90: 21).
     Most re-reads are an agent paging through one large file with
     `sed -n 'a,bp'`.
   - 938 file and session pairs were read 10 times or more, 164 of them 50
     times or more. The worst cases are long Markdown plans.
   - OpenCode and Kiro add 3,032 unchanged re-reads (570 same-range) and 264
     after compaction.
   - Large outputs: 7,571 Codex commands returned more than 20,000
     characters, and 763 OpenCode and Kiro reads did. OpenCode truncated 867
     tool outputs.
2. **Context pressure and compaction.**
   - Codex compacted 1,485 times in 190 sessions; one session compacted 476
     times, and 10 sessions compacted 10 times or more.
   - 60 of 354 Codex sessions with context data passed 80% of the window, and
     10 of 26 Kiro sessions did.
   - OpenCode compacted 86 times in 10 sessions. Its logs show compaction
     summaries failing at the output-token limit.
3. **Patches against stale text.**
   - 3,854 Codex `apply_patch` calls failed (10.0%). 3,532 of the failures
     are "failed to find expected lines": the patch context no longer
     matched the file.
   - The failure rate depends on the file type: 6.9% for `.py`, 8.1% for
     `.rs`, 16.5% for `.md` and 22.5% for `.tex`. Prose files change more
     between reads and have fewer unique anchor lines.
   - After a failure the agent re-read the file 2,717 times (`sed` 1,485,
     `tail` 552, `rg` 467), retried the patch 1,005 times and rewrote the
     file from Python 226 times. 102 failure runs reached 3 or more misses in
     a row, 7 at most.
   - OpenCode `edit` (26) and Kiro `str_replace` (103) misses are the same
     problem at a smaller scale. ozagia already hands an exact OpenCode miss
     to its tolerant edit; Kiro and Codex have no such path.
4. **Waiting and polling.**
   - 44,938 of the 51,005 Codex `write_stdin` calls send no input: they
     poll a running process for output.
   - Codex also called `wait` 5,914 times, `wait_agent` 4,089 times and
     `sleep` 1,020 times, and ran `ps`/`pgrep` 6,099 times.
   - 4,475 runs of 3 or more consecutive polling calls account for 37,181
     calls; the longest run was 209 calls.
   - OpenCode and Kiro: 1,353 shell `sleep` calls requesting 139 hours.
5. **Ad-hoc Python and `jq` instead of tools.**
   - Codex ran inline Python 30,704 times. 18,317 of those load JSON, and
     3,053 write files outside `apply_patch`. Another 4,681 commands use
     `jq`.
   - OpenCode and Kiro: 4,544 inline-Python calls, 1,773 of them JSON
     inspection and 969 file rewrites.
   - Heredocs are the largest reason a shell command can't be captured:
     27,196 in Codex and 2,734 in the others.
6. **Test loops.**
   - Codex: 8,458 commands ran pytest, 407 of them whole-suite runs with no
     selector. 228 sessions ran tests, a median of 4.5 runs per session and
     2,090 in one session. There were 783 `cargo build/test/check/clippy`
     runs.
   - OpenCode and Kiro: 90 sessions ran tests (median 8 runs, 273 at most),
     210 whole-suite runs and 339 separate `ruff` runs.
7. **Shell forms still passed through.**
   - Codex, as reasons given by `classify`:

     | form | commands |
     |---|---|
     | heredoc | 27,196 |
     | python command | 11,854 |
     | `rg` over several paths | 10,995 |
     | `ps` | 5,040 |
     | `jq` | 3,954 |
     | does not tokenise | 3,203 |
     | environment assignment prefix | 2,907 |
     | `rg --files` | 2,495 |
     | `rg --glob` | 2,077 |
     | `find` | 1,049 |
     | `nl` | 1,040 |
     | `cargo` | 502 |
     | `rg -C` | 483 |
     | `tail -c` | 387 |
     | `rustfmt` | 269 |

   - 31,892 Codex commands are compound forms that v4 already captures,
     which ozagia could take over once it has a Codex integration.
   - OpenCode and Kiro: 1,161 subshells, 707 `sudo`, 533 bare environment
     assignments, 393 multi-path greps, 335 `timeout N` prefixes, 304 `for`
     loops, 140 `cd ~`, and pipes into `cut`, `wc` and `sort`.
8. **Orientation commands.** Codex ran 13,374 `git diff`/`status`/`log`/`show`
   commands; OpenCode and Kiro ran 2,383, plus 2,220 `ls`/`find`/`tree` and
   907 `wc -l`. The agent keeps re-establishing where things stand, at
   session start and after compactions.
9. **Wrong paths.** 531 "file not found" errors in OpenCode and Kiro (524 in
   Kiro); 110 of them had a close match in the same directory and 160 had a
   missing directory (often worktree and main checkout confused). Codex had
   only 34, because it finds files with `rg --files` first.
10. **Operations (SRE) work outside the IaC adapter's reach.** In OpenCode and
    Kiro: 1,087 podman/docker commands, 972 `curl` checks, 273 `gcloud`, 222
    `systemctl`/`journalctl`, 123 `nvidia-smi`. Codex adds 830 `podman`
    commands. Edited files include Dockerfiles, compose YAML, `.service`
    units and shell scripts, none of which the IaC adapter reads.
11. **Web fetch failures.** 21.6% of OpenCode `webfetch` calls and 17.7% of
    Kiro's failed: 404s, and PDFs the tool can't read.
12. **What users correct.**
    - 129 OpenCode and Kiro prompts and 96 Codex prompts are some form of
      "continue": the agent stopped before the work was done. Users
      interrupted Codex 142 times.
    - About 60 OpenCode and Kiro prompts and 79 Codex prompts read as
      corrections. In OpenCode and Kiro most are about meaning in research
      and writing (the wrong lesson, a missed point), not code.
    - About 45 ask for a review, often a re-review that lists the issues
      still outstanding.
13. **Concurrent sessions.** Several sessions worked in the same checkout
    with each other's uncommitted changes in place. Merges were blocked and
    files were edited by two agents. Nothing tells an agent that another
    live session owns a dirty file.

## Features, in priority order

Effort: S is a day or less, M is a few days, L is a week or more.

| # | feature | addresses | effort |
|---|---|---|---|
| 1 | **Codex integration.** Codex loads MCP servers from `~/.codex/config.toml` (`[mcp_servers]`). Add `ozagia install codex`: register the MCP server and write `AGENTS.md` guidance so the agent reads, patches and waits through ozagia tools instead of `sed -n`, `apply_patch` and polling. Before designing anything further, check whether the installed Codex version has a command-hook API that would let ozagia attach `after` context to `exec_command` and `apply_patch` the way it does for Claude Code. | Codex is 87% of tool calls here and gets no graph (findings 1, 3, 4, 7) | M |
| 2 | **Read ledger and structure-first reads.** Track what each session has read (file hash, ranges). An unchanged same-range repeat with no compaction since gets a one-line "unchanged since call N" with keys. A read of a large file with no range gets an outline first: symbols, Markdown and TeX sections with line ranges and keys, plus the first chunk. Reading a key returns the whole node, so paging stops. For Codex, map `sed -n`, `nl -ba`, `head` and `tail` reads to the same ledger. | 46k unchanged re-reads, 938 files read 10+ times in one session, 8k outputs over 20k characters (finding 1) | M |
| 3 | **Patch miss recovery.** When a patch or edit fails to find its context, return the current text of the nearest matching region (fuzzy match on the expected lines) with its node key and hash, so the next call can patch without re-reading. Accept node-key-addressed patches ("replace section X of hash H"), which can't go stale silently. | 3,532 stale-context patch failures, 2,717 re-reads and 1,005 retries after them, 16.5% failure on Markdown (finding 3) | M |
| 4 | **Compaction-aware working set.** OpenCode exposes a `compaction` session hook. Inject the session's working set: files and keys edited, tests failing, open review findings, the last plan step. The model then resumes without re-reading. Offer the same as a session-start brief and as an MCP tool Codex and Kiro can call after compaction. | 1,571 compactions, 13k re-reads after compaction, orientation commands (findings 2, 8) | M |
| 5 | **Background jobs and `wait`.** The daemon tracks processes the agent starts (pid, log, exit) and offers a blocking `wait` (until exit, a file, or a log line, with a deadline) that returns only new output. OpenCode gets completion through `session.synthetic`. | 45k empty `write_stdin` polls, 6k `ps`/`pgrep`, 37k calls in polling runs, 139 h of shell sleeps (finding 4) | M |
| 6 | **Kiro integration.** Kiro has command hooks (PreToolUse/PostToolUse, JSON on stdin, exit 2 blocks) and MCP. Port the Claude Code hook: `after` context on reads, edits and shell; ref-moving git refusal; node-key reads. Add `ozagia install kiro` (hook files plus the MCP server in `.kiro/settings/mcp.json`). | 8% of tool calls, 524 not-found errors, 103 edit misses (findings 3, 9) | M |
| 7 | **Structured data queries.** A read form for JSON, YAML and TOML paths (`json:<file>#/a/b`, keys, lengths, slices) and an outline of large JSON result files, and capture of common `jq` filters into it. | 20k inline JSON inspections, 4.7k `jq` commands (finding 5) | S |
| 8 | **More shell capture.** `rg` over several paths, `rg --files`, `--glob` and `-C`; `nl -ba` as a read; `ps`/`pgrep` answered from the job table; environment-assignment prefixes; `find`; `tail -c`; subshell `( cd x && … )`; `timeout N`; `cd ~` inside the root; `| wc -l`, `| sort`, `| cut`. | about 26k Codex and 2.4k other commands (finding 7) | S |
| 9 | **Affected tests and a test-result cache.** A captured pytest without selectors runs the tests the graph links to the changed keys first, and reports the rest as not run. A pass is cached by the hashes of the files it imported. Slow and flaky tests are remembered. Extend to `cargo test`. | 617 whole-suite runs, 2,090 test runs in one session (finding 6) | M |
| 10 | **Lint inside the edit.** After a handled edit, run the project's configured linter (`ruff`, `rustfmt --check`) on the changed file and report findings for the changed keys in the edit's `[ozagia]` lines. Capture `ruff check` and `.venv/bin/ruff`. | 339 separate `ruff` runs, 269 `rustfmt` (findings 6, 7) | S |
| 11 | **Multi-file replace.** A preview-first, graph-addressed replace across files (literal or regex, scoped by glob or keys). It goes through the edit transaction and permissions, and returns the per-file delta. | 4k Python file rewrites outside the edit tools (finding 5) | M |
| 12 | **Path resolution.** For a read or edit of a path that doesn't exist, answer with the nearest graph files: same directory, fuzzy name, and the same relative path in the main checkout or another worktree. | 531 not-found errors in OpenCode and Kiro (finding 9) | S |
| 13 | **Cross-session ownership.** The daemon journal already records which session edited which file. Expose it: before an edit, warn when another live session holds uncommitted edits to the same file, and add a "who changed this, and is it committed" query. | finding 13 | S |
| 14 | **Operations graph.** Extend the infrastructure adapter to docker/podman compose files, Containerfiles, quadlets and systemd units (services, images, ports, volumes, `depends_on`, env files, timers). Map `podman logs` and `journalctl` output to those nodes in `after`. | finding 10 | L |
| 15 | **Research: user positions and review closure.** Record a user's correction as a research holding: the user's own stance, sourced to the conversation. Surface it when the topic comes back. Keep review findings as a ledger that closes across sessions. | finding 12 | M |
| 16 | **Fetch fallback.** When `webfetch` fails on a PDF or a paper URL, offer the research fetcher (open-access resolution, PDF text, cached by hash). | finding 11 | S |

## Suggested order

1. **Reach (1–2 weeks):** 1 (Codex) and 6 (Kiro), with 7, 8 and 10 as the
   first tools they get. Without these, the other features only reach the
   5% of calls that OpenCode makes.
2. **Context economy:** 2, 3 and 4, measured with the agent eval. Compare
   tokens per task, compactions per session and patch failures, with the
   same success rate. This is the largest effect on cost.
3. **Loop time:** 5 and 9, measured as wall time per task in the eval.
4. **Scope:** 11 to 16.

## Caveats

These are heuristics:

- Kiro's failure flag is its own `success` field. A Codex call counts as
  failed when its output says "Script failed" or reports an error.
- "Re-read" means the same file name in the same session. Edits made
  through the shell are not seen, so some re-reads were legitimate.
- Codex reads are parsed from shell commands, so they include some reads
  that were piped elsewhere.
- Codex session records include forked subagents (303 of 499), which
  inherit their parent's context; counts per session are per record.
- Prompt categories are regex matches.
- The numbers describe one person's work on one machine: research, writing
  and home-lab operations as much as code. Codex here was mostly used on one
  large project.

Each feature should be measured with the agent eval before it counts as an
improvement.
