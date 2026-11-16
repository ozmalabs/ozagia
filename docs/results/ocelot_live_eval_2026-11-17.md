# Ocelot live evaluation: prompt cache, read stubs, affected tests (2026-11-17)

User-approved live runs. All sessions used OpenCode 2.3.0 and a local
`vllm/qwen3.8-27b` (an RTX 3090), Ocelot at base
`54e5eb5fae4592444136b9d70fde08e94cb8a914`, and the 8 tasks of
`evaluation/agent_e2e/tasks_v2.json`. Each arm ran each task once, with a
300 s driver limit. The harness reports $0.00 in model cost, which does not
account for local compute. The operator's global OpenCode configuration
(including a third-party goal plugin) was loaded in every arm alike.

Arms:

- **baseline**: OpenCode's own tools.
- **graph (before fix)**: `graph_intent` at `5863870`, current defaults.
- **graph**: `graph_intent` with the prompt-cache fix (`c7fd5e2`).
- **graph + stub**: the same, with `[reads] repeat = "stub"`.
- **graph + affected**: the same, with `[tests] select = "affected"`.

The configuration was Ocelot's `.ozagia.toml` plus the one setting, given
through `$OZAGIA_CONFIG`.

## Results

| Arm | Tasks passed | Mean driver time | Mean input tokens | Timeouts |
| --- | ---: | ---: | ---: | ---: |
| baseline | 4/8 | 111 s | 71.6k | 0 |
| graph (before fix) | 4/8 | 265 s | 242.2k | 5 |
| graph | 4/8 | 129 s | 63.9k | 1 |
| graph + stub | 4/8 | 190 s | 82.2k | 4 |
| graph + affected | 4/8 | 164 s | 66.2k | 1 |

The same four tasks passed in every arm (`admissible-moves-weight`,
`bound-audit-quantifier-scope`, `checkpoint-instance-index`,
`seed-dependency-closure`), and the same four failed in every arm.

**Correction: the four failures were the harness, not the agents.** The
harness ran each task's check with ozagia's own interpreter, which has no
`numpy`. The checks of `learner-board-pairs`, `life-step-cache`,
`r2-cycle-arms` and `remote-causal-posit-noop` import `numpy`, so they
stopped at collection with `ModuleNotFoundError`. The agents had run the
same tests with the system `python`, which has `numpy`, and seen them pass.
`ozagia.eval.agent validate` with that interpreter reports all four as
"check does not pass on base". Rerun with `--python` set to Ocelot's
`.venv` (same model, one run each), all four passed in both arms, editing
the expected files. So every arm solved 8/8 tasks; the success column
above reflects the broken check, and the time and token comparisons are
unaffected. A
timed-out session can still pass when its edit was already made.
Input tokens are the uncached tokens the driver reports; the graph arm's
preparation (about 70 s per task) is timed separately.

## Findings

**The task hint broke the model server's prompt cache.** Before the fix,
the OpenCode plugin re-fetched the `[ozagia task context]` system-prompt
text every 10 s of new history and after every edit. The system prompt
starts the request, so each change made the server prefill the whole
conversation again: cache reads dropped to 0 and single requests carried up
to 53k uncached input tokens, against a few hundred for baseline. The fix
asks for the hint once per user prompt and session. After it, the graph arm
read fewer uncached input tokens than baseline (63.9k against 71.6k) and its
driver time came close to baseline (129 s against 111 s).

**Read stubs (`stub`) showed no benefit.** The daemon served 7 stubs across
8 sessions. The arm used more input tokens and timed out more often than
the `note` default. With one repeat per task the timeouts are within run
to run variation (the default graph arm itself went from 66 s to 300 s on
one task between runs), but nothing here supports making `stub` a default.
It stays opt-in.

**Affected-test selection was not exercised.** `affected_runs` was 0 in
every session: the agents ran pytest with explicit test selectors, which
`select = "affected"` leaves alone. The arm is effectively a second run of
the default and says nothing about the policy. It stays off by default; a
task set where agents run the whole suite would be needed to measure it.

**No success-rate difference.** All arms solved the same tasks. With 8
tasks and one repeat, this sample can show large cost and time effects
such as the cache regression, but not small effects on success.

## Not covered

Goals (`ozagia goal`) and the repo review were not used by the agents; no
task asked for them and the prompts do not mention them. Codex, Kiro and
Claude Code were not run.
