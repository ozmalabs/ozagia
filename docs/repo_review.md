# Whole-repository review

`ozagia review-repo` (`ozagia.core.repo_review`) reviews the checkout as it
is, not a diff. It covers architecture, tests, code health, churn and
ownership, docs, security hygiene, operations sources and dependency
manifests, and writes a packet of typed findings that says what it checked
and what it could not check.

It is deterministic. It makes no model calls and no network calls, and it
does not write to the repository. It reads the tracked files (`git ls-files`),
`git log`, the graph snapshot (`.ozagia/graph.json`), the test record
(`.ozagia/working_set/tests.json`) and the edit journal
(`.ozagia/journal.jsonl`). For a review of a commit range, see
[code_review.md](code_review.md).

A finding is evidence for a person or an agent to check, not a verdict:
`truth_assessed` is always false.

## Usage

```sh
ozagia review-repo [--root R] [--json] [--out FILE]
                   [--section NAME ...] [--compare PREVIOUS.json]
                   [--fail-on blocking|major|never] [--index]
                   [--graph PATH] [--budget-s 60] [--save] [--model-pass]
```

- Output is text by default; `--json` prints the packet. `--out` also writes
  the output atomically; a path ending in `.json` always gets the JSON packet.
- `--section` (repeatable) runs only the named sections: `architecture`,
  `tests`, `health`, `churn`, `docs`, `security`, `ops`, `dependencies`.
- `--compare` takes an earlier packet and reports which finding ids are new,
  resolved or unchanged (see below).
- `--fail-on` defaults to `blocking`. The exit status is 1 when a finding is
  at or above that severity, 0 otherwise, and 2 for a bad root, a git failure,
  an unreadable `--compare` packet or a refused preflight. No check currently
  reports `blocking`, so the default only fails on future blocking checks;
  use `--fail-on major` in CI.
- `--index` builds or refreshes the graph snapshot first, the same build as
  `ozagia index`. This writes only to the state directory.
- `--budget-s` bounds the wall-clock time of the sections (default 60 s).
  It must be a finite number >= 0; `nan` or `inf` exits 2.
- `--save` also writes the packet the way the MCP tool does (below): a
  timestamped packet under `.ozagia/reviews/` and `latest.json`, both
  atomic. The path is printed after the output (on stderr with `--json`, so
  stdout stays one JSON document).
- `--model-pass` writes a compact packet to `.ozagia/reviews/compact.json`
  for model reviewers. The CLI never calls a model; see "Model pass".

The MCP tool `dev_review_repo` runs the same review (arguments `sections`,
`compare_latest`, `budget_s` up to 300, `max_findings` up to 50), writes the
full packet to `.ozagia/reviews/repo_review-<time>-<head>.json` and
`.ozagia/reviews/latest.json` (atomic writes), and returns a summary: counts
by severity and section, graph state, truncation, the top findings and the
paths of the written packet. Each top finding has a `goal` field: the id
of the goal that tracks it (`goal_from_review {finding_id}` makes one from
`latest.json`), or null. `compare_latest: true` compares with the
previous `latest.json` before it is replaced. A non-finite `budget_s` is
refused. Only the newest 20 timestamped packets are kept;
`latest.json` and `compact.json` are never pruned.

## The graph

The graph is used only when it is fresh: its recorded root and HEAD match the
checkout and its working-tree digest matches. A missing or stale graph is
reported in `packet.graph` (`freshness`, `reason`), and the checks that need
it are listed in `not_checked` with that reason instead of running on old
edges. The checks that do not need it still run. `--index` (or `ozagia
index`) rebuilds the graph; an indexing failure is recorded in
`graph.indexed` and the review continues without the graph.

| needs a fresh graph | runs without it |
|---|---|
| import and package cycles, hubs, layering | failing tests from the record |
| untested modules from test edges plus test names and imports (otherwise names and imports only, Python only, low confidence) | large files, TODO density |
| untested public functions | churn hotspots (test gap by file name) |
| large functions, unreferenced symbols | docs, security, ops, dependencies |

## Sections and checks

Every section lists the checks it ran (`checked`) and the ones it did not
(`not_checked`, with a reason). The thresholds are module constants in
`core/repo_review.py`.

### architecture

- **import_cycle** (minor): strongly connected components of the `imports`
  edges between tracked source files.
