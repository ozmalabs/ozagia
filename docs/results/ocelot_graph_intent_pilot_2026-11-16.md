# Ocelot graph-intent pilot (2026-11-16)

This was the user-approved two-session live smoke test described in
[the candidate](../plans/2026-11-16_live_eval_candidate.md). Both sessions used
OpenCode 2.3.0 and `vllm/qwen3.8-27b`, with Ocelot at base
`54e5eb5fae4592444136b9d70fde08e94cb8a914` and the
`seed-dependency-closure` task. Each arm ran once in an isolated clone.
The harness reports $0.00 in model cost; that does not account for local
compute or establish an independently measured monetary cost.

| Measure | Baseline | Graph intent |
| --- | ---: | ---: |
| Task check | 13/13 passed | 13/13 passed |
| Driver time | 63.672 s | 167.817 s |
| Graph preparation | 0 s | 52.886 s |
| Input tokens reported | 56,212 | 90,037 |
| Output tokens reported | 2,792 | 4,069 |
| Tool calls | 24 | 38 |
| Captured / passthrough calls | not applicable | 5 / 33 |
| Plugin or daemon failures | not applicable | 0 |

Both sessions modified the expected `domains/seed_registry.py` and exited
without a driver error or timeout. The graph daemon recorded a plugin hello,
no fallbacks, and no daemon errors. The check passed in each clone. The two
reported tool-call patterns differ substantially, so elapsed time and token
counts cannot be attributed solely to the graph layer. Preparation was timed
separately from driver time.

This is one task and one repeat per arm, not evidence of an average effect.
The graph-enabled run was slower and used more input tokens in this sample;
no claim of improvement is supported. It tests graph-intent capture with
OpenCode, not the Ozma assembler's four-arm context experiment. A credible
comparison needs repeated, randomized, held-out tasks across repositories,
with model/provider conditions and cold/warm preparation recorded.

The complete local report and logs from this run are at
`/tmp/ozagia-eval-live-pilot.md`, `/tmp/ozagia-eval-live-pilot.json`, and
`/tmp/ozagia-eval-live-pilot/logs/`; temporary artifacts may be removed by the
host. No further live sessions were run.
