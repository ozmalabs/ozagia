# DeepSeek V4.1 Flash complex-task A/B probe — 2026-11-18

Status: one-shot probe, not a statistical comparison. Both arms passed the
hidden acceptance checks. The treatment arm used **no Ozagia tools**, so this
run is evidence about tool selection and harness isolation, not about whether
Ozagia improves implementation work.

## Design

- Model: `opencode-go/deepseek-v4.1-flash`, one session per arm.
- Source: Ozagia `1b04ff6`, two clean worktrees at the same commit.
- Task: add a read-only `ozagia capabilities directions` view and MCP
  `dev_capabilities action=directions`, with payload, CLI, MCP schema, docs and
  tests. It is a multi-file change across `core/self_graph.py`,
  `capabilities_cli.py`, `cli.py`, the MCP server, docs and tests.
- Arms: `baseline` (plain OpenCode) and `ozagia` (full local MCP server plus
  the installed native plugin/GraphSession).
- The same prompt was used for both; no commits or pushes were requested.
- Hidden acceptance checks (not part of the repository): payload fields,
  CLI JSON/text, MCP handler and schema, existing list/audit behavior, docs,
  and the targeted test files.

Two earlier attempts are **invalid and not reported as results**: they
inherited the outer OpenCode server's `PWD`/`OPENCODE_CONFIG`/daemon socket,
and the Ozagia arm could read the harness check. The runs below used a
sanitized child environment and moved the harness outside paths referenced by
the child.

## Result

| Measure | baseline | ozagia |
| --- | ---: | ---: |
| Hidden checks | **7/7 pass** | **7/7 pass** |
| Wall time | 673.8 s | 547.1 s |
| Graph/session prep | 0 s | 7.35 s |
| Driver time | 673.8 s | 539.8 s |
| Model steps | 29 | 75 |
| Edits | 18 | 27 |
| Reads | 13 | 19 |
| Greps | 6 | 10 |
| Shell calls | 16 | 43 |
| Input tokens | 83,036 | 103,673 |
| Output tokens | 8,131 | 14,703 |
| Cache-read tokens | 1,368,192 | 4,791,296 |
| `ozagia_*` tool calls | 0 | **0** |
| Files changed | 7 | 7 |

Both implementations pass the independent checks and cover the same seven
files. The Ozagia arm finished 126.7 s sooner in this one shot, but used far
more steps, tokens and shell calls; model latency variation is large enough
that this single pair cannot support a speed claim. The 7.35 s graph prep is
real and modest; it is not the source of the wall-time difference.

## Where improvement is needed

1. **Ozagia tools were never selected.** The Ozagia arm made zero
   `ozagia_structure` and zero `dev_*` MCP calls. The task was solved with
   read/grep/edit/shell, so the new graph, maps and capability tools provided
   no observable benefit. Available tools are not enough: Ozagia needs a
   session-start brief or an explicit hint for repository discovery, or the
   tools need descriptions/examples that make them the obvious choice. A
   future A/B should mark the treatment invalid unless it records at least one
   intended Ozagia tool call.
2. **Harness isolation was wrong before the final rerun.** Passing
   `OPENCODE_CONFIG`/`PWD` from the outer agent allowed the child to inherit the
   outer server and read the hidden check. Child environments must be built
   from an allowlist and harness files must not live under referenced paths.
3. **Per-worktree test isolation was missing.** The shared `.venv` has an
   editable install pointing at the main checkout. Agents' own pytest runs
   imported main code; the Ozagia arm spent many steps debugging this before
   using `PYTHONPATH=$PWD/src`. This can falsely validate edits and should be
   fixed in the eval harness (per-worktree venv or enforced source path).
4. **MCP constructor robustness.** `DevMemoryServer(None)` in coding mode
   crashed in `_load_adapter_tools`, which assumes a memory root. The server
   now skips adapter/SRE discovery when no memory is present and still lists
   the static coding tools; a regression test was added.
5. **One shot is a probe, not evidence.** Use repeated paired tasks before
   claiming success or speed effects; this run only identifies selection and
   isolation problems.

## Artifacts

Owner-facing harness and logs: `/home/matt/.local/state/ozagia-ab/`
(`result-*.json`, `logs-*`, `check.py`, `run.py`). The worktrees are
disposable and contain the two implementations. The hidden checks were
executed against each worktree with `PYTHONPATH=<worktree>/src`.
