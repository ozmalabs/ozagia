# Candidate live coding-agent evaluation

This is a proposed, small Ocelot coding-agent pilot. It is not a test of the
Ozma assembler's four context arms. The repository rules require a person's
approval before running a live agent driver, even for a local model.

- Repository: `/applications/data/projects/ocelot`; the harness makes a
  detached local clone per live run and removes its origin.
- Tasks: `evaluation/agent_e2e/tasks_v2.json`, task
  `seed-dependency-closure`, base `54e5eb5fae4592444136b9d70fde08e94cb8a914`.
- Driver/model: OpenCode 2.3.0, `vllm/qwen3.8-27b`, which was listed by
  `opencode models`. Listing alone does not prove the endpoint is healthy.
- Arms: `baseline` and `graph_intent`, one repeat each: two model sessions,
  each with a 180-second driver timeout. The graph arm has additional index
  preparation outside that timeout. Logs and results go under a temporary
  run root, not into Ocelot's working tree.
- Stop after $0.10 in reported total cost; this is a post-session check, not a
  hard pre-session spending cap. The local model consumes compute and the
  driver may not report cost. OpenCode may load the operator's global client
  configuration; inspect it before authorizing a run.

Read-only preflight was completed: the task passed on its base and failed
when broken in a disposable clone. A fake-driver run succeeded for both arms;
that validates the harness rather than agent performance. Its graph prep took
about 51 seconds, and the fake run reports no plugin activity. This one-task,
one-repeat experiment is a smoke test; it cannot establish an end-to-end
benefit or substitute for a multi-repository, repeated, held-out benchmark.

After approval, the run would use the following command (choose an output
folder under `/tmp`):

```sh
OZAGIA_EVAL_LIVE=1 .venv/bin/python -m ozagia.eval.agent run \
  --repo /applications/data/projects/ocelot \
  --tasks /applications/data/projects/ocelot/evaluation/agent_e2e/tasks_v2.json \
  --task-ids seed-dependency-closure --driver opencode \
  --model vllm/qwen3.8-27b --arms baseline,graph_intent --repeats 1 \
  --timeout-s 180 --max-total-usd 0.10 --live \
  --run-root /tmp/ozagia-eval-live-pilot \
  --out-json /tmp/ozagia-eval-live-pilot.json \
  --out-md /tmp/ozagia-eval-live-pilot.md
```
