# Module scaffolding from a repo-scan opportunity

`ozagia.core.module_build` and `python -m ozagia.module_build_cli` are stage two
of plan item 15. Stage one (`docs/repo_scan.md`) ranks opportunities; this stage
turns **one person-selected opportunity** into a deterministic scaffold plan and
writes it only after a one-time confirmation.

The result is an advisory scaffold, not a merge. It does not edit ozagia core,
an existing package, the CLI or the configuration. It does not install a
dependency, run a test, call a model, use the network, commit, merge or push.

## Workflow

```text
repo scan report
      |
      v
preview (default)  -> plan, files, evidence, gates, token; writes nothing
      |
      v
person reviews the plan and runs the gates
      |
      v
apply --confirm TOKEN [--force]  -> files under the destination + audit
```

1. Produce a report with the advisory scanner:

   ```bash
   PYTHONPATH=src python -m ozagia.repo_scan_cli scan --root . --json \
     --out .ozagia/repo_scan.json
   ```

2. Preview one opportunity:

   ```bash
   PYTHONPATH=src python -m ozagia.module_build_cli preview \
     --root . --report .ozagia/repo_scan.json \
     --opportunity 'missing-tests:src/app.py'
   ```

3. Read the plan, especially the opportunity, evidence, `not_assessed`, review
   gates and collisions. Then apply with the printed token:

   ```bash
   PYTHONPATH=src python -m ozagia.module_build_cli apply \
     --root . --report .ozagia/repo_scan.json \
     --opportunity 'missing-tests:src/app.py' \
     --confirm TOKEN
   ```

4. Run the generated test and lint gate from the repository root, review the
   code, and decide whether to keep, change or delete the scaffold.

`list` shows only applied scaffolds recorded in the audit; it reads and writes
no module files:

```bash
PYTHONPATH=src python -m ozagia.module_build_cli list --root . --json
```

## Report and opportunity inputs

The plan may be built from:

- a repo-scan report path: `--report PATH`;
- the first conventional report under the root when neither `--report` nor an
  opportunity is given: `.ozagia/repo_scan.json`,
  `.ozagia/reports/repo_scan.json` or `.ozagia/repo_scan/latest.json`;
- a JSON file containing a single opportunity object (an object with an `id`);
- an explicit opportunity mapping through the library API.

A report with more than one opportunity requires `--opportunity ID`. The
selected opportunity supplies:

- id, category, title, action and proposed module;
- expected effect, effort (`S`/`M`/`L`), risk and confidence;
- structured evidence (file, graph key, command or metric);
- the opportunity's own `not_assessed` entries.

The plan copies these values rather than recomputing or strengthening them. A
scan is advice; a scaffold is a starting point.

## Plan

A plan is deterministic for the same root, report, opportunity and destination.
It carries no timestamp or random value. It contains:

- `module` — id, human name, destination, package, source file and test file;
- `opportunity` — the selected values above;
- `expected_effect`, `effort`, `risk`, `confidence` and `evidence`;
- `files` — ordered README, `__init__.py`, `module.py` and the generated test,
  each with bytes, SHA-256 and its full content;
- `dependencies` — empty by default; nothing is resolved or installed;
- `write_scope` — exactly the destination;
- `generated_tests` — test path and the command that runs it;
- `review_gates` — scope, tests, lint, import, manual review and no-core-edits;
- `collisions` and `applicable` — existing target files and whether a plain
  apply would refuse;
- `not_assessed` — inherited and standard limits;
- `confirmation` — the one-time token;
- `plan_digest` — SHA-256 over the plan content;
- `not_merge` — the advisory notice.

Default destination is `modules/<id>/`. `--dest DIR` overrides it. Every
destination part must be a valid Python identifier: the generated package is
imported as `modules.<id>` by default, and a destination such as
`lib/custom_app` is imported as `lib.custom_app`. Hyphens and other
non-identifier parts are refused so the generated module stays importable.

The module id is derived deterministically from the opportunity's
`proposed_module` (for example `tests/test_app.py` or `src/app.py` become
`app`), then the opportunity id and title. It is lower-case and a valid Python
identifier.

