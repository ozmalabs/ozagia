# Bigville test coverage evidence audit — 2026-11-27

## Scope and method

Read-only audit of the Bigville checkout at `/applications/data/projects/bigville`
(HEAD `e243ea70eee2a9ca5a55dfe62b9e29214f82cc87`). I queried the adapter's
`bigville_data_test_evidence` view from an adapter-enabled graph snapshot and
inspected the cited test files. No Bigville files were changed and no tests or
simulation code were run.

The query returned 1,001 subjects: 14 scenarios, 391 seed manifests and 596
pack tables. Six subject/test-file associations had literal test evidence;
all are labeled `advisory_only`. This is a candidate list, not a complete
data-to-test map.

## Candidate evidence

| Data path | Candidate test | Observed evidence | Assessment |
| --- | --- | --- | --- |
| `scenarios/reference/seasonal/victoria_apple_household_90d.json` | `tests/test_seasonal_reference_household.py` | Test opens the scenario JSON and checks 90-day energy and mass accounting, harvest arrivals, and a reduced-reserve comparison. | Strong, specific evidence for this scenario and modeled behaviors. It says nothing about other scenarios. |
| `seeds/bigville_speech_decide.json` | `tests/test_bigville_speech_migrated.py` | Test opens the root seed JSON and checks named speech rules and allowed DSL terms. | Direct structural evidence for this path. It does not establish equivalent coverage of the packaged duplicate or every runtime behavior. |
| `seeds/bigville_loud_comm.json` | `tests/test_phase028_hearing.py` | Seed path occurs in the module docstring; test bodies exercise hearing-radius and witness behavior. | Weak association: no direct seed load or assertion of this manifest was found. Keep advisory. |
| `packs/bigville/population/balance_scenario.json` | `tests/test_phase007_balance_scenario.py` | Tests compare the loaded balance scenario with the built town and assert selected model parameters. | Strong evidence for several listed fields, not every use or downstream effect. |
| `packs/bigville/world/marks.json` | `tests/test_phase062b_perceiver_marks.py` | Tests exercise perception and reference mark-table values such as thresholds and ranges. | Relevant behavior evidence for selected rows and interactions. |
| `packs/bigville/world/marks.json` | `tests/test_phase063_marks.py` | Tests exercise mark accumulation, decay, washing, visibility and related table values. | Relevant behavior evidence for selected rows and interactions. |

## Limits and recommendation

Bigville has no `.ozagia.toml` coverage declarations. Literal mentions and
selected runtime assertions do not show that every scenario, manifest, table
consumer, generated artifact, or dynamic loading path is covered. Some links
come from docstrings, while others cover only selected fields. The adapter
also marks all query rows `advisory_only`.

Do not turn this evidence into `complete` test-coverage rules. It can guide
review and test discovery; declaring complete coverage needs a separately
reviewed inventory of consumers and affected behaviors.