- **package_cycle** (major): cycles between top-level packages. A package is
  the first directory below the directory that the modules of a top-level
  tree share (for modules src/pkg/a/x.py and src/pkg/b/y.py, the packages
  are src/pkg/a and src/pkg/b). A module directly in that directory belongs
  to no package, so its cycles are reported only as import cycles. The id
  is a hash of the package names, so it stays the same when the import
  links between the packages change; the link files are the keys and
  evidence.
- **hub_module** (note): the ten files with the highest PageRank over the
  import graph are listed in `data.hubs`; a hub imported by 10 or more modules
  is a finding.
- **layer_violation** (major): with `[review] layers` in the project
  configuration, a file of a lower layer that imports a file of a higher
  layer. Layers are path prefixes, lowest first:

  ```toml
  [review]
  layers = ["src/pkg/core", "src/pkg/services", "src/pkg/cli"]
  ```

### tests

- **failing_test** (major): tests failing in the test record of earlier
  pytest runs. The review does not run tests. Without a readable record
  (`.ozagia/working_set/tests.json`) the check is listed in `not_checked`.
- **untested_module** (minor): a source module with symbols that has no
  `tests_module`, `mentions_file` or `has_test` edge, no tracked
  `test_<name>.py` or `<name>_test.py`, and no tracked Python test that
  imports it by its dotted name (`import pkg.mod`, `from pkg import mod`,
  `from pkg.mod import f`). The three sources are combined because the
  graph has no edge for some import forms. Confidence is medium with a
  fresh graph. Without one, the population is tracked Python files only and
  only names and imports are compared (low confidence); the check records
  `modes: {untested_modules: graph|filename}`.
- **untested_symbol** (note): a public top-level function of a tested module
  whose name appears in none of its covering test files. A test can still
  exercise it indirectly.
- `data.packages` gives the test-to-source ratio per directory.

Entry points (`__init__.py`, `__main__.py`, `conftest.py`, `setup.py`, ...,
files under `scripts/`, `tools/`, `bin/`) and fixture or example trees are
not expected to have tests.

### health

- **large_file** (minor): source files over 1000 lines.
- **large_function** (minor): functions whose symbol span is over 100 lines.
- **todo_density** (note): five or more TODO/FIXME/XXX markers in a file.
- **unreferenced_symbol** (minor, low confidence): a Python symbol whose name
  occurs only at its own definition(s) across tracked source, test and config
  files. This is name matching, not call resolution: a name in a string or a
  config file counts as a reference, so `__all__` entries and `pyproject.toml`
  entry points are not reported, but a getattr with a computed name, a plugin
  loader or an external caller is not seen. Dunder and framework-called names
  (`test*`, `visit_*`, `on_*`, `handle_*`, `main`, ...), decorated symbols,
  methods of subclasses (possible overrides), tests, entry points, fixtures
  and non-Python files are excluded and counted in `not_checked`.

### churn

- **churn_untested** (major): one of the ten files changed in the most
  commits over the last 90 days (at least 3 commits) that is a source module
  with no test, found the same way as for `untested_module` (mode recorded
  as `modes.hotspots`).
- **many_sessions** (note): a file edited by three or more agent sessions in
  the edit journal tail (`core.owners`). Without a journal this check is not
  run.

### docs

- **stale_doc_reference** (minor): in tracked Markdown and reStructuredText
  files, a relative link, a code span naming a path, a `path.py::name` span,
  or a `pkg.module.name` span that does not resolve. Paths must be tracked
  files or directories, from the doc's directory or the root, and must stay
  inside the root. For `path.py::name` the name is looked up in the in-root
  file the path resolved to. One finding per doc lists the failing
  references.
- Not checked: fenced and indented code blocks, URLs, code spans whose first
  path component is not in the repository (`.codex/config.toml`), directory
  spans, and docs under `plans/`, `attempts/`, `history/`, `archive/`,
  `results/`, `adr/` and changelogs, which record past or planned paths.

### security

Security hygiene only, not a security audit: no secret scanning of file
contents, no dependency advisories, no data-flow analysis.

- **committed_env_file** (major): a tracked `.env`, `.env.*` or `*.env`
  file (`prod.env`), except templates: a name ending in `.example`,
  `.sample`, `.template`, `.dist`, ..., or with such a part (`example.env`).
- **sensitive_file** (major): a tracked file named like a key or credential
  (`id_rsa`, `id_ed25519`, `.npmrc`, `credentials.json`, `*.pem`, `*.key`,
  `*.p12`, ...). A `.pem` may be a public certificate.