## Files

```text
modules/<id>/
  README.md                 opportunity, evidence, scope, gates, limits
  __init__.py               re-exports MODULE_ID and build_summary
  module.py                 deterministic scaffold summary and entry point
  tests/test_<id>.py        runnable generated tests
```

No `pyproject.toml` is generated: the scaffold uses the standard library only
and needs no packaging or dependency change. The generated test inserts the
repository root into `sys.path`, imports the package, checks deterministic
metadata and checks the entry point output.

## Preview and apply

Preview is the default and writes nothing. It does not create `.ozagia`, a
token file or a temporary directory. The confirmation token is derived from the
plan digest and the resolved root, so preview can return it without writing.

Apply:

1. resolves and re-validates the root;
2. recomputes the plan digest and refuses a changed plan;
3. accepts only the token bound to that root and digest;
4. refuses a token already recorded as used in the audit;
5. re-checks every destination and file path: inside the root, inside the
   destination, not `.git`, not `.ozagia`, not the state directory, and not a
   symlink escape;
6. refuses existing target files unless `--force`;
7. writes each file through a same-directory temporary file and `os.replace`,
   rolling back files it created if a later write fails;
8. appends an audit record under `.ozagia/modules/audit.jsonl`.

`--force` replaces existing target files. It does not bypass the token, the
plan digest, the one-time rule, path containment, the protected directories or
the write scope. A token is consumed only by a successful apply; a refused
apply leaves it usable for a corrected retry with the same plan.

The audit record contains the timestamp, module id, destination, plan digest,
token fingerprint (not the token), whether force was used, file paths, hashes
and sizes. It contains no file content and no secret. It is the only write
outside the destination.

## Review gates

The plan lists gates; apply does **not** run them:

- `scope_contained` — every file resolves inside the destination and root;
- `generated_tests` — `python -m pytest -q <test>`;
- `lint` — `ruff check <destination>`;
- `importable` — `python -c "import <package>"` from the repository root;
- `human_review` — interface, naming, edge cases and integration;
- `no_core_edits` — the change set contains only the write scope.

A green generated test is not proof of correctness. A missing or stale scan,
weak evidence, a wrong module boundary and dependency/licence questions remain
`not_assessed`.

## Scope and limits

- **No merge.** Apply creates a scaffold; it never commits, merges, pushes,
  rewrites ozagia core or opens a pull request.
- **No dependency install.** The generated module imports only the standard
  library. Adding a dependency is a separate reviewed change.
- **No core edit.** Existing modules are read only for the report; the plan
  and apply touch only the destination and the state audit.
- **No test, lint, model, network or subprocess execution.** Commands are
  text. The operator runs them.
- **No automatic selection.** Stage one ranks; a person selects one
  opportunity.
- **No automatic rollback command.** Because writes are confined to the
  destination, removing that directory after review restores the previous
  tree; the audit keeps the record. Use `git status` before deleting anything.
- **No freshness guarantee.** A report can predate the checkout. Preview does
  not refresh the graph or rescan.
- **No hidden state.** Preview writes nothing; apply writes the destination
  files and the audit only.

## Exit codes

`python -m ozagia.module_build_cli`:

- `0` — preview, successful apply or list;
- `1` — refused token, replay, path, symlink, protected directory or existing
  file without `--force`;
- `2` — usage, missing/invalid report, unknown opportunity or IO error.

## Library API

```python
from ozagia.core import module_build

plan = module_build.build_plan(
    root=".",
    report_path=".ozagia/repo_scan.json",
    opportunity_id="missing-tests:src/app.py",
    dest=None,
)
# plan has not written anything
result = module_build.apply(
    ".", plan, plan["confirmation"]["token"], force=False)
```

The explicit-opportunity form is useful for a caller that already has a
selected scan item:

```python
plan = module_build.build_plan(
    root, opportunity={"id": "opp:1", "title": "Add a parser",
                       "proposed_module": "src/parser.py"})
```

No function calls a model, opens a socket or starts a process.
