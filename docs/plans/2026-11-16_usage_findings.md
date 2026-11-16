# What local agent sessions say ozagia should do next

Date: 16 Nov 2026. Source: this machine's OpenCode and Kiro session history,
mined with throwaway scripts (`/tmp/mine/`, not in the repository). Only
aggregate counts are recorded here; no prompt text, file contents or paths
from those sessions.

## Corpus

| | OpenCode | Kiro |
|---|---|---|
| sessions (eval and probe sessions excluded) | 131 (68 of them subagents) | 26 |
| user prompts | 405 | 1,362 |
| tool calls | 13,788 | 22,117 |
| tool calls per user prompt | 34.0 | 15.6 |
| failed tool calls | 106 (0.8%) | 1,148 (5.2%) |

The 175 eval sessions (`/tmp/ozagia_pilot/...`, probes) are excluded; they are
measured in `docs/results/`. Kiro is not integrated with ozagia at all today.
It runs 62% of the tool calls here.

## Findings

Ranked by how much agent time or context they cost.

1. **Re-reading what is already in context.**
   - There were 3,032 re-reads of a file with no edit tool call and no
     compaction since the previous read. 570 of them asked for the same range.
   - The median gap between the two reads is 3 tool calls (p75: 30), so most
     re-reads are an agent paging through one large file range by range.
   - 763 reads returned more than 20,000 characters. OpenCode truncated 867
     tool outputs.
   - Context pressure shows directly:
     - OpenCode compacted 86 times in 10 sessions, 27 times in one of them.
     - The logs show compaction summaries failing at the output-token
       limit.
     - 10 of 26 Kiro sessions passed 80% context use.
     - 264 re-reads happened right after a compaction.
2. **Ad-hoc Python instead of tools.**
   - 4,544 shell calls ran inline Python (`python -c`, heredocs).
   - 1,773 of these load JSON to look inside result or config files.
   - 969 rewrite files from Python. Those are edits that no edit tool sees,
     so they skip permissions and diffs.
   - 800 import project modules to introspect them.
   - Heredocs alone are the largest reason a shell command can't be
     captured (2,734).
3. **Polling.** 1,353 `sleep` calls in 30 sessions request 139 hours of
   sleeping in total. The agent is waiting on background work (pilots,
   builds, deploys, CI) and spends turns and context checking on it.
4. **Test loops.**
   - 90 sessions ran tests: a median of 8 runs per session, 273 at most.
   - 210 runs were whole-suite runs with no selector.
   - There were 339 `ruff` runs, separate from edits; `.venv/bin/ruff` is not
     a mapped command.
5. **Wrong paths.**
   - 531 "file not found" errors, 524 of them in Kiro.
   - Of those in the repository, 110 had a close match in the same directory
     (typos, renamed files).
   - 160 had a missing directory: mistyped roots, or a worktree path
     confused with the main checkout and the reverse.
6. **Orientation commands.** There were 2,383 `git status`/`diff`/`log`
   calls, 2,220 `ls`/`find`/`tree` calls and 907 `wc -l` calls. The agent
   keeps re-establishing where things stand, both at session start and after
   compactions.
7. **Shell forms still passed through** (from real use, not the pilot):
   - 1,161 subshells `( … )`;
   - 707 `sudo` commands, mostly operations;
   - 533 environment assignments without a command (`X=1; …`);
   - 393 greps over several paths;
   - 335 commands under a `timeout N` prefix;
   - 304 `for` loops;
   - 140 `cd ~`;
   - pipes into `cut`, `wc` and `sort`.
8. **Operations (SRE) work outside the IaC adapter's reach.**
   - 1,087 podman/docker commands, 972 `curl` health and API checks, 273
     `gcloud`, 222 `systemctl`/`journalctl` and 123 `nvidia-smi`.
   - Edited files include Dockerfiles, compose YAML, `.service` units and
     shell scripts.
   - The IaC adapter reads Terraform, Kubernetes and their relatives, but not
     compose files, Containerfiles, quadlets or systemd units.
9. **Web fetch failures.** 21.6% of OpenCode `webfetch` calls failed, and
   17.7% of Kiro's: 404s, and PDFs the tool can't read.
10. **Edit misses.** 26 OpenCode `edit` and 103 Kiro `str_replace` calls
    missed. After a miss the agent usually re-reads the file or tries again.
    ozagia already hands an exact miss to OpenCode's tolerant edit. Kiro has
    no such path.
