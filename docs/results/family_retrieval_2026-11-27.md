# Family-aware federation retrieval — 2026-11-27

> **Scope:** fixture-level mechanism check. This is not evidence of a general retrieval benefit and is not a benchmark claim. The default remains `family_boost=False`.

## Setup

The evaluation script builds three temporary snapshots under a private temporary directory: an anchor, a declared family member containing the answers, and an unrelated distractor with a competitive lexical match. No project checkout is imported or written and no graph files are committed.

- Sample: 5 queries (4 constructed family queries, 1 control).
- Arms: baseline (`family_boost=False`) and family-aware (`family_boost=True`, additive rank boost 0.15, capped at 1.0).
- Retrieval depth: top 20 lexical hits; metrics at k=1, 3 and 5 plus MRR.

## Aggregate results

| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |
| --- | --- | ---: | ---: | ---: | ---: |
| all queries | `baseline` | 20.0% | 100.0% | 100.0% | 0.600 |
|  | `family_boost` | 100.0% | 100.0% | 100.0% | 1.000 |
| family queries | `baseline` | 0.0% | 100.0% | 100.0% | 0.500 |
|  | `family_boost` | 100.0% | 100.0% | 100.0% | 1.000 |
| control query | `baseline` | 100.0% | 100.0% | 100.0% | 1.000 |
|  | `family_boost` | 100.0% | 100.0% | 100.0% | 1.000 |

## Per-query results

| Query | Kind | Baseline rank | Family rank | Δ rank | Baseline RR | Family RR |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `cache-eviction` | family | 2 | 1 | -1 | 0.500 | 1.000 |
| `ledger-close` | family | 2 | 1 | -1 | 0.500 | 1.000 |
| `deploy-rollout` | family | 2 | 1 | -1 | 0.500 | 1.000 |
| `search-shards` | family | 2 | 1 | -1 | 0.500 | 1.000 |
| `telemetry-control` | control | 1 | 1 | 0 | 1.000 | 1.000 |

## Findings

- The family arm moved 4 of 4 constructed family answers from rank 2 to rank 1, so hit@1 and MRR improve on the family subset.
- The control query is rank 1 in both arms and its metrics are unchanged: `True`. The gain therefore comes entirely from the constructed family queries.
- Non-family hits remain in the results; the arm re-ranks and never filters.
- Fixture assumptions passed: `True`.

## Decision

The arm stays opt-in and the default remains `family_boost=False`. A gain on 4 constructed queries is not enough evidence to change default behavior; the gate requires a paired evaluation on real repositories and queries with no regression and a meaningful gain.

## Limits

- Three temporary synthetic snapshots and five frozen queries; this is a fixture-level mechanism check, not evidence of general retrieval benefit.
- The four family queries were constructed with a 1/7 lexical gap that the bounded +0.15 boost can close. They verify the mechanism, not that families improve real queries.
- The control query is already lexically decisive; it checks that the family arm does not disturb it.
- Lexical search only: no embeddings, parsers, adapters, target repositories, network access or model calls.
- The default remains family_boost=False. Enabling it requires a separate paired evaluation on real repositories and queries.

## Reproduce

```sh
.venv/bin/python evaluation/family_retrieval/family_retrieval.py
```

The script writes this Markdown file and the sibling JSON report. Everything else is created and removed under a temporary directory.