- **dangerous_construct** (minor): the `code_review` token table
  (`eval(`, `exec(`, `os.system(`, `shell=True`, `pickle.loads(`,
  `marshal.loads(`, `yaml.load(`) matched at word boundaries in source lines
  that are not comments. One finding per file. A `yaml.load(` call with an
  explicit `Loader=` argument is not flagged unless the loader is
  `yaml.Loader`, `yaml.UnsafeLoader` or `yaml.FullLoader`.
- Files in fixture, test-data and example trees (`fixtures/`, `testdata/`,
  `examples/`, ...) are not checked; the excluded files are listed in
  `not_checked`.
- `data.sensitive_paths` lists source paths with security words (`auth`,
  `secret`, `tls`, ...) as review focus, not as findings.

### ops

Reads the tracked operations sources the ops graph parses (compose files,
Containerfiles, quadlets, systemd units) with `core.ops_graph`, so finding keys
are the ops graph's keys (`ops:compose/<file>#service/<name>`). Only declared
sources are read; no container or unit state is queried.

- **unpinned_image** (minor): a compose service, quadlet or build stage image
  without a tag or digest, or tagged `latest`.
- **published_port** (minor on all interfaces, note on loopback).
- **no_restart_policy** (minor): a compose service without `restart` or
  `deploy.restart_policy`, or a quadlet without `[Service] Restart=`.
- **no_healthcheck** (note): a compose service without `healthcheck`, or a
  quadlet without `HealthCmd=`. The image may define its own `HEALTHCHECK`.
- Compose restart and health checks need PyYAML (the `iac-yaml` extra);
  without it they are listed in `not_checked`.
- Sources in fixture and example trees are not read; they are listed in
  `not_checked` (`ops_source`).

### dependencies

- Declared dependencies from `pyproject.toml` (project, optional, dependency
  groups, Poetry), `package.json`, `Cargo.toml`, `go.mod` and
  `requirements*.txt` are listed in `data.manifests`.
- **missing_lock_file** (minor): a manifest with dependencies and no tracked
  lock file (`uv.lock`, `poetry.lock`, `package-lock.json`, `yarn.lock`,
  `Cargo.lock`, `go.sum`, ...) in its directory or a parent. A requirements
  file whose every line is pinned with `==` counts as its own lock. Libraries
  often leave lock files out on purpose.

## Packet

Schema `ozagia.repo_review.packet.v1`:

```text
schema, schema_version (1), generator ("ozagia.repo_review"), root,
head_commit, branch, generated_at (UTC, microseconds), files
tree           working-tree fingerprint at the start of the review
               (core.fingerprint: head, dirty {path: blob}, unhashed,
               overflow, status), or null outside git
skipped_files  {path: reason}            tracked symlinks and paths outside
                                         the root, never read
preflight      {ok, checks}             root, HEAD, conflicts, resource admission
resources      admission report (core.resources, key "repo_review")
graph          {path, available, freshness, usable, snapshot_head,
                snapshot_root, generated_at, nodes, edges, reason, indexed}
sections_requested  [names]
sections       {name: {status ok|truncated|skipped, checked, not_checked,
                truncated, truncated_reason budget|cap, findings, found,
                dropped {type: n}, ids, ids_by_type, id_files {id: [files]},
                modes {check: graph|filename},
                skipped_files {check: {file: reason}}, data, elapsed_s}}
findings       [finding]                 sorted by severity, section, type
summary        {findings, blocking, major, minor, note, by_section}
checked        ["section.check", ...]
not_checked    [{section, check, reason}]
truncated      {any, sections {name: reason}}
budget         {budget_s, elapsed_s, exhausted}
limits         thresholds and caps used
comparison     null or {new, resolved, unchanged, resolved_findings,
                not_compared, held, root_mismatch, previous_root,
                previous_head, previous_generated_at, previous}
model_review   {status not_run|prepared|injected, ...}
security_note, truth_assessed (false)
```

A finding:

```text
id               "RR-" + 16 hex of sha256([type, sorted keys])
section, check, type, severity (blocking|major|minor|note), message
keys             graph keys the finding is about: file:, sym:, test:, ops:
file             the first file named by the keys
evidence         [text], at most 10
confidence       high|medium|low, with confidence_note
occurrences      how many matches were merged into this finding
source           "deterministic"
suggested_goal   {title, targets (= keys), criteria [{kind, args}]}
```

Criteria use the goal system's kinds and argument shapes. A suggested goal
never contains `review_resolved`: the goal system adds it when it converts a
finding, and checks it with `finding_state` (below). Some goals add a direct
check:

