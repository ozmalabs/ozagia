# Environments, deterministic failure classes and per-component timing

Date: 17 Nov 2026. Status: implemented, verified 2026-11-24. Project environment
discovery is in `core/environments.py`, failure classes in `core/failures.py`
and component timings in `src/ozagia/eval/timing.py`, with
`tests/test_environments.py`, `tests/test_failures.py` and
`tests/test_eval_timing.py`; usage docs are [`docs/environments.md`](../environments.md)
and [`docs/eval_agent.md`](../eval_agent.md). The W23 replay and validation on
the Ocelot tasks was not re-run for this audit, and no live model call is
claimed.

Source: the live Ocelot eval
(`docs/results/ocelot_live_eval_2026-11-17.md`). Four tasks "failed" because
the harness ran their checks with ozagia's interpreter, which lacks `numpy`;
the agents had fixed them, using a third interpreter that has it. Rerun with
Ocelot's `.venv`, every arm passed 8/8. Three lessons:

1. Which interpreter runs what is unmanaged. Nothing knew that a project had
   three Pythons, which had the test's imports, or that `numpy` was declared
   only in an optional requirements file.
2. The failure was knowable from the output (`ModuleNotFoundError` during
   collection) but was scored as a task failure. Failures should be
   classified deterministically, and an environment failure is not a task
   failure.
3. Total wall-clock is the wrong measure. A graph arm that saves 30 s of
   agent time and spends 35 s on a one-time index is not slower in any way
   that matters for a second task. Each component's time is needed, and
   pay-once costs must be shown apart from per-task costs.

Rules for every workstream are those of the usage-features plan: one
worktree per workstream under `~/ozmadev/ozagia-wt/`, stdlib-only core,
no model calls in tests, the full ruff and pytest gate, a semantic review
and a fix round before merge, and no edits to the main checkout.

## W20: project environments (`core/environments.py`)

Find a project's Python environments and what each provides, without
running project code beyond `python -c` probes of the interpreters found.

- Candidates, in order of evidence: `$VIRTUAL_ENV`; `.venv`, `venv`,
  `.venv-*` in the root (and the source repo's root for a worktree or clone
  without one); `uv.lock`/`poetry.lock`/`pdm.lock` (the tool's env if it
  exists); interpreters named in the project's command docs
  (`index.command_docs`); `python`/`python3` on PATH.
- For each: path, version, `sys.prefix`, whether it is a venv, and the
  distributions installed (`importlib.metadata`, top-level import names via
  `top_level.txt`/`RECORD`). Probes are bounded (timeout, output cap) and
  cached by interpreter path and mtime of its `site-packages`.
- Declared dependencies: reuse the manifest readers of `core/repo_review.py`
  (pyproject, requirements*, Poetry, package.json, Cargo.toml, go.mod), with
  which file declares each and whether that file is optional
  (`requirements-<extra>.txt`, optional-dependencies, groups).
- Imports: third-party top-level imports of each tracked Python file
  (stdlib list from `sys.stdlib_module_names`, first-party from the graph's
  modules and `import_roots`). For tests, also their transitive first-party
  imports' third-party imports.
- Answer `env_for(paths)`: the interpreters under which every third-party
  import of those files resolves, best first; and per import, which
  interpreters have it and which manifest declares it.
- Surfaces: `ozagia env` (list, `--for PATH`), MCP `dev_environment`, the
  graph (Environment nodes, `provides`/`imports_package`/`declares` edges,
  if cheap to add through the adapter registry; else a cached JSON under
  the state dir).
- Repo review `dependencies` section gains: `undeclared_import` (imported,
  declared nowhere), `optional_only_import` (imported by tests or core code
  but declared only in an optional file, the `numpy` case), and
  `test_env_gap` (no found interpreter can import a test's dependencies).
- Eval harness: `--python` defaults to `env_for(<the check's test files>)`
  in the source repo, not ozagia's interpreter; the chosen interpreter and
  why are recorded per run. `run` runs `validate` on the selected tasks
  first and refuses to start (exit 2, the reasons) when a check does not
  pass on base or does not fail after the break, unless `--skip-validate`.

## W21: deterministic failure classes (`core/failures.py`)

One classifier for pytest (and generic command) results, used by the eval
harness, the daemon's test record and goals.

- Input: exit code, timeout flag, stdout/stderr (bounded), the command, and
  optionally the interpreter and the environment model (W20).
- Classes, decided by rules in order, each with the matched evidence:
  `harness` (the runner itself failed: interpreter missing, command not
  found, cwd missing), `timeout`, `environment` (`ModuleNotFoundError` /
  `ImportError` for a module that is third-party, `No module named pytest`,
  missing executables, `pytest: error: unrecognized arguments` from a
  missing plugin), `collection` (syntax error or import error of a
  first-party module, fixture not found, no tests collected for a named
  node id), `assertion` (test failures with assertion output), `error`
  (exceptions in test bodies or setup that are not assertions), `passed`.
- An `environment` result names the missing module, the interpreter used,
  the manifests that declare the module (or none) and the interpreters that
  have it (W20, when available).
- Eval: every run gets `failure_class` and `failure_evidence`. Runs whose
  check failed with `harness` or `environment` are `invalid`, excluded from
  success rates and listed separately in the report with the reason. The
  agent's own last pytest result in the transcript is classified too and
  shown next to the check's, so "agent saw green, check saw red" is
  visible.
- Daemon and test record: captured pytest runs store the class (no output
  text, as now). An `environment` run is not deciding evidence for goals:
  `test_passes`/`test_fails` give `unknown` with the class and module. The
  after-command `[ozagia]` line names the missing module and, when known,
  the interpreter that has it.

## W22: per-component timing (eval report)

The report breaks each run's time into components and separates pay-once
costs from per-task costs.

- Components, from the data the harness already has (driver JSON events
  with timestamps, tool `time.start/end`, the daemon journal's per-op `ms`,
  prep and check timings):
  - setup: workdir creation, plugin install, daemon start (pay-once per
    session);
  - index: graph build and tag cache (pay-once per repo revision; reported
    as cold, plus the repeat cost a later session on the same revision
    pays instead of a build (snapshot load + refresh + tag cache), measured
    once per run root);
  - model: time from each step start to its first tool call or finish
    (prefill plus generation), split into prefill and generation when the
    driver reports token timing, else total; with uncached and cached
    input tokens;
  - tools: per tool name, time in the built-in versus time in ozagia (the
    daemon's handled `ms`), and daemon overhead on passthrough calls;
  - agent idle/other: the rest of the driver wall;
  - check: the task check (not counted against the agent).
- Report: per arm, mean of each component; a "per task" total that
  excludes pay-once components; and an amortized view for N tasks on the
  same revision (`(index cold + (N - 1) * index repeat) / N + setup +
  per-task`; setup is per session, every task being its own session), N
  given by `--amortize` (default
  the number of tasks run). The headline compares per-task time and
  amortized time, never the plain sum.
- Raw component timings are in the JSON per run, so other views can be
  computed later.

## Order

W20, W21 and W22 run in parallel. W21 depends on W20 only for the optional
"which interpreter has it" detail, through a small interface
(`environments.who_has(module) -> list`) that W21 calls if importable and
tests with a stub. W22 is independent. A short W23 step wires W21's
environment detail to W20's real model, re-runs `validate` on the Ocelot
tasks with the harness default interpreter (expect 8/8 ok without
`--python`), and replays the earlier eval logs through the new report to
check the component breakdown against them.
