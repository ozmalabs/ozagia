# Infrastructure fixture check — 2026-11-16

`python -m pytest -s -q tests/test_iac_fixture_eval.py` exercises a synthetic,
committed local repository through the real opt-in adapter, graph and queries.
It does not call a model, infrastructure CLI, cluster or cloud account.

| Check | Result |
| --- | --- |
| Terraform expected source recall | 2/2 |
| Kubernetes expected source recall | 3/3 |
| Kubernetes expected relationship recall | 2/2 |
| Kubernetes query context | 212 / 240 characters |
| Secret canary in graph snapshot or context | absent |

`python -m pytest -q tests/test_iac_fixture_eval.py` also runs the labelled
retrieval questions in `tests/fixtures/iac_eval/retrieval_questions.json`
through the real adapter and scores returned citations and edges. At both 360
and 600 character budgets, source precision and recall were 1.0; the Terraform
and Kubernetes relationships were recalled 1/1 and 2/2 at 360 characters. The
history follow-up also found all three `payments-api` declarations at 354
characters. These are three small synthetic questions, not a held-out or
production evaluation.

These checks cover one small hand-authored fixture; they do not establish
precision on unseen repositories, production-scale index latency, model token
counts, or an end-to-end improvement. Comparison against the unmodified agent
workflow and a real model requires a separately approved live evaluation.