| finding | criterion |
|---|---|
| failing_test | `test_passes {key}` |
| committed_env_file, sensitive_file | `file_absent {key}` |
| unreferenced_symbol | `symbol_absent {key}`, titled "Remove ..." |
| unpinned_image | `ops_has {key, attr: "image" (a build stage: "base_image"), pinned: true}` |

`ops_has` takes `{key, attr?, value?, pinned?}`; `pinned: true` means the
attribute is an image reference with a digest or a tag other than `latest`
(`core.ops_graph.image_pinned`, the same predicate `unpinned_image` uses).
Ops findings keep their `ops:` keys as targets; the goal system resolves
them with `declared_ops_nodes(root)`, which parses the tracked ops sources
through the same `core.ops_graph.iter_ops_sources` loop as the ops section,
since the graph built by `--index` has no `ops:` nodes. The test
`test_every_suggested_goal_meets_the_contract` checks every finding type
against a copy of this argument table, and
`tests/test_review_goals.py` converts one finding of every type of a real
review into a goal with `ozagia goal from-review`.

The goal system resolves a finding only from a later packet that
`review_repo` wrote (`generator`) for the same root; it compares the files
the goal is about against the packet's `tree` to decide whether the packet
still describes them (`docs/goals.md`, "Review findings").

Matches with the same type and keys are merged into one finding (for example
all dangerous constructs of one file), so the id stays the same when lines
move.

## Comparing packets

`--compare previous.json` (or `compare_packets(current, previous)`) reports
finding ids that are new, resolved or unchanged. An id is reported as
resolved only when the check that produces it covered the same population
in both packets:

- The check ran to completion in both packets. A check skipped in either (a
  stale graph, a section left out, the budget, a missing test record) puts
  its type in `not_compared`.
- The check ran in the same mode. `untested_modules` and `hotspots` record
  `graph` or `filename` in `sections.<name>.modes`; a change puts the type
  in `not_compared`.
- None of the finding's files (`id_files`) was skipped by the current run:
  too large, binary, unreadable or unparseable files are recorded per check
  in `skipped_files`, and tracked symlinks in the packet's
  `skipped_files`. Such ids are listed in `held` with the reason.
- Both packets are for the same root. Otherwise every type is in
  `not_compared` and `root_mismatch` is true.

Sections keep the ids of findings dropped by the cap, so a capped section
still compares fully.

### finding_state

`finding_state(packet, finding_id, *, type=None, keys=None)` answers the
same question for one finding in one packet, for the goal system's
`review_resolved` criterion:

- `present`: the id is in some section's `ids` (capped findings included).
- `absent`: `type` is given, its check is in the section's `checked`, the
  check's mode (if any) is `graph`, and the check skipped no files (or,
  with `keys`, none of the files of those keys).
- `not_checked`: anything else, including no or an unknown `type`, a stale
  graph, filename mode and a missing test record.

The goal system stores the finding's `type` and `keys` when it creates a
`review_resolved` criterion and calls `finding_state` with them, so a
finding dropped by a section cap still counts as present.

## Limits

- Resource admission runs through `core.resources` before any review work
  (512 MB memory, 64 MB disk by default). A refusal exits 2 with the
  preflight report.
- Each section keeps at most 50 findings. Each finding type first gets an
  equal share of the cap, so one plentiful type does not hide the others;
  the rest are counted in `dropped`.
- The budget is checked between files and checks. A section cut short has
  `truncated_reason: "budget"`, and a section that did not start is
  `skipped`.
- Files over 1 MB or with NUL bytes are not read, and neither are tracked
  symlinks or paths that resolve outside the root (a symlink's target can
  be any file on the host). They are recorded in `skipped_files`.
- `--index` runs the graph build after admission; the build's own
  footprint is not covered by the 512 MB estimate.

## Model pass

`run_model_pass(packet, model_runner=..., models=[...], roles=[...])` builds
the compact packet (`compact_packet`, schema `ozagia.repo_review.compact.v1`)
and runs `harness.review.run_review_matrix` with the injected runner. The
model findings are stored in `packet.model_review.findings`, each attributed
to its role and model (`reviewer`), with cited keys split into keys of the
packet and `unknown_keys`. They are never merged into the deterministic
findings. The CLI's `--model-pass` only writes the compact packet; live model
calls stay behind the existing gate:

```sh
OZAGIA_EVAL_LIVE=1 python -m ozagia.harness.review --live \
  --packet .ozagia/reviews/compact.json --plan plan.json --base-url URL
```
