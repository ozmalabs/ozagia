# Project behavioral invariants

Projects can declare a small set of behavioral claims in `.ozagia.toml` so
the relevant source and check locations travel with retrieved task context:

```toml
[invariants.backend_world_boundary]
statement = "Backend proposes actions; the world validates before applying."
sources = ["src/backend/proposals.py", "src/world/validation.py"]
checks = ["tests/test_world_validation.py"]

[invariants.seeded_replay]
statement = "A run with a fixed seed replays deterministically."
sources = ["src/simulation/replay.py"]
checks = ["tests/test_simulation_replay.py"]
```

Each entry needs a short id, a statement and at least one repository-relative
source path. `checks` is optional and names likely tests or other checks. The
reader rejects absolute paths, parent traversal, unknown fields and unbounded
entries. Task context includes matching declarations and whether each named
path currently exists. This path check is only an observation: declarations
remain `evidence = "declared"`; Ozagia does not infer that a claim is true or
that a check passed.

The syntax is available in `.ozagia.toml` and `ozagia/config.toml`. When both
files define the same invariant id, the legacy declaration wins as a whole.

## Bigville example

For a Bigville checkout, the project config could state the proposal boundary
and deterministic chance requirement like this, using paths observed in the
Bigville checkout:

```toml
[index]
docs = ["README.md", "docs/**/*.md", "tools/phase093/README.md"]
key_docs = [
  "docs/FEATURE_PHASES.md",
  "docs/phases/phase093/PHASE093_REPORT.md",
  "docs/FOOD_REPORTING_AND_FOUNDING.md",
  "tools/phase093/README.md",
  "README.md",
]

[invariants.backend_world_validation]
statement = "The backend proposes actions; the world validates before applying."
sources = ["bigville/backends/ocelot.py", "worlds/bigville_world.py"]
checks = ["tests/test_bigville_backends.py"]

[invariants.seeded_deterministic_replay]
statement = "Runtime chance uses the world seed and canonical entity state across executors."
sources = ["domains/determinism.py", "README.md"]
checks = ["tests/test_deterministic_shards.py", "tools/verify_deterministic_chance.py"]
```

`FEATURE_PHASES.md` contains the current programme priority and phase status;
the Phase 093 report itself says the evaluation is in progress and adoption is
not yet approved. Those make them stronger current-status evidence than a
completed phase report. The food programme document and runner README explain
the current evaluation protocol and limits. A project owner chooses their
priority through `key_docs`; Ozagia does not infer a document's status from its
filename or date. Completed phase reports remain retrievable when their
content matches. This is a ranking preference, not a freshness claim or
automatic status detector.

Task retrieval includes only declarations whose ids or statements match
specific task terms. Generic wording such as "ensure it is valid" adds no
invariant matches. Selected declarations share the context token budget with
graph snippets; the returned budget reports their estimated token cost.