11. **What users correct.**
    - 129 prompts are some form of "continue": the agent stopped before
      the work was done.
    - About 60 prompts are corrections. Most of these are about meaning in
      research and writing sessions (the agent took the wrong lesson or
      missed the argument's point), not about code.
    - About 45 ask for a review, often a re-review that lists the issues
      still outstanding.
12. **Concurrent sessions.** Several sessions worked in the same checkout
    with each other's uncommitted changes in place. Merges were blocked and
    files were edited by two agents. Nothing tells an agent that another
    live session owns a dirty file.

## Features, in priority order

Effort: S is a day or less, M is a few days, L is a week or more.

| # | feature | addresses | effort |
|---|---|---|---|
| 1 | **Kiro integration.** Kiro has command hooks (PreToolUse/PostToolUse, JSON on stdin, exit 2 blocks) and MCP. Port the Claude Code hook: `after` context on reads, edits and shell; ref-moving git refusal; node-key reads. Add `ozagia install kiro` (hook files plus the MCP server in `.kiro/settings/mcp.json`). | Kiro is 62% of tool calls here and gets no graph at all (findings 1, 5, 10) | M |
| 2 | **Read ledger and structure-first reads.** Track what each session has read (file hash, ranges). An unchanged same-range repeat with no compaction since gets a one-line "unchanged since call N" with keys. A read of a large file with no range gets an outline first: symbols, Markdown and TeX sections with line ranges and keys, plus the first chunk. Reading a key returns the whole node. | 3,032 repeat reads, 763 reads over 20k characters, paging (finding 1) | M |
| 3 | **Compaction-aware working set.** OpenCode exposes a `compaction` session hook. Inject the session's working set: files and keys edited, tests failing, open review findings, the last plan step. The model then resumes without re-reading. Add the same as a session-start brief (dirty files, recent commits, failing tests, what changed since the last session) through the existing context hook. | 264 re-reads after compaction, orientation commands (finding 6), failed compaction summaries | M |
| 4 | **Background jobs and `wait`.** The daemon tracks processes the agent starts (pid, log, exit) and offers a blocking `wait` (until exit, a file, or a log line, with a deadline). Completion is announced through OpenCode's `session.synthetic`, instead of `sleep N; tail log` loops. | 1,353 sleeps, 139 h requested (finding 3) | M |
| 5 | **Affected tests and a test-result cache.** A captured pytest without selectors runs the tests the graph links to the changed keys first, and reports the rest as not run. A pass is cached by the hashes of the files it imported, so an unchanged test isn't run again. Slow and flaky tests are remembered. | 210 whole-suite runs, median 8 runs per session (finding 4) | M |
| 6 | **Lint inside the edit.** After a handled edit, run the project's configured linter (`ruff`, if `.ozagia.toml` or `pyproject` names it) on the changed file. Report findings for the changed keys in the edit's `[ozagia]` lines. Capture `ruff check` / `.venv/bin/ruff` as a mapped command. | 339 separate `ruff` runs (finding 4) | S |
| 7 | **Structured data queries.** A read form for JSON, YAML and TOML paths (`json:<file>#/a/b`, keys, lengths, slices) and an outline of large JSON result files. Then "what's in this report" needs no Python. | 1,773 inline JSON inspections (finding 2) | S |
| 8 | **Path resolution.** For a read or edit of a path that doesn't exist, answer with the nearest graph files: same directory, fuzzy name, and the same relative path in the main checkout or another worktree. Kiro gets this through the hook's PreToolUse. | 531 not-found errors, 110 with a near match (finding 5) | S |
| 9 | **Multi-file replace.** A preview-first, graph-addressed replace across files (literal or regex, scoped by glob or keys). It goes through the edit transaction and permissions, and returns the per-file delta. | 969 Python file rewrites outside the edit tools (finding 2) | M |
| 10 | **More shell capture.** Subshell `( cd x && … )` treated as `cd`, multi-path grep, a `timeout N` prefix, `cd ~` inside the root, and `| wc -l`, `| sort`, `| cut -d -f` filters. | finding 7 (about 2,400 commands) | S |
| 11 | **Cross-session ownership.** The daemon journal already records which session edited which file. Expose it: before an edit, warn when another live session holds uncommitted edits to the same file, and add a "who changed this, and is it committed" query. | finding 12 | S |
| 12 | **Operations graph.** Extend the infrastructure adapter to docker/podman compose files, Containerfiles, quadlets and systemd units (services, images, ports, volumes, `depends_on`, env files, timers). Map `podman logs` and `journalctl` output to those nodes in `after`, as pytest output maps to tests. | finding 8 | L |
| 13 | **Research: user positions and review closure.** Record a user's correction as a research holding: the user's own stance, with its source being the conversation. Surface it when the topic comes back. Keep review findings as a ledger that closes across sessions ("list outstanding issues"). | finding 11 | M |
| 14 | **Fetch fallback.** When `webfetch` fails on a PDF or a paper URL, offer the research fetcher (open-access resolution, PDF text, cached by hash). | finding 9 | S |

## Suggested order

1. **Quick wins (1–2 weeks):** 1 (Kiro), 6, 7, 8, 10 and 11. These add
   capture for the agent that runs most of the work here, and remove a few
   thousand failed or wasted calls.
2. **Context economy:** 2 and 3, measured with the agent eval. Compare
   tokens per task and compactions per session, with the same success rate.
   This is the largest effect on cost.
3. **Loop time:** 4 and 5, measured as wall time per task in the eval.
4. **Scope:** 9, 12, 13 and 14.

## Caveats

These are heuristics:

- Kiro's failure flag is its own `success` field.
- "Re-read" means the same file name in the same session. Edits made
  through the shell are not seen, so some re-reads were legitimate.
- Prompt categories are regex matches.
- The numbers describe one person's work on one machine: research, writing
  and home-lab operations as much as code.

Each feature should be measured with the agent eval before it counts as an
improvement.
